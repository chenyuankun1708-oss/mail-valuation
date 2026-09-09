"""M2 研究工作流模块测试：factor_analysis / sweep / attribution。"""
import math
import unittest

from strategy_lab import factor_analysis, sweep, attribution


def _series(points_map):
    return {code: {"name": code, "points": points, "asset_class": "broad"}
            for code, points in points_map.items()}


def _row(date, code, features, target=None, target_end=""):
    row = {"date": date, "code": code, "name": code, "asset_class": "broad",
           "features": features, "available_at": date + "T15:01:00+08:00"}
    if target is not None:
        row["target_excess_return"] = target
        row["target_end"] = target_end or date
    return row


def _sample_rows(n=8):
    rows = []
    for i in range(n):
        date = "2025-%02d-28" % (i + 1) if i < 12 else "2026-01-31"
        for j, code in enumerate(("000300", "000905")):
            features = {"momentum_1m": 0.01 * (i + j), "momentum_3m": 0.03 * (i - j),
                        "momentum_6m": 0.06 * i, "momentum_12m": 0.12 * i,
                        "volatility_60d": 0.2 + 0.01 * j,
                        "relative_3m": 0.01 * (i - j), "relative_6m": 0.02 * (i - j),
                        "macro_state": 0.5, "style_state": 0.1,
                        "sentiment_state": 0.0}
            rows.append(_row(date, code, features, target=0.01 * ((i + j) % 3 - 1),
                             target_end=date))
    return rows


class FactorAnalysisTest(unittest.TestCase):
    def test_correlation_matrix_shape(self):
        rows = _sample_rows()
        report = factor_analysis.correlation_matrix(rows)
        self.assertEqual(len(report["names"]), 10)
        for a in report["names"]:
            self.assertEqual(report["matrix"][a][a], 1.0)
        # 对称
        for a in report["names"]:
            for b in report["names"]:
                self.assertEqual(report["matrix"][a][b], report["matrix"][b][a])

    def test_vif_returns_all_factors(self):
        rows = _sample_rows()
        report = factor_analysis.vif(rows)
        self.assertEqual(len(report["names"]), 10)
        # momentum_12m 与 momentum_6m 完全共线（线性依赖），其 VIF 应为 None 或很大
        # 这里只验证结构完整
        for name in report["names"]:
            self.assertIn(name, report["vif"])

    def test_rank_ic_summary(self):
        rows = _sample_rows()
        report = factor_analysis.rank_ic_series(rows)
        self.assertIn("summary", report)
        self.assertIn("momentum_1m", report["summary"])
        summary = report["summary"]["momentum_1m"]
        self.assertIn("mean_ic", summary)
        self.assertIn("icir", summary)

    def test_analyze_payload_structure(self):
        rows = _sample_rows()
        payload = factor_analysis.analyze(rows)
        self.assertIn("correlations", payload)
        self.assertIn("vif", payload)
        self.assertIn("rank_ic", payload)
        self.assertIn("high_correlation_pairs", payload)


class SweepTest(unittest.TestCase):
    def test_sweep_alpha_structure(self):
        rows = _sample_rows(6)
        series = _series({"000300": [{"date": "2025-%02d-28" % (i + 1), "close": 100.0 + i} for i in range(10)],
                           "000905": [{"date": "2025-%02d-28" % (i + 1), "close": 50.0 + i} for i in range(10)]})
        report = sweep.sweep_alpha(rows, series, alphas=(1.0, 5.0))
        self.assertEqual(report["dimension"], "ridge_alpha")
        self.assertEqual(len(report["results"]), 2)
        for item in report["results"]:
            self.assertIn("alpha", item)
            self.assertIn("annual_return", item)

    def test_sweep_min_months_structure(self):
        rows = _sample_rows(6)
        series = _series({"000300": [{"date": "2025-%02d-28" % (i + 1), "close": 100.0 + i} for i in range(10)],
                           "000905": [{"date": "2025-%02d-28" % (i + 1), "close": 50.0 + i} for i in range(10)]})
        report = sweep.sweep_min_months(rows, series, grid=(36,))
        self.assertEqual(report["dimension"], "min_train_months")
        self.assertEqual(len(report["results"]), 1)


class AttributionTest(unittest.TestCase):
    def _periods(self):
        return [{"signal_date": "2025-01-31", "next_signal_date": "2025-02-28",
                 "weights": {"ridge": {"000300": 0.6, "000905": 0.4}},
                 "predictions": [
                     {"code": "000300", "name": "000300", "prediction": 0.01,
                      "contributions": {"momentum_1m": 0.005, "volatility_60d": -0.002}},
                     {"code": "000905", "name": "000905", "prediction": -0.01,
                      "contributions": {"momentum_1m": -0.003, "volatility_60d": 0.001}}]}]

    def test_asset_attribution(self):
        # next_point 语义：期初=signal后第一个点，期末=next_signal后第一个点，需要3个数据点
        series = _series({"000300": [{"date": "2025-01-30", "close": 100.0},
                                     {"date": "2025-02-01", "close": 100.0},
                                     {"date": "2025-03-01", "close": 110.0}],
                          "000905": [{"date": "2025-01-30", "close": 50.0},
                                     {"date": "2025-02-01", "close": 50.0},
                                     {"date": "2025-03-01", "close": 49.0}]})
        rows = attribution.asset_attribution(self._periods(), series)
        self.assertEqual(len(rows), 2)
        by_code = {r["code"]: r for r in rows}
        self.assertAlmostEqual(by_code["000300"]["total_contribution"], 0.6 * 0.1, places=6)
        self.assertAlmostEqual(by_code["000905"]["total_contribution"], 0.4 * -0.02, places=6)

    def test_factor_attribution(self):
        rows = attribution.factor_attribution(self._periods())
        names = {r["factor"] for r in rows}
        self.assertIn("momentum_1m", names)
        self.assertIn("volatility_60d", names)
        total = sum(r["weighted_contribution"] for r in rows)
        expected = 0.6 * 0.005 + 0.4 * -0.003 + 0.6 * -0.002 + 0.4 * 0.001
        self.assertAlmostEqual(total, expected, places=6)

    def test_summarize(self):
        series = _series({"000300": [{"date": "2025-01-30", "close": 100.0},
                                     {"date": "2025-02-01", "close": 100.0},
                                     {"date": "2025-03-01", "close": 110.0}],
                          "000905": [{"date": "2025-01-30", "close": 50.0},
                                     {"date": "2025-02-01", "close": 50.0},
                                     {"date": "2025-03-01", "close": 49.0}]})
        payload = attribution.summarize_attribution(self._periods(), series)
        self.assertIn("asset_contribution", payload)
        self.assertIn("factor_contribution", payload)
        self.assertEqual(payload["attribution_periods"], 1)


if __name__ == "__main__":
    unittest.main()
