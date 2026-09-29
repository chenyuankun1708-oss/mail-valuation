import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .mail import load_env
from .labels import LabelConflictError, build_label_payload, mutate_catalog
from .bottom_returns import analyze_bottom_return
from .otc_store import OtcStore, RevisionConflictError, TaskConflictError
from .llm_gateway import LLMDisabledError, LLMGateway
from .knowledge_sources import KnowledgeSourceStore
from .knowledge_diligence import KnowledgeDiligenceStore
from . import api_v2


class AuthLimiter:
    """Small in-memory failed-login limiter keyed by the original client IP."""

    def __init__(self, max_failures=10, window_seconds=900):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.failures = defaultdict(list)
        self.lock = threading.Lock()

    def _recent(self, client, now):
        cutoff = now - self.window_seconds
        return [value for value in self.failures.get(client, []) if value >= cutoff]

    def blocked(self, client):
        now = time.time()
        with self.lock:
            recent = self._recent(client, now)
            self.failures[client] = recent
            return len(recent) >= self.max_failures

    def failed(self, client):
        now = time.time()
        with self.lock:
            recent = self._recent(client, now)
            recent.append(now)
            self.failures[client] = recent

    def succeeded(self, client):
        with self.lock:
            self.failures.pop(client, None)


def _credentials(env_path):
    env = load_env(env_path)
    user = os.environ.get("SHARE_USER", env.get("SHARE_USER", "")).strip()
    password = os.environ.get("SHARE_PASSWORD", env.get("SHARE_PASSWORD", ""))
    if not user or not password:
        raise RuntimeError("未配置 SHARE_USER 和 SHARE_PASSWORD，拒绝启动分享服务")
    if len(password) < 12:
        raise RuntimeError("SHARE_PASSWORD 至少需要12个字符")
    return user, password


def _system_docs_credentials(env_path):
    env = load_env(env_path)
    user = os.environ.get("SYSTEM_QA_USER", env.get("SYSTEM_QA_USER", "")).strip()
    password = os.environ.get("SYSTEM_QA_PASSWORD", env.get("SYSTEM_QA_PASSWORD", ""))
    if not user or not password:
        raise RuntimeError("未配置 SYSTEM_QA_USER 和 SYSTEM_QA_PASSWORD")
    return user, password


class StrategyTaskManager:
    """Run the fixed strategy rebuild command with one isolated worker."""

    def __init__(self, root, max_tasks=20):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, ".runtime", "strategy-tasks.json")
        self.max_tasks = max_tasks
        self.lock = threading.Lock()
        self.tasks = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.tasks = {item["id"]: item for item in saved.get("tasks", [])}
            for item in self.tasks.values():
                if item.get("status") == "running":
                    item["status"] = "interrupted"
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        ordered = sorted(self.tasks.values(), key=lambda item: item.get("created_at", 0))[-self.max_tasks:]
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tasks": ordered}, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)

    def submit(self):
        with self.lock:
            running = next((item for item in self.tasks.values()
                            if item.get("status") == "running"), None)
            if running:
                return running["id"], False
            task_id = "rebuild-%d" % int(time.time() * 1000)
            self.tasks[task_id] = {"id": task_id, "task_type": "strategy-lab-rebuild",
                                   "status": "running", "created_at": time.time(),
                                   "finished_at": None, "error": None}
            self._save()
        worker = threading.Thread(target=self._run, args=(task_id,))
        worker.daemon = True
        worker.start()
        return task_id, True

    def _run(self, task_id):
        try:
            completed = subprocess.run(
                [sys.executable, os.path.join(self.root, "app.py"), "strategy-lab"],
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=3600, check=False)
            status = "ok" if completed.returncode == 0 else "failed"
            error = None if completed.returncode == 0 else (
                "strategy process exited with code %s" % completed.returncode)
        except Exception as exc:
            status, error = "failed", "%s: %s" % (type(exc).__name__, exc)
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task.update(status=status, error=error, finished_at=time.time())
                self._save()

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None


_LLM_METHODOLOGIES = ("risk_parity_score", "momentum_tilt", "value_tilt", "defensive")


def make_handler(index_path, user, password, limiter=None,
                 system_docs_user=None, system_docs_password=None):
    index_path = os.path.abspath(index_path)
    limiter = limiter or AuthLimiter()
    report_lock = threading.Lock()
    archive_lock = threading.Lock()
    asset_lock = threading.Lock()
    label_lock = threading.Lock()
    knowledge_lock = threading.Lock()
    strategy_tasks = StrategyTaskManager(os.path.dirname(index_path))
    otc_store = OtcStore(os.path.dirname(index_path))
    llm_env = load_env(os.path.join(os.path.dirname(index_path), ".env"))
    llm_env.update({key: value for key, value in os.environ.items()
                    if key.startswith("LLM_") or key.startswith("OLLAMA_")})
    llm_tasks = LLMGateway(os.path.dirname(index_path), otc_store, llm_env)
    knowledge_sources = KnowledgeSourceStore(
        os.path.dirname(index_path), llm_env.get("KNOWLEDGE_WECHAT_ROOT") or None)
    knowledge_diligence = KnowledgeDiligenceStore(os.path.dirname(index_path))
    asset_cache = {}
    module_files = {
        "factors": "factors.json",
        "market-research": "market-research.json",
        "strategy-lab": "strategy-lab.json",
        "underlying-assets": "underlying-assets.json",
        "ledger-tables": "ledger-tables.json",
        "label-workbooks": "label-workbooks.json",
        "portfolio-var": "portfolio-var.json",
        "bottom-returns": "bottom-returns.json",
    }
    expected = "Basic " + base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")
    system_docs_limiter = AuthLimiter(max_failures=5, window_seconds=900)
    with open(__file__, "rb") as source_handle:
        server_code_sha256 = hashlib.sha256(source_handle.read()).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def _client_key(self):
            forwarded = self.headers.get("CF-Connecting-IP", "").strip()
            return forwarded or self.client_address[0]

        def _authenticate(self):
            client = self._client_key()
            if limiter.blocked(client):
                self.send_response(429)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Retry-After", str(limiter.window_seconds))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write("登录失败次数过多，请稍后重试".encode("utf-8"))
                return False
            provided = self.headers.get("Authorization", "")
            if not hmac.compare_digest(provided, expected):
                limiter.failed(client)
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="FOF valuation", charset="UTF-8"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return False
            limiter.succeeded(client)
            return True

        def _serve(self, include_body):
            if not self._authenticate():
                return
            if self.path not in ("/", "/index.html") and not self.path.startswith("/?"):
                self.send_error(404)
                return
            try:
                with open(index_path, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(503, "网页尚未生成，请先运行 python app.py build")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _serve_asset(self, path, content_type, cache_control, include_body=True):
            if not self._authenticate():
                return
            try:
                stat = os.stat(path)
                key = (path, stat.st_mtime_ns, stat.st_size)
                with asset_lock:
                    cached = asset_cache.get(key)
                if cached is None:
                    with open(path, "rb") as handle:
                        raw = handle.read()
                    cached = (raw, gzip.compress(raw, compresslevel=6),
                              '"%s"' % hashlib.sha256(raw).hexdigest())
                    with asset_lock:
                        for old_key in list(asset_cache):
                            if old_key[0] == path and old_key != key:
                                asset_cache.pop(old_key, None)
                        asset_cache[key] = cached
                raw, compressed, etag = cached
            except OSError:
                self.send_error(503, "Requested page asset has not been generated. Run python app.py build.")
                return
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                return
            use_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
            body = compressed if use_gzip else raw
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _serve_monthly_report(self):
            if not self._authenticate():
                return
            parsed = urlsplit(self.path)
            as_of = parse_qs(parsed.query).get("as_of", [""])[0]
            if len(as_of) != 10:
                self.send_error(400, "缺少有效截止日")
                return
            root = os.path.dirname(index_path)
            output_dir = os.path.join(root, ".runtime", "monthly-reports")
            output = os.path.join(output_dir, "FOF月报_%s.xlsx" % as_of)
            try:
                from monthly_report.generate_report import generate_as_of
                with report_lock:
                    generate_as_of(as_of, os.path.join(root, "products"),
                                   os.path.join(root, "专户资金台账.xlsx"), output)
                with open(output, "rb") as handle:
                    body = handle.read()
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(500, "月报生成失败：%s" % exc)
                return
            filename = "FOF月报_%s.xlsx" % as_of
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(filename))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve_valuation_archive(self):
            if not self._authenticate():
                return
            parsed = urlsplit(self.path)
            valuation_date = parse_qs(parsed.query).get("date", [""])[0]
            root = os.path.dirname(index_path)
            try:
                from .valuation_archive import build_valuation_archive
                with archive_lock:
                    body, count = build_valuation_archive(os.path.join(root, "products"), valuation_date)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(500, "估值表压缩包生成失败：%s" % type(exc).__name__)
                return
            filename = "估值表_%s_%s份.zip" % (valuation_date, count)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(filename))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve_system_docs(self):
            if not self._authenticate():
                return
            client = self._client_key()
            if system_docs_limiter.blocked(client):
                self._send_json(429, {"error": "系统文档登录失败次数过多，请稍后重试"})
                return
            try:
                request = self._read_json(max_length=4096)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not system_docs_user or system_docs_password is None:
                self._send_json(503, {"error": "系统文档独立账号尚未配置"})
                return
            supplied_user = str(request.get("username") or "")
            supplied_password = str(request.get("password") or "")
            valid = (hmac.compare_digest(supplied_user, system_docs_user) and
                     hmac.compare_digest(supplied_password, system_docs_password))
            if not valid:
                system_docs_limiter.failed(client)
                self._send_json(403, {"error": "系统文档账号或密码错误"})
                return
            system_docs_limiter.succeeded(client)
            document = str(request.get("document") or "")
            root = os.path.dirname(index_path)
            try:
                if document == "history":
                    path = os.path.join(root, "logs", "CHANGELOG.md")
                    with open(path, "r", encoding="utf-8") as handle:
                        payload = {"document": "history", "title": "历史改动",
                                   "format": "text", "content": handle.read()}
                elif document == "qa":
                    path = os.path.join(root, "web_assets", "system_qa.json")
                    with open(path, "r", encoding="utf-8") as handle:
                        payload = {"document": "qa", "title": "系统QA",
                                   "format": "qa", "content": json.load(handle)}
                else:
                    self._send_json(400, {"error": "系统文档类型不在白名单"})
                    return
            except (OSError, ValueError, TypeError) as exc:
                self._send_json(503, {"error": "系统文档读取失败：%s" % type(exc).__name__})
                return
            self._send_json(200, payload)

        def _send_cached_json(self, payload, include_body=True):
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            etag = '"%s"' % hashlib.sha256(raw).hexdigest()
            cache_control = "private, max-age=0, must-revalidate"
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                return
            use_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
            body = gzip.compress(raw, compresslevel=6) if use_gzip else raw
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _serve_download(self, path, filename, content_type, include_body=True):
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                self._send_json(404, {"error": "下载文件不存在"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(filename))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _otc_query(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"page", "page_size"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("仅接受固定分页参数")
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["20"])[0])
            if page < 1 or page_size < 20 or page_size > 200:
                raise ValueError("分页参数超出允许范围")
            return page, page_size

        def _otc_product_query(self):
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            allowed = {"page", "page_size", "q", "structure", "index_code", "status", "lifecycle",
                       "strategy_name", "start_from", "start_to", "end_from", "end_to",
                       "return_from", "return_to", "return_state", "sort_by", "sort_dir"}
            if set(query) - allowed or any(len(values) != 1 for values in query.values()):
                raise ValueError("仅接受固定产品筛选参数")
            page = int(query.pop("page", ["1"])[0]); page_size = int(query.pop("page_size", ["50"])[0])
            filters = {key: values[0] for key, values in query.items() if values[0]}
            return page, page_size, filters

        def _serve_otc_get(self, route, include_body=True):
            if not self._authenticate():
                return
            try:
                if route == "/api/modules/otc-derivatives":
                    self._send_cached_json(otc_store.module_payload(), include_body)
                    return
                if route == "/api/otc/backtests":
                    page, page_size = self._otc_query()
                    self._send_cached_json(otc_store.list_runs(page, page_size), include_body)
                    return
                if route == "/api/otc/pricing":
                    self._send_cached_json(otc_store.pricing_payload(), include_body)
                    return
                if route == "/api/otc/pricing/runs":
                    page, page_size = self._otc_query()
                    self._send_cached_json(otc_store.list_pricing_runs(page, page_size), include_body)
                    return
                pricing_match = re.match(
                    r"^/api/otc/pricing/runs/([0-9a-f-]{36})(?:/(download\.json))?$", route)
                if pricing_match:
                    run_id, action = pricing_match.groups()
                    if not otc_store.valid_run_id(run_id):
                        self.send_error(404); return
                    if action == "download.json":
                        path = otc_store.pricing_download_path(run_id)
                        run = otc_store.get_pricing_run(run_id)
                        name = str((run.get("request") or {}).get("product_name") or "经典雪球理论定价")
                        safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", name).strip("._-")[:80]
                        self._serve_download(path, "%s_%s.json" % (safe_name or "经典雪球理论定价", run_id[:8]),
                                             "application/json; charset=utf-8", include_body)
                    else:
                        self._send_cached_json(otc_store.pricing_result(run_id), include_body)
                    return
                if route == "/api/otc/products":
                    page, page_size, filters = self._otc_product_query()
                    self._send_cached_json(otc_store.list_products(page, page_size, filters), include_body)
                    return
                if route == "/api/otc/backtest-templates":
                    self._send_cached_json(otc_store.list_templates(), include_body)
                    return
                attachment_list_match = re.match(
                    r"^/api/otc/products/([0-9a-f-]{36})/attachments$", route)
                if attachment_list_match:
                    product_id = attachment_list_match.group(1)
                    self._send_cached_json(
                        {"attachments": otc_store.list_attachments(product_id)}, include_body)
                    return
                attachment_match = re.match(
                    r"^/api/otc/products/([0-9a-f-]{36})/attachments/([0-9a-f-]{36})(?:/(download))?$",
                    route)
                if attachment_match:
                    product_id, attachment_id, action = attachment_match.groups()
                    if action == "download":
                        path, item = otc_store.attachment_file_path(product_id, attachment_id)
                        self._serve_download(path, item["original_name"],
                                             "application/octet-stream", include_body)
                    else:
                        item = otc_store.get_attachment(product_id, attachment_id, True)
                        item["text_content"] = str(item.get("text_content") or "")[:200000]
                        self._send_cached_json({"attachment": item}, include_body)
                    return
                template_match = re.match(r"^/api/otc/backtest-templates/([0-9a-f-]{36})$", route)
                if template_match:
                    self._send_cached_json({"template": otc_store.get_template(template_match.group(1))}, include_body)
                    return
                product_match = re.match(r"^/api/otc/products/([0-9a-f-]{36})$", route)
                if product_match:
                    self._send_cached_json({"product": otc_store.get_product(product_match.group(1))}, include_body)
                    return
                match = re.match(r"^/api/otc/backtests/([0-9a-f-]{36})(?:/(samples|download\.xlsx|download\.json|download\.pdf))?$", route)
                if not match or not otc_store.valid_run_id(match.group(1)):
                    self.send_error(404)
                    return
                run_id, action = match.groups()
                if action == "samples":
                    page, page_size = self._otc_query()
                    self._send_cached_json(otc_store.samples(run_id, page, page_size), include_body)
                elif action in ("download.xlsx", "download.json", "download.pdf"):
                    kind = "xlsx" if action.endswith("xlsx") else "pdf" if action.endswith("pdf") else "json"
                    path = otc_store.download_path(run_id, kind)
                    run = otc_store.get_run(run_id)
                    product_name = str((run.get("request") or {}).get("product_name") or "期权回测")
                    safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", product_name).strip("._-")[:80]
                    content_type = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                    if kind == "xlsx" else "application/pdf" if kind == "pdf" else "application/json; charset=utf-8")
                    self._serve_download(path, "%s_回测报告_%s.%s" % (safe_name or "期权回测", run_id[:8], kind), content_type, include_body)
                elif action is None:
                    self._send_cached_json(otc_store.result(run_id), include_body)
                else:
                    self.send_error(404)
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except (OSError, sqlite3.Error) as exc:
                self._send_json(503, {"error": "场外衍生品数据暂不可用：%s" % type(exc).__name__})

        def _submit_otc_backtest(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json(64 * 1024)
                if request.get("product_id"):
                    allowed = {"product_id", "product_revision", "start_date", "end_date"}
                    if set(request) - allowed:
                        raise ValueError("按产品回测只接受产品ID、修订号和起止日")
                    request = otc_store.product_backtest_request(
                        request.get("product_id"), request.get("product_revision"), request)
                run = otc_store.submit(request)
                self._send_json(202, {"run": run,
                                      "poll": "/api/otc/backtests/%s" % run["id"]})
            except TaskConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _submit_otc_pricing(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json(64 * 1024)
                run = otc_store.submit_pricing(request, user)
                self._send_json(202, {"run": run,
                                      "poll": "/api/otc/pricing/runs/%s" % run["id"]})
            except TaskConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except RevisionConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _mutate_otc_product(self, action, product_id=None):
            if not self._authenticate():
                return
            try:
                request = self._read_json(64 * 1024)
                if action == "create":
                    product = otc_store.create_product(request.get("values") or {}, user)
                    status = 201
                elif action == "update":
                    product = otc_store.update_product(product_id, request.get("values") or {},
                                                       request.get("expected_revision"), user)
                    status = 200
                elif action == "event":
                    product = otc_store.add_event(product_id, request, user); status = 201
                else:
                    product = otc_store.void_product(product_id, request.get("expected_revision"), user)
                    status = 200
                self._send_json(status, {"product": product})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RevisionConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})

        def _upload_otc_attachment(self, product_id):
            if not self._authenticate():
                return
            temporary = None
            try:
                request = self._read_json(70 * 1024 * 1024)
                if not isinstance(request, dict) or set(request) != {
                        "filename", "content_base64", "category"}:
                    raise ValueError("附件上传只接受文件名、内容和固定分类")
                filename = str(request.get("filename") or "")
                content = base64.b64decode(request.get("content_base64") or "", validate=True)
                if not content:
                    raise ValueError("上传文件为空")
                from .knowledge import MAX_FILE_SIZE
                if len(content) > MAX_FILE_SIZE:
                    raise ValueError("文件超过50MB限制")
                with tempfile.NamedTemporaryFile(
                        dir=otc_store.root, delete=False,
                        suffix=os.path.splitext(filename)[1]) as handle:
                    temporary = handle.name
                    handle.write(content)
                item, duplicate = otc_store.add_attachment(
                    product_id, temporary, filename, request.get("category"), user)
                self._send_json(200 if duplicate else 201,
                                {"attachment": item, "duplicate": duplicate})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})
            finally:
                if temporary and os.path.exists(temporary):
                    os.remove(temporary)

        def _delete_otc_attachment(self, product_id, attachment_id):
            if not self._authenticate():
                return
            try:
                request = self._read_json(4096)
                if not isinstance(request, dict) or set(request) != {"expected_revision"}:
                    raise ValueError("附件停用只接受修订号")
                item = otc_store.deactivate_attachment(
                    product_id, attachment_id, request.get("expected_revision"), user)
                self._send_json(200, {"attachment": item})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RevisionConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})

        def _aggregate_otc_products(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json(128 * 1024)
                if not isinstance(request, dict) or set(request) != {"product_ids"}:
                    raise ValueError("勾选合计只接受产品ID数组")
                self._send_json(200, {"aggregates": otc_store.aggregate_products(
                    request.get("product_ids"))})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})

        def _mutate_otc_template(self, action, template_id=None):
            if not self._authenticate():
                return
            try:
                request = self._read_json(64 * 1024)
                if action == "create":
                    template = otc_store.create_template(request.get("name"), request.get("request") or {}, user); status = 201
                elif action == "update":
                    template = otc_store.update_template(template_id, request.get("name"), request.get("request") or {},
                                                         request.get("expected_revision"), user); status = 200
                else:
                    template = otc_store.void_template(template_id, request.get("expected_revision"), user); status = 200
                self._send_json(status, {"template": template})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RevisionConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_versioned_asset(self, route, include_body=True):
            match = re.match(r"^/assets/([0-9a-f]{16})/(styles\.css|bootstrap\.js|core\.js|dashboard\.js|otc_derivatives\.js)$", route)
            if not match:
                return False
            version, filename = match.groups()
            content_type = ("text/css; charset=utf-8" if filename.endswith(".css")
                            else "application/javascript; charset=utf-8")
            self._serve_asset(os.path.join(os.path.dirname(index_path), ".runtime", "assets",
                                           version, filename),
                              content_type, "private, max-age=31536000, immutable", include_body)
            return True

        def _serve_bottom_return(self, product_id, include_body=True):
            if not self._authenticate():
                return
            if not product_id or not re.match(r"^[A-Za-z0-9_-]+$", product_id):
                self.send_error(404)
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"start", "end", "benchmark"} or any(
                    len(values) != 1 for values in query.values()):
                self._send_json(400, {"error": "底层收益只接受固定的起止日和基准参数"})
                return
            start = query.get("start", [""])[0]
            end = query.get("end", [""])[0]
            benchmark = query.get("benchmark", ["000852"])[0]
            root = os.path.dirname(index_path)
            try:
                with open(os.path.join(root, ".runtime", "modules", "bottom-returns.json"),
                          "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                with open(os.path.join(root, ".runtime", "page-data.json"),
                          "r", encoding="utf-8") as handle:
                    page = json.load(handle)
                index = (page.get("benchmarks", {}).get("indices", {}).get(benchmark) or {})
                result = analyze_bottom_return(
                    os.path.join(root, "market_data", "bottom_returns.sqlite3"),
                    manifest, product_id, start, end, benchmark, index.get("points") or [])
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error) as exc:
                self._send_json(503, {"error": "底层收益缓存暂不可用：%s" % exc})
                return
            self._send_cached_json(result, include_body)

        def _serve_v2(self, route, include_body=True):
            if not self._authenticate():
                return
            root = os.path.dirname(index_path)
            try:
                page = api_v2.read_json(os.path.join(root, ".runtime", "page-data.json"))
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if route == "/api/v2/bootstrap":
                    api_v2.query_one(query, ())
                    modules = {}
                    for name, filename in module_files.items():
                        path = os.path.join(root, ".runtime", "modules", filename)
                        modules[name] = {"status": "current" if os.path.exists(path) else "missing",
                                         "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
                    payload = api_v2.bootstrap(page, modules)
                elif route == "/api/v2/overview":
                    values = api_v2.query_one(query, ("start", "end", "scope"))
                    start = api_v2.iso_date(values.get("start"), "start")
                    end = api_v2.iso_date(values.get("end"), "end")
                    if start >= end:
                        raise ValueError("start must precede end")
                    payload = api_v2.overview(page, start, end, values.get("scope", "all"))
                elif route.startswith("/api/v2/top-returns/"):
                    product_id = route[len("/api/v2/top-returns/"):]
                    if not re.match(r"^top-[0-9]{3}$", product_id):
                        raise KeyError("product is not registered")
                    values = api_v2.query_one(query, ("start", "end", "benchmark"))
                    start = api_v2.iso_date(values.get("start"), "start")
                    end = api_v2.iso_date(values.get("end"), "end")
                    if start >= end:
                        raise ValueError("start must precede end")
                    payload = api_v2.top_returns(page, product_id, start, end,
                                                 values.get("benchmark", "000852"))
                elif route == "/api/v2/strategy":
                    values = api_v2.query_one(query, ("start", "end", "page", "page_size") +
                                               api_v2.FILTER_FIELDS)
                    start = api_v2.iso_date(values.get("start"), "start")
                    end = api_v2.iso_date(values.get("end"), "end")
                    if start >= end:
                        raise ValueError("start must precede end")
                    page_number = int(values.get("page", "1"))
                    page_size = int(values.get("page_size", "50"))
                    if page_number < 1 or page_size < 20 or page_size > 200:
                        raise ValueError("pagination is outside the allowed range")
                    filters = {field: values.get(field, "") for field in api_v2.FILTER_FIELDS}
                    holding_names = [holding.get("name") for product in page.get("products", [])
                                     for point in product.get("points", [])
                                     for holding in point.get("holdings", []) if holding.get("name")]
                    page["labels"] = build_label_payload(holding_names, self._labels_path())
                    allowed = page.get("labels", {}).get("deduped_records", [])
                    for field, value in filters.items():
                        if value and value not in {str(item.get(field) or ("无" if field == "department" else "其他"))
                                                   for item in allowed}:
                            raise ValueError("label filter is not registered")
                    payload = api_v2.strategy(page, start, end, filters, page_number, page_size)
                elif route in ("/api/v2/factors",) or route.startswith("/api/v2/factors/"):
                    api_v2.query_one(query, ())
                    factors = api_v2.read_json(os.path.join(root, ".runtime", "modules", "factors.json"))
                    if route == "/api/v2/factors":
                        payload = api_v2.factor_list(page, factors)
                    else:
                        product_id = route[len("/api/v2/factors/"):]
                        if not re.match(r"^top-[0-9]{3}$", product_id):
                            raise KeyError("product is not registered")
                        payload = api_v2.factor_detail(page, factors, product_id)
                elif route.startswith("/api/v2/market/"):
                    api_v2.query_one(query, ())
                    module = route[len("/api/v2/market/"):]
                    if not re.match(r"^[a-z]+$", module):
                        raise KeyError("market module is not registered")
                    market = api_v2.read_json(os.path.join(
                        root, ".runtime", "modules", "market-research.json"))
                    payload = api_v2.market_module(page, market, module)
                else:
                    self.send_error(404)
                    return
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError as exc:
                self._send_json(503, {"error": "v2 data is unavailable: %s" % type(exc).__name__})
                return
            self._send_cached_json(payload, include_body)

        def _read_json(self, max_length=1024 * 1024):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("无效Content-Length")
            if length <= 0 or length > max_length:
                raise ValueError("请求正文大小无效")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                raise ValueError("请求JSON无效")

        def _labels_path(self):
            return os.path.join(os.path.dirname(index_path), "data_sources", "product_labels.json")

        def _serve_strategy_lab_regenerate(self):
            if not self._authenticate():
                return
            task_id, created = strategy_tasks.submit()
            self._send_json(202, {"task_id": task_id, "status": "running",
                                  "created": created,
                                  "poll": "/api/strategy-lab/task/%s" % task_id})

        def _submit_llm_task(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                if not isinstance(request, dict) or set(request) - {"methodology", "objective"}:
                    raise ValueError("策略生成只接受固定方法论和目标")
                methodology = str(request.get("methodology") or "")
                objective = str(request.get("objective") or "")[:500]
                if methodology not in _LLM_METHODOLOGIES:
                    raise ValueError("方法论不在白名单")
                task_id = llm_tasks.submit_strategy(methodology, objective, user)
                self._send_json(202, {"task_id": task_id, "status": "running",
                                      "poll": "/api/strategy-lab/task/%s" % task_id})
            except LLMDisabledError as exc:
                self._send_json(503, {"error": str(exc), "enabled": False})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _submit_unified_llm_task(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json(64 * 1024)
                if not isinstance(request, dict):
                    raise ValueError("大模型任务请求无效")
                task_type = request.get("task_type")
                if task_type == "strategy_spec":
                    if set(request) != {"task_type", "methodology", "objective"}:
                        raise ValueError("策略生成参数不在固定清单")
                    methodology = str(request.get("methodology") or "")
                    if methodology not in _LLM_METHODOLOGIES:
                        raise ValueError("方法论不在白名单")
                    task_id = llm_tasks.submit_strategy(
                        methodology, str(request.get("objective") or "")[:500], user)
                elif task_type == "otc_field_audit":
                    if set(request) != {"task_type", "product_id", "expected_revision"}:
                        raise ValueError("字段排查参数不在固定清单")
                    product_id = str(request.get("product_id") or "")
                    if not otc_store.valid_run_id(product_id):
                        raise ValueError("产品ID无效")
                    task_id = llm_tasks.submit_field_audit(
                        product_id, request.get("expected_revision"), user)
                else:
                    raise ValueError("大模型任务类型不在白名单")
                self._send_json(202, {"task_id": task_id, "status": "running",
                                      "poll": "/api/llm/tasks/%s" % task_id})
            except LLMDisabledError as exc:
                self._send_json(503, {"error": str(exc), "enabled": False})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RevisionConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, sqlite3.Error) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_llm_task(self, task_id):
            if not self._authenticate():
                return
            if not task_id or "/" in task_id:
                self._send_json(400, {"error": "无效任务ID"})
                return
            task = (strategy_tasks.get(task_id) if task_id.startswith("rebuild-")
                    else llm_tasks.get(task_id))
            if task is None:
                self._send_json(404, {"error": "任务不存在或已过期"})
                return
            self._send_json(200, task)

        def _serve_llm_status(self, include_body=True):
            if not self._authenticate():
                return
            self._send_cached_json(llm_tasks.status(), include_body)

        def _serve_labels(self):
            if not self._authenticate():
                return
            try:
                with open(self._labels_path(), "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                for record in payload.get("records", []):
                    record["department"] = str(record.get("department") or "").strip() or "无"
                page_path = os.path.join(os.path.dirname(index_path), ".runtime", "page-data.json")
                with open(page_path, "r", encoding="utf-8") as handle:
                    page = json.load(handle)
                holding_names = [holding.get("name") for product in page.get("products", [])
                                 for point in product.get("points", [])
                                 for holding in point.get("holdings", []) if holding.get("name")]
                label_runtime = build_label_payload(holding_names, self._labels_path())
                payload["matches"] = label_runtime.get("matches", {})
                payload["deduped_records"] = label_runtime.get("deduped_records", [])
                payload["dedupe_check"] = label_runtime.get("dedupe_check", {})
            except OSError:
                self._send_json(503, {"error": "标签JSON不存在，请先执行labels-migrate"})
                return
            self._send_json(200, payload)

        def _mutate_labels(self, action, record_id=None):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                with label_lock:
                    payload, item = mutate_catalog(
                        self._labels_path(), action, request.get("values", {}),
                        request.get("expected_revision"), user, record_id,
                        os.path.join(os.path.dirname(index_path), "logs", "label-audit.jsonl"))
                self._send_json(200 if action != "create" else 201,
                                {"revision": payload["revision"], "updated_at": payload["updated_at"],
                                 "record": item})
            except LabelConflictError as exc:
                self._send_json(409, {"error": str(exc)})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_core_report_export(self):
            if not self._authenticate():
                return
            try:
                from .core_report_export import export_core_report
                body = export_core_report(self._read_json())
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": "核心汇报导出失败（%s）" % type(exc).__name__})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''core-report.xlsx")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _serve_attribution_export(self):
            if not self._authenticate():
                return
            try:
                from .attribution_export import export_attribution
                body = export_attribution(self._read_json(30 * 1024 * 1024))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": "归因导出失败（%s）" % type(exc).__name__})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''holding-attribution.xlsx")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _knowledge_store(self):
            from .knowledge import KnowledgeStore
            return KnowledgeStore(os.path.join(os.path.dirname(index_path), "knowledge_base"))

        def _serve_knowledge_sources(self):
            if self._authenticate():
                self._send_json(200, knowledge_sources.sources())

        def _submit_knowledge_scan(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                if request and request.get("source_id") not in (None, "wechat"):
                    raise ValueError("来源ID无效")
                task_id = knowledge_sources.create_scan()
                worker = threading.Thread(target=knowledge_sources.scan, args=(task_id,))
                worker.daemon = True
                worker.start()
                self._send_json(202, {"task_id": task_id, "status": "运行中"})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_scan_task(self, task_id):
            if not self._authenticate():
                return
            try:
                self._send_json(200, knowledge_sources.scan_task(task_id))
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_candidates(self):
            if not self._authenticate():
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                value = knowledge_sources.candidates(
                    query.get("status", [""])[0], query.get("page", [1])[0],
                    query.get("page_size", [50])[0], query.get("product_id", [""])[0])
                self._send_json(200, value)
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _mutate_knowledge_candidate(self, candidate_id):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                item = knowledge_sources.update_candidate(candidate_id, request.get("expected_revision"),
                                                          request.get("action"), request.get("product_id", ""))
                self._send_json(200, {"candidate": item})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _import_knowledge_candidates(self):
            if not self._authenticate():
                return
            try:
                self._send_json(200, knowledge_sources.import_candidates(
                    self._read_json().get("candidate_ids") or []))
            except (ValueError, RuntimeError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_graph(self):
            # The graph response includes evidence-rich entity types and a suggested
            # manager/product center; keep this endpoint versioned with web.py health.
            if not self._authenticate():
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                self._send_json(200, knowledge_sources.graph(
                    query.get("type", [""])[0], query.get("q", [""])[0], query.get("limit", [500])[0],
                    query.get("center_id", [""])[0]))
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_facts(self):
            if not self._authenticate():
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                self._send_json(200, knowledge_diligence.facts(
                    query.get("status", [""])[0], query.get("type", [""])[0],
                    query.get("document_id", [""])[0], query.get("q", [""])[0],
                    query.get("conflict", [""])[0], query.get("page", [1])[0],
                    query.get("page_size", [50])[0]))
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _mutate_knowledge_fact(self, fact_id):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                value = knowledge_diligence.update_fact(
                    fact_id, request.get("expected_revision"), request.get("action"),
                    request.get("values") or {}, user)
                self._send_json(200, {"fact": value})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _batch_knowledge_facts(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                self._send_json(200, knowledge_diligence.batch_update(
                    request.get("items") or [], request.get("action"), user))
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_dossier(self, entity_id):
            if not self._authenticate():
                return
            try:
                self._send_json(200, knowledge_diligence.dossier(entity_id))
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})

        def _create_knowledge_obsidian_export(self):
            if not self._authenticate():
                return
            try:
                self._send_json(201, knowledge_sources.export_obsidian())
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _download_knowledge_export(self, export_id):
            if not self._authenticate():
                return
            try:
                path, _item = knowledge_sources.export_path(export_id)
                with open(path, "rb") as handle:
                    body = handle.read()
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
                return
            except (ValueError, RuntimeError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=knowledge-vault.zip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_knowledge_list(self):
            if not self._authenticate():
                return
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            inactive = params.get("inactive", ["0"])[0] == "1"
            try:
                items = self._knowledge_store().list(query, inactive, 500)
                contexts = knowledge_sources.document_context([item["id"] for item in items])
                for item in items:
                    item["source_context"] = contexts.get(str(item["id"]), {})
                product = params.get("product", [""])[0][:200]
                manager = params.get("manager", [""])[0][:200]
                source = params.get("source", [""])[0][:100]
                review = params.get("review_status", [""])[0][:30]
                fof_id = params.get("fof_id", [""])[0][:100]
                if product:
                    items = [item for item in items if item.get("product") == product]
                if manager:
                    items = [item for item in items if item.get("organization") == manager]
                if source:
                    items = [item for item in items if item.get("source") == source]
                if review:
                    items = [item for item in items if item["source_context"].get("review_status") == review]
                if fof_id:
                    items = [item for item in items if any(fof.get("id") == fof_id for fof in item["source_context"].get("associated_fofs", []))]
                self._send_json(200, {"documents": items, "query": query,
                                      "filters": {"product": product, "manager": manager, "source": source,
                                                  "review_status": review, "fof_id": fof_id}})
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _serve_knowledge_detail(self, document_id):
            if not self._authenticate():
                return
            try:
                item = self._knowledge_store().get(document_id, True)
                item.pop("stored_name", None)
                item["text_content"] = item.get("text_content", "")[:200000]
                self._send_json(200, item)
            except (KeyError, ValueError) as exc:
                self._send_json(404, {"error": str(exc)})

        def _serve_knowledge_download(self, document_id):
            if not self._authenticate():
                return
            try:
                path, item = self._knowledge_store().file_path(document_id)
                with open(path, "rb") as handle:
                    body = handle.read()
            except (KeyError, ValueError, OSError) as exc:
                self._send_json(404, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(item["original_name"]))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _upload_knowledge(self):
            if not self._authenticate():
                return
            temporary = None
            try:
                request = self._read_json(70 * 1024 * 1024)
                filename = str(request.get("filename") or "")
                content = base64.b64decode(request.get("content_base64") or "", validate=True)
                if not content:
                    raise ValueError("上传文件为空")
                root = os.path.join(os.path.dirname(index_path), "knowledge_base")
                from .knowledge import KnowledgeStore, MAX_FILE_SIZE
                if len(content) > MAX_FILE_SIZE:
                    raise ValueError("文件超过50MB限制")
                store = KnowledgeStore(root)
                with tempfile.NamedTemporaryFile(dir=store.root, delete=False,
                                                 suffix=os.path.splitext(filename)[1]) as handle:
                    temporary = handle.name
                    handle.write(content)
                with knowledge_lock:
                    item, duplicate = store.add_file(temporary, dict(request.get("metadata") or {},
                                                                    title=(request.get("metadata") or {}).get("title") or os.path.splitext(os.path.basename(filename))[0]), filename)
                self._send_json(200 if duplicate else 201, {"document": item, "duplicate": duplicate})
            except (ValueError, TypeError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
            finally:
                if temporary and os.path.exists(temporary):
                    os.remove(temporary)

        def _import_knowledge_inbox(self):
            if not self._authenticate():
                return
            with knowledge_lock:
                result = self._knowledge_store().import_inbox()
            self._send_json(200, result)

        def _mutate_knowledge(self, document_id, deactivate=False):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
                with knowledge_lock:
                    store = self._knowledge_store()
                    item = store.deactivate(document_id, request.get("expected_revision")) if deactivate else store.update(document_id, request.get("values") or {}, request.get("expected_revision"))
                self._send_json(200, {"document": item})
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError, OSError) as exc:
                self._send_json(409 if "其他页面" in str(exc) else 400, {"error": str(exc)})

        def do_GET(self):
            route = urlsplit(self.path).path
            root = os.path.dirname(index_path)
            if route == "/api/health":
                if self._authenticate():
                    self._send_json(200, {"status": "ok", "server_code_sha256": server_code_sha256})
            elif route == "/api/llm/status":
                self._serve_llm_status()
            elif route.startswith("/api/llm/tasks/"):
                self._serve_llm_task(route[len("/api/llm/tasks/"):])
            elif route == "/api/modules/otc-derivatives" or route in ("/api/otc/backtests", "/api/otc/products", "/api/otc/backtest-templates", "/api/otc/pricing", "/api/otc/pricing/runs") or route.startswith("/api/otc/backtests/") or route.startswith("/api/otc/products/") or route.startswith("/api/otc/backtest-templates/") or route.startswith("/api/otc/pricing/runs/"):
                self._serve_otc_get(route)
            elif route == "/api/v2/bootstrap" or route == "/api/v2/overview" or route == "/api/v2/strategy" or route == "/api/v2/factors" or route.startswith("/api/v2/top-returns/") or route.startswith("/api/v2/factors/") or route.startswith("/api/v2/market/"):
                self._serve_v2(route)
            elif route == "/api/monthly-report":
                self._serve_monthly_report()
            elif route == "/api/valuation-archive":
                self._serve_valuation_archive()
            elif route.startswith("/api/strategy-lab/task/"):
                self._serve_llm_task(route[len("/api/strategy-lab/task/"):])
            elif route == "/api/labels":
                self._serve_labels()
            elif route == "/api/knowledge":
                self._serve_knowledge_list()
            elif route == "/api/knowledge/sources":
                self._serve_knowledge_sources()
            elif route.startswith("/api/knowledge/scan-tasks/"):
                self._serve_knowledge_scan_task(route.rsplit("/", 1)[-1])
            elif route == "/api/knowledge/candidates":
                self._serve_knowledge_candidates()
            elif route == "/api/knowledge/facts":
                self._serve_knowledge_facts()
            elif route == "/api/knowledge/graph":
                self._serve_knowledge_graph()
            elif route.startswith("/api/knowledge/dossiers/"):
                self._serve_knowledge_dossier(route.rsplit("/", 1)[-1])
            elif route.startswith("/api/knowledge/exports/") and route.endswith("/download"):
                self._download_knowledge_export(route.split("/")[-2])
            elif route.startswith("/api/knowledge/"):
                parts = route.strip("/").split("/")
                if len(parts) == 3 and parts[2].isdigit():
                    self._serve_knowledge_detail(parts[2])
                elif len(parts) == 4 and parts[2].isdigit() and parts[3] == "download":
                    self._serve_knowledge_download(parts[2])
                else:
                    self.send_error(404)
            elif route == "/api/page-data":
                self._serve_asset(os.path.join(root, ".runtime", "page-data.json"),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate")
            elif route.startswith("/api/bottom-returns/"):
                product_id = route[len("/api/bottom-returns/"):]
                if "/" in product_id:
                    self.send_error(404)
                else:
                    self._serve_bottom_return(product_id)
            elif route.startswith("/api/modules/") and route[len("/api/modules/"):] in module_files:
                filename = module_files[route[len("/api/modules/"):]]
                self._serve_asset(os.path.join(root, ".runtime", "modules", filename),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate")
            elif self._serve_versioned_asset(route):
                return
            elif route == "/assets/app.js":
                self._serve_asset(os.path.join(root, ".runtime", "app.js"),
                                  "application/javascript; charset=utf-8",
                                  "private, max-age=0, must-revalidate")
            else:
                self._serve(True)

        def do_POST(self):
            route = urlsplit(self.path).path
            event_match = re.match(r"^/api/otc/products/([0-9a-f-]{36})/events$", route)
            attachment_match = re.match(
                r"^/api/otc/products/([0-9a-f-]{36})/attachments$", route)
            if route == "/api/system-docs/access":
                self._serve_system_docs()
            elif route == "/api/llm/tasks":
                self._submit_unified_llm_task()
            elif route == "/api/otc/backtests":
                self._submit_otc_backtest()
            elif route == "/api/otc/pricing/runs":
                self._submit_otc_pricing()
            elif route == "/api/otc/products":
                self._mutate_otc_product("create")
            elif route == "/api/otc/products/aggregate":
                self._aggregate_otc_products()
            elif route == "/api/otc/backtest-templates":
                self._mutate_otc_template("create")
            elif attachment_match:
                self._upload_otc_attachment(attachment_match.group(1))
            elif event_match:
                self._mutate_otc_product("event", event_match.group(1))
            elif urlsplit(self.path).path == "/api/labels":
                self._mutate_labels("create")
            elif urlsplit(self.path).path == "/api/strategy-lab/llm":
                self._submit_llm_task()
            elif urlsplit(self.path).path == "/api/strategy-lab/regenerate":
                self._serve_strategy_lab_regenerate()
            elif urlsplit(self.path).path == "/api/core-report.xlsx":
                self._serve_core_report_export()
            elif urlsplit(self.path).path == "/api/attribution.xlsx":
                self._serve_attribution_export()
            elif urlsplit(self.path).path == "/api/knowledge/upload":
                self._upload_knowledge()
            elif urlsplit(self.path).path == "/api/knowledge/import-inbox":
                self._import_knowledge_inbox()
            elif route == "/api/knowledge/sources/wechat/scan":
                self._submit_knowledge_scan()
            elif route == "/api/knowledge/candidates/import":
                self._import_knowledge_candidates()
            elif route == "/api/knowledge/facts/batch":
                self._batch_knowledge_facts()
            elif route == "/api/knowledge/obsidian-export":
                # The store emits a complete, collision-safe, status-partitioned Markdown Vault.
                self._create_knowledge_obsidian_export()
            else:
                self.send_error(404)

        def do_PATCH(self):
            route = urlsplit(self.path).path
            otc_match = re.match(r"^/api/otc/products/([0-9a-f-]{36})$", route)
            template_match = re.match(r"^/api/otc/backtest-templates/([0-9a-f-]{36})$", route)
            if otc_match:
                self._mutate_otc_product("update", otc_match.group(1))
                return
            elif template_match:
                self._mutate_otc_template("update", template_match.group(1))
                return
            candidate_match = re.match(r"^/api/knowledge/candidates/([0-9a-f-]{36})$", route)
            if candidate_match:
                self._mutate_knowledge_candidate(candidate_match.group(1))
                return
            fact_match = re.match(r"^/api/knowledge/facts/([0-9a-f-]{36})$", route)
            if fact_match:
                self._mutate_knowledge_fact(fact_match.group(1))
                return
            knowledge_prefix = "/api/knowledge/"
            knowledge_id = route[len(knowledge_prefix):] if route.startswith(knowledge_prefix) else ""
            if knowledge_id.isdigit():
                self._mutate_knowledge(knowledge_id)
                return
            prefix = "/api/labels/"
            record_id = route[len(prefix):] if route.startswith(prefix) else ""
            if record_id and "/" not in record_id:
                self._mutate_labels("update", record_id)
            else:
                self.send_error(404)

        def do_DELETE(self):
            route = urlsplit(self.path).path
            otc_match = re.match(r"^/api/otc/products/([0-9a-f-]{36})$", route)
            attachment_match = re.match(
                r"^/api/otc/products/([0-9a-f-]{36})/attachments/([0-9a-f-]{36})$", route)
            template_match = re.match(r"^/api/otc/backtest-templates/([0-9a-f-]{36})$", route)
            if attachment_match:
                self._delete_otc_attachment(attachment_match.group(1), attachment_match.group(2))
                return
            elif otc_match:
                self._mutate_otc_product("void", otc_match.group(1))
                return
            elif template_match:
                self._mutate_otc_template("void", template_match.group(1))
                return
            knowledge_prefix = "/api/knowledge/"
            knowledge_id = route[len(knowledge_prefix):] if route.startswith(knowledge_prefix) else ""
            if knowledge_id.isdigit():
                self._mutate_knowledge(knowledge_id, True)
                return
            prefix = "/api/labels/"
            record_id = route[len(prefix):] if route.startswith(prefix) else ""
            if record_id and "/" not in record_id:
                self._mutate_labels("deactivate", record_id)
            else:
                self.send_error(404)

        def do_HEAD(self):
            route = urlsplit(self.path).path
            root = os.path.dirname(index_path)
            if route == "/api/health":
                if self._authenticate():
                    self._send_json(200, {"status": "ok", "server_code_sha256": server_code_sha256}, False)
            elif route == "/api/llm/status":
                self._serve_llm_status(False)
            elif route.startswith("/api/llm/tasks/"):
                self._serve_llm_task(route[len("/api/llm/tasks/"):])
            elif route == "/api/modules/otc-derivatives" or route in ("/api/otc/backtests", "/api/otc/products", "/api/otc/backtest-templates", "/api/otc/pricing", "/api/otc/pricing/runs") or route.startswith("/api/otc/backtests/") or route.startswith("/api/otc/products/") or route.startswith("/api/otc/backtest-templates/") or route.startswith("/api/otc/pricing/runs/"):
                self._serve_otc_get(route, False)
            elif route == "/api/v2/bootstrap" or route == "/api/v2/overview" or route == "/api/v2/strategy" or route == "/api/v2/factors" or route.startswith("/api/v2/top-returns/") or route.startswith("/api/v2/factors/") or route.startswith("/api/v2/market/"):
                self._serve_v2(route, False)
            elif route == "/api/page-data":
                self._serve_asset(os.path.join(root, ".runtime", "page-data.json"),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            elif route.startswith("/api/bottom-returns/"):
                product_id = route[len("/api/bottom-returns/"):]
                if "/" in product_id:
                    self.send_error(404)
                else:
                    self._serve_bottom_return(product_id, False)
            elif route.startswith("/api/modules/") and route[len("/api/modules/"):] in module_files:
                filename = module_files[route[len("/api/modules/"):]]
                self._serve_asset(os.path.join(root, ".runtime", "modules", filename),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            elif self._serve_versioned_asset(route, False):
                return
            elif route == "/assets/app.js":
                self._serve_asset(os.path.join(root, ".runtime", "app.js"),
                                  "application/javascript; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            else:
                self._serve(False)

        def log_message(self, fmt, *args):
            pass

    return Handler


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent two dashboard generations from sharing one Windows port."""

    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def create_server(index_path="index.html", host="127.0.0.1", port=8000, env_path=".env"):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("分享服务只允许绑定127.0.0.1")
    user, password = _credentials(env_path)
    system_docs_user, system_docs_password = _system_docs_credentials(env_path)
    return _ExclusiveThreadingHTTPServer(("127.0.0.1", port),
                                         make_handler(index_path, user, password,
                                                      system_docs_user=system_docs_user,
                                                      system_docs_password=system_docs_password))


def serve(index_path="index.html", host="127.0.0.1", port=8000, open_browser=True, env_path=".env",
          timing_callback=None):
    started = time.perf_counter()
    server = create_server(index_path, host, port, env_path)
    if timing_callback:
        timing_callback("finish", "网页服务启动", "服务运行",
                        time.perf_counter() - started, "success")
    url = "http://127.0.0.1:%s/#home" % server.server_address[1]
    print("受密码保护的收益看板已启动：" + url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
