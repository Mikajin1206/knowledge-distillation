"""Local transactional behavior, not PostgreSQL or production authentication."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from tests._path import *
from agent_foundation.task_store import TaskStore, ConflictError


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tasks.db'
        self.grants = {'seller': True, 'dispatcher': True}
        self.store = TaskStore(self.path, lambda actor, task, action: self.grants.get(actor, False))

    def make_task(self):
        return self.store.create_task('seller', 'create-1', 'org', 'customer', 'scene')

    def test_restart_idempotency_conflict_and_revoked_retry(self):
        task = self.make_task()
        store = TaskStore(self.path, lambda actor, task, action: self.grants.get(actor, False))
        self.assertEqual(task, store.create_task('seller', 'create-1', 'org', 'customer', 'scene'))
        with self.assertRaises(ConflictError):
            store.create_task('seller', 'create-1', 'org', 'customer', 'different')
        self.grants['seller'] = False
        with self.assertRaises(PermissionError):
            store.create_task('seller', 'create-1', 'org', 'customer', 'scene')
        with self.assertRaises(PermissionError):
            store.get_task('seller', task['task_id'])

    def test_stale_confirmation_rejected_and_revision_preserved(self):
        task = self.make_task()['task_id']
        rev1 = self.store.add_revision('seller', 'r1', task, 'requirement', {'name': 'first'}, 1)
        ticket = self.store.request_confirmation('seller', 'c1', task, 'requirement', 1, rev1['content_hash'], 'seller')
        self.store.add_revision('seller', 'r2', task, 'requirement', {'name': 'second'}, 2)
        with self.assertRaises(ConflictError):
            self.store.confirm('seller', 'decision', ticket['confirmation_id'], 'accepted')
        self.assertEqual({'name': 'first'}, self.store.get_revision('seller', task, 'requirement', 1)['payload'])
        self.assertIsNone(self.store.claim_outbox('dispatcher'))

    def test_confirmation_idempotency_outbox_and_lease_retry(self):
        task = self.make_task()['task_id']
        rev = self.store.add_revision('seller', 'r1', task, 'profile', {'name': 'first'}, 1)
        ticket = self.store.request_confirmation('seller', 'c1', task, 'profile', 1, rev['content_hash'], 'seller')
        with self.assertRaises(PermissionError):
            self.store.confirm('dispatcher', 'wrong', ticket['confirmation_id'], 'accepted')
        saved = self.store.confirm('seller', 'decision', ticket['confirmation_id'], 'accepted')
        self.assertEqual(saved, self.store.confirm('seller', 'decision', ticket['confirmation_id'], 'accepted'))
        with self.assertRaises(ConflictError):
            self.store.confirm('seller', 'other', ticket['confirmation_id'], 'accepted')
        with patch('agent_foundation.task_store.time.time', return_value=100):
            item = self.store.claim_outbox('dispatcher', lease_seconds=1)
        with patch('agent_foundation.task_store.time.time', return_value=102):
            retry = self.store.claim_outbox('dispatcher', lease_seconds=5)
            self.assertEqual(item['delivery_key'], retry['delivery_key'])
            with self.assertRaises(ConflictError):
                self.store.mark_outbox_sent('dispatcher', item['event_id'], item['lease_token'])
            self.store.mark_outbox_sent('dispatcher', retry['event_id'], retry['lease_token'])
            self.assertIsNone(self.store.claim_outbox('dispatcher'))

    def test_concurrent_same_request_and_conflicting_version(self):
        task = self.make_task()['task_id']
        gate = threading.Barrier(2)
        def same(_):
            gate.wait()
            return self.store.add_revision('seller', 'r1', task, 'input', {'x': 1}, 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(same, range(2)))
        self.assertEqual(first, second)
        with self.assertRaises(ConflictError):
            self.store.add_revision('seller', 'different', task, 'input', {'x': 2}, 1)
        self.assertEqual(2, self.store.get_task('seller', task)['version'])

    def test_transition_validation_and_completion_requires_saved_proposal(self):
        task = self.make_task()['task_id']
        running = self.store.transition('seller', 'run', task, 1, 'running')
        with self.assertRaises(ConflictError):
            self.store.transition('seller', 'complete', task, running['version'], 'completed')
        proposal = self.store.add_revision('seller', 'p', task, 'proposal', {'text': 'draft'}, running['version'])
        self.assertEqual('completed', self.store.transition('seller', 'complete', task, proposal['task_version'], 'completed')['status'])
        with self.assertRaises(ConflictError):
            self.store.add_revision('seller', 'p2', task, 'proposal', {'text': 'new'}, 4)
