"""Execute queued text-model requests with persistent task-scoped call limits.

The worker is local development infrastructure, not a business agent. It never
executes tools or external write actions and never promotes outputs to knowledge.
"""
from __future__ import annotations
from langchain_core.messages import HumanMessage
from .model_adapter import ModelAdapter, ModelError, TaskCallBudget
from .task_store import ConflictError, TaskStore


class ModelWorker:
    def __init__(self, store: TaskStore, worker_actor: str, adapter: ModelAdapter,
                 *, max_calls_per_task: int = 3):
        self.store, self.worker_actor, self.adapter = store, worker_actor, adapter
        self.max_calls_per_task = max_calls_per_task

    async def run_once(self, lease_seconds: int = 120):
        job = self.store.claim_job(self.worker_actor, lease_seconds, worker_type='model')
        if job is None:
            return None
        call_id = None
        try:
            source = self.store.get_claimed_job_input(self.worker_actor, job['job_id'], job['lease_token'])
            if not isinstance(source, dict) or not isinstance(source.get('prompt'), str) or not source['prompt'].strip():
                raise ValueError('The job input requires a nonempty prompt')
            reserved = self.store.reserve_model_call(self.worker_actor, job['job_id'], job['lease_token'],
                                                     self.adapter.config.model, self.max_calls_per_task)
            call_id = reserved['call_id']
            result = await self.adapter.invoke([HumanMessage(content=source['prompt'])],
                                               TaskCallBudget(job['task_id'], 1))
            if result.message.tool_calls or not isinstance(result.message.content, str):
                raise ModelError('invalid_response', 'Text-only job returned a non-text response')
            self.store.record_model_outcome(self.worker_actor, job['job_id'], call_id, 'success')
            call_id = None
            return self.store.finish_job(self.worker_actor, job['job_id'], job['lease_token'],
                                         {'text': result.message.content, 'model': result.configured_model,
                                          'model_call_id': reserved['call_id']})
        except PermissionError:
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'], retryable=False, reason='access_denied')
            except ConflictError:
                pass
            raise
        except ConflictError:
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'], retryable=False, reason='stale_input')
            except ConflictError:
                pass
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ModelError) else 'unexpected'
            if call_id is not None:
                self.store.record_model_outcome(self.worker_actor, job['job_id'], call_id, code)
            try:
                self.store.fail_job(self.worker_actor, job['job_id'], job['lease_token'],
                                    retryable=code in {'timeout', 'connection', 'rate_limit'}, reason='handler_failed')
            except ConflictError:
                pass
            raise
