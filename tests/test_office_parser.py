"""OOXML Office parser smoke tests using small, deterministic packages."""
from __future__ import annotations

import tempfile
import sqlite3
import struct
import unittest
import zipfile
import zlib
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from tests._path import *  # noqa: F401,F403
from ckp.distill import validate_quality_report
from ckp.office_parser import (PARSER_VERSION, _find_soffice, _png_is_blank, _render_emfplus_bitmap, _trim_png_canvas,
                               _sha256, inspect_office, render_image_for_mcp)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def _rgb_png(width: int, height: int, pixels: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = b"".join(b"\0" + pixels[row * width * 3:(row + 1) * width * 3]
                         for row in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(scanlines)) + _png_chunk(b"IEND", b""))


def _emfplus_bitmap_fixture(*, width=2, height=1, stride=8,
                            pixel_format=0x000E200B,
                            pixels=bytes((0, 0, 128, 128, 255, 0, 0, 255)),
                            object_flags=0x0501) -> bytes:
    bitmap = struct.pack("<IIiiiII", 0xDBC01002, 1, width, height, stride,
                         pixel_format, 0) + pixels
    plus_header = struct.pack("<HHII", 0x4001, 0, 28, 16) + bytes(16)
    plus_object = struct.pack("<HHII", 0x4008, object_flags, 12 + len(bitmap), len(bitmap)) + bitmap
    plus_eof = struct.pack("<HHII", 0x4002, 0, 12, 0)
    comment_data = b"EMF+" + plus_header + plus_object + plus_eof
    comment = struct.pack("<III", 70, 12 + len(comment_data), len(comment_data)) + comment_data
    header = struct.pack("<II", 1, 88) + bytes(80)
    eof = struct.pack("<II", 14, 8)
    return header + comment + eof


def _png_rgb_scanlines(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    width, height, depth, color_type, *_ = struct.unpack_from(">IIBBBBB", raw, 16)
    compressed = bytearray()
    offset = 8
    while offset + 12 <= len(raw):
        size = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4:offset + 8]
        if kind == b"IDAT":
            compressed.extend(raw[offset + 8:offset + 8 + size])
        if kind == b"IEND":
            break
        offset += size + 12
    if depth != 8 or color_type != 2:
        raise AssertionError("expected 8-bit RGB PNG")
    return width, height, zlib.decompress(compressed)


class OfficeParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-office-parser-"))

    def _package(self, name: str, files: dict[str, str]) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, contents in files.items():
                archive.writestr(member, contents)
        return path

    def _inspect(self, path: Path) -> dict:
        return inspect_office(path, self.tmp / (path.stem + "-out"), file_id="f-test",
                              version_id="v1", expected_sha256=_sha256(path))

    def test_png_blank_guard_distinguishes_blank_from_diagram_pixels(self):
        blank = self.tmp / "blank.png"
        blank.write_bytes(_rgb_png(2, 2, b"\xff\xff\xff" * 4))
        diagram = self.tmp / "diagram.png"
        diagram.write_bytes(_rgb_png(2, 1, b"\xff\xff\xff\0\0\0"))
        self.assertTrue(_png_is_blank(blank))
        self.assertFalse(_png_is_blank(diagram))

    def test_png_blank_guard_detects_empty_rectangle_preview(self):
        width, height = 100, 80
        pixels = bytearray(b"\xff\xff\xff" * (width * height))
        for x in range(10, 90):
            for y in (10, 69):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = b"\x30\x30\x30"
        for y in range(10, 70):
            for x in (10, 89):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = b"\x30\x30\x30"
        frame = self.tmp / "empty-frame.png"
        frame.write_bytes(_rgb_png(width, height, bytes(pixels)))
        self.assertTrue(_png_is_blank(frame))

    def test_png_blank_guard_keeps_rectangle_with_interior_diagram_content(self):
        width, height = 100, 80
        pixels = bytearray(b"\xff\xff\xff" * (width * height))
        for x in range(10, 90):
            for y in (10, 69):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = b"\x30\x30\x30"
        for y in range(10, 70):
            for x in (10, 89):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = b"\x30\x30\x30"
        for x in range(40, 50):
            offset = (40 * width + x) * 3
            pixels[offset:offset + 3] = b"\x30\x30\x30"
        frame_with_content = self.tmp / "frame-with-content.png"
        frame_with_content.write_bytes(_rgb_png(width, height, bytes(pixels)))
        self.assertFalse(_png_is_blank(frame_with_content))

    def test_png_canvas_trim_crops_small_diagram_from_oversized_white_margin(self):
        pixels = bytearray(b"\xff\xff\xff" * (100 * 100))
        for y in range(40, 60):
            for x in range(40, 60):
                index = (y * 100 + x) * 3
                pixels[index:index + 3] = b"\0\0\0"
        image = self.tmp / "oversized.png"
        image.write_bytes(_rgb_png(100, 100, bytes(pixels)))
        self.assertTrue(_trim_png_canvas(image))
        cropped = image.read_bytes()
        width, height = struct.unpack_from(">II", cropped, 16)
        self.assertLess(width, 30)
        self.assertLess(height, 30)
        self.assertFalse(_png_is_blank(image))

    def test_emfplus_embedded_bitmap_renders_without_libreoffice(self):
        source = self.tmp / "diagram.emf"
        source.write_bytes(_emfplus_bitmap_fixture())
        with patch("ckp.office_parser._find_soffice", return_value=None):
            rendered = render_image_for_mcp(source, self.tmp / "rendered")
        self.assertFalse(_png_is_blank(rendered))
        width, height, pixels = _png_rgb_scanlines(rendered)
        self.assertEqual((2, 1), (width, height))
        self.assertEqual(b"\0\xff\x7f\x7f\x00\x00\xff", pixels)

    def test_emfplus_prefers_full_libreoffice_render_over_embedded_preview(self):
        source = self.tmp / "vector.emf"
        source.write_bytes(_emfplus_bitmap_fixture())
        full_diagram = _rgb_png(3, 1, b"\xff\xff\xff\0\0\0\xff\x00\x00")
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            output = Path(command[command.index("--outdir") + 1]) / "vector.png"
            output.write_bytes(full_diagram)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("ckp.office_parser._find_soffice", return_value="/fake/soffice"), \
             patch("ckp.office_parser.subprocess.run", side_effect=fake_run):
            rendered = render_image_for_mcp(source, self.tmp / "rendered-full")

        self.assertEqual(1, len(calls))
        self.assertEqual((3, 1), _png_rgb_scanlines(rendered)[:2])

    def test_emfplus_negative_stride_restores_top_down_row_order(self):
        # Negative stride stores bottom row first: opaque blue, then opaque red.
        source = self.tmp / "bottom-up.emf"
        source.write_bytes(_emfplus_bitmap_fixture(
            width=1, height=2, stride=-4, pixel_format=0x00022009,
            pixels=bytes((255, 0, 0, 0, 0, 0, 255, 0))))
        target = self.tmp / "bottom-up.png"
        self.assertTrue(_render_emfplus_bitmap(source, target))
        self.assertEqual((1, 2, b"\0\xff\0\0\0\0\0\xff"), _png_rgb_scanlines(target))

    def test_emfplus_argb_composites_transparency_on_white(self):
        source = self.tmp / "alpha.emf"
        # Transparent red and half-alpha red in straight BGRA storage.
        source.write_bytes(_emfplus_bitmap_fixture(
            pixels=bytes((0, 0, 255, 0, 0, 0, 255, 128)),
            pixel_format=0x0026200a))
        target = self.tmp / "alpha.png"
        self.assertTrue(_render_emfplus_bitmap(source, target))
        self.assertEqual((2, 1, b"\0\xff\xff\xff\xff\x7f\x7f"),
                         _png_rgb_scanlines(target))

    def test_emfplus_rejects_truncated_records_without_writing_png(self):
        source = self.tmp / "truncated.emf"
        malformed = bytearray(_emfplus_bitmap_fixture())
        struct.pack_into("<I", malformed, 0, 70)  # record extends past EOF
        struct.pack_into("<I", malformed, 4, len(malformed) + 1)
        source.write_bytes(malformed)
        target = self.tmp / "truncated.png"
        self.assertFalse(_render_emfplus_bitmap(source, target))
        self.assertFalse(target.exists())

    def test_docx_preserves_paragraphs_tables_and_image_refs(self):
        path = self._package("sample.docx", {
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>关键内容</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格值</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><a:blip r:embed="rId1"/></w:r></w:p></w:body></w:document>''',
            "word/_rels/document.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>''',
            "word/media/image1.png": "pixels",
        })
        manifest = self._inspect(path)
        self.assertEqual(1, manifest["summary"]["paragraphs"])
        self.assertEqual(1, manifest["tables"][0]["row_count"])
        self.assertEqual(1, len(manifest["assets"]))
        self.assertEqual("word/document.xml:paragraph:2:body_order:3",
                         manifest["assets"][0]["block_ref"])

    def test_docx_detects_legacy_vml_image_relationship(self):
        path = self._package("vml.docx", {
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:pict><v:shape><v:imagedata r:id="rIdVml"/></v:shape></w:pict></w:r></w:p></w:body></w:document>''',
            "word/_rels/document.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdVml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/legacy.jpg"/></Relationships>''',
            "word/media/legacy.jpg": "pixels",
        })
        manifest = self._inspect(path)
        self.assertEqual(1, len(manifest["assets"]))
        self.assertEqual("vml", manifest["assets"][0]["reference_type"])
        self.assertEqual("word/media/legacy.jpg", manifest["assets"][0]["part"])
        self.assertTrue(manifest["assets"][0]["block_ref"].endswith("paragraph:1:body_order:1"))
        self.assertNotIn("unreferenced_media", manifest)

    def test_docx_prefers_svg_relationship_over_raster_fallback(self):
        path = self._package("svg.docx", {
            "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="png" ContentType="image/png"/><Override PartName="/word/media/graphic" ContentType="image/svg+xml"/></Types>''',
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><a:blip r:embed="rRaster"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}"><asvg:svgBlip r:embed="rSvg"/></a:ext></a:extLst></a:blip></w:r></w:p></w:body></w:document>''',
            "word/_rels/document.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rRaster" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/fallback.png"/><Relationship Id="rSvg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/graphic"/></Relationships>''',
            "word/media/fallback.png": "fallback",
            "word/media/graphic": "<svg xmlns='http://www.w3.org/2000/svg'/>"})
        manifest = self._inspect(path)
        self.assertEqual(1, len(manifest["assets"]))
        self.assertEqual("svg", manifest["assets"][0]["reference_type"])
        self.assertEqual("word/media/graphic", manifest["assets"][0]["part"])
        self.assertTrue(manifest["assets"][0]["local_path"].endswith(".svg"))

    def test_image_format_conversion_to_png_when_libreoffice_available(self):
        if not _find_soffice():
            self.skipTest("LibreOffice 未安装")
        source = self.tmp / "vector.svg"
        source.write_text("""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" height="60" fill="#fff"/><path d="M10 30 H100" stroke="#000" stroke-width="4"/></svg>""", encoding="utf-8")
        rendered = render_image_for_mcp(source, self.tmp / "converted")
        self.assertEqual(".png", rendered.suffix)
        self.assertGreater(rendered.stat().st_size, 100)
        self.assertEqual(b"\x89PNG\r\n\x1a\n", rendered.read_bytes()[:8])

    def test_blank_direct_render_uses_pdf_raster_fallback(self):
        source = self.tmp / "vector.emf"
        source.write_bytes(b"fixture")
        blank = _rgb_png(2, 2, b"\xff\xff\xff" * 4)
        diagram = _rgb_png(2, 1, b"\xff\xff\xff\0\0\0")
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if "--convert-to" in command and command[command.index("--convert-to") + 1] == "png":
                (Path(command[command.index("--outdir") + 1]) / "vector.png").write_bytes(blank)
            elif "--convert-to" in command and command[command.index("--convert-to") + 1] == "pdf":
                pdf_dir = Path(command[command.index("--outdir") + 1])
                (pdf_dir / "vector.pdf").write_bytes(b"pdf")
            elif "-singlefile" in command:
                Path(command[-1] + ".png").write_bytes(diagram)
            else:
                raise AssertionError(f"unexpected command: {command}")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("ckp.office_parser._find_soffice", return_value="/fake/soffice"), \
             patch("ckp.office_parser._find_pdftoppm", return_value="/fake/pdftoppm"), \
             patch("ckp.office_parser.subprocess.run", side_effect=fake_run):
            result = render_image_for_mcp(source, self.tmp / "fallback")

        self.assertTrue(result.is_file())
        self.assertFalse(_png_is_blank(result))
        self.assertEqual(3, len(calls))

    def test_docx_skips_header_and_footer_decorative_media(self):
        path = self._package("decorations.docx", {
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>''',
            "word/header1.xml": '''<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:p><a:blip r:embed="rLogo"/></w:p></w:hdr>''',
            "word/_rels/header1.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rLogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/></Relationships>''',
            "word/media/logo.png": "pixels",
        })
        manifest = self._inspect(path)
        self.assertEqual([], manifest["assets"])
        self.assertNotIn("unreferenced_media", manifest)

    def test_image_only_paragraph_receives_adjacent_heading_context(self):
        path = self._package("legend-context.docx", {
            "word/document.xml": '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>流程图图例</w:t></w:r></w:p><w:p><w:r><a:blip r:embed="rId1"/></w:r></w:p><w:p><w:r><w:t>圆角矩形表示开始或结束</w:t></w:r></w:p></w:body></w:document>''',
            "word/_rels/document.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image.png"/></Relationships>''',
            "word/media/image.png": "pixels"})
        manifest = self._inspect(path)
        asset = manifest["assets"][0]
        self.assertIn("流程图图例", asset["block_context"])
        self.assertIn("圆角矩形表示开始或结束", asset["block_context"])

    def test_xlsx_keeps_cell_formula_and_row_structure(self):
        path = self._package("sample.xlsx", {
            "xl/workbook.xml": '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="问题" sheetId="1" r:id="rId1"/></sheets></workbook>''',
            "xl/_rels/workbook.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>''',
            "xl/worksheets/sheet1.xml": '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B3"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>编号</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>001</t></is></c><c r="B2"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>''',
        })
        manifest = self._inspect(path)
        sheet = manifest["sheets"][0]
        self.assertEqual("001", sheet["cells"]["A2"]["value"])
        self.assertEqual("1+1", sheet["cells"]["B2"]["formula"])
        self.assertEqual([1, 2], sheet["nonempty_row_numbers"])

    def test_quality_gate_rejects_non_word_material(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_office_scans (task_id TEXT, file_id TEXT, version_id TEXT, sha256 TEXT, parser_version TEXT, manifest_json TEXT)")
        task = {"task_id": "t-1", "materials": [
            {"file_id": "f-1", "version_id": "v1", "sha256": "abc", "filename": "issues.xlsx"}]}
        with self.assertRaisesRegex(ValueError, "仅支持 Word"):
            validate_quality_report(conn, task, "", {"asset_reviews": [], "flowcharts": [], "table_checks": []})

    def test_flow_quality_gaps_are_diagnostic_not_submission_blockers(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_office_scans (task_id TEXT, file_id TEXT, version_id TEXT, sha256 TEXT, parser_version TEXT, manifest_json TEXT)")
        task = {"task_id": "t-flow", "materials": [
            {"file_id": "f-flow", "version_id": "v1", "sha256": "xyz", "filename": "plan.docx"}]}
        manifest = {"format": "docx", "assets": [{"asset_id": "f-flow:diagram1", "part": "word/media/flow.png"}],
                    "unreferenced_media": [], "tables": []}
        import json
        conn.execute("INSERT INTO task_office_scans VALUES (?,?,?,?,?,?)",
                     ("t-flow", "f-flow", "v1", "xyz", PARSER_VERSION, json.dumps(manifest)))
        body = """## 图像与流程资产

流程图复原：A 到 B，原图位置第 2 页。

| 起点节点 | 终点节点 | 箭头方向 | 条件 | 动作 | 原图核验 |
|---|---|---|---|---|---|
| A | B | A→B | 满足条件 | 执行动作 | 已逐边核验 |
"""
        report = {"asset_reviews": [{"asset_id": "f-flow:diagram1", "category": "flowchart",
                                      "status": "reviewed", "description": "流程图，含 A 到 B 的分支",
                                      "body_ref": "流程图复原：A 到 B"}],
                  "flowcharts": [{"asset_id": "f-flow:diagram1", "checked_against_original": True,
                                  "body_ref": "流程图复原：A 到 B",
                                  "nodes": [{"id": "A", "name": "节点 A"}, {"id": "B", "name": "节点 B"}],
                                  "edges": [{"from": "A", "to": "B", "direction": "A→B",
                                             "condition": "满足条件", "action": "执行动作",
                                             "verified_against_original": True}]}],
                  "table_checks": []}
        result = validate_quality_report(conn, task, body, report)
        self.assertEqual("diagnostic_only", result["status"])
        self.assertEqual(1, result["asset_count"])
        report["flowcharts"][0]["edges"][0]["verified_against_original"] = False
        result = validate_quality_report(conn, task, body, report)
        self.assertEqual("diagnostic_only", result["status"])

    def test_quality_gate_does_not_require_orphan_media_to_be_visually_reviewed(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_office_scans (task_id TEXT, file_id TEXT, version_id TEXT, sha256 TEXT, parser_version TEXT, manifest_json TEXT)")
        task = {"task_id": "t-orphan", "materials": [
            {"file_id": "f-orphan", "version_id": "v1", "sha256": "abc", "filename": "plan.docx"}]}
        import json
        manifest = {"format": "docx", "assets": [], "unreferenced_media": ["word/media/unused.emf"], "tables": []}
        conn.execute("INSERT INTO task_office_scans VALUES (?,?,?,?,?,?)",
                     ("t-orphan", "f-orphan", "v1", "abc", PARSER_VERSION, json.dumps(manifest)))
        result = validate_quality_report(conn, task, "正文", {
            "asset_reviews": [], "flowcharts": [], "table_checks": []})
        self.assertEqual(0, result["asset_count"])
        self.assertNotIn("unreferenced_media_count", result)

    def test_incorrect_image_exclusion_is_diagnostic_not_a_submission_blocker(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_office_scans (task_id TEXT, file_id TEXT, version_id TEXT, sha256 TEXT, parser_version TEXT, manifest_json TEXT)")
        task = {"task_id": "t-decoration", "materials": [
            {"file_id": "f-decoration", "version_id": "v1", "sha256": "abc", "filename": "plan.docx"}]}
        import json
        manifest = {"format": "docx", "assets": [{"asset_id": "f-decoration:word/document.xml:rId1:1", "part": "word/media/shape.emf"}], "tables": []}
        conn.execute("INSERT INTO task_office_scans VALUES (?,?,?,?,?,?)",
                     ("t-decoration", "f-decoration", "v1", "abc", PARSER_VERSION, json.dumps(manifest)))
        report = {"asset_reviews": [], "irrelevant_asset_ids": ["f-decoration:word/document.xml:rId1:1"],
                  "flowcharts": [], "table_checks": []}
        result = validate_quality_report(conn, task, "正文", report)
        self.assertEqual(1, result["asset_count"])
        result = validate_quality_report(conn, task, "引用 word/media/shape.emf", report)
        self.assertEqual("diagnostic_only", result["status"])
        report["irrelevant_asset_ids"] = ["f-decoration:unknown"]
        result = validate_quality_report(conn, task, "正文", report)
        self.assertEqual("diagnostic_only", result["status"])


if __name__ == "__main__":
    unittest.main()
