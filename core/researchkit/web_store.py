"""Protected, server-side storage for immutable Web submission versions.

This module deliberately accepts only an already-authenticated ``Principal`` and
already-packed file bytes.  It never receives a workspace path, imports project
code, parses project metadata, or treats client supplied identity as authority.

The database protects snapshots from ordinary IDE edits only when it is owned by
a dedicated service UID and kept in a private directory.  The checks here reject
unsafe local storage layouts, but cannot protect a database from another process
running as that same service UID.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .files import KitError
from .web_identity import Principal

MAX_FILES = 1_000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DATA_REFERENCES = 1_000
_BROWSER_PAGE_DEFAULT = 20
_BROWSER_PAGE_MAX = 50
_DATABASE_NAME = 'submissions.sqlite3'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_DATASET_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_BUNDLE_ID = re.compile(r'[0-9a-f]{32}\Z')
_TRANSIENT_PATHS = {
    '.researchkit/local.json', '.researchkit/last-run.json',
    '.researchkit/run.lock', '.researchkit/review.json',
    '.researchkit/review.md', '_submission.json',
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _relative_path(value: object, *, label: str) -> str:
    """Validate a portable POSIX relative path without normalising it."""
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        raise KitError(f'{label} 必须是非空 POSIX 相对路径')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise KitError(f'{label} 必须是 UTF-8 路径') from exc
    if len(encoded) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise KitError(f'{label} 过长或包含控制字符')
    if value.startswith('/') or value.startswith('//'):
        raise KitError(f'{label} 不能是绝对路径：{value!r}')
    parts = value.split('/')
    if (any(part in ('', '.', '..') for part in parts) or ':' in parts[0] or
            any(len(part.encode('utf-8')) > 255 for part in parts)):
        raise KitError(f'{label} 包含非法路径：{value!r}')
    return value


def _record_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise KitError(f'{label} 无效')
    return value


def _validate_files(files: object) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    if type(files) is not dict or not files:
        raise KitError('提交文件必须是非空 dict[str, bytes]')
    if len(files) > MAX_FILES:
        raise KitError('提交文件数超过上限')
    packed: dict[str, bytes] = {}
    total = 0
    for raw_path, raw_content in files.items():
        path = _relative_path(raw_path, label='提交文件路径')
        if path in _TRANSIENT_PATHS or path == '_runs' or path.startswith('_runs/'):
            raise KitError(f'提交不能把本地运行状态当作可信快照：{path}')
        if not isinstance(raw_content, bytes):
            raise KitError(f'提交文件必须是 bytes：{path}')
        if len(raw_content) > MAX_FILE_BYTES:
            raise KitError(f'提交单个文件超过上限：{path}')
        total += len(raw_content)
        if total > MAX_TOTAL_BYTES:
            raise KitError('提交文件总量超过上限')
        if path in packed:
            raise KitError(f'提交含有重复文件：{path}')
        # bytes is immutable, and the new object prevents a bytes subclass from
        # retaining surprising behaviour after this call returns.
        packed[path] = bytes(raw_content)
    names = set(packed)
    for path in names:
        parts = path.split('/')
        if any('/'.join(parts[:index]) in names for index in range(1, len(parts))):
            raise KitError(f'提交文件与目录冲突：{path}')
    manifest = [
        {'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        for path, content in sorted(packed.items())
    ]
    return packed, manifest


def _files_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        name = path.encode('utf-8')
        digest.update(len(name).to_bytes(4, 'big'))
        digest.update(name)
        digest.update(len(files[path]).to_bytes(8, 'big'))
        digest.update(files[path])
    return digest.hexdigest()


def _project_name(name: object) -> str:
    if (not isinstance(name, str) or '/' in name or '\\' in name or '\x00' in name or
            any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise KitError('项目名称不能是路径，且长度不得超过 200')
    try:
        if len(name.encode('utf-8')) > 200:
            raise KitError('项目名称不能是路径，且长度不得超过 200')
    except UnicodeEncodeError as exc:
        raise KitError('项目名称必须是 UTF-8 文本') from exc
    return name


def _bundle(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {'schema', 'bundle_id', 'baseline_sha256'}:
        raise KitError('可信包标记格式无效')
    schema, bundle_id, baseline = value['schema'], value['bundle_id'], value['baseline_sha256']
    if schema != 'researchkit.web-bundle/v1' or not isinstance(bundle_id, str) or not _BUNDLE_ID.fullmatch(bundle_id) or not isinstance(baseline, str) or not _HASH.fullmatch(baseline):
        raise KitError('可信包标记格式无效')
    return {'schema': schema, 'bundle_id': bundle_id, 'baseline_sha256': baseline}


def _validate_data_references(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise KitError('数据引用必须是列表')
    if len(value) > MAX_DATA_REFERENCES:
        raise KitError('数据引用数超过上限')
    references: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for reference in value:
        if type(reference) is not dict or set(reference) != {'dataset_id', 'path', 'size', 'sha256'}:
            raise KitError('数据引用必须只含 dataset_id、path、size、sha256')
        dataset_id = reference['dataset_id']
        if (not isinstance(dataset_id, str) or len(dataset_id) > 128 or
                not _DATASET_ID.fullmatch(dataset_id)):
            raise KitError(f'数据集 ID 不合法：{dataset_id!r}')
        path = _relative_path(reference['path'], label='数据引用路径')
        size = reference['size']
        checksum = reference['sha256']
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise KitError('数据引用 size 必须是非负整数')
        if not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
            raise KitError('数据引用 sha256 必须是小写十六进制 SHA-256')
        identity = (dataset_id, path)
        if identity in seen:
            raise KitError(f'数据引用重复：{dataset_id}/{path}')
        seen.add(identity)
        references.append({'dataset_id': dataset_id, 'path': path, 'size': size, 'sha256': checksum})
    return references


def _principal(principal: 'Principal') -> tuple[int, str, str]:
    """Read a trusted identity adapter result; do not deserialize network input here."""
    uid = getattr(principal, 'uid', None)
    username = getattr(principal, 'username', None)
    role = getattr(principal, 'role', None)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise KitError('可信身份缺少有效 UID')
    if (not isinstance(username, str) or not username or len(username) > 32 or
            any(ord(char) < 32 or ord(char) == 127 for char in username)):
        raise KitError('可信身份缺少用户名')
    if role not in {'teacher', 'student'}:
        raise KitError('可信身份角色无效')
    if (role == 'teacher') != (username == 'ly'):
        raise KitError('可信身份的 ly 教师角色不一致')
    return uid, username, role


class SubmissionStore:
    """SQLite backed immutable project and submission records for one Web service."""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.database = self.directory / _DATABASE_NAME
        # Cursors are valid only while this service process is alive.  Their
        # signature keeps callers from changing the page boundary, filters, or
        # authenticated identity embedded in an opaque token.
        self._browser_cursor_key = os.urandom(32)
        if (not self.directory.is_absolute() or self.directory == Path(self.directory.anchor) or
                '..' in self.directory.parts):
            raise KitError('提交存储目录必须是非根绝对路径')
        self._ensure_private_storage()
        self._initialize()

    def _verify_ancestors(self) -> None:
        """Reject symlinked or third-party-writable routes to the private store."""
        ancestors = list(reversed(self.directory.parents))
        for ancestor in ancestors:
            try:
                information = os.lstat(ancestor)
            except FileNotFoundError as exc:
                raise KitError(f'提交存储父目录不存在：{ancestor}') from exc
            if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
                raise KitError('提交存储父路径必须是非链接目录')
            if information.st_uid not in {0, os.geteuid()}:
                raise KitError('提交存储父目录必须属于 root 或当前服务 UID')
            mode = stat.S_IMODE(information.st_mode)
            # The only shared ancestor accepted here is a root-owned sticky
            # temporary directory.  Production policy adds deployment checks.
            shared_tmp = (information.st_uid == 0 and bool(information.st_mode & stat.S_ISVTX)
                          and bool(mode & stat.S_IWOTH))
            if mode & (stat.S_IWGRP | stat.S_IWOTH) and not shared_tmp:
                raise KitError('提交存储父目录不能由第三方写入')

    def _ensure_private_storage(self) -> None:
        self._verify_ancestors()
        try:
            information = os.lstat(self.directory)
        except FileNotFoundError:
            self.directory.mkdir(mode=0o700)
            information = os.lstat(self.directory)
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
            raise KitError('提交存储目录必须是非链接的普通目录')
        if information.st_uid != os.geteuid():
            raise KitError('提交存储目录必须属于当前服务 UID')
        if stat.S_IMODE(information.st_mode) != 0o700:
            raise KitError('提交存储目录权限必须是 0700')
        try:
            information = os.lstat(self.database)
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(self.database, flags, 0o600)
            os.close(descriptor)
            information = os.lstat(self.database)
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            raise KitError('提交数据库必须是非链接的普通文件')
        if information.st_uid != os.geteuid():
            raise KitError('提交数据库必须属于当前服务 UID')
        if information.st_nlink != 1:
            raise KitError('提交数据库不能有硬链接')
        if stat.S_IMODE(information.st_mode) != 0o600:
            raise KitError('提交数据库权限必须是 0600')

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_storage()
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        from .web_content import register
        register(connection)
        connection.execute('PRAGMA busy_timeout = 5000')
        connection.execute('PRAGMA foreign_keys = ON')
        # DELETE journal avoids durable sidecar files; the enclosing private
        # directory is still required for the database itself.
        connection.execute('PRAGMA journal_mode = DELETE')
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """Yield one immediate SQLite transaction for a multi-step service action.

        The Web service normally keeps each public operation small.  A direct
        review publication is deliberately different: candidate creation,
        review, publication, metadata and provenance must either become visible
        together or not at all.  Keeping this helper on the store makes that
        boundary explicit, rather than emulating rollback by copying tables.
        """
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def _initialize(self) -> None:
        with self._connection() as connection:
            from .web_content import backup_before_migration
            backup_before_migration(connection, self.database)
            connection.execute('BEGIN IMMEDIATE')
            try:
                for statement in (
                    '''CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL, owner_username TEXT NOT NULL,
                        source TEXT NOT NULL CHECK (source = 'self-created'), name TEXT NOT NULL,
                        created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS submissions (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                        sequence INTEGER NOT NULL, version_major INTEGER NOT NULL, version_minor INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('minor', 'major')),
                        parent_submission_id TEXT REFERENCES submissions(id), content_sha256 TEXT NOT NULL,
                        manifest_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        UNIQUE(project_id, sequence), UNIQUE(project_id, version_major, version_minor))''',
                    '''CREATE TABLE IF NOT EXISTS submission_files (
                        submission_id TEXT NOT NULL REFERENCES submissions(id), path TEXT NOT NULL,
                        sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                        PRIMARY KEY(submission_id, path))''',
                    '''CREATE INDEX IF NOT EXISTS submissions_project_sequence
                       ON submissions(project_id, sequence DESC)''',
                    '''CREATE INDEX IF NOT EXISTS submissions_project_created_at_id
                       ON submissions(project_id, created_at DESC, id DESC)''',
                    '''CREATE INDEX IF NOT EXISTS submissions_created_at_id
                       ON submissions(created_at DESC, id DESC)''',
                    '''CREATE TABLE IF NOT EXISTS distributions (
                        id TEXT PRIMARY KEY,
                        source_submission_id TEXT NOT NULL REFERENCES submissions(id),
                        baseline_sha256 TEXT NOT NULL,
                        source_project_name TEXT NOT NULL,
                        source_version TEXT NOT NULL,
                        target_uid INTEGER NOT NULL, target_username TEXT NOT NULL,
                        teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('pending', 'claiming', 'claimed')),
                        project_id TEXT REFERENCES projects(id) UNIQUE,
                        created_at TEXT NOT NULL, deleted_at TEXT, deleted_by_username TEXT,
                        UNIQUE(source_submission_id, target_uid)
                    )''',
                    '''CREATE TABLE IF NOT EXISTS project_trusted_sources (
                        project_id TEXT PRIMARY KEY REFERENCES projects(id),
                        distribution_id TEXT NOT NULL UNIQUE REFERENCES distributions(id),
                        baseline_submission_id TEXT NOT NULL REFERENCES submissions(id),
                        baseline_sha256 TEXT NOT NULL,
                        source TEXT NOT NULL CHECK (source = 'teacher-assigned')
                    )''',
                    '''CREATE TABLE IF NOT EXISTS package_bundles (
                        bundle_id TEXT PRIMARY KEY, source_submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
                        baseline_sha256 TEXT NOT NULL, teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                        created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS project_package_sources (
                        project_id TEXT PRIMARY KEY REFERENCES projects(id), bundle_id TEXT NOT NULL REFERENCES package_bundles(bundle_id),
                        baseline_submission_id TEXT NOT NULL REFERENCES submissions(id), baseline_sha256 TEXT NOT NULL,
                        source TEXT NOT NULL CHECK (source = 'teacher-unassigned'))''',
                    '''CREATE TABLE IF NOT EXISTS package_import_receipts (
                        owner_uid INTEGER NOT NULL, owner_username TEXT NOT NULL, bundle_identity TEXT NOT NULL,
                        files_sha256 TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_uid, owner_username, bundle_identity, files_sha256))''',
                    '''CREATE INDEX IF NOT EXISTS package_receipts_project ON package_import_receipts(project_id)''',
                    '''CREATE INDEX IF NOT EXISTS distributions_target
                       ON distributions(target_uid, target_username, created_at DESC)''',
                    '''CREATE INDEX IF NOT EXISTS distributions_teacher
                       ON distributions(teacher_uid, created_at DESC)''',
                ):
                    connection.execute(statement)
                distribution_columns = {row['name'] for row in connection.execute('PRAGMA table_info(distributions)')}
                if 'deleted_at' not in distribution_columns:
                    connection.execute('ALTER TABLE distributions ADD COLUMN deleted_at TEXT')
                if 'deleted_by_username' not in distribution_columns:
                    connection.execute('ALTER TABLE distributions ADD COLUMN deleted_by_username TEXT')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_assignment_baselines (
                    teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
                    PRIMARY KEY(teacher_uid,request_id))''')
                from .web_content import normalize
                normalize(connection, 'submission_files')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def create_project(self, principal: 'Principal', name: str = '') -> dict[str, object]:
        """Create a server-owned self-created project; project.yaml is never consulted."""
        owner_uid, owner_username, _ = _principal(principal)
        name = _project_name(name)
        project_id = str(uuid.uuid4())
        created_at = _timestamp()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute(
                    'INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (project_id, owner_uid, owner_username, 'self-created', name, created_at),
                )
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'project_id': project_id, 'id': project_id, 'owner_uid': owner_uid, 'owner_username': owner_username,
                'source': 'self-created', 'name': name, 'created_at': created_at}

    def get_project(self, project_id: str, principal: 'Principal') -> dict[str, object]:
        """Return a project only to its owner; teachers cannot browse student workspaces."""
        owner_uid, owner_username, _ = _principal(principal)
        project_id = _record_id(project_id, label='项目 ID')
        with self._connection() as connection:
            record = connection.execute(self._project_query() + ' WHERE projects.id = ?', (project_id,)).fetchone()
            internal = self._internal_project(connection, project_id)
            purged = None
            if record is None and not internal:
                has_audit = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_project_purge_audit'"
                ).fetchone()
                if has_audit is not None:
                    purged = connection.execute(
                        'SELECT 1 FROM web_project_purge_audit WHERE project_id = ? AND owner_username = ? LIMIT 1',
                        (project_id, owner_username),
                    ).fetchone()
        if purged is not None:
            raise KitError('该项目中央历史记录已清理')
        if record is None or internal or (record['owner_uid'],record['owner_username']) != (owner_uid,owner_username):
            raise KitError('无权查看该项目')
        return self._project_record(record)

    def submit(self, project_id: str, principal: 'Principal', kind: str,
               files: dict[str, bytes], data_references: list[dict[str, object]], *,
               assignment_request_id: str | None = None, assignment_fingerprint: str | None = None) -> dict[str, object]:
        """Atomically store one immutable minor or major version without checking code."""
        owner_uid, owner_username, _ = _principal(principal)
        if assignment_request_id is not None:
            assignment_request_id = _record_id(assignment_request_id, label='任务请求 ID')
            if _principal(principal)[2] != 'teacher' or kind != 'major':
                raise KitError('只有教师可以生成任务固定基线')
            if not isinstance(assignment_fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', assignment_fingerprint):
                raise KitError('任务请求摘要无效')
        project_id = _record_id(project_id, label='项目 ID')
        if kind not in {'minor', 'major'}:
            raise KitError('提交版本类型必须是 minor 或 major')
        packed, file_manifest = _validate_files(files)
        references = _validate_data_references(data_references)
        manifest = {
            'schema': 'researchkit.web-submission/v1',
            'snapshot': 'all listed small files are stored in the submission database',
            'files': file_manifest,
            'data_references': {'status': 'unverified', 'references': references,
                                'snapshot': 'external data is referenced only and was not traversed or verified'},
        }
        content_sha256 = hashlib.sha256(_json(manifest).encode('utf-8')).hexdigest()
        submission_id = str(uuid.uuid4())
        created_at = _timestamp()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if assignment_request_id is not None:
                    old = connection.execute('''SELECT * FROM web_assignment_baselines
                        WHERE teacher_uid=? AND request_id=?''', (owner_uid, assignment_request_id)).fetchone()
                    if old is not None:
                        if old['teacher_username'] != principal.username or old['fingerprint'] != assignment_fingerprint:
                            raise KitError('任务请求 ID 已用于其他内容，请新建任务')
                        connection.execute('COMMIT')
                        recovered = self.get_submission(old['submission_id'], principal)
                        return {key:value for key,value in recovered.items() if key != 'files'}
                from .web_cleanup import require_available
                for data_reference in references:
                    require_available(connection, 'dataset', data_reference['dataset_id'])
                project = connection.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
                if project is None:
                    raise KitError('项目不存在')
                if self._internal_project(connection, project_id):
                    raise KitError('内部验证快照不能提交或修改')
                if (project['owner_uid'],project['owner_username']) != (owner_uid,owner_username):
                    raise KitError('只能提交自己的项目')
                previous = connection.execute(
                    'SELECT id, sequence, version_major, version_minor FROM submissions WHERE project_id = ? ORDER BY sequence DESC LIMIT 1',
                    (project_id,),
                ).fetchone()
                if previous is None:
                    sequence, major, minor, parent_id = 1, (1 if kind == 'major' else 0), (0 if kind == 'major' else 1), None
                elif kind == 'major':
                    sequence, major, minor, parent_id = previous['sequence'] + 1, previous['version_major'] + 1, 0, previous['id']
                else:
                    sequence, major, minor, parent_id = previous['sequence'] + 1, previous['version_major'], previous['version_minor'] + 1, previous['id']
                connection.execute(
                    '''INSERT INTO submissions(id, project_id, sequence, version_major, version_minor, kind,
                       parent_submission_id, content_sha256, manifest_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (submission_id, project_id, sequence, major, minor, kind, parent_id,
                     content_sha256, _json(manifest), created_at),
                )
                connection.executemany(
                    'INSERT INTO submission_files(submission_id, path, sha256, size, content) VALUES (?, ?, ?, ?, ?)',
                    [(submission_id, item['path'], item['sha256'], item['size'], packed[item['path']])
                     for item in file_manifest],
                )
                if assignment_request_id is not None:
                    connection.execute('''INSERT INTO web_assignment_baselines
                        (teacher_uid,teacher_username,request_id,fingerprint,project_id,submission_id)
                        VALUES(?,?,?,?,?,?)''', (owner_uid, principal.username, assignment_request_id,
                                                assignment_fingerprint, project_id, submission_id))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        result = self._submission_metadata({
            'id': submission_id, 'project_id': project_id, 'sequence': sequence,
            'version_major': major, 'version_minor': minor, 'kind': kind,
            'parent_submission_id': parent_id, 'content_sha256': content_sha256,
            'manifest_json': _json(manifest), 'created_at': created_at,
        })
        result['project'] = self.get_project(project_id, principal)
        return result

    def assignment_baseline(self, principal: 'Principal', request_id: str, fingerprint: str):
        uid,username,role = _principal(principal)
        if role != 'teacher':
            raise KitError('只有教师可以查看任务基线')
        request_id = _record_id(request_id, label='任务请求 ID')
        if not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',fingerprint):
            raise KitError('任务请求摘要无效')
        with self._connection() as con:
            old = con.execute('SELECT * FROM web_assignment_baselines WHERE teacher_uid=? AND request_id=?', (uid,request_id)).fetchone()
        if old is None:
            return None
        if old['teacher_username'] != username or old['fingerprint'] != fingerprint:
            raise KitError('任务请求 ID 已用于其他内容，请新建任务')
        source = self.get_submission(old['submission_id'],principal)
        return {key:value for key,value in source.items() if key != 'files'}

    def _source_files(self, connection: sqlite3.Connection, submission_id: str) -> tuple[sqlite3.Row, dict[str, bytes]]:
        record = connection.execute(self._submission_query() + ' WHERE submissions.id = ?', (submission_id,)).fetchone()
        if record is None:
            raise KitError('提交不存在')
        rows = connection.execute('SELECT path, sha256, size, content FROM submission_files WHERE submission_id = ? ORDER BY path', (submission_id,)).fetchall()
        manifest = self._stored_manifest(record)
        self._verify_files(manifest, rows)
        return record, {row['path']: bytes(row['content']) for row in rows}

    @staticmethod
    def _marker_table_exists(connection: sqlite3.Connection) -> bool:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                  ('web_asset_validation_snapshots',)).fetchone() is not None

    @staticmethod
    def _internal_project(connection: sqlite3.Connection, project_id: str) -> bool:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
            if connection.execute('SELECT 1 FROM web_submission_preparations p JOIN submissions s ON s.id=p.snapshot_id WHERE s.project_id=?',(project_id,)).fetchone():
                return True
        """Whether a project is a service-only candidate-validation snapshot.

        The marker table is introduced by ``web_asset_validation``.  Keeping
        this query tolerant of an older database makes the ordinary store safe
        to open before that optional feature is first used.
        """
        try:
            return connection.execute(
                'SELECT 1 FROM web_asset_validation_snapshots WHERE project_id = ?',
                (project_id,),
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            return False

    @classmethod
    def _internal_submission(cls, connection: sqlite3.Connection, submission_id: str) -> bool:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
            if connection.execute('SELECT 1 FROM web_submission_preparations WHERE snapshot_id=?',(submission_id,)).fetchone():
                return True
        try:
            return connection.execute(
                'SELECT 1 FROM web_asset_validation_snapshots WHERE submission_id = ?',
                (submission_id,),
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            return False

    def export_package(self, submission_id: str, principal: 'Principal') -> dict[str, object]:
        uid, username, role = _principal(principal)
        if role != 'teacher':
            raise KitError('只有教师可以导出可信包')
        submission_id = _record_id(submission_id, label='提交 ID')
        with self._connection() as connection:
            record, files = self._source_files(connection, submission_id)
            if self._internal_submission(connection, submission_id):
                raise KitError('内部验证快照不能导出')
            if record['owner_uid'] != uid or record['owner_username'] != username:
                raise KitError('只能导出自己的固定提交')
            if '.researchkit/web-bundle.json' in files:
                raise KitError('固定提交含保留可信包文件，不能导出')
            connection.execute('BEGIN IMMEDIATE')
            try:
                bundle = connection.execute('SELECT * FROM package_bundles WHERE source_submission_id = ?', (submission_id,)).fetchone()
                if bundle is None:
                    bundle_id = uuid.uuid4().hex
                    connection.execute('INSERT INTO package_bundles(bundle_id, source_submission_id, baseline_sha256, teacher_uid, teacher_username, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                                       (bundle_id, submission_id, record['content_sha256'], uid, username, _timestamp()))
                    bundle = connection.execute('SELECT * FROM package_bundles WHERE bundle_id = ?', (bundle_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'bundle': {'schema': 'researchkit.web-bundle/v1', 'bundle_id': bundle['bundle_id'], 'baseline_sha256': bundle['baseline_sha256']}, 'files': files}

    def import_package(self, principal: 'Principal', name: str, files: dict[str, bytes], bundle: object = None) -> dict[str, object]:
        uid, username, _role = _principal(principal)
        name = _project_name(name)
        packed, _manifest = _validate_files(files)
        if '.researchkit/web-bundle.json' in packed:
            raise KitError('可信包元文件必须由导入接口单独提供')
        marker = _bundle(bundle)
        digest = _files_digest(packed)
        identity = marker['bundle_id'] if marker else 'self'
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                known = None
                if marker is not None:
                    known = connection.execute('SELECT * FROM package_bundles WHERE bundle_id = ?', (marker['bundle_id'],)).fetchone()
                    if known is not None:
                        if known['baseline_sha256'] != marker['baseline_sha256']:
                            raise KitError('可信包基线摘要不匹配')
                        source, baseline_files = self._source_files(connection, known['source_submission_id'])
                        if source['content_sha256'] != known['baseline_sha256'] or baseline_files != packed:
                            raise KitError('可信包文件不等于固定教师基线；修改包暂不支持导入')
                existing = connection.execute('SELECT project_id FROM package_import_receipts WHERE owner_uid = ? AND owner_username = ? AND bundle_identity = ? AND files_sha256 = ?', (uid, username, identity, digest)).fetchone()
                if existing is not None:
                    record = connection.execute(self._project_query() + ' WHERE projects.id = ?', (existing['project_id'],)).fetchone()
                    if record is None: raise KitError('导入回执已损坏')
                    connection.execute('COMMIT')
                    return self._project_record(record)
                if known is not None:
                    distribution = connection.execute('SELECT * FROM distributions WHERE source_submission_id = ? AND target_uid = ? AND target_username = ?', (known['source_submission_id'], uid, username)).fetchone()
                    if distribution is not None:
                        if distribution['project_id'] is None:
                            project_id = str(uuid.uuid4())
                            connection.execute('INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)', (project_id, uid, username, 'self-created', distribution['source_project_name'], _timestamp()))
                            connection.execute("INSERT INTO project_trusted_sources(project_id, distribution_id, baseline_submission_id, baseline_sha256, source) VALUES (?, ?, ?, ?, 'teacher-assigned')", (project_id, distribution['id'], distribution['source_submission_id'], distribution['baseline_sha256']))
                            connection.execute("UPDATE distributions SET project_id = ?, status = 'claiming' WHERE id = ?", (project_id, distribution['id']))
                        else:
                            project_id = distribution['project_id']
                    else:
                        project_id = str(uuid.uuid4())
                        connection.execute('INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)', (project_id, uid, username, 'self-created', name, _timestamp()))
                        connection.execute("INSERT INTO project_package_sources(project_id, bundle_id, baseline_submission_id, baseline_sha256, source) VALUES (?, ?, ?, ?, 'teacher-unassigned')", (project_id, known['bundle_id'], known['source_submission_id'], known['baseline_sha256']))
                else:
                    project_id = str(uuid.uuid4())
                    connection.execute('INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)', (project_id, uid, username, 'self-created', name, _timestamp()))
                connection.execute('INSERT INTO package_import_receipts(owner_uid, owner_username, bundle_identity, files_sha256, project_id, created_at) VALUES (?, ?, ?, ?, ?, ?)', (uid, username, identity, digest, project_id, _timestamp()))
                record = connection.execute(self._project_query() + ' WHERE projects.id = ?', (project_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        if record is None: raise KitError('导入项目无法创建')
        return self._project_record(record)

    def get_package_baseline(self, project_id: str, principal: 'Principal') -> dict[str, object]:
        uid, username, role = _principal(principal)
        project_id = _record_id(project_id, label='项目 ID')
        with self._connection() as connection:
            record = connection.execute(self._project_query() + ' WHERE projects.id = ?', (project_id,)).fetchone()
            if record is None or (role != 'teacher' and (record['owner_uid'] != uid or record['owner_username'] != username)):
                raise KitError('无权查看可信包基线')
            baseline_id = record['trusted_baseline_submission_id'] if record['trusted_baseline_submission_id'] is not None else record['package_baseline_submission_id']
            baseline_sha = record['trusted_baseline_sha256'] if record['trusted_baseline_sha256'] is not None else record['package_baseline_sha256']
            if baseline_id is None: raise KitError('项目没有可信包基线')
            source, files = self._source_files(connection, baseline_id)
            if source['content_sha256'] != baseline_sha: raise KitError('可信包基线快照已损坏')
        result = self._submission_metadata(source, manifest=self._stored_manifest(source))
        result['files'] = files
        return result

    def create_distribution(self, principal: 'Principal', submission_id: str,
                            target_principal: 'Principal') -> dict[str, object]:
        return self.create_distributions(principal, submission_id, [target_principal])[0]

    def create_distributions(self, principal: 'Principal', submission_id: str,
                             targets: list['Principal'], *, trial: bool = False) -> list[dict[str, object]]:
        """Validate recipients first, then atomically expose all assignments."""
        teacher_uid, teacher_username, role = _principal(principal)
        if role != 'teacher':
            raise KitError('只有教师可以分发提交')
        if not targets or len(targets) > 100:
            raise KitError('分发需选择 1 至 100 个目标账号')
        identities = [_principal(target) for target in targets]
        if len({row[0] for row in identities}) != len(identities):
            raise KitError('分发目标不能重复')
        submission_id = _record_id(submission_id, label='提交 ID')
        source = self.get_submission(submission_id, principal)
        if not trial and (source['project']['owner_uid'],source['project']['owner_username']) != (teacher_uid,teacher_username):
            raise KitError('只能分发自己的固定提交')
        results = []
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                for target_uid, target_username, _role in identities:
                    record = connection.execute(
                        'SELECT * FROM distributions WHERE source_submission_id = ? AND target_uid = ?',
                        (submission_id, target_uid)).fetchone()
                    if record is None:
                        distribution_id = str(uuid.uuid4())
                        connection.execute('''INSERT INTO distributions(
                            id,source_submission_id,baseline_sha256,source_project_name,source_version,
                            target_uid,target_username,teacher_uid,teacher_username,status,project_id,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,?)''',
                            (distribution_id,submission_id,source['content_sha256'],source['project']['name'],
                             source['version'],target_uid,target_username,teacher_uid,teacher_username,_timestamp()))
                        record = connection.execute('SELECT * FROM distributions WHERE id=?', (distribution_id,)).fetchone()
                    elif record['target_username'] != target_username:
                        raise KitError('目标 UID 已被不同用户名复用，不能复用旧分发')
                    results.append(self._distribution_record(record))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return results

    def get_submission(self, submission_id: str, principal: 'Principal') -> dict[str, object]:
        """Read a fixed byte snapshot: owner, or a teacher reviewing submitted versions."""
        uid, _, role = _principal(principal)
        submission_id = _record_id(submission_id, label='提交 ID')
        with self._connection() as connection:
            record = connection.execute(
                self._submission_query() + ' WHERE submissions.id = ?',
                (submission_id,),
            ).fetchone()
            if record is None:
                raise KitError('提交不存在')
            if self._internal_submission(connection, submission_id):
                raise KitError('内部验证快照不能作为普通提交读取')
            if role != 'teacher' and (record['owner_uid'],record['owner_username']) != (uid,principal.username):
                raise KitError('无权查看该提交')
            file_rows = connection.execute(
                'SELECT path, sha256, size, content FROM submission_files WHERE submission_id = ? ORDER BY path',
                (submission_id,),
            ).fetchall()
        manifest = self._stored_manifest(record)
        self._verify_files(manifest, file_rows)
        result = self._submission_metadata(record, manifest=manifest)
        result['project'] = self._project_record(record)
        result['files'] = {row['path']: bytes(row['content']) for row in file_rows}
        return result

    @staticmethod
    def _browser_text(value: object, *, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise KitError(f'{label} 必须是字符串')
        try:
            encoded = value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise KitError(f'{label} 必须是 UTF-8 文本') from exc
        if len(encoded) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise KitError(f'{label} 过长或包含控制字符')
        return value.strip() or None

    @staticmethod
    def _browser_date(value: object, *, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise KitError(f'{label} 必须是 YYYY-MM-DD')
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise KitError(f'{label} 必须是 YYYY-MM-DD') from exc
        if value != parsed.isoformat():
            raise KitError(f'{label} 必须是 YYYY-MM-DD')
        return value

    def _browser_filters(self, *, q: object = None, owner: object = None, kind: object = 'all',
                         from_date: object = None, to_date: object = None, limit: object = _BROWSER_PAGE_DEFAULT,
                         cursor: object = None) -> dict[str, object]:
        query = self._browser_text(q, label='q')
        owner_name = self._browser_text(owner, label='owner')
        if kind is None:
            kind = 'all'
        if not isinstance(kind, str) or kind not in {'all', 'major', 'minor'}:
            raise KitError('kind 必须是 all、major 或 minor')
        start = self._browser_date(from_date, label='from_date')
        end = self._browser_date(to_date, label='to_date')
        if start is not None and end is not None and start > end:
            raise KitError('from_date 不能晚于 to_date')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _BROWSER_PAGE_MAX:
            raise KitError(f'limit 必须是 1 到 {_BROWSER_PAGE_MAX} 的整数')
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
            raise KitError('cursor 无效')
        return {'q': query, 'owner': owner_name, 'kind': kind, 'from_date': start,
                'to_date': end, 'limit': limit, 'cursor': cursor}

    @staticmethod
    def _browser_utc_boundary(day: str, *, end: bool = False) -> str:
        try:
            local_day = date.fromisoformat(day) + (timedelta(days=1) if end else timedelta())
            local = datetime(local_day.year, local_day.month, local_day.day,
                             tzinfo=timezone(timedelta(hours=8)))
            return local.astimezone(timezone.utc).isoformat()
        except OverflowError as exc:
            raise KitError('日期超出可筛选范围') from exc

    @staticmethod
    def _browser_like(value: str) -> str:
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    def _browser_cursor(self, *, principal: 'Principal', filters: dict[str, object], snapshot: int,
                        after: dict[str, str], scope: str) -> str:
        uid, username, role = _principal(principal)
        payload = {'v': 1, 'scope': scope, 'identity': [uid, username, role],
                   'filters': {key: filters[key] for key in ('q', 'owner', 'kind', 'from_date', 'to_date')},
                   'snapshot': snapshot, 'after': after}
        raw = _json(payload).encode('utf-8')
        signature = hmac.new(self._browser_cursor_key, raw, hashlib.sha256).digest()
        return (base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') + '.' +
                base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii'))

    def _read_browser_cursor(self, cursor: str | None, *, principal: 'Principal', filters: dict[str, object],
                             scope: str) -> tuple[int | None, dict[str, str] | None]:
        if cursor is None:
            return None, None
        try:
            payload_text, signature_text = cursor.split('.', 1)
            def decode(value: str) -> bytes:
                return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
            raw, signature = decode(payload_text), decode(signature_text)
            expected = hmac.new(self._browser_cursor_key, raw, hashlib.sha256).digest()
            payload = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KitError('分页游标无效或已过期') from exc
        uid, username, role = _principal(principal)
        expected_filters = {key: filters[key] for key in ('q', 'owner', 'kind', 'from_date', 'to_date')}
        if (not hmac.compare_digest(signature, expected) or type(payload) is not dict or
                payload.get('v') != 1 or payload.get('scope') != scope or
                payload.get('identity') != [uid, username, role] or payload.get('filters') != expected_filters or
                isinstance(payload.get('snapshot'), bool) or not isinstance(payload.get('snapshot'), int) or
                payload['snapshot'] < 1 or type(payload.get('after')) is not dict):
            raise KitError('分页游标无效或已过期')
        after = payload['after']
        if set(after) != {'created_at', 'project_id'} or not all(isinstance(after[key], str) and after[key]
                                                                 for key in after):
            raise KitError('分页游标无效或已过期')
        return payload['snapshot'], after

    def _browser_where(self, connection: sqlite3.Connection, principal: 'Principal', filters: dict[str, object],
                       *, project_id: str | None = None, snapshot: int | None = None) -> tuple[list[str], list[object]]:
        uid, username, role = _principal(principal)
        where, values = [], []
        if role != 'teacher' or filters.get('_personal',False):
            where.extend(('projects.owner_uid = ?', 'projects.owner_username = ?'))
            values.extend((uid, username))
        if project_id is not None:
            where.append('submissions.project_id = ?')
            values.append(project_id)
        if filters['owner'] is not None:
            where.append('projects.owner_username = ?')
            values.append(filters['owner'])
        if filters['kind'] != 'all':
            where.append('submissions.kind = ?')
            values.append(filters['kind'])
        if filters['from_date'] is not None:
            where.append('submissions.created_at >= ?')
            values.append(self._browser_utc_boundary(filters['from_date']))
        if filters['to_date'] is not None:
            where.append('submissions.created_at < ?')
            values.append(self._browser_utc_boundary(filters['to_date'], end=True))
        if filters['q'] is not None:
            like = '%' + self._browser_like(filters['q']) + '%'
            where.append("(projects.name LIKE ? ESCAPE '\\' OR projects.owner_username LIKE ? ESCAPE '\\' OR submissions.project_id LIKE ? ESCAPE '\\' OR submissions.id LIKE ? ESCAPE '\\' OR (CAST(submissions.version_major AS TEXT) || '.' || CAST(submissions.version_minor AS TEXT)) LIKE ? ESCAPE '\\')")
            values.extend((like, like, like, like, like))
        if snapshot is not None:
            where.append('submissions.rowid <= ?')
            values.append(snapshot)
        if self._marker_table_exists(connection):
            where.append('''NOT EXISTS (SELECT 1 FROM web_asset_validation_snapshots AS internal
                WHERE internal.submission_id = submissions.id)''')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
            where.append('NOT EXISTS (SELECT 1 FROM web_submission_preparations p WHERE p.snapshot_id=submissions.id)')
        where.append('NOT EXISTS (SELECT 1 FROM web_assignment_baselines b WHERE b.submission_id=submissions.id)')
        return where, values

    def _browser_snapshot(self, connection: sqlite3.Connection, principal: 'Principal', filters: dict[str, object],
                          *, project_id: str | None = None) -> int | None:
        where, values = self._browser_where(connection, principal, filters, project_id=project_id)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        record = connection.execute('SELECT MAX(submissions.rowid) AS maximum FROM submissions '
                                    'JOIN projects ON projects.id = submissions.project_id' + clause, values).fetchone()
        return None if record is None or record['maximum'] is None else int(record['maximum'])

    def list_submission_projects(self, principal: 'Principal', *, q: object = None, owner: object = None,
                                 kind: object = 'all', from_date: object = None, to_date: object = None,
                                 limit: object = _BROWSER_PAGE_DEFAULT, cursor: object = None, personal: bool = False,
                                 latest_first: bool = False) -> dict[str, object]:
        """Return a bounded, stable project page for the fixed-submission browser."""
        filters = self._browser_filters(q=q, owner=owner, kind=kind, from_date=from_date, to_date=to_date,
                                        limit=limit, cursor=cursor)
        if personal:
            filters['owner']=principal.username;filters['_personal']=True
        if type(latest_first) is not bool:
            raise KitError('提交预览排序参数无效')
        scope = ('personal-projects' if personal else 'projects') + ('-latest' if latest_first else '')
        snapshot, after = self._read_browser_cursor(filters['cursor'], principal=principal, filters=filters,
                                                     scope=scope)
        with self._connection() as connection:
            if snapshot is None:
                snapshot = self._browser_snapshot(connection, principal, filters)
            if snapshot is None:
                return {'items': [], 'next_cursor': None}
            where, values = self._browser_where(connection, principal, filters, snapshot=snapshot)
            visible = self._submission_query(metadata_only=True) + (' WHERE ' + ' AND '.join(where) if where else '')
            page_where, page_values = '', []
            if after is not None:
                page_where = ' WHERE (grouped.latest_at < ? OR (grouped.latest_at = ? AND grouped.project_id < ?))'
                page_values = [after['created_at'], after['created_at'], after['project_id']]
            preview_order = 'created_at DESC, id DESC' if latest_first else "CASE kind WHEN 'major' THEN 0 ELSE 1 END, created_at DESC, id DESC"
            query = f'''WITH visible AS ({visible}),
                grouped AS (SELECT project_id, MAX(created_at) AS latest_at, COUNT(*) AS version_count
                            FROM visible GROUP BY project_id),
                ranked AS (SELECT visible.*, ROW_NUMBER() OVER (
                              PARTITION BY project_id
                              ORDER BY {preview_order}) AS preview_rank
                           FROM visible)
                SELECT ranked.*, grouped.latest_at, grouped.version_count
                FROM grouped JOIN ranked ON ranked.project_id = grouped.project_id AND ranked.preview_rank = 1
                {page_where}
                ORDER BY grouped.latest_at DESC, grouped.project_id DESC LIMIT ?'''
            records = connection.execute(query, values + page_values + [filters['limit'] + 1]).fetchall()
        page = records[:filters['limit']]
        items = []
        for record in page:
            items.append({'project': self._project_record(record), 'version_count': int(record['version_count']),
                          'latest_at': record['latest_at'],
                          'preview': self._browser_submission_metadata(record, include_project=True)})
        next_cursor = None
        if len(records) > filters['limit']:
            last = page[-1]
            next_cursor = self._browser_cursor(principal=principal, filters=filters, snapshot=snapshot,
                                                after={'created_at': last['latest_at'], 'project_id': last['project_id']},
                                                scope=scope)
        return {'items': items, 'next_cursor': next_cursor}

    def list_project_versions(self, principal: 'Principal', project_id: object, *, q: object = None,
                              owner: object = None, kind: object = 'all', from_date: object = None,
                              to_date: object = None, limit: object = _BROWSER_PAGE_DEFAULT,
                              cursor: object = None, personal: bool = False) -> dict[str, object]:
        """Return one visible project's filtered immutable history, newest first."""
        project_id = _record_id(project_id, label='项目 ID')
        filters = self._browser_filters(q=q, owner=owner, kind=kind, from_date=from_date, to_date=to_date,
                                        limit=limit, cursor=cursor)
        if personal:
            filters['owner']=principal.username;filters['_personal']=True
        snapshot, after = self._read_browser_cursor(filters['cursor'], principal=principal, filters=filters,
                                                     scope=('personal-versions:' if personal else 'versions:') + project_id)
        uid, username, role = _principal(principal)
        with self._connection() as connection:
            project = connection.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
            if (project is None or self._internal_project(connection, project_id) or
                    ((role != 'teacher' or personal) and (project['owner_uid'] != uid or project['owner_username'] != username))):
                raise KitError('无权查看该项目提交历史')
            if snapshot is None:
                snapshot = self._browser_snapshot(connection, principal, filters, project_id=project_id)
            if snapshot is None:
                return {'items': [], 'next_cursor': None}
            where, values = self._browser_where(connection, principal, filters, project_id=project_id,
                                                 snapshot=snapshot)
            keyset, key_values = '', []
            if after is not None:
                keyset = ' AND (submissions.created_at < ? OR (submissions.created_at = ? AND submissions.id < ?))'
                key_values = [after['created_at'], after['created_at'], after['project_id']]
            query = self._submission_query(metadata_only=True) + ' WHERE ' + ' AND '.join(where) + keyset + \
                ' ORDER BY submissions.created_at DESC, submissions.id DESC LIMIT ?'
            records = connection.execute(query, values + key_values + [filters['limit'] + 1]).fetchall()
        page = records[:filters['limit']]
        next_cursor = None
        if len(records) > filters['limit']:
            last = page[-1]
            next_cursor = self._browser_cursor(principal=principal, filters=filters, snapshot=snapshot,
                                                after={'created_at': last['created_at'], 'project_id': last['id']},
                                                scope=('personal-versions:' if personal else 'versions:') + project_id)
        return {'items': [self._browser_submission_metadata(record, include_project=True) for record in page],
                'next_cursor': next_cursor}

    def list_submissions(self, principal: 'Principal', project_id: str | None = None,
                         *, include_minor: bool = True, personal: bool = False) -> list[dict[str, object]]:
        """List fixed versions; majors are presented first, while minor history remains available."""
        uid, _, role = _principal(principal)
        if project_id is not None:
            project_id = _record_id(project_id, label='项目 ID')
        if not isinstance(include_minor, bool):
            raise KitError('include_minor 必须是布尔值')
        filters, values = [], []
        if role != 'teacher' or personal:
            filters.extend(['projects.owner_uid = ?', 'projects.owner_username = ?'])
            values.extend([uid,principal.username])
        if project_id is not None:
            filters.append('submissions.project_id = ?')
            values.append(project_id)
        if not include_minor:
            filters.append("submissions.kind = 'major'")
        with self._connection() as connection:
            if self._marker_table_exists(connection):
                filters.append('''NOT EXISTS (SELECT 1 FROM web_asset_validation_snapshots AS internal
                    WHERE internal.submission_id = submissions.id)''')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone():
                filters.append('NOT EXISTS (SELECT 1 FROM web_submission_preparations p WHERE p.snapshot_id=submissions.id)')
            filters.append('NOT EXISTS (SELECT 1 FROM web_assignment_baselines b WHERE b.submission_id=submissions.id)')
            where = ' WHERE ' + ' AND '.join(filters) if filters else ''
            query = self._submission_query() + where + '''
                       ORDER BY CASE submissions.kind WHEN 'major' THEN 0 ELSE 1 END,
                                submissions.version_major DESC, submissions.version_minor DESC'''
            records = connection.execute(query, values).fetchall()
        return [self._submission_metadata(record, include_project=True) for record in records]

    def list_distributions(self, principal: 'Principal', *, personal: bool = False) -> list[dict[str, object]]:
        """Teachers see what they assigned; recipients see only their own rows."""
        uid, username, role = _principal(principal)
        filters = ['distributions.deleted_at IS NULL']
        if role == 'teacher' and not personal:
            filters.extend(['teacher_uid = ?', 'teacher_username = ?'])
            values = (uid, username)
        else:
            filters.extend(['target_uid = ?', 'target_username = ?'])
            values = (uid, username)
        with self._connection() as connection:
            task_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_task_recipients'").fetchone()
            if task_tables is not None:
                filters.append('''(NOT EXISTS (
                    SELECT 1 FROM web_task_recipients recipients
                    JOIN web_task_deliveries tasks ON tasks.id=recipients.task_id
                    WHERE recipients.distribution_id=distributions.id)
                    OR EXISTS (
                    SELECT 1 FROM web_task_recipients recipients
                    JOIN web_task_deliveries tasks ON tasks.id=recipients.task_id
                    WHERE recipients.distribution_id=distributions.id AND tasks.deleted_at IS NULL))''')
            records = connection.execute('SELECT * FROM distributions WHERE ' + ' AND '.join(filters) +
                                         ' ORDER BY created_at DESC', values).fetchall()
        return [self._distribution_record(record) for record in records]

    @staticmethod
    def _distribution_claim_blocked(connection: sqlite3.Connection, record: sqlite3.Row) -> bool:
        """Pending work cannot be started after its task or legacy row is removed."""
        if record['status'] != 'pending':
            return False
        if record['deleted_at'] is not None:
            return True
        task_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_task_recipients'"
        ).fetchone()
        if task_table is None:
            return False
        has_task = connection.execute('''SELECT 1 FROM web_task_recipients recipients
            JOIN web_task_deliveries tasks ON tasks.id=recipients.task_id
            WHERE recipients.distribution_id=?''', (record['id'],)).fetchone() is not None
        if not has_task:
            return False
        has_active_task = connection.execute('''SELECT 1 FROM web_task_recipients recipients
            JOIN web_task_deliveries tasks ON tasks.id=recipients.task_id
            WHERE recipients.distribution_id=? AND tasks.deleted_at IS NULL''',
            (record['id'],)).fetchone() is not None
        return not has_active_task

    def claim_distribution(self, distribution_id: str, principal: 'Principal') -> dict[str, object]:
        """Reserve one project ID and fixed baseline for the authenticated recipient."""
        uid, username, _ = _principal(principal)
        distribution_id = _record_id(distribution_id, label='分发 ID')
        # Submissions are immutable. Read and verify the fixed source before
        # acquiring the write transaction, rather than opening a second SQLite
        # connection while that transaction holds the write lock.
        with self._connection() as connection:
            distribution = connection.execute(
                'SELECT * FROM distributions WHERE id = ? AND target_uid = ? AND target_username = ?',
                (distribution_id, uid, username),
            ).fetchone()
            blocked = distribution is not None and self._distribution_claim_blocked(connection, distribution)
        if distribution is None:
            raise KitError('无权领取该分发')
        if blocked:
            raise KitError('该布置任务已被管理员删除，无法再次启动新项目。')
        source_principal = Principal(distribution['teacher_uid'], distribution['teacher_username'], 'teacher')
        source = self.get_submission(distribution['source_submission_id'], source_principal)
        if source['content_sha256'] != distribution['baseline_sha256']:
            raise KitError('分发基线快照已损坏')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute(
                    'SELECT * FROM distributions WHERE id = ? AND target_uid = ? AND target_username = ?',
                    (distribution_id, uid, username),
                ).fetchone()
                if record is None:
                    raise KitError('无权领取该分发')
                if (record['source_submission_id'] != distribution['source_submission_id'] or
                        record['baseline_sha256'] != distribution['baseline_sha256']):
                    raise KitError('分发基线快照已损坏')
                if self._distribution_claim_blocked(connection, record):
                    raise KitError('该布置任务已被管理员删除，无法再次启动新项目。')
                if record['project_id'] is None:
                    project_id, created_at = str(uuid.uuid4()), _timestamp()
                    connection.execute(
                        'INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                        (project_id, uid, username, 'self-created', record['source_project_name'], created_at),
                    )
                    connection.execute(
                        '''INSERT INTO project_trusted_sources(
                           project_id, distribution_id, baseline_submission_id, baseline_sha256, source)
                           VALUES (?, ?, ?, ?, 'teacher-assigned')''',
                        (project_id, distribution_id, record['source_submission_id'], record['baseline_sha256']),
                    )
                    connection.execute(
                        "UPDATE distributions SET project_id = ?, status = 'claiming' WHERE id = ?",
                        (project_id, distribution_id),
                    )
                    record = connection.execute('SELECT * FROM distributions WHERE id = ?', (distribution_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        if record is None:  # pragma: no cover - guarded above
            raise KitError('分发记录无法领取')
        project = self.get_project(record['project_id'], principal)
        return {
            'distribution': self._distribution_record(record),
            'project': project,
            'files': source['files'],
            'data_references': source['manifest']['data_references']['references'],
        }

    def confirm_distribution(self, distribution_id: str, principal: 'Principal') -> dict[str, object]:
        """Mark a previously allocated distribution as durably written by its recipient."""
        uid, username, _ = _principal(principal)
        distribution_id = _record_id(distribution_id, label='分发 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute(
                    'SELECT * FROM distributions WHERE id = ? AND target_uid = ? AND target_username = ?',
                    (distribution_id, uid, username),
                ).fetchone()
                if record is None or record['project_id'] is None:
                    raise KitError('分发尚未领取或无权确认')
                if record['status'] != 'claimed':
                    connection.execute("UPDATE distributions SET status = 'claimed' WHERE id = ?", (distribution_id,))
                    record = connection.execute('SELECT * FROM distributions WHERE id = ?', (distribution_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        if record is None:  # pragma: no cover - guarded above
            raise KitError('分发记录无法确认')
        return self._distribution_record(record)

    def get_distribution_baseline(self, distribution_id: str, principal: 'Principal') -> dict[str, object]:
        """Read only the exact baseline fixed for a recipient or its assigning teacher."""
        uid, username, role = _principal(principal)
        distribution_id = _record_id(distribution_id, label='分发 ID')
        with self._connection() as connection:
            record = connection.execute('SELECT * FROM distributions WHERE id = ?', (distribution_id,)).fetchone()
        if record is None:
            raise KitError('分发不存在')
        recipient = record['target_uid'] == uid and record['target_username'] == username
        teacher = role == 'teacher' and record['teacher_uid'] == uid and record['teacher_username'] == username
        if not (recipient or teacher):
            raise KitError('无权查看该分发基线')
        source_principal = Principal(record['teacher_uid'], record['teacher_username'], 'teacher')
        source = self.get_submission(record['source_submission_id'], source_principal)
        if source['content_sha256'] != record['baseline_sha256']:
            raise KitError('分发基线快照已损坏')
        return source

    @staticmethod
    def _project_query() -> str:
        return ('SELECT projects.*, trusted.distribution_id AS trusted_distribution_id, '
                'trusted.baseline_submission_id AS trusted_baseline_submission_id, '
                'trusted.baseline_sha256 AS trusted_baseline_sha256, trusted_bundle.bundle_id AS trusted_bundle_id, package.bundle_id AS package_bundle_id, '
                'package.baseline_submission_id AS package_baseline_submission_id, package.baseline_sha256 AS package_baseline_sha256 FROM projects '
                'LEFT JOIN project_trusted_sources AS trusted ON trusted.project_id = projects.id '
                'LEFT JOIN package_bundles AS trusted_bundle ON trusted_bundle.source_submission_id = trusted.baseline_submission_id '
                'LEFT JOIN project_package_sources AS package ON package.project_id = projects.id')

    @staticmethod
    def _submission_query(*, metadata_only: bool = False) -> str:
        columns = ', '.join('submissions.' + field for field in (
            'id', 'project_id', 'sequence', 'version_major', 'version_minor', 'kind',
            'parent_submission_id', 'content_sha256', 'created_at')) if metadata_only else 'submissions.*'
        return ('SELECT ' + columns + ', projects.owner_uid, projects.owner_username, projects.source, projects.name, '
                'projects.created_at AS project_created_at, trusted.distribution_id AS trusted_distribution_id, '
                'trusted.baseline_submission_id AS trusted_baseline_submission_id, '
                'trusted.baseline_sha256 AS trusted_baseline_sha256, trusted_bundle.bundle_id AS trusted_bundle_id, package.bundle_id AS package_bundle_id, '
                'package.baseline_submission_id AS package_baseline_submission_id, package.baseline_sha256 AS package_baseline_sha256 FROM submissions '
                'JOIN projects ON projects.id = submissions.project_id '
                'LEFT JOIN project_trusted_sources AS trusted ON trusted.project_id = projects.id '
                'LEFT JOIN package_bundles AS trusted_bundle ON trusted_bundle.source_submission_id = trusted.baseline_submission_id '
                'LEFT JOIN project_package_sources AS package ON package.project_id = projects.id')

    @staticmethod
    def _project_record(record: sqlite3.Row | dict[str, object]) -> dict[str, object]:
        keys = record.keys()
        project_id = record['project_id'] if 'project_id' in keys else record['id']
        created_at = record['project_created_at'] if 'project_created_at' in keys else record['created_at']
        result: dict[str, object] = {
            'project_id': project_id, 'owner_uid': record['owner_uid'], 'owner_username': record['owner_username'],
            'source': record['source'],
            'name': record['name'], 'created_at': created_at,
        }
        if 'trusted_distribution_id' in keys and record['trusted_distribution_id'] is not None:
            result.update({
                'source': 'teacher-assigned',
                'distribution_id': record['trusted_distribution_id'],
                'baseline_submission_id': record['trusted_baseline_submission_id'],
                'baseline_sha256': record['trusted_baseline_sha256'],
                **({'bundle_id': record['trusted_bundle_id']} if 'trusted_bundle_id' in keys and record['trusted_bundle_id'] is not None else {}),
            })
        elif 'package_bundle_id' in keys and record['package_bundle_id'] is not None:
            result.update({'source': 'teacher-unassigned', 'bundle_id': record['package_bundle_id'],
                           'baseline_submission_id': record['package_baseline_submission_id'],
                           'baseline_sha256': record['package_baseline_sha256']})
        return result

    @staticmethod
    def _distribution_record(record: sqlite3.Row | dict[str, object]) -> dict[str, object]:
        return {
            'distribution_id': record['id'],
            'baseline_submission_id': record['source_submission_id'],
            'baseline_sha256': record['baseline_sha256'],
            'source_project_name': record['source_project_name'],
            'source_version': record['source_version'],
            'target_uid': record['target_uid'],
            'target_username': record['target_username'],
            'teacher_uid': record['teacher_uid'],
            'teacher_username': record['teacher_username'],
            'status': record['status'],
            'project_id': record['project_id'],
            'created_at': record['created_at'],
        }

    @staticmethod
    def _stored_manifest(record: sqlite3.Row | dict[str, object]) -> dict[str, object]:
        try:
            manifest = json.loads(record['manifest_json'])
        except (TypeError, ValueError) as exc:
            raise KitError('提交清单已损坏') from exc
        if (type(manifest) is not dict or manifest.get('schema') != 'researchkit.web-submission/v1' or
                not isinstance(manifest.get('files'), list) or
                type(manifest.get('data_references')) is not dict or
                manifest['data_references'].get('status') != 'unverified' or
                not isinstance(manifest['data_references'].get('references'), list)):
            raise KitError('提交清单已损坏')
        observed = hashlib.sha256(_json(manifest).encode('utf-8')).hexdigest()
        if observed != record['content_sha256']:
            raise KitError('提交内容哈希不匹配')
        return manifest

    @staticmethod
    def _verify_files(manifest: dict[str, object], rows: list[sqlite3.Row]) -> None:
        actual = []
        for row in rows:
            content = bytes(row['content'])
            checksum = hashlib.sha256(content).hexdigest()
            if row['size'] != len(content) or row['sha256'] != checksum:
                raise KitError('提交文件快照已损坏')
            actual.append({'path': row['path'], 'size': len(content), 'sha256': checksum})
        if manifest['files'] != actual:
            raise KitError('提交文件快照已损坏')

    @staticmethod
    def _submission_metadata(record: sqlite3.Row | dict[str, object], *, include_project: bool = False,
                             manifest: dict[str, object] | None = None) -> dict[str, object]:
        manifest = manifest if manifest is not None else SubmissionStore._stored_manifest(record)
        result: dict[str, object] = {
            'submission_id': record['id'], 'id': record['id'], 'project_id': record['project_id'],
            'sequence': record['sequence'], 'version': f"{record['version_major']}.{record['version_minor']}",
            'kind': record['kind'], 'parent_submission_id': record['parent_submission_id'],
            'content_sha256': record['content_sha256'], 'content_hash': record['content_sha256'],
            'created_at': record['created_at'], 'manifest': manifest,
            'data_status': manifest['data_references']['status'],
        }
        if include_project:
            result['project'] = SubmissionStore._project_record(record)
        return result

    @staticmethod
    def _browser_submission_metadata(record: sqlite3.Row | dict[str, object], *, include_project: bool = False) -> dict[str, object]:
        """Small list representation: a history page never carries file manifests."""
        result: dict[str, object] = {
            'submission_id': record['id'], 'id': record['id'], 'project_id': record['project_id'],
            'sequence': record['sequence'], 'version': f"{record['version_major']}.{record['version_minor']}",
            'kind': record['kind'], 'parent_submission_id': record['parent_submission_id'],
            'content_sha256': record['content_sha256'], 'content_hash': record['content_sha256'],
            'created_at': record['created_at'], 'data_status': 'unverified',
        }
        if include_project:
            result['project'] = SubmissionStore._project_record(record)
        return result
