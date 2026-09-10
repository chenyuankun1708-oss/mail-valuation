import json
import shutil
import subprocess
import unittest

from valuation_app.static import HTML


def helper(*names):
    prefixes = tuple("function %s" % name for name in names)
    return "\n".join(line for line in HTML.splitlines()
                     if line.startswith(prefixes))


JS_METRIC = """
function holdingLabel(h){return h._label||h.label||{primary:'其他',secondary:'其他',vehicle:'其他',department:'无',match_status:'其他'}}
function holdingMetric(p,h){
  const series=(p.points||[]).map(point=>{
    const row=(point.holdings||[]).find(x=>x.code===h.code);
    return row&&Number(row.price)>0?row:null;
  }).filter(Boolean),first=series[0],last=series[series.length-1];
  return {period:first&&last?(last.price/first.price-1)*100:null,label:holdingLabel(h)};
}
"""


def point(date, holdings):
    return {"valuation_date": date, "holdings": holdings}


def holding(code, name, quantity, price, label):
    return {
        "code": code,
        "name": name,
        "quantity": quantity,
        "price": price,
        "market_value": quantity * price,
        "cost": quantity,
        "valuation_gain": quantity * (price - 1),
        "label": label,
    }


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript tests")
class GlobalStrategyTest(unittest.TestCase):
    def run_summary(self, products, filters="{}"):
        source = """
const input=%s;
%s
%s
const report=input.map(p=>Object.assign({},p,{last:p.points[p.points.length-1]}));
console.log(JSON.stringify(globalStrategySummary(%s)));
""" % (
            json.dumps(products, ensure_ascii=False),
            JS_METRIC,
            helper("holdingReport", "strategyProductRows", "globalStrategySummary"),
            filters,
        )
        return json.loads(subprocess.check_output(["node", "-e", source]).decode("utf-8"))

    def test_historical_codes_are_merged_by_product_name_within_each_fof(self):
        label = {
            "product": "标准产品名称",
            "primary": "股票指增",
            "secondary": "中证1000",
            "vehicle": "集合",
            "department": "上海虹桥路",
            "match_status": "已匹配",
        }
        products = [
            {
                "status": "ok",
                "name": "FOF甲",
                "inception": False,
                "points": [
                    point("2026-06-30", [holding("旧代码", "产品简称", 100, 1, label)]),
                    point("2026-09-09", [holding("新代码", "产品全称", 100, 1.2, label)]),
                ],
            },
            {
                "status": "ok",
                "name": "FOF乙",
                "inception": False,
                "points": [
                    point("2026-06-30", [holding("另一代码", "另一个简称", 200, 1, label)]),
                    point("2026-09-09", [holding("另一代码", "另一个简称", 200, 0.975, label)]),
                ],
            },
        ]
        result = self.run_summary(products)
        item = result["items"][0]
        self.assertEqual(item["market"], 315)
        self.assertAlmostEqual(item["profit"], 15)
        self.assertEqual(item["count"], 2)
        self.assertEqual([row["name"] for row in item["holdings"]],
                         ["标准产品名称", "标准产品名称"])
        self.assertEqual(item["holdings"][0]["code"], "旧代码 / 新代码")
        self.assertEqual(result["holdings"], 2)
        self.assertAlmostEqual(item["absolute_return"],
                               (120 * 20 + 195 * -2.5) / 315)

    def test_department_filter_recalculates_metrics_from_selected_products(self):
        east = {"product": "底仓甲", "primary": "CTA", "secondary": "全品种",
                "vehicle": "集合", "department": "华东营业部", "match_status": "已匹配"}
        south = {"product": "底仓乙", "primary": "股票指增", "secondary": "中证1000",
                 "vehicle": "专户", "department": "华南营业部", "match_status": "已匹配"}
        products = [{
            "status": "ok",
            "name": "FOF甲",
            "inception": False,
            "points": [
                point("2026-06-30", [holding("A", "底仓甲", 100, 1, east),
                                      holding("B", "底仓乙", 900, 1, south)]),
                point("2026-09-09", [holding("A", "底仓甲", 100, 1.06, east),
                                      holding("B", "底仓乙", 900, 1.10, south)]),
            ],
        }]
        result = self.run_summary(products, "{department:'华东营业部'}")
        self.assertEqual(result["products"], 1)
        self.assertEqual(result["holdings"], 1)
        self.assertEqual(result["items"][0]["market"], 106)
        self.assertAlmostEqual(result["items"][0]["profit"], 6)
        self.assertAlmostEqual(result["items"][0]["absolute_return"], 6)


if __name__ == "__main__":
    unittest.main()
