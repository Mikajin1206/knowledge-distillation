import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests._path import *
from agent_foundation.task_store import TaskStore, ConflictError
from agent_foundation.worker import Worker

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='tmp')
        self.addCleanup(self.tmp.cleanup)
        self.allow = {'seller': True, 'worker': True}
        self.path = Path(self.tmp.name) / 'worker.db'
        self.store = TaskStore(self.path, lambda actor, task, action: self.allow.get(actor, False))
        self.task = self.store.create_task('seller', 'create', 'org', 'customer', 'scene')['task_id']
        self.rev = self.store.add_revision('seller', 'input', self.task, 'input', {'value': 2}, 1)

    def enqueue(self):
        return self.store.enqueue_job('seller', 'job-request', self.task, 'input', 1, self.rev['content_hash'])

    def test_restart_idempotency_and_atomic_result(self):
        first = self.enqueue()
        self.assertEqual(first, self.enqueue())
        reopened = TaskStore(self.path, lambda actor, task, action: self.allow.get(actor, False))
        result = Worker(reopened, 'worker', lambda data: {'value': data['value'] * 2}).run_once()
        self.assertEqual(first['job_id'], result['job_id'])
        self.assertEqual({'value': 4}, reopened.get_revision('seller', self.task, 'result', 1)['payload'])
        self.assertIsNone(Worker(reopened, 'worker', lambda data: data).run_once())
        event = reopened.claim_outbox('worker')
        self.assertEqual('job.completed', event['event_type'])
        self.assertEqual(result['job_id'], event['payload']['job_id'])

    def test_lost_lease_cannot_save_and_recovery_does_not_duplicate_result(self):
        self.enqueue()
        with patch('agent_foundation.task_store.time.time', return_value=100):
            old = self.store.claim_job('worker', 1)
        with patch('agent_foundation.task_store.time.time', return_value=102):
            new = self.store.claim_job('worker', 5)
            with self.assertRaises(ConflictError):
                self.store.finish_job('worker', old['job_id'], old['lease_token'], {'v': 1})
            self.store.finish_job('worker', new['job_id'], new['lease_token'], {'v': 2})
            with self.assertRaises(ConflictError):
                self.store.finish_job('worker', new['job_id'], new['lease_token'], {'v': 2})
        self.assertEqual({'v': 2}, self.store.get_revision('seller', self.task, 'result', 1)['payload'])

    def test_revoked_or_changed_input_never_produces_result(self):
        self.enqueue()
        self.allow['seller'] = False
        with self.assertRaises(PermissionError): Worker(self.store, 'worker', lambda x: x).run_once()
        self.allow['seller'] = True
        self.assertIsNone(self.store.claim_job('worker'))
        self.store.enqueue_job('seller', 'job-2', self.task, 'input', 1, self.rev['content_hash'])
        self.store.add_revision('seller', 'input-2', self.task, 'input', {'value': 3}, 2)
        with self.assertRaises(ConflictError): Worker(self.store, 'worker', lambda x: x).run_once()
        self.assertIsNone(self.store.claim_job('worker'))

    def test_transient_handler_failure_releases_job(self):
        self.enqueue()
        def fail(value): raise RuntimeError('temporary failure')
        with self.assertRaises(RuntimeError): Worker(self.store, 'worker', fail).run_once()
        self.assertEqual(1, Worker(self.store, 'worker', lambda value: value).run_once()['result_revision'])

    def test_exhausted_attempts_do_not_execute_again(self):
        self.enqueue()
        calls = []
        def fail(value):
            calls.append(value)
            raise RuntimeError('offline')
        worker = Worker(self.store, 'worker', fail)
        for _ in range(3):
            with self.assertRaises(RuntimeError): worker.run_once()
        self.assertIsNone(worker.run_once())
        self.assertEqual(3, len(calls))

    def test_stale_input_does_not_run_handler(self):
        self.enqueue()
        self.store.add_revision('seller', 'new-input', self.task, 'input', {'value': 3}, 2)
        calls = []
        with self.assertRaises(ConflictError):
            Worker(self.store, 'worker', lambda value: calls.append(value)).run_once()
        self.assertEqual([], calls)

    def test_read_revocation_during_execution_cannot_commit(self):
        self.enqueue()
        previous = self.store.authorize
        revoked = {'read': False}
        self.store.authorize = lambda actor, task, action: previous(actor, task, action) and not (actor == 'seller' and action == 'read' and revoked['read'])
        def handler(value):
            revoked['read'] = True
            return value
        with self.assertRaises(PermissionError): Worker(self.store, 'worker', handler).run_once()
        self.assertIsNone(self.store.claim_outbox('worker'))
        self.assertIsNone(self.store.claim_job('worker'))
