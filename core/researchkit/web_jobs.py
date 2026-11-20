"""Persistent, broker-owned queue for running immutable Web submissions."""
from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .files import KitError
from .web_identity import Principal
from .web_store import SubmissionStore, _validate_files, _relative_path, _HASH


_OPERATIONS = {'check', 'run'}
_TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}
_MAX_LOG_BYTES = 1024 * 1024
_HEARTBEAT_SECONDS = 20
_MAX_ARTIFACT_FILES = 64
_MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(value: object) -> tuple[int, str, str]:
    if not isinstance(value, Principal):
        raise KitError('作业需要经 broker 验证的 Principal')
    uid, username, role = value.uid, value.username, value.role
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise KitError('可信身份缺少有效 UID')
    if (not isinstance(username, str) or not username or len(username) > 64 or
            any(ord(char) < 32 or ord(char) == 127 for char in username)):
        raise KitError('可信身份缺少用户名')
    if role not in {'teacher', 'student'} or (role == 'teacher') != (username == 'ly'):
        raise KitError('可信身份角色无效')
    return uid, username, role


def _instance(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200 or
            any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise KitError('worker 实例标识无效')
    return value


def _execution_uid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KitError('执行 UID 无效')
    return value


def _text(value: object, label: str, limit: int = 16_384) -> str:
    if not isinstance(value, str):
        raise KitError(f'{label} 必须是文本')
    try:
        if len(value.encode('utf-8')) > limit:
            raise KitError(f'{label} 过长')
    except UnicodeEncodeError as exc:
        raise KitError(f'{label} 必须是 UTF-8 文本') from exc
    return value


def _artifact_files(value: object) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    """Apply submission-byte validation, then the tighter result-file policy."""
    if value is None:
        return {}, []
    if type(value) is not dict:
        raise KitError('作业产物必须是 dict[str, bytes] 或 null')
    if not value:
        return {}, []
    packed, manifest = _validate_files(value)
    if len(packed) > _MAX_ARTIFACT_FILES:
        raise KitError('作业产物文件数超过上限')
    total = 0
    for path, content in packed.items():
        if not (path.startswith('results/') or path.startswith('report/')):
            raise KitError(f'作业产物只能写入 results/ 或 report/：{path}')
        if len(content) > _MAX_ARTIFACT_FILE_BYTES:
            raise KitError('作业产物单个文件超过上限')
        total += len(content)
        if total > _MAX_ARTIFACT_TOTAL_BYTES:
            raise KitError('作业产物总量超过上限')
    return packed, manifest


def _staged_artifact_manifest(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if type(value) is not list or len(value) > _MAX_ARTIFACT_FILES:
        raise KitError('暂存作业产物列表无效或过多')
    total, result, seen = 0, [], set()
    for item in value:
        if type(item) is not dict or set(item) != {'path', 'size', 'sha256'}:
            raise KitError('暂存作业产物字段无效')
        path = _relative_path(item['path'], label='暂存作业产物路径')
        if not any(path.startswith(prefix) for prefix in ('results/', 'report/', 'data/processed/', 'artifacts/')) or path in seen:
            raise KitError('暂存作业产物路径无效或重复')
        size, sha256 = item['size'], item['sha256']
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 32 * 1024**3:
            raise KitError('暂存作业产物大小无效')
        if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
            raise KitError('暂存作业产物摘要无效')
        total += size
        if total > 64 * 1024**3:
            raise KitError('暂存作业产物总量超过 64 GiB')
        seen.add(path)
        result.append({'path': path, 'size': size, 'sha256': sha256})
    return sorted(result, key=lambda item: str(item['path']))


class Jobs:
    """A small durable queue.  Worker methods are trusted broker-internal RPCs."""

    def __init__(self, store: SubmissionStore):
        if not isinstance(store, SubmissionStore):
            raise KitError('作业队列需要 SubmissionStore')
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS web_jobs (
                    run_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, owner_uid INTEGER NOT NULL,
                    owner_username TEXT NOT NULL, requester_uid INTEGER NOT NULL,
                    requester_username TEXT NOT NULL, operation TEXT NOT NULL
                        CHECK (operation IN ('check', 'run')),
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    status TEXT NOT NULL CHECK (status IN
                        ('queued', 'running', 'succeeded', 'failed', 'cancelled',
                         'timed_out', 'interrupted')),
                    worker_instance TEXT, execution_uid INTEGER, exit_code INTEGER,
                    reason TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (cancel_requested IN (0, 1)),
                    log BLOB NOT NULL DEFAULT X'', log_truncated INTEGER NOT NULL DEFAULT 0
                        CHECK (log_truncated IN (0, 1)),
                    FOREIGN KEY(submission_id) REFERENCES submissions(id)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_job_worker (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    instance_id TEXT NOT NULL, execution_uid INTEGER NOT NULL,
                    heartbeat REAL NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_job_artifacts (
                    run_id TEXT NOT NULL REFERENCES web_jobs(run_id), path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    PRIMARY KEY(run_id, path)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_job_staged_artifacts (
                    run_id TEXT NOT NULL REFERENCES web_jobs(run_id), path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, expires_at REAL NOT NULL,
                    retained_dataset_id TEXT, PRIMARY KEY(run_id, path)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_job_artifact_retention (
                    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES web_jobs(run_id),
                    path TEXT NOT NULL, name TEXT NOT NULL, requester_uid INTEGER NOT NULL,
                    requester_username TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
                        ('queued','running','succeeded','failed')), dataset_id TEXT,
                    upload_id TEXT, worker_instance TEXT, reason TEXT, created_at TEXT NOT NULL, finished_at TEXT,
                    UNIQUE(run_id, path, requester_uid, requester_username)
                )''')
                if 'worker_instance' not in {row['name'] for row in connection.execute('PRAGMA table_info(web_job_artifact_retention)')}:
                    connection.execute('ALTER TABLE web_job_artifact_retention ADD COLUMN worker_instance TEXT')
                # The marker is intentionally created with the queue schema:
                # Jobs must be able to filter service-only snapshots before the
                # optional validation service has been instantiated.
                connection.execute('''CREATE TABLE IF NOT EXISTS web_asset_validation_snapshots (
                    submission_id TEXT PRIMARY KEY REFERENCES submissions(id),
                    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
                    candidate_id TEXT NOT NULL, candidate_sha256 TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')),
                    created_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_jobs_queue
                    ON web_jobs(status, created_at, run_id)''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_jobs_owner
                    ON web_jobs(owner_uid, owner_username, created_at DESC)''')
                from .web_content import normalize
                normalize(connection, 'web_job_artifacts')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _job(record: Any, *, include_log: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            'run_id': record['run_id'], 'submission_id': record['submission_id'],
            'content_sha256': record['content_sha256'], 'owner_uid': record['owner_uid'],
            'owner_username': record['owner_username'], 'requester_uid': record['requester_uid'],
            'requester_username': record['requester_username'], 'operation': record['operation'],
            'created_at': record['created_at'], 'started_at': record['started_at'],
            'finished_at': record['finished_at'], 'status': record['status'],
            'worker_instance': record['worker_instance'], 'execution_uid': record['execution_uid'],
            'exit_code': record['exit_code'], 'reason': record['reason'],
            'cancel_requested': bool(record['cancel_requested']),
            'log_truncated': bool(record['log_truncated']),
        }
        if include_log:
            try:
                result['log'] = bytes(record['log']).decode('utf-8')
            except UnicodeDecodeError as exc:  # defensive: append_log writes UTF-8 only
                raise KitError('作业日志已损坏') from exc
        return result

    @staticmethod
    def _visible(record: Any, identity: tuple[int, str, str]) -> bool:
        uid, username, role = identity
        return role == 'teacher' or (record['owner_uid'] == uid and record['owner_username'] == username)

    def _visible_to(self, connection: Any, record: Any, identity: tuple[int, str, str]) -> bool:
        """Keep service-only validation jobs teacher-only even though they use jobs.

        Internal validation snapshots deliberately reuse the immutable queue and
        runner.  They are never ordinary workspace submissions, so an owner
        match is insufficient for their detail, cancellation, or result bytes.
        """
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
            prep=connection.execute('SELECT owner_uid,owner_username FROM web_submission_preparations WHERE snapshot_id=?',(record['submission_id'],)).fetchone()
            if prep is not None:return (prep['owner_uid'],prep['owner_username'])==identity[:2]
        if self.store._internal_submission(connection, record['submission_id']):
            return identity[2] == 'teacher'
        return self._visible(record, identity)

    @staticmethod
    def _artifact_manifest(connection: Any, run_id: str) -> list[dict[str, object]]:
        rows = connection.execute('''SELECT path, size, sha256 FROM web_job_artifacts
            WHERE run_id = ? ORDER BY path''', (run_id,)).fetchall()
        return [{'path': row['path'], 'size': row['size'], 'sha256': row['sha256']} for row in rows]

    @staticmethod
    def _staged_manifest(connection: Any, run_id: str) -> list[dict[str, object]]:
        rows = connection.execute('''SELECT path, size, sha256, expires_at, retained_dataset_id FROM web_job_staged_artifacts
            WHERE run_id = ? ORDER BY path''', (run_id,)).fetchall()
        return [{'path': row['path'], 'size': row['size'], 'sha256': row['sha256'],
                 'retained_dataset_id': row['retained_dataset_id'],
                 'expires_at': datetime.fromtimestamp(row['expires_at'], timezone.utc).isoformat()} for row in rows]

    def _worker(self, connection: Any, instance_id: str, *, heartbeat: bool = True) -> Any:
        record = connection.execute('SELECT * FROM web_job_worker WHERE singleton = 1').fetchone()
        if record is None or record['instance_id'] != instance_id:
            raise KitError('worker 实例未注册或已被替换')
        if heartbeat:
            connection.execute('UPDATE web_job_worker SET heartbeat = ? WHERE singleton = 1', (time.time(),))
        return record

    # Browser/broker-facing operations ---------------------------------
    def request(self, principal: Principal, submission_id: str, operation: str) -> dict[str, object]:
        uid, username, _role = _identity(principal)
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise KitError('作业操作必须是 check 或 run')
        # This is the sole user-facing submission read.  It establishes both
        # authorization and the immutable owner/hash identity captured below.
        submission = self.store.get_submission(submission_id, principal)
        with self.store._connection() as connection:
            owner = connection.execute('''SELECT projects.owner_uid, projects.owner_username,
                submissions.content_sha256 FROM submissions JOIN projects ON projects.id = submissions.project_id
                WHERE submissions.id = ?''', (submission_id,)).fetchone()
        if owner is None:
            raise KitError('提交不存在')
        owner_uid, owner_username = owner['owner_uid'], owner['owner_username']
        if not isinstance(owner_uid, int) or not isinstance(owner_username, str):
            raise KitError('提交所有者记录无效')
        # get_submission historically authorizes by UID; jobs keep the stricter
        # UID+username binding required for durable records.
        if _role != 'teacher' and (owner_uid != uid or owner_username != username):
            raise KitError('无权为该提交创建作业')
        content_sha256 = submission['content_sha256']
        if not isinstance(content_sha256, str) or content_sha256 != owner['content_sha256']:
            raise KitError('提交内容哈希无效')
        run_id, created_at = str(uuid.uuid4()), _now()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                owner_pending = connection.execute(
                    "SELECT COUNT(*) FROM web_jobs WHERE status = 'queued' AND owner_uid = ? AND owner_username = ?",
                    (owner_uid, owner_username)).fetchone()[0]
                queued = connection.execute("SELECT COUNT(*) FROM web_jobs WHERE status = 'queued'").fetchone()[0]
                if owner_pending >= 4:
                    raise KitError('该提交所有者的待处理作业已达上限 4')
                if queued >= 32:
                    raise KitError('待处理作业总数已达上限 32')
                connection.execute('''INSERT INTO web_jobs(
                    run_id, submission_id, content_sha256, owner_uid, owner_username,
                    requester_uid, requester_username, operation, created_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')''',
                    (run_id, submission_id, content_sha256, owner_uid, owner_username,
                     uid, username, operation, created_at))
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        if record is None:  # pragma: no cover
            raise KitError('作业无法创建')
        return self._job(record)

    def queue_internal(self, connection: Any, principal: Principal, submission_id: str,
                       content_sha256: str, operation: str) -> dict[str, object]:
        """Append a validated service-only snapshot job inside a caller transaction."""
        uid, username, role = _identity(principal)
        preparation = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
            preparation=connection.execute('SELECT owner_uid,owner_username FROM web_submission_preparations WHERE snapshot_id=?',(submission_id,)).fetchone()
        if role != 'teacher' and (preparation is None or (preparation['owner_uid'],preparation['owner_username'])!=(uid,username)):
            raise KitError('无权创建内部验证')
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise KitError('作业操作必须是 check 或 run')
        if not isinstance(content_sha256, str) or len(content_sha256) != 64:
            raise KitError('内部验证固定提交摘要无效')
        source = connection.execute('''SELECT projects.owner_uid, projects.owner_username,
            submissions.content_sha256 FROM submissions JOIN projects ON projects.id = submissions.project_id
            WHERE submissions.id = ?''', (submission_id,)).fetchone()
        if source is None or not self.store._internal_submission(connection, submission_id):
            raise KitError('内部验证快照不存在')
        if (source['owner_uid'], source['owner_username']) != (uid, username):
            raise KitError('内部验证快照所有者不一致')
        if source['content_sha256'] != content_sha256:
            raise KitError('内部验证固定提交摘要不一致')
        owner_pending = connection.execute(
            "SELECT COUNT(*) FROM web_jobs WHERE status = 'queued' AND owner_uid = ? AND owner_username = ?",
            (uid, username)).fetchone()[0]
        queued = connection.execute("SELECT COUNT(*) FROM web_jobs WHERE status = 'queued'").fetchone()[0]
        if owner_pending >= 4:
            raise KitError('该提交所有者的待处理作业已达上限 4')
        if queued >= 32:
            raise KitError('待处理作业总数已达上限 32')
        run_id, created_at = str(uuid.uuid4()), _now()
        connection.execute('''INSERT INTO web_jobs(
            run_id, submission_id, content_sha256, owner_uid, owner_username,
            requester_uid, requester_username, operation, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')''',
            (run_id, submission_id, content_sha256, uid, username, uid, username, operation, created_at))
        record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
        if record is None:  # pragma: no cover - guarded by the INSERT above
            raise KitError('内部验证作业无法创建')
        return self._job(record)

    def list(self, principal: Principal, submission_id: str | None = None, *, personal: bool = False) -> list[dict[str, object]]:
        identity = _identity(principal)
        if submission_id is not None:
            # Preserve the same user-facing authorization as submission reads.
            self.store.get_submission(submission_id, principal)
        with self.store._connection() as connection:
            if identity[2] == 'teacher' and not personal:
                query, values = ('SELECT * FROM web_jobs WHERE NOT EXISTS '
                                 '(SELECT 1 FROM web_asset_validation_snapshots AS internal '
                                 'WHERE internal.submission_id = web_jobs.submission_id)'), []
            else:
                query = ('SELECT * FROM web_jobs WHERE owner_uid = ? AND owner_username = ? AND NOT EXISTS '
                         '(SELECT 1 FROM web_asset_validation_snapshots AS internal '
                         'WHERE internal.submission_id = web_jobs.submission_id)')
                values = [identity[0], identity[1]]
            if submission_id is not None:
                query += ' WHERE ' if ' WHERE ' not in query else ' AND '
                query += 'submission_id = ?'
                values.append(submission_id)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
                query += ' AND NOT EXISTS (SELECT 1 FROM web_submission_preparations p WHERE p.snapshot_id=web_jobs.submission_id)'
            # A list response never needs the potentially 1 MiB log BLOB.
            columns = ('run_id,submission_id,content_sha256,owner_uid,owner_username,'
                       'requester_uid,requester_username,operation,created_at,started_at,finished_at,'
                       'status,worker_instance,execution_uid,exit_code,reason,cancel_requested,log_truncated')
            query = query.replace('SELECT *', 'SELECT ' + columns)
            query += ' ORDER BY created_at DESC, run_id DESC LIMIT 200'
            records = connection.execute(query, values).fetchall()
        return [self._job(record) for record in records]

    def get(self, principal: Principal, run_id: str) -> dict[str, object]:
        _identity(principal)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        with self.store._connection() as connection:
            record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
            artifacts = self._artifact_manifest(connection, run_id) if record is not None else []
            if record is None or not self._visible_to(connection, record, _identity(principal)):
                raise KitError('无权查看该作业')
            staged = self._staged_manifest(connection, run_id)
        result = self._job(record, include_log=True)
        result['artifacts'] = artifacts
        result['staged_artifacts'] = staged
        return result

    def artifacts(self, principal: Principal, run_id: str) -> dict[str, bytes]:
        identity = _identity(principal)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        with self.store._connection() as connection:
            record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
            if record is None or not self._visible_to(connection, record, identity):
                raise KitError('无权查看该作业产物')
            rows = connection.execute('''SELECT path, size, sha256, content FROM web_job_artifacts
                WHERE run_id = ? ORDER BY path''', (run_id,)).fetchall()
        result: dict[str, bytes] = {}
        for row in rows:
            content = bytes(row['content'])
            if row['size'] != len(content) or row['sha256'] != hashlib.sha256(content).hexdigest():
                raise KitError('作业产物已损坏')
            result[row['path']] = content
        return result

    def request_artifact_retention(self, principal: Principal, run_id: str, paths: object,
                                   name_prefix: object = None) -> list[dict[str, object]]:
        identity = _identity(principal)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        if type(paths) is not list or not paths or len(paths) > _MAX_ARTIFACT_FILES or any(not isinstance(path, str) for path in paths):
            raise KitError('保留产物路径必须是 1 至 64 项文本')
        if len(set(paths)) != len(paths):
            raise KitError('保留产物路径重复')
        prefix = '' if name_prefix is None else _text(name_prefix, '派生数据名称前缀', 120)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or not self._visible_to(connection, record, identity):
                    raise KitError('无权保留该作业产物')
                if record['status'] != 'succeeded':
                    raise KitError('只能保留成功作业的产物')
                rows = connection.execute('''SELECT path, expires_at, retained_dataset_id FROM web_job_staged_artifacts
                    WHERE run_id = ?''', (run_id,)).fetchall()
                available = {row['path']: row for row in rows}
                created: list[dict[str, object]] = []
                for path in paths:
                    row = available.get(path)
                    if row is None or (row['retained_dataset_id'] is None and row['expires_at'] < time.time()):
                        raise KitError('作业产物已过期或不可保留')
                    if row['retained_dataset_id'] is not None:
                        created.append({'path': path, 'status': 'succeeded', 'dataset_id': row['retained_dataset_id']})
                        continue
                    request_id = str(uuid.uuid4())
                    name = (prefix + '-' if prefix else '') + path.rsplit('/', 1)[-1]
                    name = name.encode('utf-8')[:200].decode('utf-8', errors='ignore')
                    connection.execute('''INSERT INTO web_job_artifact_retention(
                        request_id,run_id,path,name,requester_uid,requester_username,status,dataset_id,reason,created_at,finished_at)
                        VALUES (?,?,?,?,?,?,'queued',NULL,NULL,?,NULL)
                        ON CONFLICT(run_id,path,requester_uid,requester_username) DO NOTHING''',
                                       (request_id, run_id, path, name, identity[0], identity[1], _now()))
                    prior = connection.execute('''SELECT request_id,status,dataset_id,reason FROM web_job_artifact_retention
                        WHERE run_id = ? AND path = ? AND requester_uid = ? AND requester_username = ?''',
                                               (run_id, path, identity[0], identity[1])).fetchone()
                    if prior['status'] == 'failed':
                        connection.execute("""UPDATE web_job_artifact_retention SET status='queued',name=?,upload_id=NULL,
                            worker_instance=NULL,reason=NULL,finished_at=NULL WHERE request_id=?""",(name,prior['request_id']))
                        prior = connection.execute('SELECT request_id,status,dataset_id,reason FROM web_job_artifact_retention WHERE request_id=?',
                                                   (prior['request_id'],)).fetchone()
                    created.append({'path': path, 'request_id': prior['request_id'], 'status': prior['status'],
                                    'dataset_id': prior['dataset_id'], 'reason': prior['reason']})
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return created

    def claim_artifact_retention(self, instance_id: str) -> dict[str, object] | None:
        instance_id = _instance(instance_id)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                row = connection.execute('''SELECT r.*, j.owner_uid,j.owner_username,j.submission_id,
                    a.size,a.sha256,a.retained_dataset_id FROM web_job_artifact_retention r
                    JOIN web_jobs j ON j.run_id = r.run_id
                    JOIN web_job_staged_artifacts a ON a.run_id = r.run_id AND a.path = r.path
                    WHERE r.status = 'queued' AND (a.expires_at >= ? OR a.retained_dataset_id IS NOT NULL)
                    ORDER BY r.created_at,r.request_id LIMIT 1''',
                                         (time.time(),)).fetchone()
                if row is None:
                    connection.execute('COMMIT')
                    return None
                connection.execute("UPDATE web_job_artifact_retention SET status = 'running',worker_instance=? WHERE request_id = ?",
                                   (instance_id,row['request_id']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {key: row[key] for key in row.keys()}

    def finish_artifact_retention(self, instance_id: str, request_id: str, *, dataset_id: str | None,
                                  reason: str | None = None) -> None:
        instance_id = _instance(instance_id)
        if not isinstance(request_id, str) or not request_id:
            raise KitError('保留请求 ID 无效')
        if dataset_id is not None and (not isinstance(dataset_id, str) or not dataset_id):
            raise KitError('派生数据 ID 无效')
        if reason is not None:
            reason = _text(reason, '保留失败原因', 1000)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                row = connection.execute('SELECT * FROM web_job_artifact_retention WHERE request_id = ?', (request_id,)).fetchone()
                if row is None or row['status'] != 'running' or row['worker_instance'] != instance_id:
                    raise KitError('保留请求不属于当前执行服务或已结束')
                status = 'succeeded' if dataset_id is not None else 'failed'
                connection.execute('''UPDATE web_job_artifact_retention SET status = ?,dataset_id = ?,reason = ?,finished_at = ?
                    WHERE request_id = ?''', (status, dataset_id, reason, _now(), request_id))
                if dataset_id is not None:
                    connection.execute('''UPDATE web_job_staged_artifacts SET retained_dataset_id = ?
                        WHERE run_id = ? AND path = ?''', (dataset_id, row['run_id'], row['path']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def retention_info(self, instance_id: str, request_id: str) -> dict[str, object]:
        instance_id = _instance(instance_id)
        with self.store._connection() as connection:
            self._worker(connection, instance_id)
            row = connection.execute('''SELECT r.*, j.owner_uid,j.owner_username,j.submission_id,a.size,a.sha256
                FROM web_job_artifact_retention r JOIN web_jobs j ON j.run_id = r.run_id
                JOIN web_job_staged_artifacts a ON a.run_id = r.run_id AND a.path = r.path
                WHERE r.request_id = ? AND r.status = 'running' AND r.worker_instance = ?''',
                                     (request_id, instance_id)).fetchone()
        if row is None:
            raise KitError('保留请求不属于当前执行服务或已结束')
        return {key: row[key] for key in row.keys()}

    def set_retention_upload(self, instance_id: str, request_id: str, upload_id: str) -> None:
        instance_id = _instance(instance_id)
        if not isinstance(upload_id, str) or not upload_id:
            raise KitError('上传 ID 无效')
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                row = connection.execute('SELECT * FROM web_job_artifact_retention WHERE request_id = ?', (request_id,)).fetchone()
                if row is None or row['status'] != 'running' or row['upload_id'] is not None or row['worker_instance'] != instance_id:
                    raise KitError('保留请求状态无效')
                connection.execute('UPDATE web_job_artifact_retention SET upload_id = ? WHERE request_id = ?',
                                   (upload_id, request_id))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def claim_expired_staging(self, instance_id: str, *, limit: int = 64) -> list[dict[str, object]]:
        """Return expired, unretained runner files and remove their DB handles."""
        instance_id = _instance(instance_id)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                rows = connection.execute('''SELECT run_id,path FROM web_job_staged_artifacts
                    WHERE expires_at < ? AND retained_dataset_id IS NULL
                    AND NOT EXISTS(SELECT 1 FROM web_job_artifact_retention r WHERE r.run_id=web_job_staged_artifacts.run_id
                        AND r.path=web_job_staged_artifacts.path AND r.status='running')
                    ORDER BY expires_at LIMIT ?''',
                                          (time.time(), limit)).fetchall()
                connection.executemany('DELETE FROM web_job_staged_artifacts WHERE run_id = ? AND path = ?',
                                       [(row['run_id'], row['path']) for row in rows])
                connection.executemany("""UPDATE web_job_artifact_retention SET status='failed',reason='作业产物已过期',finished_at=?
                    WHERE run_id=? AND path=? AND status='queued'""",
                    [(_now(),row['run_id'],row['path']) for row in rows])
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return [{'run_id': row['run_id'], 'path': row['path']} for row in rows]

    def staging_inventory(self, instance_id: str) -> list[str]:
        with self.store._connection() as connection:
            self._worker(connection, _instance(instance_id))
            return [row['run_id'] for row in connection.execute('SELECT DISTINCT run_id FROM web_job_staged_artifacts')]

    def cancel(self, principal: Principal, run_id: str) -> dict[str, object]:
        identity = _identity(principal)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or not self._visible_to(connection, record, identity):
                    raise KitError('无权取消该作业')
                if record['status'] == 'queued':
                    connection.execute("UPDATE web_jobs SET status = 'cancelled', finished_at = ?, reason = ? WHERE run_id = ?",
                                       (_now(), '已取消', run_id))
                elif record['status'] == 'running':
                    connection.execute('UPDATE web_jobs SET cancel_requested = 1 WHERE run_id = ?', (run_id,))
                else:
                    raise KitError('作业已经结束')
                result = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return self._job(result)

    # Trusted worker operations ----------------------------------------
    def start_worker(self, instance_id: str, execution_uid: int) -> None:
        instance_id, execution_uid = _instance(instance_id), _execution_uid(execution_uid)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                # There is intentionally no retry: after an instance dies the
                # fixed submission remains available, but the prior run is final.
                connection.execute("""UPDATE web_jobs SET status = 'interrupted', finished_at = ?,
                    reason = COALESCE(reason, 'worker 重启导致运行中断') WHERE status = 'running'""", (_now(),))
                connection.execute("""UPDATE web_job_artifact_retention SET status='failed',finished_at=?,
                    reason='worker 重启导致产物保存中断；可重新申请保留' WHERE status='running'""",(_now(),))
                connection.execute('''INSERT INTO web_job_worker(singleton, instance_id, execution_uid, heartbeat)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET instance_id = excluded.instance_id,
                        execution_uid = excluded.execution_uid, heartbeat = excluded.heartbeat''',
                    (instance_id, execution_uid, time.time()))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def worker_status(self) -> dict[str, object]:
        with self.store._connection() as connection:
            record = connection.execute('SELECT * FROM web_job_worker WHERE singleton = 1').fetchone()
        if record is None:
            return {'online': False, 'execution_uid': None}
        return {'online': record['heartbeat'] >= time.time() - _HEARTBEAT_SECONDS,
                'execution_uid': record['execution_uid']}

    def claim(self, instance_id: str) -> dict[str, object] | None:
        instance_id = _instance(instance_id)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                worker = self._worker(connection, instance_id)
                record = connection.execute("SELECT * FROM web_jobs WHERE status = 'queued' ORDER BY created_at, run_id LIMIT 1").fetchone()
                if record is None:
                    connection.execute('COMMIT')
                    return None
                started = _now()
                connection.execute('''UPDATE web_jobs SET status = 'running', started_at = ?,
                    worker_instance = ?, execution_uid = ? WHERE run_id = ? AND status = 'queued' ''',
                    (started, instance_id, worker['execution_uid'], record['run_id']))
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (record['run_id'],)).fetchone()
                source, files = self.store._source_files(connection, record['submission_id'])
                if source['content_sha256'] != record['content_sha256']:
                    raise KitError('作业固定提交内容哈希不匹配')
                manifest = self.store._stored_manifest(source)
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        result = self._job(record)
        result['files'] = files
        result['data_references'] = manifest['data_references']['references']
        return result

    def poll(self, instance_id: str, run_id: str) -> dict[str, object]:
        instance_id = _instance(instance_id)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or record['status'] != 'running' or record['worker_instance'] != instance_id:
                    raise KitError('作业不属于该 worker 或已经结束')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'cancel_requested': bool(record['cancel_requested'])}

    def reject_claim(self, instance_id: str, run_id: str, reason: str) -> dict[str, object]:
        """Finish a claimed task when trusted input preparation is refused.

        This avoids leaving a task in ``running`` when the broker cannot issue
        the runner an authorized external-data description after claiming it.
        """
        instance_id = _instance(instance_id)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        reason = _text(reason, '结束原因', 1000)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or record['status'] != 'running' or record['worker_instance'] != instance_id:
                    raise KitError('作业不属于该 worker 或已经结束')
                connection.execute('''UPDATE web_jobs SET status = 'failed', finished_at = ?, reason = ?
                    WHERE run_id = ? AND status = 'running' AND worker_instance = ?''',
                                   (_now(), reason, run_id, instance_id))
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return self._job(record)

    def append_log(self, instance_id: str, run_id: str, text: str) -> None:
        instance_id, text = _instance(instance_id), _text(text, '日志', _MAX_LOG_BYTES * 2)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or record['status'] != 'running' or record['worker_instance'] != instance_id:
                    raise KitError('作业不属于该 worker 或已经结束')
                combined = bytes(record['log']) + text.encode('utf-8')
                truncated = len(combined) > _MAX_LOG_BYTES
                if truncated:
                    combined = combined[:_MAX_LOG_BYTES].decode('utf-8', 'ignore').encode('utf-8')
                connection.execute('UPDATE web_jobs SET log = ?, log_truncated = ? WHERE run_id = ?',
                                   (combined, int(bool(record['log_truncated']) or truncated), run_id))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def finish(self, instance_id: str, run_id: str, status: str,
               exit_code: int | None, reason: str | None,
               artifacts: dict[str, bytes] | None = None,
               staged_artifacts: object = None) -> dict[str, object]:
        instance_id = _instance(instance_id)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        if not isinstance(status, str) or status not in _TERMINAL:
            raise KitError('作业结束状态无效')
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise KitError('退出码必须是整数或 null')
        if reason is not None:
            reason = _text(reason, '结束原因', 4000)
        artifact_files, artifact_manifest = _artifact_files(artifacts)
        staged_manifest = _staged_artifact_manifest(staged_artifacts)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._worker(connection, instance_id)
                record = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if record is None or record['status'] != 'running' or record['worker_instance'] != instance_id:
                    raise KitError('作业不属于该 worker 或已经结束')
                if bool(record['cancel_requested']) and status == 'succeeded':
                    status, reason = 'cancelled', reason or '已取消'
                connection.execute('''UPDATE web_jobs SET status = ?, finished_at = ?, exit_code = ?, reason = ?
                    WHERE run_id = ? AND status = 'running' AND worker_instance = ?''',
                    (status, _now(), exit_code, reason, run_id, instance_id))
                connection.executemany('''INSERT INTO web_job_artifacts(run_id, path, sha256, size, content)
                    VALUES (?, ?, ?, ?, ?)''',
                    [(run_id, item['path'], item['sha256'], item['size'], artifact_files[item['path']])
                     for item in artifact_manifest])
                connection.executemany('''INSERT INTO web_job_staged_artifacts(
                    run_id,path,sha256,size,expires_at,retained_dataset_id) VALUES (?,?,?,?,?,NULL)''',
                    [(run_id, item['path'], item['sha256'], item['size'], time.time() + 7 * 24 * 3600)
                     for item in staged_manifest])
                result = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return self._job(result)
