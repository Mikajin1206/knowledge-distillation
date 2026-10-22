"""
MongoDB 通用操作类
- 配置来源：项目根目录 .env
  （MongoDB_USERNAME / MongoDB_PASSWORD / MongoDB_HOST / MongoDB_PORT / MongoDB_DB_NAME）
- 与具体集合结构解耦：传入集合名 + dict 文档即可 CRUD。
- 兼容 .env 里 `KEY = "value"` 带空格/引号的写法（见 _env 处理）。
"""
from __future__ import annotations

import os
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

logger = logging.getLogger(__name__)


def _project_root() -> str:
    # 与 embedding.py / milvus_db.py 同目录，反推三级到项目根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(name: str, default: str = "") -> str:
    """读取环境变量，并去除首尾空格/引号，兼容 `KEY = "value"` 写法。"""
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    if len(v) >= 2 and v[0] in ("'", '"') and v[-1] == v[0]:
        v = v[1:-1]
    return v


class MongoDB:
    def __init__(
        self,
        db_name: Optional[str] = None,
        *,
        host: Optional[str] = None,
        port: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_source: Optional[str] = None,
        uri: Optional[str] = None,
    ):
        load_dotenv(os.path.join(_project_root(), ".env"))

        self.db_name = db_name or _env("MongoDB_DB_NAME", "test")
        self.host = host or _env("MongoDB_HOST", "localhost")
        self.port = port or _env("MongoDB_PORT", "27017")
        # 允许显式传空字符串覆盖 .env；否则取 .env
        self.username = username if username is not None else _env("MongoDB_USERNAME", "")
        self.password = password if password is not None else _env("MongoDB_PASSWORD", "")
        # 用户通常在自己的业务库下创建，默认 authSource=db_name；若建在 admin 下请传 auth_source="admin"
        self.auth_source = auth_source or self.db_name
        self.uri = uri

        self._client: Optional[MongoClient] = None
        self._db: Optional[Database] = None
        self._connected = False

    # ---------- 连接 ----------
    def connect(self) -> "MongoDB":
        if self._connected:
            return self
        try:
            if self.uri:
                self._client = MongoClient(self.uri)
            elif self.username:
                self._client = MongoClient(
                    host=self.host,
                    port=int(self.port),
                    username=self.username,
                    password=self.password,
                    authSource=self.auth_source,
                )
            else:
                self._client = MongoClient(host=self.host, port=int(self.port))

            self._client.admin.command("ping")  # 尽早暴露鉴权/网络错误
            self._db = self._client[self.db_name]
            self._connected = True
            logger.info("已连接到 MongoDB: %s:%s / db=%s", self.host, self.port, self.db_name)
        except Exception as e:
            logger.error("连接 MongoDB 失败: %s", e)
            raise
        return self

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._connected = False
        logger.info("已关闭 MongoDB 连接")

    def __enter__(self) -> "MongoDB":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 集合 ----------
    def get_collection(self, name: str) -> Collection:
        if not self._connected:
            raise RuntimeError("请先调用 connect()")
        return self._db[name]

    # ---------- 写入 ----------
    def insert_one(self, collection: str, document: Dict[str, Any]) -> str:
        return str(self.get_collection(collection).insert_one(document).inserted_id)

    def insert_many(self, collection: str, documents: Iterable[Dict[str, Any]]) -> List[str]:
        return [str(i) for i in self.get_collection(collection).insert_many(documents).inserted_ids]

    # ---------- 查询 ----------
    def find(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        limit: int = 0,
        sort: Optional[List[Tuple[str, int]]] = None,
    ) -> List[Dict[str, Any]]:
        cur = self.get_collection(collection).find(filter or {}, projection)
        if sort:
            cur = cur.sort(sort)
        if limit > 0:
            cur = cur.limit(limit)
        return list(cur)

    def find_one(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.get_collection(collection).find_one(filter or {}, projection)

    def count(self, collection: str, filter: Optional[Dict[str, Any]] = None) -> int:
        return self.get_collection(collection).count_documents(filter or {})

    # ---------- 更新 ----------
    def update_one(self, collection: str, filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> int:
        return self.get_collection(collection).update_one(filter, update, upsert=upsert).modified_count

    def update_many(self, collection: str, filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> int:
        return self.get_collection(collection).update_many(filter, update, upsert=upsert).modified_count

    def upsert_one(self, collection: str, filter: Dict[str, Any], update: Dict[str, Any]) -> int:
        return self.update_one(collection, filter, update, upsert=True)

    # ---------- 删除 ----------
    def delete_one(self, collection: str, filter: Dict[str, Any]) -> int:
        return self.get_collection(collection).delete_one(filter).deleted_count

    def delete_many(self, collection: str, filter: Dict[str, Any]) -> int:
        return self.get_collection(collection).delete_many(filter).deleted_count

    # ---------- 索引 / 聚合 ----------
    def create_index(self, collection: str, keys, unique: bool = False, **kwargs):
        return self.get_collection(collection).create_index(keys, unique=unique, **kwargs)

    def aggregate(self, collection: str, pipeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return list(self.get_collection(collection).aggregate(pipeline))

    @property
    def client(self) -> MongoClient:
        if self._client is None:
            raise RuntimeError("请先调用 connect()")
        return self._client

    @property
    def db(self) -> Database:
        if self._db is None:
            raise RuntimeError("请先调用 connect()")
        return self._db
