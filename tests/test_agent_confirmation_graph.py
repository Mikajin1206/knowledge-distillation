import unittest
from tests._path import *
from langgraph.checkpoint.memory import InMemorySaver
from agent_foundation.confirmation_graph import ConfirmationGraph

class ConfirmationTests(unittest.TestCase):
    def test_resume_rechecks_permission_and_exact_version(self):
        allowed = {'alice'}
        version = [1, 'hash']
        graph = ConfirmationGraph(InMemorySaver(), lambda actor, task: actor in allowed,
                                  lambda task: tuple(version))
        first = graph.start('alice', 'task-1', 1, 'hash', 'Check the proposal')
        self.assertEqual(1, first['__interrupt__'][0].value['revision'])
        with self.assertRaises(PermissionError): graph.resume('bob', 'task-1', 'approve')
        version[0] = 2
        with self.assertRaisesRegex(ValueError, 'revision changed'): graph.resume('alice', 'task-1', 'approve')
        version[0] = 1
        self.assertEqual('approve', graph.resume('alice', 'task-1', 'approve')['decision'])
        with self.assertRaises(ValueError): graph.resume('alice', 'task-1', 'approve')

    def test_tasks_are_independent(self):
        graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, lambda t: (1, 'hash'))
        graph.start('alice', 'a', 1, 'hash', 'A')
        graph.start('alice', 'b', 1, 'hash', 'B')
        self.assertEqual('reject', graph.resume('alice', 'b', 'reject')['decision'])
        self.assertEqual('approve', graph.resume('alice', 'a', 'approve')['decision'])

    def test_same_task_multiple_confirmations_and_allowed_person(self):
        graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, lambda t: (1, 'hash'))
        graph.start('alice', 'task', 1, 'hash', 'First', confirmation_id='one', allowed_actor='bob')
        graph.start('alice', 'task', 1, 'hash', 'Second', confirmation_id='two', allowed_actor='alice')
        with self.assertRaises(PermissionError): graph.resume('alice', 'task', 'approve', confirmation_id='one')
        self.assertEqual('approve', graph.resume('bob', 'task', 'approve', confirmation_id='one')['decision'])
        self.assertEqual('reject', graph.resume('alice', 'task', 'reject', confirmation_id='two')['decision'])

    def test_concurrent_opposing_decisions_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, lambda t: (1, 'hash'))
        graph.start('alice', 'task', 1, 'hash', 'First', confirmation_id='one')
        gate = Barrier(2)
        def resume(decision):
            gate.wait()
            try:
                return graph.resume('alice', 'task', decision, confirmation_id='one')['decision']
            except ValueError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(resume, ['approve', 'reject']))
        self.assertEqual(1, results.count('rejected'))
        self.assertEqual(1, sum(value in ('approve', 'reject') for value in results))

    def test_domain_failure_leaves_graph_pending_for_retry(self):
        calls = []
        def commit(actor, task, confirmation, revision, digest, decision):
            calls.append((actor, task, confirmation, revision, digest, decision))
            if len(calls) == 1:
                raise RuntimeError('transient domain error')
        graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, lambda t: (1, 'hash'), commit_decision=commit)
        graph.start('alice', 'task', 1, 'hash', 'Check', confirmation_id='id')
        with self.assertRaises(RuntimeError): graph.resume('alice', 'task', 'approve', confirmation_id='id')
        self.assertEqual('approve', graph.resume('alice', 'task', 'approve', confirmation_id='id')['decision'])
        self.assertEqual(('alice', 'task', 'id', 1, 'hash', 'approve'), calls[1])

    def test_domain_commit_is_bound_and_idempotent_after_crash(self):
        import tempfile
        from pathlib import Path
        from agent_foundation.confirmation_graph import task_store_commit
        from agent_foundation.task_store import TaskStore
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            store = TaskStore(Path(folder) / 'tasks.db', lambda a, t, action: True)
            task = store.create_task('alice', 'create', 'org', 'customer', 'scene')['task_id']
            rev = store.add_revision('alice', 'rev', task, 'profile', {'x': 1}, 1)
            ticket = store.request_confirmation('alice', 'ask', task, 'profile', 1, rev['content_hash'], 'alice')
            actual_commit = task_store_commit(store)
            calls = []
            def commit(*args):
                result = actual_commit(*args)
                calls.append(result)
                if len(calls) == 1:
                    raise RuntimeError('crash after domain commit')
            graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, lambda t: (1, rev['content_hash']), commit_decision=commit)
            graph.start('alice', task, 1, rev['content_hash'], 'Confirm', confirmation_id=ticket['confirmation_id'])
            with self.assertRaises(RuntimeError): graph.resume('alice', task, 'approve', confirmation_id=ticket['confirmation_id'])
            self.assertEqual('approve', graph.resume('alice', task, 'approve', confirmation_id=ticket['confirmation_id'])['decision'])
            self.assertEqual(calls[0], calls[1])
            self.assertEqual('accepted', calls[0]['decision'])
            outbox = store.claim_outbox('worker')
            self.assertEqual('confirmation.decided', outbox['event_type'])
            self.assertIsNone(store.claim_outbox('worker'))

    def test_confirmation_revision_is_scoped_by_domain_kind(self):
        import tempfile
        from pathlib import Path
        from agent_foundation.confirmation_graph import task_store_commit, task_store_confirmation_revision
        from agent_foundation.task_store import TaskStore
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            store = TaskStore(Path(folder) / 'tasks.db', lambda a, t, action: True)
            task = store.create_task('alice', 'create', 'org', 'customer', 'scene')['task_id']
            profile = store.add_revision('alice', 'profile', task, 'profile', {'name': 'A'}, 1)
            requirement = store.add_revision('alice', 'requirement', task, 'requirement', {'need': 'B'}, 2)
            p = store.request_confirmation('alice', 'ask-p', task, 'profile', 1, profile['content_hash'], 'alice')
            r = store.request_confirmation('alice', 'ask-r', task, 'requirement', 1, requirement['content_hash'], 'alice')
            def legacy(task):
                raise AssertionError('confirmation-specific lookup must be used')
            graph = ConfirmationGraph(InMemorySaver(), lambda a, t: True, legacy,
                current_confirmation_revision=task_store_confirmation_revision(store),
                commit_decision=task_store_commit(store))
            graph.start('alice', task, 1, profile['content_hash'], 'profile', confirmation_id=p['confirmation_id'])
            graph.start('alice', task, 1, requirement['content_hash'], 'requirements', confirmation_id=r['confirmation_id'])
            store.add_revision('alice', 'requirement-2', task, 'requirement', {'need': 'C'}, 3)
            self.assertEqual('approve', graph.resume('alice', task, 'approve', confirmation_id=p['confirmation_id'])['decision'])
            with self.assertRaisesRegex(ValueError, 'revision changed'):
                graph.resume('alice', task, 'approve', confirmation_id=r['confirmation_id'])
