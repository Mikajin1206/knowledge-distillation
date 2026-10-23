"""Isolated development RAG: latest CKP routing, immutable revisions, active pointers.

This service is deliberately sandbox-only. It does not implement approval or
production publication. Drafts can be exercised without becoming formal knowledge.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import threading
import copy
from contextlib import contextmanager
from pymongo.errors import DuplicateKeyError
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.documents import Document
from ckp.md_render import extract_rag_payload, split_frontmatter, verify_md
from ckp.knowledge_types import STAGE_COLLECTIONS, STAGES, validate_stage
from .milvus_db import MilvusDB
from .mongodb_db import MongoDB

ROOT = Path(__file__).resolve().parents[2]


class FixtureEmbedding:
    """Deterministic test vectors; never a substitute for semantic evaluation."""
    dimension = 1024
    model = 'fixture-sha256-v1'

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        vector = [0.0] * self.dimension
        text = re.sub(r'\s+', '', text.lower())
        for i in range(max(1, len(text) - 1)):
            digest = hashlib.sha256(text[i:i + 2].encode()).digest()
            vector[int.from_bytes(digest[:4], 'big') % self.dimension] += 1.0
        return vector


def prepare(path: str | Path, splitter=None) -> dict:
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != '.md':
        raise ValueError('入库入口只接受蒸馏导出的 MD 与同名 JSON，不解析原件')
    raw = path.read_bytes()
    text = raw.decode('utf-8')
    check = verify_md(path)
    if not check.get('match'):
        raise ValueError('Markdown 正文哈希校验失败')
    sidecar_raw = path.with_suffix('.json').read_bytes()
    sidecar = json.loads(sidecar_raw)
    meta, body = split_frontmatter(text)
    for key in ('knowledge_id', 'revision', 'content_hash', 'status', 'title', 'knowledge_type'):
        if str(meta.get(key)) != str(sidecar.get(key)):
            raise ValueError(f'MD/sidecar 字段不一致: {key}')
    meta['knowledge_type'] = str(meta.get('knowledge_type') or '').strip()
    if not meta['knowledge_type']:
        raise ValueError('文档类型不能为空')
    stage = validate_stage(sidecar.get('stage'))
    # Product/domain are project-manager classifications. New exports must carry
    # the same scalar/list values in MD and sidecar; historical exports without
    # these fields remain readable and retain empty supplemental metadata.
    sidecar_classification = sidecar.get('classification')
    if sidecar_classification is not None:
        if not isinstance(sidecar_classification, dict):
            raise ValueError('sidecar classification 格式无效')
        products = meta.get('products', '')
        domains = meta.get('domain', [])
        if (not isinstance(products, str) or not products.strip()
                or not isinstance(domains, list) or not domains
                or sidecar_classification.get('products') != products
                or sidecar_classification.get('domain') != domains):
            raise ValueError('MD frontmatter 与 sidecar 的产品线/领域分类不一致')
        if not isinstance(products, str) or not products.strip() or not isinstance(domains, list) or not domains:
            raise ValueError('MD/sidecar 产品线或领域分类缺失')
        if any(not isinstance(value, str) or not value.strip() for value in domains):
            raise ValueError('MD/sidecar 领域分类格式无效')
    else:
        # Legacy sidecars may contain the former inferred/list-valued products
        # field. Do not promote it to a PM-confirmed RAG filter.
        products, domains = '', []
    meta['products'] = products
    meta['domain'] = domains
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(meta.get('knowledge_id', ''))):
        raise ValueError('knowledge_id 格式无效')
    try:
        revision = int(meta['revision'])
        if revision < 1:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('revision 必须为正整数') from None
    refs = sidecar.get('sources')
    if not isinstance(refs, list) or not refs:
        raise ValueError('sidecar 缺少来源版本')
    for ref in refs:
        if not ref.get('file_id') or not ref.get('version_id') or not re.fullmatch(r'[a-f0-9]{64}', str(ref.get('sha256', ''))):
            raise ValueError('来源身份、版本或 SHA-256 无效')
    selection = sidecar.get('rag_selection') or {}
    expected_refs = [{'source_ref_id': f"{ref['file_id']}@{ref['version_id']}",
                      'file_id': ref['file_id'], 'version_id': ref['version_id'], 'sha256': ref['sha256']}
                     for ref in refs]
    if selection.get('source_refs') != expected_refs:
        raise ValueError('sidecar 来源引用与来源版本不一致')
    payload = extract_rag_payload(text, (sidecar.get('protected_assets') or {}).get('items', []),
                                  selection.get('source_refs', []))
    if not payload['routing_valid']:
        raise ValueError('章节路由无效: ' + '; '.join(payload['errors']))
    if selection.get('sections') != payload['sections'] or not selection.get('routing_valid'):
        raise ValueError('sidecar 路由清单过期或不一致，请通过最新蒸馏代码重新导出')
    # The authoritative parser owns validation and the retrieval view. Split only
    # its validated sections, including H4-H6; audit/metadata/exclude never index.
    matches = list(re.finditer(r'^#{2,6}\s+[^\n]+$', body, re.M))
    chunks = []
    for i, section in enumerate(payload['sections']):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[matches[i].end():end].strip()
        if section['route'] != 'body' or not content:
            continue
        heading = ' / '.join(section['heading_path'])
        # Local BGE uses real tokenizer windows rather than truncating at 512.
        if splitter:
            fragments = splitter(heading, content)
        else:
            fragments = ((content[i:i+8000], i) for i in range(0, len(content), 8000))
        for fragment, offset in fragments:
            chunks.append({'text': f'## {heading}\n\n{fragment}',
                           'section': section, 'offset': offset})
    if not chunks:
        raise ValueError('没有可入库的 body 章节')
    collection = STAGE_COLLECTIONS[stage]
    fingerprint = hashlib.sha256(raw + b'\x00' + sidecar_raw).hexdigest()
    return {'path': path, 'raw': raw, 'sidecar_raw': sidecar_raw, 'meta': meta,
            'sidecar': sidecar, 'payload': payload, 'chunks': chunks,
            'collection': collection, 'stage': stage, 'revision': revision,
            'fingerprint': fingerprint}


class SandboxRAG:
    def __init__(self, embedding=None, mode=None):
        load_dotenv(ROOT / '.env')
        self.mode = mode or os.getenv('KD_EMBEDDING_MODE', 'qwen')
        if self.mode not in ('local', 'qwen', 'fixture'):
            raise ValueError('KD_EMBEDDING_MODE 必须为 local、qwen 或 fixture')
        if embedding is None:
            if self.mode == 'local':
                from .local_embedding import LocalBGEEmbedding
                embedding = LocalBGEEmbedding()
            elif self.mode == 'fixture':
                embedding = FixtureEmbedding()
            else:
                from .embedding import EmbeddingModel
                embedding = EmbeddingModel(dimension=1024)
        self.embedding = embedding
        self.dimension = embedding.dimension or 1024
        self.prefix = f'kd_dev_{self.mode}_'
        db_name = os.getenv('KD_MONGO_DATABASE', 'kd_dev')
        if not db_name.startswith('kd_dev'):
            raise ValueError('开发服务只允许 kd_dev 开头的 MongoDB 数据库')
        self.mongo = MongoDB(db_name=db_name, uri=os.getenv('KD_MONGO_URI', 'mongodb://127.0.0.1:27017'))
        self.mongo.connect()
        self.archive = Path(os.getenv('KD_ARCHIVE', str(ROOT / '.cloud/archive'))).resolve()
        self.dbs = {}

    def close(self):
        for db in self.dbs.values():
            db.close()
        self.mongo.close()

    def _names(self, stage):
        """Resolve a controlled lifecycle stage to its sandbox collection."""
        return self.prefix + STAGE_COLLECTIONS[validate_stage(stage)]

    def _db(self, collection):
        if collection not in self.dbs:
            db = MilvusDB(collection, embedding=self.embedding,
                          host=os.getenv('MILVUS_HOST', '127.0.0.1'),
                          port=os.getenv('MILVUS_PORT', '19530'), dimension=self.dimension,
                          alias='kd_' + uuid4().hex, index_preset='flat')
            db.connect()
            db.create_collection(drop_if_exists=False)
            self.dbs[collection] = db
        return self.dbs[collection]

    @contextmanager
    def _lease(self, knowledge_id):
        locks = self.mongo.get_collection(self.prefix + 'locks')
        token = uuid4().hex
        try:
            locks.update_one({'_id': knowledge_id, 'expires': {'$lte': time.time()}},
                             {'$set': {'token': token, 'expires': time.time() + 600}}, upsert=True)
        except DuplicateKeyError:
            raise ValueError('该知识正在入库，请等待当前操作完成后重试') from None
        stopped = threading.Event()
        lost = threading.Event()
        def renew():
            while not stopped.wait(30):
                try:
                    result = locks.update_one({'_id': knowledge_id, 'token': token},
                                              {'$set': {'expires': time.time() + 600}})
                    if result.matched_count != 1:
                        lost.set(); return
                except Exception:
                    lost.set(); return
        worker = threading.Thread(target=renew, daemon=True)
        worker.start()
        try:
            yield lost
        finally:
            stopped.set(); worker.join(timeout=2)
            locks.delete_one({'_id': knowledge_id, 'token': token})

    def ingest(self, path, publication=None, progress=None):
        item = prepare(path, getattr(self.embedding, 'split_section', None))  # Validate before writes.
        if progress:
            progress('validated', 0, len(item['chunks']))
        with self._lease(item['meta']['knowledge_id']) as lost:
            return self._ingest(item, lost, publication or {}, progress)

    def _ingest(self, item, lost, publication=None, progress=None):
        publication = publication or {}
        meta = item['meta']; kid = meta['knowledge_id']; rev = item['revision']
        meta.setdefault('products', '')
        meta.setdefault('domain', [])
        collection = self._names(item['stage'])
        records = self.mongo.get_collection(collection)
        heads = self.mongo.get_collection(self.prefix + 'heads')
        record_id = f'{kid}@{rev}'
        existing = records.find_one({'_id': record_id})
        if existing and existing['fingerprint'] != item['fingerprint']:
            raise ValueError('同一 knowledge_id/revision 内容已存在，修改必须产生新版本')
        head = heads.find_one({'_id': kid})
        if head and rev < head['revision']:
            raise ValueError('禁止用旧版本覆盖当前版本')
        if existing and existing.get('state') == 'ready' and existing.get('publication', {}) == publication:
            return {'state': 'unchanged', 'knowledge_id': kid, 'revision': rev,
                    'active': bool(head and head.get('record_id') == record_id and head.get('collection') == collection and head.get('active')),
                    'chunks': len(existing['chunk_ids']), 'sandbox': True, 'embedding_mode': self.mode}
        asset_dir = self.archive / item['fingerprint']
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / 'knowledge.md').write_bytes(item['raw'])
        (asset_dir / 'knowledge.json').write_bytes(item['sidecar_raw'])
        if (asset_dir / 'knowledge.md').read_bytes() != item['raw']:
            raise ValueError('归档内容校验失败')
        ids = [hashlib.sha256(f'{collection}:{record_id}:{i}'.encode()).hexdigest()
               for i in range(len(item['chunks']))]
        documents = []
        score = float(meta.get('quality_score') or 0)
        for chunk in item['chunks']:
            documents.append(Document(page_content=chunk['text'], metadata={
                'url': str(asset_dir / 'knowledge.md'), 'mongo_id': record_id,
                'knowledge_id': kid, 'revision': rev, 'content_hash': meta['content_hash'],
                'project_id': meta.get('project_id'), 'stage': item['stage'],
                'knowledge_type': meta['knowledge_type'],
                'products': meta['products'], 'domain': meta['domain'],
                'section_id': chunk['section']['id'], 'heading_path': chunk['section']['heading_path'],
                'offset': chunk['offset'], 'sources': item['sidecar']['sources'],
                'protected_asset_refs': chunk['section']['protected_asset_refs'],
                'file_name': meta['title'], 'score': score, 'sandbox': True,
                'embedding_mode': self.mode}))
            documents[-1].metadata['review_id'] = publication.get('review_id')
            documents[-1].metadata['review_status'] = publication.get('status')
        safe_sidecar = copy.deepcopy(item['sidecar'])
        safe_sidecar.get('quality_report', {}).pop('flowcharts', None)
        for asset in safe_sidecar.get('protected_assets', {}).get('items', []):
            asset.pop('payload_ref', None)
        record = {'_id': record_id, 'knowledge_id': kid, 'revision': rev,
                  'fingerprint': item['fingerprint'], 'state': 'staging', 'chunk_ids': ids,
                  'stage': item['stage'], 'knowledge_type': meta['knowledge_type'],
                  'products': meta['products'], 'domain': meta['domain'],
                  'title': meta['title'], 'content': item['payload']['retrieval_md'],
                  'fileUrl': str(asset_dir / 'knowledge.md'), 'json': safe_sidecar,
                  'publication': publication, 'sandbox': True}
        records.replace_one({'_id': record_id}, record, upsert=True)
        try:
            vector_db = self._db(collection)
            if progress:
                progress('embedding_and_writing', 0, len(ids))
            vector_db.insert(documents, ids=ids)
            expected = {cid: doc for cid, doc in zip(ids, documents)}
            verified = 0
            for offset in range(0, len(ids), 128):
                batch = ids[offset:offset + 128]
                rows = vector_db.query_by_ids(batch)
                if len(rows) != len(batch) or {row['id'] for row in rows} != set(batch):
                    raise ValueError('向量逐块读回不完整；保持旧活动版本')
                for row in rows:
                    doc = expected[row['id']]
                    if row['text'] != doc.page_content or json.loads(row['metadata']) != doc.metadata:
                        raise ValueError('向量正文或元数据读回不一致；保持旧活动版本')
                verified += len(batch)
                if progress:
                    progress('verifying_chunks', verified, len(ids))
            records.update_one({'_id': record_id}, {'$set': {'state': 'ready'}})
            if lost.is_set():
                raise RuntimeError('入库锁失效；保留旧版本，请重试')
            new_head = {'_id': kid, 'record_id': record_id, 'revision': rev, 'active': True, 'collection': collection}
            # Compare and swap: failed/concurrent publication keeps the old head.
            if head:
                result = heads.replace_one({'_id': kid, 'record_id': head['record_id'],
                                            'active': head['active'], 'collection': head['collection']}, new_head)
                if result.matched_count != 1:
                    raise RuntimeError('当前版本已并发变化，请重试')
            else:
                heads.insert_one(new_head)
        except Exception:
            records.update_one({'_id': record_id}, {'$set': {'state': 'failed'}})
            raise
        return {'state': 'indexed', 'knowledge_id': kid, 'revision': rev,
                'chunks': len(ids), 'verified_chunks': verified,
                'sandbox': True, 'embedding_mode': self.mode}

    def search(self, query, knowledge_type=None, limit=5, approved_only=False, stage=None,
               products=None, domain=None):
        """Search active stage collections; document type remains an optional exact attribute filter."""
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError('query 不能为空，limit 必须为 1-20')
        results = []
        stage_filter = validate_stage(stage) if stage else None
        document_type_filter = str(knowledge_type or '').strip() or None
        product_filter = str(products or '').strip() or None
        domain_filter = set(domain or [])
        stage_values = [stage_filter] if stage_filter else list(STAGES)
        for stage_value in stage_values:
            collection = self._names(stage_value)
            heads = list(self.mongo.get_collection(self.prefix + 'heads').find({'active': True, 'collection': collection}))
            records = self.mongo.get_collection(collection)
            allowed = {}
            for head in heads:
                record = records.find_one({'_id': head['record_id'], 'state': 'ready'})
                if (record and record.get('stage') == stage_value
                        and (document_type_filter is None or record.get('knowledge_type') == document_type_filter)
                        and (product_filter is None or record.get('products', '') == product_filter)
                        and (not domain_filter or domain_filter.intersection(record.get('domain', [])))
                        and (not approved_only or record.get('publication', {}).get('status') == 'approved')):
                    allowed.update({cid: record for cid in record['chunk_ids']})
            if not allowed:
                continue
            # Batch the allowlist: only complete active sandbox revisions enter
            # candidate selection, including when many historical vectors remain.
            all_ids = list(allowed)
            for offset in range(0, len(all_ids), 200):
                expr = 'id in ' + json.dumps(all_ids[offset:offset + 200])
                hits = self._db(collection).search(query, k=limit, filter_expr=expr)
                for hit in hits:
                    record = allowed.get(hit['id'])
                    if not record:
                        continue
                    # Recheck the head after vector retrieval (withdraw/version race).
                    if not self.mongo.get_collection(self.prefix + 'heads').find_one(
                        {'_id': record['knowledge_id'], 'record_id': record['_id'], 'active': True, 'collection': collection}):
                        continue
                    results.append({**hit, 'stage': stage_value,
                                    'knowledge_type': record.get('knowledge_type'), 'sandbox': True,
                                    'products': record.get('products', ''), 'domain': record.get('domain', []),
                                    'embedding_mode': self.mode})
        return sorted(results, key=lambda hit: hit['score'], reverse=True)[:limit]

    def read(self, stage, knowledge_id, revision=None, approved_only=False):
        """Read from the collection selected by the required lifecycle stage."""
        collection = self._names(stage)
        if revision is None:
            head = self.mongo.get_collection(self.prefix + 'heads').find_one({'_id': knowledge_id, 'active': True, 'collection': collection})
            if not head:
                return None
            record_id = head['record_id']
        else:
            record_id = f'{knowledge_id}@{int(revision)}'
        record = self.mongo.get_collection(collection).find_one({'_id': record_id, 'state': 'ready'})
        if record and approved_only and record.get('publication', {}).get('status') != 'approved':
            return None
        return record

    def withdraw(self, stage, knowledge_id, revision=None):
        """Withdraw the active record in the collection selected by lifecycle stage."""
        collection = self._names(stage)
        predicate = {'_id': knowledge_id, 'collection': collection, 'active': True}
        if revision is not None:
            predicate['record_id'] = f'{knowledge_id}@{int(revision)}'
        result = self.mongo.get_collection(self.prefix + 'heads').update_one(
            predicate, {'$set': {'active': False}})
        return {'withdrawn': bool(result.matched_count), 'sandbox': True}
