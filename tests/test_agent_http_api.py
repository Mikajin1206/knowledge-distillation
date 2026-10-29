import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from tests._path import *
from agent_foundation.access import Grant, LocalAccess, Principal
from agent_foundation.features import Feature, Features
from agent_foundation.http_api import create_http_app
from agent_foundation.local_application import LocalApplication

class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir='tmp'); self.addCleanup(self.folder.cleanup)
        alice, bob = Principal('org', 'alice'), Principal('org', 'bob')
        self.access = LocalAccess({'alice-secret': alice, 'bob-secret': bob}, {Grant(alice, 'create', 'org', 'org')})
        flags = Features([Feature('tasks','1'), Feature('confirmations','1',('tasks',)), Feature('worker','1',('tasks',)), Feature('model','1',('worker',))],
                         {'tasks','confirmations','worker','model'})
        self.service = LocalApplication(Path(self.folder.name) / 'tasks.db', self.access, flags)
        self.client = TestClient(create_http_app(self.service))
        self.addCleanup(self.client.close)
        self.auth = {'Authorization': 'Bearer alice-secret'}

    def request(self, path, body, key, auth=None):
        return self.client.post(path, json=body, headers={**(auth or self.auth), 'Idempotency-Key': key})

    def test_authenticated_versioned_workflow_and_conflicts(self):
        body = {'customer_id': 'c1', 'scenario': 'research'}
        first = self.request('/tasks', body, 'create')
        self.assertEqual(201, first.status_code)
        self.assertEqual(first.json(), self.request('/tasks', body, 'create').json())
        self.assertEqual(409, self.request('/tasks', {**body, 'scenario':'other'}, 'create').status_code)
        task = first.json()['task_id']
        self.assertEqual(401, self.client.get(f'/tasks/{task}').status_code)
        self.assertEqual(401, self.client.get(f'/tasks/{task}', headers={'Authorization':'Bearer invalid'}).status_code)
        self.assertEqual(403, self.client.get(f'/tasks/{task}', headers={'Authorization':'Bearer bob-secret'}).status_code)
        revision = self.request(f'/tasks/{task}/revisions', {'kind':'input','payload':{'prompt':'hello'},'expected_version':1}, 'rev')
        self.assertEqual(201, revision.status_code)
        self.assertEqual(409, self.request(f'/tasks/{task}/revisions', {'kind':'input','payload':{},'expected_version':1}, 'rev2').status_code)
        rev = revision.json()
        ticket = self.request(f'/tasks/{task}/confirmations', {'kind':'input','revision':1,
            'content_hash':rev['content_hash'],'confirmer_employee_id':'alice'}, 'ticket')
        self.assertEqual(201, ticket.status_code)
        confirmation = ticket.json()['confirmation_id']
        decision = self.request(f'/confirmations/{confirmation}/decisions', {'decision':'accepted'}, 'decision')
        self.assertEqual('accepted', decision.json()['decision'])
        self.assertEqual(decision.json(), self.request(f'/confirmations/{confirmation}/decisions', {'decision':'accepted'}, 'decision').json())
        job = self.request(f'/tasks/{task}/model-jobs', {'kind':'input','revision':1,'content_hash':rev['content_hash']}, 'job')
        self.assertEqual(202, job.status_code)
        job_id = job.json()['job_id']
        self.assertEqual('pending', self.client.get(f'/jobs/{job_id}', headers=self.auth).json()['status'])
        self.assertEqual(403, self.client.get(f'/jobs/{job_id}', headers={'Authorization':'Bearer bob-secret'}).status_code)
        self.assertNotIn('lease_token', self.client.get(f'/jobs/{job_id}', headers=self.auth).json())

    def test_input_cannot_spoof_actor_or_configuration(self):
        self.assertEqual(422, self.request('/tasks', {'customer_id':'c1','scenario':'x','employee_id':'bob'}, 'key').status_code)
        self.assertEqual(422, self.client.post('/tasks', json={'customer_id':'c1','scenario':'x'}, headers=self.auth).status_code)
        self.assertEqual(404, self.client.get('/openapi.json', headers=self.auth).status_code)
