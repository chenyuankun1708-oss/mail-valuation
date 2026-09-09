import json
import math
import os
import tempfile
import unittest
from datetime import date, timedelta

from strategy_lab.features import attach_targets, feature_rows, month_ends
from strategy_lab.model import fit_ridge, predict
from strategy_lab.pipeline import run
from strategy_lab.portfolio import limit_turnover, target_weights, validate


def trading_points(start=date(2019, 1, 1), count=1300, drift=.0002):
    result, value, current = [], 100.0, start
    while len(result) < count:
        if current.weekday() < 5:
            value *= 1 + drift + .001 * math.sin(len(result) / 13.0)
            result.append({"date": current.isoformat(), "close": value})
        current += timedelta(days=1)
    return result


class StrategyLabTest(unittest.TestCase):
    def series(self):
        return {code: {"name": code, "points": trading_points(drift=.0001 + index * .00003)}
                for index, code in enumerate(("000300", "000905", "000852", "000510", "000922", "801010"))}

    def test_features_use_publication_date_and_ridge_is_reproducible(self):
        series = self.series()
        research = {"macro": {"data": {"PMI": [
            {"info_date": "2020-01-10", "value": 49},
            {"info_date": "2030-01-10", "value": 99},
        ]}}, "style_factor_returns": {"data": {}}, "option_surfaces": {"data": {}}}
        rows = attach_targets(feature_rows(series, research), series)
        training = [row for row in rows if row.get("target_excess_return") is not None][-100:]
        model1, model2 = fit_ridge(training), fit_ridge(training)
        self.assertEqual(model1, model2)
        value1, contributions1 = predict(model1, training[-1])
        value2, contributions2 = predict(model2, training[-1])
        self.assertEqual(value1, value2)
        self.assertEqual(contributions1, contributions2)
        self.assertTrue(all(row["available_at"][:10] == row["date"] for row in rows))

    def test_incomplete_current_month_is_not_a_signal(self):
        points = [{"date": "2026-08-31", "close": 100},
                  {"date": "2026-09-07", "close": 101}]
        self.assertEqual(month_ends(points, as_of="2026-09-08"), ["2026-08-31"])
        self.assertEqual(month_ends(points, as_of="2026-09-30"), ["2026-08-31", "2026-09-07"])

    def test_portfolio_limits_and_turnover(self):
        rows = [{"code": code, "features": {"volatility_60d": .10 + index * .01}}
                for index, code in enumerate(("000300", "000905", "000852", "000510", "000922", "801010", "801020", "801030"))]
        scores = {row["code"]: {"score": 1.0 - index * .05} for index, row in enumerate(rows)}
        target = target_weights(rows, scores)
        self.assertTrue(all(validate(target).values()))
        previous = {"000300": .25, "000905": .25, "000852": .25, "000510": .25}
        limited, turnover = limit_turnover(previous, target)
        self.assertLessEqual(turnover, .30)
        self.assertAlmostEqual(sum(limited.values()), 1.0)

    def test_pipeline_writes_versioned_isolated_results(self):
        with tempfile.TemporaryDirectory() as root:
            market = os.path.join(root, "market_data")
            os.makedirs(market)
            series = self.series()
            with open(os.path.join(market, "index_daily.json"), "w", encoding="utf-8") as handle:
                json.dump({"updated_at": "2026-01-01", "indices": {code: value for code, value in series.items() if not code.startswith("801")}}, handle)
            research_series = {"801010.INDX": series["801010"]}
            with open(os.path.join(market, "market_research.json"), "w", encoding="utf-8") as handle:
                json.dump({"updated_at": "2026-01-01", "industry_indices": {"data": research_series},
                           "macro": {"data": {}}, "style_factor_returns": {"data": {}},
                           "option_surfaces": {"data": {}}, "etf_mapping": {"data": {}}}, handle)
            result = run(root, "lab")
            self.assertEqual(result["schema_version"], 1)
            self.assertTrue(result["research_only"])
            self.assertEqual(result["latest_signal_date"], result["latest_model"]["signal_date"])
            self.assertTrue(result["validation"]["no_lookahead"])
            self.assertTrue(result["validation"]["portfolio_constraints"])
            self.assertGreater(result["validation"]["out_of_sample_periods"], 0)
            self.assertIn("annual_return_improvement", result["model_comparison"])
            for filename in ("raw_snapshot.json", "clean_snapshot.json", "factor_snapshot.json", "result.json"):
                self.assertTrue(os.path.isfile(os.path.join(root, "lab", filename)))
            self.assertNotIn("orders", result)


if __name__ == "__main__":
    unittest.main()
