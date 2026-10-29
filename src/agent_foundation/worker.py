"""Single-job worker for isolated use. Handlers must not perform external writes."""
from __future__ import annotations
from typing import Any, Callable
from .task_store import ConflictError, TaskStore


class Worker:
    def __init__(self, store: TaskStore, worker_actor: str,
                 handler: Callable[[dict[str, Any]], Any]):
        self.store, self.worker_actor, self.handler = store, worker_actor, handler

    def run_once(self, lease_seconds: int = 60) -> dict[str, Any] | None:
        job = self.store.claim_job(self.worker_actor, lease_seconds)
        if job is None:
            return None
        try:
            # Read also reauthorizes the original actor immediately before execution.
            source = self.store.get_claimed_job_input(self.worker_actor, job['job_id'], job['lease_token'])
            result = self.handler(source)
            return self.store.finish_job(self.worker_actor, job['job_id'], job['lease_token'], result)
        except PermissionError:
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'], retryable=False, reason='access_denied')
            except ConflictError:
                pass
            raise
        except ConflictError:
            # An expired lease belongs to another worker; never change its state.
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'], retryable=False, reason='stale_input')
            except ConflictError:
                pass
            raise
        except Exception:
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'], retryable=True, reason='handler_failed')
            except ConflictError:
                pass
            raise
