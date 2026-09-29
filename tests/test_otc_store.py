import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from valuation_app.otc_store import OtcStore, TaskConflictError


class OtcStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = OtcStore(self.tempdir.name)
        self.request = {"structure": "classic_snowball", "index_code": "000852",
                        "start_date": "2024-01-01", "end_date": "2026-01-01"}

    def tearDown(self):
        self.tempdir.cleanup()

    def test_submission_is_persistent_and_single_concurrency(self):
        with patch.object(OtcStore, "_run_worker", return_value=None):
            first = self.store.submit(self.request)
            self.assertEqual(first["status"], "queued")
            with self.assertRaises(TaskConflictError):
                self.store.submit(self.request)
        reopened = OtcStore(self.tempdir.name)
        self.assertEqual(reopened.get_run(first["id"])["status"], "interrupted")

    def test_completed_result_is_paginated_and_paths_are_fixed(self):
        with patch.object(OtcStore, "_run_worker", return_value=None):
            run = self.store.submit(self.request)
        run_root = os.path.join(self.store.runs_root, run["id"])
        os.makedirs(run_root)
        result_path = os.path.join(run_root, "result.json")
        excel_path = os.path.join(run_root, "details.xlsx")
        pdf_path = os.path.join(run_root, "report.pdf")
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": {"total_samples": 25}, "samples": [{"n": i} for i in range(25)]}, handle)
        with open(excel_path, "wb") as handle:
            handle.write(b"PK")
        with open(pdf_path, "wb") as handle:
            handle.write(b"%PDF")
        connection = sqlite3.connect(self.store.db_path)
        connection.execute("UPDATE backtest_runs SET status='completed',result_path=?,excel_path=?,pdf_path=? WHERE id=?",
                           (os.path.relpath(result_path, self.store.root),
                            os.path.relpath(excel_path, self.store.root),
                            os.path.relpath(pdf_path, self.store.root), run["id"]))
        connection.commit(); connection.close()
        page = self.store.samples(run["id"], 2, 20)
        self.assertEqual(page["total"], 25)
        self.assertEqual(len(page["items"]), 5)
        self.assertTrue(self.store.download_path(run["id"], "xlsx").endswith("details.xlsx"))
        self.assertTrue(self.store.download_path(run["id"], "pdf").endswith("report.pdf"))
        with self.assertRaises(KeyError):
            self.store.get_run("../../secret")

    def test_pricing_run_is_persistent_and_shares_numeric_concurrency(self):
        request = {"structure": "classic_snowball", "index_code": "000852"}
        with patch.object(OtcStore, "_run_pricing_worker", return_value=None):
            run = self.store.submit_pricing(request, "tester")
            self.assertEqual(run["status"], "queued")
            self.assertEqual(run["request"]["term_months"], 24)
            self.assertTrue(run["request"]["product_name"].startswith("经典雪球－中证1000－"))
            with self.assertRaises(TaskConflictError):
                self.store.submit(self.request)
        reopened = OtcStore(self.tempdir.name)
        self.assertEqual(reopened.get_pricing_run(run["id"])["status"], "interrupted")
        payload = reopened.pricing_payload()
        self.assertEqual(payload["model"]["structure"], "classic_snowball")
        self.assertEqual(len(payload["structures"]), 4)

    def test_pricing_linked_product_name_is_server_authoritative(self):
        product = self.store.create_product({
            "name": "正式雪球产品", "structure": "classic_snowball",
            "index_code": "000852", "terms": {}, "reference": {}}, "tester")
        request = {"structure": "classic_snowball", "index_code": "000852",
                   "product_id": product["id"], "product_revision": product["revision"],
                   "product_name": "浏览器覆盖名称"}
        with patch.object(OtcStore, "_run_pricing_worker", return_value=None):
            run = self.store.submit_pricing(request, "tester")
        self.assertEqual(run["request"]["product_name"], "正式雪球产品")


if __name__ == "__main__":
    unittest.main()
