"""Minimal OpenAI-compatible model calls for the agent service.

This module selects no vendor and grants no knowledge or business permissions.
The API endpoint, actual model identifier and environment variable naming the
credential are explicit deployment configuration; credentials are not stored.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import openai
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    credential_env: str
    timeout_seconds: float = 60.0
    max_output_tokens: int = 2048
    # Explicitly set only when using the provider's documented option.
    disable_thinking: bool = False

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('A valid API base URL without credentials or query is required')
        if not self.model.strip() or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.credential_env):
            raise ValueError('Model identifier and credential environment variable are required')
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 600:
            raise ValueError('Model timeout must be greater than zero and at most 600 seconds')
        if (isinstance(self.max_output_tokens, bool) or not isinstance(self.max_output_tokens, int)
                or not 1 <= self.max_output_tokens <= 32768):
            raise ValueError('Model output token limit is invalid')
        if not isinstance(self.disable_thinking, bool):
            raise ValueError('disable_thinking must be a boolean')

    def credential(self, environ: Mapping[str, str] | None = None) -> str:
        value = (environ if environ is not None else os.environ).get(self.credential_env, '').strip()
        if not value:
            raise ModelConfigurationError('Model credential is unavailable')
        return value


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelConfigurationError(ModelError):
    def __init__(self, message: str) -> None:
        super().__init__('configuration', message)


class TaskCallBudget:
    """A local per-task request budget; share one instance across its calls.

    A call consumes a slot before contacting the endpoint, including on failure.
    It is process-local and does not substitute for a persistent multi-worker quota.
    """
    def __init__(self, task_id: str, max_calls: int) -> None:
        if not task_id.strip() or isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError('A task ID and a positive call limit are required')
        self.task_id = task_id
        self.max_calls = max_calls
        self._used = 0
        self._lock = asyncio.Lock()

    @property
    def used(self) -> int:
        return self._used

    async def reserve(self) -> None:
        async with self._lock:
            if self._used >= self.max_calls:
                raise ModelError('limit', 'Task model call limit reached')
            self._used += 1


@dataclass(frozen=True)
class ModelResult:
    task_id: str
    configured_model: str
    # The message retains tool call IDs and provider metadata for subsequent turns.
    message: AIMessage
    json_object: dict[str, Any] | None = None


class ModelAdapter:
    def __init__(self, config: ModelConfig, *, environ: Mapping[str, str] | None = None) -> None:
        self.config = config
        options: dict[str, Any] = {
            'model': config.model,
            'base_url': config.base_url,
            'api_key': config.credential(environ),
            'timeout': config.timeout_seconds,
            'max_retries': 0,
            'max_completion_tokens': config.max_output_tokens,
        }
        if config.disable_thinking:
            options['extra_body'] = {'thinking': {'type': 'disabled'}}
        self._client = ChatOpenAI(**options)

    async def invoke(self, messages: Sequence[BaseMessage], budget: TaskCallBudget, *,
                     tools: Sequence[Any] = (), json_output: bool = False) -> ModelResult:
        if not messages or not all(isinstance(message, BaseMessage) for message in messages):
            raise ValueError('At least one LangChain message is required')
        # JSON output is a final answer mode; tool requests use the normal mode.
        if tools and json_output:
            raise ValueError('Choose tools or JSON output for a single call')
        await budget.reserve()
        client = self._client.bind_tools(list(tools)) if tools else self._client
        if json_output:
            client = client.bind(response_format={'type': 'json_object'})
        try:
            message = await asyncio.wait_for(client.ainvoke(list(messages)), timeout=self.config.timeout_seconds)
        except (asyncio.TimeoutError, openai.APITimeoutError) as exc:
            raise ModelError('timeout', 'Model request timed out') from exc
        except openai.AuthenticationError as exc:
            raise ModelError('authentication', 'Model API authentication failed') from exc
        except openai.RateLimitError as exc:
            raise ModelError('rate_limit', 'Model API rate limit reached') from exc
        except openai.APIConnectionError as exc:
            raise ModelError('connection', 'Model API connection failed') from exc
        except openai.APIStatusError as exc:
            raise ModelError('provider', f'Model API returned HTTP {exc.status_code}') from exc
        except openai.APIError as exc:
            raise ModelError('provider', 'Model API request failed') from exc
        if not isinstance(message, AIMessage) or message.invalid_tool_calls:
            raise ModelError('invalid_response', 'Model returned an invalid response or tool call')
        parsed = None
        if json_output:
            try:
                parsed = json.loads(message.content)
            except (TypeError, ValueError) as exc:
                raise ModelError('invalid_response', 'Model response is not valid JSON') from exc
            if not isinstance(parsed, dict):
                raise ModelError('invalid_response', 'Model response must be a JSON object')
        return ModelResult(budget.task_id, self.config.model, message, parsed)
