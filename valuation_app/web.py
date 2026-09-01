import base64
import gzip
import hashlib
import hmac
import os
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .mail import load_env


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


def make_handler(index_path, user, password, limiter=None):
    index_path = os.path.abspath(index_path)
    limiter = limiter or AuthLimiter()
    report_lock = threading.Lock()
    asset_lock = threading.Lock()
    asset_cache = {}
    module_files = {
        "factors": "factors.json",
        "market-research": "market-research.json",
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

        def do_GET(self):
            route = urlsplit(self.path).path
            root = os.path.dirname(index_path)
            if route == "/api/monthly-report":
                self._serve_monthly_report()
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
                                  "private, max-age=3600")
            else:
                self._serve(True)

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
                                  "private, max-age=3600", False)
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
