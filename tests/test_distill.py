"""蒸馏任务与草稿：任务流转、来源校验、MD 导出与哈希验证。"""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._path import *  # noqa: F401,F403
from ckp.distill import Distill
from ckp.md_render import export_draft, split_frontmatter, verify_md
from ckp.store import Store


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    rules_dir = repo / "templates" / "蒸馏规范"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rules.json").write_text(
        json.dumps({"version": "test-rules-v1", "files": ["规范.md"]},
                   ensure_ascii=False), encoding="utf-8")
    (rules_dir / "规范.md").write_text("# 规范\n只读任务材料。\n", encoding="utf-8")
    (rules_dir / "评分权重.json").write_text(json.dumps({
        "version": "test-score-v1",
        "dimensions": [
            {"key": "core_coverage", "label": "覆盖", "weight": 50},
            {"key": "source_traceability", "label": "来源", "weight": 25},
            {"key": "structure_and_retrieval", "label": "结构", "weight": 25}
        ],
        "bands": [{"minimum": 80, "label": "优先"},
                  {"minimum": 0, "label": "待提升"}]
    }, ensure_ascii=False), encoding="utf-8")
    return repo


BODY = """# 样例知识

## 身份与适用范围 <!-- kd-section id=identity_scope rag=body -->

产品范围：ERP；CRM

## 冲突、缺口与待确认项 <!-- kd-section id=uncertainties rag=body -->

无冲突；缺口见任务记录。
"""

SOURCES = [{"file_id": None, "version_id": "", "location": "全文", "supports": "示例"}]


class DistillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-distill-"))
        self.store = Store(self.tmp / "data")
        self.distill = Distill(self.store, make_repo(self.tmp))
        self.project = self.store.create_project("演示项目", owner="张三",
                                                 products=["项目档案产品标签"])
        upload = self.store.upload(write_minimal_docx(self.tmp / "需求纪要.docx", "# 需求\n内容A"),
                                   "演示项目")
        self.file_id = upload["uploaded"][0]["file_id"]
        self.version_id = upload["uploaded"][0]["version_id"]

    def _src(self, name: str, content: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def _prepare_quality(self, task_id):
        task = self.distill.get_task(task_id)
        for material in task["materials"]:
            self.distill.inspect_word(task_id, material["file_id"])
        return {"asset_reviews": [], "flowcharts": [], "table_checks": []}

    def _submit(self, *args, **kwargs):
        kwargs.setdefault("quality_report", self._prepare_quality(args[0]))
        return self.distill.submit_draft(*args, **kwargs)

    def _task(self):
        return self.distill.start_task("演示项目", "调研与需求")

    def _sources(self, location="全文"):
        return [{"file_id": self.file_id, "version_id": self.version_id,
                 "location": location, "supports": "正文示例"}]

    def test_existing_database_migrates_quality_report_snapshot_column(self):
        self.store.conn.execute("ALTER TABLE drafts DROP COLUMN quality_report_json")
        self.store.conn.commit()
        self.store.conn.close()
        migrated = Store(self.tmp / "data")
        columns = {row[1] for row in migrated.conn.execute("PRAGMA table_info(drafts)")}
        self.assertIn("quality_report_json", columns)
        migrated.conn.close()

    def test_stage_must_be_valid(self):
        with self.assertRaises(ValueError):
            self.distill.start_task("演示项目", "不存在的阶段")

    def test_guidance_requires_rules(self):
        task = self._task()
        guidance = self.distill.load_rules()
        self.assertEqual("test-rules-v1", guidance["version"])
        self.assertIn("规范.md", guidance["files"])
        self.assertTrue(task["rules_hash"])

    def test_attach_pins_version(self):
        task = self._task()
        attached = self.distill.attach(task["task_id"], self.file_id)
        self.assertEqual(self.version_id, attached["version_id"])
        with self.assertRaises(ValueError):
            self.distill.attach(task["task_id"], self.file_id, version_id="v-nope")
        self.store.upload(write_minimal_docx(self.tmp / "需求纪要.docx", "# 需求\n内容B"), "演示项目")
        with self.assertRaises(ValueError):
            self.distill.attach(task["task_id"], self.file_id)
        record = self.distill.get_task(task["task_id"])
        self.assertEqual(1, len(record["materials"]))
        self.assertEqual(self.version_id, record["materials"][0]["version_id"])

    def test_material_copy_and_cleanup(self):
        task = self._task()
        self.distill.attach(task["task_id"], self.file_id)
        copy = self.distill.material_copy(task["task_id"], self.file_id)
        with zipfile.ZipFile(copy["path"]) as archive:
            self.assertIn("# 需求", archive.read("word/document.xml").decode())
        self.distill.cleanup_task(task["task_id"])
        self.assertFalse(Path(copy["path"]).exists())
        self.assertEqual("closed", self.distill.get_task(task["task_id"])["status"])
        with self.assertRaises(ValueError):
            self.distill.attach(task["task_id"], self.file_id)

    def test_gap_and_human_input_require_content(self):
        task = self._task()
        with self.assertRaises(ValueError):
            self.distill.declare_gap(task["task_id"], "  ")
        gap = self.distill.declare_gap(task["task_id"], "缺少验收报告")
        with self.assertRaises(ValueError):
            self.distill.add_input(task["task_id"], "", "内容")
        record = self.distill.add_input(task["task_id"], "赵六", "实际未用该模块")
        self.assertEqual("赵六", record["provider"])

    def test_submit_rejects_unattached_or_incomplete_sources(self):
        task = self._task()
        self.distill.attach(task["task_id"], self.file_id)
        with self.assertRaises(ValueError):
            self._submit(task["task_id"], "标题", "问题案例", BODY,
                                      [{"version_id": "v-not-attached", "location": "全文"}])
        auto = self._submit(task["task_id"], "标题", "问题案例", BODY, [])
        self.assertTrue(auto["quality_check"]["source_autofilled"])
        source = self._sources()[0]
        source["location"] = ""
        coarse = self._submit(task["task_id"], "标题", "问题案例", BODY, [source])
        self.assertEqual("coarse", coarse["sources"][0]["mapping_precision"])

    def test_low_quality_submission_is_scored_not_blocked(self):
        task = self._task()
        self.distill.attach(task["task_id"], self.file_id)
        stripped = BODY.split("## 冲突、缺口与待确认项")[0]
        draft = self._submit(task["task_id"], "标题", "问题案例", stripped,
                             self._sources())
        self.assertEqual(["冲突、缺口与待确认项"],
                         draft["section_check"]["missing_required"])
        self.assertIn("必备章节缺失", " ".join(draft["quality_check"]["flags"]))
        self.assertEqual("test-score-v1", draft["quality_check"]["score_version"])

    def test_score_weights_are_human_editable_and_snapshotted(self):
        task = self._task()
        attached = self.distill.attach(task["task_id"], self.file_id)
        score_file = self.distill.score_policy_path
        current = json.loads(score_file.read_text(encoding="utf-8"))
        current["version"] = "test-score-v2"
        current["dimensions"][0]["weight"] = 40
        current["dimensions"][1]["weight"] = 30
        current["dimensions"][2]["weight"] = 30
        score_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        guidance = self.distill.guidance(task["task_id"])
        self.assertEqual("test-score-v1", guidance["score_policy"]["version"])
        draft = self.distill.submit_draft(
            task["task_id"], "低分也导出的知识", "方案设计", "只有一段的草稿",
            sources=[{"file_id": self.file_id, "version_id": attached["version_id"],
                      "location": "第 1 段", "supports": "测试来源"}],
            quality_report={"scores": {"core_coverage": 1},
                            "evidence": {"core_coverage": "内容很少"}})
        self.assertEqual("test-score-v1", draft["quality_check"]["score_version"])
        self.assertEqual(50, draft["quality_check"]["weighted_score"])

    def test_full_draft_flow_with_export_and_verify(self):
        task = self._task()
        self.distill.attach(task["task_id"], self.file_id)
        self.distill.declare_gap(task["task_id"], "缺少验收报告")
        self.distill.add_input(task["task_id"], "赵六", "实际未用该模块")
        source_sha = self.store.get_file(self.file_id)["versions"][0]["sha256"]
        flowchart = {
            "asset_id": f"{self.file_id}:word/document.xml:rId1:1",
            "asset_type": "process_flow",
            "source_file_id": self.file_id,
            "source_version_id": self.version_id,
            "source_sha256": source_sha,
            "source_location": "word/document.xml drawing 1",
            "checked_against_original": True,
            "nodes": [{"id": "start", "name": "开始", "type": "start"},
                      {"id": "approve", "name": "审批", "type": "activity", "role": "经理"},
                      {"id": "end", "name": "结束", "type": "end"}],
            "edges": [{"from": "start", "to": "approve", "direction": "start→approve",
                       "relationship": "sequence", "condition": "", "action": "提交",
                       "verified_against_original": True},
                      {"from": "approve", "to": "end", "direction": "approve→end",
                       "relationship": "sequence", "condition": "通过", "action": "完成",
                       "verified_against_original": True}],
            "unresolved_items": [],
        }
        quality_report = {"asset_reviews": [], "flowcharts": [flowchart],
                          "table_checks": [], "evidence": {"reading_coverage": "complete"}}
        body = (BODY + f"\n## 图像与流程资产 <!-- kd-section id=visual_assets rag=body -->\n\n"
                f"审批流程图；完整结构引用 asset_id={flowchart['asset_id']}。\n")
        draft = self._submit(task["task_id"], "调研结论样例", "方案设计",
                             body, self._sources(), model="unittest-stub",
                             quality_report=quality_report)
        self.assertEqual(1, draft["revision"])
        self.assertEqual("draft", draft["status"])
        self.assertEqual(1, len(draft["sources"]))
        self.assertEqual(self.version_id, draft["sources"][0]["version_id"])
        project = self.store.get_project("演示项目")
        out = export_draft(draft, project, self.tmp / "out")
        md_path = Path(out["md"])
        self.assertTrue(md_path.exists())
        check = verify_md(md_path)
        self.assertTrue(check["match"])
        self.assertEqual(draft["content_hash"], check["content_hash_actual"])
        sidecar = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
        self.assertEqual("ckp-distilled-knowledge/v0.4", sidecar["schema"])
        self.assertTrue(sidecar["rag_selection"]["routing_valid"])
        self.assertEqual(quality_report, sidecar["quality_report"])
        protected = sidecar["protected_assets"]["items"][0]
        self.assertEqual("quality_report.flowcharts[0]", protected["payload_ref"])
        self.assertEqual(self.file_id, protected["source"]["source_file_id"])
        self.assertEqual(self.version_id, protected["source"]["source_version_id"])
        canonical = json.dumps(flowchart, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), protected["payload_sha256"])
        self.assertEqual("scored", sidecar["quality_check"]["status"])
        self.assertEqual("test-score-v1", sidecar["quality_check"]["score_version"])
        self.assertIn("quality_score", md_path.read_text(encoding="utf-8"))
        frontmatter, _ = split_frontmatter(md_path.read_text(encoding="utf-8"))
        self.assertEqual(["ERP", "CRM"], frontmatter["products"])
        for process_field in ("stage", "owner", "task_id", "model", "rules_version",
                              "quality_score_version", "topics", "industry"):
            self.assertNotIn(process_field, frontmatter)
        self.assertEqual(1, len(sidecar["sources"]))
        self.assertEqual("赵六", sidecar["human_inputs"][0]["provider"])
        self.assertEqual("缺少验收报告", sidecar["gaps"][0]["description"])
        revised = self._submit(task["task_id"], "调研结论样例", "方案设计",
                                            BODY + "\n补充一行。\n", self._sources(),
                                            knowledge_id=draft["knowledge_id"])
        self.assertEqual(2, revised["revision"])
        with self.assertRaises(ValueError):
            self._submit(task["task_id"], "调研结论样例", "方案设计",
                                      BODY + "\n补充一行。\n", self._sources(),
                                      knowledge_id=draft["knowledge_id"])

    def test_frontmatter_roundtrip_with_special_chars(self):
        from ckp.md_render import render_frontmatter
        meta = {"knowledge_id": "k-abc", "revision": 1,
                "title": '含"引号"与\n换行 的标题', "products": ["ERP", "主,题"],
                "content_hash": "x" * 64}
        text = render_frontmatter(meta) + "正文\n"
        parsed, body = split_frontmatter(text)
        self.assertEqual('含"引号"与\n换行 的标题', parsed["title"])
        self.assertEqual(["ERP", "主,题"], parsed["products"])
        self.assertEqual("正文\n", body)


if __name__ == "__main__":
    unittest.main()
