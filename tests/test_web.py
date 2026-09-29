import base64
import gzip
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from http.server import ThreadingHTTPServer

from valuation_app.web import AuthLimiter, _credentials, create_server, make_handler


class ShareServerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.index_path = os.path.join(self.tempdir.name, "index.html")
        with open(self.index_path, "w", encoding="utf-8") as output:
            output.write("<html>new dashboard</html>")
        runtime = os.path.join(self.tempdir.name, ".runtime")
        os.makedirs(runtime)
        with open(os.path.join(runtime, "page-data.json"), "w", encoding="utf-8") as output:
            output.write('{"products":[],"benchmarks":{"indices":{"000852":{"points":[]}}}}')
        with open(os.path.join(runtime, "app.js"), "w", encoding="utf-8") as output:
            output.write("window.loaded=true")
        assets = os.path.join(runtime, "assets", "0123456789abcdef")
        os.makedirs(assets)
        with open(os.path.join(assets, "styles.css"), "w", encoding="utf-8") as output:
            output.write("body{color:#123}")
        with open(os.path.join(assets, "core.js"), "w", encoding="utf-8") as output:
            output.write("window.coreLoaded=true")
        with open(os.path.join(assets, "dashboard.js"), "w", encoding="utf-8") as output:
            output.write("window.dashboardLoaded=true")
        with open(os.path.join(assets, "otc_derivatives.js"), "w", encoding="utf-8") as output:
            output.write("window.otcLoaded=true")
        with open(os.path.join(assets, "bootstrap.js"), "w", encoding="utf-8") as output:
            output.write("window.bootstrapLoaded=true")
        modules = os.path.join(runtime, "modules")
        os.makedirs(modules)
        with open(os.path.join(modules, "factors.json"), "w", encoding="utf-8") as output:
            output.write('{"status":"ok"}')
        with open(os.path.join(modules, "strategy-lab.json"), "w", encoding="utf-8") as output:
            output.write('{"schema_version":1,"research_only":true}')
        with open(os.path.join(modules, "market-research.json"), "w", encoding="utf-8") as output:
            output.write('{"updated_at":"2026-09-14","macro":{"series":[]},"errors":[]}')
        with open(os.path.join(modules, "bottom-returns.json"), "w", encoding="utf-8") as output:
            output.write('{"products":[{"product_id":"P001","product":"底层A"}]}')
        sources = os.path.join(self.tempdir.name, "data_sources")
        os.makedirs(sources)
        with open(os.path.join(sources, "product_labels.json"), "w", encoding="utf-8") as output:
            json.dump({"schema_version": 1, "revision": 1, "updated_at": "2026-09-07",
                       "records": []}, output)
        logs = os.path.join(self.tempdir.name, "logs")
        os.makedirs(logs)
        with open(os.path.join(logs, "CHANGELOG.md"), "w", encoding="utf-8") as output:
            output.write("# 历史改动\n\n- 测试变更\n")
        web_assets = os.path.join(self.tempdir.name, "web_assets")
        os.makedirs(web_assets)
        with open(os.path.join(web_assets, "system_qa.json"), "w", encoding="utf-8") as output:
            json.dump({"schema_version": 1, "items": [{"question": "如何更新？", "answer": ["运行refresh"]}]}, output)
        limiter = AuthLimiter(max_failures=2, window_seconds=60)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.index_path, "viewer", "long-password", limiter,
                                                       system_docs_user="test-doc-user",
                                                       system_docs_password="test-doc-password"))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tempdir.cleanup()

    def request(self, authorization=None, client="198.51.100.1", path="/", extra_headers=None,
                method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        headers = {"CF-Connecting-IP": client}
        if authorization:
            headers["Authorization"] = authorization
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
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

    def test_health_endpoint_identifies_running_server_code(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/health")[0], 401)
        status, _, body = self.request(token, path="/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertRegex(payload["server_code_sha256"], r"^[0-9a-f]{64}$")

    def test_system_docs_require_independent_credentials(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        path = "/api/system-docs/access"
        valid = json.dumps({"username": "test-doc-user", "password": "test-doc-password",
                            "document": "qa"}).encode("utf-8")
        self.assertEqual(self.request(path=path, method="POST", body=valid)[0], 401)
        invalid = json.dumps({"username": "test-doc-user", "password": "wrong",
                              "document": "qa"}).encode("utf-8")
        self.assertEqual(self.request(token, path=path, method="POST", body=invalid)[0], 403)
        status, headers, body = self.request(token, path=path, method="POST", body=valid)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["document"], "qa")
        self.assertIn("如何更新", payload["content"]["items"][0]["question"])
        history = json.dumps({"username": "test-doc-user", "password": "test-doc-password",
                              "document": "history"}).encode("utf-8")
        status, _, body = self.request(token, path=path, method="POST", body=history)
        self.assertEqual(status, 200)
        self.assertIn("测试变更", json.loads(body.decode("utf-8"))["content"])
        self.assertNotIn(b"test-doc-password", body)

    def test_create_server_refuses_a_duplicate_listener(self):
        env_path = os.path.join(self.tempdir.name, ".env")
        with open(env_path, "w", encoding="utf-8") as output:
            output.write("SHARE_USER=viewer\nSHARE_PASSWORD=long-password\nSYSTEM_QA_USER=test-doc-user\nSYSTEM_QA_PASSWORD=test-doc-password\n")
        first = create_server(self.index_path, port=0, env_path=env_path)
        port = first.server_address[1]
        try:
            with self.assertRaises(OSError):
                create_server(self.index_path, port=port, env_path=env_path)
        finally:
            first.server_close()

    def test_protected_assets_use_gzip_etag_and_conditional_cache(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        status, headers, body = self.request(token, path="/api/page-data",
                                             extra_headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertIn(b'"products":[]', gzip.decompress(body))
        self.assertIn("ETag", headers)
        status, _, body = self.request(token, path="/api/page-data",
                                       extra_headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(self.request(path="/assets/app.js")[0], 401)
        self.assertEqual(self.request(path="/assets/0123456789abcdef/core.js",
                                      client="198.51.100.8")[0], 401)
        status, versioned_headers, body = self.request(
            token, path="/assets/0123456789abcdef/core.js",
            extra_headers={"Accept-Encoding": "gzip"}, client="198.51.100.9")
        self.assertEqual(status, 200)
        self.assertEqual(gzip.decompress(body), b"window.coreLoaded=true")
        self.assertIn("immutable", versioned_headers["Cache-Control"])
        self.assertEqual(self.request(token, path="/assets/../../secret/core.js")[0], 404)
        status, _, body = self.request(token, path="/api/modules/factors")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}')
        status, _, body = self.request(token, path="/api/modules/strategy-lab")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["research_only"])
        self.assertEqual(self.request(token, path="/api/modules/not-allowed")[0], 404)

    def test_v2_bootstrap_is_small_authenticated_versioned_and_cached(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/v2/bootstrap")[0], 401)
        status, headers, body = self.request(token, path="/api/v2/bootstrap",
                                             extra_headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertLess(len(body), 250 * 1024)
        payload = json.loads(gzip.decompress(body).decode("utf-8"))
        for field in ("version", "generated_at", "data_cutoff", "source_count",
                      "status", "warning_count", "warnings", "data"):
            self.assertIn(field, payload)
        self.assertEqual(payload["data"]["navigation"][0], "home")
        self.assertIn("ETag", headers)
        self.assertEqual(self.request(token, path="/api/v2/bootstrap?path=secret")[0], 400)
        status, _, asset = self.request(token, path="/assets/0123456789abcdef/bootstrap.js")
        self.assertEqual(status, 200)
        self.assertIn(b"bootstrapLoaded", asset)

    def test_otc_module_and_backtest_routes_are_authenticated_and_bounded(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/modules/otc-derivatives")[0], 401)
        status, headers, body = self.request(
            token, path="/api/modules/otc-derivatives",
            extra_headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        payload = json.loads(gzip.decompress(body).decode("utf-8"))
        self.assertEqual({item["code"] for item in payload["structures"]},
                         {"classic_snowball", "european_snowball", "dcn", "dcn_snowball_combo"})
        self.assertIn("dcn_max_loss", {item["name"] for item in payload["parameter_definitions"]})
        invalid = json.dumps({"structure": "classic_snowball", "index_code": "000016",
                              "start_date": "2024-01-01", "end_date": "2025-01-01"}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/otc/backtests", method="POST", body=invalid,
                                      extra_headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request(token, path="/api/otc/backtests/../../secret")[0], 404)
        self.assertEqual(self.request(token, path="/api/otc/backtests?page=1&page_size=201")[0], 400)
        status, _, pricing_body = self.request(token, path="/api/otc/pricing")
        self.assertEqual(status, 200)
        pricing = json.loads(pricing_body.decode("utf-8"))
        self.assertEqual(pricing["model"]["structure"], "classic_snowball")
        self.assertFalse(next(x for x in pricing["structures"] if x["code"] == "dcn")["enabled"])
        self.assertNotIn("sha256", pricing_body.decode("utf-8").lower())
        invalid_pricing = json.dumps({"structure": "dcn", "index_code": "000852"}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/otc/pricing/runs", method="POST",
                                      body=invalid_pricing,
                                      extra_headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request(token, path="/api/otc/pricing/runs/../../secret")[0], 404)
        status, _, asset = self.request(token, path="/assets/0123456789abcdef/otc_derivatives.js")
        self.assertEqual(status, 200)
        self.assertIn(b"otcLoaded", asset)

    def test_otc_ledger_crud_is_authenticated_revisioned_and_soft_deleted(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        values = {"name": "网页发行", "strategy_name": "DCN", "structure": "dcn",
                  "index_code": "000852", "notional": 10000000, "start_date": "2026-01-02",
                  "end_date": "2026-12-31", "legacy_annual_return": .08,
                  "terms": {}, "reference": {}, "notes": ""}
        status, _, body = self.request(token, path="/api/otc/products", method="POST",
                                       body=json.dumps({"values": values}).encode("utf-8"))
        self.assertEqual(status, 201); product = json.loads(body)["product"]
        product_id = product["id"]
        self.assertEqual(product["end_date"], "2026-12-31")
        aggregate = json.dumps({"product_ids": [product_id]}).encode("utf-8")
        status, _, body = self.request(token, path="/api/otc/products/aggregate",
                                       method="POST", body=aggregate)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["aggregates"]["count"], 1)
        self.assertEqual(self.request(token, path="/api/otc/products?strategy_name=DCN&end_from=2026-01-01&sort_by=name&sort_dir=asc")[0], 200)
        invalid_ids = json.dumps({"product_ids": ["../secret"]}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/otc/products/aggregate",
                                      method="POST", body=invalid_ids)[0], 400)
        status, _, body = self.request(token, path="/api/otc/products/%s" % product_id)
        self.assertEqual(status, 200)
        event = {"event_type": "生效", "event_date": "2026-01-02", "values": {"note": "人工"},
                 "expected_revision": product["revision"]}
        status, _, body = self.request(token, path="/api/otc/products/%s/events" % product_id,
                                       method="POST", body=json.dumps(event).encode("utf-8"))
        self.assertEqual(status, 201); active = json.loads(body)["product"]
        self.assertEqual(active["status"], "存续")
        stale = {"values": {"notes": "冲突"}, "expected_revision": 1}
        self.assertEqual(self.request(token, path="/api/otc/products/%s" % product_id,
                                      method="PATCH", body=json.dumps(stale).encode("utf-8"))[0], 409)
        deletion = json.dumps({"expected_revision": active["revision"]}).encode("utf-8")
        status, _, body = self.request(token, path="/api/otc/products/%s" % product_id,
                                       method="DELETE", body=deletion)
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)["product"]["status"], "已作废")
        self.assertEqual(self.request(path="/api/otc/products")[0], 401)
        self.assertEqual(self.request(token, path="/api/otc/products?unknown=x")[0], 400)

    def test_otc_attachments_are_uuid_scoped_deduplicated_and_soft_deleted(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        create = json.dumps({"values": {"name": "附件接口产品", "terms": {},
                                         "reference": {}}}, ensure_ascii=False).encode("utf-8")
        status, _, body = self.request(token, path="/api/otc/products", method="POST", body=create)
        self.assertEqual(status, 201)
        product_id = json.loads(body.decode("utf-8"))["product"]["id"]
        path = "/api/otc/products/%s/attachments" % product_id
        upload = json.dumps({"filename": "合同.md", "category": "产品合同",
                             "content_base64": base64.b64encode("本金1000万元".encode("utf-8")).decode("ascii")},
                            ensure_ascii=False).encode("utf-8")
        self.assertEqual(self.request(path=path, method="POST", body=upload)[0], 401)
        status, _, body = self.request(token, path=path, method="POST", body=upload)
        self.assertEqual(status, 201)
        attachment = json.loads(body.decode("utf-8"))["attachment"]
        status, _, body = self.request(token, path=path, method="POST", body=upload)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["duplicate"])
        status, _, body = self.request(token, path=path)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body.decode("utf-8"))["attachments"]), 1)
        detail = path + "/" + attachment["id"]
        status, _, body = self.request(token, path=detail)
        self.assertEqual(status, 200)
        self.assertIn("本金1000", json.loads(body.decode("utf-8"))["attachment"]["text_content"])
        status, headers, body = self.request(token, path=detail + "/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(body, "本金1000万元".encode("utf-8"))
        invalid = json.dumps({"filename": "bad.exe", "category": "产品合同",
                              "content_base64": "eA=="}).encode("utf-8")
        self.assertEqual(self.request(token, path=path, method="POST", body=invalid)[0], 400)
        deletion = json.dumps({"expected_revision": attachment["revision"]}).encode("utf-8")
        self.assertEqual(self.request(token, path=detail, method="DELETE", body=deletion)[0], 200)
        self.assertEqual(self.request(token, path=detail)[0], 404)
        self.assertEqual(self.request(token, path=path + "/../../secret")[0], 404)

    def test_unified_llm_gateway_is_authenticated_and_disabled_without_network(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/llm/status")[0], 401)
        status, _, body = self.request(token, path="/api/llm/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body.decode("utf-8"))["enabled"])
        request = json.dumps({"task_type": "strategy_spec", "methodology": "risk_parity_score",
                              "objective": "test"}).encode("utf-8")
        with patch("valuation_app.llm_gateway.load_providers") as providers:
            status, _, body = self.request(token, path="/api/llm/tasks", method="POST", body=request)
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body.decode("utf-8"))["enabled"])
            providers.assert_not_called()
        unknown = json.dumps({"task_type": "arbitrary_prompt"}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/llm/tasks", method="POST", body=unknown)[0], 400)
        legacy = json.dumps({"methodology": "risk_parity_score", "objective": "test"}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/strategy-lab/llm",
                                      method="POST", body=legacy)[0], 503)

    def test_otc_parameter_template_crud_and_product_aggregates(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        request = {"structure": "classic_snowball", "index_code": "000852",
                   "start_date": "2020-01-01", "end_date": "2026-01-01"}
        status, _, body = self.request(token, path="/api/otc/backtest-templates", method="POST",
                                       body=json.dumps({"name": "标准雪球", "request": request}).encode("utf-8"))
        self.assertEqual(status, 201); template = json.loads(body)["template"]
        status, _, body = self.request(token, path="/api/otc/backtest-templates/%s" % template["id"])
        self.assertEqual(status, 200)
        update = {"name": "标准雪球2", "request": request, "expected_revision": 1}
        self.assertEqual(self.request(token, path="/api/otc/backtest-templates/%s" % template["id"],
                                      method="PATCH", body=json.dumps(update).encode("utf-8"))[0], 200)
        self.assertEqual(self.request(token, path="/api/otc/products?lifecycle=%s&return_state=missing" % quote("存续"))[0], 200)
        self.assertEqual(self.request(token, path="/api/otc/backtest-templates/../../secret")[0], 404)

    def test_v2_parameter_whitelists_pagination_and_path_traversal(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        base = "/api/v2/overview?start=2026-01-01&end=2026-02-01&scope=all"
        self.assertEqual(self.request(token, path=base)[0], 200)
        self.assertEqual(self.request(token, path=base.replace("scope=all", "scope=bad"))[0], 400)
        self.assertEqual(self.request(token, path=base + "&path=../x")[0], 400)
        strategy = "/api/v2/strategy?start=2026-01-01&end=2026-02-01&page=1&page_size=20"
        self.assertEqual(self.request(token, path=strategy)[0], 200)
        self.assertEqual(self.request(token, path=strategy.replace("page_size=20", "page_size=201"))[0], 400)
        self.assertEqual(self.request(token, path="/api/v2/top-returns/../../secret?start=2026-01-01&end=2026-02-01")[0], 404)
        status, _, body = self.request(token, path="/api/v2/market/macro")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["data"]["module"], "macro")
        self.assertEqual(self.request(token, path="/api/v2/market/security")[0], 404)

    def test_bottom_return_endpoint_is_protected_validated_and_cached(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        path = "/api/bottom-returns/P001?start=2026-01-01&end=2026-02-01&benchmark=000852"
        self.assertEqual(self.request(path=path)[0], 401)
        payload = {"status": "ok", "product": {"product_id": "P001"}, "holdings": []}
        with patch("valuation_app.web.analyze_bottom_return", return_value=payload) as analyze:
            status, headers, body = self.request(
                token, path=path, extra_headers={"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(gzip.decompress(body).decode("utf-8")), payload)
            self.assertIn("ETag", headers)
            self.assertEqual(headers["Cache-Control"], "private, max-age=0, must-revalidate")
            status, _, body = self.request(
                token, path=path, extra_headers={"If-None-Match": headers["ETag"]})
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
            self.assertEqual(analyze.call_args[0][2], "P001")
        with patch("valuation_app.web.analyze_bottom_return",
                   side_effect=ValueError("基准指数不在固定白名单")):
            self.assertEqual(self.request(
                token, path=path.replace("000852", "000001"))[0], 400)
        self.assertEqual(self.request(token, path=path + "&security=600000")[0], 400)
        self.assertEqual(self.request(token, path=path.replace("2026-01-01", "bad-date"))[0], 400)
        self.assertEqual(self.request(token, path=path.replace("P001", "UNKNOWN"))[0], 404)
        self.assertEqual(self.request(token, path=path.replace("P001", "SB9057"))[0], 404)
        self.assertEqual(self.request(token, path="/api/bottom-returns/../secret")[0], 404)

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

    def test_label_api_is_authenticated_versioned_and_audited(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/labels")[0], 401)
        status, _, body = self.request(token, path="/api/labels")
        self.assertEqual(status, 200)
        catalog = json.loads(body.decode("utf-8"))
        self.assertEqual(catalog["revision"], 1)
        self.assertEqual(catalog["deduped_records"], [])
        self.assertEqual(catalog["dedupe_check"]["duplicate_groups"], 0)
        create = json.dumps({"expected_revision": 1, "values": {
            "product": "测试产品", "manager": "测试管理人", "primary": "CTA",
            "secondary": "全品种", "vehicle": "专户", "classification_basis": "人工确认"
        }}, ensure_ascii=False).encode("utf-8")
        status, _, body = self.request(token, path="/api/labels", method="POST", body=create,
                                       extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        created = json.loads(body.decode("utf-8"))
        record_id = created["record"]["record_id"]
        self.assertEqual(created["revision"], 2)
        self.assertEqual(created["record"]["department"], "无")
        self.assertEqual(self.request(token, path="/api/labels", method="POST", body=create,
                                      extra_headers={"Content-Type": "application/json"})[0], 409)
        update = json.dumps({"expected_revision": 2,
                             "values": {"secondary": "管理期货", "department": "华东营业部"}},
                            ensure_ascii=False).encode("utf-8")
        status, _, body = self.request(token, path="/api/labels/" + record_id,
                                       method="PATCH", body=update,
                                       extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))["record"]
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["department"], "华东营业部")
        deactivate = json.dumps({"expected_revision": 3}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/labels/" + record_id,
                                      method="DELETE", body=deactivate,
                                      extra_headers={"Content-Type": "application/json"})[0], 200)
        self.assertTrue(os.path.exists(os.path.join(self.tempdir.name, "logs", "label-audit.jsonl")))
        self.assertTrue(os.path.isdir(os.path.join(self.tempdir.name, "data_sources", "label_backups")))

    def test_core_report_excel_is_authenticated(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        payload = json.dumps({"summary": {}, "attention": [], "contributions": [],
                              "concentration": {}, "risk": {}, "quality": {},
                              "holding_changes": []}).encode("utf-8")
        self.assertEqual(self.request(path="/api/core-report.xlsx", method="POST", body=payload,
                                      extra_headers={"Content-Type": "application/json"})[0], 401)
        status, headers, body = self.request(token, path="/api/core-report.xlsx", method="POST",
                                             body=payload,
                                             extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn("spreadsheetml", headers["Content-Type"])
        self.assertTrue(body.startswith(b"PK"))

    def test_attribution_excel_is_authenticated(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        payload = json.dumps({"summary": {}, "products": [], "underlying": [],
                              "strategies": [], "managers": [], "actions": [],
                              "intervals": []}).encode("utf-8")
        self.assertEqual(self.request(path="/api/attribution.xlsx", method="POST", body=payload,
                                      extra_headers={"Content-Type": "application/json"})[0], 401)
        status, headers, body = self.request(token, path="/api/attribution.xlsx", method="POST",
                                             body=payload,
                                             extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn("spreadsheetml", headers["Content-Type"])
        self.assertTrue(body.startswith(b"PK"))

    def test_knowledge_api_is_authenticated_and_uses_document_ids(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/knowledge")[0], 401)
        upload = json.dumps({
            "filename": "会议纪要.md",
            "content_base64": base64.b64encode("讨论源泉优享FOF3号".encode("utf-8")).decode("ascii"),
            "metadata": {"document_type": "会议纪要", "tags": ["FOF"]},
        }, ensure_ascii=False).encode("utf-8")
        status, _, body = self.request(token, path="/api/knowledge/upload", method="POST",
                                       body=upload, extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        created = json.loads(body.decode("utf-8"))["document"]
        document_id = created["id"]
        status, _, body = self.request(token, path="/api/knowledge?q=" + quote("源泉优享"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["documents"][0]["id"], document_id)
        status, _, body = self.request(token, path="/api/knowledge/%s" % document_id)
        self.assertIn("源泉优享", json.loads(body.decode("utf-8"))["text_content"])
        status, _, body = self.request(token, path="/api/knowledge/%s/download" % document_id)
        self.assertEqual(status, 200)
        self.assertIn("源泉优享", body.decode("utf-8"))
        update = json.dumps({"expected_revision": created["revision"],
                             "values": {"title": "修改后的纪要"}}, ensure_ascii=False).encode("utf-8")
        status, _, body = self.request(token, path="/api/knowledge/%s" % document_id,
                                       method="PATCH", body=update,
                                       extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))["document"]
        deactivate = json.dumps({"expected_revision": updated["revision"]}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/knowledge/%s" % document_id,
                                      method="DELETE", body=deactivate,
                                      extra_headers={"Content-Type": "application/json"})[0], 200)
        self.assertEqual(json.loads(self.request(token, path="/api/knowledge")[2].decode("utf-8"))["documents"], [])

    def test_knowledge_source_graph_and_export_endpoints_are_fixed_and_authenticated(self):
        token = "Basic " + base64.b64encode(b"viewer:long-password").decode("ascii")
        self.assertEqual(self.request(path="/api/knowledge/sources")[0], 401)
        self.assertEqual(self.request(path="/api/knowledge/facts", client="198.51.100.9")[0], 401)
        status, _, body = self.request(token, path="/api/knowledge/sources")
        self.assertEqual(status, 200)
        source = json.loads(body.decode("utf-8"))["sources"][0]
        self.assertEqual(source["id"], "wechat")
        self.assertNotIn("root", source)
        self.assertNotIn("path", source)
        status, _, body = self.request(token, path="/api/knowledge/candidates?page_size=20")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["items"], [])
        status, _, body = self.request(token, path="/api/knowledge/graph?limit=20")
        self.assertEqual(status, 200)
        graph = json.loads(body.decode("utf-8"))
        self.assertIn("nodes", graph)
        self.assertIn("type_counts", graph)
        self.assertEqual(self.request(token, path="/api/knowledge/graph?center_id=..%2Fsecret")[0], 400)
        status, _, body = self.request(token, path="/api/knowledge/facts?page_size=20")
        self.assertEqual(status, 200)
        self.assertIn("fact_types", json.loads(body.decode("utf-8")))
        self.assertEqual(self.request(token, path="/api/knowledge/facts?type=" + quote("任意字段"))[0], 400)
        self.assertEqual(self.request(token, path="/api/knowledge/dossiers/..%2Fsecret")[0], 400)
        invalid_batch = json.dumps({"action": "confirm", "items": []}).encode("utf-8")
        self.assertEqual(self.request(token, path="/api/knowledge/facts/batch", method="POST",
                                      body=invalid_batch,
                                      extra_headers={"Content-Type": "application/json"})[0], 400)
        status, _, body = self.request(token, path="/api/knowledge/obsidian-export", method="POST",
                                       body=b"{}", extra_headers={"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        export_id = json.loads(body.decode("utf-8"))["export_id"]
        status, headers, body = self.request(token, path="/api/knowledge/exports/%s/download" % export_id)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(body.startswith(b"PK"))

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
