"""
FastAPI 测试接口：用 HTTP 调 Agent（与 MCP 共用同一 Agent 核心）
启动：
  PYTHONPATH=src .venv/bin/python -m ai_generate_solution.api
  或 PYTHONPATH=src .venv/bin/uvicorn ai_generate_solution.api:app --port 8001 --reload
测试：
  curl -X POST http://127.0.0.1:8001/generate_solution \
    -H 'Content-Type: application/json' \
    -d '{"user_prompt":"给一个采购退货系统的解决方案大纲"}'
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .agent import run_agent
import json
from typing import Iterator
from fastapi.responses import StreamingResponse
from .agent import run_agent, stream_agent   # 原来只 import 了 run_agent

app = FastAPI(title="AI Generate Solution API", version="0.1.0")


class GenerateRequest(BaseModel):
    user_prompt: str = Field(..., min_length=1, description="用户的业务需求描述")


class GenerateResponse(BaseModel):
    answer: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/generate_solution", response_model=GenerateResponse)
def generate_solution(req: GenerateRequest) -> GenerateResponse:
    try:
        return GenerateResponse(answer=run_agent(req.user_prompt))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc) or type(exc).__name__) from exc

@app.post("/generate_solution/stream")
def generate_solution_stream(req: GenerateRequest) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        try:
            for delta in stream_agent(req.user_prompt):
                # SSE 帧：data: {"delta":"..."}\n\n（JSON 会转义换行，安全）
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(exc) or type(exc).__name__}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 关掉 nginx 等反代缓冲
        },
    )

@app.post("/generate_solution/stream_text")
def generate_solution_stream_text(req: GenerateRequest):
    return StreamingResponse(
        stream_agent(req.user_prompt),
        media_type="text/plain; charset=utf-8",
        headers={"X-Accel-Buffering": "no"},
    )




class RagRequest(BaseModel):
    action: str
    path: str = ''
    query: str = ''
    knowledge_type: str = ''
    knowledge_id: str = ''
    revision: int = 0
    limit: int = Field(default=5, ge=1, le=20)


@app.post('/rag')
def rag_workflow(req: RagRequest) -> dict:
    """Sandbox ingestion/search/read/withdraw; never formal publication."""
    from chunks_and_in_vector.mcp_server import rag_knowledge
    try:
        return rag_knowledge(**req.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8001")))
