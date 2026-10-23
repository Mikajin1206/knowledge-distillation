"""Controlled lifecycle stages and suggested document types."""
from __future__ import annotations


STAGE_COLLECTIONS = {
    "立项与范围": "cb_initiation_scope",
    "调研与需求": "cb_research_requirements",
    "方案与设计": "cb_solution_design",
    "实施与变更": "cb_implementation_change",
    "测试与验收": "cb_test_acceptance",
    "运维与改进": "cb_operations_improvement",
}
STAGES = tuple(STAGE_COLLECTIONS)

# Product materials form a separate logical corpus, routed within the same MCP
# service and sandbox RAG deployment. They do not belong to project lifecycle
# stages and therefore have their own controlled collections.
PRODUCT_MATERIAL_COLLECTIONS = {
    "产品手册": "cb_product_manuals",
    "培训资料": "cb_training_materials",
}
PRODUCT_MATERIAL_CATEGORIES = tuple(PRODUCT_MATERIAL_COLLECTIONS)
PRODUCT_MATERIAL_INGEST_CATEGORIES = ("产品手册",)

# These are client suggestions only. knowledge_type remains a free-form
# document attribute and does not select a RAG collection.
DOCUMENT_TYPE_OPTIONS = (
    "方案设计", "调研报告", "概要设计", "开发功能说明书",
)


def validate_stage(value: object) -> str:
    """Normalize and validate the RAG primary classification."""
    normalized = value.strip() if isinstance(value, str) else ""
    if normalized not in STAGE_COLLECTIONS:
        if normalized:
            raise ValueError(
                f"未知项目阶段 {normalized!r}；可选值：{'、'.join(STAGES)}"
            )
        raise ValueError(f"项目阶段不能为空；可选值：{'、'.join(STAGES)}")
    return normalized


def validate_product_material_category(value: object) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if normalized not in PRODUCT_MATERIAL_COLLECTIONS:
        raise ValueError(
            f"未知产品资料类别 {normalized!r}；可选值：{'、'.join(PRODUCT_MATERIAL_CATEGORIES)}"
        )
    return normalized


def validate_product_material_ingest_category(value: object) -> str:
    normalized = validate_product_material_category(value)
    if normalized not in PRODUCT_MATERIAL_INGEST_CATEGORIES:
        raise ValueError("当前版本仅接受规范产品手册；培训资料暂不支持，已列入后续迭代")
    return normalized
