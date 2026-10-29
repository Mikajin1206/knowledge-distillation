import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tests._path import *
from langchain_core.messages import HumanMessage
from agent_foundation.model_adapter import (
    ModelAdapter, ModelConfig, ModelConfigurationError, ModelError, TaskCallBudget,
)


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        request = json.loads(raw)
        self.server.requests.append((self.path, request, self.headers.get('Authorization')))
        if self.server.mode == 'timeout':
            time.sleep(0.3)
        if self.server.mode == 'auth':
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"secret must not be echoed"}}')
            return
        if self.server.mode == 'tool':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"query":"stock"}'}}]}
            finish = 'tool_calls'
        else:
            message = {'role': 'assistant', 'content': '{"answer":"ok"}' if self.server.mode == 'json' else 'ok'}
            finish = 'stop'
        payload = json.dumps({'id': 'chatcmpl-local', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture-model-actual', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ModelAdapterTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests = []
        self.server.mode = 'text'
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def adapter(self, timeout=1):
        return ModelAdapter(ModelConfig(self.base, 'fixture-model', 'TEST_AGENT_KEY', timeout_seconds=timeout,
                                        max_output_tokens=256, disable_thinking=True),
                            environ={'TEST_AGENT_KEY': 'test-token'})

    async def test_actual_http_tool_call_and_task_association(self):
        self.server.mode = 'tool'
        tool = {'type': 'function', 'function': {'name': 'lookup', 'description': 'Lookup',
                'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}
        budget = TaskCallBudget('task-1', 1)
        result = await self.adapter().invoke([HumanMessage(content='Find stock')], budget, tools=[tool])
        self.assertEqual(result.task_id, 'task-1')
        self.assertEqual(result.message.tool_calls[0]['id'], 'call_1')
        self.assertEqual(result.message.tool_calls[0]['args'], {'query': 'stock'})
        self.assertEqual(result.message.response_metadata['model_name'], 'fixture-model-actual')
        path, request, authorization = self.server.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(authorization, 'Bearer test-token')
        self.assertEqual(request['thinking'], {'type': 'disabled'})
        self.assertEqual(request['tools'][0]['function']['name'], 'lookup')
        with self.assertRaises(ModelError) as error:
            await self.adapter().invoke([HumanMessage(content='again')], budget)
        self.assertEqual(error.exception.code, 'limit')
        self.assertEqual(len(self.server.requests), 1)

    async def test_json_result_and_invalid_json(self):
        self.server.mode = 'json'
        adapter = self.adapter()
        budget = TaskCallBudget('task-2', 2)
        result = await adapter.invoke([HumanMessage(content='JSON')], budget, json_output=True)
        self.assertEqual(result.json_object, {'answer': 'ok'})
        self.assertEqual(self.server.requests[0][1]['response_format'], {'type': 'json_object'})
        self.server.mode = 'text'
        with self.assertRaises(ModelError) as error:
            await adapter.invoke([HumanMessage(content='bad JSON')], budget, json_output=True)
        self.assertEqual(error.exception.code, 'invalid_response')

    async def test_auth_failure_and_timeout_classified_without_provider_message(self):
        self.server.mode = 'auth'
        with self.assertRaises(ModelError) as error:
            await self.adapter().invoke([HumanMessage(content='hello')], TaskCallBudget('auth', 1))
        self.assertEqual(error.exception.code, 'authentication')
        self.assertNotIn('secret', str(error.exception))
        self.server.mode = 'timeout'
        with self.assertRaises(ModelError) as error:
            await self.adapter(timeout=0.05).invoke([HumanMessage(content='hello')], TaskCallBudget('slow', 1))
        self.assertEqual(error.exception.code, 'timeout')

    async def test_configuration_and_concurrent_budget(self):
        with self.assertRaises(ModelConfigurationError):
            ModelAdapter(ModelConfig(self.base, 'model', 'MISSING_KEY'), environ={})
        with self.assertRaises(ValueError):
            ModelConfig('https://user:password@example.invalid', 'model', 'KEY')
        budget = TaskCallBudget('task-3', 1)
        results = await asyncio.gather(budget.reserve(), budget.reserve(), return_exceptions=True)
        self.assertEqual(budget.used, 1)
        self.assertEqual(sum(isinstance(x, ModelError) for x in results), 1)
