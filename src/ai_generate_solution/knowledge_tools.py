"""Agent retrieval through the sandbox active-version contract, with citations."""
from functools import lru_cache
import json
from langchain_core.tools import tool
from chunks_and_in_vector.pipeline import SandboxRAG

@lru_cache(maxsize=1)
def _service():
    return SandboxRAG()


def _render(kind, query):
    hits = _service().search(query, kind, 3)
    if not hits:
        return '隔离开发库中未发现相关证据。不得据此推断事实不存在。'
    return json.dumps({'sandbox': True, 'notice': '开发草稿结果，不表示审核通过；保留事实状态与待确认项。',
                       'results': hits}, ensure_ascii=False)

@tool
def search_solution_design(query: str) -> str:
    """检索开发库的方案设计，返回当前版本正文片段、章节和来源版本。"""
    return _render('方案设计', query)

@tool
def search_outline_design(query: str) -> str:
    """检索开发库的概要设计，返回当前版本正文片段、章节和来源版本。"""
    return _render('概要设计', query)

@tool
def search_research_report(query: str) -> str:
    """检索开发库的调研报告，返回当前版本正文片段、章节和来源版本。"""
    return _render('调研报告', query)

@tool
def search_dev_function_spec(query: str) -> str:
    """检索开发库的功能说明书，返回当前版本正文片段、章节和来源版本。"""
    return _render('开发功能说明书', query)

KNOWLEDGE_TOOLS = [search_solution_design, search_outline_design,
                   search_research_report, search_dev_function_spec]
