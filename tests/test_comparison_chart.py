import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


def comparison_source():
    wanted = ("function comparisonSeries",)
    return "\n".join(line for line in HTML.splitlines() if line.startswith(wanted))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for comparison chart tests")
class ComparisonChartJavascriptTest(unittest.TestCase):
    def evaluate(self, selected):
        raw = {
            "products": [
                {"name": "当前产品", "points": [
                    {"valuation_date": "2026-01-02", "accumulated_nav": 1.0},
                    {"valuation_date": "2026-01-05", "accumulated_nav": 1.1},
                ]},
                {"name": "其他产品", "points": [
                    {"valuation_date": "2026-01-02", "accumulated_nav": 2.0},
                    {"valuation_date": "2026-01-05", "accumulated_nav": 1.8},
                ]},
            ],
            "benchmarks": {"indices": {
                code: {"name": name, "points": [
                    {"date": "2025-12-31", "close": 990},
                    {"date": "2026-01-02", "close": 1000},
                    {"date": "2026-01-05", "close": 1010},
                ]} for code, name in (("000852", "中证1000"),
                                      ("000905", "中证500"),
                                      ("000300", "沪深300"))
            }},
        }
        script = """
const RAW=%s;
function benchmarkIndex(code){return RAW.benchmarks.indices[code]}
function marketBefore(points,date){let hit=null;points.forEach(p=>{if(p.date<=date)hit=p});return hit}
window={comparisonState:{current:0,start:'2026-01-01',end:'2026-01-06',selected:new Set(%s)}};
%s
console.log(JSON.stringify(comparisonSeries('2026-01-01','2026-01-06')));
""" % (json.dumps(raw, ensure_ascii=False), json.dumps(selected), comparison_source())
        output = subprocess.check_output(["node", "-e", script])
        return json.loads(output.decode("utf-8"))

    def test_default_and_optional_product_selection(self):
        defaults = ["product:0", "index:000852", "index:000905", "index:000300"]
        series = self.evaluate(defaults)
        self.assertEqual(len(series), 4)
        self.assertEqual([item["name"] for item in series],
                         ["当前产品", "中证1000", "中证500", "沪深300"])
        self.assertTrue(all(item["points"][0]["value"] == 100 for item in series))

        with_other = self.evaluate(defaults + ["product:1"])
        self.assertEqual(len(with_other), 5)
        self.assertIn("其他产品", [item["name"] for item in with_other])

        without_index = self.evaluate(["product:0", "product:1", "index:000852",
                                       "index:000905"])
        self.assertEqual(len(without_index), 4)
        self.assertNotIn("沪深300", [item["name"] for item in without_index])


if __name__ == "__main__":
    unittest.main()
