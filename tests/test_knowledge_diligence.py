import json
import os
import tempfile
import unittest
from datetime import datetime

from valuation_app.knowledge import KnowledgeStore
from valuation_app.knowledge_diligence import KnowledgeDiligenceStore
from valuation_app.knowledge_sources import KnowledgeSourceStore


class KnowledgeDiligenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = self.temp.name
        os.makedirs(os.path.join(self.project, "data_sources"))
        with open(os.path.join(self.project, "data_sources", "product_labels.json"), "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "revision": 1, "records": []}, handle)
        source = os.path.join(self.project, "sample.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("测试资产管理有限公司成立于2020年。创始人张三负责投研团队。\n"
                         "公司采用CTA策略并建立风险控制体系，止损线为10%。\n"
                         "产品名称为测试一号，基金托管人为测试银行。\n"
                         "产品管理费为1%，截至2026年8月31日年化收益率为12%。")
        knowledge = KnowledgeStore(os.path.join(self.project, "knowledge_base"))
        self.document, _duplicate = knowledge.add_file(
            source, {"organization": "测试资产管理有限公司", "document_date": "2026-08-31",
                     "document_type": "产品路演材料"})
        self.store = KnowledgeDiligenceStore(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_creates_pending_page_evidence_and_all_fact_types(self):
        result = self.store.prepare([self.document["id"]])
        self.assertEqual(result["documents"], 1)
        self.assertGreaterEqual(result["facts"], 5)
        facts = self.store.facts(status="待确认", page_size=200)
        types = {item["fact_type"] for item in facts["items"]}
        self.assertTrue({"公司信息", "人员", "产品信息", "投资策略", "产品条款", "业绩记录", "风控信息"}.issubset(types))
        self.assertTrue(all(item["evidence"][0]["location"] == "第1页" for item in facts["items"]))
        self.assertTrue(all(item["status"] == "待确认" for item in facts["items"]))
        unchanged = self.store.prepare([self.document["id"]])
        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(self.store.facts(status="待确认", page_size=200)["total"], facts["total"])

    def test_fact_revision_audit_batch_and_dossier(self):
        self.store.prepare([self.document["id"]])
        items = self.store.facts(status="待确认", page_size=200)["items"]
        first = items[0]
        updated = self.store.update_fact(first["id"], first["revision"], "confirm", username="tester")
        self.assertEqual(updated["status"], "已确认")
        with self.assertRaises(RuntimeError):
            self.store.update_fact(first["id"], first["revision"], "reject", username="tester")
        remaining = self.store.facts(status="待确认", page_size=200)["items"][:2]
        result = self.store.batch_update(
            [{"id": item["id"], "expected_revision": item["revision"]} for item in remaining],
            "reject", "tester")
        self.assertEqual(result["updated"], len(remaining))
        dossier = self.store.dossier(first["subject_entity_id"])
        self.assertEqual(dossier["entity"]["canonical_name"], "测试资产管理有限公司")
        with self.store._connect() as connection:
            self.assertGreaterEqual(connection.execute("SELECT COUNT(1) FROM knowledge_fact_audit").fetchone()[0], 3)

    def test_fact_subject_mapping_can_be_corrected(self):
        self.store.prepare([self.document["id"]])
        fact = self.store.facts(status="待确认", page_size=200)["items"][0]
        with self.store._connect() as connection:
            connection.execute("INSERT INTO knowledge_entities VALUES(?,?,?,?,?)",
                               ("manager-corrected", "管理人", "修正后的管理人", "{}", "2026-09-23T00:00:00"))
        updated = self.store.update_fact(fact["id"], fact["revision"], "update",
                                         {"predicate": fact["predicate"],
                                          "display_value": fact["display_value"],
                                          "normalized_value": fact["normalized_value"],
                                          "subject_entity_id": "manager-corrected"}, "tester")
        self.assertEqual(updated["subject_entity_id"], "manager-corrected")
        with self.store._connect() as connection:
            edge = connection.execute("""SELECT from_entity_id FROM knowledge_graph_edges
                WHERE source_ref=? AND relation_type<>'文档披露事实'""", (fact["id"],)).fetchone()
        self.assertEqual(edge["from_entity_id"], "manager-corrected")

    def test_export_batch_is_hash_bound_and_vault_has_dossiers_and_graph_config(self):
        exported = self.store.export_batch()
        self.assertEqual(exported["count"], 1)
        with open(exported["path"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["items"][0]["sha256"], self.document["sha256"])
        self.assertEqual(payload["items"][0]["pages"][0]["page"], 1)
        source_store = KnowledgeSourceStore(self.project, now=datetime(2026, 9, 23))
        vault = source_store.export_obsidian()
        path, _record = source_store.export_path(vault["export_id"])
        import zipfile
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("首页.md", names)
            self.assertIn(".obsidian/graph.json", names)
            self.assertTrue(any(name.startswith("管理人尽调档案/") for name in names))
            self.assertTrue(any(name.startswith("待确认/风控信息/") for name in names))


if __name__ == "__main__":
    unittest.main()
