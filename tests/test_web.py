import base64
import gzip
import http.client
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from http.server import ThreadingHTTPServer

from valuation_app.web import AuthLimiter, _credentials, make_handler


class ShareServerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.index_path = os.path.join(self.tempdir.name, "index.html")
        with open(self.index_path, "w", encoding="utf-8") as output:
            output.write("<html>new dashboard</html>")
        runtime = os.path.join(self.tempdir.name, ".runtime")
        os.makedirs(runtime)
        with open(os.path.join(runtime, "page-data.json"), "w", encoding="utf-8") as output:
            output.write('{"products":[]}')
        with open(os.path.join(runtime, "app.js"), "w", encoding="utf-8") as output:
            output.write("window.loaded=true")
        modules = os.path.join(runtime, "modules")
        os.makedirs(modules)
        with open(os.path.join(modules, "factors.json"), "w", encoding="utf-8") as output:
            output.write('{"status":"ok"}')
        limiter = AuthLimiter(max_failures=2, window_seconds=60)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.index_path, "viewer", "long-password", limiter))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tempdir.cleanup()

    def request(self, authorization=None, client="198.51.100.1", path="/", extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        headers = {"CF-Connecting-IP": client}
        if authorization:
            headers["Authorization"] = authorization
        headers.update(extra_headers or {})
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), body

    def test_authentication_and_latest_index(self):
        status, headers, _ = self.request(client="198.51.100.2")
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)
        token = base64.b64encode(b"viewer:long-password").decode("ascii")
        status, _, body = self.request("Basic " + token, client="198.51.100.2")
        self.assertEqual(status, 200)
        self.assertIn(b"new dashboard", body)
        with open(self.index_path, "w", encoding="utf-8") as output:
            output.write("<html>refreshed atomically</html>")
        status, _, body = self.request("Basic " + token, client="198.51.100.2")
        self.assertEqual(status, 200)
        self.assertIn(b"refreshed atomically", body)

    def test_protected_assets_use_gzip_etag_and_conditional_cache(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        status, headers, body = self.request(token, path="/api/page-data",
                                             extra_headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(body), b'{"products":[]}')
        self.assertIn("ETag", headers)
        status, _, body = self.request(token, path="/api/page-data",
                                       extra_headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(self.request(path="/assets/app.js")[0], 401)
        status, _, body = self.request(token, path="/api/modules/factors")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}')
        self.assertEqual(self.request(token, path="/api/modules/not-allowed")[0], 404)

    def test_valuation_archive_download_is_authenticated(self):
        self.assertEqual(self.request(path="/api/valuation-archive?date=2026-09-02")[0], 401)
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        with patch("valuation_app.valuation_archive.build_valuation_archive",
                   return_value=(b"zip-content", 3)):
            status, headers, body = self.request(
                token, path="/api/valuation-archive?date=2026-09-02")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("2026-09-02", headers["Content-Disposition"])
        self.assertEqual(body, b"zip-content")

    def test_failed_logins_are_rate_limited(self):
        client = "198.51.100.3"
        self.assertEqual(self.request("Basic wrong", client)[0], 401)
        self.assertEqual(self.request("Basic wrong", client)[0], 401)
        self.assertEqual(self.request("Basic wrong", client)[0], 429)

    def test_server_is_bound_to_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class CredentialTest(unittest.TestCase):
    def test_missing_credentials_refuse_to_start(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        try:
            with self.assertRaises(RuntimeError):
                _credentials(path)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
