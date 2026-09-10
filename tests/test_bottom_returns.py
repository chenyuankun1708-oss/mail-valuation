import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

from valuation_app.bottom_returns import (
    BENCHMARK_CODES, _benchmark_metrics, _holding_summaries, _snapshots, analyze_bottom_return,
    parse_leaf_investments, sync_bottom_return_cache,
)


class BottomReturnTest(unittest.TestCase):
    def _workbook(self, path):
        book = Workbook()
        sheet = book.active
        sheet.append(["科目代码", "科目名称", "数量", "市价", "市值", "成本", "估值增值"])
        rows = [
            ("11020001", "股票A", 10, 11, 110, 100, 10),
            ("11030001", "转债A", 2, 120, 240, 220, 20),
            ("11050001", "基金A", 30, 1.2, 36, 30, 6),
            ("11090001", "基金B", 20, 2, 40, 38, 2),
            ("31020001", "股指期货A", 3, 5000, -300000, -310000, 10000),
            ("32010001", "认购期权9月", -4, 8, -3200, -3000, -200),
            ("11020002", "股票成本汇总", 1, 1, 1, 1, 0),
            ("10020001", "银行存款", 1, 1, 1, 1, 0),
        ]
        for row in rows:
            sheet.append(row)
        book.save(path)

    def test_six_leaf_prefixes_derivatives_and_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sample.xlsx")
            self._workbook(path)
            rows = parse_leaf_investments(path)
        self.assertEqual({row["code"][:4] for row in rows},
                         {"1102", "1103", "1105", "1109", "3102", "3201"})
        self.assertNotIn("股票成本汇总", [row["name"] for row in rows])
        future = next(row for row in rows if row["code"].startswith("3102"))
        option = next(row for row in rows if row["code"].startswith("3201"))
        self.assertEqual(future["market_value"], -300000)
        self.assertEqual(option["asset_type"], "期权")

    def test_incremental_cache_hash_dedupe_and_richer_same_day(self):
        with tempfile.TemporaryDirectory() as folder:
            root = os.path.join(folder, "底层资产")
            product_folder = os.path.join(root, "四级估值表", "P001")
            os.makedirs(product_folder)
            paths = [os.path.join(product_folder, name)
                     for name in ("a.xls", "b.xls", "c.xls", "a-copy.xls")]
            for index, path in enumerate(paths[:3]):
                with open(path, "wb") as output:
                    output.write(("file-%d" % index).encode("ascii"))
            with open(paths[3], "wb") as output:
                output.write(b"file-0")
            registry = [{"product_id": "P001", "product": "测试底层产品",
                         "normalized": "测试底层产品", "folder": product_folder}]
            snapshots = {
                paths[0]: SimpleNamespace(valuation_date="2026-01-02", nav=1.0,
                                          accumulated_nav=1.0, net_assets=100, shares=100),
                paths[1]: SimpleNamespace(valuation_date="2026-01-02", nav=1.0,
                                          accumulated_nav=1.0, net_assets=100, shares=100),
                paths[2]: SimpleNamespace(valuation_date="2026-01-09", nav=1.1,
                                          accumulated_nav=1.1, net_assets=110, shares=100),
            }
            holdings = {paths[0]: [{"key": "a"}], paths[1]: [{"key": "a"}, {"key": "b"}],
                        paths[2]: [{"key": "a"}]}
            cache = os.path.join(folder, "cache.sqlite3")
            with patch("valuation_app.bottom_returns._registry", return_value=registry), \
                    patch("valuation_app.bottom_returns._candidate_files", return_value=paths), \
                    patch("valuation_app.bottom_returns.parse_valuation",
                          side_effect=lambda path, product: snapshots[path]) as parser, \
                    patch("valuation_app.bottom_returns.parse_leaf_investments",
                          side_effect=lambda path: holdings[path]):
                manifest = sync_bottom_return_cache(
                    root, [("父FOF", "测试底层产品")], cache)
                self.assertEqual(parser.call_count, 3)
                sync_bottom_return_cache(root, [("父FOF", "测试底层产品")], cache)
                self.assertEqual(parser.call_count, 3)
            rows, errors = _snapshots(cache, "P001")
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]["holdings"]), 2)
            self.assertEqual(errors, [])
            self.assertEqual(manifest["product_count"], 1)
            self.assertEqual(manifest["products"][0]["fof_products"], ["父FOF"])

    def test_boundaries_nav_metrics_whitelist_and_no_ledger_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = os.path.join(folder, "cache.sqlite3")
            import sqlite3
            connection = sqlite3.connect(cache)
            connection.execute("CREATE TABLE files (path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,"
                               "sha256 TEXT,product_id TEXT,product_name TEXT,valuation_date TEXT,"
                               "nav REAL,accumulated_nav REAL,net_assets REAL,shares REAL,"
                               "holdings_json TEXT,holding_count INTEGER,error TEXT,updated_at TEXT)")
            for index, (date, nav) in enumerate((("2026-01-02", 1.0),
                                                  ("2026-01-09", 1.1))):
                connection.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (str(index), 1, 1, str(index), "P001", "底层A", date,
                                    nav, nav, nav * 100, 100, "[]", 0, None, date))
            connection.commit()
            connection.close()
            manifest = {"products": [{"product_id": "P001", "product": "底层A",
                                       "fof_products": ["父FOF"]}]}
            market = [{"date": "2026-01-02", "close": 100},
                      {"date": "2026-01-09", "close": 102}]
            for code in BENCHMARK_CODES:
                result = analyze_bottom_return(cache, manifest, "P001", "2026-01-04",
                                               "2026-01-10", code, market)
                self.assertEqual(result["actual_start"], "2026-01-02")
                self.assertEqual(result["actual_end"], "2026-01-09")
                self.assertAlmostEqual(result["period_return"], 10.0)
                self.assertIsNone(result["amount_profit"])
                self.assertIsNone(result["xirr"])
                self.assertIn("无底层资金台账", result["cashflow_status"])
            with self.assertRaises(ValueError):
                analyze_bottom_return(cache, manifest, "P001", "2026-01-04",
                                      "2026-01-10", "000001", market)

    def test_derivative_signed_price_impact_new_and_exit_rules(self):
        points = [
            {"valuation_date": "2026-01-02", "holdings": [
                {"key": "f", "code": "31020001", "name": "期货", "asset_type": "期货/衍生品",
                 "quantity": 2, "price": 100, "market_value": -2000,
                 "cost": -1900, "valuation_gain": -100},
                {"key": "x", "code": "11020001", "name": "退出", "asset_type": "股票",
                 "quantity": 1, "price": 10, "market_value": 10, "cost": 9,
                 "valuation_gain": 1}]},
            {"valuation_date": "2026-01-09", "holdings": [
                {"key": "f", "code": "31020001", "name": "期货", "asset_type": "期货/衍生品",
                 "quantity": 2, "price": 110, "market_value": -2200,
                 "cost": -1900, "valuation_gain": -300},
                {"key": "n", "code": "11020002", "name": "新增", "asset_type": "股票",
                 "quantity": 2, "price": 10, "market_value": 20, "cost": 18,
                 "valuation_gain": 2}]},
        ]
        result = {item["key"]: item for item in _holding_summaries(points, 100)}
        self.assertEqual(result["f"]["estimated_profit"], -200)
        self.assertEqual(result["n"]["estimated_profit"], 2)
        self.assertIsNone(result["x"]["estimated_profit"])
        self.assertIn("无成交价", result["x"]["status"])

    def test_alpha_beta_uses_fixed_benchmark_series(self):
        from datetime import date, timedelta
        start = date(2026, 1, 2)
        product = []
        market = []
        for index in range(12):
            current = (start + timedelta(days=index * 7)).isoformat()
            market.append({"date": current,
                           "close": 100 * (1 + 0.01 * index + 0.0005 * index * index)})
            product.append({"valuation_date": current,
                            "accumulated_nav": 1 + 0.012 * index + 0.0007 * index * index})
        result = _benchmark_metrics(product, market)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["intervals"], 11)
        self.assertIsNotNone(result["alpha"])
        self.assertIsNotNone(result["beta"])


if __name__ == "__main__":
    unittest.main()
