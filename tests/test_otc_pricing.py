import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from valuation_app.otc_pricing import (PricingInputError, black_scholes,
                                       evaluate_release_gates,
                                       load_market_snapshot,
                                       pricing_reference_data,
                                       price_classic_snowball,
                                       run_parametric_pricing,
                                       save_market_snapshot,
                                       validate_parametric_request,
                                       validate_market_snapshot)


def snapshot():
    quotes = []
    spot = 100.0
    for tenor in (.5, 1.0):
        for strike in (80.0, 90.0, 100.0, 110.0, 120.0):
            for call_put in ("C", "P"):
                mid = black_scholes(spot, strike, tenor, .02, .01, .20, call_put == "C")
                quotes.append({"tenor_years": tenor, "strike": strike, "call_put": call_put,
                               "bid": max(0.0, mid - .01), "ask": mid + .01, "implied_vol": .20})
    start = datetime(2026, 9, 24); trading_calendar = []
    for offset in range(800):
        day = start + timedelta(days=offset)
        if day.weekday() < 5:
            trading_calendar.append(day.strftime("%Y-%m-%d"))
    return {"schema_version": 1, "as_of": "2026-09-24", "underlying": "000852", "spot": spot,
            "discount_curve": [{"tenor_years": .25, "zero_rate": .02},
                               {"tenor_years": 2.0, "zero_rate": .02}],
            "forward_curve": [{"tenor_years": .25, "forward": spot * math.exp(.01 * .25)},
                              {"tenor_years": 2.0, "forward": spot * math.exp(.01 * 2)}],
            "option_quotes": quotes,
            "local_vol_grid": {"times": [.01, 2.0], "moneyness": [.5, 1.0, 1.5],
                               "values": [[.20, .20, .20], [.20, .20, .20]]},
            "trading_calendar": trading_calendar,
            "source_hashes": {"options": "a" * 64, "rates": "b" * 64, "futures": "c" * 64}}


class OtcPricingResearchTest(unittest.TestCase):
    def test_black_scholes_regression_and_snapshot_gate(self):
        call = black_scholes(100, 100, 1, .05, 0, .20, True)
        self.assertAlmostEqual(call, 10.4506, places=3)
        checked = validate_market_snapshot(snapshot())
        self.assertEqual(checked["static_arbitrage_violations"], [])
        broken = snapshot(); broken.pop("local_vol_grid")
        with self.assertRaises(PricingInputError):
            validate_market_snapshot(broken)

    def test_reproducible_model_value_and_fair_coupon(self):
        terms = {"term_months": 6, "lock_period_months": 2, "knock_in_ratio": .70,
                 "knock_out_initial": 1.0, "knock_out_decrease_monthly": .005,
                 "first_coupon": .12, "second_coupon": .10, "coupon_switch_months": 3,
                 "max_loss": .8}
        first = price_classic_snowball(snapshot(), terms, path_count=256, seed=7)
        second = price_classic_snowball(snapshot(), terms, path_count=256, seed=7)
        self.assertEqual(first["present_value_ratio"], second["present_value_ratio"])
        self.assertFalse(first["eligible_for_web"])
        self.assertLessEqual(first["confidence_interval_95"][0], first["present_value_ratio"])
        self.assertGreaterEqual(first["confidence_interval_95"][1], first["present_value_ratio"])

    def test_market_snapshot_is_content_addressed_and_tamper_checked(self):
        with tempfile.TemporaryDirectory() as root:
            path, checked = save_market_snapshot(root, snapshot())
            loaded, validation = load_market_snapshot(root, checked["snapshot_sha256"])
            self.assertEqual(loaded["underlying"], "000852")
            self.assertEqual(validation["snapshot_sha256"], checked["snapshot_sha256"])
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            with self.assertRaises(PricingInputError):
                load_market_snapshot(root, checked["snapshot_sha256"])

    def test_release_gate_requires_every_scientific_threshold(self):
        passed = evaluate_release_gates(.009, .0009, .0009, .0004, .0004, [])
        self.assertTrue(passed["passed"])
        failed = evaluate_release_gates(.011, .0009, .0009, .0004, .0004, [])
        self.assertFalse(failed["passed"])

    def test_parametric_pricing_discounts_principal_and_is_reproducible(self):
        request = {"structure": "classic_snowball", "index_code": "000852",
                   "term_months": 6, "lock_period_months": 2,
                   "knock_in_ratio": .7, "knock_out_initial": 1.0,
                   "knock_out_decrease_monthly": .005,
                   "first_coupon": .12, "second_coupon": .10,
                   "coupon_switch_months": 3, "volatility": .20,
                   "basis_rate": .02, "discount_rate": .03}
        first = run_parametric_pricing(request, batch_count=4, paths_per_batch=64, seed=11)
        second = run_parametric_pricing(request, batch_count=4, paths_per_batch=64, seed=11)
        self.assertEqual(first["value_per_100"], second["value_per_100"])
        self.assertEqual(first["batch_count"], 4)
        self.assertEqual(first["path_count"], 256)
        self.assertEqual(len(first["batch_convergence"]), 4)
        self.assertTrue(first["fair_coupon"]["available"])
        self.assertNotIn("sha256", json.dumps(first).lower())

    def test_parametric_request_bounds_and_fixed_structure(self):
        base = {"structure": "classic_snowball", "index_code": "000300"}
        checked = validate_parametric_request(base)
        self.assertEqual(checked["term_months"], 24)
        with self.assertRaises(PricingInputError):
            validate_parametric_request(dict(base, volatility=0))
        with self.assertRaises(PricingInputError):
            validate_parametric_request(dict(base, structure="dcn"))
        with self.assertRaises(PricingInputError):
            validate_parametric_request(dict(base, product_name="../secret"))

    def test_zero_volatility_deterministic_limit_and_principal_cashflow(self):
        request = {"structure": "classic_snowball", "index_code": "000852",
                   "term_months": 6, "lock_period_months": 2,
                   "knock_in_ratio": .7, "knock_out_initial": 1.01,
                   "knock_out_decrease_monthly": 0,
                   "first_coupon": .12, "second_coupon": .12,
                   "coupon_switch_months": 3, "volatility": 0,
                   "basis_rate": 0, "discount_rate": 0}
        result = run_parametric_pricing(request, batch_count=4, paths_per_batch=32,
                                        seed=3, allow_zero_volatility=True)
        self.assertAlmostEqual(result["value_per_100"], 106.0, places=10)
        self.assertEqual(result["standard_error_per_100"], 0)
        self.assertEqual(result["probabilities"]["knock_out"], 0)

    def test_reference_data_has_volatility_basis_and_rate(self):
        with tempfile.TemporaryDirectory() as root:
            index_path = os.path.join(root, "index.json")
            risk_path = os.path.join(root, "risk.json")
            points = [{"date": "2026-01-%02d" % (index + 1), "close": 100 + index}
                      for index in range(21)]
            with open(index_path, "w", encoding="utf-8") as handle:
                json.dump({"indices": {code: {"name": code, "points": points}
                                       for code in ("000852", "000905", "000300")}}, handle)
            futures = {prefix: {"points": [{"date": "2026-01-21",
                                             "当月": {"value": 3.0}}]}
                       for prefix in ("IM", "IC", "IF")}
            with open(risk_path, "w", encoding="utf-8") as handle:
                json.dump({"futures": futures, "yield_curves": {"CN10Y": {
                    "points": [{"date": "2026-01-21", "yield": .02}]}}}, handle)
            result = pricing_reference_data(index_path, risk_path)
            self.assertIsNotNone(result["indices"][0]["volatility"]["20"])
            self.assertAlmostEqual(result["indices"][0]["basis"][0]["latest"], .03)
            self.assertEqual(result["discount_rate"]["value"], .02)


if __name__ == "__main__":
    unittest.main()
