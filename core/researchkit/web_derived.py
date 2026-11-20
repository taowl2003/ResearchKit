"""Broker-owned immutable derived datasets.

This module deliberately keeps bytes out of SQLite and out of the JSON broker
protocol except for bounded upload chunks.  A derived dataset is one immutable
file version.  Its normal DATA-REFERENCES entry therefore has the familiar
``dataset_id/path/size/sha256`` shape, with ``path`` fixed to ``data.bin``.

The broker is the only writer of the configured root.  The runner receives a
broker-authenticated mount description only after the broker has checked the
submission-scoped review grant; it never accepts a data root from a project.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sqlite3
import time
import uuid
import base64
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .files import KitError
from .web_datasets import DatasetMount
from .web_identity import Principal
from .web_store import SubmissionStore, _validate_data_references


_FILE_NAME = 'data.bin'
_CHUNK_BYTES = 1024 * 1024
_DEFAULT_MAX_FILE_BYTES = 32 * 1024**3
_DEFAULT_USER_QUOTA_BYTES = 64 * 1024**3
_DEFAULT_UPLOAD_TTL_SECONDS = 3600
_HASH_TIMEOUT_SECONDS = 540
_MAX_ACTIVE_UPLOADS_PER_ACCOUNT = 64
_MAX_ACTIVE_UPLOADS_TOTAL = 256
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(principal: object) -> tuple[int, str, str]:
    if not isinstance(principal, Principal):
        raise KitError('派生数据需要经 broker 验证的 Principal')
    uid, username, role = principal.uid, principal.username, principal.role
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise KitError('可信身份缺少有效 UID')
    if (not isinstance(username, str) or not username or len(username) > 64 or
            any(ord(char) < 32 or ord(char) == 127 for char in username)):
        raise KitError('可信身份缺少有效用户名')
    if role not in {'teacher', 'student'} or (role == 'teacher') != (username == 'ly'):
        raise KitError('可信身份角色无效')
    return uid, username, role


def _name(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 200 or
            '\x00' in value or any(ord(char) < 32 for char in value)):
        raise KitError('派生数据名称必须是最多 200 字节的非空文本')
    return value.strip()


def _upload_id(value: object) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise KitError('派生数据上传 ID 无效')
    return value


def _sha256(value: object, label: str = 'SHA-256') -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise KitError(f'{label} 必须是小写十六进制 SHA-256')
    return value


def _size(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise KitError(f'{label} 超出允许范围')
    return value


def _stable(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class DerivedDatasets:
    """Persistent immutable derived-file catalogue and streaming importer."""

    def __init__(self, store: SubmissionStore, root: Path | None, *,
                 max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
                 user_quota_bytes: int = _DEFAULT_USER_QUOTA_BYTES,
                 upload_ttl_seconds: int = _DEFAULT_UPLOAD_TTL_SECONDS):
        if not isinstance(store, SubmissionStore):
            raise KitError('派生数据需要 SubmissionStore')
        self.store = store
        self.root = Path(root) if root is not None else None
        self.max_file_bytes = _size(max_file_bytes, '派生数据单文件上限', 1024**5)
        self.user_quota_bytes = _size(user_quota_bytes, '派生数据账号配额', 1024**5)
        if self.max_file_bytes <= 0 or self.user_quota_bytes <= 0:
            raise KitError('派生数据大小上限必须大于零')
        if (isinstance(upload_ttl_seconds, bool) or not isinstance(upload_ttl_seconds, int) or
                not 60 <= upload_ttl_seconds <= 24 * 3600):
            raise KitError('派生数据上传时限无效')
        self.upload_ttl_seconds = upload_ttl_seconds
        if self.root is not None:
            self._ensure_root()
            self._initialize()
            self.cleanup_expired()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _require_enabled(self) -> Path:
        if self.root is None:
            raise KitError('管理员尚未启用派生数据存储')
        return self.root

    def _ensure_root(self) -> None:
        root = self._require_enabled()
        if not root.is_absolute() or '..' in root.parts:
            raise KitError('派生数据根目录不安全')
        try:
            info = os.lstat(root)
        except OSError as exc:
            raise KitError('派生数据根目录不可读取') from exc
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise KitError('派生数据根目录必须由 broker 所有且不可由组或其他账号写入')
        for name, mode in (('.uploads', 0o700), ('blobs', 0o2710)):
            child = root / name
            try:
                child.mkdir(mode=mode)
            except FileExistsError:
                pass
            try:
                child_info = os.lstat(child)
            except OSError as exc:
                raise KitError('派生数据内部目录不可读取') from exc
            if (stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode) or
                    child_info.st_uid != os.geteuid() or child_info.st_mode & 0o022):
                raise KitError('派生数据内部目录不可信')
            try:
                os.chmod(child, mode)
            except OSError as exc:
                raise KitError('派生数据内部目录无法设置安全权限') from exc

    @property
    def _uploads(self) -> Path:
        return self._require_enabled() / '.uploads'

    @property
    def _blobs(self) -> Path:
        return self._require_enabled() / 'blobs'

    def _initialize(self) -> None:
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS web_derived_datasets (
                    dataset_id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL,
                    owner_username TEXT NOT NULL, name TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('private','public')),
                    created_at TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    blob_name TEXT NOT NULL UNIQUE, source_kind TEXT NOT NULL
                        CHECK(source_kind IN ('upload','run')),
                    source_run_id TEXT, source_submission_id TEXT,
                    source_refs_json TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_derived_uploads (
                    upload_id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL,
                    owner_username TEXT NOT NULL, name TEXT NOT NULL,
                    expected_size INTEGER NOT NULL, expected_sha256 TEXT,
                    source_refs_json TEXT NOT NULL, temp_name TEXT NOT NULL UNIQUE,
                    received INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_derived_review_grants (
                    dataset_id TEXT NOT NULL REFERENCES web_derived_datasets(dataset_id),
                    submission_id TEXT NOT NULL REFERENCES submissions(id),
                    teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, submission_id, teacher_uid, teacher_username)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_derived_legacy_source_grants (
                    dataset_id TEXT NOT NULL REFERENCES web_derived_datasets(dataset_id),
                    source_project_id TEXT NOT NULL REFERENCES projects(id),
                    source_sha256 TEXT NOT NULL,
                    teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, source_project_id, source_sha256,
                                teacher_uid, teacher_username)
                )''')
                connection.execute('''CREATE VIEW IF NOT EXISTS web_derived_review_readers AS
                    SELECT dataset_id,teacher_uid,teacher_username FROM web_derived_review_grants
                    UNION SELECT dataset_id,teacher_uid,teacher_username FROM web_derived_legacy_source_grants''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_derived_owner
                    ON web_derived_datasets(owner_uid, owner_username, created_at DESC)''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_review_test_copies (
                    project_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    teacher_uid INTEGER NOT NULL,
                    teacher_username TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )''')
                for table, cols in (
                    ('web_derived_datasets', (
                        ('source_metadata_json', "TEXT NOT NULL DEFAULT '{}'"),
                        ('description', "TEXT NOT NULL DEFAULT ''"),
                        ('tags_json', "TEXT NOT NULL DEFAULT '[]'"),
                        ('readme_markdown', "TEXT NOT NULL DEFAULT ''"),
                        ('parent_dataset_id', 'TEXT'),
                        ('family_id', 'TEXT'),
                    )),
                    ('web_derived_uploads', (
                        ('description', "TEXT NOT NULL DEFAULT ''"),
                        ('tags_json', "TEXT NOT NULL DEFAULT '[]'"),
                        ('readme_markdown', "TEXT NOT NULL DEFAULT ''"),
                        ('parent_dataset_id', 'TEXT'),
                    )),
                ):
                    existing_cols = {r['name'] for r in connection.execute(f'PRAGMA table_info({table})')}
                    for col_name, col_def in cols:
                        if col_name not in existing_cols:
                            connection.execute(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_def}')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_derived_grants_submission
                    ON web_derived_review_grants(submission_id, dataset_id)''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_derived_legacy_source_grants_lookup
                    ON web_derived_legacy_source_grants(dataset_id, source_project_id, source_sha256)''')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _reference(record: Any) -> dict[str, object]:
        return {'dataset_id': record['dataset_id'], 'path': _FILE_NAME,
                'size': record['size'], 'sha256': record['sha256']}

    @staticmethod
    def _task_grant_can_read(connection: Any, record: Any, principal: Principal) -> bool:
        """Return whether one delivered task delegates this exact derived file.

        The task tables are protected broker state, but their relationship is
        still deliberately checked in full here: same UID *and* username,
        actual distribution, a live grant, and the exact immutable reference
        recorded in the material metadata.  A teacher's narrow review right is
        not a source right and therefore cannot satisfy the final sponsor test.
        """
        uid, username, _role = _identity(principal)
        try:
            rows = connection.execute('''SELECT materials.metadata_json,
                    tasks.teacher_uid,tasks.teacher_username
                FROM web_task_grants grants
                JOIN web_task_materials materials ON materials.id=grants.material_id
                JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
                JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
                  AND recipients.target_uid=grants.target_uid
                  AND recipients.target_username=grants.target_username
                WHERE grants.target_uid=? AND grants.target_username=?
                  AND grants.grant_kind='dataset' AND grants.revoked_at IS NULL
                  AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                  AND materials.kind='dataset' ''',
                (uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return False
            raise
        expected = DerivedDatasets._reference(record)
        for row in rows:
            try:
                metadata = json.loads(row['metadata_json'])
                reference = DerivedDatasets._references([metadata['reference']])[0]
            except (KeyError, TypeError, ValueError, KitError) as exc:
                raise KitError('任务派生数据材料记录已损坏') from exc
            if reference != expected:
                continue
            # Only an owner or a public record is a delegable source.  In
            # particular, a review grant to ly never becomes a sharing grant.
            if (record['visibility'] == 'public' or
                    (record['owner_uid'], record['owner_username']) ==
                    (row['teacher_uid'], row['teacher_username'])):
                return True
        try:
            cascaded = connection.execute('''SELECT grantor_uid, grantor_username, reference_json
                FROM web_case_cascaded_grants
                WHERE resource_kind='dataset' AND resource_ref=?
                  AND (public=1 OR (target_uid=? AND target_username=?))''',
                (record['dataset_id'], uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                cascaded = []
            else:
                raise
        try:
            preserved = connection.execute('''SELECT grantor_uid, grantor_username, reference_json
                FROM web_preserved_dataset_grants
                WHERE resource_ref=?
                  AND (public=1 OR (target_uid=? AND target_username=?))''',
                (record['dataset_id'], uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                preserved = []
            else:
                raise
        cascaded = list(cascaded) + list(preserved)
        for crow in cascaded:
            if crow['reference_json'] and crow['reference_json'] != '{}':
                try:
                    cref = DerivedDatasets._references([json.loads(crow['reference_json'])])[0]
                    if cref != expected:
                        continue
                except (ValueError, TypeError, KitError):
                    continue
            if (record['visibility'] == 'public' or
                    (record['owner_uid'], record['owner_username']) == (crow['grantor_uid'], crow['grantor_username'])):
                return True
        return False

    def _task_granted_dataset_ids(self, connection: Any, principal: Principal) -> set[str]:
        """Find active exact task grants suitable for catalogue queries."""
        uid, username, _role = _identity(principal)
        try:
            rows = connection.execute('''SELECT materials.metadata_json
                FROM web_task_grants grants
                JOIN web_task_materials materials ON materials.id=grants.material_id
                JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
                JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
                  AND recipients.target_uid=grants.target_uid
                  AND recipients.target_username=grants.target_username
                WHERE grants.target_uid=? AND grants.target_username=?
                  AND grants.grant_kind='dataset' AND grants.revoked_at IS NULL
                  AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                  AND materials.kind='dataset' ''',
                (uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return set()
            raise
        references: list[dict[str, object]] = []
        for row in rows:
            try:
                references.append(self._references([json.loads(row['metadata_json'])['reference']])[0])
            except (KeyError, TypeError, ValueError, KitError) as exc:
                raise KitError('任务派生数据材料记录已损坏') from exc
        records = self._records_for_references(connection, references)
        result: set[str] = set()
        for reference in references:
            record = records.get(reference['dataset_id'])
            if record is not None and reference == self._reference(record) and self._task_grant_can_read(connection, record, principal):
                result.add(str(record['dataset_id']))
        try:
            cascaded_rows = connection.execute('''SELECT DISTINCT resource_ref FROM web_case_cascaded_grants
                WHERE resource_kind='dataset' AND (public=1 OR (target_uid=? AND target_username=?))''',
                (uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                cascaded_rows = []
            else:
                raise
        try:
            preserved_rows = connection.execute('''SELECT DISTINCT resource_ref FROM web_preserved_dataset_grants
                WHERE public=1 OR (target_uid=? AND target_username=?)''',
                (uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                preserved_rows = []
            else:
                raise
        cascaded_rows = list(cascaded_rows) + list(preserved_rows)
        if cascaded_rows:
            c_ids = sorted({str(r['resource_ref']) for r in cascaded_rows if _UUID.fullmatch(str(r['resource_ref']))})
            if c_ids:
                marks = ','.join('?' for _ in c_ids)
                for rec in connection.execute(f'SELECT * FROM web_derived_datasets WHERE dataset_id IN ({marks})', c_ids).fetchall():
                    if self._task_grant_can_read(connection, rec, principal):
                        result.add(str(rec['dataset_id']))
        return result

    @staticmethod
    def _source(record: Any) -> dict[str, object]:
        result: dict[str, object] = {'kind': record['source_kind']}
        if record['source_kind'] == 'upload':
            result['verification'] = 'user-declared'
        if record['source_run_id'] is not None:
            result['run_id'] = record['source_run_id']
        if record['source_submission_id'] is not None:
            result['submission_id'] = record['source_submission_id']
        try:
            refs = json.loads(record['source_refs_json'])
        except (TypeError, ValueError) as exc:  # defensive DB corruption check
            raise KitError('派生数据来源记录已损坏') from exc
        if refs:
            result['source_refs'] = refs
        raw_metadata = record['source_metadata_json'] if 'source_metadata_json' in record.keys() else '{}'
        try:
            metadata = json.loads(raw_metadata or '{}')
        except (TypeError, ValueError) as exc:
            raise KitError('派生数据来源元数据已损坏') from exc
        if not isinstance(metadata, dict):
            raise KitError('派生数据来源元数据已损坏')
        if metadata:
            result['metadata'] = metadata
        return result

    @staticmethod
    def validate_parent_lineage(connection: Any, dataset_id: str | None, parent_id: str | None,
                                *, current_row: Any = None) -> tuple[Any | None, str]:
        """Validate parent_dataset_id existence, disallow self/cycles, and forbid mutating public lineage."""
        cur_family = str(dataset_id or '')
        if current_row is not None:
            ckeys = set(current_row.keys())
            cur_parent = current_row['parent_dataset_id'] if 'parent_dataset_id' in ckeys else None
            cur_family = str(current_row['family_id']) if ('family_id' in ckeys and current_row['family_id']) else str(dataset_id or '')
            if str(current_row['visibility']) == 'public' and parent_id is not None and parent_id != cur_parent:
                raise KitError('已公开的派生数据集不允许篡改版本族或父数据集引用')
        if not parent_id:
            return None, cur_family
        if dataset_id and parent_id == dataset_id:
            raise KitError('新版本的父数据集不能是自身')
        prow = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?', (parent_id,)).fetchone()
        if prow is None:
            raise KitError(f'所选父派生数据集不存在：{parent_id}')
        seen: set[str] = {parent_id}
        if dataset_id:
            seen.add(dataset_id)
        pkeys = set(prow.keys())
        cursor = prow['parent_dataset_id'] if 'parent_dataset_id' in pkeys else None
        while cursor:
            cstr = str(cursor)
            if cstr in seen:
                raise KitError('派生数据集父子链存在循环引用')
            seen.add(cstr)
            arow = connection.execute('SELECT parent_dataset_id FROM web_derived_datasets WHERE dataset_id=?', (cstr,)).fetchone()
            cursor = arow['parent_dataset_id'] if arow is not None else None
        family_id = str(prow['family_id']) if ('family_id' in pkeys and prow['family_id']) else parent_id
        return prow, family_id

    @staticmethod
    def _clean_tags(tags: object) -> list[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            raw = [x.strip() for x in re.split(r'[,，\n]+', tags) if x.strip()]
        elif isinstance(tags, (list, tuple)):
            raw = [str(x).strip() for x in tags if isinstance(x, str) and str(x).strip()]
        else:
            raise KitError('派生数据标签格式无效')
        seen: set[str] = set()
        out: list[str] = []
        for tag in raw:
            if any(ord(c) < 32 or ord(c) == 127 for c in tag) or len(tag.encode('utf-8')) > 64:
                raise KitError('派生数据标签不能含控制字符且最多 64 字节')
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
        if len(out) > 24:
            raise KitError('派生数据标签数量不能超过 24 个')
        return out

    @staticmethod
    def build_readme(name: str, description: str, tags: list[str], source_refs: list[dict[str, object]]) -> str:
        import yaml
        front = {'name': name, 'kind': 'derived-dataset', 'tags': tags}
        if description:
            front['description'] = description
        yaml_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
        lines = ['---', yaml_text, '---', '', f'# {name}', '']
        if description:
            lines.extend([description, ''])
        if tags:
            lines.extend(['## 标签', ', '.join(tags), ''])
        if source_refs:
            sources = sorted({str(r.get('dataset_id', '')) for r in source_refs if isinstance(r, dict) and r.get('dataset_id')})
            if sources:
                lines.extend(['## 来源数据集', *[f'- `{sid}`' for sid in sources], ''])
        return '\n'.join(lines).rstrip() + '\n'

    def _record(self, record: Any, *, review: bool = False) -> dict[str, object]:
        keys = set(record.keys())
        visibility = 'review' if review and record['visibility'] == 'private' else record['visibility']
        tags: list[str] = []
        if 'tags_json' in keys and record['tags_json']:
            try:
                parsed = json.loads(record['tags_json'])
                if isinstance(parsed, list):
                    tags = [str(t) for t in parsed if isinstance(t, str)]
            except (TypeError, ValueError):
                tags = []
        description = str(record['description']) if 'description' in keys and record['description'] else ''
        readme_md = str(record['readme_markdown']) if 'readme_markdown' in keys and record['readme_markdown'] else ''
        parent_id = record['parent_dataset_id'] if 'parent_dataset_id' in keys else None
        family_id = (record['family_id'] if 'family_id' in keys and record['family_id'] else str(record['dataset_id']))
        return {'dataset_id': record['dataset_id'], 'files': [self._reference(record)],
                'name': record['name'], 'owner_username': record['owner_username'],
                'visibility': visibility, 'created_at': record['created_at'],
                'total_size': record['size'], 'source': self._source(record),
                'description': description, 'tags': tags, 'readme_markdown': readme_md,
                'has_readme': bool(readme_md), 'parent_dataset_id': parent_id, 'family_id': family_id}

    @staticmethod
    def _references(value: object) -> list[dict[str, object]]:
        return _validate_data_references(value if value is not None else [])

    @staticmethod
    def _json_refs(references: list[dict[str, object]]) -> str:
        return json.dumps(references, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

    def _upload_path(self, name: str) -> Path:
        if not _UUID.fullmatch(name):
            raise KitError('派生数据临时文件名无效')
        return self._uploads / f'{name}.part'

    def _blob_path(self, name: str) -> Path:
        if not _UUID.fullmatch(name):
            raise KitError('派生数据文件名无效')
        return self._blobs / f'{name}.blob'

    @contextmanager
    def _session_lock(self, temp_name: str, *, blocking: bool = True) -> Iterator[None]:
        """Serialize mutations of one upload without serializing other uploads.

        The lock is deliberately separate from the payload: commit moves the
        payload into ``blobs`` before it records the immutable version, while
        append, abort and cleanup must still agree about that in-flight session.
        """
        if not _UUID.fullmatch(temp_name):
            raise KitError('派生数据临时文件名无效')
        path = self._uploads / f'{temp_name}.lock'
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise KitError('派生数据上传锁不可用') from exc
        try:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError:
                raise
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _discard_session_lock(self, temp_name: str) -> None:
        """Remove terminal session state after no operation still owns its lock."""
        try:
            os.unlink(self._uploads / f'{temp_name}.lock')
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _quota_used(self, connection: Any, uid: int, username: str) -> int:
        committed = connection.execute('''SELECT COALESCE(SUM(size), 0) FROM web_derived_datasets
            WHERE owner_uid = ? AND owner_username = ?''', (uid, username)).fetchone()[0]
        reserved = connection.execute('''SELECT COALESCE(SUM(expected_size), 0) FROM web_derived_uploads
            WHERE owner_uid = ? AND owner_username = ? AND expires_at >= ?''',
                                      (uid, username, time.time())).fetchone()[0]
        return int(committed) + int(reserved)

    def cleanup_expired(self) -> int:
        if not self.enabled:
            return 0
        now = time.time()
        with self.store._connection() as connection:
            rows = connection.execute('SELECT upload_id, temp_name FROM web_derived_uploads WHERE expires_at < ?',
                                      (now,)).fetchall()
        removed = 0
        for candidate in rows:
            try:
                with self._session_lock(candidate['temp_name'], blocking=False):
                    with self.store._connection() as connection:
                        connection.execute('BEGIN IMMEDIATE')
                        try:
                            row = connection.execute('''SELECT * FROM web_derived_uploads
                                WHERE upload_id = ? AND expires_at < ?''', (candidate['upload_id'], now)).fetchone()
                            if row is None:
                                connection.execute('COMMIT')
                                continue
                            connection.execute('DELETE FROM web_derived_uploads WHERE upload_id = ?', (row['upload_id'],))
                            connection.execute('COMMIT')
                        except Exception:
                            if connection.in_transaction:
                                connection.execute('ROLLBACK')
                            raise
                    try:
                        info = os.lstat(self._upload_path(row['temp_name']))
                        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                            os.unlink(self._upload_path(row['temp_name']))
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                    removed += 1
                self._discard_session_lock(candidate['temp_name'])
            except BlockingIOError:
                # A current append/commit/abort owns this upload.  Its next
                # cleanup pass can reclaim it once that operation is done.
                continue
        return removed

    def begin_upload(self, principal: Principal, name: object, expected_size: object,
                     expected_sha256: object = None, source_refs: object = None, *,
                     description: object = '', tags: object = None,
                     readme_markdown: object = '', parent_dataset_id: object = None) -> dict[str, object]:
        self._require_enabled()
        uid, username, _role = _identity(principal)
        name = _name(name)
        size = _size(expected_size, '派生数据大小', self.max_file_bytes)
        if expected_sha256 is not None:
            expected_sha256 = _sha256(expected_sha256, '预期 SHA-256')
        references = self._references(source_refs)
        desc = '' if description is None else str(description).strip()[:4000]
        cleaned_tags = self._clean_tags(tags)
        readme_text = '' if readme_markdown is None else str(readme_markdown).strip()[:16000]
        parent_id = _upload_id(parent_dataset_id) if parent_dataset_id is not None and str(parent_dataset_id).strip() else None
        self.cleanup_expired()
        upload_id, temp_name, now = str(uuid.uuid4()), str(uuid.uuid4()), time.time()
        path = self._upload_path(temp_name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            os.close(descriptor)
        except OSError as exc:
            raise KitError('无法创建派生数据临时文件') from exc
        try:
            with self.store._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    active = connection.execute('''SELECT COUNT(*) FROM web_derived_uploads
                        WHERE owner_uid = ? AND owner_username = ? AND expires_at >= ?''',
                                                (uid, username, now)).fetchone()[0]
                    total_active = connection.execute('SELECT COUNT(*) FROM web_derived_uploads WHERE expires_at >= ?',
                                                      (now,)).fetchone()[0]
                    if active >= _MAX_ACTIVE_UPLOADS_PER_ACCOUNT or total_active >= _MAX_ACTIVE_UPLOADS_TOTAL:
                        raise KitError('派生数据活跃上传会话过多')
                    if self._quota_used(connection, uid, username) + size > self.user_quota_bytes:
                        raise KitError('派生数据账号配额不足')
                    if parent_id:
                        self.validate_parent_lineage(connection, None, parent_id)
                    connection.execute('''INSERT INTO web_derived_uploads(
                        upload_id,owner_uid,owner_username,name,expected_size,expected_sha256,
                        source_refs_json,temp_name,received,created_at,expires_at,
                        description,tags_json,readme_markdown,parent_dataset_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (upload_id, uid, username, name, size, expected_sha256, self._json_refs(references),
                         temp_name, 0, now, now + self.upload_ttl_seconds,
                         desc, json.dumps(cleaned_tags, ensure_ascii=False), readme_text, parent_id))
                    connection.execute('COMMIT')
                except Exception:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    raise
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return {'upload_id': upload_id, 'chunk_size': _CHUNK_BYTES,
                'expires_at': datetime.fromtimestamp(now + self.upload_ttl_seconds, timezone.utc).isoformat()}

    def _owned_upload(self, connection: Any, principal: Principal, upload_id: object) -> Any:
        uid, username, _role = _identity(principal)
        upload_id = _upload_id(upload_id)
        row = connection.execute('SELECT * FROM web_derived_uploads WHERE upload_id = ?', (upload_id,)).fetchone()
        if row is None or (row['owner_uid'], row['owner_username']) != (uid, username):
            raise KitError('派生数据上传不存在或无权操作')
        if row['expires_at'] < time.time():
            raise KitError('派生数据上传已过期')
        return row

    def append_upload(self, principal: Principal, upload_id: object, offset: object, data: object) -> dict[str, object]:
        self._require_enabled()
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise KitError('派生数据上传偏移无效')
        if not isinstance(data, bytes) or len(data) > _CHUNK_BYTES:
            raise KitError('派生数据上传块必须是最多 1 MiB 的字节')
        with self.store._connection() as connection:
            row = self._owned_upload(connection, principal, upload_id)
        with self._session_lock(row['temp_name']):
            with self.store._connection() as connection:
                row = self._owned_upload(connection, principal, upload_id)
                if offset != row['received']:
                    raise KitError('派生数据上传偏移不连续')
                if len(data) > row['expected_size'] - row['received']:
                    raise KitError('派生数据上传超过声明大小')
            path = self._upload_path(row['temp_name'])
            flags = os.O_WRONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
            try:
                descriptor = os.open(path, flags)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size != row['received']:
                        raise KitError('派生数据临时文件状态异常')
                    os.lseek(descriptor, offset, os.SEEK_SET)
                    view = memoryview(data)
                    while view:
                        count = os.write(descriptor, view)
                        view = view[count:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise KitError('派生数据上传写入失败') from exc
            received = offset + len(data)
            with self.store._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    current = self._owned_upload(connection, principal, upload_id)
                    if current['temp_name'] != row['temp_name'] or current['received'] != row['received']:
                        raise KitError('派生数据上传状态已变化')
                    connection.execute('UPDATE web_derived_uploads SET received = ? WHERE upload_id = ?',
                                       (received, row['upload_id']))
                    connection.execute('COMMIT')
                except Exception:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    raise
        return {'received': received}

    def commit_upload(self, principal: Principal, upload_id: object, sha256: object, *,
                      source_kind: str = 'upload', source_run_id: str | None = None,
                      source_submission_id: str | None = None) -> dict[str, object]:
        self._require_enabled()
        if source_kind not in {'upload', 'run'}:
            raise KitError('派生数据来源类型无效')
        if source_kind == 'upload' and (source_run_id is not None or source_submission_id is not None):
            raise KitError('上传派生数据不能伪造运行来源')
        if source_kind == 'run' and (not isinstance(source_run_id, str) or not isinstance(source_submission_id, str)):
            raise KitError('运行派生数据缺少可信来源')
        digest = _sha256(sha256)
        dataset_id, blob_name = str(uuid.uuid4()), str(uuid.uuid4())
        blob = self._blob_path(blob_name)
        temp: Path | None = None
        with self.store._connection() as connection:
            row = self._owned_upload(connection, principal, upload_id)
        with self._session_lock(row['temp_name']):
            with self.store._connection() as connection:
                row = self._owned_upload(connection, principal, upload_id)
                if row['received'] != row['expected_size']:
                    raise KitError('派生数据上传尚未完成')
            temp = self._upload_path(row['temp_name'])
            flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
            try:
                descriptor = os.open(temp, flags)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size != row['expected_size']:
                        raise KitError('派生数据临时文件状态异常')
                    hasher, deadline = hashlib.sha256(), time.monotonic() + _HASH_TIMEOUT_SECONDS
                    while True:
                        if time.monotonic() >= deadline:
                            raise KitError('派生数据 SHA-256 核验超时')
                        block = os.read(descriptor, _CHUNK_BYTES)
                        if not block:
                            break
                        hasher.update(block)
                    actual = hasher.hexdigest()
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise KitError('派生数据临时文件不可读取') from exc
            if actual != digest or (row['expected_sha256'] is not None and actual != row['expected_sha256']):
                raise KitError('派生数据 SHA-256 不匹配')
            moved = False
            with self.store._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    current = self._owned_upload(connection, principal, upload_id)
                    if (current['temp_name'] != row['temp_name'] or current['received'] != current['expected_size'] or
                            current['expected_size'] != row['expected_size']):
                        raise KitError('派生数据上传状态已变化')
                    # Atomic replacement is within the same broker-owned root.
                    os.rename(temp, blob)
                    moved = True
                    # rename preserves the upload file's group; the setgid
                    # destination directory does not make it inherit runner's
                    # group. Prepare the broker-owned file before publishing
                    # its record, without changing the service's private umask.
                    descriptor = os.open(blob, flags)
                    try:
                        saved = os.fstat(descriptor)
                        if (not stat.S_ISREG(saved.st_mode) or saved.st_uid != os.geteuid() or
                                saved.st_nlink != 1 or saved.st_size != row['expected_size']):
                            raise KitError('派生数据保存文件状态异常')
                        try:
                            os.fchown(descriptor, -1, os.lstat(self._require_enabled()).st_gid)
                            os.fchmod(descriptor, 0o640)
                            os.fsync(descriptor)
                        except OSError as exc:
                            raise KitError('派生数据无法设置后台读取权限，请管理员核对服务组配置') from exc
                    finally:
                        os.close(descriptor)
                    created = _now()
                    cur_keys = set(current.keys())
                    desc = str(current['description']) if 'description' in cur_keys and current['description'] else ''
                    tags_json = str(current['tags_json']) if 'tags_json' in cur_keys and current['tags_json'] else '[]'
                    tags_list = self._clean_tags(json.loads(tags_json))
                    readme_md = str(current['readme_markdown']) if 'readme_markdown' in cur_keys and current['readme_markdown'] else ''
                    parent_id = current['parent_dataset_id'] if 'parent_dataset_id' in cur_keys else None
                    family_id = dataset_id
                    if parent_id:
                        prow, family_id = self.validate_parent_lineage(connection, dataset_id, parent_id)
                        if prow is not None:
                            if not desc:
                                desc = prow['description'] or ''
                            if not tags_list and prow['tags_json']:
                                tags_list = self._clean_tags(json.loads(prow['tags_json']))
                            if not readme_md:
                                readme_md = prow['readme_markdown'] or ''
                    source_refs_val = json.loads(current['source_refs_json'] or '[]')
                    if not readme_md and (desc or tags_list or source_refs_val):
                        readme_md = self.build_readme(current['name'], desc, tags_list, source_refs_val if isinstance(source_refs_val, list) else [])
                    connection.execute('''INSERT INTO web_derived_datasets(
                        dataset_id,owner_uid,owner_username,name,visibility,created_at,size,sha256,blob_name,
                        source_kind,source_run_id,source_submission_id,source_refs_json,
                        description,tags_json,readme_markdown,parent_dataset_id,family_id)
                        VALUES (?,?,?,?, 'private',?,?,?,?, ?,?,?,?, ?,?,?,?,?)''',
                        (dataset_id, current['owner_uid'], current['owner_username'], current['name'], created,
                         current['expected_size'], actual, blob_name, source_kind, source_run_id,
                         source_submission_id, current['source_refs_json'],
                         desc, json.dumps(tags_list, ensure_ascii=False), readme_md, parent_id, family_id))
                    connection.execute('DELETE FROM web_derived_uploads WHERE upload_id = ?', (current['upload_id'],))
                    record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                                (dataset_id,)).fetchone()
                    connection.execute('COMMIT')
                except Exception:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    if moved:
                        try:
                            os.rename(blob, temp)
                        except OSError:
                            pass
                    raise
        self._discard_session_lock(row['temp_name'])
        if record is None:  # pragma: no cover - guarded by INSERT
            raise KitError('派生数据登记失败')
        return self._record(record)

    def abort_upload(self, principal: Principal, upload_id: object) -> dict[str, object]:
        self._require_enabled()
        with self.store._connection() as connection:
            row = self._owned_upload(connection, principal, upload_id)
        with self._session_lock(row['temp_name']):
            with self.store._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    row = self._owned_upload(connection, principal, upload_id)
                    connection.execute('DELETE FROM web_derived_uploads WHERE upload_id = ?', (row['upload_id'],))
                    connection.execute('COMMIT')
                except Exception:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    raise
            try:
                os.unlink(self._upload_path(row['temp_name']))
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise KitError('派生数据临时文件无法清理') from exc
        self._discard_session_lock(row['temp_name'])
        return {'aborted': True}

    def catalog(self, principal: Principal) -> list[dict[str, object]]:
        if not self.enabled:
            return []
        uid, username, role = _identity(principal)
        with self.store._connection() as connection:
            if role == 'teacher':
                rows = connection.execute('''SELECT d.*, EXISTS(SELECT 1 FROM web_derived_review_readers g
                    WHERE g.dataset_id = d.dataset_id AND g.teacher_uid = ? AND g.teacher_username = ?) AS review
                    FROM web_derived_datasets d WHERE d.visibility = 'public' OR d.owner_uid = ?
                    OR EXISTS(SELECT 1 FROM web_derived_review_readers g WHERE g.dataset_id = d.dataset_id
                       AND g.teacher_uid = ? AND g.teacher_username = ?)
                    ORDER BY d.created_at DESC, d.dataset_id DESC''', (uid, username, uid, uid, username)).fetchall()
            else:
                has_pol = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_dataset_policies'").fetchone() is not None
                if has_pol:
                    rows = connection.execute('''SELECT d.*, 0 AS review FROM web_derived_datasets d
                        LEFT JOIN web_dataset_policies p ON p.dataset_id = d.dataset_id
                        WHERE (d.owner_uid = ? AND d.owner_username = ?)
                           OR (d.visibility = 'public' AND (
                               p.public IS NULL OR p.public = 1 OR EXISTS(
                                   SELECT 1 FROM web_dataset_grants g
                                   WHERE g.dataset_id = d.dataset_id AND g.target_uid = ? AND g.target_username = ?)))
                        ORDER BY d.created_at DESC, d.dataset_id DESC''', (uid, username, uid, username)).fetchall()
                else:
                    rows = connection.execute('''SELECT d.*, 0 AS review FROM web_derived_datasets d
                        WHERE d.visibility = 'public' OR (d.owner_uid = ? AND d.owner_username = ?)
                        ORDER BY d.created_at DESC, d.dataset_id DESC''', (uid, username)).fetchall()
            # Task data is an exact permission route, so append only IDs whose
            # stored material reference and sponsoring source right still pass.
            ids = self._task_granted_dataset_ids(connection, principal)
            present = {str(row['dataset_id']) for row in rows}
            if ids - present:
                marks = ','.join('?' for _ in sorted(ids - present))
                extra = connection.execute(f'''SELECT d.*, 0 AS review FROM web_derived_datasets d
                    WHERE d.dataset_id IN ({marks})''', sorted(ids - present)).fetchall()
                rows = [*rows, *extra]
        rows = sorted(rows, key=lambda row: (str(row['created_at']), str(row['dataset_id'])), reverse=True)
        return [self._record(row, review=bool(row['review'])) for row in rows]

    @staticmethod
    def _catalog_cursor(value: object) -> tuple[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise KitError('派生数据分页游标无效')
        try:
            raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
            decoded = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KitError('派生数据分页游标无效') from exc
        if (type(decoded) is not dict or set(decoded) != {'created_at', 'dataset_id'} or
                not isinstance(decoded['created_at'], str) or not isinstance(decoded['dataset_id'], str) or
                not decoded['created_at'] or not _UUID.fullmatch(decoded['dataset_id'])):
            raise KitError('派生数据分页游标无效')
        return decoded['created_at'], decoded['dataset_id']

    @staticmethod
    def _make_catalog_cursor(record: Any) -> str:
        payload = json.dumps({'created_at': record['created_at'], 'dataset_id': record['dataset_id']},
                             ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return base64.urlsafe_b64encode(payload).rstrip(b'=').decode('ascii')

    def catalog_page(self, principal: Principal, *, q: object = '', visibility: object = 'available',
                     limit: object = 50, cursor: object = None) -> dict[str, object]:
        """Return a bounded browser page without changing ``catalog`` compatibility."""
        if not isinstance(q, str) or len(q) > 256:
            raise KitError('派生数据搜索条件无效')
        if not isinstance(visibility,str) or visibility not in {'mine', 'available', 'public'}:
            raise KitError('派生数据可见范围无效')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise KitError('派生数据分页大小无效')
        after = self._catalog_cursor(cursor)
        if not self.enabled:
            return {'items': [], 'next_cursor': None}
        uid, username, role = _identity(principal)
        from .web_lifecycle import AssetLifecycle
        lifecycle = AssetLifecycle(self.store)
        clauses: list[str] = []
        if role != 'teacher':
            clauses.append("NOT EXISTS (SELECT 1 FROM web_asset_lifecycle l WHERE l.kind='dataset' AND l.ref=d.dataset_id AND l.state <> 'active')")
        values: list[object] = []
        with self.store._connection() as connection:
            task_ids = self._task_granted_dataset_ids(connection, principal)
        task_clause = ''
        task_values: list[object] = []
        if task_ids and visibility == 'available':
            task_clause = ' OR d.dataset_id IN (' + ','.join('?' for _ in sorted(task_ids)) + ')'
            task_values = sorted(task_ids)
        if visibility == 'mine':
            clauses.append('(d.owner_uid = ? AND d.owner_username = ?)')
            values.extend((uid, username))
        elif visibility == 'public':
            clauses.append("d.visibility = 'public'")
        elif role == 'teacher':
            clauses.append(f'''(d.visibility = 'public' OR (d.owner_uid = ? AND d.owner_username = ?)
                OR EXISTS(SELECT 1 FROM web_derived_review_readers g WHERE g.dataset_id = d.dataset_id
                    AND g.teacher_uid = ? AND g.teacher_username = ?){task_clause})''')
            values.extend((uid, username, uid, username))
            values.extend(task_values)
        else:
            clauses.append("(d.visibility = 'public' OR (d.owner_uid = ? AND d.owner_username = ?)" + task_clause + ')')
            values.extend((uid, username))
            values.extend(task_values)
        if q:
            escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append("(d.name LIKE ? ESCAPE '\\' OR d.owner_username LIKE ? ESCAPE '\\' OR d.dataset_id LIKE ? ESCAPE '\\')")
            values.extend((f'%{escaped}%', f'%{escaped}%', f'%{escaped}%'))
        if after is not None:
            clauses.append('(d.created_at < ? OR (d.created_at = ? AND d.dataset_id < ?))')
            values.extend((after[0], after[0], after[1]))
        if role == 'teacher':
            review = ('EXISTS(SELECT 1 FROM web_derived_review_readers g WHERE g.dataset_id = d.dataset_id '
                      'AND g.teacher_uid = ? AND g.teacher_username = ?) AS review')
            review_values: list[object] = [uid, username]
        else:
            review, review_values = '0 AS review', []
        with self.store._connection() as connection:
            rows = connection.execute(f'''SELECT d.*, {review} FROM web_derived_datasets d
                WHERE {' AND '.join(clauses)} ORDER BY d.created_at DESC, d.dataset_id DESC LIMIT ?''',
                                      review_values + values + [limit + 1]).fetchall()
        page = rows[:limit]
        items = [self._record(row, review=bool(row['review'])) for row in page]
        if role == 'teacher':
            for item in items:
                if item['visibility'] == 'public':
                    item['lifecycle'] = lifecycle.summary('dataset', item['dataset_id'])
        return {'items': items,
                'next_cursor': self._make_catalog_cursor(page[-1]) if len(rows) > limit else None}

    def _records_for_references(self, connection: Any, references: list[dict[str, object]]) -> dict[str, Any]:
        identifiers = sorted({str(item['dataset_id']) for item in references if _UUID.fullmatch(str(item['dataset_id']))})
        if not identifiers:
            return {}
        marks = ','.join('?' for _ in identifiers)
        rows = connection.execute(f'SELECT * FROM web_derived_datasets WHERE dataset_id IN ({marks})', identifiers).fetchall()
        return {row['dataset_id']: row for row in rows}

    def validate_submission_references(self, principal: Principal, references: object) -> None:
        """Reject a private foreign derived reference before storing a submission."""
        if not self.enabled:
            return
        uid, username, _role = _identity(principal)
        refs = self._references(references)
        with self.store._connection() as connection:
            records = self._records_for_references(connection, refs)
            for ref in refs:
                record = records.get(ref['dataset_id'])
                if record is None:
                    continue  # raw dataset; its registry keeps existing semantics
                if ref != self._reference(record):
                    raise KitError('派生数据引用不匹配')
                if not self._can_read(connection, record, principal):
                    raise KitError('无权在提交中引用他人的私有派生数据')

    @staticmethod
    def _can_read(connection: Any, record: Any, principal: Principal) -> bool:
        uid, username, role = _identity(principal)
        if (record['owner_uid'], record['owner_username']) == (uid, username):
            return True
        if record['visibility'] == 'public':
            if role == 'teacher':
                return True
            try:
                pol = connection.execute('SELECT public FROM web_dataset_policies WHERE dataset_id=?',
                                         (record['dataset_id'],)).fetchone()
            except sqlite3.OperationalError:
                pol = None
            if pol is None or bool(pol['public']):
                return True
            try:
                granted = connection.execute(
                    'SELECT 1 FROM web_dataset_grants WHERE dataset_id=? AND target_uid=? AND target_username=?',
                    (record['dataset_id'], uid, username)).fetchone()
            except sqlite3.OperationalError:
                granted = None
            if granted is not None:
                return True
            return DerivedDatasets._task_grant_can_read(connection, record, principal)
        if role == 'teacher' and connection.execute('''SELECT 1 FROM web_derived_review_grants
            WHERE dataset_id=? AND teacher_uid=? AND teacher_username=? LIMIT 1''',
            (record['dataset_id'],uid,username)).fetchone() is not None:
            return True
        if role == 'teacher' and connection.execute('''SELECT 1 FROM web_derived_legacy_source_grants
            WHERE dataset_id=? AND teacher_uid=? AND teacher_username=? LIMIT 1''',
            (record['dataset_id'],uid,username)).fetchone() is not None:
            return True
        return DerivedDatasets._task_grant_can_read(connection, record, principal)

    @staticmethod
    def _trial_review_can_mount(connection: Any, record: Any, principal: Principal,
                                submission_id: str) -> bool:
        """Allow ly to run one claimed trial, without widening a review grant.

        A review grant is deliberately tied to the student's immutable source
        submission.  A trial necessarily creates a different, ly-owned project
        and then a different preparation snapshot.  The only permitted bridge
        is therefore the protected chain ``snapshot -> source project ->
        claimed distribution -> trial task -> reviewed baseline``.  It is
        intentionally unavailable to every other ly project and to students.
        """
        _uid, _username, role = _identity(principal)
        if role != 'teacher':
            return False
        source = connection.execute('SELECT project_id FROM submissions WHERE id=?',
                                    (submission_id,)).fetchone()
        if source is None:
            return False
        project_id = source['project_id']
        try:
            preparation = connection.execute('''SELECT source_project_id FROM web_submission_preparations
                WHERE snapshot_id=?''', (submission_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            preparation = None
        if preparation is not None:
            project_id = preparation['source_project_id']
        try:
            trial = connection.execute('''SELECT 1
                FROM project_trusted_sources trusted
                JOIN web_task_recipients recipients ON recipients.distribution_id=trusted.distribution_id
                JOIN web_task_deliveries tasks ON tasks.id=recipients.task_id
                JOIN web_derived_review_grants review ON review.dataset_id=?
                  AND review.submission_id=tasks.baseline_submission_id
                  AND review.teacher_uid=? AND review.teacher_username=?
                WHERE trusted.project_id=? AND trusted.baseline_submission_id=tasks.baseline_submission_id
                  AND tasks.kind='trial' AND recipients.target_uid=? AND recipients.target_username=?
                  AND recipients.distribution_id IS NOT NULL
                LIMIT 1''',
                (record['dataset_id'], principal.uid, principal.username, project_id,
                 principal.uid, principal.username)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return False
            raise
        return trial is not None

    @staticmethod
    def _validation_review_can_mount(connection: Any, record: Any, principal: Principal,
                                     submission_id: str) -> bool:
        """Keep legacy validation tied to its reviewed immutable source.

        The validation snapshot is service-created, not an arbitrary teacher
        project. Its candidate, source digest and private data reference must
        all agree with the original, narrowly scoped review grant.
        """
        if _identity(principal)[2] != 'teacher':
            return False
        try:
            row=connection.execute('''SELECT c.data_references_json
                FROM web_asset_validation_snapshots v
                JOIN web_asset_candidates c ON c.candidate_id=v.candidate_id
                  AND c.candidate_sha256=v.candidate_sha256 AND c.kind='template'
                JOIN submissions source ON source.id=c.submission_id
                  AND source.content_sha256=c.source_sha256
                JOIN submissions snapshot ON snapshot.id=v.submission_id AND snapshot.project_id=v.project_id
                JOIN projects p ON p.id=v.project_id
                JOIN web_derived_review_grants g ON g.submission_id=c.submission_id AND g.dataset_id=?
                  AND g.teacher_uid=? AND g.teacher_username=?
                WHERE v.submission_id=? AND v.kind='template' AND p.owner_uid=? AND p.owner_username=?''',
                (record['dataset_id'],principal.uid,principal.username,submission_id,principal.uid,principal.username)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):return False
            raise
        if row is None:return False
        try:
            references=DerivedDatasets._references(json.loads(row['data_references_json']))
        except (TypeError,ValueError,KitError):
            return False
        return DerivedDatasets._reference(record) in references

    @staticmethod
    def _can_mount_for_submission(connection: Any, record: Any, principal: Principal,
                                  submission_id: str) -> bool:
        """Check a run's exact source, rather than a broad catalogue right."""
        uid, username, role = _identity(principal)
        if record['visibility'] == 'public' or (record['owner_uid'], record['owner_username']) == (uid, username):
            return True
        if DerivedDatasets._task_grant_can_read(connection, record, principal):
            return True
        if role != 'teacher':
            return False
        exact_review = connection.execute('''SELECT 1 FROM web_derived_review_grants
            WHERE dataset_id=? AND submission_id=? AND teacher_uid=? AND teacher_username=? LIMIT 1''',
            (record['dataset_id'], submission_id, uid, username)).fetchone()
        legacy_source_review = connection.execute('''SELECT 1
            FROM web_derived_legacy_source_grants legacy
            JOIN submissions source ON source.project_id=legacy.source_project_id
              AND source.content_sha256=legacy.source_sha256
            WHERE legacy.dataset_id=? AND source.id=?
              AND legacy.teacher_uid=? AND legacy.teacher_username=? LIMIT 1''',
            (record['dataset_id'], submission_id, uid, username)).fetchone()
        test_copy = False
        try:
            sub_row = connection.execute('SELECT project_id FROM submissions WHERE id=?', (submission_id,)).fetchone()
            prep = connection.execute('SELECT source_project_id FROM web_submission_preparations WHERE snapshot_id=?', (submission_id,)).fetchone()
            proj_id = prep['source_project_id'] if prep is not None else (sub_row['project_id'] if sub_row is not None else None)
            if proj_id:
                test_copy = connection.execute('''SELECT 1 FROM web_review_test_copies tc
                    JOIN web_derived_review_grants g ON g.submission_id=tc.submission_id
                      AND g.dataset_id=? AND g.teacher_uid=? AND g.teacher_username=?
                    WHERE tc.project_id=? AND tc.teacher_uid=? AND tc.teacher_username=? LIMIT 1''',
                    (record['dataset_id'], uid, username, proj_id, uid, username)).fetchone() is not None
        except sqlite3.OperationalError:
            test_copy = False
        return (exact_review is not None or legacy_source_review is not None or test_copy or DerivedDatasets._trial_review_can_mount(
            connection, record, principal, submission_id) or DerivedDatasets._validation_review_can_mount(
            connection, record, principal, submission_id))

    def bind_submission(self, principal: Principal, submission_id: object, references: object,
                        teacher: Principal) -> None:
        """Persist narrowly-scoped teacher review grants for an immutable submission."""
        if not self.enabled:
            return
        uid, username, role = _identity(principal)
        teacher_uid, teacher_username, teacher_role = _identity(teacher)
        if teacher_role != 'teacher':
            raise KitError('派生数据审核主体必须是教师')
        if not isinstance(submission_id, str) or not _UUID.fullmatch(submission_id):
            raise KitError('固定提交 ID 无效')
        refs = self._references(references)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                records = self._records_for_references(connection, refs)
                for ref in refs:
                    record = records.get(ref['dataset_id'])
                    if record is None:
                        continue
                    if ref != self._reference(record):
                        raise KitError('派生数据引用不匹配')
                    if not self._can_read(connection, record, principal):
                        raise KitError('无权在提交中引用他人的私有派生数据')
                    if record['visibility'] != 'public' and (record['owner_uid'],record['owner_username']) != (teacher_uid,teacher_username):
                        connection.execute('''INSERT OR IGNORE INTO web_derived_review_grants(
                            dataset_id,submission_id,teacher_uid,teacher_username,created_at)
                            VALUES (?,?,?,?,?)''', (record['dataset_id'], submission_id, teacher_uid,
                                                    teacher_username, _now()))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def publish(self, principal: Principal, dataset_id: object, *,
                description: object = None, tags: object = None,
                readme_markdown: object = None, parent_dataset_id: object = None) -> dict[str, object]:
        self._require_enabled()
        uid, username, role = _identity(principal)
        if role != 'teacher':
            raise KitError('只有教师可以公开派生数据')
        dataset_id = _upload_id(dataset_id)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                            (dataset_id,)).fetchone()
                if record is None:
                    raise KitError('派生数据不存在')
                review = connection.execute('''SELECT 1 FROM web_derived_review_readers WHERE dataset_id = ?
                    AND teacher_uid = ? AND teacher_username = ? LIMIT 1''',
                                            (dataset_id, uid, username)).fetchone()
                if (record['owner_uid'], record['owner_username']) != (uid, username) and review is None:
                    raise KitError('只能公开本人或已进入审核范围的派生数据')
                desc = record['description'] if description is None else str(description).strip()[:4000]
                cleaned_tags = json.loads(record['tags_json'] or '[]') if tags is None else self._clean_tags(tags)
                parent_id = record['parent_dataset_id']
                family_id = record['family_id'] or dataset_id
                if parent_dataset_id is not None and str(parent_dataset_id).strip():
                    parent_id = _upload_id(parent_dataset_id)
                    prow, family_id = self.validate_parent_lineage(connection, dataset_id, parent_id, current_row=record)
                    if not desc and prow['description']:
                        desc = prow['description']
                    if not cleaned_tags and prow['tags_json']:
                        cleaned_tags = json.loads(prow['tags_json'])
                readme_md = record['readme_markdown'] if readme_markdown is None else str(readme_markdown).strip()[:16000]
                if not readme_md and (desc or cleaned_tags):
                    readme_md = self.build_readme(record['name'], desc, cleaned_tags, json.loads(record['source_refs_json'] or '[]'))
                connection.execute('''UPDATE web_derived_datasets
                    SET visibility='public', description=?, tags_json=?, readme_markdown=?,
                        parent_dataset_id=?, family_id=?
                    WHERE dataset_id=?''',
                    (desc, json.dumps(cleaned_tags, ensure_ascii=False), readme_md, parent_id, family_id, dataset_id))
                record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                            (dataset_id,)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return self._record(record)

    def get_readme(self, principal: Principal, dataset_id: object) -> dict[str, object]:
        self._require_enabled()
        dataset_id = _upload_id(dataset_id)
        with self.store._connection() as connection:
            record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?', (dataset_id,)).fetchone()
            if record is None or not self._can_read(connection, record, principal):
                raise KitError('派生数据不存在或无权读取')
        rec = self._record(record)
        md = rec['readme_markdown'] or self.build_readme(rec['name'], rec['description'], rec['tags'], rec.get('source', {}).get('source_refs', []))
        return {'dataset_id': dataset_id, 'name': rec['name'], 'readme_markdown': md, 'tags': rec['tags'], 'description': rec['description']}

    def grant_review(self, principal: Principal, dataset_id: object, submission_id: object) -> dict[str, object]:
        """Idempotently grant a teacher only a genuinely referenced version."""
        self._require_enabled()
        teacher_uid, teacher_username, role = _identity(principal)
        if role != 'teacher':
            raise KitError('只有教师可以授予审核读取权限')
        dataset_id, submission_id = _upload_id(dataset_id), _upload_id(submission_id)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                            (dataset_id,)).fetchone()
                source = connection.execute('''SELECT submissions.manifest_json, projects.owner_uid, projects.owner_username
                    FROM submissions JOIN projects ON projects.id = submissions.project_id WHERE submissions.id = ?''',
                                            (submission_id,)).fetchone()
                if record is None or source is None:
                    raise KitError('派生数据或固定提交不存在')
                try:
                    references = json.loads(source['manifest_json'])['data_references']['references']
                    references = self._references(references)
                except (TypeError, ValueError, KeyError) as exc:
                    raise KitError('固定提交数据引用记录已损坏') from exc
                if self._reference(record) not in references:
                    raise KitError('固定提交未实际引用该派生数据版本')
                if (source['owner_uid'], source['owner_username']) != (record['owner_uid'], record['owner_username']):
                    raise KitError('固定提交所有者不能授予他人的私有派生数据')
                connection.execute('''INSERT OR IGNORE INTO web_derived_review_grants(
                    dataset_id,submission_id,teacher_uid,teacher_username,created_at) VALUES (?,?,?,?,?)''',
                                   (dataset_id, submission_id, teacher_uid, teacher_username, _now()))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'dataset_id': dataset_id, 'submission_id': submission_id, 'granted': True}

    def grant_run_output(self, principal: Principal, dataset_id: object, run_id: object) -> dict[str, object]:
        """Grant ly read access to one retained, visible successful run output.

        This is intentionally not a general ``run_id -> dataset`` capability:
        both the immutable blob provenance and the retained staged-artifact
        evidence must name the same successful job and exact bytes.
        """
        self._require_enabled()
        teacher_uid, teacher_username, role = _identity(principal)
        if role != 'teacher' or teacher_username != 'ly':
            raise KitError('只有教师可以授予运行产物审核读取权限')
        dataset_id = _upload_id(dataset_id)
        if not isinstance(run_id, str) or not run_id:
            raise KitError('作业 ID 无效')
        from .web_jobs import Jobs
        jobs = Jobs(self.store)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                job = connection.execute('SELECT * FROM web_jobs WHERE run_id = ?', (run_id,)).fetchone()
                if job is None or job['status'] != 'succeeded' or not jobs._visible_to(connection, job, (teacher_uid, teacher_username, role)):
                    raise KitError('无权审核该成功作业的保留产物')
                record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                            (dataset_id,)).fetchone()
                if (record is None or record['source_kind'] != 'run' or record['source_run_id'] != run_id or
                        record['source_submission_id'] != job['submission_id']):
                    raise KitError('派生数据不是该作业的保留产物')
                staged = connection.execute('''SELECT 1 FROM web_job_staged_artifacts
                    WHERE run_id = ? AND retained_dataset_id = ? AND size = ? AND sha256 = ? LIMIT 1''',
                                            (run_id, dataset_id, record['size'], record['sha256'])).fetchone()
                retained = connection.execute('''SELECT 1 FROM web_job_artifact_retention
                    WHERE run_id = ? AND dataset_id = ? AND status = 'succeeded' LIMIT 1''',
                                              (run_id, dataset_id)).fetchone()
                if staged is None or retained is None:
                    raise KitError('作业保留产物证据不完整')
                connection.execute('''INSERT OR IGNORE INTO web_derived_review_grants(
                    dataset_id,submission_id,teacher_uid,teacher_username,created_at) VALUES (?,?,?,?,?)''',
                                   (dataset_id, job['submission_id'], teacher_uid, teacher_username, _now()))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'dataset_id': dataset_id, 'run_id': run_id, 'submission_id': job['submission_id'], 'granted': True}

    @contextmanager
    def open_file(self, principal: Principal, dataset_id: object, path: object) -> Iterator[tuple[int, dict[str, object]]]:
        """Open one broker-authorized immutable derived file for SCM_RIGHTS."""
        self._require_enabled()
        uid, username, role = _identity(principal)
        dataset_id = _upload_id(dataset_id)
        if path != _FILE_NAME:
            raise KitError('派生数据只包含 data.bin')
        with self.store._connection() as connection:
            record = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id = ?',
                                        (dataset_id,)).fetchone()
            if record is None:
                raise KitError('派生数据不存在')
            if not self._can_read(connection, record, principal):
                raise KitError('无权读取派生数据')
            mount = {'reference': self._reference(record), 'blob_name': record['blob_name']}
        with self.open_claimed_mounts(self.root, [mount], verify_after=False) as mounts:
            yield mounts[0].fd, self._reference(record)

    def claim_mounts(self, owner: Principal, requester: Principal, submission_id: object,
                     references: object) -> list[dict[str, object]]:
        """Return only broker-authorized immutable derived mount descriptions."""
        if not self.enabled:
            return []
        _identity(owner)
        _identity(requester)
        if not isinstance(submission_id, str) or not _UUID.fullmatch(submission_id):
            raise KitError('固定提交 ID 无效')
        refs = self._references(references)
        result: list[dict[str, object]] = []
        with self.store._connection() as connection:
            records = self._records_for_references(connection, refs)
            for ref in refs:
                record = records.get(ref['dataset_id'])
                if record is None:
                    continue
                if ref != self._reference(record):
                    raise KitError('派生数据引用不匹配')
                if not self._can_mount_for_submission(connection, record, owner, submission_id) or not self._can_mount_for_submission(
                        connection, record, requester, submission_id):
                    raise KitError('当前固定提交不具备派生数据读取授权')
                result.append({'reference': dict(ref), 'blob_name': record['blob_name']})
        return result

    @staticmethod
    @contextmanager
    def open_claimed_mounts(root: Path | None, mounts: object, *, verify_after: bool = True) -> Iterator[list[DatasetMount]]:
        """Runner-side opening of broker-issued mount descriptions.

        This validates the exact immutable file and keeps its descriptor pinned
        through bubblewrap.  It intentionally cannot catalogue or authorize
        arbitrary roots; the broker supplied only a UUID blob name after ACL
        checks.
        """
        if root is None:
            if mounts:
                raise KitError('派生数据存储未启用')
            yield []
            return
        root = Path(root)
        if not root.is_absolute() or '..' in root.parts:
            raise KitError('派生数据根目录不安全')
        if type(mounts) is not list:
            raise KitError('派生数据挂载描述无效')
        opened: list[DatasetMount] = []
        try:
            for item in mounts:
                if type(item) is not dict or set(item) != {'reference', 'blob_name'}:
                    raise KitError('派生数据挂载描述无效')
                refs = DerivedDatasets._references([item['reference']])
                if len(refs) != 1 or refs[0]['path'] != _FILE_NAME:
                    raise KitError('派生数据挂载引用无效')
                if not isinstance(item['blob_name'], str) or not _UUID.fullmatch(item['blob_name']):
                    raise KitError('派生数据挂载文件名无效')
                blob = root / 'blobs' / f"{item['blob_name']}.blob"
                flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
                try:
                    before = os.lstat(blob)
                    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or
                            before.st_nlink != 1 or before.st_size != refs[0]['size']):
                        raise KitError('派生数据文件状态异常')
                    descriptor = os.open(blob, flags)
                    after = os.fstat(descriptor)
                    if _stable(before) != _stable(after) or after.st_size != refs[0]['size']:
                        os.close(descriptor)
                        raise KitError('派生数据文件读取时变化')
                except OSError as exc:
                    raise KitError('派生数据文件不可读取') from exc
                opened.append(DatasetMount(dict(refs[0]), descriptor,
                                           f"/datasets/{refs[0]['dataset_id']}/{_FILE_NAME}"))
                DerivedDatasets._verify_content(descriptor, refs[0])
            yield opened
            if verify_after:
                for mount in opened:
                    DerivedDatasets._verify_content(mount.fd, mount.reference)
        finally:
            for mount in opened:
                try:
                    os.close(mount.fd)
                except OSError:
                    pass

    @staticmethod
    def _verify_content(fd: int, reference: dict[str, object]) -> None:
        before = os.fstat(fd)
        digest, offset, deadline = hashlib.sha256(), 0, time.monotonic() + _HASH_TIMEOUT_SECONDS
        while True:
            if time.monotonic() >= deadline:
                raise KitError('派生数据内容核验超时')
            # SCM_RIGHTS duplicates the same open-file description. pread keeps
            # the user's read offset untouched when handing a file to an IDE.
            block = os.pread(fd, _CHUNK_BYTES, offset)
            if not block:
                break
            offset += len(block); digest.update(block)
        if (offset != reference['size'] or digest.hexdigest() != reference['sha256']
                or _stable(os.fstat(fd)) != _stable(before)):
            raise KitError('派生数据内容已变化或损坏')
