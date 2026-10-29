import tempfile
import unittest
from pathlib import Path
from tests._path import *
from agent_foundation.access import Grant, LocalAccess, Principal
from agent_foundation.features import Feature, FeatureDisabled, Features
from agent_foundation.local_application import LocalApplication

class LocalApplicationTests(unittest.TestCase):
    def test_authenticated_task_lifecycle_and_cross_user_isolation(self):
        alice, bob = Principal('org', 'alice'), Principal('org', 'bob')
        access = LocalAccess({'a': alice, 'b': bob}, {Grant(alice, 'create', 'org', 'org')})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            app = LocalApplication(Path(folder) / 'db', access, Features([Feature('tasks', '1'), Feature('confirmations', '1', ('tasks',))], {'tasks', 'confirmations'}))
            task = app.create_task('a', 'create', 'customer', 'scene')
            with self.assertRaises(PermissionError): app.get_task('b', task['task_id'])
            revision = app.add_revision('a', 'edit', task['task_id'], 'profile', {'name': 'draft'}, 1)
            ticket = app.request_confirmation('a', 'request', task['task_id'], 'profile', 1, revision['content_hash'], alice)
            self.assertEqual('accepted', app.confirm('a', 'confirm', ticket['confirmation_id'], 'accepted')['decision'])
            self.assertEqual({'name': 'draft'}, app.get_revision('a', task['task_id'], 'profile', 1)['payload'])
            with self.assertRaises(PermissionError): app.get_task('wrong', task['task_id'])
            app.features = Features([Feature('tasks', '1')], set())
            with self.assertRaises(FeatureDisabled): app.get_task('a', task['task_id'])

    def test_worker_rechecks_revoked_owner_and_saves_result(self):
        alice = Principal('org', 'alice')
        access = LocalAccess({'a': alice}, {Grant(alice, 'create', 'org', 'org')})
        features = Features([Feature('tasks', '1'), Feature('worker', '1', ('tasks',))], {'tasks', 'worker'})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            app = LocalApplication(Path(folder) / 'db', access, features)
            task = app.create_task('a', 'create', 'customer', 'scene')['task_id']
            rev = app.add_revision('a', 'input', task, 'input', {'v': 1}, 1)
            app.enqueue_job('a', 'job', task, 'input', 1, rev['content_hash'])
            self.assertEqual(1, app.run_worker_once(lambda value: {'v': value['v'] + 1})['result_revision'])
            self.assertEqual({'v': 2}, app.get_revision('a', task, 'result', 1)['payload'])
            app.enqueue_job('a', 'job-2', task, 'input', 1, rev['content_hash'])
            access.revoke_principal(alice)
            ran = []
            with self.assertRaises(PermissionError): app.run_worker_once(lambda value: ran.append(value))
            self.assertEqual([], ran)

    def test_confirmation_graph_commits_bound_domain_record(self):
        from langgraph.checkpoint.memory import InMemorySaver
        alice, bob = Principal('org', 'alice'), Principal('org', 'bob')
        access = LocalAccess({'a': alice, 'b': bob}, {Grant(alice, 'create', 'org', 'org')})
        features = Features([Feature('tasks', '1'), Feature('confirmations', '1', ('tasks',))],
                            {'tasks', 'confirmations'})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            app = LocalApplication(Path(folder) / 'db', access, features, checkpointer=InMemorySaver(),
                                   lock_dir=Path(folder) / 'locks')
            task = app.create_task('a', 'create', 'customer', 'scene')['task_id']
            profile = app.add_revision('a', 'profile', task, 'profile', {'name': 'Draft'}, 1)
            requirement = app.add_revision('a', 'requirement', task, 'requirement', {'need': 'A'}, 2)
            p = app.request_confirmation('a', 'ask-profile', task, 'profile', 1, profile['content_hash'], alice)
            r = app.request_confirmation('a', 'ask-requirement', task, 'requirement', 1, requirement['content_hash'], alice)
            interrupted = app.start_confirmation_graph('a', p['confirmation_id'])
            self.assertIn('Draft', interrupted['__interrupt__'][0].value['proposal'])
            app.start_confirmation_graph('a', r['confirmation_id'])
            with self.assertRaises(PermissionError): app.resume_confirmation_graph('b', p['confirmation_id'], 'approve')
            self.assertEqual('approve', app.resume_confirmation_graph('a', p['confirmation_id'], 'approve')['decision'])
            self.assertEqual('reject', app.resume_confirmation_graph('a', r['confirmation_id'], 'reject')['decision'])
            p_record = app.store.get_confirmation('["org","alice"]', p['confirmation_id'])
            self.assertEqual('accepted', p_record['decision'])
            event = app.store.claim_outbox(app._worker_actor)
            self.assertEqual('confirmation.decided', event['event_type'])
