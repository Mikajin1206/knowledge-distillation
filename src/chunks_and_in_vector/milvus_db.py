"""
Milvus 向量库通用操作类
- 与具体 embedding 实现解耦：只需传入对象具备
  embed_documents(list[str]) / embed_query(str)（及可选 aembed_* 异步方法）。
- 默认集合结构：id / text / metadata / file_name / score(FLOAT) / level / parent_id / embedding。
- 默认度量：COSINE。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import math
from typing import Any, Dict, List, Optional

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)
from langchain_core.documents import Document

logger = logging.getLogger(__name__)


INDEX_PRESETS: Dict[str, Dict[str, Any]] = {
    "flat": {"index_type": "FLAT", "params": {}},
    "ivf_flat": {"index_type": "IVF_FLAT", "params": {"nlist": 128}},
    "hnsw": {"index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}},
}

SEARCH_PARAM_PRESETS: Dict[str, Dict[str, Any]] = {
    "flat": {},
    "ivf_flat": {"nprobe": 16},
    "hnsw": {"ef": 64},
}

# 仅列出真实存储字段
_OUTPUT_FIELDS = [
    "id", "text", "metadata", "file_name", "score"
]

class MilvusDB:
    def __init__(
        self,
        collection_name: str,
        embedding: Any = None,
        host: str = "localhost",
        port: str = "19530",
        uri: Optional[str] = None,
        token: Optional[str] = None,
        dimension: Optional[int] = None,
        *,
        alias: str = "default",
        index_preset: str = "ivf_flat",
        metric_type: str = "COSINE",
        fields: Optional[List[FieldSchema]] = None,
        id_prefix: str = "",
    ):
        self.collection_name = collection_name
        self.embedding = embedding
        self.host = host
        self.port = port
        self.uri = uri
        self.token = token
        self.alias = alias
        self.index_preset = index_preset
        self.metric_type = metric_type.upper()
        self.id_prefix = id_prefix

        # 维度：显式传入 > embedding 自带 > 默认 1024
        self.dimension = dimension or getattr(embedding, "dimension", None) or 1024
        self.fields = fields or self._default_fields()
        self._collection: Optional[Collection] = None
        self._connected = False

    # ---------- 集合结构 ----------
    def _default_fields(self) -> List[FieldSchema]:
        return [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=65535, is_primary=True),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="score", dtype=DataType.FLOAT),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
        ]

    # ---------- 连接 ----------
    def connect(self) -> None:
        try:
            if self.uri:
                connections.connect(alias=self.alias, uri=self.uri, token=self.token)
            else:
                connections.connect(alias=self.alias, host=self.host, port=self.port)
            self._connected = True
            logger.info("已连接到 Milvus: %s", self.uri or f"{self.host}:{self.port}")
        except Exception as e:
            logger.error("连接 Milvus 失败: %s", e)
            raise

    def close(self) -> None:
        if self._connected:
            connections.disconnect(self.alias)
            self._connected = False
            logger.info("已断开 Milvus 连接")

    def __enter__(self) -> "MilvusDB":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 集合管理 ----------
    def create_collection(self, drop_if_exists: bool = False) -> None:
        if not self._connected:
            raise RuntimeError("请先调用 connect() 方法")

        if utility.has_collection(self.collection_name, using=self.alias):
            if drop_if_exists:
                utility.drop_collection(self.collection_name, using=self.alias)
                logger.info("已删除现有集合: %s", self.collection_name)
            else:
                self._collection = Collection(self.collection_name, using=self.alias)
                self._collection.load()
                logger.info("集合已存在: %s", self.collection_name)
                return

        schema = CollectionSchema(
            fields=self.fields,
            description="通用知识库向量集合",
            enable_dynamic_field=True,
        )
        self._collection = Collection(name=self.collection_name, schema=schema, using=self.alias, consistency_level="Strong")
        self._collection.create_index(field_name="embedding", index_params=self._index_params())
        self._collection.load()
        logger.info(
            "集合创建成功: %s (索引: %s, 度量: %s)",
            self.collection_name, self.index_preset, self.metric_type,
        )

    def _index_params(self) -> Dict[str, Any]:
        preset = INDEX_PRESETS.get(self.index_preset, INDEX_PRESETS["ivf_flat"])
        return {"index_type": preset["index_type"], "metric_type": self.metric_type, "params": preset["params"]}

    def _search_params(self) -> Dict[str, Any]:
        return {
            "metric_type": self.metric_type,
            "params": SEARCH_PARAM_PRESETS.get(self.index_preset, SEARCH_PARAM_PRESETS["ivf_flat"]),
        }

    # ---------- 写入 ----------
    @staticmethod
    def _extract_metadata_field(doc: Document, field: str, default: str = "") -> str:
        val = doc.metadata.get(field, default)
        return str(val) if val is not None else default

    @staticmethod
    def _extract_score(doc: Document) -> float:
        raw = doc.metadata.get("score", 0.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _build_columns(self, documents, ids):
        texts = [d.page_content for d in documents]
        # 保留完整版本、来源与章节元数据供回读追溯。
        metadatas = [
            json.dumps(
                d.metadata,
                ensure_ascii=False,
            )
            for d in documents
        ]
        file_names = [self._extract_metadata_field(d, "file_name") for d in documents]
        scores = [self._extract_score(d) for d in documents]
        if self.embedding is not None:
            if hasattr(self.embedding, "embed_documents"):
                embeddings = self.embedding.embed_documents(texts)
            else:
                embeddings = [self.embedding.embed_query(t) for t in texts]
        else:
            raise ValueError("必须配置 embedding，禁止用零向量替代真实向量")
        if len(embeddings) != len(documents) or any(
            len(v) != self.dimension or not all(math.isfinite(x) for x in v) for v in embeddings
        ):
            raise ValueError("embedding 数量、维度或数值无效")
        for values, maximum in ((texts, 65535), (metadatas, 65535), (file_names, 512)):
            if any(len(value.encode("utf-8")) > maximum for value in values):
                raise ValueError("文本或元数据超过 Milvus UTF-8 字节限制")
        return [ids, texts, metadatas, file_names, scores, embeddings]

    def insert(self, documents: List[Document], ids: Optional[List[str]] = None) -> List[str]:
        if not self._collection:
            raise RuntimeError("集合未初始化，请先 create_collection()")
        if ids is not None and len(ids) != len(documents):
            raise ValueError("ids 与 documents 长度不一致")
        ids = ids or [f"{self.id_prefix}{uuid.uuid4()}" for _ in documents]
        try:
            self._collection.upsert(self._build_columns(documents, ids))
            self._collection.flush()
            logger.info("成功插入 %d 个文档到集合 %s", len(documents), self.collection_name)
            return ids
        except Exception as e:
            logger.error("插入失败: %s", e)
            raise

    def add_texts(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        *,
        file_name: str = "",
        level: str = "",
        parent_id: str = "",
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """不依赖 langchain Document 的便捷写入接口。"""
        metadatas = metadatas or [{} for _ in texts]
        docs = [
            Document(
                page_content=t,
                metadata={
                    **md,
                    "file_name": md.get("file_name", file_name),
                    "level": md.get("level", level),
                    "parent_id": md.get("parent_id", parent_id),
                },
            )
            for t, md in zip(texts, metadatas)
        ]
        return self.insert(docs, ids=ids)

    async def ainsert(self, documents: List[Document], ids: Optional[List[str]] = None) -> List[str]:
        return await asyncio.to_thread(self.insert, documents, ids)

    # ---------- 检索 ----------
    def _parse_hits(self, hits) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for hit in hits[0]:
            raw_meta = hit.entity.get("metadata")
            meta = json.loads(raw_meta) if raw_meta else {}
            results.append({
                "id": hit.id,
                "text": hit.entity.get("text"),
                "metadata": meta,  # 仅 {"url": ...}
                "file_name": hit.entity.get("file_name"),
                "score": hit.score,
                "distance": hit.distance,
            })
        return results

    def _apply_threshold(self, results: List[Dict[str, Any]], score_threshold: Optional[float]) -> List[Dict[str, Any]]:
        if score_threshold is None:
            return results
        if self.metric_type in ("L2", "L1"):
            return [r for r in results if r["distance"] <= score_threshold]
        return [r for r in results if r["score"] >= score_threshold]

    def search(
        self,
        query: str,
        k: int = 5,
        score_threshold: Optional[float] = None,
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        if self.embedding is None:
            raise RuntimeError("未配置 embedding 模型")
        query_embedding = self.embedding.embed_query(query)
        hits = self._collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param=self._search_params(),
            limit=k,
            expr=filter_expr,
            output_fields=_OUTPUT_FIELDS,
            consistency_level="Strong",
        )
        return self._apply_threshold(self._parse_hits(hits), score_threshold)

    async def asearch(
        self,
        query: str,
        k: int = 5,
        score_threshold: Optional[float] = None,
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        if self.embedding is None:
            raise RuntimeError("未配置 embedding 模型")
        if hasattr(self.embedding, "aembed_query"):
            qe = await self.embedding.aembed_query(query)
        else:
            qe = await asyncio.to_thread(self.embedding.embed_query, query)

        def _run():
            return self._collection.search(
                data=[qe],
                anns_field="embedding",
                param=self._search_params(),
                limit=k,
                expr=filter_expr,
                output_fields=_OUTPUT_FIELDS,
            )

        hits = await asyncio.to_thread(_run)
        return self._apply_threshold(self._parse_hits(hits), score_threshold)

    # ---------- 其他操作 ----------
    def query(self, filter_expr: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        return self._collection.query(expr=filter_expr, limit=limit, output_fields=_OUTPUT_FIELDS)

    def query_by_ids(self, ids: List[str]) -> List[Dict[str, Any]]:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        if not ids:
            return []
        try:
            quoted = [f'"{str(i).replace(chr(34), chr(92) + chr(34))}"' for i in ids]
            expr = f'id in [{", ".join(quoted)}]'
            return self._collection.query(expr=expr, limit=len(ids), output_fields=_OUTPUT_FIELDS)
        except Exception as e:
            logger.error("按ID查询失败: %s", e)
            return []

    async def aquery_by_ids(self, ids: List[str]) -> List[Dict[str, Any]]:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        if not ids:
            return []
        try:
            quoted = [f'"{str(i).replace(chr(34), chr(92) + chr(34))}"' for i in ids]
            expr = f'id in [{", ".join(quoted)}]'
            return await asyncio.to_thread(
                self._collection.query, expr=expr, limit=len(ids), output_fields=_OUTPUT_FIELDS
            )
        except Exception as e:
            logger.error("按ID查询失败: %s", e)
            return []

    def delete(self, ids: List[str]) -> bool:
        if not self._collection:
            raise RuntimeError("集合未初始化")
        if not ids:
            return True
        try:
            quoted = [f'"{str(i).replace(chr(34), chr(92) + chr(34))}"' for i in ids]
            expr = f'id in [{", ".join(quoted)}]'
            self._collection.delete(expr)
            self._collection.flush()
            logger.info("成功删除 %d 个文档", len(ids))
            return True
        except Exception as e:
            logger.error("删除失败: %s", e)
            return False

    def get_collection(self) -> Optional[Collection]:
        return self._collection
