"""Persistent task metadata for restricted OTC backtests."""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from .otc_backtest import (INDICES, STRUCTURES, parameter_definitions,
                           validate_partial_terms, validate_request)
from .otc_pricing import (PARAMETRIC_STRUCTURE, pricing_parameter_definitions,
                          pricing_reference_data, validate_parametric_request)


RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PRODUCT_STATUSES = ("草稿", "存续", "已敲出", "到期结束", "提前终止", "已结束原因待补", "已作废")
EVENT_TYPES = ("生效", "派息确认", "敲入确认", "敲出确认", "到期确认", "提前终止", "更正备注")
EVENT_STATUS = {"生效": "存续", "敲出确认": "已敲出", "到期确认": "到期结束", "提前终止": "提前终止"}
PRODUCT_FIELDS = {"name", "strategy_name", "structure", "index_code", "notional",
                  "start_date", "terms", "notes", "reference", "performance_hurdle",
                  "performance_fee_rate", "return_as_of_date", "valuation_amount",
                  "cumulative_distributions", "template_id", "template_revision",
                  "end_date", "legacy_annual_return"}
SETTLED_STATUSES = ("已敲出", "到期结束", "提前终止", "已结束原因待补")
TEMPLATE_STATUSES = ("active", "void")
ATTACHMENT_CATEGORIES = ("产品合同", "投资者名单", "公告", "回测报告", "宣传材料", "其他")


class TaskConflictError(ValueError):
    pass


class RevisionConflictError(ValueError):
    pass


class OtcStore:
    def __init__(self, project_root):
        self.project_root = os.path.abspath(project_root)
        self.root = os.path.join(self.project_root, "otc_derivatives_data")
        self.db_path = os.path.join(self.root, "otc.sqlite3")
        self.runs_root = os.path.join(self.root, "runs")
        self.pricing_runs_root = os.path.join(self.root, "pricing_runs")
        self.attachments_root = os.path.join(self.root, "attachments")
        self.attachment_files = os.path.join(self.attachments_root, "files")
        self.attachment_trash = os.path.join(self.attachments_root, "trash")
        os.makedirs(self.runs_root, exist_ok=True)
        os.makedirs(self.pricing_runs_root, exist_ok=True)
        os.makedirs(self.attachment_files, exist_ok=True)
        os.makedirs(self.attachment_trash, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    result_path TEXT,
                    excel_path TEXT,
                    pdf_path TEXT
                );
                CREATE INDEX IF NOT EXISTS backtest_runs_created ON backtest_runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS pricing_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    reference_json TEXT NOT NULL,
                    product_snapshot_json TEXT,
                    product_id TEXT,
                    product_revision INTEGER,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    result_path TEXT,
                    actor TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pricing_runs_created ON pricing_runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS otc_products (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, name TEXT NOT NULL,
                    strategy_name TEXT, structure TEXT, index_code TEXT, notional REAL,
                    start_date TEXT, status TEXT NOT NULL, terms_json TEXT NOT NULL,
                    reference_json TEXT NOT NULL, notes TEXT, import_batch_id TEXT,
                    source_row INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    created_by TEXT NOT NULL, updated_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS otc_products_filter ON otc_products(status,structure,index_code,start_date);
                CREATE TABLE IF NOT EXISTS otc_events (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    event_date TEXT NOT NULL, values_json TEXT NOT NULL, created_at REAL NOT NULL,
                    created_by TEXT NOT NULL, FOREIGN KEY(product_id) REFERENCES otc_products(id)
                );
                CREATE INDEX IF NOT EXISTS otc_events_product ON otc_events(product_id, event_date, created_at);
                CREATE TABLE IF NOT EXISTS otc_import_batches (
                    id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL UNIQUE, source_name TEXT NOT NULL,
                    worksheet TEXT NOT NULL, row_count INTEGER NOT NULL, imported_at REAL NOT NULL,
                    imported_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otc_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, action TEXT NOT NULL,
                    before_json TEXT, after_json TEXT, created_at REAL NOT NULL, actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otc_backtest_templates (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL, request_json TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    created_by TEXT NOT NULL, updated_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS otc_templates_updated ON otc_backtest_templates(status,updated_at DESC);
                CREATE TABLE IF NOT EXISTS otc_attachment_blobs (
                    sha256 TEXT PRIMARY KEY, stored_name TEXT NOT NULL,
                    extension TEXT NOT NULL, size INTEGER NOT NULL,
                    text_content TEXT NOT NULL, extraction_status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otc_attachments (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, category TEXT NOT NULL,
                    sha256 TEXT NOT NULL, original_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES otc_products(id),
                    FOREIGN KEY(sha256) REFERENCES otc_attachment_blobs(sha256)
                );
                CREATE INDEX IF NOT EXISTS otc_attachments_product
                    ON otc_attachments(product_id,active,updated_at DESC);
                CREATE TABLE IF NOT EXISTS otc_field_audits (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
                    status TEXT NOT NULL, attachment_hashes_json TEXT NOT NULL,
                    provider TEXT, model TEXT, result_json TEXT, error TEXT,
                    created_at REAL NOT NULL, finished_at REAL, actor TEXT NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES otc_products(id)
                );
            """)
            self._ensure_column(connection, "backtest_runs", "pdf_path", "TEXT")
            self._ensure_column(connection, "pricing_runs", "product_snapshot_json", "TEXT")
            for name, definition in (
                    ("performance_hurdle", "REAL"), ("performance_fee_rate", "REAL"),
                    ("return_as_of_date", "TEXT"), ("valuation_amount", "REAL"),
                    ("cumulative_distributions", "REAL"), ("template_id", "TEXT"),
                    ("template_revision", "INTEGER"), ("end_date", "TEXT"),
                    ("legacy_annual_return", "REAL")):
                self._ensure_column(connection, "otc_products", name, definition)
            self._backfill_legacy_columns(connection)
            connection.execute("UPDATE backtest_runs SET status='interrupted', "
                               "finished_at=?, error='网页服务重启，任务已中断' "
                               "WHERE status IN ('queued','running')", (time.time(),))
            connection.execute("UPDATE pricing_runs SET status='interrupted', "
                               "finished_at=?, error='网页服务重启，任务已中断' "
                               "WHERE status IN ('queued','running')", (time.time(),))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _ensure_column(connection, table, name, definition):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if name not in columns:
            connection.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition))

    @staticmethod
    def _backfill_legacy_columns(connection):
        """Copy exact historical workbook values out of immutable raw_fields once."""
        rows = connection.execute(
            "SELECT id,reference_json,end_date,legacy_annual_return FROM otc_products "
            "WHERE import_batch_id IS NOT NULL AND (end_date IS NULL OR legacy_annual_return IS NULL)"
        ).fetchall()
        for row in rows:
            try:
                raw = (json.loads(row["reference_json"] or "{}").get("raw_fields") or [])
            except (TypeError, ValueError):
                continue
            end_date = row["end_date"]
            legacy_return = row["legacy_annual_return"]
            if end_date is None and len(raw) > 14 and raw[14] not in (None, "", "--", "存续"):
                candidate = str(raw[14]).strip().split(" ")[0]
                try:
                    time.strptime(candidate, "%Y-%m-%d")
                    end_date = candidate
                except ValueError:
                    pass
            if legacy_return is None and len(raw) > 16 and raw[16] not in (None, "", "--"):
                try:
                    candidate = float(raw[16])
                    if math.isfinite(candidate):
                        legacy_return = candidate
                except (TypeError, ValueError):
                    pass
            connection.execute(
                "UPDATE otc_products SET end_date=?,legacy_annual_return=? WHERE id=?",
                (end_date, legacy_return, row["id"]))

    @staticmethod
    def valid_run_id(run_id):
        return bool(RUN_ID_RE.match(str(run_id or "")))

    def submit(self, request):
        canonical = validate_request(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT id FROM backtest_runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if not running:
                running = connection.execute(
                    "SELECT id FROM pricing_runs WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
            if running:
                raise TaskConflictError("已有期权回测任务正在运行")
            run_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO backtest_runs(id,status,request_json,created_at) VALUES(?,?,?,?)",
                (run_id, "queued", json.dumps(canonical, ensure_ascii=False), time.time()))
            connection.commit()
        finally:
            connection.close()
        worker = threading.Thread(target=self._run_worker, args=(run_id,))
        worker.daemon = True
        worker.start()
        return self.get_run(run_id)

    def pricing_references(self):
        return pricing_reference_data(
            os.path.join(self.project_root, "market_data", "index_daily.json"),
            os.path.join(self.project_root, "market_data", "risk_daily.json"))

    def _pricing_product_source(self, request):
        if not request.get("product_id"):
            return None, None, None
        product_id = str(request.get("product_id"))
        if not self.valid_run_id(product_id):
            raise ValueError("产品ID无效")
        product = self.get_product(product_id, False)
        revision = int(request.get("product_revision") or 0)
        if product["revision"] != revision:
            raise RevisionConflictError("产品已经被其他页面修改，请刷新后重试")
        if product.get("structure") != PARAMETRIC_STRUCTURE:
            raise ValueError("仅经典雪球产品可复制到第一版定价研究")
        snapshot = {"id": product_id, "revision": revision, "name": product.get("name"),
                    "index_code": product.get("index_code"), "notional": product.get("notional"),
                    "terms": product.get("terms") or {}}
        return product_id, revision, snapshot

    def submit_pricing(self, request, actor="system"):
        product_id, product_revision, product_snapshot = self._pricing_product_source(request)
        canonical = validate_parametric_request(request)
        if product_snapshot:
            canonical["product_name"] = product_snapshot.get("name") or ""
        if not canonical.get("product_name"):
            canonical["product_name"] = "%s－%s－%s" % (
                STRUCTURES[PARAMETRIC_STRUCTURE],
                INDICES[canonical["index_code"]],
                time.strftime("%Y-%m-%d"))
        references = self.pricing_references()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT id FROM backtest_runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if not running:
                running = connection.execute(
                    "SELECT id FROM pricing_runs WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
            if running:
                raise TaskConflictError("已有场外衍生品数值任务正在运行")
            run_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO pricing_runs(id,status,request_json,reference_json,product_snapshot_json,product_id,"
                "product_revision,created_at,actor) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "queued", json.dumps(canonical, ensure_ascii=False),
                 json.dumps(references, ensure_ascii=False),
                 json.dumps(product_snapshot, ensure_ascii=False) if product_snapshot else None,
                 product_id, product_revision,
                 time.time(), actor))
            connection.commit()
        finally:
            connection.close()
        worker = threading.Thread(target=self._run_pricing_worker, args=(run_id,))
        worker.daemon = True
        worker.start()
        return self.get_pricing_run(run_id)

    @staticmethod
    def _date(value, field, optional=True):
        if optional and not value:
            return None
        try:
            parsed = time.strptime(str(value), "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError("%s必须是ISO日期" % field)
        return time.strftime("%Y-%m-%d", parsed)

    @staticmethod
    def _product_row(row):
        if row is None:
            return None
        item = dict(row)
        item["terms"] = json.loads(item.pop("terms_json") or "{}")
        item["reference"] = json.loads(item.pop("reference_json") or "{}")
        for key in ("created_at", "updated_at"):
            item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        item["lifecycle_group"] = ("存续" if item["status"] == "存续" else
                                   "了结" if item["status"] in SETTLED_STATUSES else "其他")
        item["returns"] = OtcStore._calculate_product_returns(item)
        return item

    @staticmethod
    def _calculate_product_returns(item):
        settled = item.get("status") in SETTLED_STATUSES
        effective_end = item.get("end_date") if settled else item.get("return_as_of_date")
        end_label = "终止日" if settled else "收益截止日"
        required = (("notional", "名义本金"), ("start_date", "起息日"),
                    ("valuation_amount", "最终结算金额" if settled else "当前估值金额"))
        missing = [label for field, label in required if item.get(field) in (None, "")]
        if not effective_end:
            missing.append(end_label)
        if missing:
            return {"status": "missing", "missing": missing, "fee_terms_missing":
                    item.get("performance_hurdle") is None or item.get("performance_fee_rate") is None,
                    "basis": "settlement" if settled else "valuation"}
        start = datetime.strptime(item["start_date"], "%Y-%m-%d")
        end = datetime.strptime(effective_end, "%Y-%m-%d")
        days = (end - start).days
        if days <= 0:
            return {"status": "invalid", "missing": [end_label + "必须晚于起息日"],
                    "fee_terms_missing": item.get("performance_hurdle") is None or item.get("performance_fee_rate") is None,
                    "basis": "settlement" if settled else "valuation"}
        notional = float(item["notional"])
        distributions = float(item.get("cumulative_distributions") or 0)
        gross_profit = float(item["valuation_amount"]) + distributions - notional
        gross_absolute = gross_profit / notional
        result = {"status": "gross_only", "missing": [], "days": days,
                  "actual_end_date": effective_end,
                  "basis": "settlement" if settled else "valuation",
                  "gross_profit": gross_profit, "gross_absolute_return": gross_absolute,
                  "gross_annualized_return": gross_absolute * 365.0 / days,
                  "fee_terms_missing": item.get("performance_hurdle") is None or item.get("performance_fee_rate") is None,
                  "performance_fee": None, "net_profit": None,
                  "net_absolute_return": None, "net_annualized_return": None}
        if not result["fee_terms_missing"]:
            hurdle_profit = notional * float(item["performance_hurdle"]) * days / 365.0
            fee = max(gross_profit - hurdle_profit, 0) * float(item["performance_fee_rate"])
            net_profit = gross_profit - fee
            result.update({"status": "complete", "hurdle_profit": hurdle_profit,
                           "performance_fee": fee, "net_profit": net_profit,
                           "net_absolute_return": net_profit / notional,
                           "net_annualized_return": net_profit / notional * 365.0 / days})
        return result

    def _validate_product(self, values, partial=False):
        if not isinstance(values, dict) or set(values) - PRODUCT_FIELDS:
            raise ValueError("产品字段不在固定清单")
        clean = {}
        if not partial or "name" in values:
            clean["name"] = str(values.get("name") or "").strip()
            if not clean["name"] or len(clean["name"]) > 200:
                raise ValueError("产品名称不能为空且不能超过200字")
        for field in ("strategy_name", "notes"):
            if not partial or field in values:
                value = str(values.get(field) or "").strip()
                if len(value) > 2000:
                    raise ValueError("%s内容过长" % field)
                clean[field] = value or None
        if not partial or "structure" in values:
            value = values.get("structure") or None
            if value is not None and value not in STRUCTURES:
                raise ValueError("期权结构不在固定清单")
            clean["structure"] = value
        if not partial or "index_code" in values:
            value = values.get("index_code") or None
            if value is not None and value not in INDICES:
                raise ValueError("标的指数不在固定清单")
            clean["index_code"] = value
        if not partial or "notional" in values:
            value = values.get("notional")
            if value in (None, ""):
                clean["notional"] = None
            else:
                number = float(value)
                if not math.isfinite(number) or number <= 0 or number > 1e12:
                    raise ValueError("名义本金超出允许范围")
                clean["notional"] = number
        if not partial or "start_date" in values:
            clean["start_date"] = self._date(values.get("start_date"), "起息日")
        if not partial or "end_date" in values:
            clean["end_date"] = self._date(values.get("end_date"), "正式结束日")
        if not partial or "return_as_of_date" in values:
            clean["return_as_of_date"] = self._date(values.get("return_as_of_date"), "收益截止日")
        for field, upper in (("performance_hurdle", 1), ("performance_fee_rate", 1)):
            if not partial or field in values:
                value = values.get(field)
                if value in (None, ""):
                    clean[field] = None
                else:
                    number = float(value)
                    if not math.isfinite(number) or number < 0 or number > upper:
                        raise ValueError("%s必须在0至1之间" % field)
                    clean[field] = number
        for field in ("valuation_amount", "cumulative_distributions"):
            if not partial or field in values:
                value = values.get(field)
                if value in (None, ""):
                    clean[field] = None
                else:
                    number = float(value)
                    if not math.isfinite(number) or number < 0 or number > 1e12:
                        raise ValueError("%s超出允许范围" % field)
                    clean[field] = number
        if not partial or "legacy_annual_return" in values:
            value = values.get("legacy_annual_return")
            if value in (None, ""):
                clean["legacy_annual_return"] = None
            else:
                number = float(value)
                if not math.isfinite(number) or number < -10 or number > 10:
                    raise ValueError("原表年化收益率超出允许范围")
                clean["legacy_annual_return"] = number
        if not partial or "template_id" in values:
            value = values.get("template_id") or None
            if value is not None and not self.valid_run_id(value):
                raise ValueError("模板ID无效")
            clean["template_id"] = value
        if not partial or "template_revision" in values:
            value = values.get("template_revision")
            clean["template_revision"] = int(value) if value not in (None, "") else None
            if clean["template_revision"] is not None and clean["template_revision"] < 1:
                raise ValueError("模板修订号无效")
        if not partial or "terms" in values:
            clean["terms"] = validate_partial_terms(values.get("terms") or {})
        for field in ("reference",):
            if not partial or field in values:
                value = values.get(field) or {}
                if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 20000:
                    raise ValueError("%s必须是受限JSON对象" % field)
                clean[field] = value
        return clean

    def _audit(self, connection, product_id, action, before, after, actor):
        connection.execute(
            "INSERT INTO otc_audit(product_id,action,before_json,after_json,created_at,actor) VALUES(?,?,?,?,?,?)",
            (product_id, action, json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None, time.time(), actor))

    def create_product(self, values, actor="system", status="草稿", import_meta=None):
        clean = self._validate_product(values)
        if status not in PRODUCT_STATUSES:
            raise ValueError("产品状态不在固定清单")
        product_id, now = str(uuid.uuid4()), time.time()
        meta = import_meta or {}
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO otc_products(id,revision,name,strategy_name,structure,index_code,notional,start_date,status,terms_json,reference_json,notes,import_batch_id,source_row,created_at,updated_at,created_by,updated_by,performance_hurdle,performance_fee_rate,return_as_of_date,valuation_amount,cumulative_distributions,template_id,template_revision,end_date,legacy_annual_return) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (product_id, 1, clean["name"], clean["strategy_name"], clean["structure"], clean["index_code"],
                 clean["notional"], clean["start_date"], status, json.dumps(clean["terms"], ensure_ascii=False),
                 json.dumps(clean["reference"], ensure_ascii=False), clean["notes"], meta.get("batch_id"),
                 meta.get("source_row"), now, now, actor, actor, clean["performance_hurdle"],
                 clean["performance_fee_rate"], clean["return_as_of_date"], clean["valuation_amount"],
                 clean["cumulative_distributions"], clean["template_id"], clean["template_revision"],
                 clean["end_date"], clean["legacy_annual_return"]))
            product = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            self._audit(connection, product_id, "create", None, product, actor)
            connection.commit()
        finally:
            connection.close()
        return product

    def import_products(self, digest, source_name, worksheet, rows, actor="migration"):
        if not re.match(r"^[0-9a-f]{64}$", str(digest or "")):
            raise ValueError("导入文件SHA-256无效")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM otc_import_batches WHERE source_sha256=?", (digest,)).fetchone():
                raise TaskConflictError("相同SHA-256文件已导入")
            batch_id, now = str(uuid.uuid4()), time.time()
            connection.execute("INSERT INTO otc_import_batches(id,source_sha256,source_name,worksheet,row_count,imported_at,imported_by) VALUES(?,?,?,?,?,?,?)",
                               (batch_id, digest, os.path.basename(source_name), worksheet, len(rows), now, actor))
            products = []
            for row in rows:
                clean = self._validate_product(row["values"])
                status = row["status"]
                if status not in PRODUCT_STATUSES:
                    raise ValueError("产品状态不在固定清单")
                product_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO otc_products(id,revision,name,strategy_name,structure,index_code,notional,start_date,status,terms_json,reference_json,notes,import_batch_id,source_row,created_at,updated_at,created_by,updated_by,performance_hurdle,performance_fee_rate,return_as_of_date,valuation_amount,cumulative_distributions,template_id,template_revision,end_date,legacy_annual_return) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (product_id, 1, clean["name"], clean["strategy_name"], clean["structure"], clean["index_code"], clean["notional"], clean["start_date"], status, json.dumps(clean["terms"], ensure_ascii=False), json.dumps(clean["reference"], ensure_ascii=False), clean["notes"], batch_id, row["source_row"], now, now, actor, actor, clean["performance_hurdle"], clean["performance_fee_rate"], clean["return_as_of_date"], clean["valuation_amount"], clean["cumulative_distributions"], clean["template_id"], clean["template_revision"], clean["end_date"], clean["legacy_annual_return"]))
                product = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
                self._audit(connection, product_id, "import", None, product, actor)
                products.append(product)
            connection.commit()
        finally:
            connection.close()
        return {"batch_id": batch_id, "source_sha256": digest, "worksheet": worksheet,
                "imported": len(products), "products": products}

    def get_product(self, product_id, include_history=True):
        if not self.valid_run_id(product_id):
            raise KeyError("发行产品不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise KeyError("发行产品不存在")
            item = self._product_row(row)
            if include_history:
                item["events"] = [dict(event) for event in connection.execute(
                    "SELECT id,event_type,event_date,values_json,created_at,created_by FROM otc_events WHERE product_id=? ORDER BY event_date,created_at", (product_id,)).fetchall()]
                for event in item["events"]:
                    event["values"] = json.loads(event.pop("values_json"))
                    event["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(event["created_at"]))
                item["audit"] = [dict(record) for record in connection.execute(
                    "SELECT action,before_json,after_json,created_at,actor FROM otc_audit WHERE product_id=? ORDER BY id DESC LIMIT 100", (product_id,)).fetchall()]
                for record in item["audit"]:
                    record["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record["created_at"]))
                latest_audit = connection.execute(
                    "SELECT * FROM otc_field_audits WHERE product_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (product_id,)).fetchone()
                item["latest_field_audit"] = (
                    self._field_audit_row(latest_audit) if latest_audit is not None else None)
        finally:
            connection.close()
        return item

    def list_products(self, page=1, page_size=50, filters=None):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        filters = filters or {}
        where, params = [], []
        if filters.get("q"):
            where.append("name LIKE ?")
            term = "%" + filters["q"][:100] + "%"; params.append(term)
        if filters.get("strategy_name"):
            value = str(filters["strategy_name"]).strip()
            if not value or len(value) > 200:
                raise ValueError("原策略筛选值无效")
            where.append("strategy_name=?"); params.append(value)
        for field in ("structure", "index_code", "status"):
            if filters.get(field):
                allowed = STRUCTURES if field == "structure" else INDICES if field == "index_code" else PRODUCT_STATUSES
                if filters[field] not in allowed:
                    raise ValueError("筛选值不在固定清单")
                where.append(field + "=?"); params.append(filters[field])
        lifecycle = filters.get("lifecycle")
        if lifecycle:
            if lifecycle == "存续":
                where.append("status='存续'")
            elif lifecycle == "了结":
                where.append("status IN (%s)" % ",".join("?" for _ in SETTLED_STATUSES)); params.extend(SETTLED_STATUSES)
            elif lifecycle == "其他":
                where.append("status IN ('草稿','已作废')")
            else:
                raise ValueError("生命周期筛选值不在固定清单")
        for field, operator in (("start_from", ">="), ("start_to", "<=")):
            if filters.get(field):
                where.append("start_date%s?" % operator); params.append(self._date(filters[field], field, False))
        for field, operator in (("return_from", ">="), ("return_to", "<=")):
            if filters.get(field):
                where.append("return_as_of_date%s?" % operator); params.append(self._date(filters[field], field, False))
        for field, operator in (("end_from", ">="), ("end_to", "<=")):
            if filters.get(field):
                where.append("end_date%s?" % operator); params.append(self._date(filters[field], field, False))
        return_state = filters.get("return_state")
        if return_state:
            boundary = "CASE WHEN status IN (%s) THEN end_date ELSE return_as_of_date END" % ",".join(
                "?" for _ in SETTLED_STATUSES)
            boundary_params = list(SETTLED_STATUSES)
            if return_state == "complete":
                where.append("notional IS NOT NULL AND start_date IS NOT NULL AND %s>start_date AND valuation_amount IS NOT NULL AND performance_hurdle IS NOT NULL AND performance_fee_rate IS NOT NULL" % boundary)
                params.extend(boundary_params)
            elif return_state == "gross_only":
                where.append("notional IS NOT NULL AND start_date IS NOT NULL AND %s>start_date AND valuation_amount IS NOT NULL AND (performance_hurdle IS NULL OR performance_fee_rate IS NULL)" % boundary)
                params.extend(boundary_params)
            elif return_state == "missing":
                where.append("(notional IS NULL OR start_date IS NULL OR %s IS NULL OR %s<=start_date OR valuation_amount IS NULL)" % (boundary, boundary))
                params.extend(boundary_params + boundary_params)
            else:
                raise ValueError("收益状态筛选值不在固定清单")
        clause = " WHERE " + " AND ".join(where) if where else ""
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM otc_products" + clause, params).fetchone()[0]
            all_rows = connection.execute("SELECT * FROM otc_products" + clause, params).fetchall()
        finally:
            connection.close()
        calculated = [self._product_row(row) for row in all_rows]
        sort_by = filters.get("sort_by") or "updated_at"
        sort_dir = filters.get("sort_dir") or "desc"
        sort_fields = {"name": lambda x: x.get("name"), "start_date": lambda x: x.get("start_date"),
                       "end_date": lambda x: x.get("end_date"),
                       "return_as_of_date": lambda x: x.get("return_as_of_date"),
                       "notional": lambda x: x.get("notional"), "updated_at": lambda x: x.get("updated_at"),
                       "gross_profit": lambda x: (x.get("returns") or {}).get("gross_profit"),
                       "gross_absolute_return": lambda x: (x.get("returns") or {}).get("gross_absolute_return"),
                       "net_profit": lambda x: (x.get("returns") or {}).get("net_profit"),
                       "net_absolute_return": lambda x: (x.get("returns") or {}).get("net_absolute_return")}
        if sort_by not in sort_fields or sort_dir not in ("asc", "desc"):
            raise ValueError("排序参数不在固定清单")
        getter = sort_fields[sort_by]
        present = [item for item in calculated if getter(item) is not None]
        missing_sort = [item for item in calculated if getter(item) is None]
        present.sort(key=lambda item: (getter(item), item.get("name") or "", item["id"]),
                     reverse=sort_dir == "desc")
        calculated = present + missing_sort
        offset = (page - 1) * page_size
        return {"items": calculated[offset:offset + page_size], "page": page, "page_size": page_size,
                "total": total, "matching_ids": [item["id"] for item in calculated],
                "sort_by": sort_by, "sort_dir": sort_dir,
                "aggregates": self._product_aggregates(calculated)}

    def aggregate_products(self, product_ids):
        if not isinstance(product_ids, list) or len(product_ids) > 2000:
            raise ValueError("勾选产品ID必须是最多2000项的数组")
        unique = []
        for product_id in product_ids:
            if not self.valid_run_id(product_id):
                raise ValueError("勾选产品ID无效")
            if product_id not in unique:
                unique.append(product_id)
        if not unique:
            return self._product_aggregates([])
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM otc_products WHERE id IN (%s)" % ",".join("?" for _ in unique),
                unique).fetchall()
        finally:
            connection.close()
        if len(rows) != len(unique):
            raise KeyError("勾选产品中存在未登记ID")
        return self._product_aggregates([self._product_row(row) for row in rows])

    @staticmethod
    def _product_aggregates(items):
        groups = {name: {"count": 0, "notional": 0.0} for name in ("存续", "了结", "草稿", "作废")}
        sums = {key: 0.0 for key in ("notional", "valuation_amount", "cumulative_distributions",
                                     "gross_profit", "performance_fee", "net_profit")}
        complete = gross_only = missing = 0
        gross_notional = net_notional = 0.0
        for item in items:
            group = ("存续" if item["status"] == "存续" else "了结" if item["status"] in SETTLED_STATUSES else
                     "作废" if item["status"] == "已作废" else "草稿")
            groups[group]["count"] += 1
            groups[group]["notional"] += float(item.get("notional") or 0)
            for key in ("notional", "valuation_amount", "cumulative_distributions"):
                sums[key] += float(item.get(key) or 0)
            returns = item.get("returns") or {}
            if returns.get("status") == "complete":
                complete += 1; gross_notional += float(item.get("notional") or 0); net_notional += float(item.get("notional") or 0)
            elif returns.get("status") == "gross_only":
                gross_only += 1; gross_notional += float(item.get("notional") or 0)
            else: missing += 1
            for key in ("gross_profit", "performance_fee", "net_profit"):
                sums[key] += float(returns.get(key) or 0)
        sums["gross_absolute_return"] = sums["gross_profit"] / gross_notional if gross_notional else None
        sums["net_absolute_return"] = sums["net_profit"] / net_notional if net_notional else None
        sums["gross_return_notional"] = gross_notional; sums["net_return_notional"] = net_notional
        return {"count": len(items), "groups": groups, "return_status": {"complete": complete,
                "gross_only": gross_only, "missing": missing}, "sums": sums}

    @staticmethod
    def _attachment_row(row, include_text=False):
        item = dict(row)
        for key in ("created_at", "updated_at"):
            item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        item["active"] = bool(item["active"])
        if not include_text:
            item.pop("text_content", None)
        item.pop("stored_name", None)
        return item

    @staticmethod
    def _safe_attachment_name(name):
        value = os.path.basename(str(name or "")).strip()
        if not value or value in (".", ".."):
            raise ValueError("附件文件名无效")
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)

    @staticmethod
    def _file_sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def add_attachment(self, product_id, source_path, filename, category, actor="system"):
        from .knowledge import MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, extract_text
        if category not in ATTACHMENT_CATEGORIES:
            raise ValueError("附件分类不在固定清单")
        self.get_product(product_id, False)
        safe_name = self._safe_attachment_name(filename)
        extension = os.path.splitext(safe_name)[1].lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("不支持的附件类型：%s" % extension)
        source_path = os.path.realpath(source_path)
        if not os.path.isfile(source_path):
            raise ValueError("附件文件不存在")
        size = os.path.getsize(source_path)
        if size <= 0 or size > MAX_FILE_SIZE:
            raise ValueError("附件必须大于0且不超过50MB")
        digest = self._file_sha256(source_path)
        stored_name = digest + extension
        target = os.path.join(self.attachment_files, stored_name)
        try:
            text = extract_text(source_path).strip()
            extraction_status = "ok" if text else "needs_ocr"
        except Exception:
            text, extraction_status = "", "failed"
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT a.*,b.extension,b.size,b.extraction_status,b.text_content,b.stored_name "
                "FROM otc_attachments a JOIN otc_attachment_blobs b ON b.sha256=a.sha256 "
                "WHERE a.product_id=? AND a.sha256=? AND a.category=? AND a.active=1",
                (product_id, digest, category)).fetchone()
            if existing:
                connection.rollback()
                return self._attachment_row(existing), True
            blob = connection.execute("SELECT 1 FROM otc_attachment_blobs WHERE sha256=?", (digest,)).fetchone()
            if blob is None:
                temporary = target + ".tmp-%s" % uuid.uuid4().hex
                shutil.copyfile(source_path, temporary)
                os.replace(temporary, target)
                connection.execute(
                    "INSERT INTO otc_attachment_blobs(sha256,stored_name,extension,size,text_content,extraction_status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (digest, stored_name, extension, size, text, extraction_status, now))
            attachment_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO otc_attachments(id,product_id,category,sha256,original_name,active,revision,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,1,1,?,?,?,?)",
                (attachment_id, product_id, category, digest, safe_name, now, now, actor, actor))
            row = connection.execute(
                "SELECT a.*,b.extension,b.size,b.extraction_status,b.text_content,b.stored_name "
                "FROM otc_attachments a JOIN otc_attachment_blobs b ON b.sha256=a.sha256 WHERE a.id=?",
                (attachment_id,)).fetchone()
            item = self._attachment_row(row)
            self._audit(connection, product_id, "attachment:create", None, item, actor)
            connection.commit()
            return item, False
        finally:
            connection.close()

    def list_attachments(self, product_id, include_inactive=False):
        self.get_product(product_id, False)
        clause = "" if include_inactive else " AND a.active=1"
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT a.*,b.extension,b.size,b.extraction_status,b.text_content,b.stored_name "
                "FROM otc_attachments a JOIN otc_attachment_blobs b ON b.sha256=a.sha256 "
                "WHERE a.product_id=?" + clause + " ORDER BY a.updated_at DESC",
                (product_id,)).fetchall()
        finally:
            connection.close()
        return [self._attachment_row(row) for row in rows]

    def get_attachment(self, product_id, attachment_id, include_text=True, include_inactive=False):
        if not self.valid_run_id(product_id) or not self.valid_run_id(attachment_id):
            raise KeyError("产品附件不存在")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT a.*,b.extension,b.size,b.extraction_status,b.text_content,b.stored_name "
                "FROM otc_attachments a JOIN otc_attachment_blobs b ON b.sha256=a.sha256 "
                "WHERE a.product_id=? AND a.id=?", (product_id, attachment_id)).fetchone()
        finally:
            connection.close()
        if row is None or (not include_inactive and not row["active"]):
            raise KeyError("产品附件不存在")
        return self._attachment_row(row, include_text)

    def attachment_file_path(self, product_id, attachment_id):
        item = self.get_attachment(product_id, attachment_id, False)
        connection = self._connect()
        try:
            row = connection.execute("SELECT stored_name FROM otc_attachment_blobs WHERE sha256=?",
                                     (item["sha256"],)).fetchone()
        finally:
            connection.close()
        path = os.path.realpath(os.path.join(self.attachment_files, row[0] if row else ""))
        if os.path.commonpath((self.attachment_files, path)) != self.attachment_files or not os.path.isfile(path):
            raise KeyError("产品附件文件不存在")
        return path, item

    def deactivate_attachment(self, product_id, attachment_id, expected_revision, actor="system"):
        before = self.get_attachment(product_id, attachment_id, False)
        if int(expected_revision or 0) != before["revision"]:
            raise RevisionConflictError("附件已被其他页面修改，请刷新后重试")
        connection = self._connect()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE otc_attachments SET active=0,revision=revision+1,updated_at=?,updated_by=? WHERE id=? AND product_id=? AND active=1",
                (now, actor, attachment_id, product_id))
            row = connection.execute(
                "SELECT a.*,b.extension,b.size,b.extraction_status,b.text_content,b.stored_name "
                "FROM otc_attachments a JOIN otc_attachment_blobs b ON b.sha256=a.sha256 WHERE a.id=?",
                (attachment_id,)).fetchone()
            after = self._attachment_row(row)
            self._audit(connection, product_id, "attachment:deactivate", before, after, actor)
            connection.commit()
        finally:
            connection.close()
        return after

    def create_field_audit(self, product_id, actor="system"):
        product = self.get_product(product_id, False)
        attachments = self.list_attachments(product_id)
        hashes = [item["sha256"] for item in attachments if item["category"] != "投资者名单"]
        audit_id, now = str(uuid.uuid4()), time.time()
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO otc_field_audits(id,product_id,product_revision,status,attachment_hashes_json,created_at,actor) VALUES(?,?,?,?,?,?,?)",
                (audit_id, product_id, product["revision"], "running", json.dumps(hashes), now, actor))
            connection.commit()
        finally:
            connection.close()
        return {"id": audit_id, "product_id": product_id, "product_revision": product["revision"],
                "status": "running", "attachment_hashes": hashes}

    def field_audit_context(self, product_id, expected_revision):
        product = self.get_product(product_id, False)
        if int(expected_revision or 0) != product["revision"]:
            raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
        attachments = []
        for item in self.list_attachments(product_id):
            if item["category"] == "投资者名单":
                continue
            detail = self.get_attachment(product_id, item["id"], True)
            attachments.append({key: detail.get(key) for key in
                                ("id", "category", "original_name", "sha256", "extraction_status", "text_content")})
        return product, attachments

    def finish_field_audit(self, audit_id, status, provider=None, model=None, result=None, error=None):
        if not self.valid_run_id(audit_id) or status not in ("completed", "failed"):
            raise ValueError("字段排查任务状态无效")
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE otc_field_audits SET status=?,provider=?,model=?,result_json=?,error=?,finished_at=? WHERE id=?",
                (status, provider, model, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 str(error or "")[:1000] or None, time.time(), audit_id))
            row = connection.execute("SELECT * FROM otc_field_audits WHERE id=?", (audit_id,)).fetchone()
            connection.commit()
        finally:
            connection.close()
        if row is None:
            raise KeyError("字段排查任务不存在")
        return self._field_audit_row(row)

    @staticmethod
    def _field_audit_row(row):
        item = dict(row)
        item["attachment_hashes"] = json.loads(item.pop("attachment_hashes_json") or "[]")
        item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        item.pop("result_json", None)
        for key in ("created_at", "finished_at"):
            if item.get(key):
                item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        return item

    def get_field_audit(self, audit_id):
        if not self.valid_run_id(audit_id):
            raise KeyError("字段排查任务不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM otc_field_audits WHERE id=?", (audit_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("字段排查任务不存在")
        return self._field_audit_row(row)

    def update_product(self, product_id, values, expected_revision, actor="system"):
        clean = self._validate_product(values, True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            if before is None:
                raise KeyError("发行产品不存在")
            if int(expected_revision or 0) != before["revision"]:
                raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
            merged = dict(before); merged.update(clean)
            revision, now = before["revision"] + 1, time.time()
            connection.execute("UPDATE otc_products SET revision=?,name=?,strategy_name=?,structure=?,index_code=?,notional=?,start_date=?,terms_json=?,reference_json=?,notes=?,updated_at=?,updated_by=?,performance_hurdle=?,performance_fee_rate=?,return_as_of_date=?,valuation_amount=?,cumulative_distributions=?,template_id=?,template_revision=?,end_date=?,legacy_annual_return=? WHERE id=?",
                               (revision, merged["name"], merged["strategy_name"], merged["structure"], merged["index_code"], merged["notional"], merged["start_date"], json.dumps(merged["terms"], ensure_ascii=False), json.dumps(merged["reference"], ensure_ascii=False), merged["notes"], now, actor, merged.get("performance_hurdle"), merged.get("performance_fee_rate"), merged.get("return_as_of_date"), merged.get("valuation_amount"), merged.get("cumulative_distributions"), merged.get("template_id"), merged.get("template_revision"), merged.get("end_date"), merged.get("legacy_annual_return"), product_id))
            after = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            self._audit(connection, product_id, "update", before, after, actor); connection.commit()
        finally:
            connection.close()
        return after

    def add_event(self, product_id, request, actor="system"):
        if not isinstance(request, dict) or set(request) - {"event_type", "event_date", "values", "expected_revision"}:
            raise ValueError("事件字段不在固定清单")
        event_type = request.get("event_type")
        if event_type not in EVENT_TYPES:
            raise ValueError("事件类型不在固定清单")
        event_date = self._date(request.get("event_date"), "事件日期", False)
        values = request.get("values") or {}
        if not isinstance(values, dict) or len(json.dumps(values, ensure_ascii=False)) > 10000:
            raise ValueError("事件内容必须是受限JSON对象")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            if before is None:
                raise KeyError("发行产品不存在")
            if int(request.get("expected_revision") or 0) != before["revision"]:
                raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
            event_id, now = str(uuid.uuid4()), time.time()
            connection.execute("INSERT INTO otc_events(id,product_id,event_type,event_date,values_json,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                               (event_id, product_id, event_type, event_date, json.dumps(values, ensure_ascii=False), now, actor))
            status = EVENT_STATUS.get(event_type, before["status"])
            revision = before["revision"] + 1
            end_date = event_date if event_type in ("敲出确认", "到期确认", "提前终止") else before.get("end_date")
            connection.execute("UPDATE otc_products SET status=?,end_date=?,revision=?,updated_at=?,updated_by=? WHERE id=?",
                               (status, end_date, revision, now, actor, product_id))
            after = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            self._audit(connection, product_id, "event:" + event_type, before, after, actor); connection.commit()
        finally:
            connection.close()
        return self.get_product(product_id)

    def void_product(self, product_id, expected_revision, actor="system"):
        product = self.get_product(product_id, False)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            if int(expected_revision or 0) != current["revision"]:
                raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
            connection.execute("UPDATE otc_products SET status='已作废',revision=revision+1,updated_at=?,updated_by=? WHERE id=?", (time.time(), actor, product_id))
            after = self._product_row(connection.execute("SELECT * FROM otc_products WHERE id=?", (product_id,)).fetchone())
            self._audit(connection, product_id, "void", product, after, actor); connection.commit()
        finally:
            connection.close()
        return after

    @staticmethod
    def _template_row(row):
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        source = item.pop("product_snapshot_json", None)
        item["source_product"] = json.loads(source) if source else None
        for key in ("created_at", "updated_at"):
            item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        return item

    def list_templates(self, include_void=False):
        connection = self._connect()
        try:
            clause = "" if include_void else " WHERE status='active'"
            rows = connection.execute("SELECT * FROM otc_backtest_templates" + clause +
                                      " ORDER BY updated_at DESC").fetchall()
        finally:
            connection.close()
        return {"items": [self._template_row(row) for row in rows], "total": len(rows)}

    def get_template(self, template_id):
        if not self.valid_run_id(template_id):
            raise KeyError("回测参数模板不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM otc_backtest_templates WHERE id=?", (template_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("回测参数模板不存在")
        return self._template_row(row)

    def create_template(self, name, request, actor="system"):
        title = str(name or "").strip()
        if not title or len(title) > 120:
            raise ValueError("模板名称不能为空且不能超过120字")
        canonical = validate_request(request)
        canonical.pop("product_id", None); canonical.pop("product_revision", None)
        canonical.pop("template_id", None); canonical.pop("template_revision", None)
        canonical.pop("product_name", None)
        template_id, now = str(uuid.uuid4()), time.time()
        connection = self._connect()
        try:
            connection.execute("INSERT INTO otc_backtest_templates(id,revision,name,status,request_json,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
                               (template_id, 1, title, "active", json.dumps(canonical, ensure_ascii=False), now, now, actor, actor))
            connection.commit()
        finally:
            connection.close()
        return self.get_template(template_id)

    def update_template(self, template_id, name, request, expected_revision, actor="system"):
        current = self.get_template(template_id)
        if current["revision"] != int(expected_revision or 0):
            raise RevisionConflictError("模板已被其他页面修改，请刷新后重试")
        title = str(name or "").strip()
        if not title or len(title) > 120:
            raise ValueError("模板名称不能为空且不能超过120字")
        canonical = validate_request(request)
        canonical.pop("product_id", None); canonical.pop("product_revision", None)
        canonical.pop("template_id", None); canonical.pop("template_revision", None)
        canonical.pop("product_name", None)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM otc_backtest_templates WHERE id=?", (template_id,)).fetchone()
            if row is None or row[0] != int(expected_revision or 0):
                raise RevisionConflictError("模板已被其他页面修改，请刷新后重试")
            connection.execute("UPDATE otc_backtest_templates SET revision=revision+1,name=?,request_json=?,updated_at=?,updated_by=? WHERE id=?",
                               (title, json.dumps(canonical, ensure_ascii=False), time.time(), actor, template_id))
            connection.commit()
        finally:
            connection.close()
        return self.get_template(template_id)

    def void_template(self, template_id, expected_revision, actor="system"):
        current = self.get_template(template_id)
        if current["revision"] != int(expected_revision or 0):
            raise RevisionConflictError("模板已被其他页面修改，请刷新后重试")
        connection = self._connect()
        try:
            connection.execute("UPDATE otc_backtest_templates SET status='void',revision=revision+1,updated_at=?,updated_by=? WHERE id=? AND revision=?",
                               (time.time(), actor, template_id, int(expected_revision)))
            connection.commit()
        finally:
            connection.close()
        return self.get_template(template_id)

    def product_backtest_request(self, product_id, revision, dates):
        product = self.get_product(product_id, False)
        if product["revision"] != int(revision or 0):
            raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
        if not product.get("structure") or not product.get("index_code"):
            raise ValueError("产品尚未补全可回测的结构化条款")
        request = dict(product.get("terms") or {})
        request.update({"structure": product["structure"], "index_code": product["index_code"],
                        "start_date": dates.get("start_date"), "end_date": dates.get("end_date"),
                        "product_id": product_id, "product_revision": product["revision"],
                        "product_name": product["name"]})
        if product.get("template_id") and product.get("template_revision"):
            request.update({"template_id": product["template_id"],
                            "template_revision": product["template_revision"]})
        return validate_request(request)

    def _run_worker(self, run_id):
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "valuation_app.otc_worker", "--run-id", run_id,
                 "--project-root", self.project_root],
                cwd=self.project_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1800, check=False)
            if completed.returncode != 0:
                connection = self._connect()
                try:
                    connection.execute(
                        "UPDATE backtest_runs SET status='failed',finished_at=?,error=? "
                        "WHERE id=? AND status IN ('queued','running')",
                        (time.time(), "回测子进程异常退出", run_id))
                    connection.commit()
                finally:
                    connection.close()
        except subprocess.TimeoutExpired:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE backtest_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                    (time.time(), "回测超过30分钟限制", run_id))
                connection.commit()
            finally:
                connection.close()

    def _run_pricing_worker(self, run_id):
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "valuation_app.otc_pricing_worker", "--run-id", run_id,
                 "--project-root", self.project_root],
                cwd=self.project_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1800, check=False)
            if completed.returncode != 0:
                connection = self._connect()
                try:
                    connection.execute(
                        "UPDATE pricing_runs SET status='failed',finished_at=?,error=? "
                        "WHERE id=? AND status IN ('queued','running')",
                        (time.time(), "定价子进程异常退出", run_id))
                    connection.commit()
                finally:
                    connection.close()
        except subprocess.TimeoutExpired:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE pricing_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                    (time.time(), "定价超过30分钟限制", run_id))
                connection.commit()
            finally:
                connection.close()

    def _row(self, row, include_request=True):
        if row is None:
            return None
        item = dict(row)
        if include_request:
            item["request"] = json.loads(item.pop("request_json"))
        else:
            item.pop("request_json", None)
        for key in ("created_at", "started_at", "finished_at"):
            if item.get(key) is not None:
                item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        return item

    def get_run(self, run_id):
        if not self.valid_run_id(run_id):
            raise KeyError("回测任务不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("回测任务不存在")
        return self._row(row)

    def list_runs(self, page=1, page_size=20):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size)).fetchall()
        finally:
            connection.close()
        return {"items": [self._row(row) for row in rows], "page": page,
                "page_size": page_size, "total": total}

    def result(self, run_id):
        run = self.get_run(run_id)
        if run["status"] != "completed" or not run.get("result_path"):
            return {"run": run}
        path = os.path.join(self.root, run["result_path"])
        with open(path, "r", encoding="utf-8") as handle:
            result = json.load(handle)
        result.pop("samples", None)
        return {"run": run, "result": result}

    def samples(self, run_id, page=1, page_size=50):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        run = self.get_run(run_id)
        if run["status"] != "completed" or not run.get("result_path"):
            raise ValueError("回测结果尚未生成")
        with open(os.path.join(self.root, run["result_path"]), "r", encoding="utf-8") as handle:
            rows = json.load(handle).get("samples", [])
        start = (page - 1) * page_size
        return {"items": rows[start:start + page_size], "page": page,
                "page_size": page_size, "total": len(rows)}

    def download_path(self, run_id, kind):
        run = self.get_run(run_id)
        key = "excel_path" if kind == "xlsx" else "pdf_path" if kind == "pdf" else "result_path"
        relative = run.get(key)
        if run["status"] != "completed" or not relative:
            raise KeyError("回测下载文件不存在")
        path = os.path.realpath(os.path.join(self.root, relative))
        if os.path.commonpath((self.root, path)) != self.root or not os.path.isfile(path):
            raise KeyError("回测下载文件不存在")
        return path

    def _pricing_row(self, row):
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        # Reference values are shown by the dedicated page endpoint, not copied
        # into every run response.  This also keeps internal audit data compact.
        item.pop("reference_json", None)
        item.pop("result_path", None)
        for key in ("created_at", "started_at", "finished_at"):
            if item.get(key) is not None:
                item[key] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(item[key]))
        return item

    def get_pricing_run(self, run_id):
        if not self.valid_run_id(run_id):
            raise KeyError("定价任务不存在")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM pricing_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("定价任务不存在")
        return self._pricing_row(row)

    def list_pricing_runs(self, page=1, page_size=20):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM pricing_runs").fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM pricing_runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size)).fetchall()
        finally:
            connection.close()
        return {"items": [self._pricing_row(row) for row in rows], "page": page,
                "page_size": page_size, "total": total}

    def pricing_result(self, run_id):
        run = self.get_pricing_run(run_id)
        if run["status"] != "completed":
            return {"run": run}
        path = self.pricing_download_path(run_id)
        with open(path, "r", encoding="utf-8") as handle:
            return {"run": run, "result": json.load(handle)}

    def pricing_download_path(self, run_id):
        run = self.get_pricing_run(run_id)
        connection = self._connect()
        try:
            row = connection.execute("SELECT result_path FROM pricing_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            connection.close()
        relative = row[0] if row else None
        if run["status"] != "completed" or not relative:
            raise KeyError("定价结果文件不存在")
        path = os.path.realpath(os.path.join(self.root, relative))
        root = os.path.realpath(self.pricing_runs_root)
        if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
            raise KeyError("定价结果文件不存在")
        return path

    def pricing_payload(self):
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id,revision,name,structure,index_code,notional,terms_json,status "
                "FROM otc_products WHERE structure=? AND status<>'已作废' "
                "ORDER BY updated_at DESC", (PARAMETRIC_STRUCTURE,)).fetchall()
        finally:
            connection.close()
        products = []
        for row in rows:
            item = dict(row); item["terms"] = json.loads(item.pop("terms_json") or "{}")
            products.append(item)
        return {"schema_version": 1,
                "model": {"status": "available", "structure": PARAMETRIC_STRUCTURE,
                          "version": "constant-vol-gbm-relative-v1",
                          "batch_count": 8, "paths_per_batch": 4096,
                          "disclaimer": "参数化理论定价研究，不是市场公允价值、交易报价或结算价。"},
                "structures": [{"code": "classic_snowball", "name": "经典雪球", "enabled": True},
                               {"code": "european_snowball", "name": "欧式雪球", "enabled": False},
                               {"code": "dcn", "name": "DCN", "enabled": False},
                               {"code": "dcn_snowball", "name": "DCN＋雪球组合", "enabled": False}],
                "indices": [{"code": code, "name": name} for code, name in INDICES.items()],
                "parameter_definitions": pricing_parameter_definitions(),
                "references": self.pricing_references(), "products": products,
                "recent_runs": self.list_pricing_runs(1, 20)["items"]}

    def module_payload(self):
        cache_path = os.path.join(self.project_root, "market_data", "index_daily.json")
        market = {"status": "missing", "updated_at": None, "indices": []}
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                cache = json.load(handle)
            market["status"] = "current" if cache.get("source") == "Wind Oracle数据库" else "missing"
            market["updated_at"] = cache.get("updated_at")
            for code, name in INDICES.items():
                item = (cache.get("indices") or {}).get(code) or {}
                points = item.get("points") or []
                market["indices"].append({"code": code, "name": name, "available": bool(points),
                                          "first_date": points[0].get("date") if points else None,
                                          "last_date": points[-1].get("date") if points else None})
        except (OSError, ValueError):
            pass
        ledger = self.list_products(1, 20)
        connection = self._connect()
        try:
            active = connection.execute("SELECT COUNT(*) FROM otc_products WHERE status='存续'").fetchone()[0]
            strategies = [row[0] for row in connection.execute(
                "SELECT DISTINCT strategy_name FROM otc_products WHERE strategy_name IS NOT NULL "
                "AND strategy_name<>'' ORDER BY strategy_name").fetchall()]
        finally:
            connection.close()
        return {"schema_version": 5, "structures": [
                    {"code": code, "name": name} for code, name in STRUCTURES.items()],
                "parameter_definitions": parameter_definitions(),
                "statuses": list(PRODUCT_STATUSES), "event_types": list(EVENT_TYPES),
                "attachment_categories": list(ATTACHMENT_CATEGORIES),
                "strategy_names": strategies,
                "market": market, "recent_runs": self.list_runs(1, 20)["items"],
                "templates": self.list_templates()["items"],
                "ledger": {"total": ledger["total"], "active": active,
                           "recent_products": ledger["items"]}}
