import os
import json
import tempfile
import unittest
import zipfile
from unittest import mock

from valuation_app.labels import (build_label_payload, deduplicate_label_records,
                                  ensure_catalog_products, load_json_catalog, match_label,
                                  migrate_catalog, mutate_catalog, normalize_name, read_workbook)


class LabelTest(unittest.TestCase):
    def test_normalize_removes_common_suffix_and_share_class(self):
        self.assertEqual(normalize_name("会世元丰CTA1号私募投资基金A类"), "会世元丰CTA1号")

    def test_exact_and_unique_short_name_matching(self):
        records = [{"manager": "会世", "product": "会世元丰CTA1号", "primary": "CTA",
                    "raw_primary": "CTA", "classification_evidence": "",
                    "secondary": "全品种", "vehicle": "集合", "source": "产品标签",
                    "sheet": "CTA", "row": 2, "normalized": "会世元丰CTA1号"}]
        self.assertEqual(match_label("会世元丰CTA1号私募投资基金A类", records)["primary"], "CTA")
        self.assertEqual(match_label("会世元丰CTA1号", records)["match_status"], "已匹配")

    def test_ambiguous_or_missing_is_other(self):
        base = {"manager": "", "product": "同名产品", "secondary": "其他", "vehicle": "其他",
                "source": "产品标签", "sheet": "股票", "row": 2, "normalized": "同名产品",
                "raw_primary": "股票", "classification_evidence": ""}
        records = [dict(base, primary="股票"), dict(base, primary="CTA")]
        self.assertEqual(match_label("同名产品私募基金", records)["match_status"], "歧义")
        self.assertEqual(match_label("不存在的产品", records)["primary"], "其他")

    def test_manager_vehicle_and_catalogue_strategy_are_merged(self):
        manager = {"manager": "会世", "product": "会世元丰CTA1号", "primary": "其他",
                   "raw_primary": "其他", "classification_evidence": "",
                   "secondary": "其他", "vehicle": "专户", "source": "管理人清单",
                   "sheet": "策略归类拆分", "row": 2, "normalized": "会世元丰CTA1号"}
        catalogue = dict(manager, primary="CTA", raw_primary="CTA", secondary="全品种", vehicle="其他",
                         source="产品标签", sheet="CTA")
        result = match_label("会世元丰CTA1号私募基金", [manager, catalogue])
        self.assertEqual((result["primary"], result["secondary"], result["vehicle"]),
                         ("CTA", "全品种", "专户"))

    def test_stock_primary_is_split_by_exposure_and_description(self):
        base = {"manager": "甲", "product": "产品", "primary": "股票", "raw_primary": "股票",
                "secondary": "中证1000", "vehicle": "专户", "source": "产品标签",
                "sheet": "股票", "row": 2, "normalized": "产品名称足够长", "classification_evidence": ""}
        cases = [("100%：指数增强", "股票指增"), ("0-5%：完全对冲", "股票中性"),
                 ("0-100%：灵活对冲", "股票对冲"), ("不完全对冲", "股票对冲")]
        for evidence, expected in cases:
            record = dict(base, classification_evidence=evidence)
            record["normalized"] = normalize_name("产品名称足够长")
            self.assertEqual(match_label("产品名称足够长", [record])["primary"], expected)

    def test_invalid_styles_are_ignored_by_xml_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sample.xlsx")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="标签" sheetId="1" r:id="rId1"/></sheets></workbook>')
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
                archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>产品名称</t></is></c></row></sheetData></worksheet>')
                archive.writestr("xl/styles.xml", "<broken")
            self.assertEqual(read_workbook(path)["标签"][0][0], "产品名称")

    def test_catalog_migration_and_json_runtime_source(self):
        records = [{"manager": "甲", "product": "测试产品", "primary": "CTA",
                    "raw_primary": "CTA", "secondary": "全品种", "vehicle": "专户",
                    "source": "产品标签", "sheet": "CTA", "row": 2,
                    "normalized": normalize_name("测试产品"),
                    "classification_evidence": "规则说明"}]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "product_labels.json")
            with mock.patch("valuation_app.labels.load_catalog", return_value=(records, {}, [])):
                payload = migrate_catalog("产品标签.xlsx", "管理人清单.xlsx", path)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["revision"], 1)
            self.assertEqual(payload["records"][0]["department"], "无")
            self.assertTrue(payload["records"][0]["product_id"].startswith("product_"))
            self.assertEqual(payload["records"][0]["version"], 1)
            loaded, raw = load_json_catalog(path)
            self.assertEqual(loaded[0]["classification_evidence"], "规则说明")
            self.assertEqual(raw["migrated_from"], ["产品标签.xlsx", "管理人清单.xlsx"])
            result = build_label_payload(["测试产品"], path)
            self.assertEqual(result["matches"]["测试产品"]["primary"], "CTA")

    def test_disabled_json_record_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "product_labels.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "revision": 2, "updated_at": "2026-09-07",
                           "records": [{"product": "测试产品", "normalized": "测试产品",
                                        "active": False}]}, handle)
            records, _payload = load_json_catalog(path)
            self.assertEqual(records, [])
            self.assertEqual(build_label_payload(["测试产品"], path)["matches"]["测试产品"]["primary"], "其他")

    def test_department_defaults_to_wu_and_is_only_changed_online(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "product_labels.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "revision": 1, "updated_at": "2026-09-10",
                           "records": [{"record_id": "label_1", "product": "测试产品足够长",
                                        "normalized": normalize_name("测试产品足够长"),
                                        "primary": "CTA", "raw_primary": "CTA",
                                        "secondary": "全品种", "vehicle": "集合",
                                        "manager": "甲", "source": "网页标签", "active": True}]}, handle)
            records, _payload = load_json_catalog(path)
            self.assertEqual(records[0]["department"], "无")
            payload, item = mutate_catalog(path, "update", {"department": "华东营业部"}, 1,
                                           "tester", record_id="label_1")
            self.assertEqual(item["department"], "华东营业部")
            self.assertEqual(payload["revision"], 2)

    def test_product_name_dedupe_merges_complementary_fields(self):
        base = {"product": "华年元享1号私募证券投资基金", "normalized": "华年元享1号",
                "manager": "华年", "primary": "股票指增", "secondary": "量化选股",
                "vehicle": "专户", "department": "无", "classification_basis": "人工维护",
                "source": "管理人清单", "record_id": "label_old", "active": True}
        department = dict(base, manager="", primary="其他", secondary="其他",
                          department="上海虹桥路", source="网页标签",
                          record_id="label_department")
        records, check = deduplicate_label_records([base, department])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["department"], "上海虹桥路")
        self.assertEqual(records[0]["primary"], "股票指增")
        self.assertEqual(check["duplicate_groups"], 1)
        self.assertEqual(check["duplicates"][0]["record_ids"],
                         ["label_old", "label_department"])

    def test_missing_end_holding_is_auto_added_once_with_neutral_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "product_labels.json")
            audit_path = os.path.join(folder, "logs", "label-audit.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "revision": 4, "updated_at": "2026-09-10",
                           "records": []}, handle)
            result = build_label_payload(
                ["待补录产品私募证券投资基金"], path,
                auto_add_names=["待补录产品私募证券投资基金", "待补录产品"],
                audit_path=audit_path)
            self.assertEqual(result["revision"], 5)
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["auto_added_products"], ["待补录产品私募证券投资基金"])
            record = result["deduped_records"][0]
            self.assertEqual((record["manager"], record["primary"], record["secondary"],
                              record["vehicle"], record["department"]),
                             ("", "其他", "其他", "其他", "无"))
            self.assertEqual(result["matches"]["待补录产品私募证券投资基金"]["match_status"],
                             "已匹配")
            second = build_label_payload([], path, auto_add_names=["待补录产品"])
            self.assertEqual(second["revision"], 5)
            self.assertTrue(os.path.exists(audit_path))

    def test_create_and_rename_reject_duplicate_normalized_product_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "product_labels.json")
            records = [{"record_id": "label_1", "product": "已有产品私募证券投资基金",
                        "normalized": normalize_name("已有产品私募证券投资基金"),
                        "active": True, "version": 1},
                       {"record_id": "label_2", "product": "另一产品",
                        "normalized": normalize_name("另一产品"),
                        "active": True, "version": 1}]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "revision": 1,
                           "updated_at": "2026-09-10", "records": records}, handle)
            with self.assertRaises(ValueError):
                mutate_catalog(path, "create", {"product": "已有产品"}, 1, "tester")
            with self.assertRaises(ValueError):
                mutate_catalog(path, "update", {"product": "已有产品"}, 1, "tester",
                               record_id="label_2")


if __name__ == "__main__":
    unittest.main()
