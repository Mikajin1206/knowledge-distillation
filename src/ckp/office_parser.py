"""On-demand, read-only Office structure inspection for attached distillation material.

The parser never runs during upload.  It reads a hash-verified task copy and writes
only a task-scoped JSON manifest plus extracted media beneath the task tmp folder.
OOXML parsing uses the standard library; legacy .doc conversion requires LibreOffice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

PARSER_VERSION = "ckp-office-inspector-v0.3"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
_R_ID = "{" + NS["r"] + "}id"
_R_EMBED = "{" + NS["r"] + "}embed"
_R_LINK = "{" + NS["r"] + "}link"


def inspect_office(source: Path, out_dir: Path, *, file_id: str,
                   version_id: str, expected_sha256: str) -> dict:
    source, out_dir = Path(source), Path(out_dir)
    actual_hash = _sha256(source)
    if actual_hash != expected_sha256:
        raise ValueError("Office 解析前材料副本哈希不匹配")
    suffix = source.suffix.lower()
    if suffix not in {".docx", ".xlsx", ".pptx", ".doc"}:
        raise ValueError(f"暂不支持 Office 结构解析: {suffix or '(无扩展名)'}")
    out_dir.mkdir(parents=True, exist_ok=True)
    work_source = source
    conversion = None
    if suffix == ".doc":
        work_source, conversion = _convert_doc(source, out_dir)
        suffix = ".docx"
    if not zipfile.is_zipfile(work_source):
        raise ValueError(f"{source.suffix} 解析输入不是有效 OOXML 包")
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(work_source) as zf:
        if suffix == ".docx":
            manifest = _inspect_docx(zf, file_id, assets_dir)
        elif suffix == ".xlsx":
            manifest = _inspect_xlsx(zf, file_id, assets_dir)
        else:
            manifest = _inspect_pptx(zf, file_id, assets_dir)
    manifest.update({
        "schema": "ckp-office-manifest/v1",
        "parser": PARSER_VERSION,
        "source": {"file_id": file_id, "version_id": version_id,
                   "sha256": actual_hash, "filename": source.name,
                   "format": source.suffix.lower()},
        "conversion": conversion,
    })
    manifest["summary"]["asset_placements"] = len(manifest["assets"])
    if "unreferenced_media" in manifest:
        manifest["summary"]["unreferenced_media_parts"] = len(manifest["unreferenced_media"])
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    manifest["assets_dir"] = str(assets_dir)
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _convert_doc(source: Path, out_dir: Path) -> tuple[Path, dict]:
    converter = _find_soffice()
    if not converter:
        raise ValueError("旧版 .doc 需要 LibreOffice soffice 转换；未找到可执行文件，已阻止按纯文本通过解析")
    converted_dir = out_dir / "converted"
    profile = (out_dir / "soffice-profile").resolve()
    converted_dir.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    command = [converter, "--headless", f"-env:UserInstallation={profile.as_uri()}",
               "--convert-to", "docx", "--outdir", str(converted_dir), str(source)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120,
                            env=_soffice_environment(converter))
    target = converted_dir / f"{source.stem}.docx"
    if result.returncode != 0 or not target.is_file():
        detail = (result.stderr or result.stdout or "转换器未生成 docx")[-1200:]
        raise ValueError(f"旧版 .doc 转换失败，原件未改动: {detail}")
    return target, {"tool": Path(converter).name, "returncode": result.returncode,
                    "output": target.name, "stdout": result.stdout[-1000:],
                    "stderr": result.stderr[-1000:]}


def _find_soffice() -> str | None:
    configured = os.environ.get("CKP_SOFFICE")
    if configured:
        return configured if Path(configured).is_file() and os.access(configured, os.X_OK) else None
    found = shutil.which("soffice")
    if found:
        return found
    candidates = [
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path("/Applications/LibreOfficeDev.app/Contents/MacOS/soffice"),
        Path("/opt/homebrew/bin/soffice"),
        Path("/usr/local/bin/soffice"),
    ]
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    if runtime_root.is_dir():
        candidates.extend(sorted(runtime_root.glob("*/dependencies/bin/override/soffice")))
    return next((str(path) for path in candidates
                 if path.is_file() and os.access(path, os.X_OK)), None)


def _soffice_environment(converter: str) -> dict[str, str]:
    """Expose the bundled LibreOffice fontconfig config, including macOS host fonts."""
    environment = os.environ.copy()
    if environment.get("FONTCONFIG_FILE") and environment.get("FONTCONFIG_PATH"):
        return environment

    executable = Path(converter).resolve()
    candidates: list[Path] = []
    for parent in (executable, *executable.parents):
        if parent.name == "MacOS":
            candidates.append(parent.parent / "Resources" / "fontconfig" / "fonts.conf")
        if parent.name == "dependencies":
            candidates.extend(parent.glob(
                "native/libreoffice-headless/libreoffice/*.app/Contents/Resources/fontconfig/fonts.conf"
            ))

    fontconfig_file = next((candidate for candidate in candidates if candidate.is_file()), None)
    if fontconfig_file:
        # The bundled macOS runtime otherwise omits host fonts from Fontconfig;
        # LibreOffice silently renders CJK glyphs as blanks or replacement boxes.
        environment.setdefault("FONTCONFIG_FILE", str(fontconfig_file))
        environment.setdefault("FONTCONFIG_PATH", str(fontconfig_file.parent))
    return environment


MCP_NATIVE_IMAGE_FORMATS = ("png", "jpg", "jpeg", "gif", "webp")
MCP_CONVERT_IMAGE_FORMATS = ("svg", "emf", "wmf", "bmp", "tif", "tiff")


def render_image_for_mcp(source: Path, output_dir: Path) -> Path:
    """Convert Word image formats that MCP clients may not render into task-local PNG."""
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    image_format = source.suffix.lower().lstrip(".")
    if image_format not in MCP_CONVERT_IMAGE_FORMATS:
        raise ValueError(f"不支持转换为 MCP 图片的格式: {source.suffix or '(无扩展名)'}")
    converter = _find_soffice()
    if not converter:
        raise ValueError(f"图片 {source.name} 是 {source.suffix.lower()} 格式；当前环境未安装 LibreOffice，无法转换为 MCP 可读 PNG")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{source.stem}.png"
    if target.is_file() and target.stat().st_size:
        return target
    with tempfile.TemporaryDirectory(prefix="vector-render-", dir=output_dir) as temp_name:
        temp_dir = Path(temp_name)
        profile = (temp_dir / "profile").resolve()
        profile.mkdir()
        result = subprocess.run(
            [converter, "--headless", f"-env:UserInstallation={profile.as_uri()}",
             "--convert-to", "png", "--outdir", str(temp_dir), str(source)],
            capture_output=True, text=True, timeout=60,
            env=_soffice_environment(converter))
        rendered = temp_dir / f"{source.stem}.png"
        if result.returncode != 0 or not rendered.is_file() or not rendered.stat().st_size:
            detail = (result.stderr or result.stdout or "未生成 PNG")[-800:]
            raise ValueError(f"图片 {source.name} 转换为 PNG 失败: {detail}")
        os.replace(rendered, target)
    return target


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    pieces = []
    for child in node.iter():
        if child.tag.endswith("}t"):
            pieces.append(child.text or "")
        elif child.tag.endswith("}tab"):
            pieces.append("\t")
        elif child.tag.endswith("}br") or child.tag.endswith("}cr"):
            pieces.append("\n")
    return "".join(pieces)


def _rels_name(part: str) -> str:
    path = PurePosixPath(part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _resolve_target(part: str, target: str) -> str:
    if target.startswith("/"):
        candidate = PurePosixPath(target.lstrip("/"))
    else:
        candidate = PurePosixPath(part).parent / target
    stack: list[str] = []
    for piece in candidate.parts:
        if piece in {"", "."}:
            continue
        if piece == "..":
            if not stack:
                raise ValueError(f"Office 包关系路径越界: {part} -> {target}")
            stack.pop()
        else:
            stack.append(piece)
    return "/".join(stack)


def _relationships(zf: zipfile.ZipFile, part: str) -> dict[str, dict]:
    rels = _rels_name(part)
    if rels not in zf.namelist():
        return {}
    root = _xml(zf, rels)
    return {el.get("Id", ""): {
        "target": _resolve_target(part, el.get("Target", "")),
        "type": el.get("Type", "").rsplit("/", 1)[-1],
        "external": el.get("TargetMode") == "External",
    } for el in root}


def _media_ext(part: str) -> str:
    suffix = Path(part).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin"


def _media_extensions(zf: zipfile.ZipFile) -> dict[str, str]:
    """Resolve extensionless media through OOXML's package content-type table."""
    name = "[Content_Types].xml"
    if name not in zf.namelist():
        return {}
    root = _xml(zf, name)
    mime_suffixes = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/bmp": ".bmp", "image/x-wmf": ".wmf",
        "image/x-emf": ".emf", "image/emf": ".emf", "image/wmf": ".wmf",
        "image/tiff": ".tiff", "image/svg+xml": ".svg",
    }
    defaults = {el.get("Extension", "").lower(): mime_suffixes.get(
                    el.get("ContentType", "").lower(), "." + el.get("Extension", "").lower())
                for el in root if el.tag.endswith("}Default") and el.get("Extension")}
    overrides = {el.get("PartName", "").lstrip("/"): mime_suffixes[el.get("ContentType", "").lower()]
                 for el in root if el.tag.endswith("}Override")
                 and el.get("ContentType", "").lower() in mime_suffixes}
    return {name: overrides.get(name, defaults.get(Path(name).suffix.lower().lstrip("."), ".bin"))
            for name in zf.namelist() if "/media/" in name}


def _copy_asset(zf: zipfile.ZipFile, target: str, assets_dir: Path,
                file_id: str, index: int, media_extensions: dict[str, str] | None = None) -> str | None:
    if target not in zf.namelist():
        return None
    extension = _media_ext(target)
    if extension == ".bin" and media_extensions:
        extension = media_extensions.get(target, extension)
    out = assets_dir / f"{file_id}_{index:04d}{extension}"
    out.write_bytes(zf.read(target))
    return str(out)


def _unreferenced_media(zf: zipfile.ZipFile, used: set[str]) -> list[str]:
    media = [name for name in zf.namelist()
             if re.search(r"(?:^|/)media/[^/]+$", name)]
    return sorted(set(media) - used)


def _inspect_docx(zf: zipfile.ZipFile, file_id: str, assets_dir: Path) -> dict:
    main_part = "word/document.xml"
    if main_part not in zf.namelist():
        raise ValueError("DOCX 缺少 word/document.xml")
    root = _xml(zf, main_part)
    body = root.find("w:body", NS)
    paragraphs, tables = [], []
    if body is not None:
        for child_index, child in enumerate(list(body), 1):
            if child.tag == f"{{{NS['w']}}}p":
                text = _text(child)
                if text.strip():
                    ppr = child.find("w:pPr", NS)
                    pstyle = ppr.find("w:pStyle", NS) if ppr is not None else None
                    outline = ppr.find("w:outlineLvl", NS) if ppr is not None else None
                    paragraphs.append({"index": len(paragraphs) + 1, "body_order": child_index,
                                       "text": text, "style": pstyle.get(f"{{{NS['w']}}}val") if pstyle is not None else None,
                                       "outline_level": outline.get(f"{{{NS['w']}}}val") if outline is not None else None})
            elif child.tag == f"{{{NS['w']}}}tbl":
                table = _docx_table(child, len(tables) + 1)
                table["body_order"] = child_index
                tables.append(table)
    assets, idx = [], 0
    media_extensions = _media_extensions(zf)
    # Only inspect document content and substantive notes. Header/footer media
    # (logos and page decoration) are outside the knowledge scope.
    parts = [n for n in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")
             if n in zf.namelist()]
    for part in parts:
        rels = _relationships(zf, part)
        try:
            xml = _xml(zf, part)
        except ET.ParseError:
            continue
        part_body = xml.find("w:body", NS) if part == main_part else None
        image_refs = []
        parents = {child: parent for parent in xml.iter() for child in parent}
        svg_refs = {node for node in xml.iter() if node.tag == f"{{{NS['asvg']}}}svgBlip"}
        for node in xml.iter():
            if node.tag == f"{{{NS['a']}}}blip":
                svg = next((child for child in node.iter() if child in svg_refs), None)
                image_refs.append((svg if svg is not None else node,
                                   "svg" if svg is not None else "drawingml"))
            elif node.tag == f"{{{NS['v']}}}imagedata":
                image_refs.append((node, "vml"))
        for place, (image_ref, reference_type) in enumerate(image_refs, 1):
            # Word may store inline images as DrawingML (a:blip) or legacy VML
            # (v:imagedata). Both point to the same relationship table; omitting
            # VML made valid images look like unreferenced package leftovers.
            rid = (image_ref.get(_R_EMBED) or image_ref.get(_R_LINK)
                   or image_ref.get(_R_ID))
            rel = rels.get(rid or "", {})
            target = rel.get("target", "")
            if not target:
                continue
            idx += 1
            copied = _copy_asset(zf, target, assets_dir, file_id, idx, media_extensions)
            # ElementTree has no parent pointers; use the nearest drawing container.
            docpr = _docpr_for_image_ref(xml, image_ref)
            block_ref, block_context = _asset_block_context(xml, part_body,
                                                           image_ref, parents, part)
            assets.append({"asset_id": f"{file_id}:{part}:{rid}:{place}", "kind": "image",
                           "part": target, "relationship_id": rid,
                           "location": f"{part} drawing {place}",
                           "block_ref": block_ref, "block_context": block_context,
                           "name": docpr.get("name") if docpr is not None else None,
                           "description": docpr.get("descr") if docpr is not None else None,
                           "reference_type": reference_type,
                           "local_path": copied})
    object_parts = [n for n in zf.namelist() if n.startswith("word/embeddings/")]
    return {"format": "docx", "paragraphs": paragraphs, "tables": tables,
            "assets": assets, "objects": {"embedded_objects": object_parts},
            "summary": {"paragraphs": len(paragraphs), "tables": len(tables),
                        "image_references": len(assets), "embedded_objects": len(object_parts)}}


def _docx_table(table: ET.Element, number: int) -> dict:
    rows = []
    for row_index, row in enumerate(table.findall("w:tr", NS), 1):
        cells = []
        for col_index, cell in enumerate(row.findall("w:tc", NS), 1):
            tcpr = cell.find("w:tcPr", NS)
            gridspan = tcpr.find("w:gridSpan", NS) if tcpr is not None else None
            vmerge = tcpr.find("w:vMerge", NS) if tcpr is not None else None
            cells.append({"column": col_index, "text": "\n".join(_text(p) for p in cell.findall("w:p", NS)),
                          "grid_span": gridspan.get(f"{{{NS['w']}}}val") if gridspan is not None else "1",
                          "vertical_merge": vmerge.get(f"{{{NS['w']}}}val", "continue") if vmerge is not None else None})
        rows.append({"row": row_index, "cells": cells})
    return {"table": number, "rows": rows, "row_count": len(rows),
            "column_counts": [len(row["cells"]) for row in rows]}


def _docpr_for_image_ref(root: ET.Element, target: ET.Element) -> ET.Element | None:
    parents = {child: parent for parent in root.iter() for child in parent}
    current = target
    while current in parents:
        current = parents[current]
        docpr = current.find(".//wp:docPr", NS)
        if docpr is not None:
            return docpr
    return None


def _asset_block_context(root: ET.Element, body: ET.Element | None,
                         image_ref: ET.Element, parents: dict[ET.Element, ET.Element],
                         part: str) -> tuple[str, str]:
    """Tie a picture to its paragraph/table so scope filtering can precede vision calls."""
    current = image_ref
    paragraph = table = None
    while current in parents:
        current = parents[current]
        if paragraph is None and current.tag == f"{{{NS['w']}}}p":
            paragraph = current
        if current.tag == f"{{{NS['w']}}}tbl":
            table = current
            break
    if table is not None:
        table_nodes = list(root.iter(f"{{{NS['w']}}}tbl"))
        table_number = table_nodes.index(table) + 1
        context = _text(table)[:160]
        if body is not None:
            direct = next((node for node in body if table is node or table in node.iter()), None)
            if direct is not None:
                context = _nearby_body_context(body, list(body).index(direct), context)
        return f"{part}:table:{table_number}", context
    if paragraph is not None:
        if body is not None:
            direct = next((node for node in body if paragraph is node or paragraph in node.iter()), None)
            if direct is not None:
                body_order = list(body).index(direct) + 1
                paragraph_number = sum(1 for node in list(body)[:body_order]
                                      if node.tag == f"{{{NS['w']}}}p")
                context = _nearby_body_context(body, body_order - 1, _text(paragraph)[:160])
                return f"{part}:paragraph:{paragraph_number}:body_order:{body_order}", context
        paragraphs = list(root.iter(f"{{{NS['w']}}}p"))
        paragraph_number = paragraphs.index(paragraph) + 1
        return f"{part}:paragraph:{paragraph_number}", _text(paragraph)[:240]
    return f"{part}:unplaced", ""


def _nearby_body_context(body: ET.Element, index: int, current: str) -> str:
    pieces = []
    for position in range(max(0, index - 1), min(len(body), index + 2)):
        text = _text(body[position]).strip()
        if position == index and current:
            text = current
        if text:
            pieces.append(text[:160])
    return " | ".join(pieces)[:480]


def _inspect_xlsx(zf: zipfile.ZipFile, file_id: str, assets_dir: Path) -> dict:
    book_part = "xl/workbook.xml"
    if book_part not in zf.namelist():
        raise ValueError("XLSX 缺少 xl/workbook.xml")
    book = _xml(zf, book_part)
    workbook_rels = _relationships(zf, book_part)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        sr = _xml(zf, "xl/sharedStrings.xml")
        shared = [_text(si) for si in sr.findall("m:si", NS)]
    sheets = []
    for sheet_index, sheet_el in enumerate(book.findall("m:sheets/m:sheet", NS), 1):
        name = sheet_el.get("name", f"Sheet{sheet_index}")
        rid = sheet_el.get(_R_ID, "")
        rel = workbook_rels.get(rid, {})
        part = rel.get("target", "")
        if part not in zf.namelist():
            sheets.append({"name": name, "state": sheet_el.get("state", "visible"), "error": "worksheet part missing"})
            continue
        root = _xml(zf, part)
        cells, row_records = {}, []
        for row_el in root.findall(".//m:sheetData/m:row", NS):
            row_num = int(row_el.get("r", "0"))
            row_cells = {}
            for cell in row_el.findall("m:c", NS):
                address = cell.get("r", "")
                val_el, formula_el, inline_el = cell.find("m:v", NS), cell.find("m:f", NS), cell.find("m:is", NS)
                value: Any = _text(inline_el) if inline_el is not None else (val_el.text if val_el is not None else "")
                if cell.get("t") == "s" and val_el is not None:
                    try:
                        value = shared[int(val_el.text or "0")]
                    except (IndexError, ValueError):
                        value = {"shared_string_index": val_el.text, "error": "invalid index"}
                record = {"address": address, "value": value,
                          "type": cell.get("t", "n"), "style_id": cell.get("s"),
                          "formula": formula_el.text if formula_el is not None else None}
                if value != "" or formula_el is not None:
                    row_cells[address] = record
                    cells[address] = record
            if row_cells:
                row_records.append({"row": row_num, "hidden": row_el.get("hidden") == "1",
                                    "cells": row_cells})
        merges = [m.get("ref") for m in root.findall("m:mergeCells/m:mergeCell", NS)]
        filters = [e.get("ref") for e in root.findall("m:autoFilter", NS) if e.get("ref")]
        cols = [{"min": c.get("min"), "max": c.get("max"), "hidden": c.get("hidden") == "1",
                 "width": c.get("width")} for c in root.findall("m:cols/m:col", NS)]
        sheets.append({"name": name, "part": part, "state": sheet_el.get("state", "visible"),
                       "dimension": (root.find("m:dimension", NS).get("ref")
                                     if root.find("m:dimension", NS) is not None else None),
                       "cells": cells, "rows": row_records, "merged_ranges": merges,
                       "filters": filters, "column_properties": cols,
                       "nonempty_row_numbers": [r["row"] for r in row_records]})
    assets, used, idx = [], set(), 0
    object_refs = []
    for sheet in sheets:
        part = sheet.get("part")
        if not part:
            continue
        rels = _relationships(zf, part)
        xml = _xml(zf, part)
        drawing_rel = next((r for r in rels.values() if r["type"] == "drawing"), None)
        if not drawing_rel:
            continue
        drawing_part = drawing_rel["target"]
        drawing_rels = _relationships(zf, drawing_part)
        drawing = _xml(zf, drawing_part)
        anchors = list(drawing)
        for anchor_index, anchor in enumerate(anchors, 1):
            def marker(tag: str) -> dict | None:
                node = anchor.find(f"xdr:{tag}", NS)
                if node is None:
                    return None
                def n(name):
                    child = node.find(f"xdr:{name}", NS)
                    return int(child.text) + 1 if child is not None and child.text else None
                return {"row": n("row"), "column": n("col")}
            start, end = marker("from"), marker("to")
            location = f"{sheet['name']}!{_cell_ref(start)}"
            blip = anchor.find(".//a:blip", NS)
            if blip is not None:
                rid = blip.get(_R_EMBED) or blip.get(_R_LINK)
                target = drawing_rels.get(rid or "", {}).get("target", "")
                if target:
                    idx += 1
                    copied = _copy_asset(zf, target, assets_dir, file_id, idx)
                    used.add(target)
                    assets.append({"asset_id": f"{file_id}:{sheet['name']}:{drawing_part}:{anchor_index}",
                                   "kind": "image", "part": target, "relationship_id": rid,
                                   "location": location, "from": start, "to": end, "local_path": copied})
            chart = anchor.find(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}chart", NS)
            if chart is not None:
                rid = chart.get(_R_ID)
                target = drawing_rels.get(rid or "", {}).get("target", "")
                object_refs.append({"kind": "chart", "part": target, "location": location})
    media = [n for n in zf.namelist() if n.startswith("xl/media/") or n.startswith("xl/drawings/media/")]
    return {"format": "xlsx", "sheets": sheets, "assets": assets,
            "unreferenced_media": _unreferenced_media(zf, used),
            "objects": {"media_parts": len(media), "charts": object_refs,
                        "pivot_parts": [n for n in zf.namelist() if n.startswith("xl/pivotTables/")],
                        "embedded_objects": [n for n in zf.namelist() if n.startswith("xl/embeddings/")]},
            "summary": {"sheets": len(sheets),
                        "nonempty_rows": {s["name"]: len(s.get("rows", [])) for s in sheets},
                        "formulas": sum(1 for s in sheets for c in s.get("cells", {}).values() if c.get("formula") is not None),
                        "image_references": len(assets), "charts": len(object_refs)}}


def _cell_ref(marker: dict | None) -> str:
    if not marker or marker.get("row") is None or marker.get("column") is None:
        return "unknown"
    col, n = marker["column"], ""
    while col:
        col, rem = divmod(col - 1, 26)
        n = chr(65 + rem) + n
    return f"{n}{marker['row']}"


def _inspect_pptx(zf: zipfile.ZipFile, file_id: str, assets_dir: Path) -> dict:
    presentation = "ppt/presentation.xml"
    if presentation not in zf.namelist():
        raise ValueError("PPTX 缺少 ppt/presentation.xml")
    pres = _xml(zf, presentation)
    rels = _relationships(zf, presentation)
    slide_parts = []
    for sld in pres.findall("p:sldIdLst/p:sldId", NS):
        rel = rels.get(sld.get(_R_ID, ""), {})
        if rel.get("target"):
            slide_parts.append(rel["target"])
    slides, assets, used, charts, diagrams = [], [], set(), [], []
    idx = 0
    for slide_number, part in enumerate(slide_parts, 1):
        root = _xml(zf, part)
        slide_rels = _relationships(zf, part)
        shapes, slide_tables = [], []
        for shape_index, shape in enumerate(root.findall(".//p:sp", NS), 1):
            tx = shape.find("p:txBody", NS)
            txt = "\n".join(_text(p) for p in tx.findall("a:p", NS)) if tx is not None else ""
            nv = shape.find("p:nvSpPr/p:cNvPr", NS)
            if txt.strip() or nv is not None:
                shapes.append({"index": shape_index, "name": nv.get("name") if nv is not None else None,
                               "text": txt, "shape_id": nv.get("id") if nv is not None else None})
        connectors = []
        for connector_index, connector in enumerate(root.findall(".//p:cxnSp", NS), 1):
            nv = connector.find("p:nvCxnSpPr/p:cNvCxnSpPr", NS)
            c_nv = connector.find("p:nvCxnSpPr/p:cNvPr", NS)
            props = connector.find("p:spPr", NS)
            line = props.find("a:ln", NS) if props is not None else None
            end_arrow = line.find("a:tailEnd", NS) if line is not None else None
            head_arrow = line.find("a:headEnd", NS) if line is not None else None
            txt = _text(connector.find("p:txBody", NS))
            connectors.append({
                "index": connector_index,
                "name": c_nv.get("name") if c_nv is not None else None,
                "shape_id": c_nv.get("id") if c_nv is not None else None,
                "text": txt,
                "start_connection": (nv.find("a:stCxn", NS).attrib
                                     if nv is not None and nv.find("a:stCxn", NS) is not None else None),
                "end_connection": (nv.find("a:endCxn", NS).attrib
                                   if nv is not None and nv.find("a:endCxn", NS) is not None else None),
                "tail_arrow": end_arrow.get("type") if end_arrow is not None else None,
                "head_arrow": head_arrow.get("type") if head_arrow is not None else None,
            })
        for frame_index, frame in enumerate(root.findall(".//p:graphicFrame", NS), 1):
            tbl = frame.find(".//a:tbl", NS)
            if tbl is not None:
                rows = [[_text(tc) for tc in tr.findall("a:tc", NS)] for tr in tbl.findall("a:tr", NS)]
                slide_tables.append({"table": frame_index, "rows": rows,
                                     "row_count": len(rows), "column_counts": [len(r) for r in rows]})
            chart = frame.find(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}chart", NS)
            if chart is not None:
                rid = chart.get(_R_ID)
                target = slide_rels.get(rid or "", {}).get("target", "")
                charts.append({"slide": slide_number, "part": target, "relationship_id": rid})
            diagram = frame.find(".//{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds", NS)
            if diagram is not None:
                diagrams.append({"slide": slide_number, "relationship_ids": dict(diagram.attrib)})
        for pic_index, pic in enumerate(root.findall(".//p:pic", NS), 1):
            blip = pic.find(".//a:blip", NS)
            rid = blip.get(_R_EMBED) if blip is not None else None
            target = slide_rels.get(rid or "", {}).get("target", "")
            if target:
                idx += 1
                copied = _copy_asset(zf, target, assets_dir, file_id, idx)
                used.add(target)
                c = pic.find("p:nvPicPr/p:cNvPr", NS)
                assets.append({"asset_id": f"{file_id}:{part}:{rid}:{pic_index}", "kind": "image",
                               "part": target, "relationship_id": rid,
                               "location": f"slide {slide_number}",
                               "name": c.get("name") if c is not None else None,
                               "description": c.get("descr") if c is not None else None,
                               "local_path": copied})
        notes_rel = next((r for r in slide_rels.values() if r["type"] == "notesSlide"), None)
        note_text = ""
        notes_part = notes_rel["target"] if notes_rel else None
        if notes_part and notes_part in zf.namelist():
            note_root = _xml(zf, notes_part)
            note_text = "\n".join(_text(p) for p in note_root.findall(".//a:p", NS))
        slides.append({"slide": slide_number, "part": part, "shapes": shapes,
                       "connectors": connectors,
                       "tables": slide_tables, "notes_part": notes_part, "notes": note_text})
    media = [n for n in zf.namelist() if n.startswith("ppt/media/")]
    return {"format": "pptx", "slides": slides, "assets": assets,
            "unreferenced_media": _unreferenced_media(zf, used),
            "objects": {"media_parts": len(media), "charts": charts,
                        "smartart": diagrams,
                        "embedded_objects": [n for n in zf.namelist() if n.startswith("ppt/embeddings/")]},
            "summary": {"slides": len(slides), "notes": sum(1 for s in slides if s["notes_part"]),
                        "text_shapes": sum(len(s["shapes"]) for s in slides),
                        "connectors": sum(len(s["connectors"]) for s in slides),
                        "tables": sum(len(s["tables"]) for s in slides),
                        "image_references": len(assets), "charts": len(charts),
                        "smartart": len(diagrams)}}
