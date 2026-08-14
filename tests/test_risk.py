import os
import tempfile
import unittest
from datetime import datetime

from valuation_app.risk import (FUTURES, TENORS, _normalize_futures,
                                _normalize_index, load_cache, update_cache)


class Cursor(object):
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, query, **params):
        self.connection.calls.append((query, params))
        if "AIndexEODPrices" in query:
            self.rows = [("20100104", 1000), ("20100105", 990)]
        else:
            prefix = params["prefix"]
            self.rows = [("20100104", prefix + "1001.CFE", 990, "20100115"),
                         ("20100104", prefix + "1002.CFE", 980, "20100219"),
                         ("20100104", prefix + "1003.CFE", 970, "20100319"),
                         ("20100104", prefix + "1006.CFE", 960, "20100618")]
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Connection(object):
    def __init__(self):
        self.calls = []
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class RiskDataTest(unittest.TestCase):
    def test_four_tenors_and_positive_discount(self):
        spot = _normalize_index([("20100104", 1000)])
        rows = [("20100104", "IF1001.CFE", 990, "20100115"),
                ("20100104", "IF1002.CFE", 980, "20100219"),
                ("20100104", "IF1003.CFE", 970, "20100319"),
                ("20100104", "IF1006.CFE", 960, "20100618"),
                ("20100104", "BAD", -1, "20100115")]
        points = _normalize_futures(rows, spot)
        self.assertEqual(set(points[0]) - {"date"}, set(TENORS))
        self.assertGreater(points[0]["当月"]["value"], 0)

    def test_update_queries_three_products_and_writes_cache(self):
        connection = Connection()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "risk.json")
            result = update_cache(path, connect=lambda: connection,
                                  now=datetime(2026, 8, 13, 10, 0))
            cache = load_cache(path)
        self.assertEqual(result["successes"], list(FUTURES))
        self.assertEqual(set(cache["futures"]), set(FUTURES))
        self.assertTrue(connection.closed)
        self.assertTrue(all(":prefix" in call[0] for call in connection.calls if "Futures" in call[0]))


if __name__ == "__main__":
    unittest.main()
