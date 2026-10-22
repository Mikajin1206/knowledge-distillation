"""AI Generate Solution MCP server（本地 HTTP 入口，Streamable HTTP）。

复用 main.py 里已注册的 MCPServer 与工具，只是换一种传输方式：
stdio 版由客户端拉起子进程（main.py），本入口改为监听端口供本地/远程 HTTP 客户端连接。

仅用于本机或内网联调：本入口不提供 TLS，禁止直接暴露到公网。
需要公网访问时，请放在反向代理（Caddy/Nginx/Cloudflare Tunnel）之后由代理终结 HTTPS。

启动：
  .venv/bin/python src/ai_generate_solution/mcp_http.py
  PYTHONPATH=src .venv/bin/python -m ai_generate_solution.mcp_http
  HOST=0.0.0.0 MCP_TOKEN=xxx .venv/bin/python src/ai_generate_solution/mcp_http.py

环境变量：
  HOST       监听地址，默认 127.0.0.1（仅本机可连）
  PORT       监听端口，默认 8010
  MCP_PATH   端点路径，默认 /mcp
  MCP_TOKEN  非空时校验 Authorization: Bearer <token>
"""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parents[1]

# 直接运行脚本时 sys.path[0] 是本文件所在目录：
# 先剔除它（避免遮蔽官方 mcp 包），再把项目 src 加入，才能以「包」的方式导入 main。
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(REPO_ROOT / "src"))

# noinspection PyUnresolvedReferences
from ai_generate_solution.main import server

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8010"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
TOKEN = (os.environ.get("MCP_TOKEN") or "").strip()


class BearerAuth:
    """最小 ASGI 中间件：MCP_TOKEN 非空时校验 Authorization 头。

    纯 ASGI 包装（不用 BaseHTTPMiddleware），避免缓冲影响 Streamable HTTP 的流式响应；
    lifespan 等非 http scope 原样透传。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not TOKEN:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, b"Bearer " + TOKEN.encode()):
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def build_app():
    """返回可直接交给 uvicorn 的 ASGI app（Starlette 自带 session_manager 生命周期）。"""

    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=HOST)
    return BearerAuth(app) if TOKEN else app


def main() -> int:
    import uvicorn

    print(
        f"MCP endpoint: http://{'127.0.0.1' if HOST == '0.0.0.0' else HOST}:{PORT}{MCP_PATH}"
        f"  auth={'Bearer token' if TOKEN else 'off'}",
        flush=True,
    )
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
