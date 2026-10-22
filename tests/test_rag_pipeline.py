"""Boundary tests for the actual ingestion parser; no database/model required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from tests._path import *
from ckp.md_render import render_frontmatter, extract_rag_payload
from chunks_and_in_vector.pipeline import prepare

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
        meta = {'knowledge_id':'k-fixture', 'revision':1, 'title':'Test',
                'knowledge_type':'方案设计', 'status':'draft', 'project_id':'p-fixture',
                'content_hash':hashlib.sha256(body.encode()).hexdigest(), **changes}
        text = render_frontmatter(meta) + body
        refs = [{'file_id':'f-test', 'version_id':'v-test', 'sha256':'a'*64}]
        source_refs = [{'source_ref_id':'f-test@v-test', **refs[0]}]
        payload = extract_rag_payload(text, [], source_refs)
        payload['source_refs'] = source_refs
        sidecar = {**meta, 'sources':refs, 'rag_selection':payload}
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

    def test_unknown_type_rejected(self):
        self.write(knowledge_type='unconfigured')
        with self.assertRaisesRegex(ValueError, '集合映射'): prepare(self.path)

    def test_no_indexable_content_rejected(self):
        self.write('## Excluded <!-- kd-section id=only rag=exclude -->\nnot searchable')
        with self.assertRaisesRegex(ValueError, 'body'): prepare(self.path)

if __name__ == '__main__': unittest.main()
