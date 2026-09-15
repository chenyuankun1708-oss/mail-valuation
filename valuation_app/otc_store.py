"""Persistent task metadata for restricted OTC backtests."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .otc_backtest import INDICES, STRUCTURES, validate_request


RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PRODUCT_STATUSES = ("草稿", "存续", "已敲出", "到期结束", "提前终止", "已结束原因待补", "已作废")
EVENT_TYPES = ("生效", "派息确认", "敲入确认", "敲出确认", "到期确认", "提前终止", "更正备注")
EVENT_STATUS = {"生效": "存续", "敲出确认": "已敲出", "到期确认": "到期结束", "提前终止": "提前终止"}
PRODUCT_FIELDS = {"name", "strategy_name", "structure", "index_code", "notional",
                  "start_date", "terms", "notes", "reference"}


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
        os.makedirs(self.runs_root, exist_ok=True)
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
                    excel_path TEXT
                );
                CREATE INDEX IF NOT EXISTS backtest_runs_created ON backtest_runs(created_at DESC);
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
            """)
            connection.execute("UPDATE backtest_runs SET status='interrupted', "
                               "finished_at=?, error='网页服务重启，任务已中断' "
                               "WHERE status IN ('queued','running')", (time.time(),))
            connection.commit()
        finally:
            connection.close()

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
        return item

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
                if number <= 0 or number > 1e12:
                    raise ValueError("名义本金超出允许范围")
                clean["notional"] = number
        if not partial or "start_date" in values:
            clean["start_date"] = self._date(values.get("start_date"), "起息日")
        for field in ("terms", "reference"):
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
                "INSERT INTO otc_products(id,revision,name,strategy_name,structure,index_code,notional,start_date,status,terms_json,reference_json,notes,import_batch_id,source_row,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (product_id, 1, clean["name"], clean["strategy_name"], clean["structure"], clean["index_code"],
                 clean["notional"], clean["start_date"], status, json.dumps(clean["terms"], ensure_ascii=False),
                 json.dumps(clean["reference"], ensure_ascii=False), clean["notes"], meta.get("batch_id"),
                 meta.get("source_row"), now, now, actor, actor))
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
                    "INSERT INTO otc_products(id,revision,name,strategy_name,structure,index_code,notional,start_date,status,terms_json,reference_json,notes,import_batch_id,source_row,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (product_id, 1, clean["name"], clean["strategy_name"], clean["structure"], clean["index_code"], clean["notional"], clean["start_date"], status, json.dumps(clean["terms"], ensure_ascii=False), json.dumps(clean["reference"], ensure_ascii=False), clean["notes"], batch_id, row["source_row"], now, now, actor, actor))
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
        finally:
            connection.close()
        return item

    def list_products(self, page=1, page_size=50, filters=None):
        if page < 1 or page_size < 20 or page_size > 200:
            raise ValueError("分页参数超出允许范围")
        filters = filters or {}
        where, params = [], []
        if filters.get("q"):
            where.append("(name LIKE ? OR strategy_name LIKE ?)")
            term = "%" + filters["q"][:100] + "%"; params.extend((term, term))
        for field in ("structure", "index_code", "status"):
            if filters.get(field):
                allowed = STRUCTURES if field == "structure" else INDICES if field == "index_code" else PRODUCT_STATUSES
                if filters[field] not in allowed:
                    raise ValueError("筛选值不在固定清单")
                where.append(field + "=?"); params.append(filters[field])
        for field, operator in (("start_from", ">="), ("start_to", "<=")):
            if filters.get(field):
                where.append("start_date%s?" % operator); params.append(self._date(filters[field], field, False))
        clause = " WHERE " + " AND ".join(where) if where else ""
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM otc_products" + clause, params).fetchone()[0]
            rows = connection.execute("SELECT * FROM otc_products" + clause + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                                      params + [page_size, (page - 1) * page_size]).fetchall()
        finally:
            connection.close()
        return {"items": [self._product_row(row) for row in rows], "page": page, "page_size": page_size, "total": total}

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
            connection.execute("UPDATE otc_products SET revision=?,name=?,strategy_name=?,structure=?,index_code=?,notional=?,start_date=?,terms_json=?,reference_json=?,notes=?,updated_at=?,updated_by=? WHERE id=?",
                               (revision, merged["name"], merged["strategy_name"], merged["structure"], merged["index_code"], merged["notional"], merged["start_date"], json.dumps(merged["terms"], ensure_ascii=False), json.dumps(merged["reference"], ensure_ascii=False), merged["notes"], now, actor, product_id))
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
            connection.execute("UPDATE otc_products SET status=?,revision=?,updated_at=?,updated_by=? WHERE id=?",
                               (status, revision, now, actor, product_id))
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

    def product_backtest_request(self, product_id, revision, dates):
        product = self.get_product(product_id, False)
        if product["revision"] != int(revision or 0):
            raise RevisionConflictError("产品已被其他页面修改，请刷新后重试")
        if not product.get("structure") or not product.get("index_code"):
            raise ValueError("产品尚未补全可回测的结构化条款")
        request = dict(product.get("terms") or {})
        request.update({"structure": product["structure"], "index_code": product["index_code"],
                        "start_date": dates.get("start_date"), "end_date": dates.get("end_date"),
                        "product_id": product_id, "product_revision": product["revision"]})
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
        key = "excel_path" if kind == "xlsx" else "result_path"
        relative = run.get(key)
        if run["status"] != "completed" or not relative:
            raise KeyError("回测下载文件不存在")
        path = os.path.realpath(os.path.join(self.root, relative))
        if os.path.commonpath((self.root, path)) != self.root or not os.path.isfile(path):
            raise KeyError("回测下载文件不存在")
        return path

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
        finally:
            connection.close()
        return {"schema_version": 2, "structures": [
                    {"code": code, "name": name} for code, name in STRUCTURES.items()],
                "statuses": list(PRODUCT_STATUSES), "event_types": list(EVENT_TYPES),
                "market": market, "recent_runs": self.list_runs(1, 20)["items"],
                "ledger": {"total": ledger["total"], "active": active,
                           "recent_products": ledger["items"]}}
