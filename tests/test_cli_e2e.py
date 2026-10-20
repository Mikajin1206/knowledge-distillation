"""CLI 端到端：从建项目到导出 md 的完整命令链（独立数据目录，不碰真实 data/）。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests._path import *  # noqa: F401,F403
from ckp import cli

BODY = """# 调研知识样例

## 调研结论 <!-- kd-section id=research_conclusion rag=body -->

保留与项目相关的事实和结论。

## 冲突、缺口与待确认项 <!-- kd-section id=uncertainties rag=body -->

缺口：验收材料未上传。
"""


class CliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-cli-"))
        self._old_env = {key: os.environ.get(key) for key in ("CKP_DATA", "CKP_OUTPUT")}
        os.environ["CKP_DATA"] = str(self.tmp / "data")
        os.environ["CKP_OUTPUT"] = str(self.tmp / "out")
        self.src = write_minimal_docx(self.tmp / "需求纪要.docx", "# 需求纪要\n关键业务规则如下。")

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run(self, *argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(list(argv))
        self.assertEqual(0, code, msg=f"命令失败: {argv}")
        return buffer.getvalue().strip()

    def _run_fail(self, *argv: str) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        self.assertEqual(2, code, msg=f"命令应失败: {argv}")

    def test_end_to_end(self):
        self._run("project", "create", "CLI演示项目", "--owner", "张三",
                  "--industry", "制造,物流")
        upload_out = json.loads(self._run("upload", str(self.src),
                                          "--project", "CLI演示项目",
                                          "--uploader", "李四",
                                          "--stage", "调研与需求", "--tags", "需求"))
        self.assertEqual(1, upload_out["accepted_files"])
        self.assertIn("蒸馏仅支持 Word 文件 .doc/.docx", upload_out["note"])
        file_id = upload_out["uploaded"][0]["file_id"]

        listing = self._run("file", "list", "--project", "CLI演示项目")
        self.assertIn(file_id, listing)
        self.assertIn("需求纪要.docx", listing)

        task = json.loads(self._run("distill", "start", "--project", "CLI演示项目",
                                    "--stage", "调研与需求"))
        task_id = task["task_id"]
        self._run("distill", "attach", task_id, "--file", file_id)
        self._run("distill", "inspect-word", task_id, "--file", file_id)
        self._run("distill", "gap", task_id, "验收材料未上传")
        self._run("distill", "input", task_id, "--provider", "赵六", "补充：旧系统将于年底停用")
        guidance = json.loads(self._run("distill", "guidance", task_id))
        self.assertEqual("distill-rules-v0.10", guidance["rules_version"])
        self.assertIn("通用蒸馏规范.md", guidance["rules_files"])
        material = json.loads(self._run("distill", "material", task_id, "--file", file_id))
        self.assertTrue(Path(material["path"]).exists())

        md_file = self.tmp / "draft.md"
        md_file.write_text(BODY, encoding="utf-8")
        sources_file = self.tmp / "sources.json"
        sources_file.write_text(json.dumps([{
            "file_id": file_id, "version_id": upload_out["uploaded"][0]["version_id"],
            "location": "全文", "supports": "示例结论"}], ensure_ascii=False),
            encoding="utf-8")
        quality_file = self.tmp / "quality.json"
        quality_file.write_text(json.dumps({
            "asset_reviews": [], "flowcharts": [], "table_checks": []}), encoding="utf-8")
        draft = json.loads(self._run("draft", "submit", task_id, "--title", "调研结论",
                                     "--type", "方案设计", "--md-file", str(md_file),
                                     "--sources-json", str(sources_file),
                                     "--quality-report-json", str(quality_file),
                                     "--model", "cli-e2e-stub"))
        knowledge_id = draft["knowledge_id"]
        self.assertEqual("draft", draft["status"])

        exported = json.loads(self._run("draft", "export", knowledge_id))
        self.assertTrue(exported["verify"]["match"])
        verify = json.loads(self._run("draft", "verify", exported["md"]))
        self.assertTrue(verify["match"])

        read = json.loads(self._run("file", "read-version", file_id))
        self.assertTrue(read["verified"])

        self._run("distill", "close", task_id)
        self.assertFalse(Path(material["path"]).exists())

    def test_invalid_command_fails_cleanly(self):
        self._run("project", "create", "另一项目")
        self._run_fail("project", "create", "另一项目")
        self._run_fail("distill", "start", "--project", "另一项目", "--stage", "错误阶段")
        self._run_fail("file", "show", "f-missing")
        self._run_fail("upload", str(self.tmp / "不存在.md"), "--project", "另一项目")


if __name__ == "__main__":
    unittest.main()
