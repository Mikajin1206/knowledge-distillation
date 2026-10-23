"""Register the local knowledge lifecycle on the existing MCP instance."""
from contextlib import contextmanager
import os
import time

from mcp.server.mcpserver import Context

from ckp import db
from ckp.knowledge import Knowledge, SCOPE, public_value
from ckp.mcp_progress import report_progress
from ckp.knowledge_types import STAGES

_PHASE_LABELS = {'queued': '等待入库', 'verifying_sources_and_assets': '核验原件与图形资产',
                 'validated': '切片完成', 'embedding_and_writing': '生成向量并写入',
                 'verifying_chunks': '逐块读回核验', 'completed': '入库完成',
                 'failed': '入库失败', 'withdrawn': '已撤回'}


def register_knowledge_tools(server, data_dir, repo_root, tool_errors):
    @contextmanager
    def session(readonly=False):
        service = Knowledge(data_dir(), repo_root, readonly=readonly)
        try:
            yield service
        finally:
            service.close()

    @server.tool(title="查看知识库状态")
    @tool_errors
    def knowledge_status(project: str = '') -> dict:
        """本地开发能力预检与知识状态清单。不调用 embedding，不写数据；scope 为 local_development。"""
        with session(readonly=True) as service:
            drafts = service.distill.list_drafts(project or None)
            rows = [{'knowledge_id': d['knowledge_id'], 'revision': d['revision'], 'title': d['title'],
                     'content_hash': d['content_hash'], **service.lifecycle(d['knowledge_id'], d['revision'])} for d in drafts]
            from chunks_and_in_vector.pipeline import SandboxRAG
            from pymilvus import connections, utility
            rag = SandboxRAG()
            try:
                mongo = rag.mongo.client.admin.command('ping').get('ok') == 1
                alias = 'kd_status_' + db.new_id('probe')
                try:
                    connections.connect(alias=alias, host=os.getenv('MILVUS_HOST', '127.0.0.1'),
                                        port=os.getenv('MILVUS_PORT', '19530'), timeout=5)
                    version = utility.get_server_version(using=alias, timeout=5)
                finally:
                    connections.disconnect(alias)
                return {'scope': SCOPE, 'single_mcp': True, 'mongo_connected': mongo, 'milvus_version': version,
                        'embedding_mode': rag.mode, 'dimension': rag.dimension,
                        'actor': os.getenv('CKP_LOCAL_ACTOR', ''), 'knowledge': rows,
                        'primary_classification': 'stage',
                        'stage_options': list(STAGES),
                        'diagram_layers': ['section_reference', 'versioned_asset_mapping', 'verified_complete_json'],
                        'file_adapter': 'local_versioned_store', 'remote_authentication': 'not_configured'}
            finally:
                rag.close()

    @server.tool()
    @tool_errors
    def submit_for_review(knowledge_id: str, revision: int, content_hash: str) -> dict:
        """提交具体知识修订审核；绑定正文、来源和完整资产清单哈希，不默认取最新版本。"""
        with session() as service:
            return service.submit_review(knowledge_id, revision, content_hash)

    @server.tool()
    @tool_errors
    def record_review(review_id: str, decision: str, comments: str) -> dict:
        """记录用户明确给出的审核结论 approved/changes_requested。审核人由本机服务配置绑定；模型不得自行批准业务事实。当前仅本地开发身份。"""
        with session() as service:
            return service.record_review(review_id, decision, comments)

    @server.tool(title="发布知识到本地 RAG")
    @tool_errors
    def publish_knowledge(knowledge_id: str, revision: int, content_hash: str) -> dict:
        """对已审核精确修订创建本地 RAG 入库任务。服务内部取 MD/sidecar、存图形、分节切片、写 MongoDB/Milvus 并逐块读回后激活。重复调用幂等；返回任务不是完成。"""
        with session() as service:
            return service.publish(knowledge_id, revision, content_hash)

    @server.tool(title="查看入库进度")
    @tool_errors
    def get_publish_task(publish_task_id: str, wait_seconds: int = 20,
                         ctx: Context = None) -> dict:
        """等待发布进度，默认最多20秒、上限30秒；终态立即返回。wait_seconds=0 立即查询。
        客户端请求时只在状态变化发标准进度通知。published 表示本地开发库完整入库；不表示服务器迁移验收。"""
        if not 0 <= wait_seconds <= 30:
            raise ValueError('wait_seconds 必须在 0 到 30 之间')
        with session(readonly=True) as service:
            deadline = time.monotonic() + wait_seconds
            previous = None
            event = 0
            while True:
                task = service.task(publish_task_id)
                state = tuple(task[k] for k in ('status', 'phase', 'completed', 'total', 'error'))
                label = _PHASE_LABELS.get(task['phase'], task['phase'])
                name = '；'.join(task.get('filenames', [])) or task.get('title') or task['knowledge_id']
                message = f"{name}：{label}"
                if task['total']:
                    message += f" {task['completed']}/{task['total']} 块"
                task['progress_message'] = message
                if state != previous:
                    # The notification counter is an event sequence, not a fake
                    # overall percentage; actual chunk counts stay in message.
                    report_progress(ctx, event, None, message)
                    event += 1
                    previous = state
                if task['status'] in ('published', 'publish_failed', 'withdrawn') or time.monotonic() >= deadline:
                    return task
                time.sleep(min(.5, max(0, deadline - time.monotonic())))

    @server.tool()
    @tool_errors
    def search_knowledge(query: str, knowledge_type: str = '', limit: int = 5,
                         stage: str = '', products: str = '',
                         domain: list[str] | None = None) -> dict:
        """检索本地开发库当前活动且有审核记录的版本。stage 为六阶段主分类，knowledge_type 为补充文档类型筛选；products/domain 为项目经理确认的分类筛选。
        只返回正文块、章节路径、来源、精确 document_ref 和资产 ID，不自动返回全文或图形 JSON。先判断这些命中是否足以回答；缺少必要前提时再按同一 document_ref 定向补读。"""
        with session(readonly=True) as service:
            service.actor()
        from chunks_and_in_vector.pipeline import SandboxRAG
        rag = SandboxRAG()
        try:
            return {'results': public_value(rag.search(query, knowledge_type or None, limit, approved_only=True,
                                                       stage=stage or None, products=products or None,
                                                       domain=domain)), 'scope': SCOPE}
        finally:
            rag.close()

    @server.tool()
    @tool_errors
    def read_knowledge(knowledge_id: str, revision: int, content_hash: str,
                       section_ids: list[str] | None = None, full_document: bool = False) -> dict:
        """按精确 document_ref 读取。默认仅返回目录；仅在命中证据缺少必要前提/限制/关联步骤时传相关 section_ids 补读。
        只有整体方案、跨章节关系/冲突或定向补读后仍有证据缺口时才用 full_document=True；full_document 与 section_ids 互斥。足够回答时不要调用；同一修订合并 section_ids，避免重复读取。始终不返回 sidecar/完整图形 JSON。"""
        with session(readonly=True) as service:
            return service.read(knowledge_id, revision, content_hash, section_ids, full_document)

    @server.tool()
    @tool_errors
    def get_asset(knowledge_id: str, revision: int, content_hash: str,
                  section_id: str, asset_id: str) -> dict:
        """仅当回答依赖图形节点/关系/分支细节或用户要求完整还原时调用；section_id 与 asset_id 必须来自同一相关命中章节，且 asset_id 必须在该节 protected_asset_refs 中。
        按精确知识修订解析指定文件版本、读取时重验 SHA-256 并返回完整结构。普通问答不调用。失败、无权、撤回或版本/哈希不符即停止，不回退最新或同名文件。"""
        with session(readonly=True) as service:
            return service.asset(knowledge_id, revision, content_hash, asset_id, section_id)

    @server.tool()
    @tool_errors
    def withdraw_knowledge(knowledge_id: str, revision: int, content_hash: str, reason: str) -> dict:
        """撤回精确活动修订：退出默认检索，保留来源、审核和历史记录；重复发布不会自动解除撤回。"""
        with session() as service:
            return service.withdraw(knowledge_id, revision, content_hash, reason)
