import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from valuation_app.benchmark import (INDICES, QUERY, SOURCE_NAME, database_config,
                                     load_cache, page_payload, update_cache)


ROWS = [
    ("20260105", 1020),
    ("20260102", 1010),
    ("20260103", "bad"),
    ("20260102", 1011),
    ("bad-date", 999),
    ("20260106", -1),
]


class FakeCursor(object):
    def __init__(self, rows, fail_code=None, calls=None):
        self.rows = rows
        self.fail_code = fail_code
        self.calls = calls if calls is not None else []

    def execute(self, query, **params):
        self.calls.append((query, params))
        if params.get("code") == self.fail_code:
            raise RuntimeError("secret database detail")

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FakeConnection(object):
    def __init__(self, rows=ROWS, fail_code=None):
        self.rows = rows
        self.fail_code = fail_code
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows, self.fail_code, self.calls)

    def close(self):
        self.closed = True


class BenchmarkCacheTest(unittest.TestCase):
    def test_queries_whitelisted_wind_indices_and_normalizes_points(self):
        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "index_daily.json")
            result = update_cache(path, connect=lambda: connection,
                                  now=datetime(2026, 8, 11, 18, 30))
            cache = load_cache(path)
        self.assertEqual(result["successes"], list(INDICES))
        self.assertEqual(result["failures"], [])
        self.assertTrue(connection.closed)
        self.assertEqual([params["code"] for _, params in connection.calls],
                         [details["wind_code"] for details in INDICES.values()])
        self.assertTrue(all(":code" in query for query, _ in connection.calls))
        calls = {params["code"]: query for query, params in connection.calls}
        self.assertIn("THIRDPARTYINDEXEOD", calls["NH0100.NHF"])
        self.assertNotIn("000852.SH", QUERY)
        self.assertEqual(cache["source"], SOURCE_NAME)
        for code in INDICES:
            self.assertEqual(cache["indices"][code]["points"], [
                {"date": "2026-01-02", "close": 1011.0},
                {"date": "2026-01-05", "close": 1020.0},
            ])

    def test_partial_query_failure_keeps_previous_index_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "index_daily.json")
            update_cache(path, connect=lambda: FakeConnection(),
                         now=datetime(2026, 8, 10))
            result = update_cache(path,
                                  connect=lambda: FakeConnection(fail_code="000905.SH"),
                                  now=datetime(2026, 8, 11))
            cache = load_cache(path)
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(result["failures"][0]["used_cache"])
        self.assertNotIn("secret database detail", result["failures"][0]["error"])
        self.assertEqual(cache["indices"]["000905"]["points"][-1]["close"], 1020.0)
        self.assertEqual(cache["indices"]["000905"]["updated_at"],
                         "2026-08-10T00:00:00")

    def test_connection_failure_preserves_all_wind_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "index_daily.json")
            update_cache(path, connect=lambda: FakeConnection(),
                         now=datetime(2026, 8, 10))

            def fail_connect():
                raise OSError("dsn and password must not leak")

            result = update_cache(path, connect=fail_connect,
                                  now=datetime(2026, 8, 11))
            cache = load_cache(path)
        self.assertEqual(result["successes"], [])
        self.assertEqual(len(result["failures"]), len(INDICES))
        self.assertTrue(all(item["used_cache"] for item in result["failures"]))
        self.assertNotIn("password", json.dumps(result, ensure_ascii=False))
        self.assertTrue(all(cache["indices"][code]["points"] for code in INDICES))

    def test_missing_configuration_is_reported_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            env_path = os.path.join(folder, "missing.env")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    database_config(env_path)
        self.assertIn("WIND_DB_USER", str(caught.exception))
        self.assertIn("WIND_DB_PASSWORD", str(caught.exception))

    def test_page_payload_keeps_history_from_2010_and_preserves_missing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "index_daily.json")
            payload = {
                "source": SOURCE_NAME, "updated_at": "2026-08-11T00:00:00",
                "indices": {"000852": {
                    "code": "000852", "name": "中证1000", "error": None,
                    "updated_at": "2026-08-11T00:00:00", "points": [
                        {"date": "2025-12-01", "close": 900.0},
                        {"date": "2025-12-25", "close": 950.0},
                        {"date": "2026-01-02", "close": 1000.0},
                    ],
                }},
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            page = page_payload(path, earliest_date="2026-01-01")
        self.assertTrue(page["available"])
        self.assertEqual([p["date"] for p in page["indices"]["000852"]["points"]],
                         ["2025-12-01", "2025-12-25", "2026-01-02"])
        self.assertEqual(page["indices"]["000905"]["points"], [])


if __name__ == "__main__":
    unittest.main()
