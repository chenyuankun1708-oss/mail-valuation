import os
import tempfile
import unittest
from datetime import datetime

from valuation_app.risk import (FUTURES, TENORS, _normalize_futures,
                                _normalize_fx, _normalize_index, _normalize_multi_curve,
                                _normalize_us_curve, _normalize_yield_curve,
                                load_cache, update_cache)


class YieldFrame(object):
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


class RQData(object):
    def __init__(self, frame=None, error=None):
        self.frame = frame
        self.error = error
        self.calls = []
        self.econ = self

    def init(self, **kwargs):
        self.calls.append(("init", kwargs))
        if self.error:
            raise self.error

    def get_yield_curve(self, **kwargs):
        self.calls.append(("get_yield_curve", kwargs))
        return self.frame

    def get_us_treasury_yield(self, **kwargs):
        self.calls.append(("get_us_treasury_yield", kwargs))
        return YieldFrame([("2026-08-18", {"1M": 0.02, "10Y": 0.04}),
                           ("2026-08-19", {"1M": 0.021, "10Y": 0.041})])

    def get_cny_reference_rate(self, **kwargs):
        self.calls.append(("get_cny_reference_rate", kwargs))
        return YieldFrame([("2026-08-18", {"USD/CNY": 7.0}),
                           ("2026-08-19", {"USD/CNY": 6.99})])


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
    def test_normalize_yield_curve_sorts_deduplicates_and_filters(self):
        frame = YieldFrame([
            ("2026-08-20", {"10Y": 0.0168}),
            ("2026-08-18", {"10Y": 0.0169}),
            ("2026-08-20", {"10Y": 0.0167}),
            ("2026-08-21", {"10Y": float("nan")}),
            ("2026-08-22", {"10Y": -1}),
            ("bad", {"10Y": 0.01}),
        ])
        self.assertEqual(_normalize_yield_curve(frame), [
            {"date": "2026-08-18", "yield": 0.0169},
            {"date": "2026-08-20", "yield": 0.0167},
        ])

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

    def test_normalize_us_curve_and_fx(self):
        frame = YieldFrame([("2026-08-19", {"1M": 0.02, "10Y": 0.04}),
                            ("2026-08-18", {"1M": 0.019, "10Y": float("nan")})])
        self.assertEqual(_normalize_multi_curve(frame, ("1M", "10Y")), [
            {"date": "2026-08-18", "1M": 0.019},
            {"date": "2026-08-19", "1M": 0.02, "10Y": 0.04},
        ])
        self.assertEqual(_normalize_fx(YieldFrame([("2026-08-19", {"USD/CNY": 7.0})])),
                         [{"date": "2026-08-19", "rate": 7.0}])
        self.assertEqual(_normalize_us_curve(YieldFrame([
            ("2026-08-19", {"1M": 3.79, "10Y": 4.70})])), [
                {"date": "2026-08-19", "10Y": 0.047},
            ])

    def test_update_queries_three_products_and_writes_cache(self):
        connection = Connection()
        rqdata = RQData(YieldFrame([("2026-08-18", {"10Y": 0.0169}),
                                    ("2026-08-19", {"10Y": 0.0168})]))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "risk.json")
            env_path = os.path.join(folder, ".env")
            with open(env_path, "w") as handle:
                handle.write("RQDATAC_CONF=test-uri\n")
            result = update_cache(path, connect=lambda: connection, rqdata_client=rqdata,
                                  env_path=env_path, now=datetime(2026, 8, 13, 10, 0))
            cache = load_cache(path)
        self.assertEqual(result["successes"], list(FUTURES))
        self.assertEqual(result["yield_successes"], ["CN10Y"])
        self.assertEqual(result["rqdata_successes"], ["US_TREASURY", "USDCNY"])
        self.assertEqual(set(cache["futures"]), set(FUTURES))
        self.assertEqual(cache["yield_curves"]["CN10Y"]["points"][-1]["yield"], 0.0168)
        self.assertEqual(cache["exchange_rates"]["USDCNY"]["points"][-1]["rate"], 6.99)
        self.assertTrue(connection.closed)
        self.assertTrue(all(":prefix" in call[0] for call in connection.calls if "Futures" in call[0]))

    def test_yield_failure_preserves_previous_cache(self):
        connection = Connection()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "risk.json")
            env_path = os.path.join(folder, ".env")
            with open(env_path, "w") as handle:
                handle.write("RQDATAC_CONF=test-uri\n")
            update_cache(path, connect=lambda: connection,
                         rqdata_client=RQData(YieldFrame([("2026-08-18", {"10Y": 0.0169})])),
                         env_path=env_path, now=datetime(2026, 8, 18, 10, 0))
            result = update_cache(path, connect=lambda: Connection(),
                                  rqdata_client=RQData(error=RuntimeError("secret detail")),
                                  env_path=env_path, now=datetime(2026, 8, 19, 10, 0))
            cache = load_cache(path)
        self.assertEqual(result["yield_successes"], [])
        self.assertTrue(result["yield_failures"][0]["used_cache"])
        self.assertNotIn("secret detail", result["yield_failures"][0]["error"])
        self.assertEqual(cache["yield_curves"]["CN10Y"]["points"][0]["yield"], 0.0169)


if __name__ == "__main__":
    unittest.main()
