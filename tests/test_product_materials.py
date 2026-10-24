"""Product-material search must stay inside its local SQLite catalog."""
from __future__ import annotations

import tempfile
import unittest
import hashlib
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from tests._path import *  # noqa: F401,F403
from ckp.product_materials import (PRODUCT_MATERIAL_INDEX_VERSION, ProductMaterials,
                                   _figure_key_from_id, _figure_query_id, _figure_references,
                                   _heading_quality_warnings, _is_toc_style,
                                   _numbered_heading_level,
                                   _sections)
from ckp.office_parser import PARSER_VERSION


class _Collection:
    def __init__(self, rows):
        self.rows = rows
        self.lookups = []

    def find(self, query):
        return [row for row in self.rows if all(row.get(k) == v for k, v in query.items())]

    def find_one(self, query):
        self.lookups.append(query)
        return next((row for row in self.rows if all(row.get(k) == v for k, v in query.items())), None)


class ProductMaterialSearchTests(unittest.TestCase):
    def test_exact_figure_query_uses_figure_mapping_without_vector_search(self):
        self.assertEqual("图5.2-01", _figure_query_id("图5.2.01"))
        self.assertEqual("图5.2-01", _figure_query_id("图5.2－01"))
        self.assertEqual(_figure_key_from_id("图5.2.01"),
                         _figure_key_from_id("图5.2-01"))
        self.assertEqual("流程图 05.02.01", _figure_query_id("流程图 05.02.01"))
        self.assertEqual("图05.02.01", _figure_query_id("图05.02.01"))
        self.assertEqual("图例05/02/01", _figure_query_id("图例：05／02／01"))
        self.assertEqual("图1", _figure_query_id("图1"))
        self.assertEqual("图@body_order:12#2", _figure_query_id("图@body_order:12#2"))
        self.assertEqual("存货核算图 5.2-01", _figure_query_id("存货核算图5.2-01"))

    def test_word_toc_rows_do_not_create_headings_or_enter_body(self):
        manifest = {
            "paragraphs": [
                {"index": 1, "body_order": 1, "text": "总账管理", "style": "Heading1",
                 "heading_level": 1},
                {"index": 2, "body_order": 2, "text": "3.3.2.2 采购结算", "style": "TOC4"},
                {"index": 3, "body_order": 3, "text": "正文保留该编号和标题原文。"},
            ], "tables": [], "assets": [],
        }
        sections, _ = _sections(manifest, "pm-test", 1)
        self.assertTrue(_is_toc_style("TOC4"))
        self.assertTrue(_is_toc_style("目录 2"))
        self.assertFalse(_is_toc_style("Heading 2"))
        self.assertEqual(1, len(sections))
        self.assertEqual(["总账管理"], sections[0]["heading_path"])
        self.assertEqual("正文保留该编号和标题原文。", sections[0]["text"])

    def test_toc_body_number_mismatch_is_reported_without_rewriting(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.3.2.2 采购结算", "style": "TOC4"},
            {"index": 2, "text": "3.3.2.2 采购结算", "style": "Heading 4",
             "heading_level": 4},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(1, len(warnings))
        self.assertEqual("toc_body_heading_number_mismatch", warnings[0]["code"])
        self.assertEqual("5.3.2.2", warnings[0]["toc_number"])
        self.assertEqual("3.3.2.2", warnings[0]["body_number"])
        self.assertEqual("采购结算", warnings[0]["title"])

    def test_toc_body_matching_number_has_no_warning(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "7.8.12 成本核算", "style": "TOC4"},
            {"index": 2, "text": "7.8.12 成本核算", "style": "Heading 4",
             "heading_level": 4},
        ]}
        self.assertEqual([], _heading_quality_warnings(manifest))

    def test_toc_entry_without_body_heading_is_reported(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.2.4 接口设置", "style": "TOC3"},
            {"index": 2, "text": "5.2.3 委外加工", "style": "Heading 3",
             "heading_level": 3},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(["toc_heading_missing_from_body"], [w["code"] for w in warnings])
        self.assertEqual(1, warnings[0]["toc_paragraph"])

    def test_toc_body_heading_level_mismatch_is_reported(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.2.4 接口设置", "style": "TOC2"},
            {"index": 2, "text": "5.2.4 接口设置", "style": "Heading 3",
             "heading_level": 3},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(["toc_body_heading_level_mismatch"], [w["code"] for w in warnings])
        self.assertEqual((2, 3), (warnings[0]["toc_level"], warnings[0]["body_level"]))

    def test_toc_body_order_mismatch_is_reported(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.2.1 采购", "style": "TOC2"},
            {"index": 2, "text": "5.2.2 销售", "style": "TOC2"},
            {"index": 3, "text": "5.2.2 销售", "style": "Heading 2",
             "heading_level": 2},
            {"index": 4, "text": "5.2.1 采购", "style": "Heading 2",
             "heading_level": 2},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(["toc_body_heading_order_mismatch"], [w["code"] for w in warnings])

    def test_numbered_heading_parent_mismatch_is_reported(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.3.2 业务处理", "style": "Heading 3",
             "heading_level": 3},
            {"index": 2, "text": "3.3.2.2 多工厂集中销售", "style": "Heading 4",
             "heading_level": 4},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(["heading_number_parent_mismatch"], [w["code"] for w in warnings])
        self.assertEqual("3.3.2", warnings[0]["expected_parent_number"])
        self.assertEqual("5.3.2", warnings[0]["actual_parent_number"])

    def test_numbered_heading_without_space_is_recognized(self):
        self.assertEqual(4, _numbered_heading_level("3.3.2.2多工厂集中销售"))

    def test_missing_number_between_siblings_is_reported(self):
        manifest = {"paragraphs": [
            {"index": 1, "text": "5.2.1 采购", "style": "Heading 3",
             "heading_level": 3},
            {"index": 2, "text": "5.2.3 委外加工", "style": "Heading 3",
             "heading_level": 3},
        ]}
        warnings = _heading_quality_warnings(manifest)
        self.assertEqual(["heading_number_gap"], [w["code"] for w in warnings])
        self.assertEqual(["5.2.2"], warnings[0]["missing_numbers"])

    def test_read_image_restores_extension_for_content_addressed_docx(self):
        with tempfile.TemporaryDirectory(prefix="product-material-image-") as tmp:
            root = Path(tmp)
            source_dir = root / "data" / "originals" / "ab"
            source_dir.mkdir(parents=True)
            source = source_dir / "source-fixture"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("word/_rels/document.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
                    '</Relationships>')
                archive.writestr("word/media/image1.png", b"fixture-png")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            source.rename(source_dir / source_hash)
            data_dir = root / "data"
            service = ProductMaterials(data_dir, Path(__file__).resolve().parents[1])
            try:
                service.store.conn.execute(
                    "INSERT INTO blobs(sha256,size,rel_path) VALUES(?,?,?)",
                    (source_hash, (source_dir / source_hash).stat().st_size,
                     f"originals/ab/{source_hash}"))
                service.store.conn.execute(
                    "INSERT INTO product_materials(material_id,category,product,title,current_version_id,active,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ("pm-fixture", "产品手册", "NCC", "测试手册", "pmv-fixture", 1, "now", "now"))
                service.store.conn.execute(
                    "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
                    "domains_json,parser_version,section_count,image_count,state,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("pmv-fixture", "pm-fixture", 1, source_hash,
                     (source_dir / source_hash).stat().st_size, "application/docx", "测试手册.docx",
                     '["财务会计"]', PRODUCT_MATERIAL_INDEX_VERSION, 1, 1, "ready", "now"))
                service.store.conn.commit()
                records = _Collection([{
                    "_id": "pm-fixture@1", "state": "ready", "version_id": "pmv-fixture",
                    "source_sha256": source_hash, "figure_refs": [{
                        "figure_id": "图5.2-01", "relationship_id": "rId1",
                        "part": "word/media/image1.png"}],
                    "sections": [{"figure_ids": ["图5.2-01"], "heading_path": ["5.2"]}],
                }])
                service._rag = MagicMock(mongo=MagicMock())
                service._rag.mongo.get_collection.return_value = records
                payload, image_format, context = service.read_image(
                    "pm-fixture", 1, source_hash, "图5.2-01")
                self.assertEqual(b"fixture-png", payload)
                self.assertEqual("png", image_format)
                self.assertEqual(["5.2"], context["heading_path"])
                self.assertEqual("image/png", context["mime_type"])
                self.assertTrue(context["hash_verified"])
            finally:
                service.close()

    def test_formal_figure_caption_gets_marker_and_exact_image_mapping(self):
        manifest = {
            "paragraphs": [
                {"index": 1, "body_order": 1, "text": "5.2 集成应用", "style": "Heading 2"},
                {"index": 2, "body_order": 2, "text": "接口图如下："},
                {"index": 3, "body_order": 4, "text": "图 5.2.01 存货核算功能接口图"},
                {"index": 4, "body_order": 5, "text": "下一段"},
            ],
            "tables": [],
            "assets": [{"kind": "image", "part": "word/media/image1.png",
                        "relationship_id": "rId7",
                        "block_ref": "word/document.xml:paragraph:2:body_order:3"}],
        }
        refs, by_order, rule = _figure_references(manifest)
        sections, chunks = _sections(manifest, "pm-test", 2,
                                     figure_refs_by_order=by_order)
        self.assertEqual("图5.2-01", refs[0]["figure_id"])
        self.assertIn("[[FIGURE_REF: 图5.2-01]]", sections[0]["text"])
        self.assertEqual(["图5.2-01"], sections[0]["figure_ids"])
        self.assertEqual("rId7", sections[0]["figure_refs"][0]["relationship_id"])
        self.assertTrue(any("[[FIGURE_REF: 图5.2-01]]" in chunk["text"] for chunk in chunks))
        self.assertEqual("detected", rule["status"])
        self.assertEqual("图", rule["numbered_patterns"][0]["label"])

    def test_ambiguous_or_duplicate_figure_mapping_fails_closed(self):
        manifest = {
            "paragraphs": [{"index": 1, "body_order": 3, "text": "图5.2-01 接口图"}],
            "assets": [
                {"kind": "image", "part": "word/media/a.png", "relationship_id": "rId1",
                 "block_ref": "word/document.xml:paragraph:1:body_order:1"},
                {"kind": "image", "part": "word/media/b.png", "relationship_id": "rId2",
                 "block_ref": "word/document.xml:paragraph:2:body_order:2"},
                {"kind": "image", "part": "word/media/c.png", "relationship_id": "rId3",
                 "block_ref": "word/document.xml:paragraph:8:body_order:2"},
            ],
        }
        refs, _, _ = _figure_references(manifest)
        self.assertEqual([], refs)  # Neither duplicate IDs nor nearby uncited images are guessed.
        self.assertNotEqual(PARSER_VERSION, PRODUCT_MATERIAL_INDEX_VERSION)

    def test_document_specific_figure_rules_and_unnumbered_image_positions(self):
        manifest = {
            "paragraphs": [
                {"index": 1, "body_order": 3, "text": "流程图 05.02.01 业务流转"},
                {"index": 2, "body_order": 6, "text": "图例：审批节点说明"},
            ],
            "tables": [],
            "assets": [
                {"kind": "image", "part": "word/media/a.png", "relationship_id": "rId1",
                 "block_ref": "word/document.xml:paragraph:1:body_order:2"},
                {"kind": "image", "part": "word/media/b.png", "relationship_id": "rId2",
                 "block_ref": "word/document.xml:paragraph:3:body_order:5"},
            ],
        }
        refs, by_order, rule = _figure_references(manifest)
        self.assertEqual(["流程图 05.02.01", "图@body_order:5"],
                         [ref["figure_id"] for ref in refs])
        self.assertEqual("numbered_caption", refs[0]["locator_type"])
        self.assertEqual("body_position", refs[1]["locator_type"])
        self.assertEqual("审批节点说明", refs[1]["caption_title"])
        self.assertEqual(["流程图"], [row["label"] for row in rule["numbered_patterns"]])
        self.assertTrue(rule["requires_confirmation"])
        self.assertEqual(["rId1"], [ref["relationship_id"] for ref in by_order[3]])
        self.assertEqual(["rId2"], [ref["relationship_id"] for ref in by_order[5]])
        sections, _ = _sections(manifest, "pm-rules", 1,
                                figure_refs_by_order=by_order)
        self.assertIn("[[FIGURE_REF: 图@body_order:5]]", sections[0]["text"])
        self.assertEqual("流程图", sections[0]["figure_refs"][0]["label"])

    def test_table_embedded_unnumbered_image_gets_table_locator(self):
        manifest = {
            "paragraphs": [],
            "tables": [{"table": 1, "body_order": 4, "rows": [
                {"row": 1, "cells": [{"column": 1, "text": "图例：库存流转"}]}
            ]}],
            "assets": [{"kind": "image", "part": "word/media/t.png", "relationship_id": "rIdT",
                        "block_ref": "word/document.xml:table:1", "block_context": "图例：库存流转"}],
        }
        refs, _, rule = _figure_references(manifest)
        self.assertEqual("图@table:1#1", refs[0]["figure_id"])
        self.assertEqual("库存流转", refs[0]["caption_title"])
        self.assertEqual(1, refs[0]["table_number"])
        self.assertEqual("图例", rule["unnumbered_candidates"][0]["label"])

    def test_figure_caption_does_not_jump_over_body_text_to_an_image(self):
        manifest = {
            "paragraphs": [
                {"index": 1, "body_order": 2, "text": "该段包含与图无关的操作说明。"},
                {"index": 2, "body_order": 3, "text": "图5.2-01 接口图"},
            ],
            "tables": [],
            "assets": [{"kind": "image", "part": "word/media/a.png", "relationship_id": "rId1",
                        "block_ref": "word/document.xml:paragraph:1:body_order:1"}],
        }
        refs, _, rule = _figure_references(manifest)
        self.assertEqual([], refs)
        self.assertEqual("needs_human_review", rule["confirmation_state"])
        self.assertEqual([3], [row["body_order"] for row in rule["numbered_patterns"][0]["examples"]])

    def test_search_ignores_active_mongo_heads_missing_from_local_catalog(self):
        with tempfile.TemporaryDirectory(prefix="product-material-search-") as tmp:
            service = ProductMaterials(Path(tmp) / "data", Path(__file__).resolve().parents[1])
            try:
                service.store.conn.execute(
                    "INSERT INTO product_materials(material_id,category,product,title,current_version_id,active,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ("local-pm", "产品手册", "NCC", "总账管理", "local-v1", 1, "now", "now"))
                service.store.conn.execute(
                    "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
                    "domains_json,parser_version,section_count,image_count,state,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("local-v1", "local-pm", 1, "local-hash", 10, "application/docx", "总账管理.docx",
                     '["财务会计"]', "parser-v1", 1, 0, "ready", "now"))
                service.store.conn.commit()

                prefix = "test_"
                collection_name = prefix + "cb_product_manuals"
                heads = _Collection([
                    {"_id": "local-pm", "record_id": "local-pm@1", "revision": 1,
                     "active": True, "collection": collection_name},
                    {"_id": "foreign-pm", "record_id": "foreign-pm@9", "revision": 9,
                     "active": True, "collection": collection_name},
                ])
                records = _Collection([
                    {"_id": "local-pm@1", "material_id": "local-pm", "revision": 1,
                     "version_id": "local-v1", "source_sha256": "local-hash", "state": "ready",
                     "chunk_ids": ["local-chunk"], "products": "NCC", "domain": ["财务会计"],
                     "title": "总账管理", "sections": [{"section_id": "s1",
                     "figure_ids": ["图5.2-01"]}]},
                    {"_id": "foreign-pm@9", "material_id": "foreign-pm", "revision": 9,
                     "version_id": "foreign-v9", "source_sha256": "foreign-hash", "state": "ready",
                     "chunk_ids": ["foreign-chunk"], "products": "NCC", "domain": ["财务会计"],
                     "title": "总账管理"},
                ])
                mongo = MagicMock()
                mongo.get_collection.side_effect = lambda name: (
                    heads if name == prefix + "product_material_heads" else records)
                rag = MagicMock(prefix=prefix, mongo=mongo, mode="fixture")
                rag._db.return_value.search.return_value = [{
                    "id": "local-chunk", "score": 0.9, "text": "凭证管理",
                    "metadata": {"section_id": "s1", "heading_path": ["总账管理"]},
                }]
                rag._db.return_value.query_by_ids.return_value = [{
                    "id": "local-chunk", "text": "## 总账管理\n\n[[FIGURE_REF: 图5.2-01]]",
                    "metadata": '{"section_id":"s1","heading_path":["总账管理"]}',
                }]
                service._rag = rag

                hits = service.search("凭证管理", product="NCC", domain=["财务会计"],
                                       category="产品手册", limit=1)

                self.assertEqual(["local-pm"], [hit["material_id"] for hit in hits])
                self.assertEqual(1, len(records.lookups))
                self.assertEqual({"_id": "local-pm@1", "state": "ready"}, records.lookups[0])

                exact_hits = service.search("图5.2.01", product="NCC", domain=["财务会计"],
                                            category="产品手册", limit=1)
                self.assertEqual("图5.2-01", exact_hits[0]["metadata"]["figure_ids"][0])
                self.assertEqual("figure_id", exact_hits[0]["exact_match"])
                self.assertEqual("", exact_hits[0]["stage"])
                self.assertEqual("", exact_hits[0]["metadata"]["stage"])
                rag._db.return_value.search.assert_called_once()
                rag._db.return_value.query_by_ids.assert_called_once_with(["local-chunk"])
            finally:
                service.close()

    def test_idempotent_ingest_returns_saved_counts_without_claiming_fresh_readback(self):
        with tempfile.TemporaryDirectory(prefix="product-material-idempotent-") as tmp:
            root = Path(__file__).resolve().parents[1]
            source = Path(tmp) / "测试手册.docx"
            source.write_bytes(b"fixture word bytes")
            service = ProductMaterials(Path(tmp) / "data", root)
            try:
                import hashlib

                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                service.store.conn.execute(
                    "INSERT INTO product_materials(material_id,category,product,title,current_version_id,active,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ("pm-test", "产品手册", "NCC", "测试手册", "pmv-test", 1, "now", "now"))
                service.store.conn.execute(
                    "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
                    "domains_json,parser_version,section_count,image_count,state,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("pmv-test", "pm-test", 1, source_hash, source.stat().st_size,
                     "application/docx", source.name, '["财务会计"]', PRODUCT_MATERIAL_INDEX_VERSION, 2, 1, "ready", "now"))
                service.store.conn.commit()

                prefix = "test_"
                service._rag = MagicMock(prefix=prefix, mode="fixture")
                collection_name = service._collection("产品手册")
                heads = _Collection([{
                    "_id": "pm-test", "record_id": "pm-test@1", "revision": 1,
                    "active": True, "collection": collection_name,
                }])
                records = _Collection([{
                    "_id": "pm-test@1", "material_id": "pm-test", "revision": 1,
                    "version_id": "pmv-test", "source_sha256": source_hash, "state": "ready",
                    "chunk_ids": ["chunk-1", "chunk-2"],
                    "sections": [{"section_id": "s1"}, {"section_id": "s2"}],
                    "image_count": 1, "products": "NCC", "domain": ["财务会计"],
                    "title": "测试手册",
                }])
                mongo = MagicMock()
                mongo.get_collection.side_effect = lambda name: (
                    heads if name == prefix + "product_material_heads" else records)
                service._rag.mongo = mongo

                result = service.ingest(source, "产品手册", "NCC", ["财务会计"])

                self.assertEqual("unchanged", result["state"])
                self.assertEqual(2, result["sections"])
                self.assertEqual(2, result["chunks"])
                self.assertEqual(2, result["verified_chunks"])
                self.assertEqual("original_ingest", result["verification_basis"])
                self.assertEqual("", result["stage"])
                self.assertFalse(result["readback_performed_this_call"])
                self.assertEqual(1, result["embedded_images_not_ocr_processed"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
