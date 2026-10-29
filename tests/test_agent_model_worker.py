import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tests._path import *
from tests.test_agent_model_adapter import ModelHandler
from agent_foundation.access import Grant, LocalAccess, Principal
from agent_foundation.features import Feature, Features
from agent_foundation.local_application import LocalApplication
from agent_foundation.model_adapter import ModelAdapter, ModelConfig
from agent_foundation.task_store import ConflictError

class ModelWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        cls.server.daemon_threads = True
        cls.server.mode = 'text'; cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    async def test_http_result_is_saved_and_budget_survives_restart(self):
        alice = Principal('org', 'alice')
        access = LocalAccess({'token': alice}, {Grant(alice, 'create', 'org', 'org')})
        features = Features([Feature('tasks', '1'), Feature('worker', '1', ('tasks',)),
                             Feature('model', '1', ('worker',))], {'tasks', 'worker', 'model'})
        adapter = ModelAdapter(ModelConfig(f'http://127.0.0.1:{self.server.server_port}/v1',
                                           'fixture', 'TEST_KEY', timeout_seconds=2), environ={'TEST_KEY': 'test'})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            path = Path(folder) / 'tasks.db'
            app = LocalApplication(path, access, features)
            task = app.create_task('token', 'create', 'customer', 'scene')['task_id']
            revision = app.add_revision('token', 'input', task, 'input', {'prompt': 'hello'}, 1)
            generic = app.enqueue_job('token', 'generic', task, 'input', 1, revision['content_hash'])
            model_job = app.enqueue_model_job('token', 'model', task, 'input', 1, revision['content_hash'])
            result = await app.run_model_worker_once(adapter, max_calls_per_task=1)
            self.assertEqual(model_job['job_id'], result['job_id'])
            saved = app.get_revision('token', task, 'result', 1)['payload']
            self.assertEqual(('ok', 'fixture'), (saved['text'], saved['model']))
            self.assertEqual(generic['job_id'], app.run_worker_once(lambda source: {'done': True})['job_id'])
            app.enqueue_model_job('token', 'model-2', task, 'input', 1, revision['content_hash'])
            app2 = LocalApplication(path, access, features)
            with self.assertRaisesRegex(ConflictError, 'limit'):
                await app2.run_model_worker_once(adapter, max_calls_per_task=1)
            with sqlite3.connect(path) as db:
                rows = db.execute('SELECT model,outcome FROM model_calls').fetchall()
            self.assertEqual([('fixture', 'success')], rows)

    async def test_revoked_read_prevents_result_commit(self):
        alice = Principal('org', 'alice')
        access = LocalAccess({'token': alice}, {Grant(alice, 'create', 'org', 'org')})
        features = Features([Feature('tasks', '1'), Feature('model', '1', ('tasks',))], {'tasks', 'model'})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            app = LocalApplication(Path(folder) / 'tasks.db', access, features)
            task = app.create_task('token', 'create', 'customer', 'scene')['task_id']
            rev = app.add_revision('token', 'input', task, 'input', {'prompt': 'hello'}, 1)
            app.enqueue_model_job('token', 'model', task, 'input', 1, rev['content_hash'])
            from types import SimpleNamespace
            from langchain_core.messages import AIMessage
            class Adapter:
                config = SimpleNamespace(model='fixture')
                async def invoke(self, messages, budget):
                    access.revoke_principal(alice)
                    return SimpleNamespace(message=AIMessage(content='result'), configured_model='fixture')
            with self.assertRaises(PermissionError): await app.run_model_worker_once(Adapter())
            with sqlite3.connect(app.database) as db:
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM revisions WHERE kind='result'").fetchone()[0])
                self.assertEqual(0, db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
