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
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": {"total_samples": 25}, "samples": [{"n": i} for i in range(25)]}, handle)
        with open(excel_path, "wb") as handle:
            handle.write(b"PK")
        connection = sqlite3.connect(self.store.db_path)
        connection.execute("UPDATE backtest_runs SET status='completed',result_path=?,excel_path=? WHERE id=?",
                           (os.path.relpath(result_path, self.store.root),
                            os.path.relpath(excel_path, self.store.root), run["id"]))
        connection.commit(); connection.close()
        page = self.store.samples(run["id"], 2, 20)
        self.assertEqual(page["total"], 25)
        self.assertEqual(len(page["items"]), 5)
        self.assertTrue(self.store.download_path(run["id"], "xlsx").endswith("details.xlsx"))
        with self.assertRaises(KeyError):
            self.store.get_run("../../secret")


if __name__ == "__main__":
    unittest.main()
