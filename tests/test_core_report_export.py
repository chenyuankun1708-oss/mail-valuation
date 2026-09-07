import io
import unittest

import openpyxl

from valuation_app.core_report_export import export_core_report


class CoreReportExportTest(unittest.TestCase):
    def test_export_contains_management_sheets(self):
        payload = {
            "start": "2026-01-01", "end": "2026-08-31", "scope": "all",
            "rule_note": "确定性规则",
            "summary": {"opening": 100, "ending": 110, "subscriptions": 0,
                        "redemptions": 0, "dividends": 0, "profit": 10,
                        "reconciliation": 0, "calculated": 1, "eligible": 1,
                        "coverage": 1},
            "attention": [{"kind": "收益贡献", "text": "甲贡献10", "rule": "阈值", "level": .5}],
            "contributions": [{"name": "甲", "profit": 10, "status": "ok",
                               "first": {"net_assets": 100}, "last": {"net_assets": 110}}],
            "concentration": {"products": {"total": 110, "top3": 1, "hhi": 1}},
            "risk": {}, "quality": {}, "holding_changes": [],
        }
        book = openpyxl.load_workbook(io.BytesIO(export_core_report(payload)), data_only=True)
        self.assertEqual(book.sheetnames, ["核心摘要", "重点事项", "收益贡献", "集中度",
                                          "风险与数据质量", "持仓变化"])
        self.assertEqual(book["核心摘要"]["B5"].value, 100)

    def test_export_rejects_unbounded_rows(self):
        with self.assertRaises(ValueError):
            export_core_report({"summary": {}, "attention": [{}] * 5001})


if __name__ == "__main__":
    unittest.main()
