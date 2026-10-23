"""Knowledge Distillation MCP server（stdio，官方 mcp SDK ≥2.3 的 MCPServer 高层接口）。

- 工具面与 ckp CLI 同源；带 request_id 的写工具与 CLI 幂等语义一致。
- Store/Distill 按工具调用创建（SDK 在工作线程执行工具，SQLite 连接不可跨线程；
  同时保证只读工具不初始化数据库——与 CLI 的 F16 行为一致）。
- 预期业务错误以 ToolError 透传消息（客户端模型可读并自我纠正），未预期异常由
  SDK 记录堆栈并返回通用错误。
启动：./ckp-mcp.sh（或 PYTHONPATH=src .venv/bin/python -m ckp.mcp_server）。
数据目录：CKP_DATA（默认 <仓库根>/data）；导出目录：CKP_OUTPUT（默认 output/distilled）。
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ckp.distill import STAGES, Distill  # noqa: E402
from ckp.md_render import export_draft, verify_md, verify_sidecar  # noqa: E402
from ckp.store import Store  # noqa: E402
from ckp.knowledge_types import PRODUCT_MATERIAL_INGEST_CATEGORIES  # noqa: E402
from ckp.product_material_tools import register_product_material_tools  # noqa: E402

from mcp.server.mcpserver import Context, Image, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from ckp.mcp_progress import report_progress  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

_ANTICIPATED = (ValueError, FileNotFoundError, json.JSONDecodeError,
                UnicodeDecodeError, sqlite3.Error, OSError, TimeoutError)
_LOGGER = logging.getLogger(__name__)


def _public_error_message(exc: Exception) -> str:
    """Keep local deployment paths in server logs, not MCP tool responses."""
    message = str(exc) or type(exc).__name__
    private_roots = (
        ("服务目录", REPO_ROOT),
        ("数据目录", _data_dir()),
        ("导出目录", _output_dir()),
    )
    for label, root in sorted(private_roots, key=lambda item: len(str(item[1])), reverse=True):
        path = str(root.expanduser().resolve())
        if path and path in message:
            _LOGGER.warning("MCP tool error referenced local %s %s: %s", label, path, message)
            message = message.replace(path, f"<{label}>")
    if isinstance(exc, OSError) and exc.filename:
        message = message.replace(str(exc.filename), '<文件>')
    message = re.sub(r'/(?:Users|home|var|tmp|private|Volumes|workspace|opt|srv|mnt)(?:/[^\n\'"，；）]*)?',
                     '<本机路径>', message)
    return message


def _tool_errors(fn):
    """把预期业务错误转为带消息的 ToolError；保留原函数签名供 schema 生成。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise ToolError(_public_error_message(exc)) from exc

    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


def _data_dir() -> Path:
    return Path(os.environ.get("CKP_DATA", str(REPO_ROOT / "data")))


def _output_dir() -> Path:
    return Path(os.environ.get("CKP_OUTPUT", str(REPO_ROOT / "output" / "distilled")))


def _rid(request_id: str | None) -> str | None:
    return (request_id or "").strip() or None


def _split_list(text: str | None) -> list[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


server = MCPServer(
    "Knowledge Distillation",
    version="1.5.5",
    instructions=(
        "Knowledge Distillation:先在对话中确认处理意图；用户只说上传/提交/归档时仅保存原件，不解析、不蒸馏、不索引；用户明确提出蒸馏后才可启动任务，且只蒸馏用户明确指定的 Word。默认每份 Word 单独启动一个蒸馏任务并产出一份 MD。多份 Word 请求蒸馏时，提醒项目经理 MCP 支持将明确指定的一组文件合并分析并产出一份知识文件；未明确要求合并时无需等待选择，按逐份处理。“全部蒸馏”表示逐份处理，不代表合并。只有项目经理明确要求指定多份资料合并分析并共同产出一个知识文件时，才将该组材料关联到同一任务并提交一份综合 MD；不得按项目、阶段、主题或相似度自行合并。仅说关键文档但未指明文件时只追问一次具体范围。用户只说处理/整理等无法判断意图时，先询问仅归档还是同时蒸馏并询问文件范围，答复前不调用任何 MCP 工具。不得根据文件名、格式或内容替用户判断蒸馏意图/关键文档。合并稿保留各来源版本与冲突，不静默裁决。已明确上传并蒸馏及范围时不重复询问，先保存原件再蒸馏。只有 .doc/.docx 可蒸馏。项目名称必须由项目经理提供；上传前先调用现有 project_list 检查相同/相近名称并展示候选；由项目经理判断是否同一项目，同一项目沿用清单规范名称并再次确认；若确认是不同项目，保留项目经理提供的新名称；项目名称未提供或项目归属未确认前不得上传；不能从材料推断、自动改名或合并项目。"
        "仅当用户明确提出蒸馏且范围明确时才调用 distill_stages，并记录 server_info.server_version、parser_version、image_renderer_version；缺少版本字段或版本与当前验收基线不符时停止图片验收并报告，不把旧 MCP 结果当作当前代码效果。只说上传/归档时不调用任何蒸馏预检或蒸馏工具。"
        "draft_submit 前必须调用 metadata_catalog 读取当前产品线/领域清单，由项目经理明确单选一个 products，并从该产品线关联领域中选择一个或多个 domain；不得根据正文、项目产品标签或文件名推断。将选择原样传给 draft_submit；缺选或映射不符时修正选择后再提交。"
        "按 project → upload（只处理用户给定的完整路径，不枚举目录）→（用户明确要求蒸馏后）distill_stages → distill_start/attach → distill_guidance（每任务一次）→ distill_word_content（limit 默认 60，按 has_more/next_offset 读完；若返回仍超限则减半，不得用 100 以上大页）→ 核心图片识别 → distill_quality_report_template（排除无关图后调用一次）→ metadata_catalog/项目经理确认产品线与领域 → draft_submit（一次提交来源和完整 quality_report，并传入分类）→ draft_export → distill_close 执行。"
        "进度沟通：每份 Word 开始时说明文件名和生命周期阶段；长文档只在正文读取中段、图片核验中段或阶段切换时更新；完成后报告实际结果。分页和逐图工具调用之间不逐次发状态，不重复‘正在蒸馏 Word 文字/文档’等泛化话术。当前文件和批次序号要明确；正文进度按工具返回的 offset/total_blocks，图片进度按已实际核验数，不估计完成度或时间。"
        "质量分低、图片不确定、章节或来源定位不完整均记录为评分/待确认项，不阻止草稿提交；只有文件完整性、错误版本、无可用正文和非 Word 范围错误可阻断。空白/无效图片转换由服务端拒绝并返回明确原因；保留原图、标记未识别，不要猜测流程。"
        "draft_submit 前逐项填写 distill_guidance 快照中的所有 owner=client 评分维度，并补全模板中的图片与表格检查；不要先提交默认分再重复提交同一内容。若遗漏评分，保留当前草稿并披露评估覆盖，不为补分创建重复修订。"
        "用户混选 Word 与非 Word 时，先停止蒸馏并确认是否只用 Word 继续；非 Word 可作为原件保存，但不得读取或交给模型。"
        "排除封面、目录、对象说明、版本记录、正文前说明、图例、页眉页脚和无关装饰；只对核心正文相关图片调用 distill_word_image，语义由客户端多模态模型直接识别，不用 OCR。"
        "MCP 负责用户指定材料的归档、解析/渲染、忠实蒸馏及技术来源追溯；不代项目经理审查或修订文档规范、签署/盖章、文件标识/版本/日期一致性、附件或测试验收材料齐套、方案决策闭环、编码/业务口径一致性，也不核对图像姓名与人员档案。不得主动开展此类审查、生成相关缺陷/待确认项或把它们列为 MCP 测试问题。忠实保留实际读取到的业务陈述，不判断其正确性、完整性或合规性；用户要求此类检查时说明超出 MCP 蒸馏范围。多文件明确合并时，保留来源及直接影响语义的陈述差异，不自行裁决。知识 Markdown 写入业务事实和适用边界；业务事实不因标题层级、表格或清单形式改为元数据。章节路由只使用 body/exclude；来源映射、读取覆盖、模型/规范信息、图片与表格技术证据和审核发布信息写入 sources/quality_report/sidecar，不创建 metadata/audit 正文章节。旧 metadata/audit 标记不自动迁移。"
        "RAG 问答先 search_knowledge，只用命中正文分节判断证据是否充分；足够时直接回答，不调用 read_knowledge。缺少必要前提、限制或关联步骤时，按精确知识修订和命中分节定向补读；多个命中属于同一修订时先合并 section_ids，避免重复读取。只有整体方案/跨章节关系、冲突核对，或定向补读仍不足时才读全文；命中少或分数低本身不是读全文理由。产品资料使用 search_product_materials，精确图号按映射定位，不依赖向量语义召回。"
        "来源必须绑定任务材料及其版本；服务端可自动补全粗粒度来源并降低来源评分。draft_export 已附正文哈希校验，通常无需再次调用 draft_verify。"
        "只用本 MCP 读取材料和检查导出，不用本机目录枚举/文件搜索、shell、其他 MCP 或 Office 工具。完成后列明每份源 Word 对应的任务及 MD；合并时列出该综合 MD 的全部来源文件。草稿未经项目经理审核，不代表发布或入库；写操作支持 request_id 幂等重试。"
        "本服务为单一 MCP，含本地开发 RAG 工具。stage 六阶段为主分类，knowledge_type 为补充文档类型，products 为项目经理单选产品线、domain 为所选产品线下多选领域；三者保存在 RAG 元数据中。用户明确要求入库时：knowledge_status 预检；对用户确认的知识 revision/content_hash 调用 submit_for_review，用户明确给出审核结论后 record_review，再 publish_knowledge；get_publish_task 返回 published 才报告本地入库完成，必须保留 chunks/verified_chunks。不得自行批准业务内容。"
        "只有回答确实需要流程图、系统图的节点/关系/分支细节，或用户要求完整还原时，才从相关命中章节的 protected_asset_refs 取 asset_id，并以同一 knowledge_id、revision、content_hash 及该节 section_id 调用 get_asset。服务会核验 asset_id 确实由指定 body 分节引用。按返回的完整结构核对；完整还原须保留节点、边、条件、方向、分组及 unresolved_items，不得静默截断、按名称猜资产或回退其他版本。普通问答不调用 get_asset。读取失败、资产映射或哈希不符时停止使用并说明缺口。search_knowledge 可按 stage、knowledge_type、products、domain 筛选；用户未限定分类时不自行推断筛选值。发布、读取、资产和撤回都用确切知识版本与哈希。产品资料属于独立逻辑资料库，不关联客户项目、不属于项目生命周期六阶段，也不要求指定 stage；使用 product_material_ingest 保留并直接解析用户指定的 Word 原件，不经过蒸馏与审核；当前仅接受类别为产品手册的规范 Word，培训资料入库必须拒绝并留待后续迭代；products 单选、domain 由目录校验。手册正文只有与唯一内嵌图片一一对应的正式图号（如图5.2-01）才生成独立标记，不分析图片、不保存图片副本；按同一 material_id/revision/source_sha256 和 figure_id 调用 read_product_material_image 从原 Word 取回原图。产品问题优先调用 search_product_materials，命中充分时直接回答，缺少上下文时按精确 material_id/revision/source_sha256 调用 read_product_material；只有明确需要原件时才导出原始 Word。产品资料搜索和项目档案搜索分别路由，售前是否联合调用由问题意图决定。当前身份与存储是本机单用户开发适配，服务器认证与文件服务迁移另验收。"
    ),
)


# ---------- 项目 ----------

def _server_capabilities() -> dict:
    from ckp.knowledge_types import DOCUMENT_TYPE_OPTIONS, STAGE_COLLECTIONS
    from ckp.office_parser import (MCP_CONVERT_IMAGE_FORMATS,
                                   IMAGE_RENDERER_VERSION,
                                   MCP_NATIVE_IMAGE_FORMATS, PARSER_VERSION)
    return {
        "server_name": "Knowledge Distillation",
        "server_version": "1.5.5",
        "parser_version": PARSER_VERSION,
        "image_renderer_version": IMAGE_RENDERER_VERSION,
        "word_formats": [".doc", ".docx"],
        "image_reference_types": ["drawingml", "vml", "svg"],
        "image_formats_direct": list(MCP_NATIVE_IMAGE_FORMATS),
        "image_formats_converted_to_png": list(MCP_CONVERT_IMAGE_FORMATS),
        "visual_understanding": "client_bound_multimodal_model",
        "ocr": False,
        "server_side_visual_inference": False,
        "progress_notifications": "when_requested_by_client",
        "image_result": "image_and_source_context; returned_is_not_reviewed",
        "classification": {"primary": "stage", "stage_collections": STAGE_COLLECTIONS,
                           "product_material_categories": list(PRODUCT_MATERIAL_INGEST_CATEGORIES),
                           "product_materials": {"classification": "independent_product_material_corpus",
                                                 "uses_lifecycle_stage": False,
                                                 "categories": list(PRODUCT_MATERIAL_INGEST_CATEGORIES)},
                           "document_type_field": "knowledge_type",
                           "document_type_examples": list(DOCUMENT_TYPE_OPTIONS),
                           "product_field": "products", "product_type": "single_catalog_choice",
                           "domain_field": "domain", "domain_type": "multiple_choice_from_selected_product",
                           "source": "阶段材料清单.md"},
        "score_policy": "人工维护的评分权重.json；任务启动时固定版本与哈希",
        "front_matter_policy": "exclude_cover_toc_audience_notes_revision_history_legends_and_their_images",
    }

@server.tool()
@_tool_errors
def project_create(name: str, industry: str = "", products: str = "",
                   owner: str = "", tags: str = "", notes: str = "") -> dict:
    """创建项目（名称唯一）。行业/产品/标签为逗号分隔多值；项目经理等责任字段由人工提供，不从正文猜测。"""
    return Store(_data_dir()).create_project(
        name, industry=_split_list(industry), products=_split_list(products),
        owner=owner, tags=_split_list(tags), notes=notes or "")


@server.tool()
@_tool_errors
def project_list() -> list[dict]:
    """列出全部项目，供上传前人工核对项目名称及相近候选；不判断或合并项目。"""
    projects = Store(_data_dir(), readonly=True).list_projects()
    if not projects:
        return CallToolResult(
            content=[TextContent(text="未找到项目（0 项）。")],
            structured_content={"result": projects},
        )
    return projects


@server.tool(title="选择产品线与领域")
@_tool_errors
def metadata_catalog(product: str = '') -> dict:
    """读取可配置产品线及其领域；products 由项目经理单选，domain 从关联领域多选。
    product 留空返回产品线及映射；给定 product 返回其领域。每次读取最新配置。"""
    from ckp.catalog import list_choices
    return list_choices(REPO_ROOT, product)


# ---------- 原件 ----------

@server.tool()
@_tool_errors
def upload_original(path: str, project: str, uploader: str = "", stage: str = "",
                    tags: str = "", source_alias: str = "") -> dict:
    """上传原件（本机绝对路径，文件或目录）：只保存与登记元数据，不解析、不蒸馏、不索引。
    stage 必须是六阶段之一或留空；全部文件被排除时报错。返回 file_id/version_id/sha256。"""
    return Store(_data_dir()).upload(Path(path).expanduser(), project,
                                     uploader=uploader or None, stage=stage or None,
                                     tags=_split_list(tags),
                                     source_alias=source_alias or None)


@server.tool()
@_tool_errors
def file_list(project: str = "", name_like: str = "", tag: str = "",
              upload_tag: str = "", stage: str = "") -> list[dict]:
    """按项目/名称/标签/上传标签/阶段查找原件。"""
    files = Store(_data_dir(), readonly=True).search_files(
        project=project or None, name_like=name_like or None, tag=tag or None,
        upload_tag=upload_tag or None, stage=stage or None)
    if not files:
        return CallToolResult(
            content=[TextContent(text="未找到文件（0 个）。")],
            structured_content={"result": files},
        )
    return files


@server.tool()
@_tool_errors
def file_read_version(file_id: str, version_id: str = "", out: str = "") -> dict:
    """读取指定版本（默认当前版本）并重新校验存储哈希；out 给定时复制到该路径（不得位于 data/ 内）。"""
    return Store(_data_dir(), readonly=True).read_version(
        file_id, version_id or None, out_path=Path(out) if out else None)


# ---------- 蒸馏任务 ----------

@server.tool()
@_tool_errors
def distill_stages() -> dict:
    """蒸馏预检：返回阶段选项、服务/解析器版本与支持格式及视觉边界。"""
    return {"stages": list(STAGES), "server_info": _server_capabilities()}


@server.tool()
@_tool_errors
def distill_start(project: str, stage: str, notes: str = "",
                  request_id: str = "") -> dict:
    """开蒸馏任务；任务开启时固化规范快照。失败后重试请携带同一 request_id（幂等）。"""
    return Distill(Store(_data_dir()), REPO_ROOT).start_task(
        project, stage, notes=notes or "", request_id=_rid(request_id))


@server.tool()
@_tool_errors
def distill_attach(task_id: str, file_id: str, version_id: str = "",
                   note: str = "", request_id: str = "") -> dict:
    """仅把项目内 Word（.doc/.docx）原件按版本钉进蒸馏任务。
    Excel/PPT/PDF/图片等仍可作为原件保存，但此处会拒绝且不解析、不交给本地模型。
    混合材料遇到拒绝后必须停止并询问用户是否仅用 Word 继续，不得静默跳过。"""
    return Distill(Store(_data_dir()), REPO_ROOT).attach(
        task_id, file_id, version_id=version_id or None, note=note or "",
        request_id=_rid(request_id))


@server.tool()
@_tool_errors
def distill_gap(task_id: str, description: str, request_id: str = "") -> dict:
    """声明材料缺口（缺什么、影响哪些结论、建议由谁补充）；缺材料必须登记，不得虚构填补。"""
    return Distill(Store(_data_dir()), REPO_ROOT).declare_gap(
        task_id, description, request_id=_rid(request_id))


@server.tool()
@_tool_errors
def distill_input(task_id: str, provider: str, content: str,
                  request_id: str = "") -> dict:
    """登记人工补充（必须记录提供人）；人工补充不是原件内容，正文中须标注。"""
    return Distill(Store(_data_dir()), REPO_ROOT).add_input(
        task_id, provider, content, request_id=_rid(request_id))


@server.tool()
@_tool_errors
def distill_guidance(task_id: str) -> dict:
    """任务执行说明：返回该任务的蒸馏规范、评分权重快照、材料与任务状态；每个任务只需读取一次。"""
    return Distill(Store(_data_dir(), readonly=True), REPO_ROOT).guidance(task_id)


@server.tool()
@_tool_errors
def distill_material(task_id: str, file_id: str) -> dict:
    """校验已关联 Word 版本并准备服务端临时副本；不向客户端暴露本机路径。
    正文必须通过 distill_word_content 读取；副本在 distill_close 时清理。"""
    result = Distill(Store(_data_dir()), REPO_ROOT).material_copy(task_id, file_id)
    return {key: result[key] for key in
            ("task_id", "file_id", "version_id", "sha256", "filename", "verified", "direct_text", "encoding", "note", "lifecycle")}


@server.tool()
@_tool_errors
def distill_quality_report_template(task_id: str,
                                    excluded_asset_ids: list[str] | None = None) -> dict:
    """根据已扫描的任务材料生成 draft_submit.quality_report 的待填骨架。
    调用前先用 distill_word_content 分页读取每份 Word 并确定核心正文范围。
    将 block_ref/block_context 所示封面、目录、说明、修订记录和图例图片 ID 放入 excluded_asset_ids；
    服务端核验 ID 后不为这些图片生成复核项。骨架只覆盖未排除的正文/脚注/尾注图片和 DOCX 表格。
    页眉、页脚与未引用媒体不进入蒸馏；相关流程图仍须在 flowcharts 中逐边回对原图。"""
    distill = Distill(Store(_data_dir(), readonly=True), REPO_ROOT)
    from ckp.office_parser import PARSER_VERSION
    task = distill.get_task(task_id)
    word_materials = [m for m in task["materials"]
                      if Path(m["filename"]).suffix.lower() in {".doc", ".docx"}]
    if not word_materials:
        return {"task_id": task_id, "quality_report": {
            "asset_reviews": [], "irrelevant_asset_ids": [],
            "flowcharts": [], "table_checks": []},
            "instructions": "任务没有 Word 材料。"}
    rows = distill.store.conn.execute(
        "SELECT file_id,version_id,sha256,parser_version,manifest_json FROM task_office_scans WHERE task_id = ?",
        (task_id,)).fetchall()
    scans = {row["file_id"]: row for row in rows}
    missing = [m["file_id"] for m in word_materials
               if m["file_id"] not in scans
               or scans[m["file_id"]]["version_id"] != m["version_id"]
               or scans[m["file_id"]]["sha256"] != m["sha256"]
               or scans[m["file_id"]]["parser_version"] != PARSER_VERSION]
    if missing:
        raise ValueError("请先对以下 Word 材料调用 distill_word_content 完成读取与扫描，再生成质量报告骨架: "
                         + ", ".join(missing))
    asset_reviews, table_checks = [], []
    excluded_asset_ids = excluded_asset_ids or []
    if (not isinstance(excluded_asset_ids, list)
            or any(not isinstance(aid, str) for aid in excluded_asset_ids)):
        raise ValueError("excluded_asset_ids 必须是字符串数组")
    if len(set(excluded_asset_ids)) != len(excluded_asset_ids):
        raise ValueError("excluded_asset_ids 中有重复图片 ID")
    known_asset_ids = {asset["asset_id"] for material in word_materials
                       for asset in json.loads(scans[material["file_id"]]["manifest_json"]).get("assets", [])}
    unknown_assets = sorted(set(excluded_asset_ids) - known_asset_ids)
    if unknown_assets:
        raise ValueError("excluded_asset_ids 包含扫描清单之外的图片: " + ", ".join(unknown_assets[:8]))
    for material in word_materials:
        manifest = json.loads(scans[material["file_id"]]["manifest_json"])
        for asset in manifest.get("assets", []):
            if asset["asset_id"] not in excluded_asset_ids:
                asset_reviews.append({"asset_id": asset["asset_id"],
                                      "source_file_id": material["file_id"],
                                      "source_version_id": material["version_id"],
                                      "source_sha256": material["sha256"],
                                      "source_location": asset.get("location") or asset.get("block_ref", ""),
                                      "category": "", "status": "", "description": "", "body_ref": ""})
        if manifest.get("format") == "docx":
            for table in manifest.get("tables", []):
                table_checks.append({"file_id": material["file_id"], "kind": "docx",
                                     "table_id": table["table"],
                                     "row_count": table["row_count"],
                                     "status": "", "rationale": ""})
    return {"task_id": task_id,
            "quality_report": {"asset_reviews": asset_reviews,
                               "irrelevant_asset_ids": list(excluded_asset_ids),
                               "flowcharts": [], "table_checks": table_checks},
            "field_rules": {
                "asset_reviews.category": ["flowchart", "process_diagram", "screenshot",
                                            "illustration", "photo", "other"],
                "asset_reviews.status": ["reviewed"],
                "irrelevant_asset_ids": "核心正文范围外的图片（封面、目录、对象说明、版本记录、图例等）及确认无关装饰图放在此数组；不调用视觉工具、不写入 MD 或流程图。页眉/页脚图片已由扫描器自动跳过。",
                "flowcharts": "每个 category 为 flowchart/process_diagram 的资产必须有一项完整结构对象；保留 asset_id、asset_type、source_file_id、source_version_id、source_sha256、source_location、checked_against_original、nodes、edges、groups/lanes/layout（原图能识别时）和 unresolved_items。节点保留 id/name/type/role/label；边保留 from/to/direction/relationship/condition/action/label/verified_against_original。不得用 Mermaid 或摘要替代此对象。若不能确认完整性，保留已识别内容并标记 unresolved_items，不得声称完整复原。",
                "table_checks.status": ["reviewed", "not_relevant"],
                "table_checks.key": ["file_id", "kind", "table_id"],
                "body_ref": "必须是蒸馏 Markdown 正文中原样存在的定位文本。",
            }}


def _word_manifest(task_id: str, file_id: str):
    distill = Distill(Store(_data_dir()), REPO_ROOT)
    task = distill.get_task(task_id)
    if task["status"] == "closed":
        raise ValueError("任务已关闭，不能读取材料")
    material = next((item for item in task["materials"] if item["file_id"] == file_id), None)
    if material is None:
        raise ValueError(f"该文件未关联到任务，请先 attach: {file_id}")
    from ckp.office_parser import PARSER_VERSION
    row = distill.store.conn.execute(
        "SELECT version_id, sha256, parser_version, manifest_json FROM task_office_scans"
        " WHERE task_id = ? AND file_id = ?", (task_id, file_id)).fetchone()
    if (row is None or row["version_id"] != material["version_id"]
            or row["sha256"] != material["sha256"]
            or row["parser_version"] != PARSER_VERSION):
        distill.inspect_word(task_id, file_id)
        row = distill.store.conn.execute(
            "SELECT version_id, sha256, parser_version, manifest_json FROM task_office_scans"
            " WHERE task_id = ? AND file_id = ?", (task_id, file_id)).fetchone()
    manifest = json.loads(row["manifest_json"])
    manifest["filename"] = material["filename"]
    return distill, row, manifest


@server.tool(title="读取 Word 正文")
@_tool_errors
def distill_word_content(task_id: str, file_id: str, offset: int = 0,
                         limit: int = 60, ctx: Context = None) -> dict:
    """通过 MCP 返回已关联 Word 的提取正文、表格与图片说明，不返回本机文件路径。
    以 offset/limit 分页读取 blocks；has_more=true 时用 next_offset 继续。"""
    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if not 1 <= limit <= 500:
        raise ValueError("limit 必须在 1 到 500 之间")
    _, row, manifest = _word_manifest(task_id, file_id)
    blocks = ([{"kind": "paragraph", **item} for item in manifest.get("paragraphs", [])]
              + [{"kind": "table", **item} for item in manifest.get("tables", [])])
    blocks.sort(key=lambda item: (item.get("body_order", item.get("index", item.get("table", 0))),
                                  item["kind"]))
    page = blocks[offset:offset + limit]
    assets = ([{key: value for key, value in asset.items() if key != "local_path"}
               for asset in manifest.get("assets", [])] if offset == 0 else [])
    returned = offset + len(page)
    message = f"{manifest['filename']}：正文已返回至 {min(returned, len(blocks))}/{len(blocks)} 块"
    report_progress(ctx, min(returned, len(blocks)), len(blocks), message)
    return {"task_id": task_id, "file_id": file_id, "filename": manifest["filename"],
            "version_id": row["version_id"], "sha256": row["sha256"],
            "format": manifest["format"], "parser_version": manifest["parser"],
            "total_blocks": len(blocks), "offset": offset, "limit": limit,
            "blocks": page, "has_more": offset + len(page) < len(blocks),
            "next_offset": offset + len(page) if offset + len(page) < len(blocks) else None,
            "assets": assets,
            "progress": {"phase": "content_returned", "completed": min(returned, len(blocks)),
                         "total": len(blocks), "message": message},
            "summary": manifest["summary"]}


@server.tool(title="读取 Word 图片", structured_output=False)
@_tool_errors
def distill_word_image(task_id: str, file_id: str, asset_id: str,
                      ctx: Context = None) -> list:
    """通过 MCP 图像内容返回指定 Word 图片，由客户端当前绑定的本地多模态模型直接识别。
    PNG/JPEG/GIF/WebP 直接返回；SVG/EMF/WMF/BMP/TIFF 转为 PNG，并拒绝全透明或单色空白转换结果。
    同时返回文件名、原件图片序号和位置。序号不代表模型已核验数。
    服务端不做 OCR、图像描述或语义识别；不返回本机路径。"""
    _, _, manifest = _word_manifest(task_id, file_id)
    asset = next((item for item in manifest.get("assets", [])
                  if item.get("asset_id") == asset_id), None)
    if asset is None:
        raise ValueError(f"Word 图片资产不存在: {asset_id}")
    position = manifest['assets'].index(asset) + 1
    label = f"{manifest['filename']}：原件图片 {position}/{len(manifest['assets'])}"
    report_progress(ctx, 0, 1, label + "，正在准备图片")
    if not asset.get("local_path"):
        raise ValueError(f"Word 图片关系已识别，但包内图片数据不存在: {asset.get('part', asset_id)}")
    path = Path(asset["local_path"]).resolve()
    task_root = (REPO_ROOT / "tmp" / "distill" / task_id).resolve()
    try:
        path.relative_to(task_root)
    except ValueError as exc:
        raise ValueError("图片资产不在当前任务临时目录内") from exc
    image_format = path.suffix.lower().lstrip(".")
    from ckp.office_parser import MCP_CONVERT_IMAGE_FORMATS, MCP_NATIVE_IMAGE_FORMATS
    if image_format in MCP_CONVERT_IMAGE_FORMATS:
        from ckp.office_parser import IMAGE_RENDERER_VERSION, render_image_for_mcp
        path = render_image_for_mcp(
            path, task_root / "office" / file_id / "assets" / "mcp-rendered" / IMAGE_RENDERER_VERSION)
        image_format = "png"
    if image_format not in MCP_NATIVE_IMAGE_FORMATS:
        raise ValueError(f"MCP 图像返回暂不支持该图片格式: {path.suffix or '(无扩展名)'}")
    image = Image(path=path, format="jpeg" if image_format == "jpg" else image_format)
    context = {"filename": manifest['filename'], "file_id": file_id, "asset_id": asset_id,
               "image_position": position, "total_images": len(manifest['assets']),
               "location": asset.get('block_ref') or asset.get('location'),
               "nearby_text": (asset.get('block_context') or '')[:480],
               "phase": "image_returned", "reviewed_by_model": False,
               "message": label + "已返回，等待模型核验"}
    report_progress(ctx, 1, 1, context['message'])
    return [image, json.dumps(context, ensure_ascii=False)]


@server.tool()
@_tool_errors
def distill_show(task_id: str) -> dict:
    """查看任务状态、材料、缺口、补充与已提交草稿。"""
    return Distill(Store(_data_dir(), readonly=True), REPO_ROOT).get_task(task_id)


@server.tool()
@_tool_errors
def distill_close(task_id: str) -> dict:
    """关闭任务并清理临时副本；tmp_cleanup_status 明确返回 removed 或 not_present。
    not_present 表示关闭时该任务的临时目录不存在，不代表清理失败。关闭后任务内容冻结。"""
    return Distill(Store(_data_dir()), REPO_ROOT).cleanup_task(task_id)


# ---------- 草稿 ----------

@server.tool()
@_tool_errors
def draft_submit(task_id: str, title: str, knowledge_type: str, body_md: str,
                 sources: list[dict] | None = None, model: str = "", knowledge_id: str = "",
                 topics: str = "", quality_report: dict | None = None,
                 request_id: str = "", products: str = "",
                 domain: list[str] | None = None) -> dict:
    """提交蒸馏草稿。body_md 为 UTF-8 Markdown 正文；正文保留业务事实、冲突缺口与待确认项。
    来源通过 sources 参数提供，读取覆盖和处理证据放入 quality_report；缺少推荐章节不阻断，记录在评分中。
    sources 可省略，服务按任务 Word 材料自动补全粗定位；
    可选 source 为 {version_id, location, supports, file_id?, sha256?}，错误版本/哈希仍拒绝。
    quality_report 使用 scores/evidence/flags/unresolved_items，并可含 distill_quality_report_template 返回的 asset_reviews、
    irrelevant_asset_ids、flowcharts、table_checks。Word 流程应在首次提交时一并填充；缺项只记为未评与待提升，不阻止导出。
    返回 quality_check.assessment_status、unassessed_dimensions 与 flags；提交后若为 partial，必须向用户说明评分覆盖和暂按 0 分计入的维度，不得只报告综合分。
    products 必须是项目经理从 metadata_catalog 单选的产品线；domain 为其关联领域多选。
    不可由模型根据正文或文件名代选。权重来自任务启动时固定的评分权重配置。续修订传 knowledge_id。"""
    return Distill(Store(_data_dir()), REPO_ROOT).submit_draft(
        task_id, title, knowledge_type, body_md, sources, model=model or None,
        knowledge_id=knowledge_id or None, topics=_split_list(topics),
        quality_report=quality_report,
        products=products, domain=domain,
        request_id=_rid(request_id))


@server.tool()
@_tool_errors
def draft_show(knowledge_id: str, revision: int = 0, include_body: bool = False) -> dict:
    """查看草稿（含来源、快照与章节检查结果）；revision=0 取最新修订。
    默认只返回正文预览；确需审核/恢复处理时 include_body=True 读取完整正文。"""
    draft = Distill(Store(_data_dir(), readonly=True), REPO_ROOT).get_draft(
        knowledge_id, revision or None)
    from ckp.knowledge import Knowledge
    knowledge = Knowledge(_data_dir(), REPO_ROOT, readonly=True)
    try:
        lifecycle = knowledge.lifecycle(knowledge_id, draft['revision'])
    finally:
        knowledge.close()
    result = {key: value for key, value in draft.items() if key != "body_md"} | {
        "body_preview": draft["body_md"][:800], "draft_status": draft['status'], **lifecycle}
    if include_body:
        result['body_md'] = draft['body_md']
    return result


@server.tool()
@_tool_errors
def draft_export(knowledge_id: str, revision: int = 0, out_dir: str = "") -> dict:
    """导出 md+sidecar（同一修订重复导出字节一致），并随附正文及 sidecar 哈希、版本关联校验。默认目录 output/distilled。"""
    distill = Distill(Store(_data_dir(), readonly=True), REPO_ROOT)
    draft = distill.get_draft(knowledge_id, revision or None)
    project = distill.store.get_project(draft["project_id"])
    result = export_draft(draft, project,
                          Path(out_dir) if out_dir else _output_dir())
    result["verify"] = verify_md(Path(result["md"]))
    result["verify_sidecar"] = verify_sidecar(Path(result["json"]), Path(result["md"]))
    return result


@server.tool()
@_tool_errors
def draft_verify(md_path: str, sidecar_path: str = "") -> dict:
    """校验 Markdown 正文 SHA-256；可选传 sidecar_path 独立校验 JSON 完整性并核对知识版本身份。不读数据库。"""
    body = verify_md(Path(md_path))
    if sidecar_path:
        sidecar = verify_sidecar(Path(sidecar_path), Path(md_path))
        return {"markdown": body, "sidecar": sidecar,
                "match": body.get("match", False) and sidecar.get("match", False)}
    return body


from ckp.knowledge_tools import register_knowledge_tools
register_knowledge_tools(server, _data_dir, REPO_ROOT, _tool_errors)
register_product_material_tools(server, _data_dir, REPO_ROOT, _tool_errors)


def main() -> int:
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
