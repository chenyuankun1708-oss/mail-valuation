import os
import tempfile
import unittest

from openpyxl import Workbook

from valuation_app.otc_ledger import migrate_workbook
from valuation_app.otc_store import OtcStore, RevisionConflictError, TaskConflictError


class OtcLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = OtcStore(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_product_revision_manual_event_soft_void_and_backtest_mapping(self):
        product = self.store.create_product({
            "name": "测试发行", "strategy_name": "DCN", "structure": "dcn",
            "index_code": "000852", "notional": 10000000, "start_date": "2026-01-02",
            "terms": {"term_months": 24, "lock_period_months": 3,
                      "knock_in_ratio": .7, "knock_out_initial": 1,
                      "knock_out_decrease_monthly": .005, "first_coupon": .12,
                      "second_coupon": .12, "coupon_switch_months": 12,
                      "dividend_barrier": .8, "monthly_dividend": .008},
            "reference": {}, "notes": "仅测试"}, "alice")
        updated = self.store.update_product(product["id"], {"notes": "修订"}, 1, "bob")
        with self.assertRaises(RevisionConflictError):
            self.store.update_product(product["id"], {"notes": "冲突"}, 1, "alice")
        ki = self.store.add_event(product["id"], {"event_type": "敲入确认",
            "event_date": "2026-02-02", "values": {"note": "人工确认"},
            "expected_revision": updated["revision"]}, "bob")
        self.assertEqual(ki["status"], "草稿")
        active = self.store.add_event(product["id"], {"event_type": "生效",
            "event_date": "2026-01-02", "values": {}, "expected_revision": ki["revision"]}, "alice")
        self.assertEqual(active["status"], "存续")
        request = self.store.product_backtest_request(product["id"], active["revision"],
                                                       {"start_date": "2020-01-01", "end_date": "2026-01-01"})
        self.assertEqual(request["product_id"], product["id"])
        voided = self.store.void_product(product["id"], active["revision"], "alice")
        self.assertEqual(voided["status"], "已作废")
        self.assertGreaterEqual(len(self.store.get_product(product["id"])["audit"]), 5)

    def test_migration_keeps_13_rows_duplicates_raw_trace_and_is_idempotent(self):
        source = os.path.join(self.temporary.name, "历史零售雪球要素.xlsx")
        workbook = Workbook(); sheet = workbook.active; sheet.title = "Sheet1"
        for row in range(3, 16):
            sheet.cell(row, 1, "重复产品" if row in (9, 10) else "产品%d" % row)
            sheet.cell(row, 2, "原始策略%d" % row); sheet.cell(row, 3, "000852.SH")
            sheet.cell(row, 4, 1000 + row); sheet.cell(row, 5, 24)
            sheet.cell(row, 6, "2025-01-%02d" % row); sheet.cell(row, 7, 6000 + row)
            sheet.cell(row, 15, "存续" if row < 7 else "2026-01-01")
            sheet.cell(row, 18, "原始备注")
        workbook.save(source)
        with open(source, "rb") as handle:
            before = handle.read()
        result = migrate_workbook(self.store, source)
        self.assertEqual(result["imported"], 13)
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), before)
        products = self.store.list_products(1, 50)["items"]
        self.assertEqual(sum(item["name"] == "重复产品" for item in products), 2)
        self.assertEqual(sum(item["status"] == "存续" for item in products), 4)
        self.assertTrue(all(item["structure"] is None for item in products))
        traced = next(item for item in products if item["source_row"] == 3)
        self.assertEqual(traced["reference"]["source_row"], 3)
        with self.assertRaises(TaskConflictError):
            migrate_workbook(self.store, source)
