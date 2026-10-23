"""Local role, immutable-review and pre-activation integrity boundaries."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from tests._path import *
from ckp.knowledge import Knowledge, public_value
from ckp.md_render import export_draft
from chunks_and_in_vector.pipeline import SandboxRAG
from tests.test_distill import make_repo


class ReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Knowledge(Path(self.tmp.name)/'data', Path(self.tmp.name))
        self.addCleanup(self.service.close)
        self.env = patch.dict('os.environ', {'CKP_LOCAL_ACTOR':'tester', 'CKP_REVIEWER_IDS':'tester'})
        self.env.start();self.addCleanup(self.env.stop)
        self.service.snapshot = MagicMock(return_value=({'content_hash':'bodyhash'}, {}, {}, 'snapshot'))

    def test_catalog_change_preserves_review_snapshot_and_allows_publish(self):
        root = Path(self.tmp.name) / 'catalog-change'
        repo = make_repo(root)
        data_dir = root / 'data'
        service = Knowledge(data_dir, repo)
        self.addCleanup(service.close)
        catalog_path = repo / 'config' / 'product_domain_catalog.json'
        body = ('## 范围 <!-- kd-section id=scope rag=body -->\n'
                '已确认产品范围与实施边界。\n')

        with patch.dict('os.environ', {'CKP_PRODUCT_DOMAIN_CATALOG': str(catalog_path)}):
            project = service.store.create_project('目录快照项目', owner='PM')
            source_path = write_minimal_docx(root / 'source.docx', '分类快照测试材料')
            uploaded = service.store.upload(source_path, project['project_id'])['uploaded'][0]
            task = service.distill.start_task(project['project_id'], '方案与设计')
            service.distill.attach(task['task_id'], uploaded['file_id'], uploaded['version_id'])
            service.distill.inspect_word(task['task_id'], uploaded['file_id'])
            draft = service.distill.submit_draft(
                task['task_id'], '目录快照知识', '项目知识', body,
                sources=[{'file_id': uploaded['file_id'], 'version_id': uploaded['version_id'],
                          'location': '全文', 'supports': '分类快照测试材料'}],
                quality_report={'asset_reviews': [], 'flowcharts': [], 'table_checks': []},
                products='测试产品', domain=['测试领域'])

            _, _, before_sidecar, before_snapshot = service.snapshot(
                draft['knowledge_id'], draft['revision'], draft['content_hash'])
            export_before = export_draft(draft, project, root / 'before')
            before_md = Path(export_before['md']).read_bytes()
            before_json = Path(export_before['json']).read_bytes()
            review = service.submit_review(draft['knowledge_id'], draft['revision'], draft['content_hash'])
            service.record_review(review['review_id'], 'approved', '隔离分类快照测试')

            catalog_path.write_text(json.dumps({
                'schema': 'ckp-product-domain-catalog/v1',
                'products': [{'name': '新产品', 'domains': ['新领域']}],
            }, ensure_ascii=False), encoding='utf-8')

            _, _, after_sidecar, after_snapshot = service.snapshot(
                draft['knowledge_id'], draft['revision'], draft['content_hash'])
            export_after = export_draft(draft, project, root / 'after')
            self.assertEqual(before_snapshot, after_snapshot)
            self.assertEqual(before_sidecar['classification'], after_sidecar['classification'])
            self.assertEqual(before_md, Path(export_after['md']).read_bytes())
            self.assertEqual(before_json, Path(export_after['json']).read_bytes())

            published = {}

            class PreparedRAG:
                def __init__(self):
                    pass

                def ingest(self, md_path, publication=None, progress=None):
                    from chunks_and_in_vector.pipeline import prepare
                    prepared_item = prepare(md_path)
                    published['meta'] = prepared_item['meta']
                    published['classification'] = prepared_item['sidecar']['classification']
                    return {'state': 'indexed', 'chunks': len(prepared_item['chunks']),
                            'verified_chunks': len(prepared_item['chunks'])}

                def close(self):
                    pass

            class InlineThread:
                def __init__(self, target, args=(), daemon=None):
                    self.target, self.args = target, args
                    self.running = False

                def start(self):
                    self.running = True
                    try:
                        self.target(*self.args)
                    finally:
                        self.running = False

                def is_alive(self):
                    return self.running

            with patch('chunks_and_in_vector.pipeline.SandboxRAG', PreparedRAG), \
                    patch('ckp.knowledge.threading.Thread', InlineThread):
                result = service.publish(draft['knowledge_id'], draft['revision'], draft['content_hash'])

        self.assertEqual('published', result['status'])
        self.assertEqual('测试产品', published['meta']['products'])
        self.assertEqual(['测试领域'], published['meta']['domain'])
        self.assertEqual({'products': '测试产品', 'domain': ['测试领域']}, published['classification'])

    def test_publish_requires_exact_approved_snapshot(self):
        with self.assertRaisesRegex(ValueError,'审核通过'):
            self.service.publish('k-check',1,'bodyhash')
        requested=self.service.submit_review('k-check',1,'bodyhash')
        self.service.record_review(requested['review_id'],'approved','User authorized development verification')
        self.service.snapshot.return_value=({'content_hash':'bodyhash'}, {}, {}, 'changed-source-list')
        with self.assertRaisesRegex(ValueError,'审核通过'):
            self.service.publish('k-check',1,'bodyhash')

    def test_previous_revision_approval_cannot_publish_new_revision(self):
        requested = self.service.submit_review('k-check', 1, 'bodyhash')
        self.service.record_review(requested['review_id'], 'approved', 'approved revision 1')
        with self.assertRaisesRegex(ValueError, '精确修订尚无有效审核通过记录'):
            self.service.publish('k-check', 2, 'bodyhash')

    def test_actor_cannot_self_supply_review_role(self):
        requested=self.service.submit_review('k-check',1,'bodyhash')
        with patch.dict('os.environ',{'CKP_LOCAL_ACTOR':'unprivileged'}):
            with self.assertRaisesRegex(ValueError,'没有审核权限'):
                self.service.record_review(requested['review_id'],'approved','test')

    def test_retry_queues_same_task_before_returning(self):
        requested = self.service.submit_review('k-check', 1, 'bodyhash')
        self.service.record_review(requested['review_id'], 'approved', 'test')
        with patch('ckp.knowledge.threading.Thread') as thread:
            thread.return_value.is_alive.return_value = True
            first = self.service.publish('k-check', 1, 'bodyhash')
            self.service.progress(first['publish_task_id'], 'failed', 0, 0,
                                  status='publish_failed', error='old failure')
            retried = self.service.publish('k-check', 1, 'bodyhash')
        self.assertEqual(first['publish_task_id'], retried['publish_task_id'])
        self.assertEqual('queued', retried['status'])
        self.assertIsNone(retried['error'])
        self.assertEqual(2, thread.return_value.start.call_count)

    def test_review_decision_is_immutable_and_idempotent(self):
        requested=self.service.submit_review('k-check',1,'bodyhash')
        first=self.service.record_review(requested['review_id'],'approved','test')
        self.assertEqual(first,self.service.record_review(requested['review_id'],'approved','test'))
        with self.assertRaisesRegex(ValueError,'已有结论'):
            self.service.record_review(requested['review_id'],'changes_requested','changed decision')

    def test_bad_body_stops_versioned_read(self):
        self.service.distill.get_draft=MagicMock(return_value={'body_md':'tampered','content_hash':'claimed'})
        with self.assertRaisesRegex(ValueError,'正文哈希'):
            self.service.active_record('k-check',1,'claimed')

    def test_asset_mapping_must_match_publication_triple(self):
        body = '## 流程 <!-- kd-section id=flow rag=body -->\nasset_id=diagram\n'
        self.service.active_record=MagicMock(return_value=(
            {'title': '测试', 'body_md': body},
            {'publication':{'assets':[{'asset_id':'diagram','file_id':'f-approved','version_id':'v-approved','sha256':'a'*64}]},
             'json':{'protected_assets':{'items':[{'asset_id':'diagram'}]},
                     'rag_selection':{'source_refs':[]}}}))
        self.service.store.conn.execute('INSERT INTO knowledge_asset_refs VALUES (?,?,?,?,?,?)',('k-check',1,'diagram','f-other','v-other','b'*64))
        with self.assertRaisesRegex(ValueError,'指定版本不一致'):
            self.service.asset('k-check',1,'hash','diagram','flow')

    def test_asset_must_be_referenced_by_requested_body_section(self):
        body = ('## 流程 <!-- kd-section id=flow rag=body -->\nasset_id=diagram\n'
                '## 备注 <!-- kd-section id=notes rag=body -->\n普通说明\n')
        self.service.active_record=MagicMock(return_value=(
            {'title': '测试', 'body_md': body},
            {'publication':{'assets':[{'asset_id':'diagram','file_id':'f','version_id':'v','sha256':'a'*64}]},
             'json':{'protected_assets':{'items':[{'asset_id':'diagram'}]},
                     'rag_selection':{'source_refs':[]}}}))
        with self.assertRaisesRegex(ValueError,'未被指定正文分节引用'):
            self.service.asset('k-check',1,'hash','diagram','notes')

    def test_full_document_and_sections_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError,'不能同时指定'):
            self.service.read('k-check',1,'hash',section_ids=['flow'],full_document=True)

    def test_public_results_hide_sidecars_and_paths(self):
        self.assertEqual({'metadata':{'section_id':'s'}}, public_value({'fileUrl':'/private/a','json':{'flowcharts':[1]},'metadata':{'url':'/private/b','section_id':'s'}}))

    def test_directory_has_shared_unique_sources_and_no_body(self):
        body = '## Section <!-- kd-section id=section rag=body -->\nActual body facts.\n'
        ref = {'source_ref_id':'f-a@v-a','file_id':'f-a','version_id':'v-a','sha256':'a'*64}
        self.service.active_record = MagicMock(return_value=(
            {'body_md':body,'content_hash':hashlib.sha256(body.encode()).hexdigest()},
            {'json':{'rag_selection':{'source_refs':[ref,ref,ref]}}}))
        result = self.service.read('k-check',1,'hash')
        self.assertEqual('directory',result['mode'])
        self.assertEqual([ref],result['source_refs'])
        self.assertNotIn('body_md',result)
        self.assertNotIn('content',result['sections'][0])
        self.assertNotIn('text',result['sections'][0])
        self.assertNotIn('source_refs',result['sections'][0])
        detail = self.service.read('k-check',1,'hash',section_ids=['section'])
        self.assertEqual('sections',detail['mode'])
        self.assertIn('Actual body facts.',detail['sections'][0]['text'])


class VectorActivationTests(unittest.TestCase):
    def test_withdrawal_matches_expected_revision(self):
        rag=SandboxRAG.__new__(SandboxRAG)
        rag.prefix='kd_dev_fixture_';rag.mongo=MagicMock()
        rag.mongo.get_collection.return_value.update_one.return_value.matched_count=0
        self.assertFalse(rag.withdraw('方案与设计','k-check',revision=1)['withdrawn'])
        predicate=rag.mongo.get_collection.return_value.update_one.call_args.args[0]
        self.assertEqual('k-check@1',predicate['record_id'])
        self.assertTrue(predicate['active'])

    def test_missing_readback_does_not_activate(self):
        with tempfile.TemporaryDirectory() as folder:
            rag=SandboxRAG.__new__(SandboxRAG)
            rag.mode='fixture';rag.prefix='kd_dev_fixture_';rag.archive=Path(folder)
            records=MagicMock();heads=MagicMock()
            records.find_one.return_value=None;heads.find_one.return_value=None
            rag.mongo=MagicMock();rag.mongo.get_collection.side_effect=lambda name: heads if name.endswith('heads') else records
            vector_db=MagicMock();vector_db.query_by_ids.return_value=[]
            rag._db=MagicMock(return_value=vector_db)
            item={'meta':{'knowledge_id':'k-check','title':'check','knowledge_type':'项目知识','content_hash':'hash'},'stage':'方案与设计','revision':1,'fingerprint':'fingerprint','raw':b'md','sidecar_raw':b'{}',
                  'sidecar':{'sources':[]},'payload':{'retrieval_md':'text'},'chunks':[{'text':'text','offset':0,'section':{'id':'s','heading_path':['s'],'protected_asset_refs':[]}}]}
            with self.assertRaisesRegex(ValueError,'读回不完整'):
                rag._ingest(item,threading.Event())
            heads.insert_one.assert_not_called()
            heads.replace_one.assert_not_called()
            self.assertEqual('failed',records.update_one.call_args.args[1]['$set']['state'])

if __name__=='__main__':unittest.main()
