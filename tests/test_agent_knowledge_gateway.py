import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests._path import *
from agent_foundation.knowledge_gateway import EvidenceRef, KnowledgeError, KnowledgeGateway


def response(payload, error=False):
    return SimpleNamespace(isError=error, structuredContent=payload)


def project_hit(identifier='k-1', revision=2, digest='hash'):
    return {'text': 'evidence', 'score': .5, 'metadata': {
        'knowledge_id': identifier, 'revision': revision, 'content_hash': digest,
        'section_id': 's1', 'heading_path': ['heading'], 'sources': ['source'],
        'protected_asset_refs': ['asset']}}


def product_hit():
    return {'text': 'product evidence', 'material_id': 'm-1', 'revision': 3,
            'source_sha256': 'source-hash', 'stage': '', 'metadata': {
                'section_id': 'intro', 'material_id': 'm-1', 'revision': 3,
                'source_sha256': 'source-hash', 'heading_path': ['manual']}}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = SimpleNamespace(call_tool=AsyncMock(return_value=response({'results': [project_hit()]})))
        self.authorize = AsyncMock(return_value=True)
        self.trace = AsyncMock()
        self.gateway = KnowledgeGateway(self.session, self.authorize, self.trace)

    async def test_search_routes_and_filters_per_document(self):
        self.session.call_tool.return_value = response({'results': [project_hit(), project_hit('k-private')], 'scope':'local_development',
                                                       'unexpected': 'must not leak'})
        async def check(actor, corpus, ref): return ref is None or ref.identifier != 'k-private'
        self.authorize.side_effect = check
        result = await self.gateway.search('sales', 'project', '采购', trace_id='trace1')
        self.assertEqual([h['metadata']['knowledge_id'] for h in result['results']], ['k-1'])
        self.assertNotIn('unexpected', result)
        self.session.call_tool.assert_awaited_once_with('search_knowledge', {'query': '采购', 'limit': 5})
        self.assertTrue(any(c.args[0]['outcome'] == 'denied' for c in self.trace.await_args_list))
        self.assertTrue(all(c.args[0]['trace_id'] == 'trace1' for c in self.trace.await_args_list))

    async def test_product_evidence_preserves_source_and_version(self):
        self.session.call_tool.return_value = response({'results': [product_hit()]})
        item = (await self.gateway.search_evidence('sales', 'product', '采购'))[0]
        self.assertEqual((item['identifier'], item['revision'], item['digest']), ('m-1', 3, 'source-hash'))
        self.assertEqual(item['heading_path'], ['manual'])
        self.assertIsNone(item['fact_status'])
        self.session.call_tool.assert_awaited_once_with('search_product_materials', {'query': '采购', 'limit': 5})

    async def test_read_uses_exact_identity_and_deduplicates_sections(self):
        self.session.call_tool.return_value = response({'knowledge_id':'k-1','revision':2,'content_hash':'hash', 'sections':[{'id':'s1'},{'id':'s2'}]})
        ref = EvidenceRef('project', 'k-1', 2, 'hash')
        await self.gateway.read_sections('sales', ref, ['s1', 's1', 's2'])
        self.session.call_tool.assert_awaited_once_with('read_knowledge', {
            'knowledge_id': 'k-1', 'revision': 2, 'content_hash': 'hash',
            'section_ids': ['s1', 's2'], 'full_document': False})
        self.authorize.assert_awaited_once_with('sales', 'project', ref)

    async def test_denial_tool_error_and_invalid_response_fail_closed(self):
        self.authorize.return_value = False
        with self.assertRaises(PermissionError): await self.gateway.search('sales', 'project', '采购')
        self.session.call_tool.assert_not_awaited()
        self.authorize.return_value = True
        self.session.call_tool.return_value = response({}, True)
        with self.assertRaisesRegex(KnowledgeError, 'tool_error'): await self.gateway.search('sales', 'project', '采购')
        self.session.call_tool.return_value = response({'results': [{'text':'missing identity'}]})
        with self.assertRaisesRegex(KnowledgeError, 'invalid_response'): await self.gateway.search('sales', 'project', '采购')
        self.session.call_tool.return_value = response({'results': [{**product_hit(), 'revision':4}]})
        with self.assertRaisesRegex(KnowledgeError, 'identity_mismatch'): await self.gateway.search('sales', 'product', '采购')

    async def test_product_read_requires_matching_version_and_sections(self):
        with self.assertRaises(ValueError): EvidenceRef('product', 'm-1', 0, 'hash')
        ref = EvidenceRef('product', 'm-1', 3, 'source-hash')
        with self.assertRaises(ValueError): await self.gateway.read_sections('sales', ref, [])
        self.session.call_tool.return_value = response({'material_id':'m-1', 'revision':4, 'source_sha256':'source-hash'})
        with self.assertRaisesRegex(KnowledgeError, 'identity_mismatch'):
            await self.gateway.read_sections('sales', ref, ['intro'])
        self.session.call_tool.assert_awaited_once_with('read_product_material', {
            'material_id': 'm-1', 'revision': 3, 'source_sha256': 'source-hash',
            'section_ids': ['intro'], 'full_document': False})

    async def test_full_read_and_asset_are_explicit(self):
        ref = EvidenceRef('project', 'k-1', 2, 'hash')
        with self.assertRaises(ValueError): await self.gateway.read_full_document('sales', ref, reason='')
        self.session.call_tool.return_value = response({'knowledge_id':'k-1','revision':2,'content_hash':'hash'})
        await self.gateway.read_full_document('sales', ref, reason='跨章节核对')
        self.session.call_tool.assert_awaited_with('read_knowledge', {'knowledge_id':'k-1','revision':2,
                                              'content_hash':'hash', 'full_document':True})
        self.session.call_tool.return_value = response({'asset': {'knowledge_id':'k-1','revision':2,'asset_id':'a'},
                                                        'complete': True, 'payload': {}})
        await self.gateway.read_asset('sales', ref, 's1', 'a')
        self.session.call_tool.assert_awaited_with('get_asset', {'knowledge_id':'k-1','revision':2,
                                         'content_hash':'hash','section_id':'s1','asset_id':'a'})

    async def test_transport_error_and_limit(self):
        with self.assertRaises(ValueError): await self.gateway.search('sales', 'project', '采购', limit=21)
        self.session.call_tool.side_effect = TimeoutError('private endpoint')
        with self.assertRaisesRegex(KnowledgeError, 'transport_error') as error:
            await self.gateway.search('sales', 'project', '采购')
        self.assertNotIn('private endpoint', str(error.exception))

    async def test_image_requires_exact_identity_and_verified_hash(self):
        import json
        from mcp.types import CallToolResult, ImageContent, TextContent
        ref = EvidenceRef('product', 'm-1', 3, 'source-hash')
        context = {'material_id':'m-1', 'revision':3, 'source_sha256':'source-hash',
                   'figure_id':'图1', 'mime_type':'image/png', 'hash_verified':True}
        self.session.call_tool.return_value = CallToolResult(content=[
            ImageContent(type='image', data='aW1hZ2U=', mimeType='image/png'),
            TextContent(type='text', text=json.dumps(context))])
        image = await self.gateway.read_product_image('sales', ref, '图1')
        self.assertEqual(image['mime_type'], 'image/png')
        self.assertEqual(image['data_base64'], 'aW1hZ2U=')
        context['hash_verified'] = False
        self.session.call_tool.return_value = CallToolResult(content=[
            ImageContent(type='image', data='aW1hZ2U=', mimeType='image/png'),
            TextContent(type='text', text=json.dumps(context))])
        with self.assertRaisesRegex(KnowledgeError, 'invalid_response'):
            await self.gateway.read_product_image('sales', ref, '图1')


class StdioProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_stdio_search_and_exact_revision_read(self):
        import sys
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        server_code = '''
from mcp.server.mcpserver import MCPServer, Image
from mcp.server.mcpserver.exceptions import ToolError
import json
s = MCPServer('fixture')
@s.tool()
def search_knowledge(query: str, limit: int=5) -> dict:
    return {'scope':'local_development', 'results':[{'text':'actual protocol evidence', 'metadata': {
        'knowledge_id':'k-1', 'revision':2, 'content_hash':'hash', 'section_id':'s1',
        'heading_path':['H'], 'sources':[{'file_id':'f'}], 'protected_asset_refs':[]}}]}
@s.tool()
def read_knowledge(knowledge_id: str, revision: int, content_hash: str,
                   section_ids: list[str] | None=None, full_document: bool=False) -> dict:
    if revision != 2 or content_hash != 'hash': raise ToolError('invalid version')
    return {'knowledge_id':knowledge_id,'revision':revision,'content_hash':content_hash,
            'sections':[{'id':'s1','text':'full section evidence'}]}
@s.tool(structured_output=False)
def read_product_material_image(material_id: str, revision: int, source_sha256: str, figure_id: str) -> list:
    return [Image(data=b'image', format='png'), json.dumps({'material_id':material_id,
            'revision':revision, 'source_sha256':source_sha256, 'figure_id':figure_id,
            'mime_type':'image/png', 'hash_verified':True})]
s.run(transport='stdio')
'''
        params = StdioServerParameters(command=sys.executable, args=['-c', server_code])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def authorized(actor, corpus, ref): return True
                gateway = KnowledgeGateway(session, authorized)
                hits = await gateway.search_evidence('user', 'project', 'query')
                self.assertEqual(hits[0]['text'], 'actual protocol evidence')
                ref = EvidenceRef('project', hits[0]['identifier'], hits[0]['revision'], hits[0]['digest'])
                result = await gateway.read_sections('user', ref, ['s1'])
                self.assertEqual(result['sections'][0]['text'], 'full section evidence')
                with self.assertRaisesRegex(KnowledgeError, 'tool_error'):
                    await gateway.read_sections('user', EvidenceRef('project', 'k-1', 3, 'bad'), ['s1'])
                image = await gateway.read_product_image('user', EvidenceRef('product','m-1',1,'sha'), '图1')
                self.assertEqual(image['data_base64'], 'aW1hZ2U=')
