import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


class HoldingTradingScaleJavascriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable for JavaScript chart tests")
        lines = HTML.splitlines()
        cls.time_source = next(line for line in lines if line.startswith("function timeScale("))
        cls.trading_source = next(line for line in lines if line.startswith("function holdingTradingScale("))

    def run_scale(self, points, calendar):
        raw = {"benchmarks": {"indices": {"000300": {"points": [
            {"date": date, "close": 1} for date in calendar
        ]}}}}
        script = (
            "const DAY=86400000;const RAW=" + json.dumps(raw) + ";"
            + self.time_source + self.trading_source
            + "const s=holdingTradingScale(" + json.dumps(points) + ",0,300);"
            + "console.log(JSON.stringify({fallback:s.fallback,"
              "positions:s.points.map(p=>s.position(p.valuation_date)),"
              "xs:s.points.map(p=>s.x(p.valuation_date))}));"
        )
        completed = subprocess.run(
            ["node", "-e", script], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        return json.loads(completed.stdout)

    def test_weekend_is_compressed_but_missing_trading_days_remain(self):
        calendar = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26"]
        points = [
            {"valuation_date": "2026-08-21"},
            {"valuation_date": "2026-08-24"},
            {"valuation_date": "2026-08-26"},
        ]
        result = self.run_scale(points, calendar)
        self.assertFalse(result["fallback"])
        self.assertEqual(result["positions"], [0, 1, 3])
        self.assertEqual(result["xs"], [0, 100, 300])

    def test_incomplete_calendar_falls_back_to_real_dates(self):
        result = self.run_scale(
            [{"valuation_date": "2026-08-21"}, {"valuation_date": "2026-08-26"}],
            ["2026-08-21", "2026-08-24", "2026-08-25"],
        )
        self.assertTrue(result["fallback"])


if __name__ == "__main__":
    unittest.main()
