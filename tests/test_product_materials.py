"""Product-material search must stay inside its local SQLite catalog."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tests._path import *  # noqa: F401,F403
from ckp.product_materials import ProductMaterials


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
                     "title": "总账管理"},
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
                service._rag = rag

                hits = service.search("凭证管理", product="NCC", domain=["财务会计"],
                                       category="产品手册", limit=1)

                self.assertEqual(["local-pm"], [hit["material_id"] for hit in hits])
                self.assertEqual(1, len(records.lookups))
                self.assertEqual({"_id": "local-pm@1", "state": "ready"}, records.lookups[0])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
