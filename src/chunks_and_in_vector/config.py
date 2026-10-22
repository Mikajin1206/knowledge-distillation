"""
公司制度知识库统一配置文件
"""
from __future__ import annotations
from dataclasses import dataclass
from .settings import get_settings


@dataclass(frozen=True)
class CorpKnowledgeConfig:
    """
    Backward-compatible facade over `Settings`.
    Prefer injecting `Settings` (via `get_settings()`) in new code.
    """

    @property
    def MILVUS_HOST(self) -> str:
        return get_settings().milvus_host

    @property
    def MILVUS_PORT(self) -> str:
        return str(get_settings().milvus_port)

    @property
    def MILVUS_INDEX_PRESET(self) -> str:
        return get_settings().milvus_index_preset

    @property
    def COLLECTION_NAMES(self) -> dict[str, str]:
        return get_settings().collection_names

    @property
    def COLLECTION_NAME_PAGE(self) -> str:
        return get_settings().collection_names["page"]

    @property
    def COLLECTION_NAME_PARAGRAPH(self) -> str:
        return get_settings().collection_names["paragraph"]

    @property
    def COLLECTION_NAME_SENTENCE(self) -> str:
        return get_settings().collection_names["sentence"]

    @property
    def EMBEDDING_DIMENSION(self) -> int:
        return get_settings().embedding_dimension

    @property
    def ENABLE_INCREMENTAL(self) -> bool:
        return get_settings().enable_incremental

    @property
    def DATA_DIR(self) -> str:
        return get_settings().data_dir

    @property
    def BATCH_SIZE(self) -> int:
        return get_settings().batch_size

    @property
    def FILE_EXTENSIONS(self) -> list[str]:
        return get_settings().file_extensions

    @property
    def PARAGRAPH_MIN_LENGTH(self) -> int:
        return get_settings().paragraph_min_length

    @property
    def SENTENCE_MIN_LENGTH(self) -> int:
        return get_settings().sentence_min_length

    @property
    def CHECKPOINT_FILE(self) -> str:
        return get_settings().effective_checkpoint_file


class QwenEmbeddingConfig:
    @classmethod
    def get_config(cls):
        s = get_settings()
        if not s.qwen_api_key:
            raise ValueError("QWEN_API_KEY 环境变量未设置，请在环境中配置 API Key")
        return {
            "api_key": s.qwen_api_key,
            "base_url": s.qwen_base_url,
            "model": s.qwen_model_name,
            "dimension": s.embedding_dimension,
            "batch_size": s.embedding_batch_size,
            "encoding_format": s.qwen_encoding_format,
        }


AVAILABLE_MODELS = {
    "text-embedding-v1": {
        "name": "text-embedding-v1",
        "dimension": 1536,
        "description": "通义千问文本向量模型 v1",
    },
    "text-embedding-v2": {
        "name": "text-embedding-v2",
        "dimension": 1536,
        "description": "通义千问文本向量模型 v2",
    },
    "text-embedding-v3": {
        "name": "text-embedding-v3",
        "dimension": 1024,
        "description": "通义千问文本向量模型 v3（推荐）",
    },
    "text-embedding-async-v1": {
        "name": "text-embedding-async-v1",
        "dimension": 1536,
        "description": "通义千问异步文本向量模型 v1",
    },
    "text-embedding-async-v2": {
        "name": "text-embedding-async-v2",
        "dimension": 1536,
        "description": "通义千问异步文本向量模型 v2",
    },
    "text-embedding-async-v3": {
        "name": "text-embedding-async-v3",
        "dimension": 1024,
        "description": "通义千问异步文本向量模型 v3",
    },
}
