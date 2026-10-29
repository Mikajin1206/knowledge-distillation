"""Channel-independent human confirmation graph.

The graph only records a proposed decision. A domain transaction must validate
and commit it separately; a checkpoint is never treated as a business record.
The local file lock prevents concurrent use on one shared filesystem. Production
multi-host deployments must use the domain transaction for authoritative decisions.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Callable, TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def task_store_confirmation_revision(store):
    """Read the latest revision for the confirmation's task and kind."""
    def current(task_id: str, confirmation_id: str) -> tuple[int, str]:
        with sqlite3.connect(store.path) as db:
            row = db.execute(
                'SELECT r.revision,r.content_hash FROM confirmations c '
                'JOIN revisions r ON r.task_id=c.task_id AND r.kind=c.kind '
                'WHERE c.task_id=? AND c.confirmation_id=? '
                'ORDER BY r.revision DESC LIMIT 1', (task_id, confirmation_id)).fetchone()
        if row is None:
            raise ValueError('Domain confirmation not found')
        return row[0], row[1]
    return current


def task_store_commit(store):
    """Bind graph confirmations to a local TaskStore's authoritative record.

    Confirmation identity/target fields are immutable. TaskStore.confirm performs
    current authorization, latest-version checks and the idempotent transaction.
    """
    def commit(actor: str, task_id: str, confirmation_id: str,
               revision: int, content_hash: str, decision: str):
        with sqlite3.connect(store.path) as db:
            row = db.execute('SELECT task_id, revision, content_hash FROM confirmations WHERE confirmation_id=?',
                             (confirmation_id,)).fetchone()
        if row != (task_id, revision, content_hash):
            raise ValueError('Domain confirmation target mismatch')
        return store.confirm(actor, f'graph-confirm:{confirmation_id}', confirmation_id,
                             'accepted' if decision == 'approve' else 'rejected')
    return commit


class ConfirmationState(TypedDict, total=False):
    task_id: str
    confirmation_id: str
    allowed_actor: str
    revision: int
    content_hash: str
    proposal: str
    decision: str


def _wait(state: ConfirmationState):
    answer = interrupt({'task_id': state['task_id'], 'confirmation_id': state['confirmation_id'],
                        'revision': state['revision'], 'content_hash': state['content_hash'],
                        'proposal': state['proposal']})
    if answer not in ('approve', 'reject'):
        raise ValueError('Invalid confirmation decision')
    return {'decision': answer}


class ConfirmationGraph:
    def __init__(self, checkpointer, authorize: Callable[[str, str], bool],
                 current_revision: Callable[[str], tuple[int, str]], *,
                 current_confirmation_revision: Callable[[str, str], tuple[int, str]] | None = None,
                 commit_decision: Callable[[str, str, str, int, str, str], object] | None = None,
                 lock_dir: str | Path = 'tmp/confirmation-graph-locks'):
        self.authorize = authorize
        self.current_revision = current_revision
        self.current_confirmation_revision = current_confirmation_revision
        self.commit_decision = commit_decision
        self.lock_dir = Path(lock_dir)
        graph = StateGraph(ConfirmationState)
        graph.add_node('wait_for_confirmation', _wait)
        graph.add_edge(START, 'wait_for_confirmation')
        graph.add_edge('wait_for_confirmation', END)
        self.graph = graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _config(task_id: str, confirmation_id: str | None):
        # Preserve the original thread key for existing callers.
        thread_id = task_id if confirmation_id is None else json.dumps([task_id, confirmation_id], ensure_ascii=False)
        return {'configurable': {'thread_id': thread_id}}

    @contextmanager
    def _lock(self, config):
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(config['configurable']['thread_id'].encode()).hexdigest()
        with (self.lock_dir / key).open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _check(self, actor: str, task_id: str, revision: int, content_hash: str,
               confirmation_id: str | None = None):
        if not actor or not task_id or not self.authorize(actor, task_id):
            raise PermissionError('Task access denied')
        current = (self.current_confirmation_revision(task_id, confirmation_id)
                   if confirmation_id is not None and self.current_confirmation_revision is not None
                   else self.current_revision(task_id))
        if current != (revision, content_hash):
            raise ValueError('Task revision changed')

    def start(self, actor: str, task_id: str, revision: int, content_hash: str, proposal: str, *,
              confirmation_id: str | None = None, allowed_actor: str | None = None):
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1 or not content_hash or not proposal:
            raise ValueError('A revision, hash and proposal are required')
        if confirmation_id is not None and (not isinstance(confirmation_id, str) or not confirmation_id.strip()):
            raise ValueError('A nonempty confirmation ID is required')
        allowed_actor = actor if allowed_actor is None else allowed_actor
        if not isinstance(allowed_actor, str) or not allowed_actor.strip():
            raise ValueError('An allowed confirmer is required')
        config = self._config(task_id, confirmation_id)
        with self._lock(config):
            self._check(actor, task_id, revision, content_hash, confirmation_id)
            if self.graph.get_state(config).values:
                raise ValueError('Confirmation already has a checkpoint')
            return self.graph.invoke({'task_id': task_id, 'confirmation_id': confirmation_id or '',
                                      'allowed_actor': allowed_actor, 'revision': revision,
                                      'content_hash': content_hash, 'proposal': proposal}, config)

    def resume(self, actor: str, task_id: str, decision: str, *, confirmation_id: str | None = None):
        if decision not in ('approve', 'reject'):
            raise ValueError('Invalid confirmation decision')
        config = self._config(task_id, confirmation_id)
        with self._lock(config):
            if not actor or not task_id or not self.authorize(actor, task_id):
                raise PermissionError('Task access denied')
            snapshot = self.graph.get_state(config)
            state = snapshot.values
            if not snapshot.next or not state or state.get('task_id') != task_id or state.get('confirmation_id', '') != (confirmation_id or ''):
                raise ValueError('No pending confirmation')
            if state.get('allowed_actor') != actor:
                raise PermissionError('Actor is not the allowed confirmer')
            self._check(actor, task_id, state['revision'], state['content_hash'], confirmation_id)
            if self.commit_decision is not None:
                if not confirmation_id:
                    raise ValueError('Domain confirmation ID is required')
                # The domain adapter must verify that this confirmation ID is
                # bound to this exact task/revision/hash in its own transaction,
                # then commit idempotently (including retries after a crash).
                self.commit_decision(actor, task_id, confirmation_id,
                                     state['revision'], state['content_hash'], decision)
            return self.graph.invoke(Command(resume=decision), config)
