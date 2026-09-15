import json
import os
import tempfile
import unittest

import pandas as pd

from valuation_app.otc_backtest import Terms, _single, run_backtest, validate_request


class OtcBacktestTest(unittest.TestCase):
    def test_request_uses_fixed_structure_index_and_parameter_bounds(self):
        request = validate_request({
            "structure": "classic_snowball", "index_code": "000852",
            "start_date": "2024-01-01", "end_date": "2026-01-01",
        })
        self.assertEqual(request["term_months"], 24)
        with self.assertRaises(ValueError):
            validate_request(dict(request, index_code="000016"))
        with self.assertRaises(ValueError):
            validate_request(dict(request, path="../secret"))
        with self.assertRaises(ValueError):
            validate_request(dict(request, term_months=61))

    def test_month_end_observation_is_derived_from_entry_and_rolled_forward(self):
        dates = pd.bdate_range("2024-01-31", "2024-05-03")
        frame = pd.DataFrame({"date": dates, "price": 90.0})
        frame.loc[frame["date"] == pd.Timestamp("2024-01-31"), "price"] = 100.0
        frame.loc[frame["date"] == pd.Timestamp("2024-04-30"), "price"] = 101.0
        terms = Terms(3, 1, .68, 1.0, 0, .12, .08, 2, None, .8, .01)
        row = _single(frame, 0, terms, "classic_snowball")
        self.assertEqual(row["exit_date"], "2024-04-30")
        self.assertEqual(row["exit_observation_month"], 3)
        self.assertEqual(row["holding_calendar_days"], 90)
        self.assertAlmostEqual(row["annualized_return"], .08)

    def test_barrier_equality_is_not_knock_out(self):
        dates = pd.bdate_range("2024-01-02", "2024-03-04")
        frame = pd.DataFrame({"date": dates, "price": 100.0})
        frame.loc[frame["date"] >= pd.Timestamp("2024-03-04"), "price"] = 101.0
        terms = Terms(2, 1, .7, 1.0, 0, .12, .12, 1, None, .8, .01)
        row = _single(frame, 0, terms, "classic_snowball")
        self.assertEqual(row["exit_observation_month"], 2)

    def test_local_wind_cache_only_and_combo_weights_are_not_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "index.json")
            points = []
            for day in pd.bdate_range("2024-01-02", "2024-07-05"):
                points.append({"date": day.strftime("%Y-%m-%d"), "close": 100 + len(points) * .1})
            payload = {"source": "Wind Oracle数据库", "updated_at": "2024-07-05",
                       "indices": {"000852": {"points": points, "updated_at": "2024-07-05"}}}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            request = {"structure": "dcn_snowball_combo", "index_code": "000852",
                       "start_date": "2024-01-02", "end_date": "2024-07-05",
                       "term_months": 3, "lock_period_months": 1,
                       "dcn_weight": 1, "snowball_weight": .2}
            result = run_backtest(request, path)
            row = next(item for item in result["samples"] if not item["still_running"])
            expected = row["dcn_absolute_return"] + .2 * row["snowball_absolute_return"]
            self.assertAlmostEqual(row["absolute_return"], expected)
            payload["source"] = "simulated"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaises(ValueError):
                run_backtest(request, path)


if __name__ == "__main__":
    unittest.main()
