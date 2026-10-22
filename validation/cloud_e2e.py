"""Real MongoDB/Milvus + two stdio MCP servers; synthetic Word/model draft.
No external model call, OCR, or company file-server call is made.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from chunks_and_in_vector.pipeline import SandboxRAG, prepare

BODY = '''# 开发验证知识

## 采购审批 <!-- kd-section id=approval rag=body -->
已验证：采购审批必须由负责人复核。
### 排除材料 <!-- kd-section id=excluded_child rag=exclude -->
EXCLUDED_CHILD_SENTINEL
#### 例外规则 <!-- kd-section id=exception rag=body -->
待确认：紧急采购的审批时限，材料未提供。
## 排除附录 <!-- kd-section id=excluded_appendix rag=exclude -->
EXCLUDED_APPENDIX_SENTINEL
'''

async def run():
    folder = ROOT / '.cloud' / 'validation'
    folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CKP_DATA=str(folder / 'data'), CKP_OUTPUT=str(folder / 'output'),
               KD_EMBEDDING_MODE='fixture', KD_ARCHIVE=str(folder / 'archive'))
    source = folder / 'synthetic.docx'
    with zipfile.ZipFile(source, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>采购审批必须由负责人复核。紧急采购审批时限材料未提供。</w:t></w:r></w:p></w:body></w:document>')
    params = StdioServerParameters(command=str(ROOT / 'ckp-mcp.sh'), cwd=str(ROOT), env=env)
    checks = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as ckp:
            await ckp.initialize()
            tools = await ckp.list_tools()
            assert len(tools.tools) == 21
            checks.append('latest CKP MCP: 21 tools')
            async def call(client, tool_name, **args):
                response = await client.call_tool(tool_name, args)
                assert not response.is_error, (tool_name, response)
                if response.structured_content is not None:
                    return response.structured_content.get('result', response.structured_content)
                return json.loads(next(c.text for c in response.content if c.type == 'text'))
            from uuid import uuid4
            project = 'Synthetic validation ' + uuid4().hex[:8]
            await call(ckp, 'project_create', name=project)
            uploaded = await call(ckp, 'upload_original', path=str(source), project=project)
            stages = await call(ckp, 'distill_stages')
            task = await call(ckp, 'distill_start', project=project, stage=stages['stages'][0])
            tid = task['task_id']; fid = uploaded['uploaded'][0]['file_id']
            await call(ckp, 'distill_attach', task_id=tid, file_id=fid)
            await call(ckp, 'distill_guidance', task_id=tid)
            content = await call(ckp, 'distill_word_content', task_id=tid, file_id=fid)
            assert not content['has_more']
            await call(ckp, 'distill_quality_report_template', task_id=tid)
            async def draft(body, kid='', kind='方案设计'):
                d = await call(ckp, 'draft_submit', task_id=tid, title='Synthetic approval knowledge',
                               knowledge_type=kind, body_md=body, knowledge_id=kid,
                               model='synthetic-model-fixture')
                return d, await call(ckp, 'draft_export', knowledge_id=d['knowledge_id'])
            d1, e1 = await draft(BODY)
            assert e1['verify']['match']
            checks.append('Word upload/read -> synthetic model draft -> MD/JSON/hash')
            kid = d1['knowledge_id']
            # Separate consolidated RAG MCP runs against actual local databases.
            rag_params = StdioServerParameters(command=str(ROOT / 'rag-mcp.sh'), cwd=str(ROOT), env=env)
            async with stdio_client(rag_params) as (rr, rw):
                async with ClientSession(rr, rw) as rag_client:
                    await rag_client.initialize()
                    assert [t.name for t in (await rag_client.list_tools()).tools] == ['rag_knowledge']
                    result = await call(rag_client, 'rag_knowledge', action='ingest', path=e1['md'])
                    assert result['chunks'] == 2
                    repeat = await call(rag_client, 'rag_knowledge', action='ingest', path=e1['md'])
                    assert repeat['state'] == 'unchanged'
                    found = await call(rag_client, 'rag_knowledge', action='search', query='采购审批必须由负责人复核', knowledge_type='方案设计')
                    assert found['results']
                    record = await call(rag_client, 'rag_knowledge', action='read', knowledge_type='方案设计', knowledge_id=kid)
                    text = record['document']['content']
                    assert 'EXCLUDED_' not in text and '待确认' in text and '材料未提供' in text
                    assert record['document']['json']['sources'][0]['sha256'] == uploaded['uploaded'][0]['sha256']
                    assert all(hit['metadata']['sources'] and hit['metadata']['section_id'] for hit in found['results'])
                    checks += ['RAG MCP ingest/search/read against real MongoDB 4.0.28 and Milvus 2.6.24',
                               'body-only chunks; excluded child/appendix absent; uncertainty/source versions preserved',
                               'idempotent repeat ingestion']
            # Exercise a partial vector failure and retry against actual databases.
            os.environ['KD_ARCHIVE'] = str(folder / 'archive')
            rag = SandboxRAG(mode='fixture')
            try:
                d2, e2 = await draft(BODY.replace('必须由负责人复核', '必须由两位负责人复核'), kid)
                col = rag._names('方案设计'); db = rag._db(col)
                real_insert = db.insert
                def partial_failure(documents, ids=None):
                    real_insert(documents[:1], ids=ids[:1])
                    raise TimeoutError('synthetic timeout after partial vector write')
                with patch.object(db, 'insert', side_effect=partial_failure):
                    try:
                        rag.ingest(e2['md'])
                        raise AssertionError('failure expected')
                    except TimeoutError:
                        pass
                assert rag.read('方案设计', kid)['revision'] == 1
                assert all(h['metadata']['revision'] == 1 for h in rag.search('采购审批', '方案设计', 20) if h['metadata']['knowledge_id'] == kid)
                checks.append('partial vector write invisible; old revision remains readable/searchable')
                assert rag.ingest(e2['md'])['revision'] == 2
                assert all(h['metadata']['revision'] == 2 for h in rag.search('采购审批', '方案设计', 20) if h['metadata']['knowledge_id'] == kid)
                assert rag.read('方案设计', kid, 1)['revision'] == 1
                checks.append('retry repairs stable IDs; revision switch and historical read')
                try:
                    rag.ingest(e1['md'])
                    raise AssertionError('downgrade expected to fail')
                except ValueError:
                    pass
                with rag._lease(kid):
                    try:
                        rag.ingest(e2['md'])
                        raise AssertionError('concurrent import should be rejected')
                    except ValueError as exc:
                        assert '正在入库' in str(exc)
                checks.append('cross-process MongoDB lease rejects concurrent same-knowledge ingestion')
                d3, e3 = await draft(BODY, kid, '概要设计')
                rag.ingest(e3['md'])
                assert rag.read('方案设计', kid) is None
                assert rag.read('概要设计', kid)['revision'] == 3
                assert not [h for h in rag.search('采购审批', '方案设计', 20)
                            if h['metadata']['knowledge_id'] == kid]
                checks.append('knowledge-type change atomically replaces old collection visibility')
                rag.withdraw('概要设计', kid)
                assert rag.read('概要设计', kid) is None
                assert not [h for h in rag.search('采购审批', '概要设计') if h['metadata']['knowledge_id'] == kid]
                checks.append('old revision rejected; withdrawal removes default read/search')
                # Keep one independent synthetic sample available for user exploration.
                demo, demo_export = await draft(BODY)
                rag.ingest(demo_export['md'])
                summary = {'status': 'passed', 'checks': checks, 'demo_knowledge_id': demo['knowledge_id'],
                           'demo_md': demo_export['md'], 'embedding_mode': 'fixture',
                           'not_verified': ['real Qwen embeddings', 'real DeepSeek generation',
                                            'company file server', 'human multimodal Word/diagram distillation',
                                            'formal approval/publication and production authorization']}
                folder.joinpath('result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            finally:
                rag.close()
            await call(ckp, 'distill_close', task_id=tid)
            assert source.is_file(), 'original must remain'

if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(run(), timeout=180))
