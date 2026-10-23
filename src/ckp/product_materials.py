"""Direct Word intake and retrieval for the product-material RAG corpus.

Product manuals and training material keep their original Word file and are
parsed into source-linked sections and chunks without the project distillation
or review workflow. MongoDB/Milvus collections remain in the existing sandbox
deployment and are routed separately from the six project lifecycle stages.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document

from ckp import db
from ckp.catalog import validate_selection
from ckp.intake import hash_file
from ckp.office_parser import PARSER_VERSION, inspect_office
from ckp.store import Store
from ckp.knowledge_types import (
    PRODUCT_MATERIAL_COLLECTIONS, PRODUCT_MATERIAL_CATEGORIES,
    validate_product_material_category,
)
from chunks_and_in_vector.pipeline import SandboxRAG


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_text(table: dict) -> str:
    rows = []
    for row in table.get("rows", []):
        cells = [re.sub(r"\s+", " ", cell.get("text", "")).strip()
                 for cell in row.get("cells", [])]
        if any(cells):
            rows.append(f"第{row.get('row', len(rows) + 1)}行：" + " | ".join(cells))
    return f"表格 {table.get('table', '')}\n" + "\n".join(rows) if rows else ""


def _numbered_heading_level(text: str) -> int | None:
    value = text.strip()
    if re.match(r"^第[一二三四五六七八九十百0-9]+[章节篇]\s*\S", value):
        return 1
    if re.match(r"^[一二三四五六七八九十]+[、.．]\s*\S", value):
        return 1
    decimal = re.match(r"^(\d+(?:\.\d+){1,5})[、.．]?\s+\S", value)
    if decimal:
        return min(decimal.group(1).count(".") + 1, 6)
    return None


def _sections(manifest: dict, material_id: str, revision: int,
              splitter=None) -> tuple[list[dict], list[dict]]:
    blocks = []
    for item in manifest.get("paragraphs", []):
        blocks.append((item.get("body_order", 0), "paragraph", item))
    for item in manifest.get("tables", []):
        blocks.append((item.get("body_order", 0), "table", item))
    blocks.sort(key=lambda item: item[0])
    sections: list[dict] = []
    heading_path: list[str] = []
    current = None
    for body_order, kind, item in blocks:
        if kind == "paragraph":
            text = (item.get("text") or "").strip()
            outline = item.get("outline_level")
            style = str(item.get("style") or "")
            try:
                level = int(item.get("heading_level")) if item.get("heading_level") is not None else None
            except (TypeError, ValueError):
                level = None
            if outline is not None:
                if level is None:
                    try:
                        outline_level = int(outline)
                        level = outline_level + 1 if 0 <= outline_level <= 5 else None
                    except (TypeError, ValueError):
                        level = None
            if level is None:
                match = re.search(r"(?:heading|标题)\s*([1-6])$", style, re.I)
                if match:
                    level = int(match.group(1))
            if level is None and len(text) <= 100:
                level = _numbered_heading_level(text)
            if level and text:
                heading_path = heading_path[:level - 1] + [text]
                current = None
                continue
        else:
            text = _table_text(item)
        if not text:
            continue
        if current is None:
            section_index = len(sections) + 1
            section_id = hashlib.sha256(
                f"{material_id}@{revision}:{section_index}".encode()).hexdigest()[:20]
            current = {"section_id": section_id,
                       "heading_path": list(heading_path) or ["正文"], "blocks": []}
            sections.append(current)
        current["blocks"].append({"body_order": body_order, "type": kind,
                                   "source_index": item.get("index", item.get("table")),
                                   "text": text})

    rendered = []
    for section in sections:
        section["text"] = "\n\n".join(block["text"] for block in section["blocks"])
        del section["blocks"]
        rendered.append({**section, "heading": " / ".join(section["heading_path"])})
    if not rendered:
        raise ValueError("Word 文档未提取到可索引的正文或表格")

    chunks = []
    for section in rendered:
        text = section["text"]
        heading = section["heading"]
        if splitter:
            fragments = splitter(heading, text)
        else:
            # Fixture embeddings have no tokenizer; respect the same 4096-byte
            # ceiling used by the production Qwen splitter.
            fragments = []
            cursor = 0
            while cursor < len(text):
                end = cursor
                size = len(f"## {heading}\n\n".encode("utf-8"))
                while end < len(text) and size + len(text[end].encode("utf-8")) <= 4096:
                    size += len(text[end].encode("utf-8")); end += 1
                if end == cursor:
                    raise ValueError("章节标题过长，无法满足向量输入字节上限")
                if end < len(text):
                    boundary = max(text.rfind("\n\n", cursor, end), text.rfind("。", cursor, end),
                                   text.rfind("；", cursor, end))
                    if boundary > cursor + 1200:
                        end = boundary + (1 if text[boundary] in "。；" else 0)
                fragments.append((text[cursor:end], cursor))
                cursor = end
        for fragment, offset in fragments:
            if fragment:
                chunks.append({"section_id": section["section_id"],
                               "heading_path": section["heading_path"],
                               "offset": offset, "text": fragment})
    return rendered, chunks


class ProductMaterials:
    def __init__(self, data_dir: Path, repo_root: Path, readonly: bool = False):
        self.store = Store(data_dir, readonly=readonly)
        self.data_dir = self.store.data_dir
        self.repo_root = Path(repo_root)
        self._rag = None

    @property
    def rag(self):
        if self._rag is None:
            self._rag = SandboxRAG()
        return self._rag

    def close(self):
        if self._rag is not None:
            self._rag.close()
        self.store.conn.close()

    def _collection(self, category: str) -> str:
        return self.rag.prefix + PRODUCT_MATERIAL_COLLECTIONS[category]

    def _has_schema(self) -> bool:
        return bool(self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_materials'"
        ).fetchone())

    def ingest(self, path: str | Path, category: str, product: str,
               domains: list[str] | None) -> dict:
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ValueError("产品资料必须提供文件绝对路径")
        if source.suffix.lower() not in {".docx", ".doc"}:
            raise ValueError("产品资料首版只接受 Word 文件 .doc/.docx")
        if not source.is_file():
            raise FileNotFoundError("指定的 Word 文件不存在")
        category = validate_product_material_category(category)
        product, domains = validate_selection(self.repo_root, product, domains)
        source = source.resolve()
        sha256 = hash_file(source)
        size = source.stat().st_size
        title = source.stem.strip() or source.name
        existing = db.rowdict(self.store.conn.execute(
            "SELECT * FROM product_materials WHERE category=? AND product=? AND title=?",
            (category, product, title)).fetchone())
        active_identities = db.rowdicts(self.store.conn.execute(
            "SELECT * FROM product_materials WHERE product=? AND title=? AND active=1 ORDER BY updated_at DESC",
            (product, title)).fetchall())
        conflicting_identity = next((row for row in active_identities
                                     if row["category"] != category), None)
        if conflicting_identity:
            raise ValueError(
                f"同一产品下已存在活动资料类别 {conflicting_identity['category']}；"
                "为避免同一资料重复归类，请先按精确修订撤回旧类别，再重新确认分类入库")
        if existing:
            current = db.rowdict(self.store.conn.execute(
                "SELECT * FROM product_material_versions WHERE version_id=?",
                (existing["current_version_id"],)).fetchone()) if existing.get("current_version_id") else None
            if (current and current["sha256"] == sha256 and current["state"] == "ready"
                    and current["parser_version"] == PARSER_VERSION
                    and db.loads(current.get("domains_json"), []) == domains):
                record_id = f"{existing['material_id']}@{current['revision']}"
                active_head = self.rag.mongo.get_collection(
                    self.rag.prefix + "product_material_heads").find_one(
                        {"_id": existing["material_id"], "record_id": record_id,
                         "active": True, "collection": self._collection(category)})
                indexed_record = (self.rag.mongo.get_collection(self._collection(category)).find_one(
                    {"_id": record_id, "state": "ready", "version_id": current["version_id"],
                     "source_sha256": sha256}) if active_head else None)
                if indexed_record:
                    return {"state": "unchanged", "material_id": existing["material_id"],
                            "revision": current["revision"], "source_sha256": sha256,
                            "category": category, "products": product, "domain": domains,
                            "active": True,
                            "indexed_chunks": self._active_chunk_count(existing["material_id"], current["revision"]),
                            "embedding_mode": self.rag.mode}

        duplicate = db.rowdict(self.store.conn.execute(
            "SELECT v.material_id, m.category, m.product, m.title, v.revision FROM product_material_versions v"
            " JOIN product_materials m ON m.material_id=v.material_id"
            " WHERE v.sha256=? AND v.state='ready' AND m.active=1",
            (sha256,)).fetchone())
        if duplicate and (not existing or duplicate["material_id"] != existing["material_id"]):
            raise ValueError(
                f"该原件已登记为 {duplicate['category']} / {duplicate['product']} / {duplicate['title']}；"
                "为避免重复索引，请使用现有资料记录或先确认分类变更")

        material_id = existing["material_id"] if existing else db.new_id("pm")
        revision = (db.rowdict(self.store.conn.execute(
            "SELECT COALESCE(MAX(revision),0) AS n FROM product_material_versions WHERE material_id=?",
            (material_id,)).fetchone()) or {"n": 0})["n"] + 1
        version_id = db.new_id("pmv")
        temp_root = self.data_dir / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="product-material-", dir=temp_root) as tmp:
            manifest = inspect_office(source, Path(tmp), file_id=material_id,
                                      version_id=version_id, expected_sha256=sha256)
        sections, chunks = _sections(manifest, material_id, revision,
                                     getattr(self.rag.embedding, "split_section", None))
        self.store._store_blob(source, sha256, size)
        collection = self._collection(category)
        record_id = f"{material_id}@{revision}"
        records = self.rag.mongo.get_collection(collection)
        heads = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads")
        head = heads.find_one({"_id": material_id})
        if head and revision <= int(head.get("revision", 0)):
            raise ValueError("产品资料修订号必须高于当前活动版本")

        now = _now()
        if not existing:
            self.store.conn.execute(
                "INSERT INTO product_materials(material_id,category,product,title,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)", (material_id, category, product, title, now, now))
        else:
            self.store.conn.execute(
                "UPDATE product_materials SET product=?,updated_at=? WHERE material_id=?",
                (product, now, material_id))
        self.store.conn.execute(
            "INSERT INTO product_material_versions(version_id,material_id,revision,sha256,size,media_type,filename,"
            "domains_json,parser_version,section_count,image_count,state,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, material_id, revision, sha256, size, db.guess_media_type(source.name),
             source.name, db.dumps(domains), PARSER_VERSION, len(sections),
             len(manifest.get("assets", [])), "staging", now))
        self.store.conn.commit()

        fingerprint = hashlib.sha256(
            f"{material_id}@{revision}:{sha256}:{category}:{product}:{db.dumps(domains)}".encode()).hexdigest()
        ids = [hashlib.sha256(f"{collection}:{record_id}:{i}".encode()).hexdigest()
               for i in range(len(chunks))]
        record = {"_id": record_id, "material_id": material_id, "revision": revision,
                  "version_id": version_id, "source_sha256": sha256, "fingerprint": fingerprint,
                  "state": "staging", "chunk_ids": ids, "category": category,
                  "knowledge_class": "product_material", "products": product, "domain": domains,
                  "title": title, "filename": source.name,
                  "content": "\n\n".join(f"## {' / '.join(s['heading_path'])}\n\n{s['text']}" for s in sections),
                  "sections": sections, "image_count": len(manifest.get("assets", [])),
                  "parser_version": PARSER_VERSION, "sandbox": True}
        records.replace_one({"_id": record_id}, record, upsert=True)
        documents = []
        for chunk in chunks:
            documents.append(Document(page_content=f"## {' / '.join(chunk['heading_path'])}\n\n{chunk['text']}",
                                      metadata={"url": f"original://{material_id}@{revision}",
                                                "mongo_id": record_id, "material_id": material_id,
                                                "revision": revision, "source_sha256": sha256,
                                                "category": category, "knowledge_class": "product_material",
                                                "products": product, "domain": domains,
                                                "section_id": chunk["section_id"],
                                                "heading_path": chunk["heading_path"],
                                                "offset": chunk["offset"], "file_name": title,
                                                "source_filename": source.name, "sandbox": True,
                                                "embedding_mode": self.rag.mode}))
        activated = False
        try:
            vector_db = self.rag._db(collection)
            vector_db.insert(documents, ids=ids)
            expected = {cid: document for cid, document in zip(ids, documents)}
            verified = 0
            for offset in range(0, len(ids), 128):
                batch = ids[offset:offset + 128]
                rows = vector_db.query_by_ids(batch)
                if len(rows) != len(batch) or {row["id"] for row in rows} != set(batch):
                    raise ValueError("产品资料向量逐块读回不完整；未激活此修订")
                for row in rows:
                    expected_doc = expected[row["id"]]
                    if (row["text"] != expected_doc.page_content
                            or json.loads(row["metadata"]) != expected_doc.metadata):
                        raise ValueError("产品资料向量正文或元数据读回不一致；未激活此修订")
                verified += len(batch)
            records.update_one({"_id": record_id}, {"$set": {"state": "ready"}})
            new_head = {"_id": material_id, "record_id": record_id, "revision": revision,
                        "active": True, "collection": collection}
            if head:
                result = heads.replace_one({"_id": material_id, "record_id": head["record_id"],
                                            "active": head["active"], "collection": head["collection"]}, new_head)
                if result.matched_count != 1:
                    raise RuntimeError("产品资料活动版本已并发变化，请重试")
            else:
                heads.insert_one(new_head)
            activated = True
            self.store.conn.execute(
                "UPDATE product_material_versions SET state='ready' WHERE version_id=?", (version_id,))
            self.store.conn.execute(
                "UPDATE product_materials SET current_version_id=?,active=1,updated_at=? WHERE material_id=?",
                (version_id, _now(), material_id))
            self.store.conn.commit()
        except Exception:
            if activated:
                if head:
                    heads.replace_one({"_id": material_id, "record_id": record_id}, head)
                else:
                    heads.delete_one({"_id": material_id, "record_id": record_id})
            records.update_one({"_id": record_id}, {"$set": {"state": "failed"}})
            self.store.conn.execute(
                "UPDATE product_material_versions SET state='failed' WHERE version_id=?", (version_id,))
            if existing:
                self.store.conn.execute(
                    "UPDATE product_materials SET current_version_id=?,active=?,updated_at=? WHERE material_id=?",
                    (existing.get("current_version_id"), existing.get("active", 1), _now(), material_id))
            else:
                self.store.conn.execute(
                    "UPDATE product_materials SET current_version_id=NULL,active=0,updated_at=? WHERE material_id=?",
                    (_now(), material_id))
            self.store.conn.commit()
            raise
        return {"state": "indexed", "material_id": material_id, "revision": revision,
                "version_id": version_id, "source_sha256": sha256, "category": category,
                "products": product, "domain": domains, "sections": len(sections),
                "chunks": len(chunks), "verified_chunks": verified,
                "embedded_images_not_ocr_processed": len(manifest.get("assets", [])),
                "sandbox": True, "embedding_mode": self.rag.mode}

    def _active_chunk_count(self, material_id: str, revision: int) -> int:
        for category in PRODUCT_MATERIAL_CATEGORIES:
            record = self.rag.mongo.get_collection(self._collection(category)).find_one(
                {"_id": f"{material_id}@{revision}", "state": "ready"})
            if record:
                return len(record.get("chunk_ids", []))
        return 0

    def list(self, product: str = "", category: str = "") -> list[dict]:
        if not self._has_schema():
            return []
        where, params = [], []
        if product:
            where.append("product=?"); params.append(product.strip())
        if category:
            where.append("category=?"); params.append(validate_product_material_category(category))
        sql = ("SELECT m.*,v.revision,v.sha256 AS source_sha256,v.state AS version_state,"
               "v.filename,v.domains_json FROM product_materials m LEFT JOIN product_material_versions v"
               " ON v.version_id=m.current_version_id"
               + (" WHERE " + " AND ".join("m." + item for item in where) if where else "")
               + " ORDER BY m.title")
        rows = []
        for row in self.store.conn.execute(sql, params).fetchall():
            item = dict(row)
            item["domain"] = db.loads(item.pop("domains_json", "[]"), [])
            item["products"] = item.pop("product")
            rows.append(item)
        return rows

    def read(self, material_id: str, revision: int, source_sha256: str,
             section_ids: list[str] | None = None, full_document: bool = False) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        if full_document and section_ids:
            raise ValueError("full_document 与 section_ids 不能同时传入")
        item = db.rowdict(self.store.conn.execute(
            "SELECT m.category,v.* FROM product_materials m JOIN product_material_versions v"
            " ON v.material_id=m.material_id WHERE m.material_id=? AND v.revision=? AND v.state='ready'",
            (material_id, revision)).fetchone())
        if not item or item["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        record = self.rag.mongo.get_collection(self._collection(item["category"])).find_one(
            {"_id": f"{material_id}@{revision}", "state": "ready"})
        if not record:
            raise ValueError("产品资料当前不可读")
        sections = record.get("sections", [])
        if full_document:
            selected = sections
        elif section_ids:
            requested = set(section_ids)
            selected = [section for section in sections if section["section_id"] in requested]
            if len(selected) != len(requested):
                raise ValueError("section_ids 包含不属于该产品资料修订的章节")
        else:
            selected = [{"section_id": s["section_id"], "heading_path": s["heading_path"],
                         "characters": len(s["text"])} for s in sections]
        return {"material_id": material_id, "revision": revision, "source_sha256": source_sha256,
                "category": item["category"], "products": record.get("products", record.get("product")),
                "domain": record["domain"],
                "title": record["title"], "filename": item["filename"],
                "sections": selected, "section_count": len(sections),
                "embedded_images_not_ocr_processed": item["image_count"],
                "sandbox": True}

    def search(self, query: str, product: str = "", domain: list[str] | None = None,
               category: str = "", limit: int = 5) -> list[dict]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query 不能为空，limit 必须为 1-20")
        category_filter = validate_product_material_category(category) if category else None
        product_filter = product.strip() or None
        domain_filter = set(domain or [])
        categories = [category_filter] if category_filter else list(PRODUCT_MATERIAL_CATEGORIES)
        results = []
        local_active = {}
        if self._has_schema():
            for row in self.store.conn.execute(
                "SELECT m.material_id,m.category,v.version_id,v.revision,v.sha256"
                " FROM product_materials m JOIN product_material_versions v"
                " ON v.version_id=m.current_version_id AND v.material_id=m.material_id"
                " WHERE m.active=1 AND v.state='ready'"
            ).fetchall():
                local_active[row["material_id"]] = dict(row)
        heads = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads")
        for cat in categories:
            collection = self._collection(cat)
            active_heads = list(heads.find({"active": True, "collection": collection}))
            records = self.rag.mongo.get_collection(collection)
            allowed = {}
            for head in active_heads:
                local_version = local_active.get(head.get("_id"))
                if (not local_version or local_version["category"] != cat
                        or local_version["revision"] != head.get("revision")):
                    continue
                record = records.find_one({"_id": head["record_id"], "state": "ready"})
                if (record and record.get("material_id") == local_version["material_id"]
                        and record.get("version_id") == local_version["version_id"]
                        and record.get("source_sha256") == local_version["sha256"]
                        and (not product_filter or record.get("products", record.get("product")) == product_filter)
                        and (not domain_filter or domain_filter.intersection(record.get("domain", [])))):
                    allowed.update({cid: record for cid in record.get("chunk_ids", [])})
            if not allowed:
                continue
            all_ids = list(allowed)
            for offset in range(0, len(all_ids), 200):
                hits = self.rag._db(collection).search(query, k=limit,
                    filter_expr="id in " + json.dumps(all_ids[offset:offset + 200]))
                for hit in hits:
                    record = allowed.get(hit["id"])
                    if not record:
                        continue
                    current = heads.find_one({"_id": record["material_id"], "record_id": record["_id"],
                                              "active": True, "collection": collection})
                    if not current:
                        continue
                    hit_metadata = hit.get("metadata") or {}
                    if "products" not in hit_metadata and hit_metadata.get("product"):
                        hit_metadata["products"] = hit_metadata["product"]
                    results.append({**hit, "metadata": hit_metadata,
                                    "category": cat, "knowledge_class": "product_material",
                                    "material_id": record["material_id"], "revision": record["revision"],
                                    "source_sha256": record["source_sha256"],
                                    "products": record.get("products", record.get("product")),
                                    "domain": record["domain"], "title": record["title"],
                                    "sandbox": True, "embedding_mode": self.rag.mode})
        return sorted(results, key=lambda hit: hit["score"], reverse=True)[:limit]

    def export_original(self, material_id: str, revision: int, source_sha256: str,
                        out_path: str | Path) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        row = db.rowdict(self.store.conn.execute(
            "SELECT * FROM product_material_versions WHERE material_id=? AND revision=? AND state='ready'",
            (material_id, revision)).fetchone())
        if not row or row["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        blob = db.rowdict(self.store.conn.execute("SELECT * FROM blobs WHERE sha256=?", (source_sha256,)).fetchone())
        if not blob:
            raise ValueError("产品资料原件存储引用缺失")
        source = self.data_dir / blob["rel_path"]
        if not source.is_file() or hash_file(source) != source_sha256:
            raise ValueError("产品资料原件不存在或 SHA-256 校验失败")
        target = Path(out_path).expanduser().resolve()
        if target == self.data_dir or target.is_relative_to(self.data_dir):
            raise ValueError("out_path 不能位于受管理数据目录内")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return {"material_id": material_id, "revision": revision, "source_sha256": source_sha256,
                "filename": row["filename"], "size": row["size"], "verified": hash_file(target) == source_sha256,
                "path": str(target)}

    def withdraw(self, material_id: str, revision: int, source_sha256: str) -> dict:
        if not self._has_schema():
            raise ValueError("产品资料修订不存在")
        item = db.rowdict(self.store.conn.execute(
            "SELECT m.category,v.* FROM product_materials m JOIN product_material_versions v"
            " ON v.material_id=m.material_id WHERE m.material_id=? AND v.revision=? AND v.state='ready'",
            (material_id, revision)).fetchone())
        if not item or item["sha256"] != source_sha256:
            raise ValueError("产品资料修订不存在或来源哈希不匹配")
        collection = self._collection(item["category"])
        result = self.rag.mongo.get_collection(self.rag.prefix + "product_material_heads").update_one(
            {"_id": material_id, "record_id": f"{material_id}@{revision}",
             "collection": collection, "active": True}, {"$set": {"active": False}})
        if result.matched_count:
            self.store.conn.execute("UPDATE product_materials SET active=0,updated_at=? WHERE material_id=?",
                                    (_now(), material_id))
            self.store.conn.commit()
        return {"withdrawn": bool(result.matched_count), "material_id": material_id,
                "revision": revision, "sandbox": True}
