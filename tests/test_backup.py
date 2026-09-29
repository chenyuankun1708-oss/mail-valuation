import json
import os
import sqlite3
import tempfile
import unittest

from valuation_app.backup import (BackupError, check_backup, create_backup,
                                  restore_backup)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.temporary.name, "project")
        self.backup = os.path.join(self.temporary.name, "backup")
        os.makedirs(os.path.join(self.project, "products"))
        os.makedirs(os.path.join(self.project, "data_sources"))
        with open(os.path.join(self.project, "products", "a.xls"), "wb") as handle:
            handle.write(b"same-content")
        with open(os.path.join(self.project, "data_sources", "labels.json"), "wb") as handle:
            handle.write(b"same-content")
        with open(os.path.join(self.project, ".env"), "w") as handle:
            handle.write("PASSWORD=secret")
        db = os.path.join(self.project, "data_sources", "state.sqlite3")
        connection = sqlite3.connect(db)
        connection.execute("create table sample(value text)")
        connection.execute("insert into sample values ('kept')")
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_snapshot_deduplicates_excludes_secrets_and_restores(self):
        first = create_backup(self.project, self.backup, retention=30)
        self.assertEqual(first["created_blobs"], 2)
        manifest_path = os.path.join(self.backup, "snapshots", first["snapshot_id"] + ".json")
        with open(manifest_path, encoding="utf-8") as handle:
            paths = [item["path"] for item in json.load(handle)["files"]]
        self.assertNotIn(".env", paths)
        self.assertEqual(check_backup(self.backup)["status"], "ok")
        target = os.path.join(self.temporary.name, "restore")
        restored = restore_backup(self.backup, target)
        self.assertEqual(restored["restored"], 3)
        connection = sqlite3.connect(os.path.join(target, "data_sources", "state.sqlite3"))
        self.assertEqual(connection.execute("select value from sample").fetchone()[0], "kept")
        connection.close()

    def test_otc_runtime_database_is_backed_up_consistently(self):
        otc = os.path.join(self.project, "otc_derivatives_data")
        os.makedirs(otc)
        connection = sqlite3.connect(os.path.join(otc, "otc.sqlite3"))
        connection.execute("create table products(name text)")
        connection.execute("insert into products values ('发行产品')")
        connection.commit(); connection.close()
        attachment_dir = os.path.join(otc, "attachments", "files")
        os.makedirs(attachment_dir)
        with open(os.path.join(attachment_dir, "content.md"), "w", encoding="utf-8") as output:
            output.write("合同内容")
        snapshot = create_backup(self.project, self.backup)
        with open(os.path.join(self.backup, "snapshots", snapshot["snapshot_id"] + ".json"),
                  encoding="utf-8") as handle:
            paths = [item["path"] for item in json.load(handle)["files"]]
        self.assertIn("otc_derivatives_data/otc.sqlite3", paths)
        self.assertIn("otc_derivatives_data/attachments/files/content.md", paths)

    def test_tamper_is_detected_and_nonempty_restore_is_rejected(self):
        result = create_backup(self.project, self.backup)
        manifest_path = os.path.join(self.backup, "snapshots", result["snapshot_id"] + ".json")
        with open(manifest_path, encoding="utf-8") as handle:
            digest = json.load(handle)["files"][0]["sha256"]
        with open(os.path.join(self.backup, "blobs", digest[:2], digest), "ab") as handle:
            handle.write(b"tampered")
        self.assertEqual(check_backup(self.backup)["status"], "failed")
        target = os.path.join(self.temporary.name, "not-empty")
        os.makedirs(target)
        with open(os.path.join(target, "keep.txt"), "w") as handle:
            handle.write("keep")
        with self.assertRaises(BackupError):
            restore_backup(self.backup, target)

    def test_retention_removes_only_unreferenced_blobs(self):
        create_backup(self.project, self.backup, retention=1)
        with open(os.path.join(self.project, "products", "a.xls"), "wb") as handle:
            handle.write(b"new-content")
        second = create_backup(self.project, self.backup, retention=1)
        snapshots = os.listdir(os.path.join(self.backup, "snapshots"))
        self.assertEqual(snapshots, [second["snapshot_id"] + ".json"])
        self.assertEqual(check_backup(self.backup)["status"], "ok")

    def test_backup_inside_project_is_rejected(self):
        with self.assertRaises(BackupError):
            create_backup(self.project, os.path.join(self.project, "backup"))


if __name__ == "__main__":
    unittest.main()
