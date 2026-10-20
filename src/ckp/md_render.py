"""蒸馏知识 md 产出：frontmatter + 正文导出，及 content_hash 独立校验。

产出契约（详见 docs/05 接续文档）：
- 导出文件为 UTF-8 Markdown：YAML frontmatter + 正文；正文与草稿存档逐字节一致。
- content_hash = SHA-256(正文原字节)；正文指 frontmatter 结束行 `---` 之后的
  全部字节。校验按字节进行：换行被改为 CRLF 等任何字节变化都会判不匹配。
- content_hash 只覆盖正文，不覆盖 frontmatter 与 sidecar——元数据被改动
  不影响正文哈希，反之亦然；交付包如需整体完整性，另行做包级哈希（后续）。
- 同一修订重复导出生成相同字节：sidecar 数据来自提交时快照。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ckp import db

# frontmatter 中只放标量与字符串列表；完整来源/补充/缺口/检查记录在 sidecar。
FM_SCALARS = ("knowledge_id", "revision", "title", "knowledge_type", "status",
              "project_id", "project_name", "quality_score", "content_hash")
FM_LISTS = ("products",)


def render_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for field in FM_SCALARS:
        value = meta.get(field)
        if value is None or value == "":
            continue
        lines.append(f"{field}: {_yq(value)}")
    for field in FM_LISTS:
        values = meta.get(field) or []
        if not values:
            continue
        lines.append(f"{field}:")
        lines.extend(f"  - {_yq(v)}" for v in values)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yq(value) -> str:
    text = str(value)
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


_UNESCAPE = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}

# Routing is explicit and deliberately independent from human heading text.
# Every H2-H6 section must end with e.g.:
#   <!-- kd-section id=procurement rag=body -->
_RAG_MARKER_RE = re.compile(
    r"\s*<!--\s*kd-section\s+id=([a-z][a-z0-9_-]*)\s+"
    r"rag=(body|exclude)\s*-->\s*$"
)
_SECTION_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.M)


def extract_rag_payload(md_text: str) -> dict:
    """Split marked H2-H6 sections into retrieval and excluded routes.

    Missing, malformed, or duplicate markers fail closed: no body text is
    returned for indexing. Heading wording is preserved for display only and
    never determines the route.
    """
    frontmatter, body = split_frontmatter(md_text)
    matches = list(_SECTION_HEADING_RE.finditer(body))
    errors: list[str] = []
    sections: list[dict] = []
    seen_ids: set[str] = set()
    heading_stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        raw_heading = match.group(2).strip()
        marker = _RAG_MARKER_RE.search(raw_heading)
        if marker is None:
            errors.append(f"第 {index + 1} 个章节标题缺少有效 kd-section 标识")
            title = raw_heading
            section_id = ""
            route = ""
        else:
            section_id, route = marker.group(1), marker.group(2)
            title = raw_heading[:marker.start()].strip()
            if section_id in seen_ids:
                errors.append(f"章节标识重复: {section_id}")
            seen_ids.add(section_id)
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_path = [item[1] for item in heading_stack] + [title]
        heading_stack.append((level, title))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[match.end():end].strip()
        sections.append({"id": section_id, "title": title, "level": level,
                         "heading_path": heading_path, "route": route,
                         "content": content,
                         "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    if not matches:
        errors.append("正文没有可路由的二级及以下章节")

    routing_valid = not errors
    body_sections = [item for item in sections if item["route"] == "body"] if routing_valid else []
    retrieval_md = "\n\n".join(
        f"## {' / '.join(item['heading_path'])}\n\n{item['content']}"
        for item in body_sections
    )
    manifest = [{key: item[key] for key in
                 ("id", "title", "level", "heading_path", "route", "content_sha256")}
                for item in sections]
    return {
        "schema": "kd-rag-selection/v2",
        "routing_valid": routing_valid,
        "errors": errors,
        "metadata": {"knowledge_id": frontmatter.get("knowledge_id"),
                     "revision": frontmatter.get("revision"),
                     "title": frontmatter.get("title"),
                     "knowledge_type": frontmatter.get("knowledge_type"),
                     "status": frontmatter.get("status"),
                     "project_id": frontmatter.get("project_id"),
                     "project_name": frontmatter.get("project_name"),
                     "products": frontmatter.get("products", []),
                     "quality_score": frontmatter.get("quality_score"),
                     "content_hash": frontmatter.get("content_hash")},
        "sections": manifest,
        "retrieval_md": retrieval_md if routing_valid else "",
    }


_PRODUCT_SCOPE_RE = re.compile(r"^\s*(?:[-*]\s*)?产品范围[：:]\s*(.+?)\s*$", re.M)


def extract_products(body_md: str) -> list[str]:
    """Extract only explicitly labeled product names, never project-master tags."""
    match = _PRODUCT_SCOPE_RE.search(body_md)
    if not match:
        return []
    values = re.split(r"[；;]", match.group(1))
    return list(dict.fromkeys(value.strip(" \t，,。；;") for value in values
                              if value.strip(" \t，,。；;")))


def _protected_assets_manifest(quality_report: dict) -> dict:
    flowcharts = quality_report.get("flowcharts", [])
    flowcharts = flowcharts if isinstance(flowcharts, list) else []
    items = []
    for index, payload in enumerate(flowcharts):
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        source = {}
        if isinstance(payload, dict):
            source = {key: payload.get(key) for key in
                      ("source_file_id", "source_version_id", "source_sha256", "source_location")
                      if payload.get(key)}
        items.append({
            "asset_id": payload.get("asset_id") if isinstance(payload, dict) else None,
            "asset_type": payload.get("asset_type", "diagram") if isinstance(payload, dict) else "diagram",
            "source": source,
            "payload_ref": f"quality_report.flowcharts[{index}]",
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        })
    return {"schema": "kd-protected-assets/v1",
            "preservation_policy": "lossless-structured-payload",
            "items": items}


def _unyq(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        body = raw[1:-1]
        return re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), "\\" + m.group(1)), body)
    return raw


def build_sidecar(draft: dict, project: dict) -> dict:
    section_check = draft.get("section_check") or {}
    rendered_md = render_frontmatter({
        "knowledge_id": draft["knowledge_id"], "revision": draft["revision"],
        "title": draft["title"], "knowledge_type": draft["knowledge_type"],
        "status": draft["status"], "project_id": project["project_id"],
        "project_name": project["name"],
        "quality_score": (draft.get("quality_check") or {}).get("weighted_score"),
        "content_hash": draft["content_hash"],
        "products": extract_products(draft["body_md"]),
    }) + draft["body_md"]
    rag_payload = extract_rag_payload(rendered_md)
    return {
        "schema": "ckp-distilled-knowledge/v0.4",
        "knowledge_id": draft["knowledge_id"],
        "revision": draft["revision"],
        "title": draft["title"],
        "knowledge_type": draft["knowledge_type"],
        "topics": draft["topics"],
        "stage": draft["stage"],
        "status": draft["status"],
        "project": {"project_id": project["project_id"], "name": project["name"],
                    "industry": project["industry"], "products": project["products"],
                    "owner": project["owner"], "tags": project["tags"]},
        "task_id": draft["task_id"],
        "rules_version": draft["rules_version"],
        "rules_hash": draft.get("task_rules_hash"),
        "model": draft["model"],
        "content_hash": draft["content_hash"],
        "content_hash_definition": "sha256(正文原字节)；正文为 frontmatter 结束行 --- 之后的全部字节；"
                                   "该哈希不覆盖 frontmatter 与 sidecar 本身",
        "created_at": draft["created_at"],
        "sources": draft["sources"],
        "human_inputs": draft.get("task_human_inputs", []),
        "gaps": draft.get("task_gaps", []),
        "section_check": section_check,
        "quality_check": draft.get("quality_check") or {"status": "not_recorded"},
        "quality_report": draft.get("quality_report") or {},
        "protected_assets": _protected_assets_manifest(draft.get("quality_report") or {}),
        "rag_selection": {"schema": rag_payload["schema"],
                          "routing_valid": rag_payload["routing_valid"],
                          "errors": rag_payload["errors"],
                          "sections": rag_payload["sections"]},
        "note": ("草稿状态：未经项目经理审核，不代表已入库；content_hash 一致不证明内容事实正确。"
                 "审核与发布由后续服务管理。RAG 切片只读取显式 kd-section 标识；"
                 "缺失或无效标识时必须停止入库。"),
    }


def export_draft(draft: dict, project: dict, out_dir: Path,
                 section_check: dict | None = None) -> dict:
    """导出同一修订总是产生相同字节：元数据取自草稿行与提交时快照。"""
    out_dir = Path(out_dir).expanduser().resolve()
    project_dir = out_dir / project["project_id"]
    project_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{draft['knowledge_id']}_rev{draft['revision']:03d}"
    meta = {key: draft.get(key) for key in FM_SCALARS}
    meta["project_name"] = project["name"]
    meta["products"] = extract_products(draft["body_md"])
    quality_check = draft.get("quality_check") or {}
    meta["quality_score"] = quality_check.get("weighted_score")
    md_text = render_frontmatter(meta) + draft["body_md"]
    md_path = project_dir / f"{stem}.md"
    json_path = project_dir / f"{stem}.json"
    md_path.write_bytes(md_text.encode("utf-8"))
    json_path.write_bytes(json.dumps(
        build_sidecar(draft, project), ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8"))
    return {"md": str(md_path), "json": str(json_path)}


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def split_frontmatter(md_text: str) -> tuple[dict, str]:
    """解析 frontmatter（测试与工具用）；返回（元数据, 正文文本）。

    标量保持标量、列表保持列表，长度为 1 的列表不折叠为标量（F14）。
    """
    match = _FM_RE.match(md_text)
    if not match:
        raise ValueError("frontmatter 格式不符（须以 --- 行开始、以 --- 行结束）")
    meta: dict[str, Any] = {}
    current_key = None
    for line in match.group(1).split("\n"):
        if line.startswith("  - ") and current_key is not None:
            if not isinstance(meta[current_key], list):
                meta[current_key] = [meta[current_key]]
            meta[current_key].append(_unyq(line[4:].strip()))
            continue
        if ":" in line:
            key, _, raw = line.partition(":")
            current_key = key.strip()
            raw = raw.strip()
            meta[current_key] = _unyq(raw) if raw else []
    return meta, md_text[match.end():]


def verify_md(md_path: Path) -> dict:
    """独立校验导出的 md：按原字节重算正文哈希并与 frontmatter 比对。

    校验范围只有正文：frontmatter 或 sidecar 的改动不会被 content_hash 发现。
    任何字节变化（含 LF→CRLF）都会导致不匹配。
    """
    md_path = Path(md_path).expanduser()
    data = md_path.read_bytes()
    result: dict = {"file": str(md_path), "match": False,
                    "scope": "仅正文原字节；不覆盖 frontmatter 与 sidecar"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        result["error"] = f"文件不是有效 UTF-8: {exc}"
        return result
    match = _FM_RE.match(text)
    if not match:
        result["error"] = "frontmatter 格式不符（须以 --- 行开始、以 --- 行结束，LF 换行）；若文件被改为 CRLF 也会判不匹配"
        return result
    try:
        meta, _ = split_frontmatter(text)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    body_bytes = data[len(match.group(0).encode("utf-8")):]
    actual = hashlib.sha256(body_bytes).hexdigest()
    claimed = str(meta.get("content_hash", ""))
    result.update({"knowledge_id": meta.get("knowledge_id"),
                   "revision": meta.get("revision"), "title": meta.get("title"),
                   "status": meta.get("status"), "body_bytes": len(body_bytes),
                   "content_hash_claimed": claimed, "content_hash_actual": actual,
                   "match": claimed == actual})
    return result
