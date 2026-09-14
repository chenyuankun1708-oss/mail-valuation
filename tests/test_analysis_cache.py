import json
import os
import sqlite3
import tempfile
import unittest

from valuation_app.analysis_cache import build_analysis_database, load_products, source_record
from valuation_app.config import PRODUCTS, TOP_PRODUCT_IDS, TOP_PRODUCTS_BY_ID


class StableProductIdentityTest(unittest.TestCase):
    def test_all_top_products_have_unique_stable_ids(self):
        self.assertEqual(list(PRODUCTS), list(TOP_PRODUCT_IDS))
        self.assertEqual(17, len(set(TOP_PRODUCT_IDS.values())))
        self.assertTrue(all(value.startswith("top-") for value in TOP_PRODUCT_IDS.values()))
        for name, product_id in TOP_PRODUCT_IDS.items():
            self.assertEqual(name, TOP_PRODUCTS_BY_ID[product_id])


class AnalysisDatabaseTest(unittest.TestCase):
    def test_atomic_database_contains_normalized_build_data_and_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.xlsx")
            with open(source, "wb") as handle:
                handle.write(b"valuation")
            record = source_record(source)
            database = os.path.join(root, "analysis.sqlite3")
            products = [{"product_id": "top-001", "name": "产品一", "points": [{
                "valuation_date": "2026-09-14", "nav": 1.1, "accumulated_nav": 1.2,
                "net_assets": 100, "shares": 90, "source_file": "source.xlsx",
                "source_sha256": record["sha256"], "holdings": [{"code": "A", "name": "持仓",
                    "quantity": 2, "price": 3, "market_value": 6, "cost": 5,
                    "valuation_gain": 1}]}], "flows": [{"flow_date": "2026-09-01",
                                                               "amount": 10, "type": "申购"}]}]
            build_analysis_database(database, products, "2026-09-14T12:00:00", [record])
            self.assertEqual(products[0]["points"], load_products(database)[0]["points"])
            connection = sqlite3.connect(database)
            try:
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual(record["sha256"], connection.execute(
                    "SELECT sha256 FROM source_files").fetchone()[0])
            finally:
                connection.close()

    def test_failed_rebuild_does_not_replace_existing_database(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "analysis.sqlite3")
            product = {"product_id": "top-001", "name": "一", "points": [], "flows": []}
            build_analysis_database(path, [product], "first")
            with self.assertRaises(sqlite3.IntegrityError):
                build_analysis_database(path, [product, product], "second")
            connection = sqlite3.connect(path)
            try:
                self.assertEqual("first", connection.execute(
                    "SELECT value FROM metadata WHERE key='generated_at'").fetchone()[0])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
