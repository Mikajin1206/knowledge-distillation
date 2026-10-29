import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tests._path import *
from agent_foundation.access import Grant, Principal
from agent_foundation.http_api import create_http_app
from agent_foundation.local_application import _actor
from agent_foundation.local_runtime import build_local_application


SERVER = '''
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
s = MCPServer('fixture')
@s.tool()
def search_knowledge(query: str, limit: int=5) -> dict:
    def hit(key):
        return {'text':'evidence-'+key,'metadata':{'knowledge_id':key,'revision':1,
          'content_hash':'hash-'+key,'section_id':'s1','heading_path':['H']}}
    return {'scope':'local_development','results':[hit('k-allowed'),hit('k-private')]}
@s.tool()
def search_product_materials(query: str, limit: int=5) -> dict:
    return {'results':[{'text':'product','material_id':'m-allowed','revision':1,'source_sha256':'mh',
                         'metadata':{'section_id':'s1'}}]}
@s.tool()
def read_knowledge(knowledge_id: str, revision: int, content_hash: str,
                   section_ids: list[str] | None=None, full_document: bool=False) -> dict:
    if knowledge_id != 'k-allowed' or revision != 1 or content_hash != 'hash-k-allowed':
        raise ToolError('incorrect version')
    return {'knowledge_id':knowledge_id,'revision':revision,'content_hash':content_hash,
            'sections':[{'id':x,'text':'detail'} for x in section_ids]}
@s.tool()
def read_product_material(material_id: str, revision: int, source_sha256: str,
                          section_ids: list[str] | None=None, full_document: bool=False) -> dict:
    if material_id != 'm-allowed' or revision != 1 or source_sha256 != 'mh':
        raise ToolError('incorrect version')
    return {'material_id':material_id,'revision':revision,'source_sha256':source_sha256,
            'sections':[{'section_id':x,'text':'product detail'} for x in section_ids]}
s.run(transport='stdio')
'''


@asynccontextmanager
async def fixture_session():
    async with stdio_client(StdioServerParameters(command=sys.executable, args=['-c', SERVER])) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


class KnowledgeHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir='tmp'); self.addCleanup(self.folder.cleanup)
        self.env = {'AGENT_LOCAL_ORG':'org','AGENT_LOCAL_EMPLOYEE':'alice','AGENT_LOCAL_TOKEN':'local-token',
                    'AGENT_LOCAL_DB':str(Path(self.folder.name)/'tasks.db'), 'AGENT_LOCAL_KNOWLEDGE_ENABLED':'1',
                    'AGENT_LOCAL_PROJECT_KNOWLEDGE_IDS':'k-allowed', 'AGENT_LOCAL_PRODUCT_MATERIAL_IDS':'m-allowed'}
        self.service = build_local_application(self.env)
        self.service.knowledge_service.session_factory = fixture_session
        self.client = TestClient(create_http_app(self.service)); self.addCleanup(self.client.close)
        self.auth = {'Authorization':'Bearer local-token'}
        response = self.client.post('/tasks', json={'customer_id':'c1','scenario':'s'},
                                    headers={**self.auth,'Idempotency-Key':'create'})
        self.task = response.json()['task_id']
        self.base = f'/tasks/{self.task}/knowledge'

    def test_project_product_search_read_and_trace_with_real_stdio_protocol(self):
        search = self.client.post(self.base+'/search', json={'corpus':'project','query':'采购'}, headers=self.auth)
        self.assertEqual(200, search.status_code, search.text)
        data = search.json()
        self.assertEqual(['k-allowed'], [x['identifier'] for x in data['results']])
        self.assertNotIn('k-private', search.text)
        trace = self.service.store.get_knowledge_trace(_actor(Principal('org','alice')), self.task, data['trace_id'])
        self.assertTrue(any(x['outcome'] == 'allowed' for x in trace))
        self.assertTrue(any(x['outcome'] == 'denied' and x['identifier'] is None for x in trace))
        body = {'corpus':'project','identifier':'k-allowed','revision':1,'digest':'hash-k-allowed','section_ids':['s1']}
        response = self.client.post(self.base+'/sections', json=body, headers=self.auth)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('detail', response.json()['result']['sections'][0]['text'])
        product = self.client.post(self.base+'/search', json={'corpus':'product','query':'manual'}, headers=self.auth)
        self.assertEqual('m-allowed', product.json()['results'][0]['identifier'])
        body = {'corpus':'product','identifier':'m-allowed','revision':1,'digest':'mh','section_ids':['s1']}
        self.assertEqual('product detail', self.client.post(self.base+'/sections', json=body, headers=self.auth).json()['result']['sections'][0]['text'])
        body['digest'] = 'wrong'
        self.assertEqual(502, self.client.post(self.base+'/sections', json=body, headers=self.auth).status_code)

    def test_permissions_and_user_supplied_command_are_rejected(self):
        self.assertEqual(401, self.client.post(self.base+'/search', json={'corpus':'project','query':'q'}).status_code)
        self.assertEqual(403, self.client.post(self.base+'/sections', json={'corpus':'project','identifier':'k-private',
            'revision':1,'digest':'hash-k-private','section_ids':['s1']}, headers=self.auth).status_code)
        self.assertEqual(422, self.client.post(self.base+'/search', json={'corpus':'project','query':'q',
            'command':'sh'}, headers=self.auth).status_code)
        self.service.access.replace_grants({Grant(Principal('org','alice'),'create','org','org')})
        self.assertEqual(403, self.client.post(self.base+'/search', json={'corpus':'project','query':'q'}, headers=self.auth).status_code)
        self.service.access.revoke_principal(Principal('org','alice'))
        self.assertEqual(401, self.client.post(self.base+'/search', json={'corpus':'project','query':'q'}, headers=self.auth).status_code)

    def test_feature_disabled_by_default(self):
        env = dict(self.env); env['AGENT_LOCAL_KNOWLEDGE_ENABLED'] = '0'
        app = build_local_application(env)
        client = TestClient(create_http_app(app)); self.addCleanup(client.close)
        response = client.post(self.base+'/search', json={'corpus':'project','query':'q'}, headers=self.auth)
        self.assertEqual(503, response.status_code)
