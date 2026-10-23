"""SQLite 连接、表结构、迁移与通用小工具。

单用户本地 CLI 场景，短事务 + WAL；多进程并发写入不在本轮范围内。
只读连接（readonly=True）不写磁盘；数据库不存在时，在内存中建立空结构供查询。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  industry TEXT NOT NULL DEFAULT '[]',
  products TEXT NOT NULL DEFAULT '[]',
  owner TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  file_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  name TEXT NOT NULL,
  stage TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  current_version_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_versions (
  version_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES files(file_id),
  seq INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  filename TEXT NOT NULL,
  doc_meta_json TEXT NOT NULL DEFAULT '{}',
  doc_meta_sources_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE (file_id, seq)
);
CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  rel_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_materials (
  material_id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  product TEXT NOT NULL,
  title TEXT NOT NULL,
  current_version_id TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(category, product, title)
);
CREATE TABLE IF NOT EXISTS product_material_versions (
  version_id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL REFERENCES product_materials(material_id),
  revision INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  filename TEXT NOT NULL,
  domains_json TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  section_count INTEGER NOT NULL,
  image_count INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'stored',
  created_at TEXT NOT NULL,
  UNIQUE(material_id, revision)
);
CREATE TABLE IF NOT EXISTS upload_records (
  upload_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  uploader TEXT,
  source_alias TEXT,
  stage TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS distill_tasks (
  task_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  rules_version TEXT NOT NULL,
  rules_hash TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_materials (
  material_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES distill_tasks(task_id),
  file_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  filename TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  attached_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_gaps (
  gap_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES distill_tasks(task_id),
  description TEXT NOT NULL,
  declared_at TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_inputs (
  input_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES distill_tasks(task_id),
  provider TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_office_scans (
  task_id TEXT NOT NULL REFERENCES distill_tasks(task_id),
  file_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, file_id)
);
CREATE TABLE IF NOT EXISTS drafts (
  knowledge_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  title TEXT NOT NULL,
  knowledge_type TEXT NOT NULL,
  products TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '[]',
  topics TEXT NOT NULL DEFAULT '[]',
  body_md TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  gaps_json TEXT NOT NULL DEFAULT '[]',
  human_inputs_json TEXT NOT NULL DEFAULT '[]',
  section_check_json TEXT NOT NULL DEFAULT '{}',
  quality_json TEXT NOT NULL DEFAULT '{}',
  quality_report_json TEXT NOT NULL DEFAULT '{}',
  model TEXT,
  rules_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  PRIMARY KEY (knowledge_id, revision)
);
CREATE TABLE IF NOT EXISTS request_log (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_reviews (
  review_id TEXT PRIMARY KEY,
  knowledge_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  reviewer TEXT,
  comments TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  UNIQUE(knowledge_id, revision, snapshot_hash)
);
CREATE TABLE IF NOT EXISTS publish_tasks (
  publish_task_id TEXT PRIMARY KEY,
  knowledge_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  review_id TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(knowledge_id, revision, snapshot_hash)
);
CREATE TABLE IF NOT EXISTS knowledge_asset_refs (
  knowledge_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  asset_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY(knowledge_id, revision, asset_id)
);
"""

# 既有数据库的列补充；新库由 SCHEMA 直接建出。
_MIGRATIONS = (
    ("file_versions", "doc_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("file_versions", "doc_meta_sources_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("upload_records", "stage", "TEXT"),
    ("upload_records", "tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("drafts", "gaps_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("drafts", "human_inputs_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("drafts", "products", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "domain", "TEXT NOT NULL DEFAULT '[]'"),
    ("drafts", "section_check_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("drafts", "quality_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("drafts", "quality_report_json", "TEXT NOT NULL DEFAULT '{}'"),
)

# 常见办公格式在部分系统的 mimetypes 表中缺失，这里补齐。
_EXTRA_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".vsdx": "application/vnd.ms-visio.drawing",
    ".md": "text/markdown",
}


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, definition in _MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    _migrate_product_material_hash_uniqueness(conn)
    conn.commit()


def _migrate_product_material_hash_uniqueness(conn: sqlite3.Connection) -> None:
    """Allow a source hash to have multiple immutable parser/index revisions."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_material_versions'"
    ).fetchone():
        return
    indexes = conn.execute("PRAGMA index_list(product_material_versions)").fetchall()
    constrained = False
    for index in indexes:
        if not index["unique"]:
            continue
        name = index["name"].replace('"', '""')
        columns = [row["name"] for row in conn.execute(f'PRAGMA index_info("{name}")')]
        if columns == ["material_id", "sha256"]:
            constrained = True
            break
    if not constrained:
        return

    conn.execute("ALTER TABLE product_material_versions RENAME TO product_material_versions_legacy")
    conn.execute(
        "CREATE TABLE product_material_versions ("
        "version_id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES product_materials(material_id),"
        "revision INTEGER NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, media_type TEXT NOT NULL,"
        "filename TEXT NOT NULL, domains_json TEXT NOT NULL, parser_version TEXT NOT NULL,"
        "section_count INTEGER NOT NULL, image_count INTEGER NOT NULL DEFAULT 0,"
        "state TEXT NOT NULL DEFAULT 'stored', created_at TEXT NOT NULL, UNIQUE(material_id, revision))"
    )
    conn.execute(
        "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
        "domains_json,parser_version,section_count,image_count,state,created_at) "
        "SELECT version_id,material_id,revision,sha256,size,media_type,filename,domains_json,parser_version,"
        "section_count,image_count,state,created_at FROM product_material_versions_legacy"
    )
    conn.execute("DROP TABLE product_material_versions_legacy")


def connect(data_dir: Path, readonly: bool = False) -> sqlite3.Connection:
    data_dir = Path(data_dir)
    db_path = data_dir / "ckp.sqlite3"
    if readonly:
        if not db_path.is_file():
            # 首次接入时，列表/查询入口应返回空集。用只存在于进程内的空库
            # 提供完整 schema，既不要求用户先做写操作，也不在查询时初始化磁盘数据。
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            _apply_schema(conn)
            conn.execute("PRAGMA query_only=ON")
            return conn
        conn = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_schema(conn)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def loads(text: str, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def rowdict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def rowdicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [rowdict(row) for row in rows]


def guess_media_type(filename: str) -> str:
    import mimetypes
    suffix = Path(filename).suffix.lower()
    if suffix in _EXTRA_MEDIA_TYPES:
        return _EXTRA_MEDIA_TYPES[suffix]
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or "application/octet-stream"


def payload_hash(operation: str, payload: Any) -> str:
    import hashlib
    return hashlib.sha256(f"{operation}\n{dumps(payload)}".encode("utf-8")).hexdigest()


def idempotent(conn: sqlite3.Connection, request_id: str | None, operation: str,
               payload: Any, fn, on_rollback=None) -> Any:
    """按 request_id 幂等执行写操作（R01：单一事务所有者）。

    - fn 内只做数据库写入，不得 commit；业务写入与 request_log 由本函数
      在同一事务中提交，任一步失败整体回滚，不留半套状态。
    - on_rollback：回滚后清理库外副作用（如规范快照目录）；本身需幂等。
    - 同号同载荷重放返回原结果；同号不同载荷报冲突。
    - 无 request_id 时同样由本函数提交一次（fn 不自行提交）。
    """
    single = request_id and str(request_id).strip()
    if not single:
        try:
            result = fn()
            conn.commit()
        except BaseException:
            conn.rollback()
            if on_rollback is not None:
                on_rollback()
            raise
        return result
    request_id = str(request_id).strip()
    digest = payload_hash(operation, payload)
    row = rowdict(conn.execute(
        "SELECT * FROM request_log WHERE request_id = ?", (request_id,)).fetchone())
    if row:
        if row["operation"] != operation or row["payload_hash"] != digest:
            raise ValueError(
                f"request_id 已被其他请求使用: {request_id}（{row['operation']}）；"
                "请更换 request_id 或核对请求内容")
        return loads(row["result_json"])
    try:
        result = fn()
        conn.execute(
            "INSERT INTO request_log (request_id, operation, payload_hash, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (request_id, operation, digest, dumps(result), now()))
        conn.commit()
    except BaseException:
        conn.rollback()
        if on_rollback is not None:
            on_rollback()
        raise
    return result
