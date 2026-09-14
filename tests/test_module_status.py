import unittest

from valuation_app.static import HELP_DATA, HISTORY_DATA


class ModuleStatusSourceTest(unittest.TestCase):
    def test_structured_document_sources_are_versioned_and_nonduplicated(self):
        self.assertEqual(1, HELP_DATA["schema_version"])
        self.assertEqual(1, HISTORY_DATA["schema_version"])
        ids = [item["id"] for item in HELP_DATA["sections"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.get("date") and item.get("title")
                            for item in HISTORY_DATA["items"]))


if __name__ == "__main__":
    unittest.main()
