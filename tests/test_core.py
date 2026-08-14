import os
import tempfile
import unittest

import xlrd

from valuation_app.analytics import analyze
from valuation_app.config import PRODUCTS, match_product
from valuation_app.parser import Snapshot, _find_date, _metric_nearest, _parse_holdings, parse_valuation, scan_valuations


class CoreTest(unittest.TestCase):
    def test_valuation_date_does_not_truncate_november_or_december(self):
        self.assertEqual(_find_date([["估值日期：2025-12-30"]], "mail_2025-01-02.xls"), "2025-12-30")
        self.assertEqual(_find_date([["估值日期：2025-11-09"]], "mail.xls"), "2025-11-09")
        self.assertEqual(_find_date([], "估值表_20251230.xls"), "2025-12-30")

    def test_exact_product_scope(self):
        self.assertEqual(len(PRODUCTS), 17)
        self.assertEqual(match_product("(SALT58)JGFA天玑13号_证券投资基金估值表"), "第一创业天玑13号单一资产管理计划")
        self.assertEqual(match_product("证券投资基金估值表_五矿证券FOF50号单一资产管理计划_2026-08-07.xls"),
                         "五矿证券FOF50号单一资产管理计划")
        self.assertEqual(match_product("证券投资基金估值表_五矿证券FOF51号单一资产管理计划_2026-08-07.xls"),
                         "五矿证券FOF51号单一资产管理计划")
        self.assertIsNone(match_product("华银元鼎月利三号估值表"))

    def test_parse_existing_sample(self):
        candidates = [name for name in os.listdir("products") if "天玑13" in name and name.endswith(".xls")]
        if not candidates:
            self.skipTest("workspace has no sample valuation")
        item = parse_valuation(os.path.join("products", sorted(candidates)[0]))
        self.assertEqual(item.product, "第一创业天玑13号单一资产管理计划")
        self.assertGreater(item.net_assets, 1000000)
        self.assertGreater(item.shares, 1000000)
        self.assertGreater(item.nav, 0)

    def test_cashflow_adjusted_profit(self):
        name = next(iter(PRODUCTS))
        rows = [
            Snapshot(name, "2026-01-01", 1.0, 1.0, 100.0, 100.0, "a.xls"),
            Snapshot(name, "2026-02-01", 1.1, 1.1, 165.0, 150.0, "b.xls"),
            Snapshot(name, "2026-03-01", 1.2, 1.2, 120.0, 100.0, "c.xls"),
        ]
        product = analyze(rows)["products"][0]
        self.assertEqual([e["type"] for e in product["events"]], ["追加申购", "部分赎回"])
        self.assertAlmostEqual(product["subscriptions"], 55.0)
        self.assertAlmostEqual(product["withdrawals_and_dividends"], 60.0)
        self.assertAlmostEqual(product["profit"], 25.0)
        self.assertAlmostEqual(product["time_weighted_return_pct"], 20.0)

    def test_multicolumn_net_assets_uses_market_value_nearest_nav(self):
        rows = [["基金资产净值", None, None, None, 51948971.80, 105.17,
                 None, 49395532.71, 100.0, -2553439.09]]
        self.assertEqual(_metric_nearest(rows, ("基金资产净值",), 50000000 * 0.9879), 49395532.71)

    def test_parse_underlying_holding_row(self):
        rows = [
            ["科目代码", "科目名称", "数量", "单位成本", "成本", "成本占净值%", "市价", "市值", "市值占净值%", "估值增值"],
            ["11090601ABC", "示例私募基金", 100.0, 1.0, 100.0, 1.0, 1.1, 110.0, 1.1, 10.0],
            ["110906", "私募理财产品", 100.0, None, 100.0, 1.0, None, 110.0, 1.1, 10.0],
        ]
        self.assertEqual(_parse_holdings(rows), [{
            "code": "11090601ABC", "name": "示例私募基金", "quantity": 100.0,
            "unit_cost": 1.0, "cost": 100.0, "price": 1.1,
            "market_value": 110.0, "valuation_gain": 10.0,
        }])

    def test_parse_estimated_price_header(self):
        rows = [
            [" 科目代码 ", "科目名称", " 数量 ", "单位成本 ", "成 本", "成本占净值比(%) ", "估值价格 ", "市 值", "市值占净值比(%) ", "估值增值"],
            ["11090601XYZ", "中信模板产品", 200.0, 1.0, 200.0, "1%", 0.9, 180.0, "1%", -20.0],
        ]
        self.assertEqual(_parse_holdings(rows)[0]["price"], 0.9)

    def test_scan_ignores_unrelated_excel(self):
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "unrelated.xls"), "wb").close()
            snapshots, errors = scan_valuations(folder)
            self.assertEqual(snapshots, [])
            # An invalid unrelated workbook is a parse error because it cannot be inspected.
            self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
