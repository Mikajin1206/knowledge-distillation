"""Isolated in-process task entrypoint with server-owned identity.

This is a development entrypoint, not a production HTTP or WeCom service.
"""
from __future__ import annotations
import json
import sqlite3
from uuid import uuid4
from pathlib import Path
from typing import Any
from .access import LocalAccess, Principal
from .features import Features
from .task_store import TaskStore
from .worker import Worker


def _actor(principal: Principal) -> str:
    return json.dumps([principal.org_id, principal.employee_id], ensure_ascii=False, separators=(',', ':'))


class LocalApplication:
    def __init__(self, database: str | Path, access: LocalAccess, features: Features,
                 *, checkpointer=None, lock_dir: str | Path = 'tmp/confirmation-graph-locks'):
        self.access = access
        self.features = features
        self.database = str(database)
        self._worker_actor = 'local-worker:' + uuid4().hex
        self.store = TaskStore(database, self._authorize)
        self.confirmation_graph = None
        if checkpointer is not None:
            from .confirmation_graph import (ConfirmationGraph, task_store_commit,
                                             task_store_confirmation_revision)
            self.confirmation_graph = ConfirmationGraph(
                checkpointer, lambda actor, task: self._authorize(actor, task, 'confirm'),
                lambda task: (_ for _ in ()).throw(ValueError('Confirmation ID is required')),
                current_confirmation_revision=task_store_confirmation_revision(self.store),
                commit_decision=task_store_commit(self.store), lock_dir=lock_dir)

    def _authorize(self, actor: str, task_id: str | None, action: str) -> bool:
        if task_id is None and action in {'work', 'dispatch'}:
            return actor == self._worker_actor
        try:
            org, employee = json.loads(actor)
            principal = Principal(org, employee)
        except (ValueError, TypeError):
            return False
        if not self.access.is_active(principal):
            return False
        if action == 'create':
            return self.access.allowed(principal, 'create', 'org', org)
        if task_id is None:
            return False
        with sqlite3.connect(self.database) as db:
            row = db.execute('SELECT org_id, owner_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row or row[0] != org:
            return False
        return row[1] == actor or self.access.allowed(principal, action, 'task', task_id)

    def _identity(self, token: str) -> Principal:
        self.features.require('tasks')
        return self.access.authenticate(token)

    def create_task(self, token: str, request_id: str, customer_id: str, scenario: str) -> dict[str, Any]:
        principal = self._identity(token)
        return self.store.create_task(_actor(principal), request_id, principal.org_id, customer_id, scenario)

    def get_task(self, token: str, task_id: str) -> dict[str, Any]:
        principal = self._identity(token)
        return self.store.get_task(_actor(principal), task_id)

    def add_revision(self, token: str, request_id: str, task_id: str, kind: str,
                     payload: Any, expected_version: int) -> dict[str, Any]:
        principal = self._identity(token)
        return self.store.add_revision(_actor(principal), request_id, task_id, kind, payload, expected_version)

    def get_revision(self, token: str, task_id: str, kind: str, revision: int) -> dict[str, Any]:
        principal = self._identity(token)
        return self.store.get_revision(_actor(principal), task_id, kind, revision)

    def request_confirmation(self, token: str, request_id: str, task_id: str, kind: str,
                             revision: int, content_hash: str, confirmer: Principal) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('confirmations')
        if confirmer.org_id != principal.org_id:
            raise PermissionError('Confirmer must belong to the same organization')
        return self.store.request_confirmation(_actor(principal), request_id, task_id, kind,
                                               revision, content_hash, _actor(confirmer))

    def confirm(self, token: str, request_id: str, confirmation_id: str, decision: str) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('confirmations')
        return self.store.confirm(_actor(principal), request_id, confirmation_id, decision)

    def get_confirmation(self, token: str, confirmation_id: str) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('confirmations')
        return self.store.get_confirmation(_actor(principal), confirmation_id)

    def start_confirmation_graph(self, token: str, confirmation_id: str):
        self.features.require('confirmations')
        principal = self._identity(token)
        if self.confirmation_graph is None:
            raise RuntimeError('Confirmation checkpointer is not configured')
        actor = _actor(principal)
        ticket = self.store.get_confirmation(actor, confirmation_id)
        if ticket['state'] != 'pending':
            raise ValueError('Confirmation is not pending')
        revision = self.store.get_revision(actor, ticket['task_id'], ticket['kind'], ticket['revision'])
        proposal = json.dumps(revision['payload'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return self.confirmation_graph.start(actor, ticket['task_id'], ticket['revision'],
                                             ticket['content_hash'], proposal,
                                             confirmation_id=confirmation_id,
                                             allowed_actor=ticket['allowed_actor'])

    def resume_confirmation_graph(self, token: str, confirmation_id: str, decision: str):
        self.features.require('confirmations')
        principal = self._identity(token)
        if self.confirmation_graph is None:
            raise RuntimeError('Confirmation checkpointer is not configured')
        actor = _actor(principal)
        ticket = self.store.get_confirmation(actor, confirmation_id)
        return self.confirmation_graph.resume(actor, ticket['task_id'], decision,
                                              confirmation_id=confirmation_id)

    def enqueue_job(self, token: str, request_id: str, task_id: str, kind: str,
                    revision: int, content_hash: str) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('worker')
        return self.store.enqueue_job(_actor(principal), request_id, task_id, kind, revision, content_hash)

    def run_worker_once(self, handler, lease_seconds: int = 60) -> dict[str, Any] | None:
        """Run an operator-supplied local handler, never a client-supplied callback."""
        self.features.require('worker')
        return Worker(self.store, self._worker_actor, handler).run_once(lease_seconds)

    def enqueue_model_job(self, token: str, request_id: str, task_id: str, kind: str,
                          revision: int, content_hash: str) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('model')
        return self.store.enqueue_job(_actor(principal), request_id, task_id, kind, revision, content_hash,
                                      worker_type='model')

    def get_job(self, token: str, job_id: str) -> dict[str, Any]:
        principal = self._identity(token)
        self.features.require('worker')
        return self.store.get_job(_actor(principal), job_id)

    async def run_model_worker_once(self, adapter, *, max_calls_per_task: int = 3,
                                    lease_seconds: int = 120):
        """Use an operator-configured model adapter; clients cannot choose an endpoint."""
        from .model_worker import ModelWorker
        self.features.require('model')
        return await ModelWorker(self.store, self._worker_actor, adapter,
                                 max_calls_per_task=max_calls_per_task).run_once(lease_seconds)
