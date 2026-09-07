import math
import unittest

from valuation_app.market_research import _metric, build_allocation


def series(code, scale=1.0, count=320):
    points = []
    value = 100.0
    start = __import__("datetime").date(2025, 1, 1)
    for index in range(count):
        day = start + __import__("datetime").timedelta(days=index)
        value *= math.exp(scale * (0.001 if index % 7 else -0.002))
        points.append({"date": day.isoformat(), "close": value})
    return points


class MarketResearchTest(unittest.TestCase):
    def test_rotation_metrics_include_momentum_volatility_and_drawdown(self):
        points = series("x")
        result = _metric(points, points)
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["momentum_12m"])
        self.assertIsNotNone(result["volatility_20d"])
        self.assertLessEqual(result["max_drawdown"], 0)
        self.assertAlmostEqual(result["relative_6m"], 0.0)

    def test_allocation_is_long_only_fully_invested_and_has_backtest(self):
        data = {"000300": series("300", 0.8), "000905": series("500", 1.0),
                "000852": series("1000", 1.2), "000510": series("a500", 0.9)}
        result = build_allocation(data)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(sum(x["weight"] for x in result["recommendations"]), 1.0)
        self.assertTrue(all(x["weight"] >= 0 for x in result["recommendations"]))
        self.assertIn(result["backtest"]["status"], ("ok", "insufficient_history"))
        self.assertEqual(result["backtest"]["cost_scenarios_bp"], [0, 10, 20])


if __name__ == "__main__":
    unittest.main()
