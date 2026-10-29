"""Task-scoped local access to the existing single knowledge MCP.

Knowledge permissions here are explicit local development grants. They are not
company identity, project ACL, or production knowledge-service authorization.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager, Callable, Mapping
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from .knowledge_gateway import Corpus, EvidenceRef, KnowledgeError, KnowledgeGateway
from .local_application import LocalApplication, _actor


class StdioKnowledgeSession:
    """Fixed repository launcher; arguments and environment never come from HTTP."""
    def __init__(self, environment: Mapping[str, str]):
        root = Path(__file__).resolve().parents[2]
        safe_env = get_default_environment()
        for name, value in environment.items():
            if name.startswith(('CKP_', 'KD_', 'MILVUS_', 'QWEN_')):
                safe_env[name] = value
        self.params = StdioServerParameters(command=str(root / 'ckp-mcp.sh'), env=safe_env)

    @asynccontextmanager
    async def __call__(self):
        async with stdio_client(self.params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class LocalKnowledgeService:
    """Wrap the gateway with current task and per-document grants."""
    def __init__(self, service: LocalApplication,
                 session_factory: Callable[[], AsyncContextManager],
                 allowed_ids: Mapping[Corpus, frozenset[str]]):
        self.service, self.session_factory = service, session_factory
        self.allowed_ids = {k: frozenset(v) for k, v in allowed_ids.items()}

    def _guard(self, token: str, task_id: str):
        self.service.features.require('knowledge')
        principal = self.service._identity(token)
        self.service.get_task(token, task_id)
        return principal, _actor(principal)

    def _is_allowed(self, principal, actor: str, task_id: str, corpus: Corpus, ref: EvidenceRef | None):
        if not self.service._authorize(actor, task_id, 'read'):
            return False
        candidates = self.allowed_ids.get(corpus, frozenset())
        if ref is not None:
            candidates = frozenset([ref.identifier]) if ref.identifier in candidates else frozenset()
        return any(self.service.access.allowed(principal, 'read', f'knowledge_{corpus}', identifier)
                   for identifier in candidates)

    def _gateway(self, session, principal, actor: str, task_id: str):
        async def authorize(supplied_actor, corpus, ref):
            return supplied_actor == actor and self._is_allowed(principal, actor, task_id, corpus, ref)

        async def trace(event):
            self.service.store.record_knowledge_trace(actor, task_id, event)

        return KnowledgeGateway(session, authorize, trace)

    async def _execute(self, principal, actor, task_id, operation):
        # MCP's AnyIO task groups wrap exceptions raised through their context
        # manager. Capture gateway failures before closing the session so callers
        # receive the original typed error rather than an ExceptionGroup.
        failure = None
        result = None
        try:
            async with self.session_factory() as session:
                try:
                    result = await operation(self._gateway(session, principal, actor, task_id))
                except Exception as exc:
                    failure = exc
        except Exception as exc:
            raise KnowledgeError('transport_error') from exc
        if failure is not None:
            raise failure
        return result

    async def search(self, token: str, task_id: str, corpus: Corpus, query: str, limit: int = 5):
        principal, actor = self._guard(token, task_id)
        if not self._is_allowed(principal, actor, task_id, corpus, None):
            raise PermissionError('Knowledge access denied')
        trace_id = uuid4().hex
        result = await self._execute(principal, actor, task_id, lambda gateway:
            gateway.search_evidence(actor, corpus, query, limit=limit, trace_id=trace_id))
        # Recheck after I/O: no content is delivered on task/document revocation.
        self._guard(token, task_id)
        for item in result:
            ref = EvidenceRef(corpus, item['identifier'], item['revision'], item['digest'])
            if not self._is_allowed(principal, actor, task_id, corpus, ref):
                raise PermissionError('Knowledge access denied')
        return {'trace_id': trace_id, 'results': result}

    async def read_sections(self, token: str, task_id: str, ref: EvidenceRef, section_ids: list[str]):
        principal, actor = self._guard(token, task_id)
        if not self._is_allowed(principal, actor, task_id, ref.corpus, ref):
            raise PermissionError('Knowledge access denied')
        trace_id = uuid4().hex
        result = await self._execute(principal, actor, task_id, lambda gateway:
            gateway.read_sections(actor, ref, section_ids, trace_id=trace_id))
        self._guard(token, task_id)
        if not self._is_allowed(principal, actor, task_id, ref.corpus, ref):
            raise PermissionError('Knowledge access denied')
        return {'trace_id': trace_id, 'result': result}
