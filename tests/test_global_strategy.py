import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


def helper():
    return "\n".join(line for line in HTML.splitlines()
                     if line.startswith("function globalStrategySummary"))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript tests")
class GlobalStrategyTest(unittest.TestCase):
    def test_duplicate_underlying_positions_are_summed_not_deduplicated(self):
        source = """
const report=%s;
function holdingLabel(h){return {primary:h.tag}}
function holdingMetric(p,h){return {period:h.rate,label:holdingLabel(h)}}
%s
console.log(JSON.stringify(globalStrategySummary()));
""" % (json.dumps([
            {"status": "ok", "first": {"net_assets": 1000}, "holdings": [
                {"name": "同名底仓", "tag": "股票指增", "market_value": 100, "period_profit": 10, "rate": 8}]},
            {"status": "ok", "first": {"net_assets": 2000}, "holdings": [
                {"name": "同名底仓", "tag": "股票指增", "market_value": 200, "period_profit": -5, "rate": -2}]},
        ], ensure_ascii=False), helper())
        result = json.loads(subprocess.check_output(["node", "-e", source]).decode("utf-8"))
        self.assertEqual(result["items"][0]["market"], 300)
        self.assertEqual(result["items"][0]["profit"], 5)
        self.assertEqual(result["items"][0]["count"], 2)
        self.assertEqual([row["rate"] for row in result["items"][0]["holdings"]], [8, -2])
        self.assertAlmostEqual(result["items"][0]["absolute_return"], (100 * 8 + 200 * -2) / 300)


if __name__ == "__main__":
    unittest.main()
