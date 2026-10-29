"""Local transactional task store for K17 development.

Uses SQLite for isolated development only. Actor identities and the authorization
callback must originate from a trusted server boundary; this store is not an
identity provider or a replacement for the planned production PostgreSQL setup.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4


class ConflictError(ValueError):
    """Version, state or idempotency conflict."""


class TaskNotFound(LookupError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _required(**values: str) -> None:
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{key} is required')


class TaskStore:
    """Transactions and versioned records; one store instance per local database.

    authorize(actor, task_id, action) is called for every read/write, including
    retries. For creation task_id is None. It must consult current grants.
    """

    def __init__(self, path: str | Path, authorize: Callable[[str, str | None, str], bool]):
        self.path = str(path)
        self.authorize = authorize
        # Each operation opens its own connection. Shared :memory: would lose its
        # contents; require an explicit file so restart behavior can be checked.
        if self.path == ':memory:':
            raise ValueError('an explicit local database file is required')
        with self._transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              customer_id TEXT NOT NULL, scenario TEXT NOT NULL,
              status TEXT NOT NULL, version INTEGER NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
              task_id TEXT NOT NULL REFERENCES tasks(task_id), kind TEXT NOT NULL,
              revision INTEGER NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
              created_by TEXT NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(task_id, kind, revision));
            CREATE TABLE IF NOT EXISTS confirmations (
              confirmation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              kind TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
              allowed_actor TEXT NOT NULL, state TEXT NOT NULL, decision TEXT,
              decided_by TEXT, created_at REAL NOT NULL, decided_at REAL,
              FOREIGN KEY(task_id, kind, revision) REFERENCES revisions(task_id, kind, revision));
            CREATE TABLE IF NOT EXISTS requests (
              actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
              response TEXT NOT NULL, PRIMARY KEY(actor, request_id));
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              actor TEXT NOT NULL, kind TEXT NOT NULL, input_revision INTEGER NOT NULL,
              input_hash TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, last_error TEXT,
              result_revision INTEGER, worker_type TEXT NOT NULL DEFAULT 'generic',
              FOREIGN KEY(task_id, kind, input_revision) REFERENCES revisions(task_id, kind, revision));
            CREATE TABLE IF NOT EXISTS model_calls (
              call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              job_id TEXT NOT NULL REFERENCES jobs(job_id), model TEXT NOT NULL,
              started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_events (
              event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              actor TEXT NOT NULL, trace_id TEXT NOT NULL, tool TEXT NOT NULL,
              corpus TEXT, identifier TEXT, revision INTEGER, digest TEXT,
              outcome TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              event_type TEXT NOT NULL, payload TEXT NOT NULL, delivery_key TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, sent_at REAL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'worker_type' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN worker_type TEXT NOT NULL DEFAULT 'generic'")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _check(self, actor: str, task_id: str | None, action: str) -> None:
        _required(actor=actor)
        if not self.authorize(actor, task_id, action):
            raise PermissionError('task access denied')

    @staticmethod
    def _task(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return row

    @staticmethod
    def _retry(db: sqlite3.Connection, actor: str, request_id: str,
               operation: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        _required(request_id=request_id)
        fingerprint = _digest({'operation': operation, 'arguments': arguments})
        row = db.execute('SELECT fingerprint, response FROM requests WHERE actor=? AND request_id=?',
                         (actor, request_id)).fetchone()
        if row and row['fingerprint'] != fingerprint:
            raise ConflictError('request ID was already used with different input')
        return fingerprint, json.loads(row['response']) if row else None

    @staticmethod
    def _save_retry(db: sqlite3.Connection, actor: str, request_id: str,
                    fingerprint: str, response: dict[str, Any]) -> dict[str, Any]:
        db.execute('INSERT INTO requests(actor, request_id, fingerprint, response) VALUES(?,?,?,?)',
                   (actor, request_id, fingerprint, _json(response)))
        return response

    def create_task(self, actor: str, request_id: str, org_id: str, customer_id: str,
                    scenario: str) -> dict[str, Any]:
        _required(org_id=org_id, customer_id=customer_id, scenario=scenario)
        self._check(actor, None, 'create')
        args = {'org_id': org_id, 'customer_id': customer_id, 'scenario': scenario}
        with self._transaction() as db:
            key, cached = self._retry(db, actor, request_id, 'create_task', args)
            if cached is not None:
                # A creation retry must also respect current task access.
                self._check(actor, cached['task_id'], 'read')
                return cached
            task_id = uuid4().hex
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, org_id, actor, customer_id, scenario, 'received', 1, time.time()))
            result = {'task_id': task_id, 'status': 'received', 'version': 1}
            return self._save_retry(db, actor, request_id, key, result)

    def get_task(self, actor: str, task_id: str) -> dict[str, Any]:
        _required(task_id=task_id)
        self._check(actor, task_id, 'read')
        with self._transaction() as db:
            return dict(self._task(db, task_id))

    def transition(self, actor: str, request_id: str, task_id: str,
                   expected_version: int, status: str) -> dict[str, Any]:
        """Compare-and-swap the task state; completion requires a saved result."""
        _required(task_id=task_id, status=status)
        self._check(actor, task_id, 'write')
        allowed = {'received': {'running', 'cancelled'},
                   'running': {'waiting_user', 'completed', 'failed', 'cancelled'},
                   'waiting_user': {'running', 'cancelled'},
                   'failed': {'running', 'cancelled'},
                   'completed': {'running'}, 'cancelled': set()}
        args = {'task_id': task_id, 'expected_version': expected_version, 'status': status}
        with self._transaction() as db:
            key, cached = self._retry(db, actor, request_id, 'transition', args)
            if cached is not None:
                return cached
            task = self._task(db, task_id)
            if task['version'] != expected_version or status not in allowed.get(task['status'], set()):
                raise ConflictError('task version or transition is invalid')
            if status == 'completed' and not db.execute(
                    "SELECT 1 FROM revisions WHERE task_id=? AND kind IN ('proposal','result') LIMIT 1", (task_id,)).fetchone():
                raise ConflictError('completion requires a saved result')
            db.execute('UPDATE tasks SET status=?,version=version+1 WHERE task_id=?', (status, task_id))
            result = {'task_id': task_id, 'status': status, 'version': expected_version + 1}
            return self._save_retry(db, actor, request_id, key, result)

    def add_revision(self, actor: str, request_id: str, task_id: str, kind: str,
                     payload: Any, expected_version: int) -> dict[str, Any]:
        _required(task_id=task_id, kind=kind)
        if kind not in {'input', 'profile', 'requirement', 'proposal', 'result'}:
            raise ValueError('unsupported revision kind')
        self._check(actor, task_id, 'write')
        args = {'task_id': task_id, 'kind': kind, 'payload': payload, 'expected_version': expected_version}
        with self._transaction() as db:
            key, cached = self._retry(db, actor, request_id, 'add_revision', args)
            if cached is not None:
                return cached
            task = self._task(db, task_id)
            if task['version'] != expected_version or task['status'] in {'completed', 'cancelled'}:
                raise ConflictError('task version or state changed')
            revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM revisions WHERE task_id=? AND kind=?',
                                  (task_id, kind)).fetchone()[0]
            content_hash = _digest(payload)
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?)',
                       (task_id, kind, revision, _json(payload), content_hash, actor, time.time()))
            db.execute('UPDATE tasks SET version=version+1 WHERE task_id=?', (task_id,))
            result = {'task_id': task_id, 'kind': kind, 'revision': revision,
                      'content_hash': content_hash, 'task_version': expected_version + 1}
            return self._save_retry(db, actor, request_id, key, result)

    def get_revision(self, actor: str, task_id: str, kind: str, revision: int) -> dict[str, Any]:
        self._check(actor, task_id, 'read')
        with self._transaction() as db:
            self._task(db, task_id)
            row = db.execute('SELECT * FROM revisions WHERE task_id=? AND kind=? AND revision=?',
                             (task_id, kind, revision)).fetchone()
            if row is None:
                raise TaskNotFound('revision')
            result = dict(row)
            result['payload'] = json.loads(result['payload'])
            return result

    def get_confirmation(self, actor: str, confirmation_id: str) -> dict[str, Any]:
        _required(confirmation_id=confirmation_id)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM confirmations WHERE confirmation_id=?', (confirmation_id,)).fetchone()
            if row is None:
                raise TaskNotFound('confirmation')
            self._check(actor, row['task_id'], 'read')
            return dict(row)

    def request_confirmation(self, actor: str, request_id: str, task_id: str, kind: str,
                             revision: int, content_hash: str, allowed_actor: str) -> dict[str, Any]:
        _required(task_id=task_id, kind=kind, content_hash=content_hash, allowed_actor=allowed_actor)
        self._check(actor, task_id, 'write')
        args = {'task_id': task_id, 'kind': kind, 'revision': revision,
                'content_hash': content_hash, 'allowed_actor': allowed_actor}
        with self._transaction() as db:
            key, cached = self._retry(db, actor, request_id, 'request_confirmation', args)
            if cached is not None:
                return cached
            task = self._task(db, task_id)
            if task['status'] in {'completed', 'cancelled'}:
                raise ConflictError('cannot request confirmation for a closed task')
            latest = db.execute('SELECT revision, content_hash FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (task_id, kind)).fetchone()
            if not latest or latest['revision'] != revision or latest['content_hash'] != content_hash:
                raise ConflictError('confirmation target is not the current exact revision')
            confirmation_id = uuid4().hex
            db.execute('INSERT INTO confirmations(confirmation_id,task_id,kind,revision,content_hash,allowed_actor,state,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (confirmation_id, task_id, kind, revision, content_hash, allowed_actor, 'pending', time.time()))
            result = {'confirmation_id': confirmation_id, 'task_id': task_id, 'state': 'pending'}
            return self._save_retry(db, actor, request_id, key, result)

    def confirm(self, actor: str, request_id: str, confirmation_id: str,
                decision: str) -> dict[str, Any]:
        _required(confirmation_id=confirmation_id)
        if decision not in {'accepted', 'rejected'}:
            raise ValueError('decision must be accepted or rejected')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM confirmations WHERE confirmation_id=?', (confirmation_id,)).fetchone()
            if row is None:
                raise TaskNotFound('confirmation')
            self._check(actor, row['task_id'], 'confirm')
            if actor != row['allowed_actor']:
                raise PermissionError('actor is not the allowed confirmer')
            args = {'confirmation_id': confirmation_id, 'decision': decision}
            key, cached = self._retry(db, actor, request_id, 'confirm', args)
            if cached is not None:
                return cached
            task = self._task(db, row['task_id'])
            if task['status'] in {'completed', 'cancelled'}:
                raise ConflictError('cannot confirm a closed task')
            latest = db.execute('SELECT revision,content_hash FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (row['task_id'], row['kind'])).fetchone()
            if row['state'] != 'pending' or not latest or (latest['revision'], latest['content_hash']) != (row['revision'], row['content_hash']):
                raise ConflictError('confirmation is stale or already decided')
            db.execute('UPDATE confirmations SET state=?,decision=?,decided_by=?,decided_at=? WHERE confirmation_id=?',
                       ('decided', decision, actor, time.time(), confirmation_id))
            event_id = uuid4().hex
            event = {'confirmation_id': confirmation_id, 'task_id': row['task_id'], 'kind': row['kind'],
                     'revision': row['revision'], 'content_hash': row['content_hash'], 'decision': decision}
            db.execute('INSERT INTO outbox(event_id,task_id,event_type,payload,delivery_key,status,created_at) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (event_id, row['task_id'], 'confirmation.decided', _json(event), event_id, 'pending', time.time()))
            result = {**event, 'state': 'decided', 'event_id': event_id}
            return self._save_retry(db, actor, request_id, key, result)

    def claim_outbox(self, actor: str, lease_seconds: int = 30) -> dict[str, Any] | None:
        self._check(actor, None, 'dispatch')
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError('lease_seconds must be between 1 and 3600')
        now = time.time()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE status='pending' OR "
                             "(status='claimed' AND lease_until<=?) ORDER BY created_at,event_id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = uuid4().hex
            db.execute("UPDATE outbox SET status='claimed', lease_token=?, lease_until=?, attempts=attempts+1 WHERE event_id=?",
                       (token, now + lease_seconds, row['event_id']))
            return {'event_id': row['event_id'], 'task_id': row['task_id'], 'event_type': row['event_type'],
                    'payload': json.loads(row['payload']), 'delivery_key': row['delivery_key'],
                    'lease_token': token, 'attempts': row['attempts'] + 1}

    def mark_outbox_sent(self, actor: str, event_id: str, lease_token: str) -> None:
        self._check(actor, None, 'dispatch')
        _required(event_id=event_id, lease_token=lease_token)
        with self._transaction() as db:
            result = db.execute("UPDATE outbox SET status='sent',sent_at=?,lease_token=NULL,lease_until=NULL "
                                "WHERE event_id=? AND status='claimed' AND lease_token=? AND lease_until>?",
                                (time.time(), event_id, lease_token, time.time()))
            if result.rowcount != 1:
                raise ConflictError('outbox lease is absent or expired')

    def enqueue_job(self, actor: str, request_id: str, task_id: str, kind: str,
                    input_revision: int, input_hash: str, *, worker_type: str = 'generic') -> dict[str, Any]:
        """Queue work tied to a currently valid, exact input revision."""
        _required(task_id=task_id, kind=kind, input_hash=input_hash)
        self._check(actor, task_id, 'write')
        if worker_type not in {'generic', 'model'}:
            raise ValueError('Unsupported worker type')
        args = {'task_id': task_id, 'kind': kind, 'input_revision': input_revision,
                'input_hash': input_hash}
        if worker_type != 'generic':
            args['worker_type'] = worker_type
        with self._transaction() as db:
            key, cached = self._retry(db, actor, request_id, 'enqueue_job', args)
            if cached is not None:
                return cached
            task = self._task(db, task_id)
            if task['status'] in {'completed', 'cancelled'}:
                raise ConflictError('task is closed')
            latest = db.execute('SELECT revision,content_hash FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (task_id, kind)).fetchone()
            if not latest or (latest['revision'], latest['content_hash']) != (input_revision, input_hash):
                raise ConflictError('job input is stale')
            job_id = uuid4().hex
            db.execute('INSERT INTO jobs(job_id,task_id,actor,kind,input_revision,input_hash,status,created_at,worker_type) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (job_id, task_id, actor, kind, input_revision, input_hash, 'pending', time.time(), worker_type))
            return self._save_retry(db, actor, request_id, key,
                                    {'job_id': job_id, 'task_id': task_id, 'status': 'pending'})

    def get_job(self, actor: str, job_id: str) -> dict[str, Any]:
        _required(job_id=job_id)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise TaskNotFound('job')
            self._check(actor, row['task_id'], 'read')
            return {key: row[key] for key in ('job_id', 'task_id', 'kind', 'input_revision',
                                               'input_hash', 'worker_type', 'status', 'attempts',
                                               'last_error', 'result_revision')}

    def claim_job(self, actor: str, lease_seconds: int = 60, max_attempts: int = 3,
                  *, worker_type: str = 'generic') -> dict[str, Any] | None:
        """Claim a job; expired claims may be recovered by another worker."""
        self._check(actor, None, 'work')
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError('lease_seconds must be between 1 and 3600')
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError('max_attempts must be between 1 and 10')
        if worker_type not in {'generic', 'model'}:
            raise ValueError('Unsupported worker type')
        now = time.time()
        with self._transaction() as db:
            db.execute("UPDATE jobs SET status='failed', last_error='attempts_exhausted', lease_token=NULL, lease_until=NULL "
                       "WHERE worker_type=? AND attempts>=? AND (status='pending' OR (status='claimed' AND lease_until<=?))",
                       (worker_type, max_attempts, now))
            row = db.execute("SELECT * FROM jobs WHERE worker_type=? AND (status='pending' OR "
                             "(status='claimed' AND lease_until<=?)) ORDER BY created_at,job_id LIMIT 1", (worker_type, now)).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute("UPDATE jobs SET status='claimed', lease_token=?, lease_until=?, attempts=attempts+1 "
                       "WHERE job_id=?", (token, now + lease_seconds, row['job_id']))
            return {'job_id': row['job_id'], 'task_id': row['task_id'], 'actor': row['actor'],
                    'kind': row['kind'], 'input_revision': row['input_revision'], 'input_hash': row['input_hash'],
                    'lease_token': token, 'attempts': row['attempts'] + 1}

    def get_claimed_job_input(self, actor: str, job_id: str, lease_token: str) -> Any:
        """Recheck the original owner's access and exact current input before work."""
        self._check(actor, None, 'work')
        _required(job_id=job_id, lease_token=lease_token)
        with self._transaction() as db:
            job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'claimed' or job['lease_token'] != lease_token or job['lease_until'] <= time.time():
                raise ConflictError('job lease is absent or expired')
            self._check(job['actor'], job['task_id'], 'read')
            task = self._task(db, job['task_id'])
            latest = db.execute('SELECT revision,content_hash,payload FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (job['task_id'], job['kind'])).fetchone()
            if task['status'] in {'completed', 'cancelled'} or not latest or (latest['revision'], latest['content_hash']) != (job['input_revision'], job['input_hash']):
                raise ConflictError('job input or task state changed')
            return json.loads(latest['payload'])

    def finish_job(self, actor: str, job_id: str, lease_token: str, result: Any) -> dict[str, Any]:
        """Atomically save the result and finish the job only under its live lease."""
        self._check(actor, None, 'work')
        _required(job_id=job_id, lease_token=lease_token)
        with self._transaction() as db:
            job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'claimed' or job['lease_token'] != lease_token or job['lease_until'] <= time.time():
                raise ConflictError('job lease is absent or expired')
            self._check(job['actor'], job['task_id'], 'read')
            self._check(job['actor'], job['task_id'], 'write')
            task = self._task(db, job['task_id'])
            latest = db.execute('SELECT revision,content_hash FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (job['task_id'], job['kind'])).fetchone()
            if task['status'] in {'completed', 'cancelled'} or not latest or (latest['revision'], latest['content_hash']) != (job['input_revision'], job['input_hash']):
                raise ConflictError('job input or task state changed')
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM revisions WHERE task_id=? AND kind='result'",
                                  (job['task_id'],)).fetchone()[0]
            digest = _digest(result)
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?)',
                       (job['task_id'], 'result', revision, _json(result), digest, job['actor'], time.time()))
            db.execute('UPDATE tasks SET version=version+1 WHERE task_id=?', (job['task_id'],))
            db.execute("UPDATE jobs SET status='completed',result_revision=?,lease_token=NULL,lease_until=NULL WHERE job_id=?",
                       (revision, job_id))
            event_id = uuid4().hex
            event = {'job_id': job_id, 'task_id': job['task_id'], 'result_revision': revision, 'content_hash': digest}
            db.execute('INSERT INTO outbox(event_id,task_id,event_type,payload,delivery_key,status,created_at) '
                       'VALUES(?,?,?,?,?,?,?)', (event_id, job['task_id'], 'job.completed', _json(event),
                                                 event_id, 'pending', time.time()))
            return event

    def fail_job(self, actor: str, job_id: str, lease_token: str, *, retryable: bool, reason: str) -> None:
        """Release or fail a job; reason is a short classification, not raw input."""
        self._check(actor, None, 'work')
        _required(job_id=job_id, lease_token=lease_token, reason=reason)
        if reason not in {'access_denied', 'stale_input', 'handler_failed'}:
            raise ValueError('Unsupported failure classification')
        with self._transaction() as db:
            updated = db.execute("UPDATE jobs SET status=?,last_error=?,lease_token=NULL,lease_until=NULL "
                                 "WHERE job_id=? AND status='claimed' AND lease_token=? AND lease_until>?",
                                 ('pending' if retryable else 'failed', reason, job_id, lease_token, time.time()))
            if updated.rowcount != 1:
                raise ConflictError('job lease is absent or expired')

    def reserve_model_call(self, actor: str, job_id: str, lease_token: str,
                           model: str, max_calls: int) -> dict[str, Any]:
        """Reserve one persisted task-level call before sending a model request."""
        self._check(actor, None, 'work')
        _required(job_id=job_id, lease_token=lease_token, model=model)
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 100:
            raise ValueError('max_calls must be between 1 and 100')
        with self._transaction() as db:
            job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'claimed' or job['lease_token'] != lease_token or job['lease_until'] <= time.time():
                raise ConflictError('job lease is absent or expired')
            self._check(job['actor'], job['task_id'], 'read')
            task = self._task(db, job['task_id'])
            latest = db.execute('SELECT revision,content_hash FROM revisions WHERE task_id=? AND kind=? '
                                'ORDER BY revision DESC LIMIT 1', (job['task_id'], job['kind'])).fetchone()
            if task['status'] in {'completed', 'cancelled'} or not latest or (latest['revision'], latest['content_hash']) != (job['input_revision'], job['input_hash']):
                raise ConflictError('job input or task state changed')
            used = db.execute('SELECT COUNT(*) FROM model_calls WHERE task_id=?', (job['task_id'],)).fetchone()[0]
            if used >= max_calls:
                raise ConflictError('task model call limit reached')
            call_id = uuid4().hex
            db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?)',
                       (call_id, job['task_id'], job_id, model, time.time(), None, 'started'))
            return {'call_id': call_id, 'task_id': job['task_id'], 'calls_used': used + 1}

    def record_model_outcome(self, actor: str, job_id: str, call_id: str, outcome: str) -> None:
        self._check(actor, None, 'work')
        _required(job_id=job_id, call_id=call_id)
        if outcome not in {'success', 'timeout', 'connection', 'rate_limit', 'authentication', 'provider',
                           'invalid_response', 'limit', 'configuration', 'unexpected'}:
            raise ValueError('Unknown model call outcome')
        with self._transaction() as db:
            result = db.execute("UPDATE model_calls SET outcome=?,finished_at=? "
                                "WHERE call_id=? AND job_id=? AND outcome='started'",
                                (outcome, time.time(), call_id, job_id))
            if result.rowcount != 1:
                raise ConflictError('model call is missing or already recorded')

    def record_knowledge_trace(self, actor: str, task_id: str, event: dict[str, Any]) -> None:
        """Persist only the authorized task's tool metadata, never query or content."""
        _required(task_id=task_id)
        self._check(actor, task_id, 'read')
        if not isinstance(event, dict):
            raise ValueError('Invalid knowledge event')
        trace_id, tool, outcome = (event.get(k) for k in ('trace_id', 'tool', 'outcome'))
        _required(trace_id=trace_id, tool=tool, outcome=outcome)
        if (len(trace_id) > 128 or tool not in {'search_knowledge', 'search_product_materials',
                'read_knowledge', 'read_product_material', 'read_sections', 'read_full_document',
                'get_asset', 'read_product_material_image'} or outcome not in {'denied', 'allowed', 'ok',
                'invalid_response', 'identity_mismatch', 'tool_error', 'transport_error'}):
            raise ValueError('Invalid knowledge event')
        corpus = event.get('corpus')
        identifier, revision, digest = (event.get(k) for k in ('identifier', 'revision', 'digest'))
        if corpus not in (None, 'project', 'product'):
            raise ValueError('Invalid knowledge corpus')
        if identifier is not None and (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256):
            raise ValueError('Invalid knowledge identity')
        if digest is not None and (not isinstance(digest, str) or not digest.strip() or len(digest) > 256):
            raise ValueError('Invalid knowledge digest')
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
            raise ValueError('Invalid knowledge revision')
        # Unauthorized identities must not be visible in a task's audit stream.
        if outcome == 'denied':
            identifier = revision = digest = None
        with self._transaction() as db:
            self._task(db, task_id)
            db.execute('INSERT INTO knowledge_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (uuid4().hex, task_id, actor, trace_id, tool, corpus, identifier, revision,
                        digest, outcome, time.time()))

    def get_knowledge_trace(self, actor: str, task_id: str, trace_id: str) -> list[dict[str, Any]]:
        _required(task_id=task_id, trace_id=trace_id)
        self._check(actor, task_id, 'read')
        with self._transaction() as db:
            self._task(db, task_id)
            rows = db.execute('SELECT trace_id,tool,corpus,identifier,revision,digest,outcome,created_at '
                              'FROM knowledge_events WHERE task_id=? AND trace_id=? ORDER BY rowid',
                              (task_id, trace_id)).fetchall()
            return [dict(row) for row in rows]
