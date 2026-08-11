import json
import math
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


class RiskMetricsJavascriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable for JavaScript formula tests")
        cls.risk_source = next(
            line for line in HTML.splitlines()
            if line.startswith("const RISK_FREE_RATE=")
        )

    def calculate(self, cases):
        script = (
            "const DAY=86400000;"
            "function ds(a,b){return Math.round((new Date(b+'T00:00:00')-"
            "new Date(a+'T00:00:00'))/DAY)}\n" + self.risk_source + "\n"
            "const cases=" + json.dumps(cases) + ";"
            "console.log(JSON.stringify(cases.map(riskMetrics)));"
        )
        completed = subprocess.run(
            ["node", "-e", script], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        return json.loads(completed.stdout)

    def test_growth_drawdown_and_observation_boundaries(self):
        results = self.calculate([
            [],
            [{"valuation_date": "2026-01-01", "accumulated_nav": 1.0}],
            [
                {"valuation_date": "2026-01-01", "accumulated_nav": 1.0},
                {"valuation_date": "2026-01-11", "accumulated_nav": 0.9},
            ],
            [
                {"valuation_date": "2026-01-01", "accumulated_nav": 1.0},
                {"valuation_date": "2026-01-11", "accumulated_nav": 1.1},
                {"valuation_date": "2026-01-21", "accumulated_nav": 1.21},
            ],
            [
                {"valuation_date": "2026-01-01", "accumulated_nav": 1.0},
                {"valuation_date": "2026-01-11", "accumulated_nav": 0.9},
                {"valuation_date": "2026-01-21", "accumulated_nav": 0.8},
            ],
        ])
        self.assertEqual(results[0]["valid_points"], 0)
        self.assertIsNone(results[0]["nav_annualized"])
        self.assertEqual(results[1]["valid_points"], 1)
        self.assertIsNone(results[1]["max_drawdown"])

        two_points = results[2]
        self.assertAlmostEqual(two_points["nav_annualized"],
                               (0.9 ** (365.0 / 10) - 1) * 100, places=9)
        self.assertAlmostEqual(two_points["max_drawdown"], -10.0, places=9)
        self.assertEqual(two_points["drawdown_peak"], "2026-01-01")
        self.assertEqual(two_points["drawdown_trough"], "2026-01-11")
        self.assertIsNone(two_points["annual_volatility"])
        self.assertIsNone(two_points["sharpe"])

        steady_growth = results[3]
        self.assertAlmostEqual(steady_growth["max_drawdown"], 0.0, places=9)
        self.assertAlmostEqual(steady_growth["annual_volatility"], 0.0, places=9)
        self.assertIsNone(steady_growth["sharpe"])

        falling = results[4]
        self.assertAlmostEqual(falling["max_drawdown"], -20.0, places=9)
        self.assertEqual(falling["drawdown_peak"], "2026-01-01")
        self.assertEqual(falling["drawdown_trough"], "2026-01-21")

    def test_irregular_dates_weight_volatility_and_sharpe(self):
        points = [
            {"valuation_date": "2026-01-01", "accumulated_nav": 1.0},
            {"valuation_date": "2026-01-03", "accumulated_nav": 1.02},
            {"valuation_date": "2026-01-10", "accumulated_nav": 1.01},
        ]
        result = self.calculate([points])[0]
        first_rate = math.log(1.02) / 2
        second_rate = math.log(1.01 / 1.02) / 7
        mean = (first_rate * 2 + second_rate * 7) / 9
        variance = (2 * (first_rate - mean) ** 2 +
                    7 * (second_rate - mean) ** 2) / 8
        volatility = math.sqrt(variance * 365)
        sharpe = (mean * 365 - math.log(1.02)) / volatility
        self.assertAlmostEqual(result["annual_volatility"], volatility * 100, places=9)
        self.assertAlmostEqual(result["sharpe"], sharpe, places=9)
        self.assertTrue(result["short_period"])
        self.assertEqual(result["valid_points"], 3)
        self.assertEqual(result["total_days"], 9)

    def test_invalid_nav_is_excluded_and_accumulated_nav_drives_metrics(self):
        result = self.calculate([[
            {"valuation_date": "2026-01-01", "nav": 1.0, "accumulated_nav": 1.0},
            {"valuation_date": "2026-01-02", "nav": 0.8, "accumulated_nav": 1.02},
            {"valuation_date": "2026-01-03", "nav": 0.81, "accumulated_nav": 1.03},
            {"valuation_date": "2026-01-04", "nav": 1.0, "accumulated_nav": 0},
        ]])[0]
        self.assertEqual(result["valid_points"], 3)
        self.assertGreater(result["nav_annualized"], 0)
        self.assertAlmostEqual(result["max_drawdown"], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
