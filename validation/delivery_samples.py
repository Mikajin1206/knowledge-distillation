"""Validate/import supplied export samples locally, without external model calls."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from chunks_and_in_vector.pipeline import prepare, SandboxRAG

service = SandboxRAG(mode='fixture')
results = []
try:
    for path in sorted(Path('/workspace/delivery/extracted/knowledge-distillation/docs').glob('*.md')):
        try:
            prepared = prepare(path)
        except ValueError as exc:
            results.append({'file':path.name,'status':'rejected','reason':str(exc)})
            continue
        result = service.ingest(path)
        record = service.read(prepared['meta']['knowledge_type'],prepared['meta']['knowledge_id'])
        assert record and record['json']['content_hash']==prepared['meta']['content_hash']
        assert record['content']==prepared['payload']['retrieval_md']
        hits = service.search(prepared['chunks'][0]['text'], prepared['meta']['knowledge_type'],20)
        assert any(h['metadata']['knowledge_id']==prepared['meta']['knowledge_id'] for h in hits)
        results.append({'file':path.name,'status':'passed','chunks':result['chunks'],
                        'knowledge_id':prepared['meta']['knowledge_id']})
finally:
    service.close()
summary={'embedding_mode':'fixture','samples':results,
         'passed':sum(r['status']=='passed' for r in results),
         'rejected':sum(r['status']=='rejected' for r in results)}
(ROOT/'.cloud'/'delivery-samples.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
