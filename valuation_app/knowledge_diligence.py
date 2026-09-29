"""Evidence-first diligence facts for local knowledge documents.

The module deliberately creates review candidates, never authoritative product
catalogue records.  Every extracted fact keeps page evidence and remains
pending until a user confirms it.
"""
from __future__ import unicode_literals

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime

from .knowledge import KnowledgeStore, _clean_unicode, extract_text
from .labels import normalize_name


FACT_TYPES = ("公司信息", "人员", "产品信息", "产品条款", "投资策略", "业绩记录", "风控信息")
FACT_STATUSES = ("待确认", "已确认", "已驳回")
ENTITY_TYPES = ("管理人", "底层私募产品")
EXTRACTOR_VERSION = "diligence-1"
COMPANY_PREDICATES = {
    "公司简介": ("公司简介", "公司介绍", "专注于", "定位为"),
    "成立时间": ("成立于", "成立时间"),
    "注册地": ("注册于", "注册地"),
    "管理规模": ("管理规模", "资产管理规模", "规模合计"),
    "股权结构": ("股权结构", "股东", "持股"),
    "机构背景": ("机构背景", "控股", "母公司"),
    "登记资质": ("基金业协会", "登记编号", "管理人登记"),
    "投研规模": ("投研团队", "团队成员", "员工人数", "投研人员"),
}
TERM_PREDICATES = {
    "管理费": ("管理费",), "托管费": ("托管费",), "业绩报酬": ("业绩报酬", "业绩提成"),
    "开放安排": ("开放日", "开放期", "申购开放", "赎回开放"),
    "封闭期": ("封闭期", "锁定期"), "起投金额": ("起投", "认购起点"),
    "产品期限": ("存续期限", "产品期限"), "业绩基准": ("业绩比较基准", "业绩基准"),
}
PRODUCT_PREDICATES = {
    "产品名称": ("产品名称", "基金名称"),
    "管理人": ("基金管理人", "资产管理人", "管理机构"),
    "托管人": ("基金托管人", "托管机构", "托管人"),
    "产品结构": ("产品结构", "基金类型"),
    "产品状态": ("运作状态", "产品状态"),
}
RISK_PREDICATES = {
    "风险体系": ("风控体系", "风险控制", "风险管理"),
    "止损": ("止损", "预警线", "止损线"),
    "集中度": ("集中度", "单票上限", "持仓上限"),
    "流动性": ("流动性风险", "流动性管理"),
    "容量": ("策略容量", "容量上限"),
    "杠杆": ("杠杆", "风险敞口"),
    "模型风险": ("模型风险", "模型失效"),
    "操作风险": ("操作风险", "交易系统风险"),
}
PERSON_ROLES = ("创始人", "合伙人", "基金经理", "投资经理", "研究员", "交易员")
STRATEGY_NAMES = ("量化选股", "指数增强", "股票多头", "市场中性", "CTA", "套利", "可转债",
                  "全天候", "多策略", "期权", "T0", "高频", "宏观", "固收")
PERFORMANCE_NAMES = ("累计收益率", "年化收益率", "最大回撤", "波动率", "夏普比率", "超额收益率")
SENSITIVE_WORDS = ("投资者名单", "身份证", "银行卡", "银行账户", "开户", "认购明细", "客户名单")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_RE = re.compile(r"(?<!\d)(?:\d{17}[0-9Xx]|\d{15})(?!\d)")
ACCOUNT_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _json(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _stable(prefix, *parts):
    value = "|".join(str(part or "") for part in parts).encode("utf-8")
    return "%s-%s" % (prefix, hashlib.sha256(value).hexdigest()[:24])


def _redact(value):
    text = ID_RE.sub("[身份证号已脱敏]", str(value or ""))
    text = PHONE_RE.sub("[手机号已脱敏]", text)
    return ACCOUNT_RE.sub("[账户号已脱敏]", text)


def _sentences(text):
    clean = re.sub(r"[\t\r ]+", " ", str(text or ""))
    values = re.split(r"[\n。；;]+", clean)
    return [value.strip() for value in values if 8 <= len(value.strip()) <= 500]


class KnowledgeDiligenceStore(object):
    def __init__(self, project_root):
        self.project_root = os.path.abspath(project_root)
        self.knowledge = KnowledgeStore(os.path.join(self.project_root, "knowledge_base"))
        self.db_path = self.knowledge.db_path
        self.review_dir = os.path.join(self.knowledge.root, "review")
        if not os.path.isdir(self.review_dir):
            os.makedirs(self.review_dir)
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
                CREATE TABLE IF NOT EXISTS knowledge_document_pages (
                    document_id INTEGER NOT NULL, page_number INTEGER NOT NULL,
                    text_content TEXT NOT NULL DEFAULT '', extraction_status TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL, PRIMARY KEY(document_id,page_number)
                );
                CREATE TABLE IF NOT EXISTS knowledge_document_diligence (
                    document_id INTEGER PRIMARY KEY, document_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL, sensitive INTEGER NOT NULL DEFAULT 0,
                    visual_review_required INTEGER NOT NULL DEFAULT 0,
                    extractor_version TEXT NOT NULL, fact_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_entity_candidates (
                    id TEXT PRIMARY KEY, graph_entity_id TEXT NOT NULL UNIQUE,
                    entity_type TEXT NOT NULL, canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL, status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_fact_candidates (
                    id TEXT PRIMARY KEY, document_id INTEGER NOT NULL, document_sha256 TEXT NOT NULL,
                    fact_type TEXT NOT NULL, subject_entity_id TEXT NOT NULL DEFAULT '',
                    subject_name TEXT NOT NULL DEFAULT '', predicate TEXT NOT NULL,
                    display_value TEXT NOT NULL, normalized_value TEXT NOT NULL DEFAULT '',
                    unit TEXT NOT NULL DEFAULT '', period_start TEXT NOT NULL DEFAULT '',
                    period_end TEXT NOT NULL DEFAULT '', as_of_date TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL, confidence REAL NOT NULL,
                    status TEXT NOT NULL, conflict_group TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL, generation_method TEXT NOT NULL,
                    graph_node_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(document_id,fact_type,predicate,normalized_value,evidence_json)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_facts_status
                    ON knowledge_fact_candidates(status,fact_type,updated_at DESC);
                CREATE TABLE IF NOT EXISTS knowledge_fact_audit (
                    id TEXT PRIMARY KEY, fact_id TEXT NOT NULL, action TEXT NOT NULL,
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                    username TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_diligence_batches (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, document_ids_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT ''
                );
            """)

    @staticmethod
    def _page_text(path, fallback):
        if os.path.splitext(path)[1].lower() != ".pdf":
            return [(1, _clean_unicode(fallback or extract_text(path)), "已提取")]
        try:
            from pypdf import PdfReader
            reader = PdfReader(path)
            result = []
            for index, page in enumerate(reader.pages):
                text = _clean_unicode(page.extract_text() or "")
                result.append((index + 1, text, "已提取" if text.strip() else "无文本层，需视觉复核"))
            return result
        except (ImportError, OSError, ValueError):
            return [(1, fallback or "", "已提取" if (fallback or "").strip() else "无文本层，需视觉复核")]

    @staticmethod
    def _sensitive(filename, text):
        sample = "%s\n%s" % (filename, text[:500000])
        return (any(word in sample for word in SENSITIVE_WORDS) or bool(ID_RE.search(sample))
                or bool(PHONE_RE.search(sample)) or bool(ACCOUNT_RE.search(sample)))

    def _known_subject(self, connection, document):
        candidates = [document["organization"], document["product"]]
        rows = connection.execute("SELECT id,entity_type,canonical_name FROM knowledge_entities").fetchall()
        for value in candidates:
            key = normalize_name(value)
            if not key:
                continue
            for row in rows:
                if normalize_name(row["canonical_name"]) == key:
                    return row["id"], row["canonical_name"]
        name = str(document["organization"] or document["product"] or "").strip()
        if not name:
            stem = os.path.splitext(document["original_name"])[0]
            stem = re.sub(r"[（(【\[].*?[）)】\]]|20\d{2}.*$", "", stem).strip(" _-—")
            match = re.match(r"([\u4e00-\u9fffA-Za-z0-9]{2,20}(?:资产|投资|基金))", stem)
            name = match.group(1) if match else ""
        if not name:
            return "document-%s" % document["id"], document["title"]
        entity_type = "管理人" if not document["product"] else "底层私募产品"
        normalized = normalize_name(name)
        graph_id = _stable("candidate-entity", entity_type, normalized)
        now = _now()
        existing = connection.execute(
            "SELECT * FROM knowledge_entity_candidates WHERE graph_entity_id=?", (graph_id,)).fetchone()
        if not existing:
            candidate_id = str(uuid.uuid4())
            connection.execute("""INSERT INTO knowledge_entity_candidates
                (id,graph_entity_id,entity_type,canonical_name,normalized_name,status,metadata_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'待确认','{}',?,?)""",
                               (candidate_id, graph_id, entity_type, name, normalized, now, now))
        connection.execute("""INSERT OR IGNORE INTO knowledge_entities
            (id,entity_type,canonical_name,metadata_json,updated_at) VALUES(?,?,?,?,?)""",
                           (graph_id, entity_type, name,
                            json.dumps({"candidate": True}, ensure_ascii=False), now))
        return graph_id, name

    @staticmethod
    def _fact_node_type(fact_type):
        return {"产品信息": "底层私募产品", "产品条款": "产品条款",
                "风控信息": "风控信息"}.get(fact_type, fact_type)

    @staticmethod
    def _fact_relation(fact_type):
        return {"公司信息": "管理人具有公司信息", "人员": "管理人具有职业人员",
                "产品信息": "管理人管理产品", "产品条款": "产品具有条款",
                "投资策略": "管理人实施策略", "业绩记录": "材料披露业绩",
                "风控信息": "管理人采用风控规则"}[fact_type]

    def _insert_fact(self, connection, document, fact_type, predicate, display, page, confidence,
                     normalized="", unit="", period_start="", period_end="", as_of="",
                     method="本地证据规则"):
        if fact_type not in FACT_TYPES or not str(display or "").strip():
            return False
        display = _redact(str(display).strip())[:500]
        evidence = [{"location": "第%d页" % page, "excerpt": display[:300]}]
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        normalized = str(normalized or display).strip()[:500]
        key = (document["id"], fact_type, predicate, normalized, evidence_json)
        if connection.execute("""SELECT id FROM knowledge_fact_candidates WHERE
            document_id=? AND fact_type=? AND predicate=? AND normalized_value=? AND evidence_json=?""", key).fetchone():
            return False
        subject_id, subject_name = self._known_subject(connection, document)
        fact_id = str(uuid.uuid4())
        graph_id = "fact-%s" % fact_id
        now = _now()
        connection.execute("""INSERT INTO knowledge_fact_candidates
            (id,document_id,document_sha256,fact_type,subject_entity_id,subject_name,predicate,
             display_value,normalized_value,unit,period_start,period_end,as_of_date,evidence_json,
             confidence,status,source_kind,generation_method,graph_node_id,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (fact_id, document["id"], document["sha256"], fact_type, subject_id,
                            subject_name, predicate, display, normalized, unit, period_start,
                            period_end, as_of, evidence_json, float(confidence), "待确认",
                            "知识库材料", method, graph_id, now, now))
        metadata = {"predicate": predicate, "value": display, "unit": unit,
                    "period_start": period_start, "period_end": period_end,
                    "as_of_date": as_of, "document_id": document["id"]}
        node_type = self._fact_node_type(fact_type)
        connection.execute("INSERT OR REPLACE INTO knowledge_entities VALUES(?,?,?,?,?)",
                           (graph_id, node_type, "%s：%s" % (predicate, display[:120]),
                            json.dumps(metadata, ensure_ascii=False), now))
        relation = self._fact_relation(fact_type)
        edge_id = _stable("edge", subject_id, graph_id, relation, document["id"], fact_id)
        connection.execute("""INSERT OR REPLACE INTO knowledge_graph_edges
            (id,from_entity_id,to_entity_id,relation_type,source_kind,source_ref,evidence_json,
             confidence,confirmation_status,generation_method,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (edge_id, subject_id, graph_id, relation, "知识库材料", fact_id,
                            evidence_json, float(confidence), "待人工确认", method, now, now))
        document_id = "document-%s" % document["id"]
        connection.execute("INSERT OR IGNORE INTO knowledge_entities VALUES(?,?,?,?,?)",
                           (document_id, "文档", document["title"],
                            json.dumps({"document_id": document["id"], "document_type": document["document_type"]}, ensure_ascii=False), now))
        doc_edge = _stable("edge", document_id, graph_id, "文档披露事实", fact_id)
        connection.execute("""INSERT OR REPLACE INTO knowledge_graph_edges
            (id,from_entity_id,to_entity_id,relation_type,source_kind,source_ref,evidence_json,
             confidence,confirmation_status,generation_method,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (doc_edge, document_id, graph_id, "文档披露事实", "知识库材料", fact_id,
                            evidence_json, float(confidence), "待人工确认", method, now, now))
        if fact_type == "业绩记录" and normalized and (as_of or period_start or period_end):
            scope = "%s|%s|%s" % (as_of, period_start, period_end)
            rows = connection.execute("""SELECT id,normalized_value FROM knowledge_fact_candidates
                WHERE subject_entity_id=? AND fact_type='业绩记录' AND predicate=?
                  AND (as_of_date||'|'||period_start||'|'||period_end)=? AND status<>'已驳回'""",
                                      (subject_id, predicate, scope)).fetchall()
            distinct = {row["normalized_value"] for row in rows}
            conflict_group = _stable("conflict", subject_id, predicate, scope) if len(distinct) > 1 else ""
            for row in rows:
                connection.execute("UPDATE knowledge_fact_candidates SET conflict_group=? WHERE id=?",
                                   (conflict_group, row["id"]))
        return True

    def _extract_document_facts(self, connection, document, pages):
        inserted = 0
        seen = set()
        for page_number, text, _status in pages:
            for sentence in _sentences(text):
                for predicate, words in COMPANY_PREDICATES.items():
                    if any(word in sentence for word in words) and ("公司" in sentence or predicate != "公司简介"):
                        key = ("公司信息", predicate, sentence)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "公司信息", predicate,
                                                             sentence, page_number, .82))
                            seen.add(key)
                for predicate, words in TERM_PREDICATES.items():
                    if any(word in sentence for word in words):
                        key = ("产品条款", predicate, sentence)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "产品条款", predicate,
                                                             sentence, page_number, .86))
                            seen.add(key)
                for predicate, words in PRODUCT_PREDICATES.items():
                    if any(word in sentence for word in words):
                        key = ("产品信息", predicate, sentence)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "产品信息", predicate,
                                                             sentence, page_number, .84))
                            seen.add(key)
                for predicate, words in RISK_PREDICATES.items():
                    if any(word in sentence for word in words):
                        key = ("风控信息", predicate, sentence)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "风控信息", predicate,
                                                             sentence, page_number, .82))
                            seen.add(key)
                for role in PERSON_ROLES:
                    matches = list(re.finditer(r"([\u4e00-\u9fff]{2,4})(?:先生|女士)?[^。；\n]{0,18}%s" % role, sentence))
                    matches += list(re.finditer(re.escape(role) + r"[^\u4e00-\u9fff]{0,3}([\u4e00-\u9fff]{2,4})", sentence))
                    for match in matches:
                        name = match.group(1)
                        if name in ("公司", "团队", "核心", "现任", "担任"):
                            continue
                        key = ("人员", role, name)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "人员", role,
                                                             "%s · %s" % (name, sentence), page_number,
                                                             .78, normalized=name))
                            seen.add(key)
                for strategy in STRATEGY_NAMES:
                    if strategy.lower() in sentence.lower():
                        key = ("投资策略", "策略名称", strategy)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "投资策略", "策略名称",
                                                             "%s · %s" % (strategy, sentence), page_number,
                                                             .84, normalized=strategy))
                            seen.add(key)
                for metric in PERFORMANCE_NAMES:
                    match = re.search(re.escape(metric) + r"[^\d\-]{0,30}(-?\d+(?:\.\d+)?)\s*(%|倍)?", sentence)
                    if match:
                        value, unit = match.group(1), match.group(2) or ""
                        key = ("业绩记录", metric, value, unit, sentence)
                        if key not in seen:
                            inserted += int(self._insert_fact(connection, document, "业绩记录", metric,
                                                             sentence, page_number, .9,
                                                             normalized=value, unit=unit,
                                                             as_of=document["document_date"]))
                            seen.add(key)
            if inserted >= 80:
                break
        return inserted

    @staticmethod
    def _rebuild_conflicts(connection):
        connection.execute("UPDATE knowledge_fact_candidates SET conflict_group='' WHERE fact_type='业绩记录'")
        rows = connection.execute("""SELECT id,document_id,subject_entity_id,predicate,normalized_value,
            as_of_date,period_start,period_end FROM knowledge_fact_candidates
            WHERE fact_type='业绩记录' AND status<>'已驳回' AND subject_entity_id NOT LIKE 'document-%'
              AND (as_of_date<>'' OR period_start<>'' OR period_end<>'')""").fetchall()
        groups = {}
        for row in rows:
            key = (row["subject_entity_id"], row["predicate"], row["as_of_date"],
                   row["period_start"], row["period_end"])
            groups.setdefault(key, []).append(row)
        for key, items in groups.items():
            if len({item["document_id"] for item in items}) < 2:
                continue
            if len({item["normalized_value"] for item in items}) < 2:
                continue
            conflict_group = _stable("conflict", *key)
            connection.executemany("UPDATE knowledge_fact_candidates SET conflict_group=? WHERE id=?",
                                   [(conflict_group, item["id"]) for item in items])

    def prepare(self, document_ids=None):
        clauses, params = ["active=1"], []
        if document_ids:
            ids = [int(value) for value in document_ids][:200]
            clauses.append("id IN (%s)" % ",".join("?" for _value in ids))
            params.extend(ids)
        with self._connect() as connection:
            documents = connection.execute(
                "SELECT * FROM documents WHERE %s ORDER BY id" % " AND ".join(clauses), params).fetchall()
        result = {"documents": 0, "unchanged": 0, "facts": 0, "sensitive": 0,
                  "visual_review_required": 0}
        for document in documents:
            with self._connect() as connection:
                current = connection.execute("""SELECT document_sha256,extractor_version,fact_count
                    FROM knowledge_document_diligence WHERE document_id=?""", (document["id"],)).fetchone()
                page_count = connection.execute("SELECT COUNT(1) FROM knowledge_document_pages WHERE document_id=?",
                                                (document["id"],)).fetchone()[0]
            if (current and current["document_sha256"] == document["sha256"]
                    and current["extractor_version"] == EXTRACTOR_VERSION and page_count):
                result["unchanged"] += 1
                result["facts"] += current["fact_count"]
                continue
            path = os.path.join(self.knowledge.files_dir, document["stored_name"])
            if not os.path.isfile(path):
                continue
            pages = self._page_text(path, document["text_content"])
            full_text = "\n".join(page[1] for page in pages)
            sensitive = self._sensitive(document["original_name"], full_text)
            visual = not full_text.strip() or any(status != "已提取" for _n, _t, status in pages)
            with self._connect() as connection:
                connection.execute("DELETE FROM knowledge_document_pages WHERE document_id=?", (document["id"],))
                for page_number, text, status in pages:
                    connection.execute("INSERT INTO knowledge_document_pages VALUES(?,?,?,?,?)",
                                       (document["id"], page_number, _redact(text), status,
                                        hashlib.sha256(text.encode("utf-8")).hexdigest()))
                self._extract_document_facts(connection, document, pages) if not sensitive else 0
                facts = connection.execute(
                    "SELECT COUNT(1) FROM knowledge_fact_candidates WHERE document_id=?", (document["id"],)).fetchone()[0]
                state = "敏感受限" if sensitive else ("待视觉复核" if visual else "待助手审阅")
                connection.execute("""INSERT OR REPLACE INTO knowledge_document_diligence
                    (document_id,document_sha256,status,sensitive,visual_review_required,extractor_version,
                     fact_count,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                                   (document["id"], document["sha256"], state, int(sensitive), int(visual),
                                    EXTRACTOR_VERSION, facts, _now()))
            result["documents"] += 1
            result["facts"] += facts
            result["sensitive"] += int(sensitive)
            result["visual_review_required"] += int(visual)
        with self._connect() as connection:
            self._rebuild_conflicts(connection)
        return result

    def export_batch(self, batch_id=None):
        self.prepare()
        batch_id = batch_id or str(uuid.uuid4())
        if not UUID_RE.match(batch_id):
            raise ValueError("批次ID无效")
        with self._connect() as connection:
            used = set()
            for batch in connection.execute("SELECT document_ids_json FROM knowledge_diligence_batches"):
                used.update(int(value) for value in _json(batch["document_ids_json"], []) if str(value).isdigit())
            rows = connection.execute("""SELECT d.id,d.sha256,d.title,d.original_name,k.status
                FROM documents d JOIN knowledge_document_diligence k ON k.document_id=d.id
                WHERE d.active=1 AND k.sensitive=0 AND k.status IN ('待助手审阅','待视觉复核')
                ORDER BY d.id""").fetchall()
            rows = [row for row in rows if row["id"] not in used][:5]
            if not rows:
                raise ValueError("没有新的非敏感知识库文档待导出")
            items = []
            for row in rows:
                pages = connection.execute("""SELECT page_number,text_content,extraction_status
                    FROM knowledge_document_pages WHERE document_id=? ORDER BY page_number""", (row["id"],)).fetchall()
                items.append({"document_id": row["id"], "sha256": row["sha256"], "title": row["title"],
                              "filename": row["original_name"], "review_status": row["status"],
                              "pages": [{"page": page["page_number"], "status": page["extraction_status"],
                                         "text": page["text_content"][:20000]} for page in pages]})
            document_ids = [item["document_id"] for item in items]
            connection.execute("INSERT INTO knowledge_diligence_batches VALUES(?,?,?,2,?,'')",
                               (batch_id, "待助手审阅", json.dumps(document_ids), _now()))
        payload = {"schema_version": 2, "batch_id": batch_id, "created_at": _now(),
                   "fact_types": list(FACT_TYPES),
                   "instructions": "仅提取有页码证据的尽调事实；不得输出联系方式、身份或账户信息；图表只抄录明确标注数值，不估算曲线。",
                   "items": items}
        path = os.path.join(self.review_dir, "diligence-%s.json" % batch_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return {"batch_id": batch_id, "count": len(items), "path": path,
                "prepared": self.coverage()}

    def import_batch(self, batch_id):
        if not UUID_RE.match(str(batch_id or "")):
            raise ValueError("批次ID无效")
        path = os.path.join(self.review_dir, "diligence-%s.review.json" % batch_id)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("batch_id") != batch_id or not isinstance(payload.get("items"), list):
            raise ValueError("尽调审阅结果格式无效")
        inserted = 0
        with self._connect() as connection:
            batch = connection.execute("SELECT * FROM knowledge_diligence_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch or batch["status"] != "待助手审阅":
                raise ValueError("批次不存在或已导入")
            allowed = set(_json(batch["document_ids_json"], []))
            for item in payload["items"]:
                document_id = int(item.get("document_id") or 0)
                if document_id not in allowed:
                    raise ValueError("审阅结果包含未登记文档")
                document = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
                if not document or item.get("sha256") != document["sha256"]:
                    raise ValueError("文档哈希不一致")
                for fact in item.get("facts") or []:
                    fact_type = fact.get("fact_type")
                    predicate = str(fact.get("predicate") or "").strip()[:100]
                    display = str(fact.get("display_value") or "").strip()[:500]
                    page = int(fact.get("page") or 0)
                    evidence = connection.execute("SELECT text_content FROM knowledge_document_pages WHERE document_id=? AND page_number=?",
                                                  (document_id, page)).fetchone()
                    if fact_type not in FACT_TYPES or not predicate or not display or not evidence:
                        raise ValueError("事实类型、字段或证据页无效")
                    if display not in evidence["text_content"] and str(fact.get("evidence_excerpt") or "") not in evidence["text_content"]:
                        raise ValueError("事实缺少对应页证据")
                    inserted += int(self._insert_fact(connection, document, fact_type, predicate, display,
                                                     page, float(fact.get("confidence") or 0),
                                                     normalized=fact.get("normalized_value") or "",
                                                     unit=fact.get("unit") or "", period_start=fact.get("period_start") or "",
                                                     period_end=fact.get("period_end") or "", as_of=fact.get("as_of_date") or "",
                                                     method="Codex脱敏审阅"))
            connection.execute("UPDATE knowledge_diligence_batches SET status='已导入',imported_at=? WHERE id=?",
                               (_now(), batch_id))
        return {"batch_id": batch_id, "inserted": inserted}

    def facts(self, status="", fact_type="", document_id="", query="", conflict="", page=1, page_size=50):
        page, page_size = max(1, int(page)), max(20, min(200, int(page_size)))
        clauses, params = [], []
        if status:
            if status not in FACT_STATUSES:
                raise ValueError("事实状态无效")
            clauses.append("f.status=?")
            params.append(status)
        if fact_type:
            if fact_type not in FACT_TYPES:
                raise ValueError("事实类型无效")
            clauses.append("f.fact_type=?")
            params.append(fact_type)
        if document_id:
            clauses.append("f.document_id=?")
            params.append(int(document_id))
        if query:
            clauses.append("(f.subject_name LIKE ? OR f.predicate LIKE ? OR f.display_value LIKE ? OR d.title LIKE ?)")
            params.extend(["%%%s%%" % str(query)[:100]] * 4)
        if conflict == "yes":
            clauses.append("f.conflict_group<>''")
        elif conflict == "no":
            clauses.append("f.conflict_group='' ")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = connection.execute("""SELECT COUNT(1) FROM knowledge_fact_candidates f
                JOIN documents d ON d.id=f.document_id%s""" % where, params).fetchone()[0]
            rows = connection.execute("""SELECT f.*,d.title AS document_title,d.original_name
                FROM knowledge_fact_candidates f JOIN documents d ON d.id=f.document_id%s
                ORDER BY f.updated_at DESC LIMIT ? OFFSET ?""" % where,
                                      params + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _json(item.pop("evidence_json"), [])
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "fact_types": list(FACT_TYPES), "statuses": list(FACT_STATUSES)}

    def _apply_fact(self, connection, fact_id, expected_revision, action, values, username):
        row = connection.execute("SELECT * FROM knowledge_fact_candidates WHERE id=?", (fact_id,)).fetchone()
        if not row:
            raise KeyError("事实不存在")
        if int(expected_revision) != row["revision"]:
            raise RuntimeError("事实已被其他页面修改")
        if action not in ("confirm", "reject", "update"):
            raise ValueError("事实操作无效")
        before = dict(row)
        status = {"confirm": "已确认", "reject": "已驳回"}.get(action, row["status"])
        predicate = str((values or {}).get("predicate", row["predicate"]) or "").strip()[:100]
        display = str((values or {}).get("display_value", row["display_value"]) or "").strip()[:500]
        normalized = str((values or {}).get("normalized_value", row["normalized_value"]) or "").strip()[:500]
        subject = str((values or {}).get("subject_entity_id", row["subject_entity_id"]) or "")[:100]
        if action == "update" and (not predicate or not display):
            raise ValueError("事实字段不能为空")
        if subject and not connection.execute("SELECT id FROM knowledge_entities WHERE id=?", (subject,)).fetchone():
            raise ValueError("关联实体不存在")
        now = _now()
        connection.execute("""UPDATE knowledge_fact_candidates SET status=?,predicate=?,display_value=?,
            normalized_value=?,subject_entity_id=?,revision=revision+1,updated_at=? WHERE id=?""",
                           (status, predicate, display, normalized, subject, now, fact_id))
        edge_status = "已确认" if status == "已确认" else ("已驳回" if status == "已驳回" else "待人工确认")
        connection.execute("UPDATE knowledge_graph_edges SET confirmation_status=?,updated_at=? WHERE source_ref=?",
                           (edge_status, now, fact_id))
        if subject != row["subject_entity_id"]:
            relation = self._fact_relation(row["fact_type"])
            connection.execute("""DELETE FROM knowledge_graph_edges
                WHERE source_ref=? AND relation_type<>'文档披露事实'""", (fact_id,))
            edge_id = _stable("edge", subject, row["graph_node_id"], relation,
                              row["document_id"], fact_id)
            connection.execute("""INSERT INTO knowledge_graph_edges
                (id,from_entity_id,to_entity_id,relation_type,source_kind,source_ref,evidence_json,
                 confidence,confirmation_status,generation_method,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (edge_id, subject, row["graph_node_id"], relation, row["source_kind"],
                                fact_id, row["evidence_json"], row["confidence"], edge_status,
                                row["generation_method"], now, now))
        if status == "已驳回":
            connection.execute("DELETE FROM knowledge_entities WHERE id=?", (row["graph_node_id"],))
        else:
            metadata = _json(connection.execute("SELECT metadata_json FROM knowledge_entities WHERE id=?",
                                                (row["graph_node_id"],)).fetchone()[0], {})
            metadata.update({"predicate": predicate, "value": display})
            connection.execute("UPDATE knowledge_entities SET canonical_name=?,metadata_json=?,updated_at=? WHERE id=?",
                               ("%s：%s" % (predicate, display[:120]), json.dumps(metadata, ensure_ascii=False),
                                now, row["graph_node_id"]))
        if status == "已确认" and subject:
            connection.execute("""UPDATE knowledge_entity_candidates SET status='已确认',
                revision=revision+1,updated_at=? WHERE graph_entity_id=? AND status='待确认'""",
                               (now, subject))
        after = dict(connection.execute("SELECT * FROM knowledge_fact_candidates WHERE id=?", (fact_id,)).fetchone())
        connection.execute("INSERT INTO knowledge_fact_audit VALUES(?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), fact_id, action, json.dumps(before, ensure_ascii=False),
                            json.dumps(after, ensure_ascii=False), str(username or "")[:100], now))
        return after

    def update_fact(self, fact_id, expected_revision, action, values=None, username=""):
        if not UUID_RE.match(str(fact_id or "")):
            raise ValueError("事实ID无效")
        with self._connect() as connection:
            return self._apply_fact(connection, fact_id, expected_revision, action, values or {}, username)

    def batch_update(self, items, action, username=""):
        if action not in ("confirm", "reject") or not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValueError("批量审核需提供1至200条事实及固定操作")
        with self._connect() as connection:
            results = []
            for item in items:
                fact_id = str(item.get("id") or "")
                if not UUID_RE.match(fact_id):
                    raise ValueError("事实ID无效")
                results.append(self._apply_fact(connection, fact_id, item.get("expected_revision"),
                                                action, {}, username))
        return {"updated": len(results)}

    def dossier(self, entity_id):
        if not re.match(r"^[A-Za-z0-9_-]{1,100}$", str(entity_id or "")):
            raise ValueError("实体ID无效")
        with self._connect() as connection:
            entity = connection.execute("SELECT * FROM knowledge_entities WHERE id=?", (entity_id,)).fetchone()
            if not entity:
                raise KeyError("实体不存在")
            edges = connection.execute("""SELECT * FROM knowledge_graph_edges
                WHERE from_entity_id=? OR to_entity_id=? ORDER BY confirmation_status,relation_type""",
                                       (entity_id, entity_id)).fetchall()
            ids = {edge["to_entity_id"] if edge["from_entity_id"] == entity_id else edge["from_entity_id"]
                   for edge in edges}
            related = {}
            if ids:
                marks = ",".join("?" for _value in ids)
                related = {row["id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM knowledge_entities WHERE id IN (%s)" % marks, list(ids))}
        groups = {}
        for edge in edges:
            other_id = edge["to_entity_id"] if edge["from_entity_id"] == entity_id else edge["from_entity_id"]
            other = related.get(other_id)
            if not other:
                continue
            groups.setdefault(other["entity_type"], []).append({"entity": other, "relation": edge["relation_type"],
                                                                  "status": edge["confirmation_status"],
                                                                  "confidence": edge["confidence"],
                                                                  "evidence": _json(edge["evidence_json"], {})})
        return {"entity": dict(entity), "groups": groups}

    def coverage(self):
        with self._connect() as connection:
            documents = connection.execute("SELECT COUNT(1) FROM knowledge_document_diligence").fetchone()[0]
            visual = connection.execute("SELECT COUNT(1) FROM knowledge_document_diligence WHERE visual_review_required=1").fetchone()[0]
            sensitive = connection.execute("SELECT COUNT(1) FROM knowledge_document_diligence WHERE sensitive=1").fetchone()[0]
            rows = connection.execute("SELECT status,COUNT(1) FROM knowledge_fact_candidates GROUP BY status").fetchall()
            conflicts = connection.execute("SELECT COUNT(1) FROM knowledge_fact_candidates WHERE conflict_group<>''").fetchone()[0]
        return {"documents": documents, "visual_review_required": visual, "sensitive": sensitive,
                "facts": {row["status"]: row[1] for row in rows}, "conflicts": conflicts}
