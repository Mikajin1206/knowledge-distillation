"""上传保存与查找：P2 验收场景的程序级验证。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._path import *  # noqa: F401,F403
from ckp.store import Store

import hashlib

CONTENT = b"# yaoqiu"
EXPECTED_SHA = hashlib.sha256(CONTENT).hexdigest()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-store-"))
        self.store = Store(self.tmp / "data")
        self.project = self.store.create_project("演示项目", industry=["制造"],
                                                 products=["ERP"], owner="张三")

    def _file(self, name: str, content: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def test_upload_two_files_and_search(self):
        result = self.store.upload(self._file("需求纪要.md", CONTENT),
                                   "演示项目", uploader="李四", stage="调研与需求",
                                   tags=["需求"])
        self.assertEqual(1, result["accepted_files"])
        self.assertEqual("created", result["uploaded"][0]["action"])
        self.assertEqual(EXPECTED_SHA, result["uploaded"][0]["sha256"])
        self.store.upload(self._file("方案v1.txt", b"fangan"), "演示项目")
        hits = self.store.search_files(project="演示项目")
        self.assertEqual(2, len(hits))
        by_name = self.store.search_files(name_like="需求")
        self.assertEqual(1, len(by_name))
        by_tag = self.store.search_files(tag="需求")
        self.assertEqual(1, len(by_tag))
        self.assertEqual("调研与需求", by_tag[0]["stage"])

    def test_duplicate_upload_keeps_relation_without_new_version(self):
        first = self.store.upload(self._file("a.md", b"abc"), "演示项目")
        second = self.store.upload(self._file("a.md", b"abc"), "演示项目", uploader="王五")
        self.assertEqual("reused", second["uploaded"][0]["action"])
        self.assertEqual(first["uploaded"][0]["version_id"],
                         second["uploaded"][0]["version_id"])
        record = self.store.get_file(first["uploaded"][0]["file_id"])
        self.assertEqual(1, len(record["versions"]))
        self.assertEqual(2, len(record["uploads"]))
        self.assertEqual("王五", record["uploads"][1]["uploader"])

    def test_changed_content_creates_readable_new_version(self):
        first = self.store.upload(self._file("方案.txt", b"v1-content"), "演示项目")
        file_id = first["uploaded"][0]["file_id"]
        second = self.store.upload(self._file("方案.txt", b"v2-content"), "演示项目")
        self.assertEqual("new_version", second["uploaded"][0]["action"])
        self.assertEqual(file_id, second["uploaded"][0]["file_id"])
        record = self.store.get_file(file_id)
        self.assertEqual(2, len(record["versions"]))
        old = self.store.read_version(file_id, first["uploaded"][0]["version_id"])
        new = self.store.read_version(file_id)
        self.assertNotEqual(old["sha256"], new["sha256"])
        self.assertEqual(first["uploaded"][0]["version_id"], old["version_id"])

    def test_same_content_across_projects_reuses_blob_keeps_relations(self):
        first = self.store.upload(self._file("shared.md", b"same"), "演示项目")
        self.store.create_project("另一项目")
        second = self.store.upload(self._file("shared.md", b"same"), "另一项目")
        self.assertNotEqual(first["uploaded"][0]["file_id"],
                            second["uploaded"][0]["file_id"])
        self.assertEqual(first["uploaded"][0]["sha256"],
                         second["uploaded"][0]["sha256"])
        blobs = list((self.tmp / "data" / "originals").rglob("*"))
        blob_files = [p for p in blobs if p.is_file()]
        self.assertEqual(1, len(blob_files))
        found = self.store.search_files(sha256=first["uploaded"][0]["sha256"])
        self.assertEqual(2, len(found))
        record = self.store.get_file(first["uploaded"][0]["file_id"])
        self.assertEqual(1, len(record["same_content_files"]))
        self.assertEqual("另一项目", record["same_content_files"][0]["project_name"])

    def test_read_version_detects_tampered_blob(self):
        result = self.store.upload(self._file("x.md", b"origin"), "演示项目")
        file_id = result["uploaded"][0]["file_id"]
        row = self.store.conn.execute("SELECT rel_path FROM blobs").fetchone()
        blob = self.tmp / "data" / row["rel_path"]
        blob.write_bytes(b"tampered")
        with self.assertRaises(ValueError) as ctx:
            self.store.read_version(file_id)
        self.assertIn("哈希校验失败", str(ctx.exception))

    def test_duplicate_project_name_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_project("演示项目")

    def test_read_version_to_out_and_hash_verified(self):
        result = self.store.upload(self._file("y.md", b"body"), "演示项目")
        out = self.tmp / "copy" / "y.md"
        read = self.store.read_version(result["uploaded"][0]["file_id"], out_path=out)
        self.assertTrue(read["verified"])
        self.assertEqual(b"body", out.read_bytes())


if __name__ == "__main__":
    unittest.main()
