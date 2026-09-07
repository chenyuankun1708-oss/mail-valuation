import os
import tempfile
import unittest

import openpyxl

from valuation_app.portfolio_var import (_credit_bp, _duration, calculate, load_carbon_prices)


class PortfolioVarTest(unittest.TestCase):
    def test_carbon_loader_accepts_trade_price_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "碳排放价格.xlsx")
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.title = "重庆碳市场"
            sheet.append(["日期", "成交量(吨)", "成交价(元/吨)"])
            sheet.append(["2026-08-20", 100, 50])
            sheet.append(["2026-08-21", 120, 45])
            book.save(path)
            book.close()
            carbon = load_carbon_prices(folder)
        self.assertEqual(len(carbon["series"]["重庆"]), 2)
        self.assertEqual(carbon["series"]["重庆"][-1]["price"], 45.0)

    def risk_cache(self):
        return {
            "yield_curves": {
                "CN10Y": {"points": [
                    {"date": "2026-08-20", "yield": 0.01},
                    {"date": "2026-08-21", "yield": 0.010662},
                ]},
                "US_TREASURY": {"points": [
                    {"date": "2026-08-20", "3Y": 0.03, "10Y": 0.04},
                    {"date": "2026-08-21", "3Y": 0.0305, "10Y": 0.0408},
                ]},
            },
            "exchange_rates": {"USDCNY": {"points": [
                {"date": "2026-08-20", "rate": 7.0},
                {"date": "2026-08-21", "rate": 6.93},
            ]}},
        }

    def test_duration_and_credit_buckets(self):
        self.assertEqual(_duration(0.8), 0.5)
        self.assertEqual(_duration(2), 2.0)
        self.assertEqual(_duration(4), 4.0)
        self.assertEqual(_duration(8), 6.0)
        self.assertEqual(_credit_bp("AAA"), 3.0)
        self.assertEqual(_credit_bp("AA+"), 5.0)
        self.assertEqual(_credit_bp("A+"), 8.0)
        self.assertEqual(_credit_bp("NR"), 10.0)
        self.assertEqual(_credit_bp("AAA", "次级"), 12.0)
        self.assertEqual(_credit_bp("AAA", trust=True), 12.0)

    def test_equity_fixed_rate_and_cn_rate_var(self):
        report = {"report_date": "2026-08-25", "report_file": "日报.xls", "assets": [
            {"id": "e", "product": "权益FOF", "name": "权益FOF", "category": "权益FOF",
             "currency": "CNY", "exposure": 100000000.0, "amount_basis": "本金",
             "fixed_var_rate": 0.05, "status": "pending", "error": None},
            {"id": "b", "product": "固收", "name": "AA+债", "category": "结构化固收",
             "currency": "CNY", "exposure": 100000000.0, "amount_basis": "认购金额",
             "rating": "AA+", "layer": "优先", "end_date": "2028-08-25",
             "status": "pending", "error": None},
        ]}
        result = calculate(report, self.risk_cache())
        equity, bond = result["assets"]
        self.assertEqual(equity["total_var"], 5000000.0)
        self.assertAlmostEqual(bond["rate_var_bp"], 6.62)
        self.assertEqual(bond["proxy_duration"], 2.0)
        self.assertAlmostEqual(bond["risk_free_var"], 132400.0)
        self.assertAlmostEqual(bond["credit_var"], 100000.0)
        self.assertIn("中债10Y VaR", bond["calculation_formula"])
        self.assertEqual(result["summary"]["calculated_assets"], 2)

    def test_us_rate_fx_and_carbon_left_tail(self):
        report = {"report_date": "2026-08-25", "report_file": "日报.xls", "assets": [
            {"id": "u", "product": "TRS", "name": "GNR", "category": "美元Agency MBS",
             "currency": "USD", "exposure": 100000000.0, "amount_basis": "人民币名义本金",
             "amount_is_cny": True, "agency_mbs": True, "end_date": "2036-08-25",
             "status": "pending", "error": None},
            {"id": "c", "product": "碳", "name": "重庆碳排放配额CQEA", "category": "碳资产",
             "currency": "CNY", "exposure": None, "amount_basis": "数量×价格", "quantity": 1000,
             "market": "重庆碳市场", "status": "pending", "error": None},
        ]}
        carbon = {"series": {"重庆": [
            {"date": "2026-08-20", "price": 100.0},
            {"date": "2026-08-21", "price": 90.0},
        ]}}
        result = calculate(report, self.risk_cache(), carbon)
        usd, carbon_asset = result["assets"]
        self.assertEqual(usd["proxy_duration"], 4.0)
        self.assertEqual(usd["rate_tenor"], "10Y")
        self.assertEqual(usd["credit_spread_bp"], 5.0)
        self.assertAlmostEqual(usd["fx_var"], 1000000.0)
        self.assertEqual(carbon_asset["exposure"], 90000.0)
        self.assertAlmostEqual(carbon_asset["carbon_var"], 9000.0)
        self.assertAlmostEqual(carbon_asset["carbon_var_rate"], 0.1)

    def test_missing_carbon_stays_visible(self):
        report = {"report_date": "2026-08-25", "report_file": "日报.xls", "assets": [
            {"id": "c", "product": "碳", "name": "CCER", "category": "碳资产",
             "currency": "CNY", "exposure": None, "amount_basis": "数量×价格", "quantity": 1,
             "market": "北京", "status": "pending", "error": None},
        ]}
        item = calculate(report, self.risk_cache(), None)["assets"][0]
        self.assertEqual(item["status"], "missing")
        self.assertIsNone(item["total_var"])

    def test_single_carbon_series_is_used_as_unified_proxy(self):
        report = {"report_date": "2026-08-25", "report_file": "日报.xls", "assets": [
            {"id": "cq", "product": "碳", "name": "重庆CQEA", "category": "碳资产",
             "currency": "CNY", "exposure": None, "amount_basis": "数量×价格", "quantity": 10,
             "market": "重庆碳市场", "status": "pending", "error": None},
            {"id": "ccer", "product": "碳", "name": "CCER", "category": "碳资产",
             "currency": "CNY", "exposure": None, "amount_basis": "数量×价格", "quantity": 20,
             "market": "北京绿色交易所", "status": "pending", "error": None},
        ]}
        carbon = {"series": {"Sheet1": [
            {"date": "2026-08-20", "price": 100.0}, {"date": "2026-08-21", "price": 90.0}]}}
        assets = calculate(report, self.risk_cache(), carbon)["assets"]
        self.assertTrue(all(item["status"] == "calculated" for item in assets))
        self.assertTrue(all("统一碳价代理" in item["method"] for item in assets))


if __name__ == "__main__":
    unittest.main()
