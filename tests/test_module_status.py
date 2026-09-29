import unittest

from valuation_app.static import ARCHITECTURE_DATA, HELP_DATA, HISTORY_DATA


class ModuleStatusSourceTest(unittest.TestCase):
    def test_structured_document_sources_are_versioned_and_nonduplicated(self):
        self.assertEqual(1, HELP_DATA["schema_version"])
        self.assertEqual(1, HISTORY_DATA["schema_version"])
        self.assertEqual(1, ARCHITECTURE_DATA["schema_version"])
        ids = [item["id"] for item in HELP_DATA["sections"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.get("date") and item.get("title")
                            for item in HISTORY_DATA["items"]))
        layer_ids = [item["id"] for item in ARCHITECTURE_DATA["layers"]]
        self.assertEqual(len(layer_ids), len(set(layer_ids)))
        indexed_files = {item["file"] for item in ARCHITECTURE_DATA["file_index"]}
        self.assertTrue({"app.py", "valuation_app/web.py", "valuation_app/static.py",
                         "web_assets/dashboard.js"}.issubset(indexed_files))


if __name__ == "__main__":
    unittest.main()
