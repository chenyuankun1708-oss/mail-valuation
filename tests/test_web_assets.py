import os
import unittest

import valuation_app.static as static


class WebAssetSourceTest(unittest.TestCase):
    def test_frontend_sources_are_split_without_patch_chains(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("shell.html", "styles.css", "core.js", "dashboard.js"):
            self.assertTrue(os.path.isfile(os.path.join(root, "web_assets", name)))
        with open(static.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("HTML = HTML.replace", source)
        self.assertNotIn("DASHBOARD_JS = DASHBOARD_JS.replace", source)
        self.assertLess(len(source), 20000)
        self.assertIn("const RAW=__DATA__", static.CORE_JS)
        self.assertIn("const DASH_PAGES=", static.DASHBOARD_JS)
        self.assertIn("#home", static.HTML)


if __name__ == "__main__":
    unittest.main()
