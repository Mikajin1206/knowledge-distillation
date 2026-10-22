"""
AI 解决方案生成 Agent → MCP 服务（stdio）
启动：./ai-generate-solution-mcp.sh  或  PYTHONPATH=src python -m ai_generate_solution.main
"""
from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .agent import run_agent

server = MCPServer(
    "AI Generate Solution",
    version="0.1.0",
    instructions=(
        "AI解决方案生成智能体。调用 generate_solution 并传入用户的业务需求描述，"
        "返回结构化的解决方案。智能体内部会按需调用知识检索等工具。"
    ),
)


@server.tool()
def generate_solution(user_prompt: str) -> str:
    """运行解决方案生成智能体：输入用户需求描述（user_prompt），返回 Markdown 格式的解决方案。"""
    try:
        return run_agent(user_prompt)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(str(exc) or type(exc).__name__) from exc


def main() -> int:
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
