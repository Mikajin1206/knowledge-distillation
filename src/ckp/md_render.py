"""蒸馏知识 md 产出：frontmatter + 正文导出，及 content_hash 独立校验。

Markdown 草稿与来源 sidecar 的产出契约：
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

from ckp import db

# frontmatter 中只放标量与字符串列表；完整来源/补充/缺口/检查记录在 sidecar。
FM_SCALARS = ("knowledge_id", "revision", "title", "knowledge_type", "status",
              "project_id", "project_name", "stage", "owner", "task_id",
              "rules_version", "model", "quality_score", "quality_score_version",
              "created_at", "content_hash")
FM_LISTS = ("topics", "industry", "products")


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


def _unyq(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        body = raw[1:-1]
        return re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), "\\" + m.group(1)), body)
    return raw


def build_sidecar(draft: dict, project: dict) -> dict:
    section_check = draft.get("section_check") or {}
    return {
        "schema": "ckp-distilled-knowledge/v0.2",
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
        "note": ("草稿状态：未经项目经理审核，不代表已入库；content_hash 一致不证明内容事实正确。"
                 "审核与发布由后续服务管理"),
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
    meta["industry"] = project["industry"]
    meta["products"] = project["products"]
    meta["owner"] = project["owner"]
    meta["topics"] = draft["topics"]
    quality_check = draft.get("quality_check") or {}
    meta["quality_score"] = quality_check.get("weighted_score")
    meta["quality_score_version"] = quality_check.get("score_version")
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
