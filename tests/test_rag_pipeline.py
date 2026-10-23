"""Boundary tests for the actual ingestion parser; no database/model required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from tests._path import *
from ckp.md_render import build_sidecar, render_frontmatter, extract_rag_payload
from chunks_and_in_vector.pipeline import SandboxRAG, prepare

BODY = '''## Parent <!-- kd-section id=parent rag=body -->
confirmed fact
### Excluded child <!-- kd-section id=excluded rag=exclude -->
PRIVATE_SENTINEL
#### Included detail <!-- kd-section id=detail rag=body -->
uncertainty must remain
###### Deep child <!-- kd-section id=deep rag=body -->
scope limit must remain
'''

class RagPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'draft.md'
        self.addCleanup(self.tmp.cleanup)

    def write(self, body=BODY, **changes):
        stage = changes.pop('stage', '方案与设计')
        meta = {'knowledge_id':'k-fixture', 'revision':1, 'title':'Test',
                'knowledge_type':'方案设计', 'status':'draft', 'project_id':'p-fixture',
                'content_hash':hashlib.sha256(body.encode()).hexdigest(), **changes}
        text = render_frontmatter(meta) + body
        refs = [{'file_id':'f-test', 'version_id':'v-test', 'sha256':'a'*64}]
        source_refs = [{'source_ref_id':'f-test@v-test', **refs[0]}]
        payload = extract_rag_payload(text, [], source_refs)
        payload['source_refs'] = source_refs
        sidecar = {**meta, 'stage':stage, 'sources':refs, 'rag_selection':payload}
        if meta.get('products'):
            sidecar['classification'] = {'products':meta['products'], 'domain':meta.get('domain', [])}
        self.path.write_text(text)
        self.path.with_suffix('.json').write_text(json.dumps(sidecar))
        return sidecar

    def test_nested_body_only_and_source_identity(self):
        self.write()
        item = prepare(self.path)
        self.assertEqual(3, len(item['chunks']))
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(c['text'] for c in item['chunks']))
        self.assertIn('uncertainty must remain', item['payload']['retrieval_md'])
        self.assertEqual(['Parent','Excluded child','Included detail','Deep child'],
                         item['chunks'][-1]['section']['heading_path'])

    def test_body_tampering_rejected(self):
        self.write(); self.path.write_text(self.path.read_text()+'tamper')
        with self.assertRaisesRegex(ValueError, '哈希'): prepare(self.path)

    def test_sidecar_metadata_tampering_rejected(self):
        sidecar = self.write();sidecar['title']='changed'
        self.path.with_suffix('.json').write_text(json.dumps(sidecar))
        with self.assertRaisesRegex(ValueError, '不一致'): prepare(self.path)

    def test_invalid_and_duplicate_routes_fail_closed(self):
        for body in ['## No marker\ntext',
                     '## A <!-- kd-section id=dup rag=body -->\nx\n## B <!-- kd-section id=dup rag=body -->\ny',
                     '## Legacy <!-- kd-section id=legacy rag=metadata -->\nsecret',
                     '## Legacy <!-- kd-section id=legacy rag=audit -->\nsecret']:
            with self.subTest(body=body):
                self.write(body)
                with self.assertRaisesRegex(ValueError, '路由'): prepare(self.path)

    def test_sidecar_manifest_must_match(self):
        sidecar=self.write();sidecar['rag_selection']['sections'][0]['content_sha256']='b'*64
        self.path.with_suffix('.json').write_text(json.dumps(sidecar))
        with self.assertRaisesRegex(ValueError, '清单'): prepare(self.path)

    def test_missing_source_revision_rejected(self):
        sidecar=self.write();sidecar['sources'][0].pop('version_id')
        self.path.with_suffix('.json').write_text(json.dumps(sidecar))
        with self.assertRaisesRegex(ValueError, '来源'): prepare(self.path)

    def test_unknown_stage_rejected(self):
        self.write(stage='项目知识')
        with self.assertRaisesRegex(ValueError, '未知项目阶段.*立项与范围.*运维与改进'):
            prepare(self.path)

    def test_product_domain_must_match_frontmatter_sidecar_and_catalog(self):
        sidecar = self.write(products='致远 OA', domain=['办公协同'])
        item = prepare(self.path)
        self.assertEqual('致远 OA', item['meta']['products'])
        self.assertEqual(['办公协同'], item['meta']['domain'])
        sidecar['classification']['domain'] = ['财务会计']
        self.path.with_suffix('.json').write_text(json.dumps(sidecar))
        with self.assertRaisesRegex(ValueError, '不一致'):
            prepare(self.path)

    def test_selected_snapshot_survives_catalog_update(self):
        self.write(products='致远 OA', domain=['办公协同'])
        changed_catalog = Path(self.tmp.name) / 'new-catalog.json'
        changed_catalog.write_text(json.dumps({'schema':'ckp-product-domain-catalog/v1',
                                               'products':[{'name':'Other','domains':['Other domain']}]}))
        with patch.dict('os.environ', {'CKP_PRODUCT_DOMAIN_CATALOG':str(changed_catalog)}):
            item = prepare(self.path)
        self.assertEqual('致远 OA', item['meta']['products'])

    def test_legacy_body_product_scope_is_not_inferred_as_classification(self):
        body = '产品范围：致远 OA。\n' + BODY
        draft = {'knowledge_id':'k-legacy','revision':1,'title':'Legacy',
                 'knowledge_type':'项目知识','status':'draft','content_hash':hashlib.sha256(body.encode()).hexdigest(),
                 'body_md':body,'topics':[],'stage':'方案与设计','task_id':'t-legacy',
                 'rules_version':'rules-v1','model':'legacy','created_at':'now','sources':[],
                 'products':'','domain':[]}
        project = {'project_id':'p-fixture','name':'Project','industry':[],'products':[],
                   'owner':'','tags':[]}
        sidecar = build_sidecar(draft, project)
        self.assertNotIn('classification', sidecar)
        self.assertNotIn('products:', render_frontmatter({'products':''}))
        self.assertNotIn('产品范围', json.dumps(sidecar['rag_selection']['document_ref'], ensure_ascii=False))

    def test_all_six_stages_route_to_their_collections(self):
        expected = {
            '立项与范围': 'cb_initiation_scope',
            '调研与需求': 'cb_research_requirements',
            '方案与设计': 'cb_solution_design',
            '实施与变更': 'cb_implementation_change',
            '测试与验收': 'cb_test_acceptance',
            '运维与改进': 'cb_operations_improvement',
        }
        for stage, collection in expected.items():
            with self.subTest(stage=stage):
                self.write(stage=stage, knowledge_type='同一项目知识文档类型')
                prepared = prepare(self.path)
                self.assertEqual(collection, prepared['collection'])
                self.assertEqual(stage, prepared['stage'])
                self.assertNotIn('stage', prepared['meta'])
                self.assertEqual('同一项目知识文档类型', prepared['meta']['knowledge_type'])

    def test_search_filters_by_stage_then_document_type(self):
        rag = SandboxRAG.__new__(SandboxRAG)
        rag.mode = 'fixture'
        rag.prefix = 'kd_dev_fixture_'
        heads = MagicMock()
        heads.find.return_value = [
            {'_id': 'k-one', 'record_id': 'k-one@1'},
            {'_id': 'k-two', 'record_id': 'k-two@1'},
        ]
        heads.find_one.return_value = {'active': True}
        records = MagicMock()
        records.find_one.side_effect = [
            {'_id': 'k-one@1', 'knowledge_id': 'k-one', 'stage': '调研与需求',
             'knowledge_type': '项目知识', 'state': 'ready', 'chunk_ids': ['c-one'],
             'products': '致远 OA', 'domain': ['办公协同'],
             'publication': {'status': 'approved'}},
            {'_id': 'k-two@1', 'knowledge_id': 'k-two', 'stage': '调研与需求',
             'knowledge_type': '调研报告', 'state': 'ready', 'chunk_ids': ['c-two'],
             'products': 'YonBIP 高级版', 'domain': ['财务会计'],
             'publication': {'status': 'approved'}},
        ]
        rag.mongo = MagicMock()
        rag.mongo.get_collection.side_effect = lambda name: heads if name.endswith('heads') else records
        vector_db = MagicMock()
        vector_db.search.return_value = [
            {'id': 'c-one', 'score': 0.9, 'text': 'A', 'metadata': {}},
            {'id': 'c-two', 'score': 0.8, 'text': 'B', 'metadata': {}},
        ]
        rag._db = MagicMock(return_value=vector_db)

        results = rag.search('查询', '项目知识', 3, True, '调研与需求',
                             products='致远 OA', domain=['办公协同'])

        rag._db.assert_called_once_with('kd_dev_fixture_cb_research_requirements')
        self.assertEqual(['c-one'], [item['id'] for item in results])
        self.assertEqual('调研与需求', results[0]['stage'])
        self.assertEqual('项目知识', results[0]['knowledge_type'])
        self.assertEqual('致远 OA', results[0]['products'])
        self.assertEqual(['办公协同'], results[0]['domain'])

    def test_no_indexable_content_rejected(self):
        self.write('## Excluded <!-- kd-section id=only rag=exclude -->\nnot searchable')
        with self.assertRaisesRegex(ValueError, 'body'): prepare(self.path)

if __name__ == '__main__': unittest.main()
