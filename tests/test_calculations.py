import json
import math
import os
import subprocess
import unittest

from valuation_app.calculations import alpha_beta, benchmark_metrics, holding_attribution, risk_metrics, xirr


ROOT = os.path.dirname(os.path.dirname(__file__))


def assert_close(test, left, right, tolerance=1e-8):
    if left is None or right is None or isinstance(left, (str, bool)):
        test.assertEqual(left, right)
    else:
        test.assertLessEqual(abs(left - right), tolerance)


class CalculationGoldenParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web_assets", "core.js"), "r", encoding="utf-8") as handle:
            source = handle.read()
        start = source.index("function xnpv")
        end = source.index("function renderBenchmarks")
        cls.javascript = "const DAY=86400000;function ds(a,b){return(new Date(b)-new Date(a))/DAY}" + source[start:end]

    def javascript_result(self, expression):
        program = self.javascript + "\nconsole.log(JSON.stringify(" + expression + "))"
        completed = subprocess.run(["node", "-e", program], cwd=ROOT, check=True,
                                   capture_output=True, text=True)
        return json.loads(completed.stdout)

    def test_xirr_matches_javascript_amount_tolerance(self):
        flows = [{"date": "2025-01-01", "value": -1000000},
                 {"date": "2025-07-01", "value": 150000},
                 {"date": "2026-01-01", "value": 940000}]
        assert_close(self, xirr(flows), self.javascript_result("xirr(%s)" % json.dumps(flows)))

    def test_risk_benchmark_and_alpha_beta_match_javascript(self):
        products = []
        market = []
        for index in range(45):
            date = "2026-01-%02d" % (index + 1) if index < 31 else "2026-02-%02d" % (index - 30)
            products.append({"valuation_date": date, "accumulated_nav": 1 + index * .001 + math.sin(index) * .0002})
            market.append({"date": date, "close": 1000 + index * 2 + math.cos(index)})
        js_risk = self.javascript_result("riskMetrics(%s)" % json.dumps(products))
        py_risk = risk_metrics(products)
        for key in js_risk:
            assert_close(self, py_risk[key], js_risk[key])
        item = {"points": market}
        js_benchmark = self.javascript_result(
            "benchmarkMetrics(%s,'2026-01-01','2026-02-14')" % json.dumps(item))
        py_benchmark = benchmark_metrics(item, "2026-01-01", "2026-02-14")
        for key in js_benchmark:
            if key not in ("first", "last"):
                assert_close(self, py_benchmark[key], js_benchmark[key])
        js_alpha = self.javascript_result("alphaBeta(%s,%s)" %
                                          (json.dumps(products), json.dumps(market)))
        py_alpha = alpha_beta(products, market)
        for key in js_alpha:
            assert_close(self, py_alpha[key], js_alpha[key])

    def test_holding_attribution_keeps_amounts_dates_and_status(self):
        points = [{"holdings": [{"code": "A", "name": "A", "quantity": 10,
                                  "price": 2, "market_value": 20, "cost": 18,
                                  "valuation_gain": 2}]},
                  {"holdings": [{"code": "A", "name": "A", "quantity": 12,
                                  "price": 3, "market_value": 36, "cost": 30,
                                  "valuation_gain": 6}]}]
        result = holding_attribution(points)
        self.assertEqual(10, result[0]["period_profit"])
        self.assertEqual("持仓变化，按可比份额估算", result[0]["status"])


if __name__ == "__main__":
    unittest.main()
