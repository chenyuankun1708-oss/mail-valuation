import json
import math
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


def javascript_helpers():
    wanted = ("function marketBefore", "function benchmarkMetrics", "function alphaBeta")
    return "\n".join(line for line in HTML.splitlines() if line.startswith(wanted))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript formula tests")
class BenchmarkMetricTest(unittest.TestCase):
    def evaluate(self, expression):
        source = """
const DAY=86400000;
const RISK_FREE_RATE=.02;
function ds(a,b){return(Date.parse(b)-Date.parse(a))/DAY}
%s
console.log(JSON.stringify(%s));
""" % (javascript_helpers(), expression)
        output = subprocess.check_output(
            ["node", "-e", source], universal_newlines=True
        )
        return json.loads(output)

    @staticmethod
    def regression_series(multiplier=1.0):
        benchmark, product = [], []
        close, nav = 1000.0, 1.0
        for index in range(12):
            day = 1 + index * 4
            date = "2026-01-%02d" % day if day <= 31 else "2026-02-%02d" % (day - 31)
            if index:
                log_return = (0.012 if index % 2 else -0.006)
                close *= math.exp(log_return)
                nav *= math.exp(log_return * multiplier)
            benchmark.append({"date": date, "close": close})
            product.append({"valuation_date": date, "accumulated_nav": nav})
        return product, benchmark

    def test_weekend_boundaries_and_drawdown(self):
        item = {"points": [
            {"date": "2026-01-02", "close": 100.0},
            {"date": "2026-01-05", "close": 120.0},
            {"date": "2026-01-06", "close": 90.0},
        ]}
        result = self.evaluate("benchmarkMetrics(%s,'2026-01-03','2026-01-06')" % json.dumps(item))
        self.assertEqual(result["first"]["date"], "2026-01-02")
        self.assertEqual(result["last"]["date"], "2026-01-06")
        self.assertAlmostEqual(result["period"], -10.0)
        self.assertAlmostEqual(result["max_drawdown"], -25.0)
        self.assertEqual(result["drawdown_peak"], "2026-01-05")
        self.assertEqual(result["drawdown_trough"], "2026-01-06")

    def test_annual_volatility_requires_twenty_returns(self):
        short = {"points": [{"date": "2026-01-%02d" % (i + 1), "close": 100 + i}
                            for i in range(20)]}
        result = self.evaluate("benchmarkMetrics(%s,'2026-01-01','2026-01-20')" % json.dumps(short))
        self.assertIsNone(result["annual_volatility"])
        long = {"points": [{"date": "2026-01-%02d" % (i + 1),
                            "close": 100 + i + (1 if i % 2 else 0)} for i in range(21)]}
        result = self.evaluate("benchmarkMetrics(%s,'2026-01-01','2026-01-21')" % json.dumps(long))
        self.assertIsNotNone(result["annual_volatility"])

    def test_copying_benchmark_has_beta_one_and_zero_alpha(self):
        product, benchmark = self.regression_series()
        result = self.evaluate("alphaBeta(%s,%s)" % (json.dumps(product), json.dumps(benchmark)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["intervals"], 11)
        self.assertAlmostEqual(result["beta"], 1.0, places=10)
        self.assertAlmostEqual(result["alpha"], 0.0, places=10)

    def test_double_benchmark_has_beta_two(self):
        product, benchmark = self.regression_series(2.0)
        result = self.evaluate("alphaBeta(%s,%s)" % (json.dumps(product), json.dumps(benchmark)))
        self.assertAlmostEqual(result["beta"], 2.0, places=10)

    def test_sample_and_variance_boundaries(self):
        product, benchmark = self.regression_series()
        short = self.evaluate("alphaBeta(%s,%s)" % (json.dumps(product[:9]), json.dumps(benchmark[:9])))
        self.assertIsNone(short["alpha"])
        self.assertIsNone(short["beta"])
        flat = [{"date": point["date"], "close": 1000.0} for point in benchmark]
        zero_variance = self.evaluate("alphaBeta(%s,%s)" % (json.dumps(product), json.dumps(flat)))
        self.assertIsNone(zero_variance["alpha"])
        self.assertIsNone(zero_variance["beta"])


if __name__ == "__main__":
    unittest.main()
