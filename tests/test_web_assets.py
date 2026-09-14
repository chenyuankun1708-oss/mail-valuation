import os
import unittest

import valuation_app.static as static


class WebAssetSourceTest(unittest.TestCase):
    def test_frontend_sources_are_split_without_patch_chains(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("shell.html", "styles.css", "bootstrap.js", "core.js", "dashboard.js"):
            self.assertTrue(os.path.isfile(os.path.join(root, "web_assets", name)))
        with open(static.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("HTML = HTML.replace", source)
        self.assertNotIn("DASHBOARD_JS = DASHBOARD_JS.replace", source)
        self.assertLess(len(source), 20000)
        self.assertIn("const RAW=__DATA__", static.CORE_JS)
        self.assertIn("const DASH_PAGES=", static.DASHBOARD_JS)
        self.assertIn("#home", static.HTML)

    def test_bootstrap_defers_full_payload_on_home_and_preserves_deep_links(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "web_assets", "bootstrap.js"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("/api/v2/bootstrap", source)
        self.assertIn("/api/page-data", source)
        self.assertIn("if(hash!=='home')", source)
        self.assertIn("data/ledger", source)
        self.assertIn("overview/report", source)
        self.assertIn("aria-expanded", source)

    def test_structured_help_history_and_status_page_are_registered(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("help.json", "history.json"):
            with open(os.path.join(root, "web_assets", name), "r", encoding="utf-8") as handle:
                payload = __import__("json").load(handle)
            self.assertEqual(1, payload["schema_version"])
        self.assertIn("data-status", {item["id"] for item in static.HELP_DATA["sections"]})
        self.assertIn("function renderDataStatus()", static.DASHBOARD_JS)
        self.assertIn("DASH_PAGES.status='模块数据状态'", static.DASHBOARD_JS)


if __name__ == "__main__":
    unittest.main()
