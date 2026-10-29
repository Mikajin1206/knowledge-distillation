import tempfile
import unittest
from pathlib import Path
from tests._path import *
from agent_foundation.task_store import TaskStore

class KnowledgeTraceTests(unittest.TestCase):
    def test_persistence_scope_and_denied_identity_redaction(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            path = Path(folder) / 'data.db'
            allow = {'alice': True}
            store = TaskStore(path, lambda actor, task, action: allow.get(actor, False))
            task = store.create_task('alice', 'create', 'org', 'customer', 'scene')['task_id']
            event = {'trace_id':'trace-1', 'tool':'search_knowledge', 'corpus':'project',
                     'identifier':'k-secret', 'revision':1, 'digest':'hash', 'outcome':'denied',
                     'query':'confidential query'}
            store.record_knowledge_trace('alice', task, event)
            store.record_knowledge_trace('alice', task, {**event, 'identifier':'k-public', 'outcome':'allowed'})
            reopened = TaskStore(path, lambda actor, task, action: allow.get(actor, False))
            events = reopened.get_knowledge_trace('alice', task, 'trace-1')
            self.assertEqual(2, len(events))
            self.assertIsNone(events[0]['identifier'])
            self.assertEqual('k-public', events[1]['identifier'])
            self.assertNotIn('query', events[1])
            allow['alice'] = False
            with self.assertRaises(PermissionError): reopened.get_knowledge_trace('alice', task, 'trace-1')
            with self.assertRaises(PermissionError): reopened.record_knowledge_trace('alice', task, event)
