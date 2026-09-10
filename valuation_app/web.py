import base64
import gzip
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .mail import load_env
from .labels import LabelConflictError, build_label_payload, mutate_catalog


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


class LLMTaskManager:
    """策略实验室 LLM 生成任务：内存存储 + 单工作线程。

    任务只保留最近 20 条，提交即后台执行，前端轮询状态。
    """

    def __init__(self, max_tasks=20):
        self.max_tasks = max_tasks
        self.tasks = {}
        self.order = []
        self.lock = threading.Lock()

    def submit(self, methodology, objective):
        task_id = "%d-%04d" % (int(time.time() * 1000), len(self.order) % 10000)
        task = {"id": task_id, "status": "running", "methodology": methodology,
                "objective": objective, "created_at": time.time(),
                "result": None, "error": None}
        with self.lock:
            self.tasks[task_id] = task
            self.order.append(task_id)
            while len(self.order) > self.max_tasks:
                self.tasks.pop(self.order.pop(0), None)
        thread = threading.Thread(target=self._run, args=(task_id, methodology, objective))
        thread.daemon = True
        thread.start()
        return task_id

    def _run(self, task_id, methodology, objective):
        try:
            from strategy_lab.pipeline import run_llm
            from strategy_lab.llm.provider import load_providers
            root = os.path.dirname(os.path.abspath(_llm_root()))
            providers, _note = load_providers()
            result = run_llm(methodology, objective, project_root=root, providers=providers)
            with self.lock:
                if task_id in self.tasks:
                    self.tasks[task_id]["result"] = {
                        "status": result.get("status"), "provider": result.get("provider"),
                        "spec": result.get("spec"), "errors": result.get("errors"),
                        "generated_at": result.get("generated_at")}
                    self.tasks[task_id]["status"] = result.get("status", "failed")
        except Exception as exc:
            with self.lock:
                if task_id in self.tasks:
                    self.tasks[task_id]["status"] = "failed"
                    self.tasks[task_id]["error"] = "%s: %s" % (type(exc).__name__, exc)

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None


def _llm_root():
    """strategy_lab 所在的项目根（web.py 位于 valuation_app/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_LLM_METHODOLOGIES = ("risk_parity_score", "momentum_tilt", "value_tilt", "defensive")


def make_handler(index_path, user, password, limiter=None):
    index_path = os.path.abspath(index_path)
    limiter = limiter or AuthLimiter()
    report_lock = threading.Lock()
    archive_lock = threading.Lock()
    asset_lock = threading.Lock()
    label_lock = threading.Lock()
    knowledge_lock = threading.Lock()
    llm_tasks = LLMTaskManager()
    asset_cache = {}
    module_files = {
        "factors": "factors.json",
        "market-research": "market-research.json",
        "strategy-lab": "strategy-lab.json",
        "underlying-assets": "underlying-assets.json",
        "ledger-tables": "ledger-tables.json",
        "label-workbooks": "label-workbooks.json",
        "portfolio-var": "portfolio-var.json",
    }
    expected = "Basic " + base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")

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
            self.wfile.write(body)

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
            try:
                from strategy_lab.pipeline import run as run_strategy_lab
                root = _llm_root()
                result = run_strategy_lab(project_root=root)
                self._send_json(200, {"status": result.get("status"),
                                      "generated_at": result.get("generated_at"),
                                      "latest_signal_date": result.get("latest_signal_date"),
                                      "recommendations": len(result.get("recommendations") or [])})
            except Exception as exc:
                self._send_json(500, {"error": "策略实验室重算失败（%s）" % type(exc).__name__})

        def _submit_llm_task(self):
            if not self._authenticate():
                return
            try:
                request = self._read_json()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            methodology = str(request.get("methodology") or "")
            objective = str(request.get("objective") or "")[:500]
            if methodology not in _LLM_METHODOLOGIES:
                self._send_json(400, {"error": "方法论不在白名单"})
                return
            task_id = llm_tasks.submit(methodology, objective)
            self._send_json(202, {"task_id": task_id, "status": "running",
                                  "poll": "/api/strategy-lab/task/%s" % task_id})

        def _serve_llm_task(self, task_id):
            if not self._authenticate():
                return
            if not task_id or "/" in task_id:
                self._send_json(400, {"error": "无效任务ID"})
                return
            task = llm_tasks.get(task_id)
            if task is None:
                self._send_json(404, {"error": "任务不存在或已过期"})
                return
            self._send_json(200, task)

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

        def _serve_knowledge_list(self):
            if not self._authenticate():
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query).get("q", [""])[0]
            inactive = parse_qs(parsed.query).get("inactive", ["0"])[0] == "1"
            try:
                items = self._knowledge_store().list(query, inactive)
                self._send_json(200, {"documents": items, "query": query})
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
            if route == "/api/monthly-report":
                self._serve_monthly_report()
            elif route == "/api/valuation-archive":
                self._serve_valuation_archive()
            elif route.startswith("/api/strategy-lab/task/"):
                self._serve_llm_task(route[len("/api/strategy-lab/task/"):])
            elif route == "/api/labels":
                self._serve_labels()
            elif route == "/api/knowledge":
                self._serve_knowledge_list()
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
            elif route.startswith("/api/modules/") and route[len("/api/modules/"):] in module_files:
                filename = module_files[route[len("/api/modules/"):]]
                self._serve_asset(os.path.join(root, ".runtime", "modules", filename),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate")
            elif route == "/assets/app.js":
                self._serve_asset(os.path.join(root, ".runtime", "app.js"),
                                  "application/javascript; charset=utf-8",
                                  "private, max-age=0, must-revalidate")
            else:
                self._serve(True)

        def do_POST(self):
            if urlsplit(self.path).path == "/api/labels":
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
            else:
                self.send_error(404)

        def do_PATCH(self):
            route = urlsplit(self.path).path
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
            if route == "/api/page-data":
                self._serve_asset(os.path.join(root, ".runtime", "page-data.json"),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            elif route.startswith("/api/modules/") and route[len("/api/modules/"):] in module_files:
                filename = module_files[route[len("/api/modules/"):]]
                self._serve_asset(os.path.join(root, ".runtime", "modules", filename),
                                  "application/json; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            elif route == "/assets/app.js":
                self._serve_asset(os.path.join(root, ".runtime", "app.js"),
                                  "application/javascript; charset=utf-8",
                                  "private, max-age=0, must-revalidate", False)
            else:
                self._serve(False)

        def log_message(self, fmt, *args):
            pass

    return Handler


def create_server(index_path="index.html", host="127.0.0.1", port=8000, env_path=".env"):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("分享服务只允许绑定127.0.0.1")
    user, password = _credentials(env_path)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(index_path, user, password))


def serve(index_path="index.html", host="127.0.0.1", port=8000, open_browser=True, env_path=".env",
          timing_callback=None):
    started = time.perf_counter()
    server = create_server(index_path, host, port, env_path)
    if timing_callback:
        timing_callback("finish", "网页服务启动", "服务运行",
                        time.perf_counter() - started, "success")
    url = "http://127.0.0.1:%s" % server.server_address[1]
    print("受密码保护的收益看板已启动：" + url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
