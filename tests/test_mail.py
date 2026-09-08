import unittest

from valuation_app.mail import match_valuation_attachment


class MailAttachmentMatchTest(unittest.TestCase):
    def test_valuation_report_marker_matches_juzhi_product(self):
        filename = "估值报表_聚智多策略9号FOF单一计划-国元证券股份有限公司_20260821.xls"
        self.assertEqual(
            match_valuation_attachment("每日估值文件", filename),
            "中信建投聚智多策略9号FOF单一资产管理计划",
        )

    def test_unrelated_excel_is_not_downloaded(self):
        self.assertIsNone(match_valuation_attachment("聚智多策略9号", "产品说明.xlsx"))


if __name__ == "__main__":
    unittest.main()
