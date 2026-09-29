import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime

from valuation_app.config import PRODUCTS
from valuation_app.knowledge_sources import KnowledgeSourceStore


class KnowledgeSourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.temp.name, "project")
        self.source = os.path.join(self.temp.name, "wechat")
        os.makedirs(os.path.join(self.project, "data_sources"))
        os.makedirs(self.source)
        with open(os.path.join(self.project, "data_sources", "product_labels.json"), "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "revision": 1, "records": [{
                "record_id": "label_product_1", "active": True, "product": "测试私募一号私募证券投资基金",
                "manager": "测试资产管理有限公司", "primary": "股票", "secondary": "量化",
                "vehicle": "私募基金", "department": "无"
            }]}, handle, ensure_ascii=False)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, month, name, content):
        folder = os.path.join(self.source, month)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_recent_six_month_scan_auto_import_and_incremental_skip(self):
        self.write("2026-04", "测试私募一号私募证券投资基金月报.txt",
                   "测试私募一号私募证券投资基金 月报 测试资产管理有限公司")
        self.write("2026-03", "测试私募一号私募证券投资基金月报.txt",
                   "测试私募一号私募证券投资基金 月报")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        first = store.scan()
        self.assertEqual(first["stats"]["inventoried"], 1)
        self.assertEqual(first["stats"]["auto_imported"], 1)
        candidates = store.candidates(page_size=20)
        self.assertEqual(candidates["total"], 1)
        self.assertEqual(candidates["items"][0]["status"], "已入库")
        second = store.scan()
        self.assertEqual(second["stats"]["unchanged"], 1)

    def test_sensitive_candidate_never_exposes_preview_or_auto_imports(self):
        self.write("2026-09", "测试私募一号投资者名单.txt",
                   "测试私募一号私募证券投资基金 身份证 110101199001011234")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        result = store.scan()
        self.assertEqual(result["stats"]["auto_imported"], 0)
        item = store.candidates(status="敏感受限", page_size=20)["items"][0]
        self.assertNotIn("redacted_preview", item)
        detail = store.candidate(item["id"])
        self.assertNotIn("redacted_preview", detail)
        with self.assertRaises(ValueError):
            store.import_candidates([item["id"]])

    def test_unknown_candidate_stays_for_review_and_review_export_is_redacted(self):
        self.write("2026-09", "未知私募产品路演.txt", "未知私募产品路演，策略为量化选股。")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        store.scan()
        item = store.candidates(status="待确认", page_size=20)["items"][0]
        self.assertFalse(item["product_id"])
        exported = store.export_review()
        with open(exported["path"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(len(payload["items"]), 1)
        self.assertNotIn("relative_path", json.dumps(payload, ensure_ascii=False))

    def test_graph_separates_deterministic_and_document_edges_and_exports_vault(self):
        self.write("2026-09", "测试私募一号私募证券投资基金月报.txt",
                   "测试私募一号私募证券投资基金 月报 测试资产管理有限公司")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        store.scan()
        graph = store.graph(limit=1000)
        self.assertTrue(any(edge["source_kind"] == "产品标签" for edge in graph["edges"]))
        self.assertTrue(any(edge["source_kind"] == "微信文档" for edge in graph["edges"]))
        exported = store.export_obsidian()
        path, _record = store.export_path(exported["export_id"])
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("README.md", names)
            self.assertIn("索引/实体目录.md", names)
            self.assertIn("索引/关系证据.md", names)
            self.assertTrue(any(name.startswith("已确认/") for name in names))
            self.assertTrue(all(not name.lower().endswith((".pdf", ".xlsx", ".docx")) for name in names))

    def test_vault_exports_every_node_and_same_names_without_overwrite(self):
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        with store._connect() as connection:
            rows = [("vault-node-%04d" % index, "文档", "同名材料", "{}", "2026-09-22T00:00:00")
                    for index in range(1005)]
            connection.executemany(
                "INSERT INTO knowledge_entities(id,entity_type,canonical_name,metadata_json,updated_at) VALUES(?,?,?,?,?)",
                rows)
            store._upsert_entity(connection, "vault-pending-fact", "公司信息", "待核规模", {"value": "待核"})
            store._upsert_edge(connection, "vault-node-0000", "vault-pending-fact", "文档披露公司信息",
                               "助手审阅", "candidate-test", {"location": "第1页"}, 0.8,
                               "待人工确认", "Codex脱敏审阅")
            expected_nodes = connection.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0]
        exported = store.export_obsidian()
        path, _record = store.export_path(exported["export_id"])
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            node_pages = [name for name in names if name.startswith(("已确认/", "待确认/"))]
            self.assertEqual(len(node_pages), expected_nodes)
            self.assertEqual(len(node_pages), len(set(node_pages)))
            self.assertEqual(sum("同名材料--" in name for name in node_pages), 1005)
            self.assertTrue(any(name.startswith("待确认/公司信息/") for name in node_pages))
            readme = archive.read("README.md").decode("utf-8")
            self.assertIn("同名实体不会互相覆盖", readme)

    def test_top_product_alias_is_registered(self):
        name = next(iter(PRODUCTS))
        alias = PRODUCTS[name][0]
        self.write("2026-09", "%s月报.txt" % alias, "%s 月报" % name)
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        store.scan()
        item = store.candidates(page_size=20)["items"][0]
        self.assertTrue(item["product_id"].startswith("top-"))

    def test_diligence_review_creates_evidence_graph_and_centered_view(self):
        self.write("2026-09", "测试资产管理有限公司路演.txt",
                   "测试资产管理有限公司介绍创始人张三及量化选股策略，并披露测试私募一号私募证券投资基金业绩。")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        store.scan()
        exported = store.export_review()
        with open(exported["path"], "r", encoding="utf-8") as handle:
            source_payload = json.load(handle)
        candidate = source_payload["items"][0]
        evidence = [{"location": "第1页", "excerpt": "材料披露的短证据"}]
        review_payload = {"batch_id": exported["batch_id"], "items": [{
            "candidate_id": candidate["candidate_id"], "sha256": candidate["sha256"],
            "review": {
                "status": "匹配", "confidence": 0.91, "document_type": "产品路演材料",
                "product_id": "label_product_1", "manager": "测试资产管理有限公司",
                "top_fof_ids": [], "strategy_labels": [], "evidence": evidence,
                "suggested_relations": [],
                "company_profile": [{"category": "管理规模", "value": "材料披露规模", "evidence": evidence}],
                "people": [{"name": "张三", "role_type": "创始人", "title": "创始人",
                            "product_ids": ["label_product_1"], "strategy_names": ["量化选股"],
                            "evidence": evidence}],
                "strategies": [{"name": "量化选股", "description": "材料披露策略摘要",
                                "asset_classes": ["股票"], "constraints": ["材料披露约束"],
                                "product_ids": ["label_product_1"], "evidence": evidence}],
                "performance_records": [{"product_id": "label_product_1",
                                         "period_start": "2025-01-01", "period_end": "2025-12-31",
                                         "metrics": {"年化收益率": 0.12, "最大回撤": -0.08},
                                         "source_basis": "材料披露", "evidence": evidence}]
            }}]}
        review_path = os.path.join(store.review_dir, exported["batch_id"] + ".review.json")
        with open(review_path, "w", encoding="utf-8") as handle:
            json.dump(review_payload, handle, ensure_ascii=False)
        store.import_review(exported["batch_id"])
        graph = store.graph(limit=1000)
        types = {node["type"] for node in graph["nodes"]}
        self.assertTrue({"公司信息", "人员", "投资策略", "业绩记录"}.issubset(types))
        self.assertTrue(graph["suggested_center_id"])
        self.assertTrue(any(edge["relation"] == "文档披露人员" and edge["status"] == "待人工确认"
                            for edge in graph["edges"]))
        manager = next(node for node in graph["nodes"]
                       if node["type"] == "管理人" and node["name"] == "测试资产管理有限公司")
        centered = store.graph(limit=1000, center_id=manager["id"])
        self.assertEqual(centered["center_id"], manager["id"])
        self.assertIn("人员", centered["type_counts"])
        with self.assertRaises(ValueError):
            store.export_review()

    def test_diligence_review_rejects_personal_contact_information(self):
        self.write("2026-09", "未知私募产品路演.txt", "未知私募产品路演，介绍管理人和团队。")
        store = KnowledgeSourceStore(self.project, self.source, datetime(2026, 9, 22))
        store.scan()
        exported = store.export_review()
        with open(exported["path"], "r", encoding="utf-8") as handle:
            candidate = json.load(handle)["items"][0]
        review = {"batch_id": exported["batch_id"], "items": [{
            "candidate_id": candidate["candidate_id"], "sha256": candidate["sha256"],
            "review": {"status": "不确定", "confidence": 0.5, "product_id": "", "manager": "测试管理人",
                       "top_fof_ids": [], "evidence": [{"location": "第1页", "excerpt": "联系电话13800138000"}],
                       "suggested_relations": [], "company_profile": [], "people": [],
                       "strategies": [], "performance_records": []}}]}
        path = os.path.join(store.review_dir, exported["batch_id"] + ".review.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(review, handle, ensure_ascii=False)
        with self.assertRaises(ValueError):
            store.import_review(exported["batch_id"])


if __name__ == "__main__":
    unittest.main()
