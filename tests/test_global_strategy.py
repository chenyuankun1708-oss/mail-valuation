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

    def test_department_filter_recalculates_metrics_from_selected_holdings(self):
        source = """
const report=%s;
function holdingLabel(h){return {primary:h.tag,secondary:h.secondary,vehicle:h.vehicle,department:h.department}}
function holdingMetric(p,h){return {period:h.rate,label:holdingLabel(h)}}
%s
console.log(JSON.stringify(globalStrategySummary({department:'华东营业部'})));
""" % (json.dumps([
            {"status": "ok", "name": "FOF甲", "holdings": [
                {"name": "底仓甲", "tag": "CTA", "secondary": "全品种", "vehicle": "集合",
                 "department": "华东营业部", "market_value": 100, "period_profit": 8, "rate": 6},
                {"name": "底仓乙", "tag": "股票指增", "secondary": "中证1000", "vehicle": "专户",
                 "department": "华南营业部", "market_value": 900, "period_profit": 90, "rate": 10}]},
        ], ensure_ascii=False), helper())
        result = json.loads(subprocess.check_output(["node", "-e", source]).decode("utf-8"))
        self.assertEqual(result["products"], 1)
        self.assertEqual(result["holdings"], 1)
        self.assertEqual(result["items"][0]["market"], 100)
        self.assertEqual(result["items"][0]["profit"], 8)
        self.assertEqual(result["items"][0]["absolute_return"], 6)


if __name__ == "__main__":
    unittest.main()
