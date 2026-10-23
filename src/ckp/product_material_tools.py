"""MCP tools for the product-material corpus on the existing MCP server."""
from contextlib import contextmanager

from ckp.product_materials import ProductMaterials


def register_product_material_tools(server, data_dir, repo_root, tool_errors):
    @contextmanager
    def session(readonly=False):
        service = ProductMaterials(data_dir(), repo_root, readonly=readonly)
        try:
            yield service
        finally:
            service.close()

    @server.tool(title="产品资料原件解析并入库")
    @tool_errors
    def product_material_ingest(path: str, category: str, product: str,
                                domain: list[str]) -> dict:
        """将用户明确指定的单个 Word 原件直接解析、切片并写入本地开发 RAG。category 为“产品手册”或“培训资料”；product 必须单选，domain 从该产品目录中选择至少一个。与项目无关；保留原件，不做蒸馏、草稿或审核。重复内容幂等；同名新内容建立新修订。返回原件哈希、分节/切片数、逐块读回核验数和未做 OCR 的图片数。state=unchanged 时这些计数来自首次成功入库记录；verification_basis=original_ingest 且 readback_performed_this_call=false 表示本次未重新读回核验。"""
        with session() as service:
            return service.ingest(path, category, product, domain)

    @server.tool(title="列出产品资料")
    @tool_errors
    def product_material_list(product: str = "", category: str = "") -> dict:
        """列出已登记的产品手册和培训资料，可按产品或类别筛选；不读原件、不连接向量服务。"""
        with session(readonly=True) as service:
            materials = service.list(product, category)
            return {"materials": materials, "count": len(materials),
                    "categories": ["产品手册", "培训资料"], "sandbox": True}

    @server.tool(title="检索产品资料")
    @tool_errors
    def search_product_materials(query: str, product: str = "", domain: list[str] | None = None,
                                 category: str = "", limit: int = 5) -> dict:
        """仅检索产品资料逻辑库，不混入项目生命周期档案。产品问答优先调用；支持产品、领域和类别过滤。返回命中分节及精确 material_id/revision/source_sha256。命中足以回答时停止，不再读取全文；有上下文缺口时再调用 read_product_material。"""
        with session(readonly=True) as service:
            return {"results": service.search(query, product, domain, category, limit),
                    "scope": "local_development", "corpus": "product_materials"}

    @server.tool(title="按修订读取产品资料")
    @tool_errors
    def read_product_material(material_id: str, revision: int, source_sha256: str,
                              section_ids: list[str] | None = None,
                              full_document: bool = False) -> dict:
        """使用检索命中的精确 material_id、revision、source_sha256 读取。默认只返回目录；证据缺口时只传相关 section_ids。full_document 仅用于整体理解/跨章节关系或定向补读仍不足时。"""
        with session(readonly=True) as service:
            return service.read(material_id, revision, source_sha256, section_ids, full_document)

    @server.tool(title="导出产品资料原件")
    @tool_errors
    def product_material_export_original(material_id: str, revision: int,
                                         source_sha256: str, out_path: str) -> dict:
        """按精确资料修订和 SHA-256 将受管原始 Word 文件导出到用户提供的本机绝对路径，并校验导出哈希。仅在用户明确需要原件时调用。"""
        with session(readonly=True) as service:
            return service.export_original(material_id, revision, source_sha256, out_path)

    @server.tool(title="撤回产品资料修订")
    @tool_errors
    def withdraw_product_material(material_id: str, revision: int, source_sha256: str) -> dict:
        """按精确产品资料修订撤出默认检索，保留原件和历史版本；重复调用不恢复索引。"""
        with session() as service:
            return service.withdraw(material_id, revision, source_sha256)
