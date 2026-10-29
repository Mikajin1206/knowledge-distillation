"""Explicitly constructed HTTP interface for an isolated local Agent service.

The application is injected by the host; this module does not start a listener,
load credentials or connect to company services. It is not production auth.
"""
from __future__ import annotations
from typing import Any, Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from .access import Principal
from .features import FeatureDisabled
from .local_application import LocalApplication
from .knowledge_gateway import EvidenceRef, KnowledgeError
from .task_store import ConflictError, TaskNotFound


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CreateTask(Input):
    customer_id: str = Field(min_length=1, max_length=200)
    scenario: str = Field(min_length=1, max_length=2000)


class Revision(Input):
    kind: Literal['input', 'profile', 'requirement', 'proposal', 'result']
    payload: Any
    expected_version: int = Field(ge=1, strict=True)


class AskConfirmation(Input):
    kind: Literal['input', 'profile', 'requirement', 'proposal', 'result']
    revision: int = Field(ge=1, strict=True)
    content_hash: str = Field(min_length=1, max_length=128)
    confirmer_employee_id: str = Field(min_length=1, max_length=200)


class Decide(Input):
    decision: Literal['accepted', 'rejected']


class QueueModel(Input):
    kind: Literal['input', 'profile', 'requirement', 'proposal', 'result']
    revision: int = Field(ge=1, strict=True)
    content_hash: str = Field(min_length=1, max_length=128)


class KnowledgeSearch(Input):
    corpus: Literal['project', 'product']
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20, strict=True)


class KnowledgeRead(Input):
    corpus: Literal['project', 'product']
    identifier: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1, strict=True)
    digest: str = Field(min_length=1, max_length=256)
    section_ids: list[str] = Field(min_length=1, max_length=100)


def create_http_app(service: LocalApplication) -> FastAPI:
    app = FastAPI(title='Agent foundation local API', docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    def token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if credentials is None or credentials.scheme.lower() != 'bearer':
            raise HTTPException(status_code=401, detail='Authentication required')
        try:
            service.access.authenticate(credentials.credentials)
        except PermissionError:
            raise HTTPException(status_code=401, detail='Authentication failed') from None
        return credentials.credentials

    def request_id(value: str = Header(alias='Idempotency-Key', min_length=1, max_length=128)) -> str:
        if not value.strip():
            raise HTTPException(status_code=400, detail='Idempotency key required')
        return value

    @app.exception_handler(PermissionError)
    async def denied(_request: Request, _exc: PermissionError):
        return JSONResponse(status_code=403, content={'detail': 'Access denied'})

    @app.exception_handler(TaskNotFound)
    async def not_found(_request: Request, _exc: TaskNotFound):
        return JSONResponse(status_code=404, content={'detail': 'Resource not found'})

    @app.exception_handler(ConflictError)
    async def conflict(_request: Request, _exc: ConflictError):
        return JSONResponse(status_code=409, content={'detail': 'Version, state or idempotency conflict'})

    @app.exception_handler(FeatureDisabled)
    async def unavailable(_request: Request, _exc: FeatureDisabled):
        return JSONResponse(status_code=503, content={'detail': 'Feature unavailable'})

    @app.exception_handler(ValueError)
    async def bad_request(_request: Request, _exc: ValueError):
        return JSONResponse(status_code=400, content={'detail': 'Invalid request'})

    @app.exception_handler(KnowledgeError)
    async def knowledge_error(_request: Request, exc: KnowledgeError):
        return JSONResponse(status_code=502, content={'detail': 'Knowledge service failed', 'kind': exc.kind})

    def knowledge():
        service.features.require('knowledge')
        instance = getattr(service, 'knowledge_service', None)
        if instance is None:
            raise FeatureDisabled('Knowledge service not configured')
        return instance

    @app.post('/tasks', status_code=201)
    def create_task(body: CreateTask, auth: str = Depends(token), key: str = Depends(request_id)):
        return service.create_task(auth, key, body.customer_id, body.scenario)

    @app.get('/tasks/{task_id}')
    def get_task(task_id: str, auth: str = Depends(token)):
        return service.get_task(auth, task_id)

    @app.post('/tasks/{task_id}/revisions', status_code=201)
    def add_revision(task_id: str, body: Revision, auth: str = Depends(token), key: str = Depends(request_id)):
        return service.add_revision(auth, key, task_id, body.kind, body.payload, body.expected_version)

    @app.get('/tasks/{task_id}/revisions/{kind}/{revision}')
    def get_revision(task_id: str, kind: str, revision: int, auth: str = Depends(token)):
        return service.get_revision(auth, task_id, kind, revision)

    @app.post('/tasks/{task_id}/confirmations', status_code=201)
    def request_confirmation(task_id: str, body: AskConfirmation,
                             auth: str = Depends(token), key: str = Depends(request_id)):
        principal = service.access.authenticate(auth)
        confirmer = Principal(principal.org_id, body.confirmer_employee_id)
        return service.request_confirmation(auth, key, task_id, body.kind, body.revision, body.content_hash, confirmer)

    @app.get('/confirmations/{confirmation_id}')
    def get_confirmation(confirmation_id: str, auth: str = Depends(token)):
        return service.get_confirmation(auth, confirmation_id)

    @app.post('/confirmations/{confirmation_id}/decisions')
    def confirm(confirmation_id: str, body: Decide, auth: str = Depends(token), key: str = Depends(request_id)):
        return service.confirm(auth, key, confirmation_id, body.decision)

    @app.post('/tasks/{task_id}/model-jobs', status_code=202)
    def enqueue_model(task_id: str, body: QueueModel, auth: str = Depends(token), key: str = Depends(request_id)):
        return service.enqueue_model_job(auth, key, task_id, body.kind, body.revision, body.content_hash)

    @app.get('/jobs/{job_id}')
    def get_job(job_id: str, auth: str = Depends(token)):
        return service.get_job(auth, job_id)

    @app.post('/tasks/{task_id}/knowledge/search')
    async def search_knowledge(task_id: str, body: KnowledgeSearch, auth: str = Depends(token)):
        return await knowledge().search(auth, task_id, body.corpus, body.query, body.limit)

    @app.post('/tasks/{task_id}/knowledge/sections')
    async def read_knowledge_sections(task_id: str, body: KnowledgeRead, auth: str = Depends(token)):
        ref = EvidenceRef(body.corpus, body.identifier, body.revision, body.digest)
        return await knowledge().read_sections(auth, task_id, ref, body.section_ids)

    return app
