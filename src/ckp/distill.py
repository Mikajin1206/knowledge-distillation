"""蒸馏任务与草稿：按阶段组织材料、引用版本、记录缺口与人工补充、提交带来源草稿。

职责边界（与需求规格 4.2/4.3 对应）：
- 本模块管理任务、材料引用、规范版本、草稿与校验；不生成正文。
- 正文由客户端模型按规范生成后经 submit_draft 提交；提交时校验来源必须
  来自任务材料且版本一致（客户端可自报 sha256，不一致拒绝；省略则由服务
  补登记哈希并标注来源），缺少推荐章节记入评分但不阻止草稿。
- 任务开启时保存规范与评分权重不可变快照；任务关闭后材料、缺口、补充、草稿均冻结。
- 草稿提交时快照人工补充与缺口，同一修订的导出字节可重复。
- 草稿只是草稿：不写 RAG、不进索引、不表示已审核；哈希通过不等于事实正确。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from ckp import db
from ckp.intake import hash_file, now_iso
from ckp.quality import load_score_policy, read_score_snapshot, score_quality, write_score_snapshot

# 与需求规格 4.2 的六个阶段一致；任务必须落在一个阶段，材料允许跨阶段。
STAGES = ("立项与范围", "调研与需求", "方案与设计", "实施与变更", "测试与验收", "运维与改进")

# 正文只要求与知识适用性直接相关的冲突/缺口说明；来源、评分和处理证据由 sidecar 承载。
REQUIRED_SECTIONS = ("冲突、缺口与待确认项",)
KNOWN_SECTIONS = (
    "身份与适用范围", "背景、问题与目标", "流程、方案与选择依据",
    "执行步骤或问题解决过程", "实际结果与适用边界", "图像与流程资产",
    "关联知识与过程变化", "冲突、缺口与待确认项",
)

# 客户端可使用自然的项目文档标题。将常见等价标题映射到模板章节，避免
# 已覆盖的内容因标题措辞不同而出现在 missing_known 中。
SECTION_ALIASES = {
    "身份与适用范围": ("知识定位与适用边界",),
    "背景、问题与目标": ("现状问题与建设思路",),
    "流程、方案与选择依据": ("方案范围", "供应链模块方案", "财务模块方案", "接口方案"),
    "执行步骤或问题解决过程": ("实施落地思路", "供应链模块方案", "采购管理", "销售管理",
                            "内部交易", "库存管理", "生产制造", "固定资产", "现金管理"),
    "实际结果与适用边界": ("知识定位与适用边界", "实际结果",),
    "图像与流程资产": ("流程图复原", "图像与流程图"),
}

RULES_DIRNAME = "蒸馏规范"
_TEXT_SUFFIXES = (".md", ".txt")
DISTILLABLE_WORD_SUFFIXES = frozenset({".doc", ".docx"})


def _require_word_file(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in DISTILLABLE_WORD_SUFFIXES:
        supported = ", ".join(sorted(DISTILLABLE_WORD_SUFFIXES))
        raise ValueError(
            f"当前蒸馏仅支持 Word 文件（{supported}）；"
            f"该原件仍可保存，但不解析、不提供给本地模型蒸馏: {filename}。"
            "请停止当前蒸馏；若同时选择了 Word，请先询问用户是否只用 Word 继续。"
        )


class Distill:
    def __init__(self, store, repo_root: Path):
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self.rules_dir = self.repo_root / "templates" / RULES_DIRNAME
        self.snapshots_dir = self.store.data_dir / "rules_snapshots"
        self.score_policy_path = self.rules_dir / "评分权重.json"
        self.score_snapshots_dir = self.store.data_dir / "score_snapshots"

    # ---------- 规范（任务开启时快照，历史任务不受后续规范修改影响） ----------

    def _read_rules_from(self, base: Path) -> dict:
        manifest_path = base / "rules.json"
        if not manifest_path.is_file():
            raise ValueError(f"蒸馏规范清单缺失: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files: dict[str, str] = {}
        for name in manifest.get("files", []):
            path = base / name
            if not path.is_file():
                raise ValueError(f"蒸馏规范文件缺失: {path}")
            files[name] = path.read_text(encoding="utf-8")
        rules_hash = hash_file(manifest_path) + ":" + ",".join(
            f"{name}:{_short_hash(text)}" for name, text in sorted(files.items()))
        return {"version": manifest.get("version", "unknown"),
                "hash": rules_hash, "dir": str(base), "files": files}

    def load_rules(self, task_id: str | None = None) -> dict:
        """task_id 给定时读取该任务的不可变规范快照；否则读取当前规范。"""
        if task_id:
            base = self.snapshots_dir / task_id
            if not (base / "rules.json").is_file():
                raise ValueError(f"任务规范快照缺失: {base}（旧任务或数据目录被改动）")
            return self._read_rules_from(base)
        return self._read_rules_from(self.rules_dir)

    def _snapshot_rules(self, task_id: str, rules: dict) -> None:
        base = self.snapshots_dir / task_id
        base.mkdir(parents=True, exist_ok=True)
        (base / "rules.json").write_text(
            (self.rules_dir / "rules.json").read_text(encoding="utf-8"), encoding="utf-8")
        for name in rules["files"]:
            (base / name).write_text(rules["files"][name], encoding="utf-8")

    # ---------- 任务 ----------

    def start_task(self, project_ref: str, stage: str, notes: str = "",
                   request_id: str | None = None) -> dict:
        self.store._require_write()
        payload = {"project_ref": project_ref, "stage": stage, "notes": notes}
        task_id = db.new_id("t")
        rules = self.load_rules()
        score_policy = load_score_policy(self.score_policy_path)
        snapshot_base = self.snapshots_dir / task_id
        score_snapshot_base = self.score_snapshots_dir / task_id

        def run() -> dict:
            project = self.store.get_project(project_ref)
            clean_stage = (stage or "").strip()
            if clean_stage not in STAGES:
                raise ValueError(f"阶段必须是 {', '.join(STAGES)} 之一，收到: {clean_stage or '(空)'}")
            now = now_iso()
            self.store.conn.execute(
                "INSERT INTO distill_tasks (task_id, project_id, stage, status, rules_version, rules_hash,"
                " notes, created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (task_id, project["project_id"], clean_stage, rules["version"], rules["hash"],
                 notes or "", now, now))
            self._snapshot_rules(task_id, rules)
            write_score_snapshot(self.store.data_dir, task_id, score_policy)
            return self.get_task(task_id)

        # R01：快照文件在事务内写入；提交失败时随回滚清理，不留孤儿目录。
        def discard_snapshot() -> None:
            shutil.rmtree(snapshot_base, ignore_errors=True)
            shutil.rmtree(score_snapshot_base, ignore_errors=True)

        return db.idempotent(self.store.conn, request_id, "start_task", payload, run,
                             on_rollback=discard_snapshot)

    def get_task(self, task_id: str) -> dict:
        record = db.rowdict(self.store.conn.execute(
            "SELECT t.*, p.name AS project_name, p.owner AS project_owner FROM distill_tasks t"
            " JOIN projects p ON p.project_id = t.project_id WHERE t.task_id = ?",
            (task_id,)).fetchone())
        if not record:
            raise ValueError(f"蒸馏任务不存在: {task_id}")
        record["materials"] = db.rowdicts(self.store.conn.execute(
            "SELECT * FROM task_materials WHERE task_id = ? ORDER BY material_id",
            (task_id,)).fetchall())
        record["gaps"] = db.rowdicts(self.store.conn.execute(
            "SELECT * FROM task_gaps WHERE task_id = ? ORDER BY declared_at",
            (task_id,)).fetchall())
        record["human_inputs"] = db.rowdicts(self.store.conn.execute(
            "SELECT * FROM task_inputs WHERE task_id = ? ORDER BY created_at",
            (task_id,)).fetchall())
        record["drafts"] = db.rowdicts(self.store.conn.execute(
            "SELECT knowledge_id, revision, title, knowledge_type, status, created_at, content_hash"
            " FROM drafts WHERE task_id = ? ORDER BY knowledge_id, revision",
            (task_id,)).fetchall())
        record["office_scans"] = db.rowdicts(self.store.conn.execute(
            "SELECT file_id, version_id, sha256, parser_version, created_at"
            " FROM task_office_scans WHERE task_id = ? ORDER BY file_id",
            (task_id,)).fetchall())
        return record

    def attach(self, task_id: str, file_id: str, version_id: str | None = None,
               note: str = "", request_id: str | None = None) -> dict:
        self.store._require_write()
        payload = {"task_id": task_id, "file_id": file_id,
                   "version_id": version_id, "note": note}

        def run() -> dict:
            task = self.get_task(task_id)
            if task["status"] == "closed":
                raise ValueError("任务已关闭，不能追加材料")
            frec = self.store.get_file(file_id)
            if frec["project_id"] != task["project_id"]:
                raise ValueError(
                    f"材料必须属于任务项目（任务项目 {task['project_id']}，"
                    f"文件属于 {frec['project_id']}）；跨项目参考材料暂不支持")
            version = self.store.get_version(file_id, version_id)
            _require_word_file(version["filename"])
            existing_material = next(
                (m for m in task["materials"] if m["file_id"] == file_id), None)
            if existing_material:
                raise ValueError(
                    f"文件已按版本关联到本任务（{existing_material['version_id']}）；"
                    "材料按关联时版本钉住，不重复关联")
            duplicate = db.rowdict(self.store.conn.execute(
                "SELECT * FROM task_materials WHERE task_id = ? AND version_id = ?",
                (task_id, version["version_id"])).fetchone())
            if duplicate:
                raise ValueError(f"该版本已关联: {file_id}/{version['version_id']}")
            now = now_iso()
            self.store.conn.execute(
                "INSERT INTO task_materials (task_id, file_id, version_id, sha256, filename, note, attached_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, file_id, version["version_id"], version["sha256"],
                 version["filename"], note or "", now))
            self.store.conn.execute(
                "UPDATE distill_tasks SET status = 'materials_attached', updated_at = ? WHERE task_id = ?"
                " AND status = 'open'", (now, task_id))
            return {"task_id": task_id, "file_id": file_id,
                    "version_id": version["version_id"], "sha256": version["sha256"],
                    "filename": version["filename"]}

        return db.idempotent(self.store.conn, request_id, "attach", payload, run)

    def declare_gap(self, task_id: str, description: str,
                    request_id: str | None = None) -> dict:
        self.store._require_write()
        payload = {"task_id": task_id, "description": description}

        def run() -> dict:
            task = self.get_task(task_id)
            if task["status"] == "closed":
                raise ValueError("任务已关闭，不能追加缺口")
            clean = (description or "").strip()
            if not clean:
                raise ValueError("缺口描述不能为空")
            gap = {"gap_id": db.new_id("g"), "task_id": task_id,
                   "description": clean, "declared_at": now_iso(), "resolved": 0}
            self.store.conn.execute(
                "INSERT INTO task_gaps (gap_id, task_id, description, declared_at, resolved)"
                " VALUES (:gap_id, :task_id, :description, :declared_at, :resolved)", gap)
            return gap

        return db.idempotent(self.store.conn, request_id, "declare_gap", payload, run)

    def add_input(self, task_id: str, provider: str, content: str,
                  request_id: str | None = None) -> dict:
        self.store._require_write()
        payload = {"task_id": task_id, "provider": provider, "content": content}

        def run() -> dict:
            task = self.get_task(task_id)
            if task["status"] == "closed":
                raise ValueError("任务已关闭，不能追加人工补充")
            clean_provider = (provider or "").strip()
            clean_content = (content or "").strip()
            if not clean_provider:
                raise ValueError("人工补充必须记录提供人")
            if not clean_content:
                raise ValueError("人工补充内容不能为空")
            record = {"input_id": db.new_id("i"), "task_id": task_id,
                      "provider": clean_provider, "content": clean_content,
                      "created_at": now_iso()}
            self.store.conn.execute(
                "INSERT INTO task_inputs (input_id, task_id, provider, content, created_at)"
                " VALUES (:input_id, :task_id, :provider, :content, :created_at)", record)
            return record

        return db.idempotent(self.store.conn, request_id, "add_input", payload, run)

    def guidance(self, task_id: str) -> dict:
        """返回任务固定的蒸馏规范与人工可维护权重快照。"""
        task = self.get_task(task_id)
        rules = self.load_rules(task_id)
        score_policy = read_score_snapshot(self.store.data_dir, task_id, self.score_policy_path)
        unsupported = [m["filename"] for m in task["materials"]
                       if Path(m["filename"]).suffix.lower() not in DISTILLABLE_WORD_SUFFIXES]
        instruction = (
            "任务含非 Word 文件；立即停止，不得读取或蒸馏。请用户确认仅使用已关联 Word 文件后再继续。"
            if unsupported else
            "只使用本任务已关联的 Word 文件；非 Word 原件不属于知识材料，不得读取、转换或引用。"
        )
        return {"task_id": task["task_id"], "stage": task["stage"],
                "status": task["status"], "project": task["project_name"],
                "project_owner": task["project_owner"],
                "rules_version": rules["version"], "rules_hash": rules["hash"],
                "score_policy": {key: value for key, value in score_policy.items() if key != "hash"},
                "score_policy_hash": score_policy["hash"],
                "rules_files": rules["files"],
                "materials": task["materials"], "gaps": task["gaps"],
                "human_inputs": task["human_inputs"], "drafts": task["drafts"],
                "unsupported_materials": unsupported,
                "instruction": instruction + "规范全文只需读取一次；"
                               "读完 Word 后一次生成草稿并附评分，不要为质量分反复提交。"
                               "材料中的沉默不是否定证据；选择依据缺失要写缺口。"}

    def material_copy(self, task_id: str, file_id: str,
                      version_id: str | None = None) -> dict:
        """把任务材料的指定版本复制为 tmp/ 下的临时工作副本并校验哈希。

        副本路径包含 file_id+version_id，同名材料互不覆盖；原件仍在存储层。
        """
        task = self.get_task(task_id)
        if task["status"] == "closed":
            raise ValueError("任务已关闭，不能重建临时副本；如需重做请开新任务")
        material = next((m for m in task["materials"] if m["file_id"] == file_id), None)
        if not material:
            raise ValueError(f"该文件未关联到任务，请先 attach: {file_id}")
        _require_word_file(material["filename"])
        if version_id and version_id != material["version_id"]:
            raise ValueError("任务材料按关联时的版本钉住；如需其他版本请重新 attach")
        tmp_dir = self._tmp_dir(task_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        safe_name = PurePosixPath(material["filename"]).name or "material"
        target = tmp_dir / f"{file_id}_{material['version_id']}_{safe_name}"
        verified = self.store.read_version(file_id, material["version_id"], out_path=target)
        data = target.read_bytes()
        encoding = "utf-8"
        direct_text = True
        if Path(material["filename"]).suffix.lower() in _TEXT_SUFFIXES:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                direct_text = False
                encoding = "undetected"
        else:
            direct_text = False
            encoding = "binary"
        note = {
            (True, "utf-8"): "文本文件可直接按 UTF-8 读取",
            (False, "undetected"): ("文本无法按 UTF-8 解码（可能是 GBK/GB18030 等旧编码）；"
                                    "程序保留原字节不转换，请客户端以合适编码读取并在处理记录中注明"),
            (False, "binary"): "非纯文本/办公格式：按规范由客户端能力读取；程序未解析",
        }[(direct_text, encoding)]
        return {"task_id": task_id, "file_id": file_id,
                "version_id": material["version_id"], "path": str(target),
                "sha256": verified["sha256"], "verified": True,
                "direct_text": direct_text, "encoding": encoding,
                "note": note,
                "lifecycle": "临时副本，任务关闭（distill close）时清理"}

    def inspect_word(self, task_id: str, file_id: str,
                     request_id: str | None = None) -> dict:
        """Inspect an attached Word version and its embedded assets under task tmp."""
        self.store._require_write()
        payload = {"task_id": task_id, "file_id": file_id}

        def run() -> dict:
            task = self.get_task(task_id)
            if task["status"] == "closed":
                raise ValueError("任务已关闭，不能解析材料")
            material = next((m for m in task["materials"] if m["file_id"] == file_id), None)
            if not material:
                raise ValueError(f"该文件未关联到任务，请先 attach: {file_id}")
            _require_word_file(material["filename"])
            source = self.material_copy(task_id, file_id)
            from ckp.office_parser import inspect_office
            manifest = inspect_office(
                Path(source["path"]), self._tmp_dir(task_id) / "office" / file_id,
                file_id=file_id, version_id=material["version_id"],
                expected_sha256=material["sha256"])
            now = now_iso()
            self.store.conn.execute(
                "INSERT INTO task_office_scans (task_id,file_id,version_id,sha256,parser_version,manifest_json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id,file_id) DO UPDATE SET"
                " version_id=excluded.version_id, sha256=excluded.sha256,"
                " parser_version=excluded.parser_version, manifest_json=excluded.manifest_json,"
                " created_at=excluded.created_at",
                (task_id, file_id, material["version_id"], material["sha256"],
                 manifest["parser"], db.dumps(manifest), now))
            return {"task_id": task_id, "file_id": file_id,
                    "version_id": material["version_id"], "sha256": material["sha256"],
                    "parser_version": manifest["parser"],
                    "format": manifest["format"], "summary": manifest["summary"],
                    "manifest_path": manifest["manifest_path"],
                    "assets_dir": manifest["assets_dir"],
                    "asset_ids": [a["asset_id"] for a in manifest["assets"]],
                    "conversion": manifest["conversion"]}

        return db.idempotent(self.store.conn, request_id, "inspect_word", payload, run)

    def cleanup_task(self, task_id: str) -> dict:
        self.store._require_write()
        self.get_task(task_id)
        tmp_dir = self._tmp_dir(task_id)
        removed = False
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
            removed = True
        self.store.conn.execute(
            "UPDATE distill_tasks SET status = 'closed', updated_at = ? WHERE task_id = ?",
            (now_iso(), task_id))
        self.store.conn.commit()
        return {"task_id": task_id, "status": "closed", "tmp_removed": removed}

    def _tmp_dir(self, task_id: str) -> Path:
        return self.repo_root / "tmp" / "distill" / task_id

    # ---------- 草稿 ----------

    def submit_draft(self, task_id: str, title: str, knowledge_type: str,
                     body_md: str, sources: list[dict] | None = None, model: str | None = None,
                     knowledge_id: str | None = None, topics: list[str] | None = None,
                     quality_report: dict | None = None,
                     request_id: str | None = None) -> dict:
        self.store._require_write()
        payload = {"task_id": task_id, "title": title, "knowledge_type": knowledge_type,
                   "body_md": body_md, "sources": sources, "model": model,
                   "knowledge_id": knowledge_id, "topics": topics,
                   "quality_report": quality_report}

        def run() -> dict:
            return self._submit_draft(task_id, title, knowledge_type, body_md,
                                      sources, model, knowledge_id, topics, quality_report)

        return db.idempotent(self.store.conn, request_id, "submit_draft", payload, run)

    def _submit_draft(self, task_id: str, title: str, knowledge_type: str,
                      body_md: str, sources: Any, model: str | None,
                      knowledge_id: str | None, topics: list[str] | None,
                      quality_report: dict | None) -> dict:
        task = self.get_task(task_id)
        if task["status"] == "closed":
            raise ValueError("任务已关闭，不能提交草稿")
        if not task["materials"]:
            raise ValueError("任务没有已关联的 Word 材料，无法生成知识草稿")
        unsupported = [m["filename"] for m in task["materials"]
                       if Path(m["filename"]).suffix.lower() not in DISTILLABLE_WORD_SUFFIXES]
        if unsupported:
            raise ValueError(
                "当前蒸馏仅支持 Word 文件（.doc、.docx）；任务含非 Word 材料，"
                "本地模型必须停止蒸馏并请用户确认仅使用 Word 材料: "
                + ", ".join(unsupported[:8])
            )
        title = (title or "").strip()
        knowledge_type = (knowledge_type or "").strip()
        if not title:
            raise ValueError("标题不能为空")
        if not knowledge_type:
            raise ValueError("知识类型不能为空（如：方案设计/问题案例/操作步骤）")
        body_md = str(body_md or "").replace("\r\n", "\n")
        if not body_md.strip():
            raise ValueError("草稿正文不能为空")
        quality_report_snapshot = quality_report if isinstance(quality_report, dict) else {}
        materials = {m["version_id"]: m for m in task["materials"]}
        supplied_sources = isinstance(sources, list) and bool(sources)
        if not supplied_sources:
            sources = [{"file_id": material["file_id"],
                        "version_id": material["version_id"],
                        "location": "全文（未细分定位）",
                        "supports": "本次蒸馏参考；具体结论与原文位置尚未逐条映射"}
                       for material in task["materials"]]
        checked_sources = []
        source_quality = []
        for index, source in enumerate(sources, 1):
            if not isinstance(source, dict):
                raise ValueError(f"第 {index} 条来源必须是对象")
            version_id = str(source.get("version_id") or "").strip()
            if not version_id and len(materials) == 1:
                version_id = next(iter(materials))
            material = materials.get(version_id)
            if not material:
                raise ValueError(
                    f"第 {index} 条来源的版本未关联到本任务: {version_id or '(空)'}；"
                    "来源必须引用任务材料（attach 后提交）")
            given_file = str(source.get("file_id") or "").strip()
            if given_file and given_file != material["file_id"]:
                raise ValueError(f"第 {index} 条来源 file_id 与任务材料不一致: {given_file}")
            client_sha = str(source.get("sha256") or "").strip().lower()
            if client_sha and client_sha != material["sha256"]:
                raise ValueError(
                    f"第 {index} 条来源 sha256 与任务材料登记值不一致"
                    f"（客户端 {client_sha[:12]}…，登记 {material['sha256'][:12]}…）；"
                    "请核实后重提，或省略 sha256 由服务补登记值")
            location = str(source.get("location") or "").strip()
            supports = str(source.get("supports") or "").strip()
            precise = bool(location and supports and supplied_sources)
            location = location or "全文（未细分定位）"
            supports = supports or "本次蒸馏参考；具体结论与原文位置尚未逐条映射"
            source_quality.append(precise)
            checked_sources.append({
                "file_id": material["file_id"], "version_id": material["version_id"],
                "sha256": material["sha256"], "filename": material["filename"],
                "location": location, "supports": supports,
                "sha256_provenance": "client_verified" if client_sha else "service_filled",
                "mapping_precision": "specific" if precise else "coarse",
            })
        missing_required, missing_known, invalid_required = check_body(body_md)
        section_check = {
            "missing_required": missing_required, "missing_known": missing_known,
            "invalid_required": invalid_required,
            "checked_rules_version": task["rules_version"],
            "note": "业务章节缺失或来源绑定不完整作为评分及待处理项记录，不阻止草稿导出",
        }
        score_policy = read_score_snapshot(
            self.store.data_dir, task_id, self.score_policy_path)
        expected_versions = {m["version_id"] for m in task["materials"]}
        linked_versions = {source["version_id"] for source in checked_sources}
        if supplied_sources and expected_versions <= linked_versions and all(source_quality):
            source_score = 5
            source_note = "已关联本任务材料并提供具体位置与支持范围"
        elif checked_sources and supplied_sources:
            source_score = 3
            source_note = "来源版本由服务端绑定；部分位置或支持范围为自动补全"
        elif checked_sources:
            source_score = 1
            source_note = "来源由服务端按任务材料自动补全，定位较粗"
        else:
            source_score = 0
            source_note = "任务没有可绑定的 Word 来源"
        section_score = max(0, 5 - 2 * len(missing_required) - len(invalid_required))
        section_note = ("必备业务章节结构齐全" if section_score == 5 else
                        "缺少或未填完整的章节将作为评分扣分项记录；草稿仍可导出")
        scan_rows = self.store.conn.execute(
            "SELECT manifest_json FROM task_office_scans WHERE task_id = ? ORDER BY file_id",
            (task_id,)).fetchall()
        manifests = [db.loads(row["manifest_json"], {}) for row in scan_rows]
        asset_count = sum(len(item.get("assets", [])) for item in manifests)
        table_count = sum(len(item.get("tables", [])) for item in manifests
                          if item.get("format") == "docx")
        quality_check = score_quality(
            score_policy, quality_report_snapshot, source_score=source_score,
            source_note=source_note, section_score=section_score,
            section_note=section_note, asset_count=asset_count,
            table_count=table_count)
        quality_check["source_autofilled"] = not supplied_sources
        if missing_required:
            quality_check["flags"].append("必备章节缺失: " + "、".join(missing_required))
        if invalid_required:
            quality_check["flags"].extend(invalid_required)
        quality_check["rules_version"] = task["rules_version"]
        content_hash = _short_hash(body_md, full=True)
        if knowledge_id:
            known = db.rowdict(self.store.conn.execute(
                "SELECT * FROM drafts WHERE knowledge_id = ? ORDER BY revision DESC LIMIT 1",
                (knowledge_id,)).fetchone())
            if not known:
                raise ValueError(f"knowledge_id 不存在: {knowledge_id}")
            if known["project_id"] != task["project_id"]:
                raise ValueError(
                    f"knowledge_id 属于项目 {known['project_id']}，不能跨项目续写；"
                    "跨项目复用请新建知识，必要时在新知识中注明派生关系")
            if known["content_hash"] == content_hash and known["task_id"] == task_id:
                raise ValueError("内容与最近修订完全相同，不产生新修订")
            revision = known["revision"] + 1
        else:
            knowledge_id, revision = db.new_id("k"), 1
        now = now_iso()
        # 快照提交时刻的人工补充与缺口：同一修订的导出字节此后不变，
        # 新补充只进入后续修订（docs/05 §4）。
        gaps_snapshot = [{"gap_id": g["gap_id"], "description": g["description"],
                          "declared_at": g["declared_at"], "resolved": g["resolved"]}
                         for g in task["gaps"]]
        inputs_snapshot = [{"input_id": i["input_id"], "provider": i["provider"],
                            "content": i["content"], "created_at": i["created_at"]}
                           for i in task["human_inputs"]]
        record = {
            "knowledge_id": knowledge_id, "revision": revision, "task_id": task_id,
            "project_id": task["project_id"], "stage": task["stage"],
            "title": title, "knowledge_type": knowledge_type,
            "topics": db.dumps(topics or []), "body_md": body_md,
            "content_hash": content_hash, "sources_json": db.dumps(checked_sources),
            "gaps_json": db.dumps(gaps_snapshot),
            "human_inputs_json": db.dumps(inputs_snapshot),
            "section_check_json": db.dumps(section_check),
            "quality_json": db.dumps(quality_check),
            "quality_report_json": db.dumps(quality_report_snapshot),
            "model": (model or "").strip() or None,
            "rules_version": task["rules_version"], "status": "draft", "created_at": now,
        }
        self.store.conn.execute(
            "INSERT INTO drafts (knowledge_id, revision, task_id, project_id, stage, title,"
            " knowledge_type, topics, body_md, content_hash, sources_json, gaps_json,"
            " human_inputs_json, section_check_json, quality_json, quality_report_json, model, rules_version, status, created_at)"
            " VALUES (:knowledge_id, :revision, :task_id, :project_id, :stage, :title,"
            " :knowledge_type, :topics, :body_md, :content_hash, :sources_json, :gaps_json,"
            " :human_inputs_json, :section_check_json, :quality_json, :quality_report_json, :model, :rules_version, :status,"
            " :created_at)", record)
        self.store.conn.execute(
            "UPDATE distill_tasks SET status = 'draft_submitted', updated_at = ? WHERE task_id = ?",
            (now, task_id))
        return self.get_draft(knowledge_id, revision)

    def get_draft(self, knowledge_id: str, revision: int | None = None) -> dict:
        if revision:
            row = self.store.conn.execute(
                "SELECT * FROM drafts WHERE knowledge_id = ? AND revision = ?",
                (knowledge_id, revision)).fetchone()
        else:
            row = self.store.conn.execute(
                "SELECT * FROM drafts WHERE knowledge_id = ? ORDER BY revision DESC LIMIT 1",
                (knowledge_id,)).fetchone()
        record = db.rowdict(row)
        if not record:
            raise ValueError(f"草稿不存在: {knowledge_id} rev {revision or '最新'}")
        record["topics"] = db.loads(record["topics"], [])
        record["sources"] = db.loads(record["sources_json"], [])
        record.pop("sources_json", None)
        record["gaps"] = db.loads(record["gaps_json"], [])
        record.pop("gaps_json", None)
        record["task_gaps"] = record["gaps"]
        record["task_human_inputs"] = db.loads(record["human_inputs_json"], [])
        record.pop("human_inputs_json", None)
        record["section_check"] = db.loads(record["section_check_json"], {})
        record.pop("section_check_json", None)
        record["quality_check"] = db.loads(record["quality_json"], {})
        record.pop("quality_json", None)
        record["quality_report"] = db.loads(record["quality_report_json"], {})
        record.pop("quality_report_json", None)
        task = db.rowdict(self.store.conn.execute(
            "SELECT rules_hash, status FROM distill_tasks WHERE task_id = ?",
            (record["task_id"],)).fetchone())
        record["task_rules_hash"] = task["rules_hash"] if task else None
        record["task_status"] = task["status"] if task else None
        return record

    def list_drafts(self, project_ref: str | None = None) -> list[dict]:
        where, params = "1=1", []
        if project_ref:
            project = self.store.get_project(project_ref)
            where, params = "d.project_id = ?", [project["project_id"]]
        rows = self.store.conn.execute(
            "SELECT d.knowledge_id, d.revision, d.title, d.knowledge_type, d.stage,"
            " d.status, d.content_hash, d.created_at, p.name AS project_name"
            f" FROM drafts d JOIN projects p ON p.project_id = d.project_id WHERE {where}"
            " ORDER BY d.created_at", params).fetchall()
        return db.rowdicts(rows)


def _strip_fenced(text: str) -> str:
    """去掉围栏代码块，避免代码示例里的标题/表格被当作正文结构。"""
    return re.sub(r"```.*?(?:```|\Z)", "", text, flags=re.S)


def _sections(body_md: str) -> dict[str, str]:
    text = _strip_fenced(body_md)
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = re.sub(
            r"\s*<!--\s*kd-section\s+id=[a-z][a-z0-9_-]*\s+"
            r"rag=(?:body|exclude)\s*-->\s*$", "",
            match.group(1).strip())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out.setdefault(title, "")
        out[title] += text[match.end():end]
    return out


def _content_ok(content: str) -> bool:
    """章节需有实质内容；“不适用”必须附带原因（去掉该词后仍有实义字符）。"""
    body = _strip_fenced(content)
    text = "".join(line.strip() for line in body.splitlines())
    if not text:
        return False
    remainder = re.sub(r"[\s，,。：:；;、\-—·()（）/]", "", text.replace("不适用", ""))
    return len(remainder) >= 4


def check_body(body_md: str) -> tuple[list[str], list[str], list[str]]:
    """返回（缺失必备章节, 未出现的其余模板章节, 必备章节内容不合格明细）。"""
    sections = _sections(body_md)

    def find(name: str) -> str | None:
        accepted = (name, *SECTION_ALIASES.get(name, ()))
        return next((content for title, content in sections.items()
                     if any(alias in title for alias in accepted)), None)

    missing_required = [s for s in REQUIRED_SECTIONS if find(s) is None]
    invalid_required: list[str] = []
    for name in REQUIRED_SECTIONS:
        content = find(name)
        if content is None:
            continue
        if not _content_ok(content):
            invalid_required.append(f"{name}: 章节为空，或“不适用”未写明原因")
    missing_known = [s for s in KNOWN_SECTIONS
                     if s not in REQUIRED_SECTIONS and find(s) is None]
    return missing_required, missing_known, invalid_required


def _short_hash(text: str, full: bool = False) -> str:
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest if full else digest[:16]


def validate_quality_report(conn, task: dict, body_md: str,
                            report: dict | None) -> dict:
    """Legacy compatibility summary; content-quality gaps never block draft output."""
    unsupported = [m["filename"] for m in task["materials"]
                   if Path(m["filename"]).suffix.lower() not in DISTILLABLE_WORD_SUFFIXES]
    if unsupported:
        raise ValueError(
            "当前蒸馏仅支持 Word 文件（.doc、.docx），非 Word 材料不能蒸馏: "
            + ", ".join(unsupported[:8]))
    rows = conn.execute(
        "SELECT manifest_json FROM task_office_scans WHERE task_id = ?", (task["task_id"],)
    ).fetchall()
    manifests = [db.loads(row["manifest_json"], {}) for row in rows]
    assets = sum(len(item.get("assets", [])) for item in manifests)
    tables = sum(len(item.get("tables", [])) for item in manifests
                 if item.get("format") == "docx")
    return {"status": "diagnostic_only", "asset_count": assets,
            "table_count": tables,
            "client_report_received": isinstance(report, dict),
            "note": "历史质量报告仅作兼容诊断；内容质量不阻止蒸馏草稿产出。"}

def _section_body(body: str, section_name: str) -> str:
    sections = _sections(body)
    return next((content for title, content in sections.items() if section_name in title), "")


def _required_flow_table_rows(section: str, headers: tuple[str, ...]) -> int:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("|") or not all(h in line for h in headers):
            continue
        rows = 0
        for candidate in lines[index + 1:]:
            stripped = candidate.strip()
            if not stripped.startswith("|"):
                break
            if re.match(r"^\|[\s:|\-]+\|$", stripped):
                continue
            rows += 1
        return rows
    raise ValueError("质量门阻止提交：流程图必须在“图像与流程资产”中提供逐边表，字段包括起点节点、终点节点、箭头方向、条件、动作、原图核验")


