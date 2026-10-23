"""docs/06 审计报告回归用例：F01—F16 逐项对应（编号见测试方法 docstring）。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._path import *  # noqa: F401,F403
from ckp import cli
from ckp.distill import Distill, check_body
from ckp.md_render import export_draft, split_frontmatter, verify_md
from ckp.store import Store

BODY = """# 知识

## 身份与适用范围 <!-- kd-section id=identity_scope rag=body -->

产品范围：ERP；CRM

## 冲突、缺口与待确认项 <!-- kd-section id=uncertainties rag=body -->

缺口：合同未上传，效力待确认。
"""


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    catalog_path = repo / "config" / "product_domain_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps({
        "schema": "ckp-product-domain-catalog/v1",
        "products": [{"name": "测试产品", "domains": ["测试领域", "办公协同"]}],
    }, ensure_ascii=False), encoding="utf-8")
    rules_dir = repo / "templates" / "蒸馏规范"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rules.json").write_text(
        json.dumps({"version": "fix-rules-v1", "files": ["规范.md"]}, ensure_ascii=False),
        encoding="utf-8")
    (rules_dir / "规范.md").write_text("# 规范\n只读任务材料。\n", encoding="utf-8")
    (rules_dir / "评分权重.json").write_text(json.dumps({
        "version": "fix-score-v1",
        "dimensions": [
            {"key": "core_coverage", "label": "覆盖", "weight": 50},
            {"key": "source_traceability", "label": "来源", "weight": 25},
            {"key": "structure_and_retrieval", "label": "结构", "weight": 25}
        ],
        "bands": [{"minimum": 80, "label": "优先"},
                  {"minimum": 0, "label": "待提升"}]
    }, ensure_ascii=False), encoding="utf-8")
    return repo


class FixBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-fix-"))
        self.store = Store(self.tmp / "data")
        self.distill = Distill(self.store, make_repo(self.tmp))
        self.project = self.store.create_project("甲项目", owner="张三")
        submit = self.distill.submit_draft

        def submit_word_fixture(task_id, title, knowledge_type, body_md, sources,
                                model=None, knowledge_id=None, topics=None,
                                quality_report=None, products="测试产品",
                                domain=None, request_id=None):
            task = self.distill.get_task(task_id)
            for material in task["materials"]:
                scanned = self.store.conn.execute(
                    "SELECT 1 FROM task_office_scans WHERE task_id=? AND file_id=?",
                    (task_id, material["file_id"])).fetchone()
                if not scanned:
                    self.distill.inspect_word(task_id, material["file_id"])
            if quality_report is None:
                quality_report = {"asset_reviews": [], "flowcharts": [], "table_checks": []}
            return submit(task_id, title, knowledge_type, body_md, sources,
                          model=model, knowledge_id=knowledge_id, topics=topics,
                          quality_report=quality_report, products=products,
                          domain=domain if domain is not None else ["测试领域"],
                          request_id=request_id)

        self.distill.submit_draft = submit_word_fixture

    def _file(self, name: str, content, encoding: str | None = None) -> Path:
        path = self.tmp / name
        if path.suffix.lower() == ".md":
            text = content if isinstance(content, str) else content.decode(encoding or "utf-8", errors="replace")
            return write_minimal_docx(path.with_suffix(".docx"), text)
        if encoding:
            path.write_text(content, encoding=encoding)
        else:
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def _upload(self, path: Path, project=None, **kwargs):
        return self.store.upload(path, project or self.project["project_id"], **kwargs)["uploaded"][0]

    def _task(self, stage="调研与需求"):
        return self.distill.start_task(self.project["project_id"], stage)

    def _sources(self, upload, **kwargs):
        entry = {"version_id": upload["version_id"], "location": "全文",
                 "supports": "示例结论"}
        entry.update(kwargs)
        return [entry]


class F01ReadOutGuard(FixBase):
    def test_out_inside_data_dir_rejected_and_original_intact(self):
        first = self._upload(self._file("a.md", "甲文件内容"))
        second = self._upload(self._file("b.md", "乙文件内容"))
        blob = self.store.data_dir / "originals" / first["sha256"][:2] / first["sha256"]
        before = blob.read_bytes()
        with self.assertRaises(ValueError):
            self.store.read_version(second["file_id"], out_path=blob)
        self.assertEqual(before, blob.read_bytes())
        self.store.read_version(second["file_id"], out_path=self.tmp / "out" / "b.md")


class F02CrossProjectRevision(FixBase):
    def test_cross_project_revision_rejected(self):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        draft = self.distill.submit_draft(task["task_id"], "知识甲", "方案设计",
                                          BODY, self._sources(upload))
        other = self.store.create_project("乙项目")
        other_upload = self.store.upload(self._file("a.md", "内容"), other["project_id"])["uploaded"][0]
        other_task = self.distill.start_task(other["project_id"], "运维与改进")
        self.distill.attach(other_task["task_id"], other_upload["file_id"])
        with self.assertRaises(ValueError) as ctx:
            self.distill.submit_draft(other_task["task_id"], "知识乙", "问题案例",
                                      BODY + "乙", self._sources(other_upload),
                                      knowledge_id=draft["knowledge_id"])
        self.assertIn("跨项目", str(ctx.exception))
        still = self.distill.get_draft(draft["knowledge_id"])
        self.assertEqual(1, still["revision"])
        self.assertEqual(self.project["project_id"], still["project_id"])


class F03TempPathCollision(FixBase):
    def test_same_name_materials_get_distinct_copies(self):
        src_dir = self.tmp / "batch"
        (src_dir / "a").mkdir(parents=True)
        (src_dir / "b").mkdir(parents=True)
        write_minimal_docx(src_dir / "a" / "source.docx", "A 项目材料内容")
        write_minimal_docx(src_dir / "b" / "source.docx", "B 文件不同内容")
        upload = self.store.upload(src_dir, self.project["project_id"])["uploaded"]
        first, second = upload[0], upload[1]
        task = self._task()
        self.distill.attach(task["task_id"], first["file_id"])
        self.distill.attach(task["task_id"], second["file_id"])
        copy1 = self.distill.material_copy(task["task_id"], first["file_id"])
        copy2 = self.distill.material_copy(task["task_id"], second["file_id"])
        self.assertNotEqual(copy1["path"], copy2["path"])
        with zipfile.ZipFile(copy1["path"]) as archive:
            self.assertIn("A 项目材料内容", archive.read("word/document.xml").decode())
        with zipfile.ZipFile(copy2["path"]) as archive:
            self.assertIn("B 文件不同内容", archive.read("word/document.xml").decode())

    def test_cross_project_attach_rejected(self):
        upload = self._upload(self._file("a.md", "甲"))
        other = self.store.create_project("乙项目")
        other_upload = self.store.upload(self._file("b.md", "乙"), other["project_id"])["uploaded"][0]
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        with self.assertRaises(ValueError):
            self.distill.attach(task["task_id"], other_upload["file_id"])


class F04SectionCheckPersisted(FixBase):
    def test_missing_known_survives_to_export(self):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        draft = self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                          self._sources(upload))
        self.assertTrue(draft["section_check"]["missing_known"])
        # 新实例模拟独立进程导出
        fresh = Distill(Store(self.tmp / "data"), self.tmp / "repo")
        project = self.store.get_project(self.project["project_id"])
        out = export_draft(fresh.get_draft(draft["knowledge_id"]), project, self.tmp / "out")
        sidecar = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
        self.assertEqual(draft["section_check"]["missing_known"],
                         sidecar["section_check"]["missing_known"])

    def test_common_equivalent_headings_count_as_covered(self):
        body = """# 项目知识
## 一、知识定位与适用边界
适用范围与材料性质。
## 三、基础数据的现状问题与建设思路
现状问题与目标说明。
## 四、供应链模块方案
业务方案与步骤。
## 七、流程图复原
流程资产说明。
## 八、冲突、缺口与待确认项
暂无冲突事项；缺口待补充。
"""
        missing_required, missing_known, invalid_required = check_body(body)
        self.assertEqual(missing_required, [])
        self.assertEqual(invalid_required, [])
        self.assertNotIn("身份与适用范围", missing_known)
        self.assertNotIn("背景、问题与目标", missing_known)
        self.assertNotIn("流程、方案与选择依据", missing_known)
        self.assertNotIn("执行步骤或问题解决过程", missing_known)
        self.assertNotIn("实际结果与适用边界", missing_known)
        self.assertNotIn("图像与流程资产", missing_known)


class F05SnapshotAndClosedFreeze(FixBase):
    def test_revision_export_stable_and_closed_frozen(self):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        self.distill.declare_gap(task["task_id"], "提交前缺口")
        draft = self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                          self._sources(upload))
        project = self.store.get_project(self.project["project_id"])
        out1 = export_draft(self.distill.get_draft(draft["knowledge_id"]), project,
                            self.tmp / "out")
        bytes1 = (Path(out1["md"]).read_bytes(), Path(out1["json"]).read_bytes())
        # 提交后新增补充：不进入已提交修订
        self.distill.add_input(task["task_id"], "后补人", "提交后才来的信息")
        out2 = export_draft(self.distill.get_draft(draft["knowledge_id"]), project,
                            self.tmp / "out")
        bytes2 = (Path(out2["md"]).read_bytes(), Path(out2["json"]).read_bytes())
        self.assertEqual(bytes1, bytes2)
        sidecar = json.loads(bytes2[1].decode("utf-8"))
        self.assertEqual([], sidecar["human_inputs"])
        self.assertEqual("提交前缺口", sidecar["gaps"][0]["description"])
        # 关闭后全部冻结
        self.distill.cleanup_task(task["task_id"])
        with self.assertRaises(ValueError):
            self.distill.add_input(task["task_id"], "再补", "关闭后补充")
        with self.assertRaises(ValueError):
            self.distill.declare_gap(task["task_id"], "关闭后缺口")
        with self.assertRaises(ValueError):
            self.distill.attach(task["task_id"], upload["file_id"])
        with self.assertRaises(ValueError):
            self.distill.material_copy(task["task_id"], upload["file_id"])
        out3 = export_draft(self.distill.get_draft(draft["knowledge_id"]), project,
                            self.tmp / "out")
        self.assertEqual(bytes1, (Path(out3["md"]).read_bytes(), Path(out3["json"]).read_bytes()))


class F06RulesSnapshotPerTask(FixBase):
    def test_task_uses_snapshot_rules(self):
        repo = self.tmp / "repo"
        task = self._task()
        rules_path = repo / "templates" / "蒸馏规范" / "rules.json"
        manifest = json.loads(rules_path.read_text(encoding="utf-8"))
        manifest["version"] = "fix-rules-v999"
        rules_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        pinned = self.distill.load_rules(task["task_id"])
        self.assertEqual("fix-rules-v1", pinned["version"])
        guidance = self.distill.guidance(task["task_id"])
        self.assertEqual("fix-rules-v1", guidance["rules_version"])
        self.assertIn("只读任务材料", guidance["rules_files"]["规范.md"])
        new_task = self.distill.start_task(self.project["project_id"], "方案与设计")
        self.assertEqual("fix-rules-v999", new_task["rules_version"])


class F07SourceShaContract(FixBase):
    def setUp(self):
        super().setUp()
        self.upload = self._upload(self._file("a.md", "内容"))
        self.task = self._task()
        self.distill.attach(self.task["task_id"], self.upload["file_id"])

    def test_explicit_wrong_sha_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.distill.submit_draft(self.task["task_id"], "知识", "方案设计", BODY,
                                      self._sources(self.upload, sha256="0" * 64))
        self.assertIn("sha256", str(ctx.exception))

    def test_correct_sha_marked_client_verified(self):
        draft = self.distill.submit_draft(
            self.task["task_id"], "知识", "方案设计", BODY,
            self._sources(self.upload, sha256=self.upload["sha256"]))
        self.assertEqual("client_verified", draft["sources"][0]["sha256_provenance"])

    def test_omitted_sha_filled_by_service(self):
        draft = self.distill.submit_draft(self.task["task_id"], "知识", "方案设计",
                                          BODY, self._sources(self.upload))
        self.assertEqual(self.upload["sha256"], draft["sources"][0]["sha256"])
        self.assertEqual("service_filled", draft["sources"][0]["sha256_provenance"])

    def test_empty_supports_are_autofilled_and_lower_source_score(self):
        draft = self.distill.submit_draft(
            self.task["task_id"], "知识", "方案设计", BODY,
            self._sources(self.upload, supports="  "))
        self.assertEqual("本次蒸馏参考；具体结论与原文位置尚未逐条映射",
                         draft["sources"][0]["supports"])
        self.assertEqual(3, next(d["score"] for d in draft["quality_check"]["dimensions"]
                                 if d["key"] == "source_traceability"))


class F08VerifyBytesAndExitCode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-f08-"))
        self.store = Store(self.tmp / "data")
        self.distill = Distill(self.store, make_repo(self.tmp))
        self.project = self.store.create_project("项目")
        upload = self.store.upload(write_minimal_docx(self.tmp / "a.docx", "内容"), "项目")["uploaded"][0]
        task = self.distill.start_task("项目", "调研与需求")
        self.distill.attach(task["task_id"], upload["file_id"])
        self.distill.inspect_word(task["task_id"], upload["file_id"])
        sources = [{"version_id": upload["version_id"], "location": "全文",
                    "supports": "结论"}]
        draft = self.distill.submit_draft(
            task["task_id"], "知识", "方案设计", BODY, sources,
            quality_report={"asset_reviews": [], "flowcharts": [], "table_checks": []},
            products="测试产品", domain=["测试领域"])
        out = export_draft(self.distill.get_draft(draft["knowledge_id"]),
                           self.store.get_project("项目"), self.tmp / "out")
        self.md_path = Path(out["md"])
        self.raw = self.md_path.read_bytes()

    def _file(self, name, content):
        path = self.tmp / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def _cli_verify(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["draft", "verify", str(self.md_path)])
        return code, buffer.getvalue()

    def test_tampered_body_exits_nonzero(self):
        self.md_path.write_bytes(self.raw + "篡改".encode("utf-8"))
        code, output = self._cli_verify()
        self.assertEqual(1, code)
        self.assertIn('"match": false', output)

    def test_crlf_body_detected(self):
        self.md_path.write_bytes(self.raw.replace(b"\n", b"\r\n"))
        code, output = self._cli_verify()
        self.assertEqual(1, code)
        self.assertIn('"match": false', output)

    def test_clean_file_exits_zero(self):
        code, _ = self._cli_verify()
        self.assertEqual(0, code)


class F09CorruptBlobReuse(FixBase):
    def test_reupload_rejects_corrupted_blob(self):
        upload = self._upload(self._file("a.md", "原始内容"))
        blob = self.store.data_dir / "originals" / upload["sha256"][:2] / upload["sha256"]
        original = blob.read_bytes()
        blob.write_bytes(b"CORRUPTED")
        uploads_before = len(self.store.get_file(upload["file_id"])["uploads"])
        with self.assertRaises(ValueError):
            self.store.upload(self._file("a.md", "原始内容"), self.project["project_id"])
        self.assertEqual(uploads_before,
                         len(self.store.get_file(upload["file_id"])["uploads"]))
        blob.write_bytes(original)
        again = self._upload(self._file("a.md", "原始内容"))
        self.assertEqual("reused", again["action"])


class F10UploadContextPolicy(FixBase):
    def test_reupload_records_context_without_mutating_file(self):
        upload = self._upload(self._file("a.md", "内容"), stage="调研与需求", tags=["初始"])
        again = self._upload(self._file("a.md", "内容"), stage="实施与变更", tags=["新标签"])
        self.assertFalse(again["upload_context"]["applied_to_file"])
        record = self.store.get_file(upload["file_id"])
        self.assertEqual("调研与需求", record["stage"])
        self.assertEqual(["初始"], record["tags"])
        latest = record["uploads"][-1]
        self.assertEqual("实施与变更", latest["stage"])
        self.assertEqual(["新标签"], latest["tags"])
        hits = self.store.search_files(upload_tag="新标签")
        self.assertEqual(1, len(hits))


class F11LegacyEncoding(FixBase):
    def test_non_word_file_is_stored_but_cannot_be_attached(self):
        raw = "现场补充：条码枪".encode("gb18030")
        upload = self._upload(self._file("old.txt", raw))
        task = self._task()
        with self.assertRaisesRegex(ValueError, "仅支持 Word 文件"):
            self.distill.attach(task["task_id"], upload["file_id"])
        out = self.tmp / "out" / "old.txt"
        self.store.read_version(upload["file_id"], out_path=out)
        self.assertEqual(raw, out.read_bytes())


class F12SectionCheckStrength(FixBase):
    def _submit_body(self, body):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        return self.distill.submit_draft(task["task_id"], "知识", "方案设计", body,
                                         self._sources(upload))

    def test_empty_required_sections_lower_score(self):
        body = "## 来源映射\n\n## 冲突、缺口与待确认项\n\n## 处理记录\n"
        draft = self._submit_body(body)
        self.assertEqual("scored", draft["quality_check"]["status"])
        self.assertTrue(draft["quality_check"]["flags"])

    def test_headers_inside_fenced_code_are_scored(self):
        fenced = "```markdown\n" + BODY + "```"
        draft = self._submit_body(fenced)
        self.assertEqual("draft", draft["status"])

    def test_source_mapping_and_processing_record_are_not_body_requirements(self):
        draft = self._submit_body(BODY)
        self.assertEqual([], draft["section_check"]["missing_required"])
        self.assertEqual([], draft["section_check"]["invalid_required"])

    def test_na_with_reason_passes(self):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        body = BODY + "\n## 图像与流程资产\n\n不适用：本任务材料为纯文本纪要，无图像。\n"
        draft = self.distill.submit_draft(task["task_id"], "知识", "方案设计", body,
                                          self._sources(upload))
        self.assertNotIn("图像与流程资产", draft["section_check"]["missing_known"])


class F13OfficeMetadataPersisted(FixBase):
    def test_author_recoverable_after_reopen(self):
        doc = self.tmp / "meta.docx"
        with zipfile.ZipFile(doc, "w") as archive:
            archive.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                ' xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>作者甲</dc:creator></cp:coreProperties>')
        upload = self._upload(doc)
        fresh = Store(self.tmp / "data")
        record = fresh.get_file(upload["file_id"])
        self.assertEqual("作者甲", record["versions"][0]["doc_meta"].get("author"))


class F14FrontmatterTypes(unittest.TestCase):
    def test_single_element_list_stays_list(self):
        from ckp.md_render import render_frontmatter
        text = render_frontmatter({"products": "one", "domain": ["领域一"]}) + "正文\n"
        meta, _ = split_frontmatter(text)
        self.assertEqual("one", meta["products"])
        self.assertEqual(["领域一"], meta["domain"])

    def test_zero_and_many(self):
        from ckp.md_render import render_frontmatter
        text = render_frontmatter({"topics": ["ignored"], "industry": ["a", "b"],
                                   "products": "ERP", "domain": ["财务会计", "供应链"]}) + "x\n"
        meta, _ = split_frontmatter(text)
        self.assertEqual("ERP", meta["products"])
        self.assertEqual(["财务会计", "供应链"], meta["domain"])
        self.assertNotIn("topics", meta)
        self.assertNotIn("industry", meta)


class F15RequestIdempotency(FixBase):
    def test_start_task_replay_and_conflict(self):
        one = self.distill.start_task(self.project["project_id"], "调研与需求",
                                      request_id="req-1")
        replay = self.distill.start_task(self.project["project_id"], "调研与需求",
                                         request_id="req-1")
        self.assertEqual(one["task_id"], replay["task_id"])
        tasks = self.store.conn.execute("SELECT COUNT(*) FROM distill_tasks").fetchone()[0]
        self.assertEqual(1, tasks)
        with self.assertRaises(ValueError):
            self.distill.start_task(self.project["project_id"], "方案与设计",
                                    request_id="req-1")

    def test_gap_and_draft_replay(self):
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        self.distill.declare_gap(task["task_id"], "同一缺口", request_id="g-1")
        self.distill.declare_gap(task["task_id"], "同一缺口", request_id="g-1")
        gaps = len(self.distill.get_task(task["task_id"])["gaps"])
        self.assertEqual(1, gaps)
        sources = self._sources(upload)
        draft = self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                          sources, request_id="d-1")
        again = self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                          sources, request_id="d-1")
        self.assertEqual(draft["knowledge_id"], again["knowledge_id"])
        self.assertEqual(1, again["revision"])
        rows = self.store.conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE knowledge_id = ?",
            (draft["knowledge_id"],)).fetchone()[0]
        self.assertEqual(1, rows)


class F16ReadOnlyQueryNoSideEffects(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-f16-"))
        self._old = {k: os.environ.get(k) for k in ("CKP_DATA", "CKP_OUTPUT")}
        os.environ["CKP_DATA"] = str(self.tmp / "never-created")
        os.environ["CKP_OUTPUT"] = str(self.tmp / "out")

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_verify_creates_nothing(self):
        md = self.tmp / "x.md"
        md.write_text("---\ncontent_hash: \"0\"\n---\n正文\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["draft", "verify", str(md)])
        self.assertEqual(1, code)
        self.assertFalse((self.tmp / "never-created").exists())
        self.assertIn('"match": false', buffer.getvalue())

    def test_query_on_missing_data_dir_returns_empty_without_creating(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["file", "list"])
        self.assertEqual(0, code)
        self.assertEqual("", buffer.getvalue().strip())
        self.assertFalse((self.tmp / "never-created").exists())


class S09AllExcludedUpload(FixBase):
    def test_zero_accepted_files_raise(self):
        temp = self._file("~$only.docx", b"temp")
        with self.assertRaises(ValueError) as ctx:
            self.store.upload(temp, self.project["project_id"])
        self.assertIn("没有可保存的文件", str(ctx.exception))


class R01IdempotentAtomicity(FixBase):
    """docs/07 R01：request_log 写入失败时业务写入必须一并回滚。"""

    def _block_log(self):
        self.store.conn.execute(
            "CREATE TRIGGER block_request_log BEFORE INSERT ON request_log"
            " BEGIN SELECT RAISE(ABORT, 'blocked'); END;")
        self.store.conn.commit()

    def _unblock_log(self):
        self.store.conn.execute("DROP TRIGGER block_request_log")
        self.store.conn.commit()

    def test_start_task_log_failure_rolls_back(self):
        import sqlite3
        self._block_log()
        with self.assertRaises(sqlite3.Error):
            self.distill.start_task(self.project["project_id"], "调研与需求",
                                    request_id="atomic-1")
        conn = self.store.conn
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM distill_tasks").fetchone()[0])
        snapshots = self.store.data_dir / "rules_snapshots"
        self.assertEqual([], list(snapshots.glob("*")) if snapshots.exists() else [])
        self._unblock_log()
        task = self.distill.start_task(self.project["project_id"], "调研与需求",
                                       request_id="atomic-1")
        replay = self.distill.start_task(self.project["project_id"], "调研与需求",
                                         request_id="atomic-1")
        self.assertEqual(task["task_id"], replay["task_id"])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM distill_tasks").fetchone()[0])

    def test_submit_draft_log_failure_rolls_back(self):
        import sqlite3
        upload = self._upload(self._file("a.md", "内容"))
        task = self._task()
        self.distill.attach(task["task_id"], upload["file_id"])
        self._block_log()
        with self.assertRaises(sqlite3.Error):
            self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                      self._sources(upload), request_id="atomic-2")
        conn = self.store.conn
        self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0])
        self.assertEqual("materials_attached", self.distill.get_task(task["task_id"])["status"])
        self._unblock_log()
        draft = self.distill.submit_draft(task["task_id"], "知识", "方案设计", BODY,
                                          self._sources(upload), request_id="atomic-2")
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0])


class C01C02RulesContent(unittest.TestCase):
    def test_rules_v03_contains_discipline(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "templates" / "蒸馏规范" / "通用蒸馏规范.md").read_text(encoding="utf-8")
        self.assertIn("distill-rules-v0.10", text)
        self.assertIn("未见证据", text)
        self.assertIn("不得写\"均未签署\"", text)
        self.assertIn("逐行清点", text)          # R02
        self.assertIn("knowledge_id+revision", text)  # R03
        self.assertIn("未查看附件", text)         # R04
        self.assertIn("逐句分离", text)           # R05
        self.assertIn("五要素", text)             # R06
        self.assertIn("逐边回对", text)           # R07
        checklist = (root / "templates" / "蒸馏规范" / "质量检查单.md").read_text(encoding="utf-8")
        self.assertIn("三态", checklist)
        manifest = json.loads((root / "templates" / "蒸馏规范" / "rules.json").read_text(encoding="utf-8"))
        self.assertEqual("distill-rules-v0.10", manifest["version"])


if __name__ == "__main__":
    unittest.main()
