"""MCP tools for the product-material corpus on the existing MCP server."""
from contextlib import contextmanager
import json

from mcp.server.mcpserver import Image

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
        """将用户明确指定的单个规范产品手册 Word 原件直接解析、切片并写入本地开发 RAG。当前仅接受 category=“产品手册”；“培训资料”暂不支持，传入时立即拒绝。product 必须单选，domain 从该产品目录中选择至少一个。stage 始终为空字符串，不归属项目生命周期六阶段。与项目无关；保留原件，不做蒸馏、草稿或审核。重复内容幂等；同名新内容建立新修订。返回原件哈希、分节/切片数、逐块读回核验数、未做 OCR 的图片数和章节质量提示。自动目录只用于和正文标题编号交叉校验，不参与正文；编号不一致时保留原文并返回质量提示，不自动纠正。state=unchanged 时计数和质量提示来自首次成功入库记录；verification_basis=original_ingest 且 readback_performed_this_call=false 表示本次未重新读回核验。"""
        with session() as service:
            return service.ingest(path, category, product, domain)

    @server.tool(title="列出产品资料")
    @tool_errors
    def product_material_list(product: str = "", category: str = "") -> dict:
        """列出已登记的产品手册和培训资料，可按产品或类别筛选；stage 始终为空字符串。不读原件、不连接向量服务。"""
        with session(readonly=True) as service:
            materials = service.list(product, category)
            return {"materials": materials, "count": len(materials),
                    "categories": ["产品手册", "培训资料"], "sandbox": True}

    @server.tool(title="检索产品资料")
    @tool_errors
    def search_product_materials(query: str, product: str = "", domain: list[str] | None = None,
                                 category: str = "", limit: int = 5) -> dict:
        """仅检索产品资料逻辑库，不混入项目生命周期档案。stage 始终为空字符串。支持产品、领域和类别过滤；正式图号查询按映射精确定位分节，不依赖向量语义召回。返回命中分节及精确 material_id/revision/source_sha256。命中足以回答时停止，不再读取全文；有上下文缺口时再调用 read_product_material。"""
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

    @server.tool(title="按图号读取产品手册原图", structured_output=False)
    @tool_errors
    def read_product_material_image(material_id: str, revision: int, source_sha256: str,
                                    figure_id: str) -> list:
        """按精确资料修订、原件哈希和正式图号返回 Word 内嵌原图。仅产品手册正文中符合“图5.2-01”形式且能唯一定位的图片可取回；不做 OCR、图片理解或描述，不另存图片副本。"""
        with session(readonly=True) as service:
            payload, image_format, context = service.read_image(
                material_id, revision, source_sha256, figure_id)
        return [Image(data=payload, format=image_format), json.dumps(context, ensure_ascii=False)]

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
