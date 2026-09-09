import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from xml.etree import ElementTree

from openpyxl import load_workbook


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".html", ".htm"}
MAX_FILE_SIZE = 50 * 1024 * 1024
DOCUMENT_TYPES = {"产品合同", "会议纪要", "产品路演材料", "投资观点", "产品报告", "市场报告", "微信文章", "其他"}
TYPE_KEYWORDS = (("月报", "产品报告"), ("周报", "产品报告"), ("日报", "产品报告"),
                 ("业绩报告", "产品报告"), ("净值报告", "产品报告"), ("估值表", "产品报告"),
                 ("路演", "产品路演材料"), ("会议纪要", "会议纪要"), ("纪要", "会议纪要"),
                 ("合同", "产品合同"), ("协议", "产品合同"),
                 ("投资观点", "投资观点"), ("市场报告", "市场报告"), ("策略报告", "市场报告"),
                 ("市场策略", "市场报告"), ("微信", "微信文章"))


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _safe_name(name):
    value = os.path.basename(str(name or "")).strip()
    if not value or value in (".", ".."):
        raise ValueError("文件名无效")
    return re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _xml_text(data):
    root = ElementTree.fromstring(data)
    values = []
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1]
        if node.text and name in ("t", "v"):
            values.append(node.text)
        if name in ("p", "tr"):
            values.append("\n")
    return " ".join(values).replace(" \n ", "\n")


def _extract_zip_xml(path, prefixes):
    parts = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist()
                       if any(name.startswith(prefix) for prefix in prefixes) and name.endswith(".xml"))
        for name in names:
            try:
                parts.append(_xml_text(archive.read(name)))
            except (ElementTree.ParseError, KeyError):
                continue
    return "\n".join(part for part in parts if part.strip())


def _read_text_file(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def extract_text(path):
    extension = os.path.splitext(path)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError("不支持的文件类型：%s" % extension)
    if extension in (".txt", ".md"):
        return _read_text_file(path)
    if extension in (".html", ".htm"):
        raw = _read_text_file(path)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()
    if extension == ".docx":
        return _extract_zip_xml(path, ("word/document.xml", "word/header", "word/footer"))
    if extension == ".pptx":
        return _extract_zip_xml(path, ("ppt/slides/slide", "ppt/notesSlides/notesSlide"))
    if extension == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=True)
        parts = []
        try:
            for sheet in book.worksheets:
                parts.append("[%s]" % sheet.title)
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value) for value in row if value not in (None, "")]
                    if values:
                        parts.append("\t".join(values))
        finally:
            book.close()
        return "\n".join(parts)
    if extension == ".xls":
        import xlrd
        book = xlrd.open_workbook(path, on_demand=True)
        parts = []
        for sheet in book.sheets():
            parts.append("[%s]" % sheet.name)
            for row_index in range(sheet.nrows):
                values = [str(value) for value in sheet.row_values(row_index) if value not in (None, "")]
                if values:
                    parts.append("\t".join(values))
        book.release_resources()
        return "\n".join(parts)
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except (ImportError, OSError, ValueError):
        # Keep a dependency-free fallback for simple, uncompressed PDF text
        # layers. Image-only/scanned documents are deliberately flagged for
        # future OCR instead of inventing content.
        with open(path, "rb") as handle:
            raw = handle.read()
        chunks = re.findall(rb"\(([^()]*)\)\s*Tj", raw)
        return b" ".join(chunks).decode("utf-8", "ignore")


def infer_metadata(filename, text):
    sample = (filename + "\n" + (text or "")[:10000]).strip()
    document_type = "其他"
    # 文件名优先：报告类关键词在文件名中出现即可定型，避免正文偶然提及"合同"等污染
    filename_sample = filename or ""
    for keyword, value in TYPE_KEYWORDS:
        if keyword in filename_sample:
            document_type = value
            break
    else:
        body = (text or "")[:10000]
        # 正文中只有较强的信号才覆盖：完整词组而非单字
        for keyword, value in (("投资观点", "投资观点"), ("会议纪要", "会议纪要"),
                               ("路演材料", "产品路演材料"), ("市场报告", "市场报告")):
            if keyword in body:
                document_type = value
                break
    match = re.search(r"(20\d{2})[-年./](\d{1,2})[-月./](\d{1,2})日?", sample)
    document_date = "%04d-%02d-%02d" % tuple(map(int, match.groups())) if match else ""
    product = ""
    try:
        from .config import PRODUCTS
        for name, aliases in PRODUCTS.items():
            if name in sample or any(alias and alias in sample for alias in aliases):
                product = name
                break
    except (ImportError, TypeError):
        pass
    tags = [keyword for keyword in ("FOF", "量化", "固收", "股票", "CTA", "宏观", "信用", "利率")
            if keyword.lower() in sample.lower()]
    # 机构识别：只认"公司/资产管理"等完整结尾的机构名，且不超过25字
    # （排除"本报告仅向特定合格投资者…"这类被截断的免责声明长句）
    organization_match = re.search(
        r"([\u4e00-\u9fffA-Za-z0-9]{2,18}(?:证券|基金|资产管理|银行|保险|期货)(?:有限责任公司|有限公司|股份有限公司))",
        sample)
    author_match = re.search(r"(?:作者|主讲|发言人|记录人)\s*[:：]\s*([\u4e00-\u9fff]{2,10})", sample)
    return {"document_type": document_type, "document_date": document_date,
            "product": product, "organization": organization_match.group(1) if organization_match else "",
            "author": author_match.group(1) if author_match else "", "tags": tags}


class KnowledgeStore:
    def __init__(self, root="knowledge_base"):
        self.root = os.path.abspath(root)
        self.files_dir = os.path.join(self.root, "files")
        self.trash_dir = os.path.join(self.root, "trash")
        self.inbox_dir = os.path.join(self.root, "inbox")
        self.db_path = os.path.join(self.root, "knowledge.sqlite3")
        for path in (self.root, self.files_dir, self.trash_dir, self.inbox_dir):
            if not os.path.isdir(path):
                os.makedirs(path)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha256 TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL, extension TEXT NOT NULL,
                    size INTEGER NOT NULL, title TEXT NOT NULL, document_type TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '', document_date TEXT NOT NULL DEFAULT '',
                    product TEXT NOT NULL DEFAULT '', organization TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
                    version TEXT NOT NULL DEFAULT '', text_content TEXT NOT NULL DEFAULT '',
                    extraction_status TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(active, updated_at DESC);
                CREATE TABLE IF NOT EXISTS document_chunks (
                    document_id INTEGER NOT NULL REFERENCES documents(id), chunk_index INTEGER NOT NULL,
                    text_content TEXT NOT NULL, PRIMARY KEY(document_id, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS document_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_document_id INTEGER NOT NULL REFERENCES documents(id),
                    to_document_id INTEGER NOT NULL REFERENCES documents(id),
                    relation_type TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(from_document_id, to_document_id, relation_type)
                );
            """)
            try:
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(title, original_name, text_content, tags)")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _metadata(values, filename):
        values = values or {}
        document_type = str(values.get("document_type") or "其他").strip()
        if document_type not in DOCUMENT_TYPES:
            raise ValueError("文档类型无效")
        tags = values.get("tags") or []
        if isinstance(tags, str):
            tags = [item.strip() for item in re.split(r"[,，;；]", tags) if item.strip()]
        if not isinstance(tags, list) or len(tags) > 50:
            raise ValueError("标签格式无效")
        return {
            "title": str(values.get("title") or os.path.splitext(filename)[0]).strip()[:300],
            "document_type": document_type,
            "source": str(values.get("source") or "").strip()[:300],
            "document_date": str(values.get("document_date") or "").strip()[:20],
            "product": str(values.get("product") or "").strip()[:300],
            "organization": str(values.get("organization") or "").strip()[:300],
            "author": str(values.get("author") or "").strip()[:200],
            "tags": json.dumps(tags, ensure_ascii=False),
            "version": str(values.get("version") or "").strip()[:100],
        }

    def add_file(self, source_path, metadata=None, original_name=None):
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise ValueError("文件不存在")
        size = os.path.getsize(source_path)
        if size <= 0 or size > MAX_FILE_SIZE:
            raise ValueError("文件大小必须在1字节至50MB之间")
        filename = _safe_name(original_name or os.path.basename(source_path))
        extension = os.path.splitext(filename)[1].lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("不支持的文件类型：%s" % extension)
        digest = _sha256(source_path)
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM documents WHERE sha256=?", (digest,)).fetchone()
            if existing:
                return self._row(existing), True
        stored_name = digest + extension
        destination = os.path.join(self.files_dir, stored_name)
        if not os.path.exists(destination):
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.files_dir, delete=False) as handle:
                    temporary = handle.name
                    with open(source_path, "rb") as source:
                        shutil.copyfileobj(source, handle)
                os.replace(temporary, destination)
            finally:
                if temporary and os.path.exists(temporary):
                    os.remove(temporary)
        try:
            text = extract_text(destination)
            status = "已提取" if text.strip() else "无文本层，需OCR或人工补录"
        except Exception as exc:
            text = ""
            status = "提取失败：%s" % type(exc).__name__
        inferred = infer_metadata(filename, text)
        supplied = metadata or {}
        inferred.update({key: value for key, value in supplied.items() if value not in (None, "", [])})
        fields = self._metadata(inferred, filename)
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute("""INSERT INTO documents
                (sha256,original_name,stored_name,extension,size,title,document_type,source,document_date,
                 product,organization,author,tags,version,text_content,extraction_status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (digest, filename, stored_name, extension, size, fields["title"], fields["document_type"],
                 fields["source"], fields["document_date"], fields["product"], fields["organization"],
                 fields["author"], fields["tags"], fields["version"], text[:2000000], status, now, now))
            document_id = cursor.lastrowid
            self._replace_chunks(connection, document_id, text)
            self._sync_fts(connection, document_id)
            row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return self._row(row), False

    @staticmethod
    def _replace_chunks(connection, document_id, text, chunk_size=2000):
        connection.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        clean = str(text or "").strip()
        for index, start in enumerate(range(0, len(clean), chunk_size)):
            connection.execute("INSERT INTO document_chunks(document_id,chunk_index,text_content) VALUES(?,?,?)",
                               (document_id, index, clean[start:start + chunk_size]))

    def _sync_fts(self, connection, document_id):
        try:
            connection.execute("DELETE FROM documents_fts WHERE rowid=?", (document_id,))
        except sqlite3.OperationalError:
            pass
        row = connection.execute("SELECT id,title,original_name,text_content,tags FROM documents WHERE id=?", (document_id,)).fetchone()
        if row:
            try:
                connection.execute("INSERT INTO documents_fts(rowid,title,original_name,text_content,tags) VALUES(?,?,?,?,?)", tuple(row))
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _row(row, include_text=False):
        data = dict(row)
        try:
            data["tags"] = json.loads(data.get("tags") or "[]")
        except ValueError:
            data["tags"] = []
        if not include_text:
            data.pop("text_content", None)
            data.pop("stored_name", None)
        return data

    def list(self, query="", include_inactive=False, limit=200):
        limit = max(1, min(int(limit), 500))
        query = str(query or "").strip()[:200]
        if query:
            # Prefer SQLite FTS5 when available. The quoted phrase form is
            # predictable for Chinese text; LIKE remains the compatibility
            # fallback for SQLite builds without FTS5 and partial substrings.
            fts_clauses = [] if include_inactive else ["d.active=1"]
            fts_clauses.append("documents_fts MATCH ?")
            fts_sql = ("SELECT d.* FROM documents d JOIN documents_fts ON documents_fts.rowid=d.id WHERE " +
                       " AND ".join(fts_clauses) + " ORDER BY d.updated_at DESC LIMIT ?")
            phrase = '"' + query.replace('"', '""') + '"'
            try:
                with self._connect() as connection:
                    rows = list(connection.execute(fts_sql, (phrase, limit)))
                if rows:
                    return [self._row(row) for row in rows]
            except sqlite3.OperationalError:
                pass
        clauses = [] if include_inactive else ["active=1"]
        params = []
        if query:
            clauses.append("(title LIKE ? OR original_name LIKE ? OR text_content LIKE ? OR tags LIKE ? OR product LIKE ? OR organization LIKE ?)")
            pattern = "%" + query[:200] + "%"
            params.extend([pattern] * 6)
        sql = "SELECT * FROM documents" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute(sql, params)]

    def get(self, document_id, include_text=True):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id=?", (int(document_id),)).fetchone()
        if not row:
            raise KeyError("文档不存在")
        return self._row(row, include_text)

    def file_path(self, document_id, allow_inactive=False):
        data = self.get(document_id, True)
        if not data["active"] and not allow_inactive:
            raise KeyError("文档已停用")
        base = self.files_dir if data["active"] else self.trash_dir
        path = os.path.abspath(os.path.join(base, data["stored_name"]))
        if os.path.dirname(path) != os.path.abspath(base):
            raise ValueError("文件路径无效")
        return path, data

    def update(self, document_id, values, expected_revision):
        current = self.get(document_id, True)
        if int(expected_revision) != current["revision"]:
            raise ValueError("文档已被其他页面修改，请刷新后重试")
        fields = self._metadata(dict(current, **(values or {})), current["original_name"])
        now = _now()
        with self._connect() as connection:
            connection.execute("""UPDATE documents SET title=?,document_type=?,source=?,document_date=?,product=?,
                organization=?,author=?,tags=?,version=?,revision=revision+1,updated_at=? WHERE id=?""",
                (fields["title"], fields["document_type"], fields["source"], fields["document_date"],
                 fields["product"], fields["organization"], fields["author"], fields["tags"],
                 fields["version"], now, int(document_id)))
            self._sync_fts(connection, int(document_id))
        return self.get(document_id, False)

    def deactivate(self, document_id, expected_revision):
        current = self.get(document_id, True)
        if int(expected_revision) != current["revision"]:
            raise ValueError("文档已被其他页面修改，请刷新后重试")
        source = os.path.join(self.files_dir, current["stored_name"])
        destination = os.path.join(self.trash_dir, current["stored_name"])
        if os.path.exists(source):
            os.replace(source, destination)
        with self._connect() as connection:
            connection.execute("UPDATE documents SET active=0,revision=revision+1,updated_at=? WHERE id=?", (_now(), int(document_id)))
        return self.get(document_id, False)

    def import_inbox(self):
        imported, duplicates, errors = [], [], []
        for name in sorted(os.listdir(self.inbox_dir)):
            path = os.path.join(self.inbox_dir, name)
            if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                item, duplicate = self.add_file(path)
                (duplicates if duplicate else imported).append(item["id"])
            except Exception as exc:
                errors.append({"file": name, "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"imported": imported, "duplicates": duplicates, "errors": errors}

    def rebuild_index(self):
        with self._connect() as connection:
            try:
                connection.execute("DELETE FROM documents_fts")
                connection.execute("INSERT INTO documents_fts(rowid,title,original_name,text_content,tags) SELECT id,title,original_name,text_content,tags FROM documents")
                return {"status": "rebuilt", "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]}
            except sqlite3.OperationalError:
                return {"status": "fts5-unavailable", "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]}

    def integrity_check(self):
        missing, mismatched = [], []
        with self._connect() as connection:
            rows = connection.execute("SELECT id,sha256,stored_name,active FROM documents").fetchall()
            database = connection.execute("PRAGMA integrity_check").fetchone()[0]
        for row in rows:
            base = self.files_dir if row["active"] else self.trash_dir
            path = os.path.join(base, row["stored_name"])
            if not os.path.isfile(path):
                missing.append(row["id"])
            elif _sha256(path) != row["sha256"]:
                mismatched.append(row["id"])
        return {"database": database, "documents": len(rows), "missing": missing, "hash_mismatch": mismatched}

    def export_metadata(self, output_path):
        payload = {"schema_version": 1, "exported_at": _now(), "documents": self.list("", True, 500)}
        directory = os.path.dirname(os.path.abspath(output_path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, output_path)
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)
        return {"path": os.path.abspath(output_path), "documents": len(payload["documents"])}
