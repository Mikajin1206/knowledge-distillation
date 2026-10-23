"""Local single-user review, publication and versioned knowledge reads.

All publication stays in the kd_dev sandbox. Actor identity is bound by server
configuration, not a tool argument. A future remote deployment must replace the
local actor and file adapter with authenticated company services.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from pathlib import Path

from ckp import db
from ckp.distill import Distill
from ckp.md_render import build_sidecar, export_draft, extract_rag_payload
from ckp.store import Store

_LOG = logging.getLogger(__name__)
_RUNNING: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()
_PUBLISH_LOCK = threading.Lock()
SCOPE = 'local_development'


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def public_value(value):
    """Do not return local archive paths, full sidecars, or credentials."""
    if isinstance(value, dict):
        return {k: public_value(v) for k, v in value.items()
                if k not in ('url', 'fileUrl', 'path', 'json', 'fingerprint')}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


class Knowledge:
    def __init__(self, data_dir: Path, repo_root: Path, readonly=False):
        self.data_dir, self.repo_root = Path(data_dir), Path(repo_root)
        self.store = Store(data_dir, readonly=readonly)
        self.distill = Distill(self.store, repo_root)

    def close(self):
        self.store.conn.close()

    def actor(self, reviewer=False):
        actor = os.getenv('CKP_LOCAL_ACTOR', '').strip()
        if not actor:
            raise ValueError('本地开发操作身份未配置：需要 CKP_LOCAL_ACTOR')
        allowed = {s.strip() for s in os.getenv('CKP_REVIEWER_IDS', '').split(',') if s.strip()}
        if reviewer and actor not in allowed:
            raise ValueError('当前服务端绑定身份没有审核权限')
        return actor

    def snapshot(self, kid, revision, content_hash=''):
        if revision < 1:
            raise ValueError('必须指定正整数 revision，不能默认取最新版本')
        draft = self.distill.get_draft(kid, revision)
        if content_hash and draft['content_hash'] != content_hash:
            raise ValueError('知识修订与 content_hash 不匹配')
        if hashlib.sha256(draft['body_md'].encode()).hexdigest() != draft['content_hash']:
            raise ValueError('服务端正文哈希不匹配')
        project = self.store.get_project(draft['project_id'])
        sidecar = build_sidecar(draft, project)
        if not sidecar['rag_selection']['routing_valid']:
            raise ValueError('章节路由无效，请人工复核并提交新修订')
        for ref in draft['sources']:
            checked = self.store.read_version(ref['file_id'], ref['version_id'])
            if checked['sha256'] != ref['sha256']:
                raise ValueError('来源文件版本哈希不匹配')
        digest = hashlib.sha256(canonical({'body': draft['body_md'], 'sidecar': sidecar})).hexdigest()
        return draft, project, sidecar, digest

    def lifecycle(self, kid, revision):
        # Older data files acquire new tables on the next explicit write.
        tables = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'knowledge_reviews' not in tables:
            return {'status': 'draft', 'scope': SCOPE}
        review = self.store.conn.execute('SELECT * FROM knowledge_reviews WHERE knowledge_id=? AND revision=? ORDER BY created_at DESC LIMIT 1', (kid, revision)).fetchone()
        task = self.store.conn.execute('SELECT * FROM publish_tasks WHERE knowledge_id=? AND revision=? ORDER BY created_at DESC LIMIT 1', (kid, revision)).fetchone()
        return {'status': task['status'] if task else review['status'] if review else 'draft',
                'review_id': review['review_id'] if review else None,
                'publish_task_id': task['publish_task_id'] if task else None, 'scope': SCOPE}

    def submit_review(self, kid, revision, content_hash):
        actor = self.actor()
        draft, _, _, digest = self.snapshot(kid, revision, content_hash)
        existing = self.store.conn.execute('SELECT * FROM knowledge_reviews WHERE knowledge_id=? AND revision=? AND snapshot_hash=?', (kid, revision, digest)).fetchone()
        if existing:
            return dict(existing) | {'scope': SCOPE}
        rid = db.new_id('review')
        self.store.conn.execute('INSERT INTO knowledge_reviews (review_id,knowledge_id,revision,content_hash,snapshot_hash,status,requested_by,created_at) VALUES (?,?,?,?,?,?,?,?)',
                                (rid, kid, revision, draft['content_hash'], digest, 'in_review', actor, db.now()))
        self.store.conn.commit()
        return {'review_id': rid, 'knowledge_id': kid, 'revision': revision, 'content_hash': draft['content_hash'],
                'snapshot_hash': digest, 'status': 'in_review', 'scope': SCOPE}

    def record_review(self, review_id, decision, comments):
        actor = self.actor(reviewer=True)
        if decision not in ('approved', 'changes_requested'):
            raise ValueError('decision 只能为 approved 或 changes_requested')
        review = self.store.conn.execute('SELECT * FROM knowledge_reviews WHERE review_id=?', (review_id,)).fetchone()
        if not review:
            raise ValueError('审核记录不存在')
        _, _, _, digest = self.snapshot(review['knowledge_id'], review['revision'], review['content_hash'])
        if digest != review['snapshot_hash']:
            raise ValueError('正文、来源或资产清单已变化，需要重新提交审核')
        if review['status'] != 'in_review':
            if (review['status'], review['reviewer'], review['comments']) == (decision, actor, comments):
                return dict(review) | {'scope': SCOPE}
            raise ValueError('该审核已有结论；修订内容应生成新 revision 后重新审核')
        self.store.conn.execute('UPDATE knowledge_reviews SET status=?,reviewer=?,comments=?,reviewed_at=? WHERE review_id=? AND status=?',
                                (decision, actor, comments, db.now(), review_id, 'in_review'))
        self.store.conn.commit()
        return dict(self.store.conn.execute('SELECT * FROM knowledge_reviews WHERE review_id=?', (review_id,)).fetchone()) | {'scope': SCOPE}

    def publish(self, kid, revision, content_hash):
        self.actor()
        _, _, _, digest = self.snapshot(kid, revision, content_hash)
        review = self.store.conn.execute('SELECT * FROM knowledge_reviews WHERE knowledge_id=? AND revision=? AND snapshot_hash=? AND status=?',
                                        (kid, revision, digest, 'approved')).fetchone()
        if not review:
            raise ValueError('该精确修订尚无有效审核通过记录')
        task = self.store.conn.execute('SELECT * FROM publish_tasks WHERE knowledge_id=? AND revision=? AND snapshot_hash=?', (kid, revision, digest)).fetchone()
        if task and task['status'] in ('published', 'withdrawn'):
            return self.task(task['publish_task_id'])
        tid = task['publish_task_id'] if task else db.new_id('publish')
        if not task:
            now = db.now()
            self.store.conn.execute('INSERT INTO publish_tasks (publish_task_id,knowledge_id,revision,review_id,snapshot_hash,status,phase,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                    (tid, kid, revision, review['review_id'], digest, 'queued', 'queued', now, now))
            self.store.conn.commit()
        with _THREADS_LOCK:
            current = _RUNNING.get(tid)
            latest_status = self.task(tid)['status']
            if current is None or not current.is_alive() or latest_status == 'publish_failed':
                # A retry must not return the previous terminal failure before
                # the new worker has had a chance to update its first phase.
                # A failed worker may still be cleaning up. The next worker
                # waits for its publication lock instead of dropping the retry.
                self.progress(tid, 'queued', 0, 0, status='queued')
                worker = threading.Thread(target=_publish_worker, args=(self.data_dir, self.repo_root, tid), daemon=True)
                _RUNNING[tid] = worker
                worker.start()
        return self.task(tid)

    def task(self, tid):
        task = self.store.conn.execute('SELECT * FROM publish_tasks WHERE publish_task_id=?', (tid,)).fetchone()
        if not task:
            raise ValueError('发布任务不存在')
        result = dict(task)
        result['result'] = db.loads(result.pop('result_json'), {})
        result['scope'] = SCOPE
        draft = self.store.conn.execute('SELECT title,sources_json FROM drafts WHERE knowledge_id=? AND revision=?',
                                        (result['knowledge_id'], result['revision'])).fetchone()
        if draft:
            result['title'] = draft['title']
            versions = [s['version_id'] for s in db.loads(draft['sources_json'], [])]
            result['filenames'] = [r['filename'] for version in dict.fromkeys(versions)
                                   for r in self.store.conn.execute('SELECT filename FROM file_versions WHERE version_id=?', (version,))]
        return result

    def progress(self, tid, phase, completed, total, status='publishing', result=None, error=None):
        self.store.conn.execute('UPDATE publish_tasks SET status=?,phase=?,completed=?,total=?,result_json=?,error=?,updated_at=? WHERE publish_task_id=?',
                                (status, phase, completed, total, db.dumps(result or {}), error, db.now(), tid))
        self.store.conn.commit()

    def publish_assets(self, draft, sidecar, tmp):
        refs = []
        payloads = (draft.get('quality_report') or {}).get('flowcharts', [])
        seen = set()
        for index, item in enumerate(sidecar['protected_assets']['items']):
            asset_id = item.get('asset_id')
            if not asset_id or asset_id in seen:
                raise ValueError('图形 asset_id 缺失或重复')
            seen.add(asset_id)
            raw = canonical(payloads[index])
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item['payload_sha256']:
                raise ValueError('图形结构哈希不匹配')
            path = tmp / ('diagram-' + digest + '.json')
            path.write_bytes(raw)
            stored = self.store.upload(path, draft['project_id'], uploader='local-publisher', tags=['protected_asset'])['uploaded'][0]
            checked = self.store.read_version(stored['file_id'], stored['version_id'])
            if checked['sha256'] != digest:
                raise ValueError('图形指定版本读回哈希失败')
            ref = {'asset_id': asset_id, 'file_id': stored['file_id'], 'version_id': stored['version_id'], 'sha256': digest}
            self.store.conn.execute('INSERT OR REPLACE INTO knowledge_asset_refs VALUES (?,?,?,?,?,?)',
                                    (draft['knowledge_id'], draft['revision'], asset_id, ref['file_id'], ref['version_id'], digest))
            refs.append(ref)
        self.store.conn.commit()
        return refs

    def active_record(self, kid, revision, content_hash):
        self.actor()
        draft = self.distill.get_draft(kid, revision)
        if revision < 1 or draft['content_hash'] != content_hash:
            raise ValueError('必须提供匹配的 revision 和 content_hash')
        if hashlib.sha256(draft['body_md'].encode()).hexdigest() != content_hash:
            raise ValueError('读取正文哈希不匹配，停止提供该修订')
        from chunks_and_in_vector.pipeline import SandboxRAG
        rag = SandboxRAG()
        try:
            record = rag.read(draft['stage'], kid, approved_only=True)
            if not record or record['revision'] != revision or record['json']['content_hash'] != content_hash:
                raise ValueError('知识版本未发布、已替代或已撤回')
            return draft, record
        finally:
            rag.close()

    def read(self, kid, revision, content_hash, section_ids=None, full_document=False):
        if full_document and section_ids:
            raise ValueError('full_document 与 section_ids 不能同时指定')
        draft, record = self.active_record(kid, revision, content_hash)
        sidecar = record['json']
        # extract_rag_payload also accepts Markdown without frontmatter; use the
        # immutable exported snapshot header when building an authoritative view.
        from ckp.md_render import render_frontmatter
        payload = extract_rag_payload(render_frontmatter(draft) + draft['body_md'], sidecar.get('protected_assets', {}).get('items', []), sidecar.get('rag_selection', {}).get('source_refs', []))
        result = {'knowledge_id': kid, 'revision': revision, 'content_hash': content_hash,
                  'title': draft.get('title', ''), 'stage': draft.get('stage', ''),
                  'knowledge_type': draft.get('knowledge_type', ''),
                  'products': draft.get('products', ''), 'domain': draft.get('domain', []),
                  'scope': SCOPE}
        refs = payload.get('sections', [{}])[0].get('source_refs', []) if payload.get('sections') else []
        result['source_refs'] = list({canonical(ref): ref for ref in refs}.values())
        # Document identity and identical source references belong once at the
        # response root. Repeating them per section made a directory response
        # larger than the source document despite containing no section body.
        def compact(section):
            return {k: v for k, v in section.items() if k not in ('document_ref', 'source_refs')}
        if full_document:
            result['mode'] = 'full_document'
            result['body_md'] = draft['body_md']
        elif section_ids:
            result['mode'] = 'sections'
            selected = set(section_ids)
            valid = {s['id'] for s in payload['sections'] if s['route'] == 'body'}
            if not selected <= valid:
                raise ValueError('分节不存在或不属于可读取的 body 章节')
            # Routed retrieval text contains only requested sections.
            from ckp.md_render import split_frontmatter
            import re
            _, body = split_frontmatter(render_frontmatter(draft) + draft['body_md'])
            matches = list(re.finditer(r'^#{2,6}\s+[^\n]+$', body, re.M))
            result['sections'] = [{**compact(s), 'text': body[matches[i].end():matches[i+1].start() if i+1 < len(matches) else len(body)].strip()}
                                  for i, s in enumerate(payload['sections']) if s['id'] in selected]
        else:
            result['mode'] = 'directory'
            result['sections'] = [compact(s) for s in payload['sections'] if s['route'] == 'body']
        return result

    def asset(self, kid, revision, content_hash, asset_id, section_id=None):
        draft, record = self.active_record(kid, revision, content_hash)
        expected = next((a for a in record.get('publication', {}).get('assets', []) if a['asset_id'] == asset_id), None)
        if not expected:
            raise ValueError('该资产未关联当前发布修订')
        if not section_id:
            raise ValueError('读取图形资产必须提供命中正文分节 section_id')
        from ckp.md_render import render_frontmatter
        sidecar = record.get('json') or {}
        payload = extract_rag_payload(
            render_frontmatter(draft) + draft['body_md'],
            sidecar.get('protected_assets', {}).get('items', []),
            sidecar.get('rag_selection', {}).get('source_refs', []))
        section = next((item for item in payload.get('sections', [])
                        if item.get('id') == section_id and item.get('route') == 'body'), None)
        if not section or asset_id not in section.get('protected_asset_refs', []):
            raise ValueError('该资产未被指定正文分节引用')
        row = self.store.conn.execute('SELECT * FROM knowledge_asset_refs WHERE knowledge_id=? AND revision=? AND asset_id=?', (kid, revision, asset_id)).fetchone()
        if not row:
            raise ValueError('指定资产版本映射缺失')
        if any(row[key] != expected[key] for key in ('file_id', 'version_id', 'sha256')):
            raise ValueError('资产映射与发布修订指定版本不一致')
        checked = self.store.read_version(row['file_id'], row['version_id'])
        if checked['sha256'] != row['sha256']:
            raise ValueError('资产指定版本哈希不匹配')
        blob = self.store.conn.execute('SELECT rel_path FROM blobs WHERE sha256=?', (row['sha256'],)).fetchone()
        payload = json.loads((self.store.data_dir / blob['rel_path']).read_bytes())
        return {'asset': dict(row), 'payload': payload, 'complete': True, 'scope': SCOPE}

    def withdraw(self, kid, revision, content_hash, reason):
        self.actor()
        draft, _ = self.active_record(kid, revision, content_hash)
        from chunks_and_in_vector.pipeline import SandboxRAG
        rag = SandboxRAG()
        try:
            result = rag.withdraw(draft['stage'], kid, revision=revision)
        finally:
            rag.close()
        if not result['withdrawn']:
            raise ValueError('活动知识修订已并发变化，未执行撤回')
        task = self.store.conn.execute('SELECT result_json FROM publish_tasks WHERE knowledge_id=? AND revision=? AND status=?', (kid, revision, 'published')).fetchone()
        result_snapshot = db.loads(task['result_json'], {}) if task else {}
        result_snapshot['withdrawal'] = {'reason': reason, 'actor': self.actor(), 'at': db.now()}
        self.store.conn.execute('UPDATE publish_tasks SET status=?,phase=?,result_json=?,updated_at=? WHERE knowledge_id=? AND revision=? AND status=?',
                                ('withdrawn', 'withdrawn', db.dumps(result_snapshot), db.now(), kid, revision, 'published'))
        self.store.conn.commit()
        return result | {'knowledge_id': kid, 'revision': revision, 'reason': reason, 'status': 'withdrawn', 'scope': SCOPE}


def _publish_worker(data_dir, repo_root, tid):
    # Single local writer; each worker owns its SQLite connection and RAG clients.
    with _PUBLISH_LOCK:
        service = Knowledge(data_dir, repo_root)
        rag = None
        tmp = Path(repo_root) / 'tmp' / 'publish' / tid
        try:
            task = service.task(tid)
            review = service.store.conn.execute('SELECT * FROM knowledge_reviews WHERE review_id=?', (task['review_id'],)).fetchone()
            draft, project, sidecar, digest = service.snapshot(task['knowledge_id'], task['revision'], review['content_hash'])
            if review['status'] != 'approved' or digest != task['snapshot_hash']:
                raise ValueError('审核绑定的修订已失效')
            service.progress(tid, 'verifying_sources_and_assets', 0, 0)
            tmp.mkdir(parents=True, exist_ok=True)
            assets = service.publish_assets(draft, sidecar, tmp)
            exported = export_draft(draft, project, tmp)
            from chunks_and_in_vector.pipeline import SandboxRAG
            rag = SandboxRAG()
            publication = {'review_id': review['review_id'], 'status': 'approved', 'reviewer': review['reviewer'],
                           'reviewed_at': review['reviewed_at'], 'snapshot_hash': digest,
                           'publish_task_id': tid, 'assets': assets, 'scope': SCOPE}
            result = rag.ingest(exported['md'], publication=publication,
                                progress=lambda phase, done, total: service.progress(tid, phase, done, total))
            if result.get('state') == 'unchanged' and not result.get('active'):
                raise ValueError('该版本已撤回或已替代，不会因重复发布重新激活')
            chunks = result['chunks']
            service.progress(tid, 'completed', chunks, chunks, status='published', result=result)
        except Exception as exc:
            _LOG.exception('Local publication %s failed', tid)
            # No endpoint, credential or filesystem path is sent to MCP clients.
            message = str(exc) if isinstance(exc, ValueError) and '/' not in str(exc) and '\\' not in str(exc) else type(exc).__name__ + '：发布失败，请检查服务端日志'
            service.progress(tid, 'failed', 0, 0, status='publish_failed', error=message)
        finally:
            if rag is not None:
                rag.close()
            shutil.rmtree(tmp, ignore_errors=True)
            service.close()
