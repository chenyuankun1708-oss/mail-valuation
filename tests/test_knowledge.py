import json
import os
import tempfile
import unittest
import zipfile

from openpyxl import Workbook

from valuation_app.knowledge import KnowledgeStore, extract_text, infer_metadata


class KnowledgeStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(os.path.join(self.temporary.name, "knowledge"))

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name="会议纪要.md", text="量化产品会议纪要，讨论天玑13号。"):
        path = os.path.join(self.temporary.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_add_deduplicates_and_searches_full_text(self):
        path = self.source()
        item, duplicate = self.store.add_file(path, {"document_type": "会议纪要", "tags": ["量化"]})
        self.assertFalse(duplicate)
        second, duplicate = self.store.add_file(path)
        self.assertTrue(duplicate)
        self.assertEqual(second["id"], item["id"])
        self.assertEqual(self.store.list("天玑13号")[0]["id"], item["id"])
        self.assertEqual(self.store.get(item["id"])["text_content"], "量化产品会议纪要，讨论天玑13号。")

    def test_update_conflict_and_deactivate_move_to_trash(self):
        item, _ = self.store.add_file(self.source(), {"document_type": "会议纪要"})
        updated = self.store.update(item["id"], {"title": "新标题", "tags": "重要,FOF"}, item["revision"])
        self.assertEqual(updated["title"], "新标题")
        with self.assertRaises(ValueError):
            self.store.update(item["id"], {"title": "旧页面"}, item["revision"])
        inactive = self.store.deactivate(item["id"], updated["revision"])
        self.assertFalse(inactive["active"])
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.list("", True)), 1)

    def test_inbox_integrity_reindex_and_export(self):
        inbox_file = os.path.join(self.store.inbox_dir, "市场报告.txt")
        with open(inbox_file, "w", encoding="utf-8") as handle:
            handle.write("市场观点")
        result = self.store.import_inbox()
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(self.store.integrity_check()["database"], "ok")
        self.assertIn(self.store.rebuild_index()["status"], ("rebuilt", "fts5-unavailable"))
        output = os.path.join(self.temporary.name, "metadata.json")
        self.store.export_metadata(output)
        with open(output, "r", encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["documents"]), 1)

    def test_html_text_extraction(self):
        path = self.source("文章.html", "<h1>标题</h1><p>正文&amp;说明</p>")
        self.assertIn("正文&说明", extract_text(path))

    def test_rule_metadata_extracts_type_date_product_and_keywords(self):
        value = infer_metadata("2026年8月31日_会议纪要.md", "中信证券有限公司；作者：张三；讨论天玑13号量化配置")
        self.assertEqual(value["document_type"], "会议纪要")
        self.assertEqual(value["document_date"], "2026-08-31")
        self.assertIn("天玑13号", value["product"])
        self.assertIn("量化", value["tags"])
        self.assertEqual(value["organization"], "中信证券有限公司")
        self.assertEqual(value["author"], "张三")

    def test_office_formats_are_extracted_and_chunked(self):
        docx = os.path.join(self.temporary.name, "合同.docx")
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="x"><w:p><w:r><w:t>合同正文</w:t></w:r></w:p></w:document>')
        self.assertIn("合同正文", extract_text(docx))

        pptx = os.path.join(self.temporary.name, "路演.pptx")
        with zipfile.ZipFile(pptx, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="p" xmlns:a="a"><a:t>路演内容</a:t></p:sld>')
        self.assertIn("路演内容", extract_text(pptx))

        xlsx = os.path.join(self.temporary.name, "报告.xlsx")
        book = Workbook()
        book.active.append(["市场报告", "正文"])
        book.save(xlsx)
        self.assertIn("市场报告", extract_text(xlsx))

        long_file = self.source("长文.md", "知识库" * 1500)
        item, _ = self.store.add_file(long_file)
        with self.store._connect() as connection:
            chunks = connection.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (item["id"],)).fetchone()[0]
        self.assertGreater(chunks, 1)


if __name__ == "__main__":
    unittest.main()
