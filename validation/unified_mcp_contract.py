"""Protocol-only empty-start and single-server checks; no Word or RAG writes."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]

async def run():
    with TemporaryDirectory(dir=ROOT/'tmp',prefix='mcp-empty-') as directory:
        data=Path(directory)/'absent-data'
        env=dict(os.environ,CKP_DATA=str(data),CKP_OUTPUT=str(Path(directory)/'output'),CKP_LOCAL_ACTOR='local-operator',CKP_REVIEWER_IDS='local-operator')
        params=StdioServerParameters(command=str(ROOT/'ckp-mcp.sh'),cwd=str(ROOT),env=env)
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as client:
                initialized=await client.initialize()
                tools=(await client.list_tools()).tools
                assert len(tools)==31,len(tools)
                names={t.name for t in tools}
                assert {'metadata_catalog','submit_for_review','record_review','publish_knowledge','get_publish_task','search_knowledge','read_knowledge','get_asset','withdraw_knowledge','knowledge_status'}<=names
                async def call(name,arguments):
                    response=await client.call_tool(name,arguments)
                    assert not response.is_error,response
                    if response.structured_content is not None:
                        return response.structured_content.get('result', response.structured_content)
                    return json.loads(next(c.text for c in response.content if c.type=='text'))
                assert await call('project_list',{})==[]
                assert await call('file_list',{})==[]
                assert (await call('metadata_catalog',{'product':'致远 OA'}))['domains']==['办公协同']
                status=await call('knowledge_status',{})
                assert status['mongo_connected'] and status['single_mcp']
                assert status['knowledge']==[]
                assert not data.exists(),'readonly request created disk data'
                failure=await client.call_tool('upload_original',{'path':'/tmp/private-deploy-missing-input.docx','project':'/Users/private/deployment-project'})
                assert failure.is_error
                text=' '.join(getattr(c,'text','') for c in failure.content)
                assert '/Users/' not in text and '/tmp/' not in text,text
                report={'server':initialized.server_info.model_dump(),'tool_count':len(tools),'empty_lists':True,'readonly_no_disk_write':True,'error_path_redacted':True,'mongo_connected':True,'milvus_version':status['milvus_version'],'product_domain_catalog':True,'tool_names':sorted(names)}
                destination=ROOT/'tmp/workbuddy-acceptance-20261022/protocol-contract.json'
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_text(json.dumps(report,ensure_ascii=False,indent=2))
                print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(run())
