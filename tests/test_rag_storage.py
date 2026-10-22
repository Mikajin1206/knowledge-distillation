"""Regression checks for dropped provenance, duplicate embedding and bad vectors."""
import asyncio
from unittest.mock import MagicMock
import unittest
from tests._path import *
from langchain_core.documents import Document
from chunks_and_in_vector.milvus_db import MilvusDB

class RagStorageTests(unittest.TestCase):
    def db(self, vectors=None):
        embedding=MagicMock()
        embedding.dimension=4
        embedding.embed_documents.return_value=vectors or [[1.,0.,0.,0.]]
        db=MilvusDB('test_collection',embedding=embedding,dimension=4)
        db._collection=MagicMock()
        return db,embedding

    def test_all_source_and_version_metadata_survives_serialization(self):
        import json
        db,_=self.db()
        doc=Document(page_content='Fact',metadata={'knowledge_id':'k-test','revision':2,
                                                  'section_id':'fact','sources':[{'version_id':'v1'}]})
        db.insert([doc],ids=['chunk'])
        columns=db._collection.upsert.call_args.args[0]
        self.assertEqual(doc.metadata,json.loads(columns[2][0]))

    def test_async_insert_embeddings_run_once(self):
        db,embedding=self.db()
        asyncio.run(db.ainsert([Document(page_content='Fact')],ids=['chunk']))
        embedding.embed_documents.assert_called_once_with(['Fact'])

    def test_vector_dimension_mismatch_stops_before_write(self):
        db,_=self.db([[1.,0.]])
        with self.assertRaisesRegex(ValueError,'维度'): db.insert([Document(page_content='Fact')])
        db._collection.upsert.assert_not_called()

    def test_no_zero_vector_fallback(self):
        db=MilvusDB('test_collection',dimension=4);db._collection=MagicMock()
        with self.assertRaisesRegex(ValueError,'embedding'): db.insert([Document(page_content='Fact')])
        db._collection.upsert.assert_not_called()

if __name__=='__main__': unittest.main()
