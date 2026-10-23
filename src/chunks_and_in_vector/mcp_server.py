"""Optional sandbox RAG MCP. The latest 21-tool CKP server stays unchanged."""
from functools import lru_cache
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from .pipeline import SandboxRAG

server = MCPServer('Knowledge RAG Development', version='0.1.0', instructions=(
    '隔离开发 RAG；所有内容均为 sandbox，不表示审核或发布。'
    '蒸馏请使用最新 Knowledge Distillation MCP；导出后才能 ingest MD+sidecar。'
    'fixture 模式只验证流程，不证明语义检索质量。'))

@lru_cache(maxsize=1)
def service():
    return SandboxRAG()

@server.tool()
def rag_knowledge(action: str, path: str = '', query: str = '', knowledge_type: str = '',
                  stage: str = '', knowledge_id: str = '', revision: int = 0,
                  limit: int = 5) -> dict:
    """内部开发 RAG：stage 是六阶段主分类，knowledge_type 是可选文档类型筛选；read/withdraw 用 stage。"""
    try:
        rag = service()
        if action == 'ingest': return rag.ingest(path)
        if action == 'search':
            return {'results': rag.search(query, knowledge_type or None, limit, stage=stage or None)}
        if action == 'read': return {'document': rag.read(stage, knowledge_id, revision or None)}
        if action == 'withdraw': return rag.withdraw(stage, knowledge_id, revision or None)
        raise ValueError('action 必须为 ingest/search/read/withdraw')
    except Exception as exc:
        raise ToolError(str(exc)) from exc

if __name__ == '__main__': server.run(transport='stdio')
