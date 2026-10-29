"""Explicit local-only configuration for K17 development processes.

Values come from the invoking process. This module does not load dotenv files,
configure a user's client, or claim company identity or production isolation.
"""
from __future__ import annotations
import os
from collections.abc import Mapping
from .access import Grant, LocalAccess, Principal
from .features import Feature, Features
from .local_application import LocalApplication
from .model_adapter import ModelAdapter, ModelConfig


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, '').strip()
    if not value:
        raise ValueError(f'{name} is required')
    return value


def build_local_application(env: Mapping[str, str] | None = None) -> LocalApplication:
    source = os.environ if env is None else env
    principal = Principal(_required(source, 'AGENT_LOCAL_ORG'), _required(source, 'AGENT_LOCAL_EMPLOYEE'))
    knowledge_enabled = source.get('AGENT_LOCAL_KNOWLEDGE_ENABLED', '0')
    if knowledge_enabled not in {'0', '1'}:
        raise ValueError('AGENT_LOCAL_KNOWLEDGE_ENABLED must be 0 or 1')
    ids = {corpus: frozenset(value.strip() for value in source.get(key, '').split(',') if value.strip())
           for corpus, key in [('project', 'AGENT_LOCAL_PROJECT_KNOWLEDGE_IDS'),
                               ('product', 'AGENT_LOCAL_PRODUCT_MATERIAL_IDS')]}
    if knowledge_enabled == '1' and not any(ids.values()):
        raise ValueError('An explicit local knowledge ID is required')
    grants = {Grant(principal, 'create', 'org', principal.org_id)}
    if knowledge_enabled == '1':
        grants.update(Grant(principal, 'read', f'knowledge_{corpus}', identity)
                      for corpus, values in ids.items() for identity in values)
    access = LocalAccess({_required(source, 'AGENT_LOCAL_TOKEN'): principal}, grants)
    modules = [Feature('tasks', '1'), Feature('confirmations', '1', ('tasks',)), Feature('worker', '1', ('tasks',)),
               Feature('model', '1', ('worker',)), Feature('knowledge', '1', ('tasks',))]
    model_enabled = source.get('AGENT_LOCAL_MODEL_ENABLED', '0')
    if model_enabled not in {'0', '1'}:
        raise ValueError('AGENT_LOCAL_MODEL_ENABLED must be 0 or 1')
    enabled = ({'tasks', 'confirmations'} | ({'worker', 'model'} if model_enabled == '1' else set())
               | ({'knowledge'} if knowledge_enabled == '1' else set()))
    app = LocalApplication(_required(source, 'AGENT_LOCAL_DB'), access, Features(modules, enabled))
    if knowledge_enabled == '1':
        from .local_knowledge import LocalKnowledgeService, StdioKnowledgeSession
        app.knowledge_service = LocalKnowledgeService(app, StdioKnowledgeSession(source), ids)
    return app


def build_model_adapter(env: Mapping[str, str] | None = None) -> ModelAdapter:
    source = os.environ if env is None else env
    if source.get('AGENT_LOCAL_MODEL_ENABLED', '0') != '1':
        raise ValueError('Local model feature is disabled')
    thinking = source.get('AGENT_MODEL_DISABLE_THINKING', '0')
    if thinking not in {'0', '1'}:
        raise ValueError('AGENT_MODEL_DISABLE_THINKING must be 0 or 1')
    config = ModelConfig(_required(source, 'AGENT_MODEL_BASE_URL'),
                         _required(source, 'AGENT_MODEL_NAME'), 'AGENT_MODEL_API_KEY',
                         disable_thinking=thinking == '1')
    return ModelAdapter(config, environ=source)
