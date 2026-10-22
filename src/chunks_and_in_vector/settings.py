from __future__ import annotations
from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized runtime configuration.

    Notes:
    - Read env on instantiation (not at import time).
    - Keep defaults conservative for security; override via env/.env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Milvus
    milvus_host: str = Field(default="127.0.0.1", alias="MILVUS_HOST")
    milvus_port: int = Field(default=19530, alias="MILVUS_PORT")
    milvus_index_preset: str = Field(
        default="ivf_flat",
        alias="MILVUS_INDEX_PRESET",
        description="索引策略: flat(小数据量), ivf_flat(中等), hnsw(大数据量)",
    )

    # Collections
    collection_name_page: str = "CorpSys_Page"
    collection_name_paragraph: str = "CorpSys_Paragraph"
    collection_name_sentence: str = "CorpSys_Sentence"

    # Embedding
    embedding_dimension: int = 1024
    embedding_batch_size: int = 8

    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )
    qwen_model_name: str = Field(default="text-embedding-v3", alias="QWEN_MODEL_NAME")
    qwen_encoding_format: str = "float"

    # CORS
    cors_allow_origins: List[str] = Field(
        default_factory=lambda: ["http://localhost:8001", "http://127.0.0.1:8001"],
        alias="CORS_ALLOW_ORIGINS",
        description="Allowed origins. Set to ['*'] only if you really want allow-all.",
    )

    # Ingestion
    enable_incremental: bool = True
    data_dir: str = Field(default="data/documents", alias="DATA_DIR")
    batch_size: int = Field(default=100, alias="BATCH_SIZE")
    file_extensions: List[str] = Field(
        default_factory=lambda: [".docx", ".doc"],
        alias="FILE_EXTENSIONS",
    )
    paragraph_min_length: int = Field(default=20, alias="PARAGRAPH_MIN_LENGTH")
    sentence_min_length: int = Field(default=10, alias="SENTENCE_MIN_LENGTH")
    checkpoint_file: str = Field(default="checkpoint.json", alias="CHECKPOINT_FILE")

    @property
    def effective_checkpoint_file(self) -> str:
        return self.checkpoint_file

    # 腾讯云OCR文档抽取API密钥（前往 https://console.cloud.tencent.com/cam/capi 获取）
    tencent_ocr_secret_id: str = Field(default="", alias="TENCENT_OCR_SECRET_ID", description="腾讯云 SecretId")
    tencent_ocr_secret_key: str = Field(default="", alias="TENCENT_OCR_SECRET_KEY", description="腾讯云 SecretKey")
    tencent_ocr_region: str = Field(default="ap-guangzhou", alias="TENCENT_OCR_REGION", description="腾讯云地域，如 ap-guangzhou（广州）、ap-shanghai（上海）等")

    @property
    def collection_names(self) -> dict[str, str]:
        return {
            "page": self.collection_name_page,
            "paragraph": self.collection_name_paragraph,
            "sentence": self.collection_name_sentence,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Cache for process lifetime; tests can clear via get_settings.cache_clear()
    return Settings()
