"""
通用文本向量化模块（OpenAI 兼容接口）
- 当前对接：通义千问 DashScope / text-embedding-v3（OpenAI 兼容模式）
- 配置来源：项目根目录 .env（QWEN_API_KEY / QWEN_BASE_URL / QWEN_MODEL_NAME / QWEN_ENCODING_FORMAT）
- 复用点：实现了 embed_documents / embed_query（同步）与 aembed_documents / aembed_query（异步），
  可直接喂给 src/chunks_and_in_vector/milvus_db.py 的 MilvusDB。
"""
from __future__ import annotations

import os
import logging
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI
from langchain_core.embeddings import Embeddings
from .chunking import split_section, validate_embedding_texts

logger = logging.getLogger(__name__)


def _project_root() -> str:
    # embedding.py 位于 <root>/src/chunks_and_in_vector/embedding.py
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class EmbeddingModel(Embeddings):
    """通用、可复用的文本向量化类（OpenAI 兼容接口）。

    用法::

        emb = EmbeddingModel()                 # 自动从根目录 .env 读取配置
        vecs = emb.embed_documents(["文本1", "文本2"])
        q = emb.embed_query("查询")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        encoding_format: str = "float",
        batch_size: int = 8,
        dimension: Optional[int] = None,
        timeout: float = 60.0,
    ):
        # 1) 显式加载根目录 .env（不依赖运行时的 cwd）
        load_dotenv(os.path.join(_project_root(), ".env"))

        self.api_key = api_key or os.getenv("QWEN_API_KEY")
        self.base_url = base_url or os.getenv(
            "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.model = model or os.getenv("QWEN_MODEL_NAME", "text-embedding-v3")
        self.encoding_format = encoding_format or os.getenv("QWEN_ENCODING_FORMAT", "float")
        self.batch_size = max(1, batch_size)
        self.dimension = dimension  # 仅作元信息，便于调用方核对 Milvus 维度

        if not self.api_key:
            raise ValueError("未找到 QWEN_API_KEY，请在项目根目录 .env 中配置。")

        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)
        self._aclient = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)
        logger.info("向量化模型初始化: model=%s base_url=%s", self.model, self.base_url)

    def split_section(self, heading: str, content: str):
        """Split one section using the conservative UTF-8 byte-bound policy."""
        return split_section(heading, content)

    # ---------- 同步 ----------
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        validate_embedding_texts(texts)
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self._client.embeddings.create(
                model=self.model,
                input=batch,
                encoding_format=self.encoding_format,
                **({"dimensions": self.dimension} if self.dimension else {}),
            )
            items = resp.data
            if items and hasattr(items[0], "index"):
                items = sorted(items, key=lambda d: d.index)
            out.extend(d.embedding for d in items)
        logger.info("向量化完成: %d 条文本", len(texts))
        return out

    def embed_query(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(
            model=self.model,
            input=[text],
            encoding_format=self.encoding_format,
                **({"dimensions": self.dimension} if self.dimension else {}),
        )
        return resp.data[0].embedding

    # ---------- 异步（可选，便于对接 MilvusDB.ainsert） ----------
    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = await self._aclient.embeddings.create(
                model=self.model,
                input=batch,
                encoding_format=self.encoding_format,
                **({"dimensions": self.dimension} if self.dimension else {}),
            )
            items = resp.data
            if items and hasattr(items[0], "index"):
                items = sorted(items, key=lambda d: d.index)
            out.extend(d.embedding for d in items)
        return out

    async def aembed_query(self, text: str) -> List[float]:
        resp = await self._aclient.embeddings.create(
            model=self.model,
            input=[text],
            encoding_format=self.encoding_format,
                **({"dimensions": self.dimension} if self.dimension else {}),
        )
        return resp.data[0].embedding


# 兼容老命名习惯
QwenEmbedding = EmbeddingModel
