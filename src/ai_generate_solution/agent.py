"""
AI 解决方案生成 Agent 核心
- LLM 配置来自项目根 .env：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_NAME（DeepSeek，OpenAI 兼容）
- 系统提示词来自 promps/system_prompt.md
- 供 MCP（main.py）与 FastAPI（api.py）共用
"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from typing import Iterator
from langchain_core.messages import AIMessageChunk

from .knowledge_tools import KNOWLEDGE_TOOLS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

PROMPT_FILE = Path(__file__).resolve().parent / "promps" / "system_prompt.md"



TOOLS = [*KNOWLEDGE_TOOLS]


# ---------- 系统提示词 ----------
def _load_system_prompt() -> str:
    if not PROMPT_FILE.is_file():
        raise FileNotFoundError(f"系统提示词文件不存在: {PROMPT_FILE}")
    text = PROMPT_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"系统提示词文件为空: {PROMPT_FILE}")
    return text


# ---------- LLM / Agent ----------
def _load_llm() -> ChatOpenAI:
    provider = os.getenv("LLM_PROVIDER", "codex-client").strip()
    if provider == "codex-client":
        raise RuntimeError("测试生成模型使用 Codex 的 GPT-6 luna / medium；请通过 Codex 客户端和 MCP 验证。当前未配置应用模型 API，自动方案生成停用。")
    if provider != "api":
        raise ValueError("LLM_PROVIDER 必须为 codex-client 或 api")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL_NAME", "gpt-6-luna").strip()
    if not api_key or not base_url:
        raise RuntimeError("应用 API 需要 LLM_API_KEY 与 LLM_BASE_URL；不会复用原 DeepSeek 凭据")
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "medium"))


@lru_cache(maxsize=1)
def get_agent():
    """惰性构建 Agent（首次调用才初始化，避免服务启动即失败）。"""
    return create_agent(
        model=_load_llm(),
        tools=TOOLS,
        system_prompt=_load_system_prompt(),
    )


def _extract_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def run_agent(user_prompt: str) -> str:
    """把用户输入交给 Agent，返回最终回答文本。"""
    if not user_prompt or not user_prompt.strip():
        raise ValueError("user_prompt 不能为空")
    result = get_agent().invoke(
        {"messages": [{"role": "user", "content": user_prompt}]}
    )
    return _extract_text(result["messages"][-1])


def _chunk_text(chunk) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def stream_agent(user_prompt: str) -> Iterator[str]:
    """流式逐 token 产出 Agent 的文本输出（工具调用/中间步骤自动过滤掉）。"""
    if not user_prompt or not user_prompt.strip():
        raise ValueError("user_prompt 不能为空")
    for chunk, _meta in get_agent().stream(
        {"messages": [{"role": "user", "content": user_prompt}]},
        stream_mode="messages",
    ):
        if isinstance(chunk, AIMessageChunk):
            text = _chunk_text(chunk)
            if text:
                yield text