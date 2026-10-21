"""原件保存与查找：项目、文件、版本、上传记录与内容寻址存储。

身份模型（与需求规格 4.1 对应）：
- project_id：稳定项目标识；文件归属唯一主项目。
- file_id：一个项目内的逻辑文件（同名新内容 → 同 file_id 的新版本）。
- version_id：一次内容版本；seq 为文件内版本序号。
- sha256：内容身份；跨项目相同内容复用同一存储对象，可按哈希互查，
  各项目的文件、上传记录与版本关系独立保留、不丢失。
- upload_record：每次上传行为独立留痕（含本次提交的 stage/tags 上下文）；
  重复上传不产生新版本也不改变文件当前分类，提交内容是否应用在响应中明确返回。

存储后端为本地内容寻址适配器（data/originals/<sha前2位>/<sha>）。
公司文件服务器接入时替换本层，适配面见 docs/05。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ckp import db
from ckp.intake import hash_file, inventory, now_iso, office_properties


class Store:
    def __init__(self, data_dir: Path, readonly: bool = False):
        self.data_dir = Path(data_dir).resolve()
        self.readonly = readonly
        self.blobs_dir = self.data_dir / "originals"
        self.conn = db.connect(self.data_dir, readonly=readonly)

    def _require_write(self) -> None:
        if self.readonly:
            raise ValueError("只读连接不能执行写操作；查询类命令请保持只读，写操作使用写命令")

    # ---------- 项目 ----------

    def create_project(self, name: str, industry: list[str] | None = None,
                       products: list[str] | None = None, owner: str | None = None,
                       tags: list[str] | None = None, notes: str = "") -> dict:
        self._require_write()
        name = (name or "").strip()
        if not name:
            raise ValueError("项目名称不能为空")
        existing = db.rowdict(self.conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)).fetchone())
        if existing:
            raise ValueError(f"项目名称已存在: {name}（{existing['project_id']}）")
        record = {
            "project_id": db.new_id("p"), "name": name,
            "industry": db.dumps(industry or []), "products": db.dumps(products or []),
            "owner": (owner or "").strip() or None, "tags": db.dumps(tags or []),
            "status": "active", "notes": notes or "", "created_at": now_iso(),
        }
        self.conn.execute(
            "INSERT INTO projects (project_id, name, industry, products, owner, tags, status, notes, created_at)"
            " VALUES (:project_id, :name, :industry, :products, :owner, :tags, :status, :notes, :created_at)",
            record)
        self.conn.commit()
        return self._project_out(record)

    def get_project(self, ref: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE project_id = ? OR name = ?",
            (ref, ref)).fetchone()
        record = db.rowdict(row)
        if not record:
            raise ValueError(f"项目不存在: {ref}")
        return self._project_out(record)

    def list_projects(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [self._project_out(r) for r in rows]

    @staticmethod
    def _project_out(record: dict) -> dict:
        out = dict(record)
        out["industry"] = db.loads(out["industry"], [])
        out["products"] = db.loads(out["products"], [])
        out["tags"] = db.loads(out["tags"], [])
        return out

    # ---------- 上传 ----------

    def upload(self, source: Path, project_ref: str, uploader: str | None = None,
               stage: str | None = None, tags: list[str] | None = None,
               source_alias: str | None = None) -> dict:
        """上传文件或目录；只保存与登记元数据，不做任何解析。"""
        self._require_write()
        project = self.get_project(project_ref)
        result = inventory(Path(source).expanduser())
        if not result["files"]:
            detail = "；".join(f"{item['reason']}:{item['relative']}"
                               for item in result["excluded"][:5]) or "来源为空"
            raise ValueError(
                f"没有可保存的文件（共 {result['total_files']} 个被排除：{detail}）")
        uploaded: list[dict] = []
        for item in result["files"]:
            entry = self._ingest_one(Path(item["path"]), item["relative"],
                                     item["sha256"], item["size"],
                                     project, uploader, stage, tags, source_alias)
            entry["relative"] = item["relative"]
            uploaded.append(entry)
        return {
            "project_id": project["project_id"],
            "project_name": project["name"],
            "uploaded": uploaded,
            "excluded": result["excluded"],
            "accepted_files": len(uploaded),
            "excluded_count": len(result["excluded"]),
            "note": "仅保存原件与元数据，未解析、未蒸馏、未索引；蒸馏仅支持 Word 文件 .doc/.docx",
        }

    def _ingest_one(self, path: Path, relative: str, sha256: str, size: int,
                    project: dict, uploader: str | None, stage: str | None,
                    tags: list[str] | None, source_alias: str | None) -> dict:
        self._store_blob(path, sha256, size)
        props, prop_sources = office_properties(path)
        now = now_iso()
        submitted_stage = (stage or "").strip() or None
        submitted_tags = tags or []
        cur = self.conn.cursor()
        existing = db.rowdict(cur.execute(
            "SELECT * FROM files WHERE project_id = ? AND name = ?",
            (project["project_id"], relative)).fetchone())
        if existing:
            file_id = existing["file_id"]
            current = db.rowdict(cur.execute(
                "SELECT * FROM file_versions WHERE version_id = ?",
                (existing["current_version_id"],)).fetchone())
            if current and current["sha256"] == sha256:
                action = "reused"
                version_id = current["version_id"]
            else:
                action = "new_version"
                seq = cur.execute("SELECT COALESCE(MAX(seq), 0) FROM file_versions WHERE file_id = ?",
                                  (file_id,)).fetchone()[0] + 1
                version_id = db.new_id("v")
                cur.execute(
                    "INSERT INTO file_versions (version_id, file_id, seq, sha256, size, media_type, filename,"
                    " doc_meta_json, doc_meta_sources_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (version_id, file_id, seq, sha256, size,
                     db.guess_media_type(relative), relative,
                     db.dumps(props), db.dumps(prop_sources), now))
                cur.execute("UPDATE files SET current_version_id = ? WHERE file_id = ?",
                            (version_id, file_id))
        else:
            action = "created"
            file_id = db.new_id("f")
            version_id = db.new_id("v")
            cur.execute(
                "INSERT INTO files (file_id, project_id, name, stage, tags, current_version_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, project["project_id"], relative, submitted_stage,
                 db.dumps(submitted_tags), version_id, now))
            cur.execute(
                "INSERT INTO file_versions (version_id, file_id, seq, sha256, size, media_type, filename,"
                " doc_meta_json, doc_meta_sources_json, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, file_id, sha256, size, db.guess_media_type(relative),
                 relative, db.dumps(props), db.dumps(prop_sources), now))
        cur.execute(
            "INSERT INTO upload_records (upload_id, project_id, file_id, version_id, sha256,"
            " uploader, source_alias, stage, tags, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (db.new_id("u"), project["project_id"], file_id, version_id, sha256,
             (uploader or "").strip() or None, (source_alias or "").strip() or None,
             submitted_stage, db.dumps(submitted_tags), now))
        self.conn.commit()
        # 分类策略：stage/tags 仅在文件首次创建时应用；重传提交的上下文
        # 记录在上传记录中并可查，不静默改变文件当前分类（docs/05 §3）。
        applied = action == "created"
        unchanged = (not applied and existing["stage"] == submitted_stage
                     and db.loads(existing["tags"], []) == submitted_tags)
        return {"action": action, "file_id": file_id, "version_id": version_id,
                "sha256": sha256, "size": size,
                "media_type": db.guess_media_type(relative),
                "upload_context": {
                    "stage": submitted_stage, "tags": submitted_tags,
                    "applied_to_file": applied,
                    "note": ("首次上传，已应用到文件分类" if applied else
                             ("与文件现有分类一致" if unchanged else
                              "仅记录在上传记录；文件保留原有分类，修改分类需显式更新接口（后续提供）")),
                },
                "office_metadata": props}

    def _store_blob(self, path: Path, sha256: str, size: int) -> Path:
        self._require_write()
        rel_path = f"originals/{sha256[:2]}/{sha256}"
        blob_path = self.data_dir / rel_path
        if blob_path.exists():
            # 复用前校验已有对象：损坏的对象不允许被计为保存成功（F09）。
            actual_size = blob_path.stat().st_size
            actual = hash_file(blob_path)
            if actual != sha256 or actual_size != size:
                raise ValueError(
                    f"存储对象已损坏，中止上传以保护数据完整性"
                    f"（sha256={sha256[:12]}…，登记 {size} 字节，磁盘对象 {actual_size} 字节且哈希不符）；"
                    f"请先修复或移除 {rel_path}")
        else:
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再改名，避免并发上传留下半写对象。
            tmp_path = blob_path.with_suffix(".partial")
            with path.open("rb") as src, tmp_path.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            if hash_file(tmp_path) != sha256:
                tmp_path.unlink(missing_ok=True)
                raise ValueError(f"写入校验失败，上传中止: {path.name}")
            tmp_path.replace(blob_path)
        self.conn.execute(
            "INSERT INTO blobs (sha256, size, rel_path) VALUES (?, ?, ?)"
            " ON CONFLICT(sha256) DO NOTHING", (sha256, size, rel_path))
        self.conn.commit()
        return blob_path

    # ---------- 查找与读取 ----------

    def search_files(self, project: str | None = None, name_like: str | None = None,
                     tag: str | None = None, stage: str | None = None,
                     sha256: str | None = None, upload_tag: str | None = None) -> list[dict]:
        where, params = ["1=1"], []
        if project:
            p = self.get_project(project)
            where.append("f.project_id = ?")
            params.append(p["project_id"])
        if name_like:
            where.append("lower(f.name) LIKE ?")
            params.append(f"%{name_like.lower()}%")
        if tag:
            where.append("f.tags LIKE ?")
            params.append(f'%"{tag}"%')
        if upload_tag:
            where.append("f.file_id IN (SELECT file_id FROM upload_records WHERE tags LIKE ?)")
            params.append(f'%"{upload_tag}"%')
        if stage:
            where.append("f.stage = ?")
            params.append(stage)
        if sha256:
            where.append("f.current_version_id IN (SELECT version_id FROM file_versions WHERE sha256 = ?)")
            params.append(sha256.strip().lower())
        rows = self.conn.execute(
            "SELECT f.*, p.name AS project_name, v.sha256 AS current_sha256, v.seq AS current_seq"
            " FROM files f JOIN projects p ON p.project_id = f.project_id"
            " LEFT JOIN file_versions v ON v.version_id = f.current_version_id"
            f" WHERE {' AND '.join(where)} ORDER BY f.created_at", params).fetchall()
        return db.rowdicts(rows)

    def get_file(self, file_id: str) -> dict:
        row = self.conn.execute(
            "SELECT f.*, p.name AS project_name FROM files f"
            " JOIN projects p ON p.project_id = f.project_id WHERE f.file_id = ?",
            (file_id,)).fetchone()
        record = db.rowdict(row)
        if not record:
            raise ValueError(f"文件不存在: {file_id}")
        record["tags"] = db.loads(record["tags"], [])
        versions = db.rowdicts(self.conn.execute(
            "SELECT * FROM file_versions WHERE file_id = ? ORDER BY seq", (file_id,)).fetchall())
        for version in versions:
            version["doc_meta"] = db.loads(version.pop("doc_meta_json"), {})
            version["doc_meta_sources"] = db.loads(version.pop("doc_meta_sources_json"), [])
        record["versions"] = versions
        uploads = db.rowdicts(self.conn.execute(
            "SELECT * FROM upload_records WHERE file_id = ? ORDER BY uploaded_at",
            (file_id,)).fetchall())
        for item in uploads:
            item["tags"] = db.loads(item["tags"], [])
        record["uploads"] = uploads
        same_content = db.rowdicts(self.conn.execute(
            "SELECT DISTINCT f2.file_id, f2.name, f2.project_id, p2.name AS project_name"
            " FROM file_versions v2 JOIN files f2 ON f2.file_id = v2.file_id"
            " JOIN projects p2 ON p2.project_id = f2.project_id"
            " WHERE v2.sha256 IN (SELECT sha256 FROM file_versions WHERE file_id = ?)"
            "   AND f2.file_id != ?", (file_id, file_id)).fetchall())
        record["same_content_files"] = same_content
        return record

    def get_version(self, file_id: str, version_id: str | None = None) -> dict:
        self.get_file(file_id)
        if version_id:
            row = self.conn.execute(
                "SELECT * FROM file_versions WHERE file_id = ? AND version_id = ?",
                (file_id, version_id)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT v.* FROM file_versions v JOIN files f ON f.current_version_id = v.version_id"
                " WHERE v.file_id = ?", (file_id,)).fetchone()
        record = db.rowdict(row)
        if not record:
            raise ValueError(f"版本不存在: {file_id} / {version_id or '最新'}")
        record["doc_meta"] = db.loads(record.pop("doc_meta_json"), {})
        record["doc_meta_sources"] = db.loads(record.pop("doc_meta_sources_json"), [])
        return record

    def read_version(self, file_id: str, version_id: str | None = None,
                     out_path: Path | None = None) -> dict:
        """读取指定版本（默认当前版本），读出时重新校验哈希。"""
        version = self.get_version(file_id, version_id)
        blob = db.rowdict(self.conn.execute(
            "SELECT * FROM blobs WHERE sha256 = ?", (version["sha256"],)).fetchone())
        if not blob:
            raise ValueError(f"存储对象缺失: {version['sha256']}")
        blob_path = self.data_dir / blob["rel_path"]
        if not blob_path.is_file():
            raise ValueError(f"存储文件缺失: {blob['rel_path']}")
        actual = hash_file(blob_path)
        if actual != version["sha256"]:
            raise ValueError(
                f"存储对象哈希校验失败（期望 {version['sha256']}，实际 {actual}），文件可能被改动")
        result = {"file_id": file_id, "version_id": version["version_id"],
                  "sha256": version["sha256"], "size": version["size"],
                  "filename": version["filename"],
                  "doc_meta": version.get("doc_meta", {}), "verified": True}
        if out_path is not None:
            out_path = Path(out_path).expanduser().resolve()
            # 导出目标不得落在受管理数据目录内，否则会覆盖原件或数据库（F01）。
            if out_path == self.data_dir or out_path.is_relative_to(self.data_dir):
                raise ValueError(
                    "out 路径位于受管理数据目录（data/）内，可能覆盖原件或数据库；"
                    "请输出到 data/ 之外的路径（如 output/、tmp/）")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(blob_path.read_bytes())
            result["path"] = str(out_path)
        return result
