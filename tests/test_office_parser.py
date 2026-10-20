"""OOXML Office parser smoke tests using small, deterministic packages."""
from __future__ import annotations

import tempfile
import sqlite3
import unittest
import zipfile
from pathlib import Path

from tests._path import *  # noqa: F401,F403
from ckp.distill import validate_quality_report
from ckp.office_parser import PARSER_VERSION, _find_soffice, _sha256, inspect_office, render_image_for_mcp


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
