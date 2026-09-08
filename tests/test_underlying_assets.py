import os
import tempfile
import unittest
from unittest.mock import patch

from valuation_app.underlying_assets import parse_underlying_asset


class UnderlyingAssetTest(unittest.TestCase):
    @patch("valuation_app.underlying_assets._find_date", return_value="2026-08-05")
    @patch("valuation_app.underlying_assets._rows")
    def test_long_exposure_uses_aggregate_initial_contract_rows(self, rows, _date):
        rows.return_value = [
            ["科目代码", "科目名称", "数量", "市价", "市值"],
            ["1102", "交易性股票投资", 1, 1, 1000],
            ["110201001", "甲股票", 1, 1, 1000],
            ["31020301", "股指期货初始合约价值-多头_中金所", 1, 1, 300],
            ["31020301IF2609", "沪深300股指期货2609合约", 1, 1, 300],
            ["31020302", "冲销股指期货初始合约价值-多头_中金所", 1, 1, -300],
            ["31020401", "股指期货初始合约价值-空头_中金所", 1, 1, -700],
            ["31020401IM2609", "中证1000股指期货2609合约", 1, 1, -700],
            ["", "基金资产净值", "", "", 1200],
        ]
        item = parse_underlying_asset(os.path.join(tempfile.gettempdir(), "测试产品_证券投资基金估值表.xls"))
        self.assertEqual(item["stock_market_value"], 1000)
        self.assertEqual(item["index_futures_long"], 300)
        self.assertEqual(item["index_futures_short"], 700)
        self.assertEqual(item["long_exposure"], 600)
        self.assertEqual(item["long_exposure_ratio"], 50)
        self.assertEqual(len(item["evidence"]), 3)

    @patch("valuation_app.underlying_assets._find_date", return_value="2026-08-05")
    @patch("valuation_app.underlying_assets._rows")
    def test_hedging_instrument_3201_rows_are_included(self, rows, _date):
        rows.return_value = [
            ["科目代码", "科目名称", "数量", "市价", "市值"],
            ["1102", "交易性股票投资", 1, 1, 154460459.26],
            ["32010121", "套期工具股指期货初始合约价值-多头_中金所", 27, 1, 39850960],
            ["32010121IM2609", "中证1000股指期货2609合约", 25, 1, 37081000],
            ["32010201", "套期工具股指期货初始合约价值-空头_中金所", 118, 1, -174825800],
            ["32010201IM2609", "中证1000股指期货2609合约", 116, 1, -172055840],
            ["", "基金资产净值", "", "", 184920000],
        ]
        item = parse_underlying_asset(os.path.join(tempfile.gettempdir(), "平方和衡盛29号_证券投资基金估值表.xls"))
        self.assertEqual(item["index_futures_long"], 39850960)
        self.assertEqual(item["index_futures_short"], 174825800)
        self.assertAlmostEqual(item["long_exposure"], 19485619.26)

    @patch("valuation_app.underlying_assets._find_date", return_value="2026-08-05")
    @patch("valuation_app.underlying_assets._rows")
    def test_unknown_index_future_direction_is_warned(self, rows, _date):
        rows.return_value = [
            ["科目代码", "科目名称", "数量", "市价", "市值"],
            ["399999", "股指期货初始合约价值_中金所", 1, 1, 100],
            ["", "基金资产净值", "", "", 1000],
        ]
        item = parse_underlying_asset(os.path.join(tempfile.gettempdir(), "未知模板_证券投资基金估值表.xls"))
        self.assertEqual(len(item["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
