"""Fixed local knowledge sources, review queue, and evidence-backed graph.

The WeChat directory is an external, read-only source.  Browser requests never
carry a path: the only configured source is read from KNOWLEDGE_WECHAT_ROOT.
"""
from __future__ import unicode_literals

import calendar
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime

from .config import PRODUCTS, TOP_PRODUCT_IDS
from .knowledge import KnowledgeStore, MAX_FILE_SIZE, SUPPORTED_EXTENSIONS, extract_text, infer_metadata
from .labels import deduplicate_label_records, load_json_catalog, normalize_name


SOURCE_ID = "wechat"
SOURCE_LABEL = "微信文件目录"
RULE_VERSION = "wechat-rules-1"
CANDIDATE_STATUSES = {
    "待确认", "已批准", "已排除", "待OCR", "格式不支持", "敏感受限", "已入库", "提取失败"
}
SENSITIVE_WORDS = ("投资者名单", "身份证", "银行卡", "银行账户", "开户", "认购明细", "客户名单", "手机号")
RELEVANT_WORDS = ("私募", "FOF", "基金", "资产管理计划", "资管计划", "月报", "周报", "净值", "路演", "合同", "公告", "投资报告", "产品报告")
MODEL_FORBIDDEN = ("投资者名单", "认购明细", "客户名单")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
ID_RE = re.compile(r"(?<!\d)(?:\d{17}[0-9Xx]|\d{15})(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ACCOUNT_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

GRAPH_ENTITY_TYPES = {
    "文档", "顶层FOF", "底层私募产品", "管理人", "一级策略标签", "二级策略标签",
    "公司信息", "人员", "投资策略", "业绩记录", "产品条款", "风控信息"
}
PERSON_ROLE_TYPES = {"创始人", "合伙人", "基金经理", "研究员", "交易员", "员工"}
COMPANY_FACT_TYPES = {"公司简介", "成立时间", "管理规模", "股权结构", "机构背景"}
PERFORMANCE_METRICS = {"累计收益率", "年化收益率", "最大回撤", "波动率", "夏普比率", "规模"}
GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable(prefix, *parts):
    raw = "|".join(str(part or "") for part in parts).encode("utf-8")
    return "%s-%s" % (prefix, hashlib.sha256(raw).hexdigest()[:24])


def _month_floor(value, months_back=5):
    year, month = value.year, value.month - months_back
    while month <= 0:
        year -= 1
        month += 12
    return "%04d-%02d" % (year, month)


def _redact(text):
    value = ID_RE.sub("[身份证号已脱敏]", str(text or ""))
    value = PHONE_RE.sub("[手机号已脱敏]", value)
    value = ACCOUNT_RE.sub("[账户号已脱敏]", value)
    return value


def _json(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


class KnowledgeSourceStore(object):
    def __init__(self, project_root, source_root=None, now=None):
        self.project_root = os.path.abspath(project_root)
        self.root = os.path.join(self.project_root, "knowledge_base")
        self.knowledge = KnowledgeStore(self.root)
        self.db_path = self.knowledge.db_path
        self.source_root = os.path.abspath(source_root or os.environ.get("KNOWLEDGE_WECHAT_ROOT") or "") if (source_root or os.environ.get("KNOWLEDGE_WECHAT_ROOT")) else ""
        self.now = now or datetime.now()
        self.review_dir = os.path.join(self.root, "review")
        self.export_dir = os.path.join(self.root, "exports", "obsidian")
        for folder in (self.review_dir, self.export_dir):
            if not os.path.isdir(folder):
                os.makedirs(folder)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_sources (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, last_scan_at TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT '未扫描', stats_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS knowledge_scan_runs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL,
                    cutoff_month TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '',
                    stats_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS knowledge_source_files (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, extension TEXT NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '', last_seen_run TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(source_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS knowledge_candidates (
                    id TEXT PRIMARY KEY, source_file_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, product_id TEXT NOT NULL DEFAULT '',
                    product_name TEXT NOT NULL DEFAULT '', manager TEXT NOT NULL DEFAULT '',
                    document_type TEXT NOT NULL DEFAULT '', document_date TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0, signals_json TEXT NOT NULL DEFAULT '[]',
                    sensitive INTEGER NOT NULL DEFAULT 0, extraction_status TEXT NOT NULL DEFAULT '',
                    redacted_preview TEXT NOT NULL DEFAULT '', imported_document_id INTEGER,
                    assistant_result_json TEXT NOT NULL DEFAULT '{}', rule_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_candidates_status ON knowledge_candidates(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS knowledge_entities (
                    id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, canonical_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
                    id TEXT PRIMARY KEY, from_entity_id TEXT NOT NULL, to_entity_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL,
                    confirmation_status TEXT NOT NULL, generation_method TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(from_entity_id,to_entity_id,relation_type,source_kind,source_ref)
                );
                CREATE TABLE IF NOT EXISTS knowledge_review_batches (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, candidate_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS knowledge_exports (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, stored_name TEXT NOT NULL,
                    created_at TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL
                );
            """)
            connection.execute("INSERT OR IGNORE INTO knowledge_sources(id,label) VALUES(?,?)", (SOURCE_ID, SOURCE_LABEL))
            connection.execute("UPDATE knowledge_scan_runs SET status='已中断',finished_at=? WHERE status='运行中'", (_now(),))

    def _catalog(self):
        entities = []
        aliases = []
        for name, values in PRODUCTS.items():
            product_id = TOP_PRODUCT_IDS[name]
            entities.append({"id": product_id, "type": "顶层FOF", "name": name, "manager": "", "primary": "", "secondary": ""})
            for alias in (name,) + tuple(values):
                if len(normalize_name(alias)) >= 4:
                    aliases.append((normalize_name(alias), product_id, name))
        labels_path = os.path.join(self.project_root, "data_sources", "product_labels.json")
        if os.path.isfile(labels_path):
            records, _payload = load_json_catalog(labels_path)
            records, _check = deduplicate_label_records(records)
            for item in records:
                name = str(item.get("product") or "").strip()
                if not name:
                    continue
                product_id = str(item.get("record_id") or _stable("product", normalize_name(name)))
                entities.append({"id": product_id, "type": "底层私募产品", "name": name,
                                 "manager": str(item.get("manager") or "").strip(),
                                 "primary": str(item.get("primary") or "").strip(),
                                 "secondary": str(item.get("secondary") or "").strip()})
                key = normalize_name(name)
                if len(key) >= 4:
                    aliases.append((key, product_id, name))
        aliases.sort(key=lambda item: len(item[0]), reverse=True)
        return entities, aliases

    def _sync_catalog_graph(self, connection, entities):
        now = _now()
        for item in entities:
            connection.execute("INSERT OR REPLACE INTO knowledge_entities(id,entity_type,canonical_name,metadata_json,updated_at) VALUES(?,?,?,?,?)",
                               (item["id"], item["type"], item["name"], json.dumps(item, ensure_ascii=False), now))
            manager = item.get("manager")
            if manager:
                manager_id = _stable("manager", normalize_name(manager))
                self._upsert_entity(connection, manager_id, "管理人", manager, {})
                self._upsert_edge(connection, manager_id, item["id"], "管理人管理产品", "产品标签", item["id"],
                                  {"field": "manager", "value": manager}, 1.0, "已确认", "确定性规则")
            for level, label in (("一级策略标签", item.get("primary")), ("二级策略标签", item.get("secondary"))):
                if label and label != "其他":
                    tag_id = _stable("strategy", level, label)
                    self._upsert_entity(connection, tag_id, level, label, {"level": level})
                    self._upsert_edge(connection, item["id"], tag_id, "产品属于策略标签", "产品标签", item["id"],
                                      {"field": level, "value": label}, 1.0, "已确认", "确定性规则")

    def _sync_holding_graph(self, connection, entities):
        path = os.path.join(self.project_root, ".runtime", "modules", "underlying-assets.json")
        if not os.path.isfile(path):
            return
        names = {normalize_name(item["name"]): item for item in entities if item["type"] == "底层私募产品"}
        top = {name: TOP_PRODUCT_IDS[name] for name in TOP_PRODUCT_IDS}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        seen = set()
        for row in payload.get("items", []):
            product = names.get(normalize_name(row.get("product")))
            if not product:
                continue
            for fof_name in row.get("fof_products") or []:
                fof_id = top.get(fof_name)
                key = (fof_id, product["id"])
                if not fof_id or key in seen:
                    continue
                seen.add(key)
                evidence = {"product": product["name"], "fof": fof_name,
                            "valuation_date": row.get("valuation_date")}
                self._upsert_edge(connection, fof_id, product["id"], "FOF历史持有底层产品",
                                  "估值表持仓", product["id"], evidence, 1.0, "已确认", "估值表确定性关系")

    @staticmethod
    def _upsert_entity(connection, entity_id, entity_type, name, metadata):
        connection.execute("INSERT OR REPLACE INTO knowledge_entities(id,entity_type,canonical_name,metadata_json,updated_at) VALUES(?,?,?,?,?)",
                           (entity_id, entity_type, name, json.dumps(metadata, ensure_ascii=False), _now()))

    @staticmethod
    def _upsert_edge(connection, from_id, to_id, relation, source_kind, source_ref, evidence,
                     confidence, status, method):
        edge_id = _stable("edge", from_id, to_id, relation, source_kind, source_ref)
        now = _now()
        connection.execute("""INSERT OR REPLACE INTO knowledge_graph_edges
            (id,from_entity_id,to_entity_id,relation_type,source_kind,source_ref,evidence_json,
             confidence,confirmation_status,generation_method,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM knowledge_graph_edges WHERE id=?),?),?)""",
                           (edge_id, from_id, to_id, relation, source_kind, source_ref,
                            json.dumps(evidence, ensure_ascii=False), confidence, status, method,
                            edge_id, now, now))

    def sources(self):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_sources WHERE id=?", (SOURCE_ID,)).fetchone()
        data = dict(row)
        data["configured"] = bool(self.source_root)
        data["available"] = bool(self.source_root and os.path.isdir(self.source_root))
        data["scope"] = "最近6个自然月"
        data["stats"] = _json(data.pop("stats_json"), {})
        return {"sources": [data]}

    def _safe_source_path(self, relative_path):
        if not self.source_root or not os.path.isdir(self.source_root):
            raise ValueError("微信文件源未配置或不可用")
        candidate = os.path.abspath(os.path.join(self.source_root, relative_path))
        root = os.path.abspath(self.source_root)
        if os.path.commonpath([root, candidate]) != root or not os.path.isfile(candidate) or os.path.islink(candidate):
            raise ValueError("来源文件无效")
        return candidate

    def create_scan(self):
        if not self.source_root or not os.path.isdir(self.source_root):
            raise ValueError("微信文件源未配置或不可用")
        with self._connect() as connection:
            running = connection.execute("SELECT id FROM knowledge_scan_runs WHERE status='运行中'").fetchone()
            if running:
                raise RuntimeError("已有微信扫描任务正在运行")
            task_id = str(uuid.uuid4())
            connection.execute("INSERT INTO knowledge_scan_runs(id,source_id,status,cutoff_month,started_at) VALUES(?,?,?,?,?)",
                               (task_id, SOURCE_ID, "运行中", _month_floor(self.now), _now()))
        return task_id

    def scan(self, task_id=None):
        if task_id is None:
            task_id = self.create_scan()
        cutoff = _month_floor(self.now)
        entities, aliases = self._catalog()
        stats = {"inventoried": 0, "unchanged": 0, "candidates": 0, "auto_imported": 0,
                 "sensitive": 0, "unsupported": 0, "failed": 0}
        try:
            with self._connect() as connection:
                self._sync_catalog_graph(connection, entities)
                self._sync_holding_graph(connection, entities)
            root_real = os.path.realpath(self.source_root)
            for folder, dirs, files in os.walk(self.source_root, followlinks=False):
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(folder, name))]
                relative_folder = os.path.relpath(folder, self.source_root).replace("\\", "/")
                first = relative_folder.split("/", 1)[0]
                if re.match(r"^20\d{2}-\d{2}$", first) and first < cutoff:
                    dirs[:] = []
                    continue
                for filename in files:
                    path = os.path.join(folder, filename)
                    if os.path.islink(path) or os.path.commonpath([root_real, os.path.realpath(path)]) != root_real:
                        continue
                    if re.match(r"^20\d{2}-\d{2}$", first) and first < cutoff:
                        continue
                    relative = os.path.relpath(path, self.source_root).replace("\\", "/")
                    try:
                        stat = os.stat(path)
                    except OSError:
                        stats["failed"] += 1
                        continue
                    stats["inventoried"] += 1
                    if self._inventory(task_id, relative, stat, aliases, entities, stats):
                        stats["unchanged"] += 1
            with self._connect() as connection:
                connection.execute("UPDATE knowledge_scan_runs SET status='完成',finished_at=?,stats_json=? WHERE id=?",
                                   (_now(), json.dumps(stats, ensure_ascii=False), task_id))
                connection.execute("UPDATE knowledge_sources SET last_scan_at=?,last_status='完成',stats_json=? WHERE id=?",
                                   (_now(), json.dumps(stats, ensure_ascii=False), SOURCE_ID))
            return {"task_id": task_id, "status": "完成", "stats": stats}
        except Exception as exc:
            with self._connect() as connection:
                connection.execute("UPDATE knowledge_scan_runs SET status='失败',finished_at=?,error=? WHERE id=?",
                                   (_now(), type(exc).__name__, task_id))
                connection.execute("UPDATE knowledge_sources SET last_scan_at=?,last_status='失败' WHERE id=?", (_now(), SOURCE_ID))
            raise

    def _inventory(self, task_id, relative, stat, aliases, entities, stats):
        extension = os.path.splitext(relative)[1].lower()
        file_id = _stable("source", SOURCE_ID, relative)
        with self._connect() as connection:
            old = connection.execute("SELECT * FROM knowledge_source_files WHERE id=?", (file_id,)).fetchone()
            if old and old["size"] == stat.st_size and old["mtime_ns"] == getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)):
                connection.execute("UPDATE knowledge_source_files SET last_seen_run=? WHERE id=?", (task_id, file_id))
                return True
            now = _now()
            connection.execute("INSERT OR REPLACE INTO knowledge_source_files(id,source_id,relative_path,size,mtime_ns,extension,sha256,last_seen_run,created_at,updated_at) VALUES(?,?,?,?,?,?,COALESCE((SELECT sha256 FROM knowledge_source_files WHERE id=?),''),?,?,?)",
                               (file_id, SOURCE_ID, relative, stat.st_size,
                                getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)), extension,
                                file_id, task_id, old["created_at"] if old else now, now))
        filename = os.path.basename(relative)
        normalized = normalize_name(filename)
        matches = []
        for alias, product_id, name in aliases:
            if alias and alias in normalized:
                matches.append((product_id, name, alias))
        unique = {(item[0], item[1]): item for item in matches}
        relevant = bool(unique or any(word.lower() in filename.lower() for word in RELEVANT_WORDS))
        if not relevant:
            return False
        stats["candidates"] += 1
        sensitive_name = any(word in filename for word in SENSITIVE_WORDS)
        if sensitive_name:
            stats["sensitive"] += 1
        if extension not in SUPPORTED_EXTENSIONS or extension == ".xlsm":
            stats["unsupported"] += 1
            self._save_candidate(file_id, "敏感受限" if sensitive_name else "格式不支持", unique, 0.0,
                                 ["文件名相关", "不支持的格式"], sensitive_name, "未提取", "", "", "")
            return False
        if stat.st_size <= 0 or stat.st_size > MAX_FILE_SIZE:
            self._save_candidate(file_id, "敏感受限" if sensitive_name else "格式不支持", unique, 0.0,
                                 ["文件名相关", "文件超过50MB"], sensitive_name, "未提取", "", "", "")
            return False
        path = self._safe_source_path(relative)
        digest = _sha256(path)
        with self._connect() as connection:
            connection.execute("UPDATE knowledge_source_files SET sha256=? WHERE id=?", (digest, file_id))
        try:
            text = extract_text(path)
        except Exception as exc:
            stats["failed"] += 1
            self._save_candidate(file_id, "提取失败", unique, 0.0, ["文件名相关"], sensitive_name,
                                 "提取失败：%s" % type(exc).__name__, "", "", "")
            return False
        inferred = infer_metadata(filename, text)
        body_sensitive = bool(ID_RE.search(text) or PHONE_RE.search(text) or ACCOUNT_RE.search(text))
        sensitive = sensitive_name or body_sensitive
        if sensitive:
            stats["sensitive"] += 0 if sensitive_name else 1
        text_norm = normalize_name(text[:200000])
        confirmed = [(pid, name, alias) for (pid, name), (_pid, _name, alias) in unique.items() if alias in text_norm]
        signals = ["文件名命中已登记产品"] if unique else ["文件名含私募/FOF业务词"]
        if confirmed:
            signals.append("正文再次确认产品名称")
        document_type = inferred.get("document_type") or "其他"
        if document_type != "其他":
            signals.append("文档类型一致")
        manager_signal = False
        chosen = confirmed[0] if len(confirmed) == 1 else (list(unique.values())[0] if len(unique) == 1 else None)
        entity_map = {item["id"]: item for item in entities}
        if chosen and entity_map.get(chosen[0], {}).get("manager") in text and entity_map.get(chosen[0], {}).get("manager"):
            manager_signal = True
            signals.append("管理人一致")
        confidence = (0.60 if len(unique) == 1 else 0) + (0.25 if len(confirmed) == 1 else 0) + (0.10 if document_type != "其他" else 0) + (0.05 if manager_signal else 0)
        if not text.strip():
            status, extraction = "待OCR", "无文本层，需OCR"
        elif sensitive:
            status, extraction = "敏感受限", "已提取但禁止模型处理"
        elif confidence >= .95 and len(unique) == 1 and len(confirmed) == 1:
            status, extraction = "已批准", "已提取"
        else:
            status, extraction = "待确认", "已提取"
        product_id = chosen[0] if chosen else ""
        product_name = chosen[1] if chosen else ""
        manager = entity_map.get(product_id, {}).get("manager", "")
        candidate_id = self._save_candidate(file_id, status, unique, confidence, signals, sensitive,
                                            extraction, _redact(text[:12000]), product_id, product_name,
                                            manager, document_type, inferred.get("document_date") or "")
        if status == "已批准":
            self.import_candidates([candidate_id], automatic=True)
            stats["auto_imported"] += 1
        return False

    def _save_candidate(self, file_id, status, matches, confidence, signals, sensitive,
                        extraction, preview, product_id, product_name, manager="", document_type="",
                        document_date=""):
        now = _now()
        with self._connect() as connection:
            old = connection.execute("SELECT id,created_at,revision FROM knowledge_candidates WHERE source_file_id=?", (file_id,)).fetchone()
            candidate_id = old["id"] if old else str(uuid.uuid4())
            revision = old["revision"] + 1 if old else 1
            connection.execute("""INSERT OR REPLACE INTO knowledge_candidates
                (id,source_file_id,status,revision,product_id,product_name,manager,document_type,document_date,
                 confidence,signals_json,sensitive,extraction_status,redacted_preview,imported_document_id,
                 assistant_result_json,rule_version,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT imported_document_id FROM knowledge_candidates WHERE id=?),NULL),
                        COALESCE((SELECT assistant_result_json FROM knowledge_candidates WHERE id=?),'{}'),?,?,?)""",
                               (candidate_id, file_id, status, revision, product_id, product_name, manager,
                                document_type, document_date, confidence, json.dumps(signals, ensure_ascii=False),
                                int(bool(sensitive)), extraction, preview, candidate_id, candidate_id,
                                RULE_VERSION, old["created_at"] if old else now, now))
        return candidate_id

    def scan_task(self, task_id):
        if not UUID_RE.match(str(task_id or "")):
            raise ValueError("扫描任务ID无效")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_scan_runs WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("扫描任务不存在")
        data = dict(row)
        data["stats"] = _json(data.pop("stats_json"), {})
        return data

    def candidates(self, status="", page=1, page_size=50, product_id=""):
        page, page_size = max(1, int(page)), max(20, min(200, int(page_size)))
        clauses, params = [], []
        if status:
            if status not in CANDIDATE_STATUSES:
                raise ValueError("候选状态无效")
            clauses.append("c.status=?")
            params.append(status)
        if product_id:
            clauses.append("c.product_id=?")
            params.append(product_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM knowledge_candidates c" + where, params).fetchone()[0]
            rows = connection.execute("""SELECT c.*,f.relative_path,f.size,f.extension,f.sha256
                FROM knowledge_candidates c JOIN knowledge_source_files f ON f.id=c.source_file_id%s
                ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""" % where,
                                      params + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["filename"] = os.path.basename(item.pop("relative_path"))
            item["signals"] = _json(item.pop("signals_json"), [])
            item.pop("redacted_preview", None)
            item.pop("assistant_result_json", None)
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def update_candidate(self, candidate_id, expected_revision, action, product_id=""):
        if not UUID_RE.match(str(candidate_id or "")):
            raise ValueError("候选ID无效")
        if action not in ("approve", "exclude", "set_product"):
            raise ValueError("候选操作无效")
        entities, _aliases = self._catalog()
        entity_map = {item["id"]: item for item in entities}
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise KeyError("候选不存在")
            if int(expected_revision) != row["revision"]:
                raise RuntimeError("候选已在其他页面修改")
            if action in ("approve", "set_product"):
                if product_id not in entity_map:
                    raise ValueError("产品ID未登记")
                entity = entity_map[product_id]
                values = ("已批准" if action == "approve" else "待确认", product_id, entity["name"], entity.get("manager", ""))
            else:
                values = ("已排除", row["product_id"], row["product_name"], row["manager"])
            connection.execute("UPDATE knowledge_candidates SET status=?,product_id=?,product_name=?,manager=?,revision=revision+1,updated_at=? WHERE id=?",
                               values + (_now(), candidate_id))
        return self.candidate(candidate_id)

    def candidate(self, candidate_id):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError("候选不存在")
        data = dict(row)
        data["signals"] = _json(data.pop("signals_json"), [])
        data["assistant_result"] = _json(data.pop("assistant_result_json"), {})
        if data.get("sensitive"):
            data.pop("redacted_preview", None)
        return data

    def import_candidates(self, candidate_ids, automatic=False):
        if not isinstance(candidate_ids, list) or not candidate_ids or len(candidate_ids) > 200:
            raise ValueError("每次需选择1至200个候选")
        imported = []
        for candidate_id in candidate_ids:
            if not UUID_RE.match(str(candidate_id or "")):
                raise ValueError("候选ID无效")
            with self._connect() as connection:
                row = connection.execute("""SELECT c.*,f.relative_path,f.sha256 FROM knowledge_candidates c
                    JOIN knowledge_source_files f ON f.id=c.source_file_id WHERE c.id=?""", (candidate_id,)).fetchone()
            if not row or row["status"] not in ("已批准", "已入库") or row["sensitive"]:
                raise ValueError("候选未批准或属于敏感受限材料")
            if row["status"] == "已入库":
                imported.append({"candidate_id": candidate_id, "document_id": row["imported_document_id"], "duplicate": True})
                continue
            path = self._safe_source_path(row["relative_path"])
            if _sha256(path) != row["sha256"]:
                raise RuntimeError("来源文件扫描后已变化，请重新扫描")
            metadata = {"title": os.path.splitext(os.path.basename(path))[0],
                        "document_type": row["document_type"] or "其他", "source": "微信文件源",
                        "document_date": row["document_date"], "product": row["product_name"],
                        "organization": row["manager"], "tags": ["微信来源", "自动入库" if automatic else "人工确认"]}
            document, duplicate = self.knowledge.add_file(path, metadata)
            with self._connect() as connection:
                connection.execute("UPDATE knowledge_candidates SET status='已入库',imported_document_id=?,revision=revision+1,updated_at=? WHERE id=?",
                                   (document["id"], _now(), candidate_id))
                document_entity = "document-%s" % document["id"]
                self._upsert_entity(connection, document_entity, "文档", document["title"],
                                    {"document_id": document["id"], "document_type": row["document_type"], "document_date": row["document_date"]})
                previous = connection.execute("""SELECT imported_document_id,document_date FROM knowledge_candidates
                    WHERE id<>? AND status='已入库' AND imported_document_id IS NOT NULL
                      AND product_id=? AND document_type=? ORDER BY document_date DESC,updated_at DESC LIMIT 1""",
                                              (candidate_id, row["product_id"], row["document_type"])).fetchone()
                if previous:
                    self._upsert_edge(connection, document_entity, "document-%s" % previous["imported_document_id"],
                                      "文档为另一文档的版本/更新", "微信文档", candidate_id,
                                      {"current_date": row["document_date"], "previous_date": previous["document_date"]},
                                      .8, "待确认", "日期与产品规则")
                if row["product_id"]:
                    self._upsert_edge(connection, document_entity, row["product_id"], "文档描述产品",
                                      "微信文档", candidate_id, {"signals": _json(row["signals_json"], [])},
                                      row["confidence"], "已确认" if not automatic else "规则确认", "自动规则" if automatic else "人工确认")
                if row["manager"]:
                    manager_id = _stable("manager", normalize_name(row["manager"]))
                    self._upsert_entity(connection, manager_id, "管理人", row["manager"], {})
                    self._upsert_edge(connection, document_entity, manager_id, "文档由管理人发布", "微信文档",
                                      candidate_id, {"manager": row["manager"]}, row["confidence"],
                                      "已确认" if not automatic else "规则确认", "自动规则" if automatic else "人工确认")
            imported.append({"candidate_id": candidate_id, "document_id": document["id"], "duplicate": duplicate})
        return {"imported": imported}

    def graph(self, entity_type="", query="", limit=500, center_id=""):
        limit = max(20, min(1000, int(limit)))
        clauses, params = [], []
        if entity_type:
            if entity_type not in GRAPH_ENTITY_TYPES:
                raise ValueError("实体类型无效")
            clauses.append("entity_type=?")
            params.append(entity_type)
        if query:
            clauses.append("canonical_name LIKE ?")
            params.append("%%%s%%" % str(query)[:100])
        if center_id and not GRAPH_ID_RE.match(str(center_id)):
            raise ValueError("中心节点ID无效")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        suggested_center_id = ""
        with self._connect() as connection:
            edges = connection.execute("SELECT * FROM knowledge_graph_edges ORDER BY updated_at DESC LIMIT 10000").fetchall()
            suggested = connection.execute("""SELECT e.id,COUNT(g.id) AS score
                FROM knowledge_entities e
                JOIN knowledge_graph_edges g ON g.from_entity_id=e.id OR g.to_entity_id=e.id
                JOIN knowledge_entities other ON other.id=CASE WHEN g.from_entity_id=e.id
                    THEN g.to_entity_id ELSE g.from_entity_id END
                WHERE e.entity_type IN ('管理人','底层私募产品','顶层FOF')
                  AND other.entity_type IN ('公司信息','人员','投资策略','业绩记录','产品条款','风控信息')
                GROUP BY e.id ORDER BY score DESC,e.canonical_name LIMIT 1""").fetchone()
            suggested_center_id = suggested["id"] if suggested else ""
            if center_id:
                center = connection.execute("SELECT id FROM knowledge_entities WHERE id=?", (center_id,)).fetchone()
                if not center:
                    raise ValueError("中心节点不存在")
                selected = {center_id}
                frontier = {center_id}
                for _depth in range(2):
                    next_frontier = set()
                    for edge in edges:
                        if edge["from_entity_id"] in frontier:
                            next_frontier.add(edge["to_entity_id"])
                        if edge["to_entity_id"] in frontier:
                            next_frontier.add(edge["from_entity_id"])
                    next_frontier -= selected
                    selected.update(next_frontier)
                    frontier = next_frontier
                    if not frontier or len(selected) >= limit:
                        break
                selected = [center_id] + sorted(selected - {center_id})[:limit - 1]
                marks = ",".join("?" for _value in selected)
                nodes = connection.execute(
                    "SELECT * FROM knowledge_entities WHERE id IN (%s) ORDER BY entity_type,canonical_name" % marks,
                    selected).fetchall()
            else:
                nodes = connection.execute("SELECT * FROM knowledge_entities%s ORDER BY entity_type,canonical_name LIMIT ?" % where,
                                           params + [limit]).fetchall()
            node_ids = {row["id"] for row in nodes}
        result_nodes = [{"id": row["id"], "type": row["entity_type"], "name": row["canonical_name"],
                         "metadata": _json(row["metadata_json"], {})} for row in nodes]
        result_edges = [{"id": row["id"], "from": row["from_entity_id"], "to": row["to_entity_id"],
                           "relation": row["relation_type"], "source_kind": row["source_kind"],
                           "evidence": _json(row["evidence_json"], {}), "confidence": row["confidence"],
                           "status": row["confirmation_status"], "method": row["generation_method"]}
                          for row in edges if row["from_entity_id"] in node_ids and row["to_entity_id"] in node_ids]
        counts = {}
        for node in result_nodes:
            counts[node["type"]] = counts.get(node["type"], 0) + 1
        return {"nodes": result_nodes, "edges": result_edges, "center_id": center_id,
                "suggested_center_id": suggested_center_id,
                "type_counts": counts,
                "legend": {"solid": "已确认关系", "dashed": "待人工确认或文档推断",
                           "performance": ["材料披露", "系统计算"]}}

    def document_context(self, document_ids):
        ids = [int(value) for value in document_ids if str(value).isdigit()][:500]
        if not ids:
            return {}
        marks = ",".join("?" for _value in ids)
        with self._connect() as connection:
            rows = connection.execute("""SELECT imported_document_id,status,product_id,product_name,manager
                FROM knowledge_candidates WHERE imported_document_id IN (%s)""" % marks, ids).fetchall()
            holding_edges = connection.execute("""SELECT from_entity_id,to_entity_id FROM knowledge_graph_edges
                WHERE relation_type='FOF历史持有底层产品' AND confirmation_status='已确认'""").fetchall()
            top_names = {row["id"]: row["canonical_name"] for row in connection.execute(
                "SELECT id,canonical_name FROM knowledge_entities WHERE entity_type='顶层FOF'")}
        by_product = {}
        for edge in holding_edges:
            by_product.setdefault(edge["to_entity_id"], []).append(
                {"id": edge["from_entity_id"], "name": top_names.get(edge["from_entity_id"], edge["from_entity_id"])})
        return {str(row["imported_document_id"]): {"review_status": row["status"],
                                                    "product_id": row["product_id"],
                                                    "product_name": row["product_name"],
                                                    "manager": row["manager"],
                                                    "associated_fofs": by_product.get(row["product_id"], [])}
                for row in rows}

    def export_review(self, batch_id=None):
        with self._connect() as connection:
            rows = connection.execute("""SELECT c.*,f.sha256 FROM knowledge_candidates c
                JOIN knowledge_source_files f ON f.id=c.source_file_id
                WHERE c.status='待确认' AND c.sensitive=0 AND c.assistant_result_json='{}'
                ORDER BY c.updated_at DESC LIMIT 30""").fetchall()
        if not rows:
            raise ValueError("没有可导出的非敏感待确认候选")
        batch_id = batch_id or str(uuid.uuid4())
        if not UUID_RE.match(batch_id):
            raise ValueError("批次ID无效")
        items = []
        for row in rows:
            if any(word in row["redacted_preview"] for word in MODEL_FORBIDDEN):
                continue
            items.append({"candidate_id": row["id"], "sha256": row["sha256"],
                          "current_match": {"product_id": row["product_id"], "product_name": row["product_name"],
                                            "manager": row["manager"], "signals": _json(row["signals_json"], [])},
                          "redacted_excerpt": row["redacted_preview"][:6000]})
        payload = {"schema_version": 2, "batch_id": batch_id, "created_at": _now(),
                   "instructions": "输出固定结构审阅JSON；只提取有原文证据的公司、职业人员、策略和材料披露业绩；不得修改产品标签或持仓，不得输出联系方式、身份或账户信息。",
                   "review_schema": {"status": ["匹配", "不匹配", "不确定"],
                                     "confidence": "0至1", "document_type": "字符串",
                                     "product_id": "已登记实体ID或空", "manager": "字符串",
                                     "top_fof_ids": "已登记顶层FOF ID数组", "strategy_labels": "字符串数组",
                                     "evidence": [{"location": "页码或工作表", "excerpt": "短证据"}],
                                     "suggested_relations": "固定关系名称数组",
                                     "company_profile": [{"category": sorted(COMPANY_FACT_TYPES), "value": "材料披露值", "evidence": "证据数组"}],
                                     "people": [{"name": "姓名", "role_type": sorted(PERSON_ROLE_TYPES), "title": "职务", "product_ids": "已登记产品ID数组", "strategy_names": "策略名称数组", "evidence": "证据数组"}],
                                     "strategies": [{"name": "策略名称", "description": "材料披露摘要", "asset_classes": "字符串数组", "constraints": "字符串数组", "product_ids": "已登记产品ID数组", "evidence": "证据数组"}],
                                     "performance_records": [{"product_id": "已登记产品ID", "period_start": "ISO日期", "period_end": "ISO日期", "metrics": "固定指标数值字典", "source_basis": "材料披露", "evidence": "证据数组"}]},
                   "items": items}
        path = os.path.join(self.review_dir, batch_id + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        with self._connect() as connection:
            connection.execute("INSERT INTO knowledge_review_batches(id,status,candidate_ids_json,created_at) VALUES(?,?,?,?)",
                               (batch_id, "待助手审阅", json.dumps([item["candidate_id"] for item in items]), _now()))
        return {"batch_id": batch_id, "count": len(items), "path": path}

    @staticmethod
    def _review_evidence(value):
        if not isinstance(value, list) or not value or len(value) > 20:
            raise ValueError("尽调实体必须提供证据")
        result = []
        for proof in value:
            if not isinstance(proof, dict):
                raise ValueError("尽调证据格式无效")
            location = str(proof.get("location") or "").strip()[:100]
            excerpt = str(proof.get("excerpt") or "").strip()[:300]
            if not location or not excerpt:
                raise ValueError("尽调证据缺少位置或短片段")
            result.append({"location": location, "excerpt": _redact(excerpt)})
        return result

    @staticmethod
    def _registered_product_ids(connection):
        return {row[0] for row in connection.execute(
            "SELECT id FROM knowledge_entities WHERE entity_type IN ('顶层FOF','底层私募产品')")}

    def _import_diligence_review(self, connection, candidate, review, confidence):
        serialized = json.dumps(review, ensure_ascii=False)
        if ID_RE.search(serialized) or PHONE_RE.search(serialized) or ACCOUNT_RE.search(serialized):
            raise ValueError("尽调结果不得包含身份、手机或账户信息")
        product_ids = self._registered_product_ids(connection)
        manager_name = str(review.get("manager") or candidate["manager"] or "").strip()[:120]
        manager_id = ""
        if manager_name:
            manager_id = _stable("manager", normalize_name(manager_name))
            self._upsert_entity(connection, manager_id, "管理人", manager_name, {})
        document_id = ("document-%s" % candidate["imported_document_id"] if candidate["imported_document_id"]
                       else "source-document-%s" % candidate["id"])
        document_name = candidate["product_name"] or candidate["document_type"] or "待审核微信文档"
        self._upsert_entity(connection, document_id, "文档", document_name,
                            {"candidate_id": candidate["id"], "document_type": candidate["document_type"],
                             "document_date": candidate["document_date"]})
        source_ref = candidate["id"]
        source_kind = "助手审阅"
        status = "待人工确认"
        method = "Codex脱敏审阅"

        for fact in review.get("company_profile") or []:
            category = str(fact.get("category") or "")
            value = str(fact.get("value") or "").strip()[:500]
            if not manager_id or category not in COMPANY_FACT_TYPES or not value:
                raise ValueError("公司信息缺少管理人、固定分类或值")
            evidence = self._review_evidence(fact.get("evidence"))
            fact_id = _stable("company-fact", manager_id, category, value)
            self._upsert_entity(connection, fact_id, "公司信息", "%s：%s" % (category, value),
                                {"category": category, "value": value, "manager_id": manager_id})
            self._upsert_edge(connection, manager_id, fact_id, "管理人具有公司信息", source_kind,
                              source_ref, evidence, confidence, status, method)
            self._upsert_edge(connection, document_id, fact_id, "文档披露公司信息", source_kind,
                              source_ref, evidence, confidence, status, method)

        for person in review.get("people") or []:
            name = str(person.get("name") or "").strip()[:80]
            role_type = str(person.get("role_type") or "")
            title = str(person.get("title") or "").strip()[:120]
            if not manager_id or len(name) < 2 or role_type not in PERSON_ROLE_TYPES:
                raise ValueError("人员实体缺少管理人、姓名或固定职业角色")
            evidence = self._review_evidence(person.get("evidence"))
            person_id = _stable("person", manager_id, normalize_name(name), role_type)
            self._upsert_entity(connection, person_id, "人员", name,
                                {"role_type": role_type, "title": title, "manager_id": manager_id})
            relation = ("创始人创办管理人" if role_type == "创始人" else
                        "合伙人任职于管理人" if role_type == "合伙人" else "人员任职于管理人")
            self._upsert_edge(connection, person_id, manager_id, relation, source_kind, source_ref,
                              evidence, confidence, status, method)
            self._upsert_edge(connection, document_id, person_id, "文档披露人员", source_kind,
                              source_ref, evidence, confidence, status, method)
            for product_id in person.get("product_ids") or []:
                if product_id not in product_ids:
                    raise ValueError("人员关系引用未登记产品")
                self._upsert_edge(connection, person_id, product_id, "人员负责产品", source_kind,
                                  source_ref, evidence, confidence, status, method)

        strategy_ids = {}
        for strategy in review.get("strategies") or []:
            name = str(strategy.get("name") or "").strip()[:120]
            if not name:
                raise ValueError("投资策略名称无效")
            evidence = self._review_evidence(strategy.get("evidence"))
            linked_products = strategy.get("product_ids") or []
            if any(product_id not in product_ids for product_id in linked_products):
                raise ValueError("投资策略引用未登记产品")
            strategy_id = _stable("investment-strategy", manager_id, normalize_name(name))
            strategy_ids[normalize_name(name)] = strategy_id
            metadata = {"manager_id": manager_id,
                        "description": str(strategy.get("description") or "")[:1000],
                        "asset_classes": [str(value)[:50] for value in (strategy.get("asset_classes") or [])[:20]],
                        "constraints": [str(value)[:200] for value in (strategy.get("constraints") or [])[:30]]}
            self._upsert_entity(connection, strategy_id, "投资策略", name, metadata)
            self._upsert_edge(connection, document_id, strategy_id, "文档披露策略", source_kind,
                              source_ref, evidence, confidence, status, method)
            if manager_id:
                self._upsert_edge(connection, manager_id, strategy_id, "管理人实施策略", source_kind,
                                  source_ref, evidence, confidence, status, method)
            for product_id in linked_products:
                self._upsert_edge(connection, product_id, strategy_id, "产品采用策略", source_kind,
                                  source_ref, evidence, confidence, status, method)

        for person in review.get("people") or []:
            person_id = _stable("person", manager_id, normalize_name(person.get("name")), person.get("role_type"))
            evidence = self._review_evidence(person.get("evidence"))
            for strategy_name in person.get("strategy_names") or []:
                strategy_id = strategy_ids.get(normalize_name(strategy_name))
                if not strategy_id:
                    raise ValueError("人员关系引用本次审阅中未登记的策略")
                self._upsert_edge(connection, person_id, strategy_id, "人员负责策略", source_kind,
                                  source_ref, evidence, confidence, status, method)

        for performance in review.get("performance_records") or []:
            product_id = performance.get("product_id")
            start = str(performance.get("period_start") or "")
            end = str(performance.get("period_end") or "")
            metrics = performance.get("metrics") or {}
            try:
                start_date = datetime.strptime(start, "%Y-%m-%d")
                end_date = datetime.strptime(end, "%Y-%m-%d")
            except ValueError:
                start_date = end_date = None
            if (product_id not in product_ids or not ISO_DATE_RE.match(start) or not ISO_DATE_RE.match(end)
                    or not start_date or start_date > end_date or performance.get("source_basis") != "材料披露"):
                raise ValueError("材料披露业绩的产品、区间或来源口径无效")
            if (not isinstance(metrics, dict) or not metrics
                    or any(key not in PERFORMANCE_METRICS or isinstance(value, bool) or not isinstance(value, (int, float))
                           for key, value in metrics.items())):
                raise ValueError("材料披露业绩指标无效")
            evidence = self._review_evidence(performance.get("evidence"))
            performance_id = _stable("performance", product_id, start, end, json.dumps(metrics, sort_keys=True))
            self._upsert_entity(connection, performance_id, "业绩记录", "%s → %s" % (start, end),
                                {"product_id": product_id, "period_start": start, "period_end": end,
                                 "metrics": metrics, "source_basis": "材料披露"})
            self._upsert_edge(connection, product_id, performance_id, "产品具有区间业绩", source_kind,
                              source_ref, evidence, confidence, status, method)
            self._upsert_edge(connection, document_id, performance_id, "文档披露业绩", source_kind,
                              source_ref, evidence, confidence, status, method)

    def import_review(self, batch_id):
        if not UUID_RE.match(str(batch_id or "")):
            raise ValueError("批次ID无效")
        review_path = os.path.join(self.review_dir, batch_id + ".review.json")
        if not os.path.isfile(review_path):
            raise ValueError("审阅结果文件不存在")
        with open(review_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("batch_id") != batch_id or not isinstance(payload.get("items"), list):
            raise ValueError("审阅结果格式无效")
        with self._connect() as connection:
            batch = connection.execute("SELECT * FROM knowledge_review_batches WHERE id=?", (batch_id,)).fetchone()
            allowed = set(_json(batch["candidate_ids_json"], [])) if batch else set()
            registered = {row[0] for row in connection.execute("SELECT id FROM knowledge_entities")}
            relation_types = {"文档描述产品", "文档由管理人发布", "管理人管理产品",
                              "FOF历史持有底层产品", "产品属于策略标签", "文档为另一文档的版本/更新"}
            for item in payload["items"]:
                candidate_id = item.get("candidate_id")
                if candidate_id not in allowed:
                    raise ValueError("审阅结果包含未登记候选")
                candidate = connection.execute("SELECT c.*,f.sha256,f.relative_path FROM knowledge_candidates c JOIN knowledge_source_files f ON f.id=c.source_file_id WHERE c.id=?", (candidate_id,)).fetchone()
                if not candidate or item.get("sha256") != candidate["sha256"]:
                    raise ValueError("候选哈希不一致")
                review = item.get("review") or {}
                if review.get("status") not in ("匹配", "不匹配", "不确定"):
                    raise ValueError("审阅状态无效")
                confidence = review.get("confidence")
                if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                    raise ValueError("审阅置信度无效")
                product_id = review.get("product_id") or ""
                if product_id and product_id not in registered:
                    raise ValueError("审阅结果引用未登记产品")
                top_fofs = review.get("top_fof_ids") or []
                if not isinstance(top_fofs, list) or any(value not in registered or not str(value).startswith("top-") for value in top_fofs):
                    raise ValueError("审阅结果引用未登记顶层FOF")
                evidence = review.get("evidence") or []
                if not isinstance(evidence, list) or len(evidence) > 20:
                    raise ValueError("审阅证据格式无效")
                for proof in evidence:
                    if (not isinstance(proof, dict) or not str(proof.get("location") or "")[:100]
                            or not str(proof.get("excerpt") or "")[:300]):
                        raise ValueError("审阅证据缺少位置或短片段")
                relations = review.get("suggested_relations") or []
                if not isinstance(relations, list) or any(value not in relation_types for value in relations):
                    raise ValueError("建议关系类型无效")
                for field, maximum in (("company_profile", 20), ("people", 50),
                                       ("strategies", 30), ("performance_records", 50)):
                    if not isinstance(review.get(field) or [], list) or len(review.get(field) or []) > maximum:
                        raise ValueError("尽调实体数量或格式无效")
                self._import_diligence_review(connection, candidate, review, confidence)
                connection.execute("UPDATE knowledge_candidates SET assistant_result_json=?,revision=revision+1,updated_at=? WHERE id=?",
                                   (json.dumps(item, ensure_ascii=False), _now(), candidate_id))
            connection.execute("UPDATE knowledge_review_batches SET status='已导入',imported_at=? WHERE id=?", (_now(), batch_id))
        return {"batch_id": batch_id, "imported": len(payload["items"])}

    def export_obsidian(self):
        export_id = str(uuid.uuid4())
        with self._connect() as connection:
            node_rows = connection.execute(
                "SELECT * FROM knowledge_entities ORDER BY entity_type,canonical_name,id").fetchall()
            edge_rows = connection.execute(
                "SELECT * FROM knowledge_graph_edges ORDER BY relation_type,from_entity_id,to_entity_id,id").fetchall()
        nodes = [{"id": row["id"], "type": row["entity_type"], "name": row["canonical_name"],
                  "metadata": _json(row["metadata_json"], {})} for row in node_rows]
        edges = [{"id": row["id"], "from": row["from_entity_id"], "to": row["to_entity_id"],
                  "relation": row["relation_type"], "source_kind": row["source_kind"],
                  "evidence": _json(row["evidence_json"], {}), "confidence": row["confidence"],
                  "status": row["confirmation_status"], "method": row["generation_method"]}
                 for row in edge_rows]
        temporary = tempfile.mkdtemp(prefix="vault-", dir=self.export_dir)
        try:
            confirmed_statuses = {"已确认", "规则确认"}
            base_types = {"文档", "顶层FOF", "底层私募产品", "管理人", "一级策略标签", "二级策略标签"}
            entity_names = {node["id"]: node["name"] for node in nodes}
            entity_map = {node["id"]: node for node in nodes}
            related_edges = {node["id"]: [] for node in nodes}
            confirmed_ids = {node["id"] for node in nodes
                             if node["type"] in base_types and not node["metadata"].get("candidate")}
            for edge in edges:
                related_edges.setdefault(edge["from"], []).append(edge)
                related_edges.setdefault(edge["to"], []).append(edge)
                if edge["status"] in confirmed_statuses:
                    confirmed_ids.update((edge["from"], edge["to"]))

            def clean_segment(value):
                value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip(" .")
                return (value[:70] or "未命名")

            node_paths = {}
            for node in nodes:
                zone = "已确认" if node["id"] in confirmed_ids else "待确认"
                suffix = hashlib.sha256(node["id"].encode("utf-8")).hexdigest()[:10]
                node_paths[node["id"]] = "%s/%s/%s--%s" % (
                    zone, clean_segment(node["type"]), clean_segment(node["name"]), suffix)
            dossier_paths = {}
            for node in nodes:
                suffix = hashlib.sha256(node["id"].encode("utf-8")).hexdigest()[:10]
                if node["type"] == "管理人":
                    dossier_paths[node["id"]] = "管理人尽调档案/%s--%s" % (
                        clean_segment(node["name"]), suffix)
                elif node["type"] in ("底层私募产品", "顶层FOF"):
                    dossier_paths[node["id"]] = "产品档案/%s--%s" % (
                        clean_segment(node["name"]), suffix)

            exported_at = _now()
            for node in nodes:
                confirmed_links, pending_links = [], []
                for edge in related_edges.get(node["id"], []):
                    outgoing = edge["from"] == node["id"]
                    other_id = edge["to"] if outgoing else edge["from"]
                    other_path = node_paths.get(other_id)
                    if not other_path:
                        continue
                    other_name = entity_names.get(other_id, other_id)
                    if outgoing:
                        statement = "- **%s** → [[%s|%s]]" % (edge["relation"], other_path, other_name)
                    else:
                        statement = "- [[%s|%s]] → **%s**" % (other_path, other_name, edge["relation"])
                    statement += "（%s；%s；置信度 %.2f）" % (
                        edge["source_kind"], edge["method"], float(edge["confidence"] or 0))
                    target = confirmed_links if edge["status"] in confirmed_statuses else pending_links
                    target.append(statement)
                metadata = json.dumps(node["metadata"], ensure_ascii=False, indent=2, sort_keys=True)
                body = ["---",
                        "id: %s" % json.dumps(node["id"], ensure_ascii=False),
                        "type: %s" % json.dumps(node["type"], ensure_ascii=False),
                        "review_status: %s" % json.dumps("已确认" if node["id"] in confirmed_ids else "待确认", ensure_ascii=False),
                        "tags: %s" % json.dumps(["知识图谱", "实体/%s" % node["type"],
                                                  "状态/%s" % ("已确认" if node["id"] in confirmed_ids else "待确认")], ensure_ascii=False),
                        "evidence_count: %d" % len(related_edges.get(node["id"], [])),
                        "exported_at: %s" % json.dumps(exported_at),
                        "---", "", "# %s" % node["name"], "", "## 元数据", "", "```json", metadata, "```", "",
                        "## 已确认关系", ""]
                body.extend(confirmed_links or ["暂无已确认关系。"])
                body.extend(["", "## 待确认关系", ""])
                body.extend(pending_links or ["暂无待确认关系。"])
                if node["id"] in dossier_paths:
                    body.extend(["", "## 汇总档案", "", "[[%s|打开管理人/产品汇总档案]]" % dossier_paths[node["id"]]])
                relative = node_paths[node["id"]] + ".md"
                full_path = os.path.join(temporary, *relative.split("/"))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(body) + "\n")

            for node in nodes:
                dossier_path = dossier_paths.get(node["id"])
                if not dossier_path:
                    continue
                groups = {}
                for edge in related_edges.get(node["id"], []):
                    other_id = edge["to"] if edge["from"] == node["id"] else edge["from"]
                    other = entity_map.get(other_id)
                    if not other or other["type"] in ("一级策略标签", "二级策略标签"):
                        continue
                    groups.setdefault(other["type"], []).append((edge, other))
                lines = ["---", "id: %s" % json.dumps("dossier-" + node["id"], ensure_ascii=False),
                         "type: %s" % json.dumps("管理人尽调档案" if node["type"] == "管理人" else "产品档案", ensure_ascii=False),
                         "entity_id: %s" % json.dumps(node["id"], ensure_ascii=False),
                         "tags: %s" % json.dumps(["尽调档案", node["type"]], ensure_ascii=False),
                         "exported_at: %s" % json.dumps(exported_at), "---", "",
                         "# %s" % node["name"], "", "权威实体：[[%s|%s]]" % (node_paths[node["id"]], node["name"]), ""]
                headings = (("公司信息", "公司概况"), ("人员", "团队与人员"),
                            ("底层私募产品", "产品体系"), ("顶层FOF", "FOF关系"),
                            ("产品条款", "产品条款"), ("投资策略", "投资策略"),
                            ("业绩记录", "材料披露业绩"), ("风控信息", "风险控制"),
                            ("文档", "来源文档"))
                for entity_type, heading in headings:
                    lines.extend(["## %s" % heading, ""])
                    values = groups.get(entity_type, [])
                    if not values:
                        lines.extend(["暂无有证据记录。", ""])
                        continue
                    for edge, other in values:
                        marker = "已确认" if edge["status"] in confirmed_statuses else "待确认"
                        lines.append("- [[%s|%s]] · %s · %s · 置信度 %.2f" % (
                            node_paths[other["id"]], other["name"], edge["relation"], marker,
                            float(edge["confidence"] or 0)))
                    lines.append("")
                full_path = os.path.join(temporary, *(dossier_path + ".md").split("/"))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")

            index_dir = os.path.join(temporary, "索引")
            os.makedirs(index_dir, exist_ok=True)
            with open(os.path.join(index_dir, "实体目录.md"), "w", encoding="utf-8") as handle:
                handle.write("# 实体目录\n\n")
                for node in nodes:
                    handle.write("- [[%s|%s]] · %s · %s\n" % (
                        node_paths[node["id"]], node["name"], node["type"],
                        "已确认" if node["id"] in confirmed_ids else "待确认"))
            with open(os.path.join(index_dir, "关系证据.md"), "w", encoding="utf-8") as handle:
                handle.write("# 关系证据索引\n\n")
                for edge in edges:
                    if edge["from"] not in node_paths or edge["to"] not in node_paths:
                        continue
                    handle.write("## %s · %s\n\n" % (edge["relation"], edge["status"]))
                    handle.write("[[%s|%s]] → [[%s|%s]]  \n" % (
                        node_paths[edge["from"]], entity_names.get(edge["from"], edge["from"]),
                        node_paths[edge["to"]], entity_names.get(edge["to"], edge["to"])))
                    handle.write("来源：%s；生成方式：%s；置信度：%.2f  \n" % (
                        edge["source_kind"], edge["method"], float(edge["confidence"] or 0)))
                    handle.write("证据：`%s`\n\n" % json.dumps(edge["evidence"], ensure_ascii=False, sort_keys=True))
            with open(os.path.join(temporary, "首页.md"), "w", encoding="utf-8") as handle:
                handle.write("# 私募知识库导航\n\n")
                handle.write("本Vault是SQLite知识图谱的只读浏览副本；待确认内容不构成正式事实。\n\n")
                handle.write("- [[索引/实体目录|实体目录]]\n- [[索引/关系证据|关系证据索引]]\n")
                handle.write("- 管理人尽调档案：%d份\n- 产品档案：%d份\n\n" % (
                    sum(1 for node in nodes if node["type"] == "管理人"),
                    sum(1 for node in nodes if node["type"] in ("底层私募产品", "顶层FOF"))))
                handle.write("全局关系图默认隐藏一级、二级标签枢纽，以突出管理人、团队、策略、业绩和风控。\n")
            obsidian_dir = os.path.join(temporary, ".obsidian")
            os.makedirs(obsidian_dir, exist_ok=True)
            graph_config = {
                "collapse-filter": False,
                "search": '-path:"已确认/一级策略标签" -path:"已确认/二级策略标签"',
                "showTags": False, "showAttachments": False, "hideUnresolved": True,
                "showOrphans": False, "collapse-color-groups": False,
                "colorGroups": [
                    {"query": 'path:"管理人尽调档案"', "color": {"a": 1, "rgb": 1608907}},
                    {"query": 'path:"已确认/人员" OR path:"待确认/人员"', "color": {"a": 1, "rgb": 15235360}},
                    {"query": 'path:"产品档案"', "color": {"a": 1, "rgb": 1861029}},
                    {"query": 'path:"已确认/投资策略" OR path:"待确认/投资策略"', "color": {"a": 1, "rgb": 14633281}},
                    {"query": 'path:"已确认/业绩记录" OR path:"待确认/业绩记录"', "color": {"a": 1, "rgb": 8017349}},
                    {"query": 'path:"已确认/风控信息" OR path:"待确认/风控信息"', "color": {"a": 1, "rgb": 14780721}},
                    {"query": 'path:"已确认/文档"', "color": {"a": 1, "rgb": 6788490}}
                ]}
            with open(os.path.join(obsidian_dir, "graph.json"), "w", encoding="utf-8") as handle:
                json.dump(graph_config, handle, ensure_ascii=False, indent=2)
            with open(os.path.join(temporary, "README.md"), "w", encoding="utf-8") as handle:
                confirmed_count = sum(1 for node in nodes if node["id"] in confirmed_ids)
                handle.write("# 私募知识图谱导出\n\n")
                handle.write("本Vault为只读浏览副本；SQLite和知识库原文件仍是权威源。\n\n")
                handle.write("- 实体：%d（已确认 %d，待确认 %d）\n" % (
                    len(nodes), confirmed_count, len(nodes) - confirmed_count))
                handle.write("- 关系：%d（已确认 %d，待确认 %d）\n" % (
                    len(edges), sum(1 for edge in edges if edge["status"] in confirmed_statuses),
                    sum(1 for edge in edges if edge["status"] not in confirmed_statuses)))
                handle.write("- 目录：`已确认/`与`待确认/`严格分开；待确认内容不得作为正式业务事实。\n")
                handle.write("- 每个文件名带稳定ID摘要，同名实体不会互相覆盖。\n")
            stored = export_id + ".zip"
            destination = os.path.join(self.export_dir, stored)
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for root, _directories, names in os.walk(temporary):
                    for name in sorted(names):
                        full_path = os.path.join(root, name)
                        archive.write(full_path, os.path.relpath(full_path, temporary).replace(os.sep, "/"))
            digest = _sha256(destination)
            size = os.path.getsize(destination)
            with self._connect() as connection:
                connection.execute("INSERT INTO knowledge_exports(id,kind,stored_name,created_at,size,sha256) VALUES(?,?,?,?,?,?)",
                                   (export_id, "obsidian", stored, _now(), size, digest))
            return {"export_id": export_id, "size": size, "sha256": digest}
        finally:
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)

    def export_path(self, export_id):
        if not UUID_RE.match(str(export_id or "")):
            raise ValueError("导出ID无效")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_exports WHERE id=?", (export_id,)).fetchone()
        if not row:
            raise KeyError("导出不存在")
        path = os.path.join(self.export_dir, row["stored_name"])
        if not os.path.isfile(path) or _sha256(path) != row["sha256"]:
            raise RuntimeError("导出文件校验失败")
        return path, dict(row)
