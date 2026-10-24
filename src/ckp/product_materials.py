"""Direct Word intake and retrieval for the product-material RAG corpus.

The current intake accepts product manuals only. Training material remains a
reserved category for a future iteration. Accepted originals are parsed into
source-linked sections and chunks without the project distillation or review
workflow. MongoDB/Milvus collections remain in the existing sandbox deployment
and are routed separately from the six project lifecycle stages.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document

from ckp import db
from ckp.catalog import validate_selection
from ckp.intake import hash_file
from ckp.office_parser import (MCP_CONVERT_IMAGE_FORMATS, MCP_NATIVE_IMAGE_FORMATS,
                               PARSER_VERSION, inspect_office, read_docx_image,
                               render_image_for_mcp)
from ckp.store import Store
from ckp.knowledge_types import (
    PRODUCT_MATERIAL_COLLECTIONS, PRODUCT_MATERIAL_CATEGORIES,
    validate_product_material_category, validate_product_material_ingest_category,
)
from chunks_and_in_vector.pipeline import SandboxRAG

PRODUCT_MATERIAL_INDEX_VERSION = f"{PARSER_VERSION}+figure-ref-v2+toc-clean-v4"
PRODUCT_MATERIAL_STAGE = ""
_FIGURE_CODE = r"\d+(?:\s*[.\-‐‑‒–—－．。＿_/／]\s*\d+){0,8}"
_FIGURE_CAPTION = re.compile(
    rf"^\s*(?P<label>[\u3400-\u9fff]{{0,10}}?(?:图例|图))\s*[:：、]?\s*"
    rf"(?P<code>{_FIGURE_CODE})(?![\d.])\s*(?P<title>.*)$"
)
_FIGURE_CAPTION_REVERSED = re.compile(
    rf"^\s*(?P<code>{_FIGURE_CODE})(?![\d.])\s*"
    rf"(?P<label>[\u3400-\u9fff]{{0,10}}?(?:图例|图))\s*[:：、]?\s*(?P<title>.*)$"
)
_UNNUMBERED_FIGURE_CAPTION = re.compile(
    r"^\s*(?P<label>[\u3400-\u9fff]{0,10}?(?:图例|图))\s*"
    r"(?:[:：、]|\s+)\s*(?P<title>[^\d]{1,80})$"
)


def _normalize_figure_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return value.translate(str.maketrans({"‐": "-", "‑": "-", "‒": "-",
                                         "–": "-", "—": "-", "－": "-",
                                         "．": ".", "。": ".", "＿": "_"}))


def _parse_figure_caption(value: str) -> dict | None:
    """Parse a caption using its own label and numeric code, independent of a manual's punctuation style."""
    value = _normalize_figure_text(value).strip()
    match = _FIGURE_CAPTION.match(value) or _FIGURE_CAPTION_REVERSED.match(value)
    if not match:
        return None
    label = re.sub(r"\s+", "", match.group("label"))
    code = re.sub(r"\s*([.\-＿_/])\s*", r"\1", match.group("code"))
    pieces = re.split(r"[.\-_/]", code)
    # Preserve the established chapter-sequence spelling (5.2-01) while
    # retaining fully hierarchical codes such as 05.02.01.
    if (len(pieces) >= 3 and pieces[-1].isdigit() and len(pieces[-1]) == 2
            and any(len(piece) != 2 for piece in pieces[:-1])
            and code.endswith("." + pieces[-1])):
        code = code[:-(len(pieces[-1]) + 1)] + "-" + pieces[-1]
    display_id = f"{label}{code}" if label in {"图", "图例"} else f"{label} {code}"
    return {"label": label, "code": code, "title": match.group("title").strip(),
            "source_text": value,
            "figure_id": display_id, "figure_key": _figure_key(label, code),
            "separators": re.findall(r"[.\-_/]", code)}


def _figure_key(label: str, code: str) -> str:
    normalized_label = re.sub(r"\s+", "", _normalize_figure_text(label)).casefold()
    digits = re.findall(r"\d+", _normalize_figure_text(code))
    return f"{normalized_label}:{'.'.join(digits)}"


def _figure_key_from_id(value: str) -> str | None:
    parsed = _parse_figure_caption(value)
    return parsed["figure_key"] if parsed and not parsed["title"] else None


def _figure_query_id(query: str) -> str | None:
    query = _normalize_figure_text(query).strip()
    if re.fullmatch(r"图@(?:body_order:\d+(?:#\d+)?|table:\d+#\d+)", query):
        return query
    parsed = _parse_figure_caption(query)
    if parsed and not parsed["title"]:
        return parsed["figure_id"]
    return None


def _body_order_from_ref(value: str) -> int | None:
    match = re.search(r":body_order:(\d+)$", value or "")
    return int(match.group(1)) if match else None


def _asset_position(asset: dict, table_orders: dict[int, int]) -> tuple[int | None, int | None]:
    ref = asset.get("block_ref", "")
    paragraph_order = _body_order_from_ref(ref)
    if paragraph_order is not None:
        return paragraph_order, None
    table_match = re.search(r":table:(\d+)$", ref)
    if table_match:
        table_number = int(table_match.group(1))
        return table_orders.get(table_number), table_number
    return None, None


def _caption_adjacent_to_image(caption_order: int, image_order: int, manifest: dict,
                               assets: list[dict], image_relationship: str) -> bool:
    """Require an uninterrupted Word block boundary between caption/reference and image."""
    low, high = sorted((caption_order, image_order))
    if low == high:
        return True
    for paragraph in manifest.get("paragraphs", []):
        order = paragraph.get("body_order")
        if not isinstance(order, int) or not low < order < high:
            continue
        style = str(paragraph.get("style") or "")
        if (str(paragraph.get("text") or "").strip()
                or paragraph.get("heading_level") is not None
                or paragraph.get("outline_level") is not None
                or _is_toc_style(style)):
            return False
    for table in manifest.get("tables", []):
        order = table.get("body_order")
        if isinstance(order, int) and low < order < high:
            return False
    for asset in assets:
        order = _body_order_from_ref(asset.get("block_ref", ""))
        if (asset.get("kind") == "image" and asset.get("relationship_id") != image_relationship
                and isinstance(order, int) and low < order < high):
            return False
    return True


def _figure_location_samples(refs: list[dict]) -> list[dict]:
    return [{"figure_id": ref["figure_id"], "locator_type": ref.get("locator_type"),
             "caption_title": ref.get("caption_title", ""),
             "heading_path": ref.get("heading_path", []),
             "image_body_order": ref.get("image_body_order"),
             "caption_body_order": ref.get("caption_body_order"),
             "table_number": ref.get("table_number")}
            for ref in refs[:20]]


def _figure_references(manifest: dict) -> tuple[list[dict], dict[int, list[dict]], dict]:
    """Detect per-document figure conventions and give every body image a stable locator."""
    paragraphs = manifest.get("paragraphs", [])
    assets = manifest.get("assets", [])
    counts: dict[str, int] = {}
    captions = []
    unnumbered = []
    unrecognized_numbered = []
    table_orders = {table.get("table"): table.get("body_order")
                    for table in manifest.get("tables", [])
                    if isinstance(table.get("table"), int)
                    and isinstance(table.get("body_order"), int)}
    caption_blocks = [(paragraph, paragraph.get("text") or "") for paragraph in paragraphs]
    for table in manifest.get("tables", []):
        for row in table.get("rows", []):
            for cell in row.get("cells", []):
                if cell.get("text"):
                    caption_blocks.append(({
                        "body_order": table.get("body_order"),
                        "index": f"table:{table.get('table')}:row:{row.get('row')}:cell:{cell.get('column')}",
                        "container_type": "table_cell",
                        "table_number": table.get("table"),
                    }, cell["text"]))
    for paragraph, text in caption_blocks:
        parsed = _parse_figure_caption(text)
        if parsed:
            key = parsed["figure_key"]
            counts[key] = counts.get(key, 0) + 1
            captions.append((parsed, paragraph))
        else:
            normalized_text = _normalize_figure_text(text).strip()
            if re.search(r"图", normalized_text) and re.search(r"\d", normalized_text):
                if re.search(r"图.*\d|\d.*图", normalized_text):
                    unrecognized_numbered.append({"source_text": normalized_text,
                                                  "body_order": paragraph.get("body_order"),
                                                  "paragraph_index": paragraph.get("index"),
                                                  "table_number": paragraph.get("table_number")})
            elif re.search(r"图", normalized_text) and len(normalized_text) <= 100:
                no_code = _UNNUMBERED_FIGURE_CAPTION.match(normalized_text)
                is_reference = re.search(r"(?:如下图|见图|参见图|如图所示|图如下)", normalized_text)
                is_figure_title = normalized_text.endswith("图")
                if not (no_code or is_reference or is_figure_title):
                    continue
                unnumbered.append({"label": (re.sub(r"\s+", "", no_code.group("label"))
                                              if no_code else "图"),
                                   "title": (no_code.group("title").strip()
                                             if no_code else normalized_text),
                                   "body_order": paragraph.get("body_order"),
                                   "paragraph_index": paragraph.get("index"),
                                   "table_number": paragraph.get("table_number")})

    refs, at_order = [], {}
    mapped_relationships: set[str] = set()
    for parsed, paragraph in captions:
        if counts[parsed["figure_key"]] != 1:
            continue
        caption_order = paragraph.get("body_order")
        if not isinstance(caption_order, int):
            continue
        candidates = []
        for asset in assets:
            image_order, table_number = _asset_position(asset, table_orders)
            if (asset.get("kind") == "image" and asset.get("relationship_id")
                    and asset.get("part") and image_order is not None
                    and asset["relationship_id"] not in mapped_relationships
                    and image_order <= caption_order
                    and _caption_adjacent_to_image(caption_order, image_order, manifest,
                                                   assets, asset["relationship_id"])):
                candidates.append((image_order, table_number, asset))
        if len(candidates) != 1:
            continue
        nearest_order, table_number, asset = candidates[0]
        ref = {"figure_id": parsed["figure_id"], "figure_key": parsed["figure_key"],
               "locator_type": "numbered_caption",
               "label": parsed["label"], "number_code": parsed["code"],
               "caption_title": parsed["title"],
               "relationship_id": asset["relationship_id"],
               "part": asset["part"], "image_body_order": nearest_order,
               "caption_body_order": caption_order}
        if table_number is not None:
            ref["table_number"] = table_number
        refs.append(ref)
        at_order.setdefault(caption_order, []).append(ref)
        mapped_relationships.add(asset["relationship_id"])

    # Product manuals can contain titled or referenced images with no formal
    # figure number. Keep their Word body position as an exact, revision-bound
    # locator instead of inventing a numbering scheme.
    image_ordinals: dict[tuple[str, int], int] = {}
    for asset in assets:
        image_order, table_number = _asset_position(asset, table_orders)
        relationship_id = asset.get("relationship_id")
        if (asset.get("kind") != "image" or not relationship_id or not asset.get("part")
                or image_order is None or relationship_id in mapped_relationships):
            continue
        position_key = ("table", table_number) if table_number is not None else ("body", image_order)
        image_ordinals[position_key] = image_ordinals.get(position_key, 0) + 1
        ordinal = image_ordinals[position_key]
        locator = (f"图@table:{table_number}#{ordinal}" if table_number is not None
                   else f"图@body_order:{image_order}" + (f"#{ordinal}" if ordinal > 1 else ""))
        positional_captions = unnumbered + [
            {"label": "待确认编号", "title": item["source_text"],
             "body_order": item.get("body_order"),
             "table_number": item.get("table_number")}
            for item in unrecognized_numbered]
        nearby_captions = [item for item in positional_captions
                           if isinstance(item.get("body_order"), int)
                           and (item.get("table_number") == table_number if table_number is not None
                                else _caption_adjacent_to_image(item["body_order"], image_order,
                                                                manifest, assets, relationship_id))]
        same_position_images = [candidate for candidate in assets
                                if candidate.get("kind") == "image"
                                and _asset_position(candidate, table_orders)
                                == (image_order, table_number)]
        if not nearby_captions:
            # Do not turn uncaptioned decorative artwork into a retrieval
            # target. A body position becomes a locator only when the document
            # itself names or references a figure there.
            continue
        nearby_caption = (nearby_captions[0] if len(nearby_captions) == 1
                          and len(same_position_images) == 1 else None)
        ref = {"figure_id": locator, "locator_type": "body_position",
               "label": nearby_caption["label"] if nearby_caption else "图",
               "number_code": None,
               "caption_title": nearby_caption["title"] if nearby_caption else "",
               "relationship_id": relationship_id, "part": asset["part"],
               "image_body_order": image_order,
               "caption_body_order": nearby_caption.get("body_order") if nearby_caption else None,
               "position_confirmed_by": "Word 正文或表格内嵌图片位置"}
        if table_number is not None:
            ref["table_number"] = table_number
        refs.append(ref)
        at_order.setdefault(image_order, []).append(ref)
        mapped_relationships.add(relationship_id)
    patterns: dict[tuple[str, int, str, tuple[int, ...]], list[dict]] = {}
    for parsed, paragraph in captions:
        code_segments = re.findall(r"\d+", parsed["code"])
        pattern = (parsed["label"], len(code_segments),
                   "".join(parsed["separators"]), tuple(map(len, code_segments)))
        patterns.setdefault(pattern, []).append({"figure_id": parsed["figure_id"],
                                                  "source_text": parsed["source_text"],
                                                  "body_order": paragraph.get("body_order")})
    rule_profile = {
        "status": "needs_review" if unrecognized_numbered else
                  "detected" if patterns or unnumbered or refs else "none_detected",
        "numbered_patterns": [
            {"label": label, "number_segments": depth, "separators": separators,
             "segment_widths": list(widths),
             "count": len(examples), "examples": examples[:5]}
            for (label, depth, separators, widths), examples in patterns.items()
        ],
        "unnumbered_candidates": unnumbered[:20],
        "unrecognized_numbered_candidates": unrecognized_numbered[:20],
        "requires_confirmation": bool(unnumbered or unrecognized_numbered) or any(len(items) < 2 for items in patterns.values()),
        "confirmation_state": ("needs_human_review" if bool(unnumbered or unrecognized_numbered)
                               or any(len(items) < 2 for items in patterns.values())
                               else "consistent"),
        "image_locator_rule": "有正式图号时按图号绑定；没有图号但有标题或正文引用时按 Word 正文/表格位置与同位置图片序号绑定",
        "unlabeled_image_count": sum(1 for ref in refs if ref["locator_type"] == "body_position"
                                     and not ref.get("caption_title")),
        "mapped_figure_count": len(refs),
    }
    return refs, at_order, rule_profile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_text(table: dict) -> str:
    rows = []
    for row in table.get("rows", []):
        cells = [re.sub(r"\s+", " ", cell.get("text", "")).strip()
                 for cell in row.get("cells", [])]
        if any(cells):
            rows.append(f"第{row.get('row', len(rows) + 1)}行：" + " | ".join(cells))
    return f"表格 {table.get('table', '')}\n" + "\n".join(rows) if rows else ""


def _numbered_heading_level(text: str) -> int | None:
    value = text.strip()
    if re.match(r"^第[一二三四五六七八九十百0-9]+[章节篇]\s*\S", value):
        return 1
    if re.match(r"^[一二三四五六七八九十]+[、.．]\s*\S", value):
        return 1
    decimal = re.match(r"^(\d+(?:\.\d+){1,5})[、.．]?\s*(?=\D)\S", value)
    if decimal:
        return min(decimal.group(1).count(".") + 1, 6)
    return None


def _is_toc_style(style: str) -> bool:
    """Recognize generated Word table-of-contents entry styles."""
    return bool(re.fullmatch(r"(?:toc|目录)\s*[1-9]?", style.strip(), re.I))


def _style_heading_level(style: str) -> int | None:
    match = re.search(r"(?:heading|标题)\s*([1-6])$", style.strip(), re.I)
    return int(match.group(1)) if match else None


def _toc_level(style: str) -> int | None:
    match = re.fullmatch(r"(?:toc|目录)\s*([1-9])?", style.strip(), re.I)
    return int(match.group(1)) if match and match.group(1) else None


_HEADING_NUMBER = re.compile(
    r"^\s*(?P<number>第[一二三四五六七八九十百0-9]+[章节篇]|"
    r"[0-9]+(?:[.．][0-9]+)*)(?:[、.．)）:-]?\s*)(?P<title>.+?)\s*$"
)


def _numbered_title(value: str) -> tuple[str, str] | None:
    """Return a visible heading number and title without changing source text."""
    text = re.sub(r"[.．·…]{2,}\s*\d+\s*$", "", value.strip())
    text = re.sub(r"\s+\d{1,4}\s*$", "", text)
    match = _HEADING_NUMBER.match(text)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group("title")).strip(" .。\t")
    number = match.group("number").replace("．", ".")
    return (number, title) if title else None


def _heading_quality_warnings(manifest: dict) -> list[dict]:
    """Compare Word TOC/body entries and numbered heading structure without rewriting labels."""
    toc: dict[str, list[dict]] = {}
    body: dict[str, list[dict]] = {}
    numbered_body = []
    structural_stack: dict[int, dict] = {}
    for paragraph in manifest.get("paragraphs", []):
        style = str(paragraph.get("style") or "")
        text = str(paragraph.get("text") or "").strip()
        numbered = _numbered_title(text)
        if not numbered:
            continue
        number, title = numbered
        title_key = re.sub(r"[^\w\u4e00-\u9fff]+", "", title).casefold()
        if not title_key:
            continue
        index = int(paragraph.get("index", 0) or 0)
        toc_entry = _is_toc_style(style)
        target = toc if toc_entry else body
        if not toc_entry and not (paragraph.get("heading_level") is not None
                                  or paragraph.get("outline_level") is not None
                                  or _style_heading_level(style) is not None):
            continue
        try:
            level = (int(paragraph.get("heading_level")) if paragraph.get("heading_level") is not None
                     else int(paragraph.get("outline_level")) + 1
                     if paragraph.get("outline_level") is not None else None)
        except (TypeError, ValueError):
            level = None
        if level is None:
            level = _toc_level(style) if toc_entry else _style_heading_level(style)
        entry = {
            "number": number, "index": index, "title": title, "level": level,
        }
        target.setdefault(title_key, []).append(entry)
        if not toc_entry:
            if level is not None:
                entry["structural_parent"] = structural_stack.get(level - 1)
                for ancestor_level in list(structural_stack):
                    if ancestor_level >= level:
                        del structural_stack[ancestor_level]
                structural_stack[level] = entry
            numbered_body.append((paragraph, entry))

    warnings = []
    for paragraph, entry in numbered_body:
        if entry["level"] is None or not re.fullmatch(r"\d+(?:\.\d+)*", entry["number"]):
            continue
        number_parts = entry["number"].split(".")
        parent = entry.get("structural_parent")
        if len(number_parts) > 1 and parent is not None:
            expected_parent = ".".join(number_parts[:-1])
            parent_number = parent["number"]
            if (re.fullmatch(r"\d+(?:\.\d+)*", parent_number)
                    and expected_parent != parent_number):
                warnings.append({
                    "code": "heading_number_parent_mismatch",
                    "severity": "review",
                    "title": entry["title"],
                    "number": entry["number"],
                    "expected_parent_number": expected_parent,
                    "actual_parent_number": parent_number,
                    "body_paragraph": entry["index"],
                    "parent_paragraph": parent["index"],
                    "action": "核对本章与上级标题的编号关系；保留原文，不自动移动或改号",
                })

    sibling_numbers: dict[str, list[dict]] = {}
    for _, entry in numbered_body:
        number_parts = entry["number"].split(".")
        if len(number_parts) < 2 or not all(part.isdigit() for part in number_parts):
            continue
        parent_number = ".".join(number_parts[:-1])
        sibling_numbers.setdefault(parent_number, []).append(entry)
    for parent_number, entries in sibling_numbers.items():
        by_number: dict[int, list[dict]] = {}
        for entry in entries:
            by_number.setdefault(int(entry["number"].split(".")[-1]), []).append(entry)
        observed = sorted(by_number)
        for previous, following in zip(observed, observed[1:]):
            if following > previous + 1:
                warnings.append({
                    "code": "heading_number_gap",
                    "severity": "review",
                    "parent_number": parent_number,
                    "missing_numbers": [f"{parent_number}.{n}"
                                        for n in range(previous + 1, following)],
                    "before_paragraph": by_number[previous][0]["index"],
                    "after_paragraph": by_number[following][0]["index"],
                    "action": "核对章节是否缺失、未被识别或编号跳号；不自动补章或改号",
                })

    matched_in_toc_order = []
    for title_key, toc_entries in toc.items():
        body_entries = body.get(title_key, [])
        if len(toc_entries) != 1 or len(body_entries) > 1:
            if body_entries:
                warnings.append({
                    "code": "toc_body_heading_match_ambiguous",
                    "severity": "review",
                    "title": toc_entries[0]["title"],
                    "toc_entries": len(toc_entries),
                    "body_entries": len(body_entries),
                    "action": "标题存在重复匹配；请人工核对目录与正文，不自动关联或改写",
                })
            continue
        toc_entry = toc_entries[0]
        if not body_entries:
            warnings.append({
                "code": "toc_heading_missing_from_body",
                "severity": "review",
                "title": toc_entry["title"],
                "toc_number": toc_entry["number"],
                "toc_paragraph": toc_entry["index"],
                "action": "核对目录是否过期或正文标题是否未被识别；不自动补写章节",
            })
            continue
        body_entry = body_entries[0]
        matched_in_toc_order.append((toc_entry, body_entry))
        if toc_entry["number"] != body_entry["number"]:
            warnings.append({
                "code": "toc_body_heading_number_mismatch",
                "severity": "review",
                "title": toc_entry["title"],
                "toc_number": toc_entry["number"],
                "body_number": body_entry["number"],
                "toc_paragraph": toc_entry["index"],
                "body_paragraph": body_entry["index"],
                "action": "核对 Word 正文与自动目录；保留原文编号，不自动改写",
            })
        if (toc_entry["level"] is not None and body_entry["level"] is not None
                and toc_entry["level"] != body_entry["level"]):
            warnings.append({
                "code": "toc_body_heading_level_mismatch",
                "severity": "review",
                "title": toc_entry["title"],
                "toc_level": toc_entry["level"],
                "body_level": body_entry["level"],
                "toc_paragraph": toc_entry["index"],
                "body_paragraph": body_entry["index"],
                "action": "核对 Word 标题样式与目录层级；不自动调整层级",
            })

    ordered_matches = sorted(matched_in_toc_order, key=lambda pair: pair[0]["index"])
    body_positions = [pair[1]["index"] for pair in ordered_matches]
    if body_positions != sorted(body_positions):
        warnings.append({
            "code": "toc_body_heading_order_mismatch",
            "severity": "review",
            "action": "目录标题顺序与正文顺序不一致；请核对目录是否过期或正文结构，不自动移动内容",
        })
    return warnings


def _sections(manifest: dict, material_id: str, revision: int,
              splitter=None, figure_refs_by_order: dict[int, list[dict]] | None = None
              ) -> tuple[list[dict], list[dict]]:
    figure_refs_by_order = figure_refs_by_order or {}
    blocks = []
    for item in manifest.get("paragraphs", []):
        blocks.append((item.get("body_order", 0), "paragraph", item))
    for item in manifest.get("tables", []):
        blocks.append((item.get("body_order", 0), "table", item))
    for body_order, refs in figure_refs_by_order.items():
        for ref in refs:
            blocks.append((body_order, "figure_ref", ref))
    blocks.sort(key=lambda item: (item[0], 0 if item[1] == "figure_ref" else 1))
    sections: list[dict] = []
    heading_path: list[str] = []
    current = None
    for body_order, kind, item in blocks:
        if kind == "figure_ref":
            text = f"[[FIGURE_REF: {item['figure_id']}]]"
        elif kind == "paragraph":
            text = (item.get("text") or "").strip()
            outline = item.get("outline_level")
            style = str(item.get("style") or "")
            # TOC rows often contain decimal chapter numbers and otherwise
            # look like headings. They are navigation noise, not body content.
            if _is_toc_style(style):
                continue
            try:
                level = int(item.get("heading_level")) if item.get("heading_level") is not None else None
            except (TypeError, ValueError):
                level = None
            if outline is not None:
                if level is None:
                    try:
                        outline_level = int(outline)
                        level = outline_level + 1 if 0 <= outline_level <= 5 else None
                    except (TypeError, ValueError):
                        level = None
            if level is None:
                match = re.search(r"(?:heading|标题)\s*([1-6])$", style, re.I)
                if match:
                    level = int(match.group(1))
            if level is None and len(text) <= 100:
                level = _numbered_heading_level(text)
            if level and text:
                heading_path = heading_path[:level - 1] + [text]
                current = None
                continue
        elif kind == "table":
            text = _table_text(item)
        if not text:
            continue
        if current is None:
            section_index = len(sections) + 1
            section_id = hashlib.sha256(
                f"{material_id}@{revision}:{section_index}".encode()).hexdigest()[:20]
            current = {"section_id": section_id,
                       "heading_path": list(heading_path) or ["正文"], "blocks": []}
            sections.append(current)
        current["blocks"].append({"body_order": body_order, "type": kind,
                                   "source_index": item.get("index", item.get("table")),
                                   "text": text})
        if kind == "figure_ref":
            current.setdefault("figure_ids", []).append(item["figure_id"])

    rendered = []
    for section in sections:
        section["text"] = "\n\n".join(block["text"] for block in section["blocks"])
        del section["blocks"]
        section_refs = [ref for ref in sum(figure_refs_by_order.values(), [])
                        if ref["figure_id"] in section.get("figure_ids", [])]
        for ref in section_refs:
            ref["heading_path"] = list(section["heading_path"])
        rendered.append({**section, "heading": " / ".join(section["heading_path"]),
                         "figure_refs": section_refs})
        section.pop("figure_ids", None)
    if not rendered:
        raise ValueError("Word 文档未提取到可索引的正文或表格")

    chunks = []
    for section in rendered:
        text = section["text"]
        heading = section["heading"]
        if splitter:
            fragments = splitter(heading, text)
        else:
            # Fixture embeddings have no tokenizer; respect the same 4096-byte
            # ceiling used by the production Qwen splitter.
            fragments = []
            cursor = 0
            while cursor < len(text):
                end = cursor
                size = len(f"## {heading}\n\n".encode("utf-8"))
                while end < len(text) and size + len(text[end].encode("utf-8")) <= 4096:
                    size += len(text[end].encode("utf-8")); end += 1
                if end == cursor:
                    raise ValueError("章节标题过长，无法满足向量输入字节上限")
                if end < len(text):
                    boundary = max(text.rfind("\n\n", cursor, end), text.rfind("。", cursor, end),
                                   text.rfind("；", cursor, end))
                    if boundary > cursor + 1200:
                        end = boundary + (1 if text[boundary] in "。；" else 0)
                fragments.append((text[cursor:end], cursor))
                cursor = end
        for fragment, offset in fragments:
            if fragment:
                chunks.append({"section_id": section["section_id"],
                               "heading_path": section["heading_path"],
                               "offset": offset, "text": fragment})
    return rendered, chunks


class ProductMaterials:
    def __init__(self, data_dir: Path, repo_root: Path, readonly: bool = False):
        self.store = Store(data_dir, readonly=readonly)
        self.data_dir = self.store.data_dir
        self.repo_root = Path(repo_root)
        self._rag = None

    @property
    def rag(self):
        if self._rag is None:
            self._rag = SandboxRAG()
        return self._rag

    def close(self):
        if self._rag is not None:
            self._rag.close()
        self.store.conn.close()

    def _collection(self, category: str) -> str:
        return self.rag.prefix + PRODUCT_MATERIAL_COLLECTIONS[category]

    def _has_schema(self) -> bool:
        return bool(self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_materials'"
        ).fetchone())

    def ingest(self, path: str | Path, category: str, product: str,
               domains: list[str] | None) -> dict:
        category = validate_product_material_ingest_category(category)
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ValueError("产品资料必须提供文件绝对路径")
        if source.suffix.lower() not in {".docx", ".doc"}:
            raise ValueError("产品资料首版只接受 Word 文件 .doc/.docx")
        if not source.is_file():
            raise FileNotFoundError("指定的 Word 文件不存在")
        product, domains = validate_selection(self.repo_root, product, domains)
        source = source.resolve()
        sha256 = hash_file(source)
        size = source.stat().st_size
        title = source.stem.strip() or source.name
        existing = db.rowdict(self.store.conn.execute(
            "SELECT * FROM product_materials WHERE category=? AND product=? AND title=?",
            (category, product, title)).fetchone())
        active_identities = db.rowdicts(self.store.conn.execute(
            "SELECT * FROM product_materials WHERE product=? AND title=? AND active=1 ORDER BY updated_at DESC",
            (product, title)).fetchall())
        conflicting_identity = next((row for row in active_identities
                                     if row["category"] != category), None)
        if conflicting_identity:
            raise ValueError(
                f"同一产品下已存在活动资料类别 {conflicting_identity['category']}；"
                "为避免同一资料重复归类，请先按精确修订撤回旧类别，再重新确认分类入库")
        if existing:
            current = db.rowdict(self.store.conn.execute(
                "SELECT * FROM product_material_versions WHERE version_id=?",
                (existing["current_version_id"],)).fetchone()) if existing.get("current_version_id") else None
            if (current and current["sha256"] == sha256 and current["state"] == "ready"
                    and current["parser_version"] == PRODUCT_MATERIAL_INDEX_VERSION
                    and db.loads(current.get("domains_json"), []) == domains):
                record_id = f"{existing['material_id']}@{current['revision']}"
                active_head = self.rag.mongo.get_collection(
                    self.rag.prefix + "product_material_heads").find_one(
                        {"_id": existing["material_id"], "record_id": record_id,
                         "active": True, "collection": self._collection(category)})
                indexed_record = (self.rag.mongo.get_collection(self._collection(category)).find_one(
                    {"_id": record_id, "state": "ready", "version_id": current["version_id"],
                     "source_sha256": sha256}) if active_head else None)
                if indexed_record:
                    indexed_chunks = len(indexed_record.get("chunk_ids", []))
                    section_count = len(indexed_record.get("sections", [])) or int(current["section_count"])
                    # A record becomes ready only after the initial write has
                    # passed the per-chunk Milvus readback check below. Preserve
                    # that result in the idempotent response without claiming
                    # that this call performed a fresh readback.
                    verified_chunks = int(indexed_record.get("verified_chunks", indexed_chunks))
                    return {"state": "unchanged", "material_id": existing["material_id"],
                            "revision": current["revision"], "version_id": current["version_id"],
                            "source_sha256": sha256,
                            "category": category, "products": product, "domain": domains,
                            "stage": PRODUCT_MATERIAL_STAGE,
                            "active": True, "sections": section_count,
                            "chunks": indexed_chunks, "indexed_chunks": indexed_chunks,
                            "verified_chunks": verified_chunks,
                            "verification_basis": "original_ingest",
                            "readback_performed_this_call": False,
                            "embedded_images_not_ocr_processed": int(indexed_record.get("image_count", current.get("image_count", 0))),
                            "figure_ref_count": len(indexed_record.get("figure_refs", [])),
                            "figure_ids_sample": [ref["figure_id"] for ref in indexed_record.get("figure_refs", [])[:20]],
                            "figure_locations_sample": _figure_location_samples(indexed_record.get("figure_refs", [])),
                            "figure_rule": indexed_record.get("figure_rule", {}),
                            "quality_warnings": indexed_record.get("quality_warnings", []),
                            "embedding_mode": self.rag.mode}

        duplicate = db.rowdict(self.store.conn.execute(
            "SELECT v.material_id, m.category, m.product, m.title, v.revision FROM product_material_versions v"
            " JOIN product_materials m ON m.material_id=v.material_id"
            " WHERE v.sha256=? AND v.state='ready' AND m.active=1",
            (sha256,)).fetchone())
        if duplicate and (not existing or duplicate["material_id"] != existing["material_id"]):
            raise ValueError(
                f"该原件已登记为 {duplicate['category']} / {duplicate['product']} / {duplicate['title']}；"
                "为避免重复索引，请使用现有资料记录或先确认分类变更")

        material_id = existing["material_id"] if existing else db.new_id("pm")
        revision = (db.rowdict(self.store.conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS n FROM product_material_versions WHERE material_id=?",
            (material_id,)).fetchone()) or {"n": 0})["n"] + 1
        version_id = db.new_id("pmv")
        temp_root = self.data_dir / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="product-material-", dir=temp_root) as tmp:
            manifest = inspect_office(source, Path(tmp), file_id=material_id,
                                      version_id=version_id, expected_sha256=sha256,
                                      copy_assets=False)
        figure_refs, figure_refs_by_order, figure_rule = _figure_references(manifest)
        quality_warnings = _heading_quality_warnings(manifest)
        sections, chunks = _sections(manifest, material_id, revision,
                                     getattr(self.rag.embedding, "split_section", None),
                                     figure_refs_by_order)
        self.store._store_blob(source, sha256, size)
        collection = self._collection(category)
        record_id = f"{material_id}@{revision}"
        records = self.rag.mongo.get_collection(collection)
        heads = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads")
        head = heads.find_one({"_id": material_id})
        if head and revision <= int(head.get("revision", 0)):
            raise ValueError("产品资料修订号必须高于当前活动版本")

        now = _now()
        if not existing:
            self.store.conn.execute(
                "INSERT INTO product_materials(material_id,category,product,title,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)", (material_id, category, product, title, now, now))
        else:
            self.store.conn.execute(
                "UPDATE product_materials SET product=?,updated_at=? WHERE material_id=?",
                (product, now, material_id))
        self.store.conn.execute(
            "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
            "domains_json,parser_version,section_count,image_count,state,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, material_id, revision, sha256, size, db.guess_media_type(source.name),
             source.name, db.dumps(domains), PRODUCT_MATERIAL_INDEX_VERSION, len(sections),
             len(manifest.get("assets", [])), "staging", now))
        self.store.conn.commit()

        fingerprint = hashlib.sha256(
            f"{material_id}@{revision}:{sha256}:{category}:{product}:{db.dumps(domains)}".encode()).hexdigest()
        ids = [hashlib.sha256(f"{collection}:{record_id}:{i}".encode()).hexdigest()
               for i in range(len(chunks))]
        record = {"_id": record_id, "material_id": material_id, "revision": revision,
                  "version_id": version_id, "source_sha256": sha256, "fingerprint": fingerprint,
                  "state": "staging", "chunk_ids": ids, "category": category,
                  "knowledge_class": "product_material", "products": product, "domain": domains,
                "stage": PRODUCT_MATERIAL_STAGE,
                  "title": title, "filename": source.name,
                  "content": "\n\n".join(f"## {' / '.join(s['heading_path'])}\n\n{s['text']}" for s in sections),
                  "sections": sections, "image_count": len(manifest.get("assets", [])),
                  "figure_refs": figure_refs,
                  "figure_rule": figure_rule,
                  "quality_warnings": quality_warnings,
                  "parser_version": PRODUCT_MATERIAL_INDEX_VERSION, "sandbox": True}
        records.replace_one({"_id": record_id}, record, upsert=True)
        documents = []
        for chunk in chunks:
            documents.append(Document(page_content=f"## {' / '.join(chunk['heading_path'])}\n\n{chunk['text']}",
                                      metadata={"url": f"original://{material_id}@{revision}",
                                                "mongo_id": record_id, "material_id": material_id,
                                                "revision": revision, "source_sha256": sha256,
                                                "category": category, "knowledge_class": "product_material",
                                                "products": product, "domain": domains,
                                                "stage": PRODUCT_MATERIAL_STAGE,
                                                "section_id": chunk["section_id"],
                                                "heading_path": chunk["heading_path"],
                                                "offset": chunk["offset"], "file_name": title,
                                                "source_filename": source.name, "sandbox": True,
                                                "embedding_mode": self.rag.mode}))
        activated = False
        try:
            vector_db = self.rag._db(collection)
            vector_db.insert(documents, ids=ids)
            expected = {cid: document for cid, document in zip(ids, documents)}
            verified = 0
            for offset in range(0, len(ids), 128):
                batch = ids[offset:offset + 128]
                rows = vector_db.query_by_ids(batch)
                if len(rows) != len(batch) or {row["id"] for row in rows} != set(batch):
                    raise ValueError("产品资料向量逐块读回不完整；未激活此修订")
                for row in rows:
                    expected_doc = expected[row["id"]]
                    if (row["text"] != expected_doc.page_content
                            or json.loads(row["metadata"]) != expected_doc.metadata):
                        raise ValueError("产品资料向量正文或元数据读回不一致；未激活此修订")
                verified += len(batch)
            records.update_one({"_id": record_id}, {"$set": {
                "state": "ready", "verified_chunks": verified,
                "section_count": len(sections), "chunk_count": len(chunks)}})
            new_head = {"_id": material_id, "record_id": record_id, "revision": revision,
                        "active": True, "collection": collection}
            if head:
                result = heads.replace_one({"_id": material_id, "record_id": head["record_id"],
                                            "active": head["active"], "collection": head["collection"]}, new_head)
                if result.matched_count != 1:
                    raise RuntimeError("产品资料活动版本已并发变化，请重试")
            else:
                heads.insert_one(new_head)
            activated = True
            self.store.conn.execute(
                "UPDATE product_material_versions SET state='ready' WHERE version_id=?", (version_id,))
            self.store.conn.execute(
                "UPDATE product_materials SET current_version_id=?,active=1,updated_at=? WHERE material_id=?",
                (version_id, _now(), material_id))
            self.store.conn.commit()
        except Exception:
            if activated:
                if head:
                    heads.replace_one({"_id": material_id, "record_id": record_id}, head)
                else:
                    heads.delete_one({"_id": material_id, "record_id": record_id})
            records.update_one({"_id": record_id}, {"$set": {"state": "failed"}})
            self.store.conn.execute(
                "UPDATE product_material_versions SET state='failed' WHERE version_id=?", (version_id,))
            if existing:
                self.store.conn.execute(
                    "UPDATE product_materials SET current_version_id=?,active=?,updated_at=? WHERE material_id=?",
                    (existing.get("current_version_id"), existing.get("active", 1), _now(), material_id))
            else:
                self.store.conn.execute(
                    "UPDATE product_materials SET current_version_id=NULL,active=0,updated_at=? WHERE material_id=?",
                    (_now(), material_id))
            self.store.conn.commit()
            raise
        return {"state": "indexed", "material_id": material_id, "revision": revision,
                "version_id": version_id, "source_sha256": sha256, "category": category,
                "products": product, "domain": domains, "stage": PRODUCT_MATERIAL_STAGE,
                "sections": len(sections),
                "chunks": len(chunks), "verified_chunks": verified,
                "embedded_images_not_ocr_processed": len(manifest.get("assets", [])),
                "figure_ref_count": len(figure_refs),
                "figure_ids_sample": [ref["figure_id"] for ref in figure_refs[:20]],
                "figure_locations_sample": _figure_location_samples(figure_refs),
                "figure_rule": figure_rule,
                "quality_warnings": quality_warnings,
                "sandbox": True, "embedding_mode": self.rag.mode}

    def _active_chunk_count(self, material_id: str, revision: int) -> int:
        for category in PRODUCT_MATERIAL_CATEGORIES:
            record = self.rag.mongo.get_collection(self._collection(category)).find_one(
                {"_id": f"{material_id}@{revision}", "state": "ready"})
            if record:
                return len(record.get("chunk_ids", []))
        return 0

    def list(self, product: str = "", category: str = "") -> list[dict]:
        if not self._has_schema():
            return []
        where, params = [], []
        if product:
            where.append("product=?"); params.append(product.strip())
        if category:
            where.append("category=?"); params.append(validate_product_material_category(category))
        sql = ("SELECT m.*,v.revision,v.sha256 AS source_sha256,v.state AS version_state,"
               "v.filename,v.domains_json FROM product_materials m LEFT JOIN product_material_versions v"
               " ON v.version_id=m.current_version_id"
               + (" WHERE " + " AND ".join("m." + item for item in where) if where else "")
               + " ORDER BY m.title")
        rows = []
        for row in self.store.conn.execute(sql, params).fetchall():
            item = dict(row)
            item["domain"] = db.loads(item.pop("domains_json", "[]"), [])
            item["products"] = item.pop("product")
            item["stage"] = PRODUCT_MATERIAL_STAGE
            rows.append(item)
        return rows

    def read(self, material_id: str, revision: int, source_sha256: str,
             section_ids: list[str] | None = None, full_document: bool = False) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        if full_document and section_ids:
            raise ValueError("full_document 与 section_ids 不能同时传入")
        item = db.rowdict(self.store.conn.execute(
            "SELECT m.category,v.* FROM product_materials m JOIN product_material_versions v"
            " ON v.material_id=m.material_id WHERE m.material_id=? AND v.revision=? AND v.state='ready'",
            (material_id, revision)).fetchone())
        if not item or item["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        record = self.rag.mongo.get_collection(self._collection(item["category"])).find_one(
            {"_id": f"{material_id}@{revision}", "state": "ready"})
        if not record:
            raise ValueError("产品资料当前不可读")
        sections = record.get("sections", [])
        if full_document:
            selected = sections
        elif section_ids:
            requested = set(section_ids)
            selected = [section for section in sections if section["section_id"] in requested]
            if len(selected) != len(requested):
                raise ValueError("section_ids 包含不属于该产品资料修订的章节")
        else:
            selected = [{"section_id": s["section_id"], "heading_path": s["heading_path"],
                         "characters": len(s["text"])} for s in sections]
        return {"material_id": material_id, "revision": revision, "source_sha256": source_sha256,
                "category": item["category"], "products": record.get("products", record.get("product")),
                "domain": record["domain"], "stage": PRODUCT_MATERIAL_STAGE,
                "title": record["title"], "filename": item["filename"],
                "sections": selected, "section_count": len(sections),
                "quality_warnings": record.get("quality_warnings", []),
                "embedded_images_not_ocr_processed": item["image_count"],
                "sandbox": True}

    def read_image(self, material_id: str, revision: int, source_sha256: str,
                   figure_id: str) -> tuple[bytes, str, dict]:
        """Return one exact, hash-bound original image without persisting a copy."""
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        item = db.rowdict(self.store.conn.execute(
            "SELECT m.category,v.* FROM product_materials m JOIN product_material_versions v"
            " ON v.material_id=m.material_id WHERE m.material_id=? AND v.revision=? AND v.state='ready'",
            (material_id, revision)).fetchone())
        if not item or item["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        record_id = f"{material_id}@{revision}"
        record = self.rag.mongo.get_collection(self._collection(item["category"])).find_one(
            {"_id": record_id, "state": "ready", "version_id": item["version_id"],
             "source_sha256": source_sha256})
        if not record:
            raise ValueError("产品资料当前不可读")
        query_id = _figure_query_id(figure_id)
        query_key = _figure_key_from_id(query_id) if query_id else None
        matches = [ref for ref in record.get("figure_refs", [])
                   if ref.get("figure_id") == figure_id
                   or (query_key and ref.get("figure_key") == query_key)]
        if len(matches) != 1:
            raise ValueError("该修订没有唯一、可定位的图片映射")
        blob = db.rowdict(self.store.conn.execute(
            "SELECT * FROM blobs WHERE sha256=?", (source_sha256,)).fetchone())
        if not blob:
            raise ValueError("产品资料原件存储引用缺失")
        source = self.data_dir / blob["rel_path"]
        if not source.is_file() or hash_file(source) != source_sha256:
            raise ValueError("产品资料原件不存在或 SHA-256 校验失败")

        temp_root = self.data_dir / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="product-material-image-", dir=temp_root) as tmp:
            # Content-addressed originals have no extension. Restore the
            # verified source filename suffix in the temporary workspace so
            # the Office parser can select its DOC/DOCX reader.
            suffix = Path(item["filename"]).suffix.lower()
            materialized_source = Path(tmp) / f"original{suffix}"
            shutil.copyfile(source, materialized_source)
            payload, image_format = read_docx_image(
                materialized_source, Path(tmp), relationship_id=matches[0]["relationship_id"],
                expected_part=matches[0]["part"], expected_sha256=source_sha256)
            image_format = image_format.lower().lstrip(".")
            if image_format in MCP_CONVERT_IMAGE_FORMATS:
                extracted = Path(tmp) / f"source.{image_format}"
                extracted.write_bytes(payload)
                rendered = render_image_for_mcp(extracted, Path(tmp) / "rendered")
                payload, image_format = rendered.read_bytes(), "png"
            if image_format not in MCP_NATIVE_IMAGE_FORMATS:
                raise ValueError(f"MCP 图片返回暂不支持该原图格式: .{image_format}")
        canonical_figure_id = matches[0]["figure_id"]
        section = next((s for s in record.get("sections", [])
                        if canonical_figure_id in s.get("figure_ids", [])), None)
        context = {"material_id": material_id, "revision": revision,
                   "source_sha256": source_sha256, "title": record.get("title"),
                   "filename": item["filename"], "figure_id": canonical_figure_id,
                   "heading_path": section.get("heading_path", []) if section else [],
                   "image_part": matches[0]["part"], "image_format": image_format,
                   "locator_type": matches[0].get("locator_type", "numbered_caption"),
                   "caption_title": matches[0].get("caption_title", ""),
                   "body_position": {"image_body_order": matches[0].get("image_body_order"),
                                     "caption_body_order": matches[0].get("caption_body_order")},
                   "mime_type": {"png": "image/png", "jpg": "image/jpeg",
                                 "jpeg": "image/jpeg", "gif": "image/gif",
                                 "webp": "image/webp"}[image_format],
                   "hash_verified": True,
                   "image_processed": False,
                   "message": "已按图号或正文位置从指定 Word 修订返回原图；服务端未解析或描述图片。"}
        return payload, ("jpeg" if image_format == "jpg" else image_format), context

    def search(self, query: str, product: str = "", domain: list[str] | None = None,
               category: str = "", limit: int = 5) -> list[dict]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query 不能为空，limit 必须为 1-20")
        category_filter = validate_product_material_category(category) if category else None
        product_filter = product.strip() or None
        domain_filter = set(domain or [])
        figure_query = _figure_query_id(query)
        categories = [category_filter] if category_filter else list(PRODUCT_MATERIAL_CATEGORIES)
        results = []
        local_active = {}
        if self._has_schema():
            for row in self.store.conn.execute(
                "SELECT m.material_id,m.category,v.version_id,v.revision,v.sha256"
                " FROM product_materials m JOIN product_material_versions v"
                " ON v.version_id=m.current_version_id AND v.material_id=m.material_id"
                " WHERE m.active=1 AND v.state='ready'"
            ).fetchall():
                local_active[row["material_id"]] = dict(row)
        heads = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads")
        for cat in categories:
            collection = self._collection(cat)
            active_heads = list(heads.find({"active": True, "collection": collection}))
            records = self.rag.mongo.get_collection(collection)
            allowed = {}
            for head in active_heads:
                local_version = local_active.get(head.get("_id"))
                if (not local_version or local_version["category"] != cat
                        or local_version["revision"] != head.get("revision")):
                    continue
                record = records.find_one({"_id": head["record_id"], "state": "ready"})
                if (record and record.get("material_id") == local_version["material_id"]
                        and record.get("version_id") == local_version["version_id"]
                        and record.get("source_sha256") == local_version["sha256"]
                        and (not product_filter or record.get("products", record.get("product")) == product_filter)
                        and (not domain_filter or domain_filter.intersection(record.get("domain", [])))):
                    allowed.update({cid: record for cid in record.get("chunk_ids", [])})
            if not allowed:
                continue
            all_ids = list(allowed)
            if figure_query:
                matching_sections = {}
                for record in {r["_id"]: r for r in allowed.values()}.values():
                    query_key = _figure_key_from_id(figure_query)
                    section_ids = {section["section_id"] for section in record.get("sections", [])
                                   if any(figure_query == figure_id
                                          or (query_key and query_key == _figure_key_from_id(figure_id))
                                          for figure_id in section.get("figure_ids", []))}
                    if section_ids:
                        matching_sections[record["_id"]] = (record, section_ids)
                if matching_sections:
                    rows = self.rag._db(collection).query_by_ids(all_ids)
                    for row in rows:
                        record = allowed.get(row["id"])
                        match = matching_sections.get(record.get("_id")) if record else None
                        if not match:
                            continue
                        metadata = row.get("metadata") or {}
                        if isinstance(metadata, str):
                            metadata = json.loads(metadata)
                        if metadata.get("section_id") not in match[1]:
                            continue
                        results.append({"id": row["id"], "text": row["text"],
                                        "metadata": {**metadata, "stage": PRODUCT_MATERIAL_STAGE,
                                                     "figure_ids": [figure_query]},
                                        "score": 1.0, "category": cat,
                                        "knowledge_class": "product_material",
                                        "stage": PRODUCT_MATERIAL_STAGE,
                                        "material_id": record["material_id"],
                                        "revision": record["revision"],
                                        "source_sha256": record["source_sha256"],
                                        "products": record.get("products", record.get("product")),
                                        "domain": record["domain"], "title": record["title"],
                                        "sandbox": True, "exact_match": "figure_id"})
                continue
            for offset in range(0, len(all_ids), 200):
                hits = self.rag._db(collection).search(query, k=limit,
                    filter_expr="id in " + json.dumps(all_ids[offset:offset + 200]))
                for hit in hits:
                    record = allowed.get(hit["id"])
                    if not record:
                        continue
                    current = heads.find_one({"_id": record["material_id"], "record_id": record["_id"],
                                              "active": True, "collection": collection})
                    if not current:
                        continue
                    hit_metadata = hit.get("metadata") or {}
                    hit_metadata["stage"] = PRODUCT_MATERIAL_STAGE
                    if "products" not in hit_metadata and hit_metadata.get("product"):
                        hit_metadata["products"] = hit_metadata["product"]
                    results.append({**hit, "metadata": hit_metadata,
                                    "category": cat, "knowledge_class": "product_material",
                                    "stage": PRODUCT_MATERIAL_STAGE,
                                    "material_id": record["material_id"], "revision": record["revision"],
                                    "source_sha256": record["source_sha256"],
                                    "products": record.get("products", record.get("product")),
                                    "domain": record["domain"], "title": record["title"],
                                    "sandbox": True, "embedding_mode": self.rag.mode})
        return sorted(results, key=lambda hit: hit["score"], reverse=True)[:limit]

    def export_original(self, material_id: str, revision: int, source_sha256: str,
                        out_path: str | Path) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        row = db.rowdict(self.store.conn.execute(
            "SELECT * FROM product_material_versions WHERE material_id=? AND revision=? AND state='ready'",
            (material_id, revision)).fetchone())
        if not row or row["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        blob = db.rowdict(self.store.conn.execute("SELECT * FROM blobs WHERE sha256=?", (source_sha256,)).fetchone())
        if not blob:
            raise ValueError("产品资料原件存储引用缺失")
        source = self.data_dir / blob["rel_path"]
        if not source.is_file() or hash_file(source) != source_sha256:
            raise ValueError("产品资料原件不存在或 SHA-256 校验失败")
        target = Path(out_path).expanduser().resolve()
        if target == self.data_dir or target.is_relative_to(self.data_dir):
            raise ValueError("out_path 不能位于受管理数据目录内")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return {"material_id": material_id, "revision": revision, "source_sha256": source_sha256,
                "filename": row["filename"], "size": row["size"], "verified": hash_file(target) == source_sha256,
                "path": str(target)}

    def withdraw(self, material_id: str, revision: int, source_sha256: str) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        item = db.rowdict(self.store.conn.execute(
            "SELECT m.category,v.* FROM product_materials m JOIN product_material_versions v"
            " ON v.material_id=m.material_id WHERE m.material_id=? AND v.revision=? AND v.state='ready'",
            (material_id, revision)).fetchone())
        if not item or item["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        collection = self._collection(item["category"])
        result = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads").update_one(
            {"_id": material_id, "record_id": f"{material_id}@{revision}",
             "collection": collection, "active": True}, {"$set": {"active": False}})
        if result.matched_count:
            self.store.conn.execute("UPDATE product_materials SET active=0,updated_at=? WHERE material_id=?",
                                    (_now(), material_id))
            self.store.conn.commit()
        return {"withdrawn": bool(result.matched_count), "material_id": material_id,
                "revision": revision, "sandbox": True}
