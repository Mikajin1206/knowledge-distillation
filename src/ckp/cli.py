"""命令行入口：python -m ckp <命令>（或 ./ckp.sh）。

所有命令只依赖标准库；数据目录默认 <仓库根>/data，可用 CKP_DATA/CKP_OUTPUT 覆盖。
查询与校验命令使用只读连接：不建目录、不建表、不写库（F16）。
`draft verify` 纯文件校验，不触库；正文不匹配退出码为 1（F08）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ckp import db  # noqa: E402
from ckp.distill import STAGES, Distill  # noqa: E402
from ckp.md_render import export_draft, verify_md  # noqa: E402
from ckp.store import Store  # noqa: E402


def data_dir() -> Path:
    return Path(os.environ.get("CKP_DATA", str(REPO_ROOT / "data")))


def output_dir() -> Path:
    return Path(os.environ.get("CKP_OUTPUT", str(REPO_ROOT / "output" / "distilled")))


def _split_list(text: str | None) -> list[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def _print(record) -> None:
    if isinstance(record, (dict, list)):
        print(db.dumps(record))
    else:
        print(record)


# ---------- 项目 ----------

def cmd_project(args) -> int:
    store = Store(data_dir(), readonly=args.action in ("list", "show"))
    if args.action == "create":
        _print(store.create_project(args.name, industry=_split_list(args.industry),
                                    products=_split_list(args.products),
                                    owner=args.owner, tags=_split_list(args.tags),
                                    notes=args.notes or ""))
    elif args.action == "list":
        for project in store.list_projects():
            print(f"{project['project_id']}  {project['name']}"
                  f"  owner={project['owner'] or '未指定'}  行业={'/'.join(project['industry']) or '-'}"
                  f"  产品={'/'.join(project['products']) or '-'}")
    elif args.action == "show":
        _print(store.get_project(args.ref))
    return 0


# ---------- 上传与文件 ----------

def cmd_upload(args) -> int:
    store = Store(data_dir())
    _print(store.upload(Path(args.path), args.project, uploader=args.uploader,
                        stage=args.stage, tags=_split_list(args.tags),
                        source_alias=args.source_alias))
    return 0


def cmd_file(args) -> int:
    store = Store(data_dir(), readonly=True)  # 文件命令全部只读
    if args.action == "list":
        for record in store.search_files(project=args.project, name_like=args.name_like,
                                         tag=args.tag, stage=args.stage,
                                         sha256=args.sha256, upload_tag=args.upload_tag):
            print(f"{record['file_id']}  {record['name']}  项目={record['project_name']}"
                  f"  v{record['current_seq'] or '?'}  sha256={(record['current_sha256'] or '')[:12]}…"
                  f"  阶段={record['stage'] or '-'}")
    elif args.action == "show":
        _print(store.get_file(args.file_id))
    elif args.action == "read-version":
        _print(store.read_version(args.file_id, args.version,
                                  out_path=Path(args.out) if args.out else None))
    return 0


# ---------- 蒸馏 ----------

def cmd_distill(args) -> int:
    if args.action == "stages":
        for stage in STAGES:
            print(stage)
        return 0
    readonly = args.action in ("show", "guidance")
    store = Store(data_dir(), readonly=readonly)
    distill = Distill(store, REPO_ROOT)
    if args.action == "start":
        _print(distill.start_task(args.project, args.stage, notes=args.notes or "",
                                  request_id=args.request_id))
    elif args.action == "attach":
        _print(distill.attach(args.task_id, args.file_id, version_id=args.version,
                              note=args.note or "", request_id=args.request_id))
    elif args.action == "gap":
        _print(distill.declare_gap(args.task_id, args.description,
                                   request_id=args.request_id))
    elif args.action == "input":
        _print(distill.add_input(args.task_id, args.provider, args.content,
                                 request_id=args.request_id))
    elif args.action == "show":
        _print(distill.get_task(args.task_id))
    elif args.action == "guidance":
        _print(distill.guidance(args.task_id))
    elif args.action == "material":
        _print(distill.material_copy(args.task_id, args.file_id))
    elif args.action == "inspect-word":
        _print(distill.inspect_word(args.task_id, args.file_id,
                                    request_id=args.request_id))
    elif args.action == "close":
        _print(distill.cleanup_task(args.task_id))
    return 0


# ---------- 草稿与导出 ----------

def cmd_draft(args) -> int:
    if args.action == "verify":
        # 纯文件校验：不初始化数据库、不创建目录（F16）。
        result = verify_md(Path(args.md_file))
        _print(result)
        return 0 if result.get("match") else 1
    readonly = args.action in ("show", "list", "export")
    store = Store(data_dir(), readonly=readonly)
    distill = Distill(store, REPO_ROOT)
    if args.action == "submit":
        sources = json.loads(Path(args.sources_json).read_text(encoding="utf-8"))
        body = Path(args.md_file).read_text(encoding="utf-8")
        quality_report = (json.loads(Path(args.quality_report_json).read_text(encoding="utf-8"))
                          if args.quality_report_json else None)
        _print(distill.submit_draft(
            args.task_id, args.title, args.knowledge_type, body, sources,
            model=args.model, knowledge_id=args.knowledge_id,
            topics=_split_list(args.topics), quality_report=quality_report,
            request_id=args.request_id))
    elif args.action == "show":
        draft = distill.get_draft(args.knowledge_id, args.revision)
        _print({key: value for key, value in draft.items() if key != "body_md"}
               | {"body_preview": draft["body_md"][:500]})
    elif args.action == "list":
        for record in distill.list_drafts(project_ref=args.project):
            print(f"{record['knowledge_id']} rev{record['revision']}  {record['title']}"
                  f"  [{record['status']}]  {record['knowledge_type']}"
                  f"  项目={record['project_name']}  {record['created_at']}")
    elif args.action == "export":
        draft = distill.get_draft(args.knowledge_id, args.revision)
        project = store.get_project(draft["project_id"])
        result = export_draft(draft, project,
                              Path(args.out_dir) if args.out_dir else output_dir())
        result["verify"] = verify_md(Path(result["md"]))
        _print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ckp", description="Knowledge Distillation tools (upload, distill, Markdown output)")
    sub = parser.add_subparsers(dest="group", required=True)

    p_project = sub.add_parser("project")
    p_project_sub = p_project.add_subparsers(dest="action", required=True)
    p_create = p_project_sub.add_parser("create")
    p_create.add_argument("name")
    p_create.add_argument("--industry", default="")
    p_create.add_argument("--products", default="")
    p_create.add_argument("--owner", default="")
    p_create.add_argument("--tags", default="")
    p_create.add_argument("--notes", default="")
    p_project_sub.add_parser("list")
    p_show = p_project_sub.add_parser("show")
    p_show.add_argument("ref")

    p_upload = sub.add_parser("upload", help="上传原件：只保存与登记，不解析不索引")
    p_upload.add_argument("path")
    p_upload.add_argument("--project", required=True)
    p_upload.add_argument("--uploader", default=None)
    p_upload.add_argument("--stage", default=None)
    p_upload.add_argument("--tags", default="")
    p_upload.add_argument("--source-alias", default=None)

    p_file = sub.add_parser("file")
    p_file_sub = p_file.add_subparsers(dest="action", required=True)
    p_list = p_file_sub.add_parser("list")
    p_list.add_argument("--project", default=None)
    p_list.add_argument("--name-like", default=None)
    p_list.add_argument("--tag", default=None)
    p_list.add_argument("--upload-tag", default=None, help="按上传记录中的标签检索")
    p_list.add_argument("--stage", default=None)
    p_list.add_argument("--sha256", default=None)
    p_fshow = p_file_sub.add_parser("show")
    p_fshow.add_argument("file_id")
    p_read = p_file_sub.add_parser("read-version")
    p_read.add_argument("file_id")
    p_read.add_argument("--version", default=None)
    p_read.add_argument("--out", default=None)

    p_distill = sub.add_parser("distill")
    p_distill_sub = p_distill.add_subparsers(dest="action", required=True)
    p_start = p_distill_sub.add_parser("start")
    p_start.add_argument("--project", required=True)
    p_start.add_argument("--stage", required=True)
    p_start.add_argument("--notes", default="")
    p_start.add_argument("--request-id", default=None)
    p_attach = p_distill_sub.add_parser("attach")
    p_attach.add_argument("task_id")
    p_attach.add_argument("--file", required=True, dest="file_id")
    p_attach.add_argument("--version", default=None)
    p_attach.add_argument("--note", default="")
    p_attach.add_argument("--request-id", default=None)
    p_gap = p_distill_sub.add_parser("gap")
    p_gap.add_argument("task_id")
    p_gap.add_argument("description")
    p_gap.add_argument("--request-id", default=None)
    p_input = p_distill_sub.add_parser("input")
    p_input.add_argument("task_id")
    p_input.add_argument("--provider", required=True)
    p_input.add_argument("content")
    p_input.add_argument("--request-id", default=None)
    p_dshow = p_distill_sub.add_parser("show")
    p_dshow.add_argument("task_id")
    p_guid = p_distill_sub.add_parser("guidance")
    p_guid.add_argument("task_id")
    p_mat = p_distill_sub.add_parser("material")
    p_mat.add_argument("task_id")
    p_mat.add_argument("--file", required=True, dest="file_id")
    p_inspect_word = p_distill_sub.add_parser("inspect-word")
    p_inspect_word.add_argument("task_id")
    p_inspect_word.add_argument("--file", required=True, dest="file_id")
    p_inspect_word.add_argument("--request-id", default=None)
    p_close = p_distill_sub.add_parser("close")
    p_close.add_argument("task_id")
    p_distill_sub.add_parser("stages")

    p_draft = sub.add_parser("draft")
    p_draft_sub = p_draft.add_subparsers(dest="action", required=True)
    p_submit = p_draft_sub.add_parser("submit")
    p_submit.add_argument("task_id")
    p_submit.add_argument("--title", required=True)
    p_submit.add_argument("--type", required=True, dest="knowledge_type")
    p_submit.add_argument("--md-file", required=True)
    p_submit.add_argument("--sources-json", required=True)
    p_submit.add_argument("--quality-report-json", default=None,
                          help="Word 内嵌图片/表格质检报告 JSON")
    p_submit.add_argument("--model", default=None)
    p_submit.add_argument("--knowledge-id", default=None)
    p_submit.add_argument("--topics", default="")
    p_submit.add_argument("--request-id", default=None)
    p_kshow = p_draft_sub.add_parser("show")
    p_kshow.add_argument("knowledge_id")
    p_kshow.add_argument("--revision", type=int, default=None)
    p_klist = p_draft_sub.add_parser("list")
    p_klist.add_argument("--project", default=None)
    p_export = p_draft_sub.add_parser("export")
    p_export.add_argument("knowledge_id")
    p_export.add_argument("--revision", type=int, default=None)
    p_export.add_argument("--out-dir", default=None)
    p_verify = p_draft_sub.add_parser("verify")
    p_verify.add_argument("md_file")

    for handler, subparser in ((cmd_project, p_project), (cmd_file, p_file),
                               (cmd_upload, p_upload), (cmd_distill, p_distill),
                               (cmd_draft, p_draft)):
        subparser.set_defaults(func=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except (ValueError, FileNotFoundError, json.JSONDecodeError,
            UnicodeDecodeError, sqlite3.Error) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0 if rc is None else rc


if __name__ == "__main__":
    sys.exit(main())
