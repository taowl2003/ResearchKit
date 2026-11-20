"""Durable task delivery records and non-invasive supplementary materials.

This module deliberately sits on the broker side of the Web application.  A
caller supplies already authenticated principals and exact, already resolved
resource bytes.  It does not resolve Unix accounts, open workspaces, or accept
paths from a browser.  In particular, a task's immutable baseline is never
rewritten when extra algorithms or datasets are offered to its recipients.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .files import KitError
from .web_content import normalize
from .web_identity import Principal
from .web_store import _principal, _record_id, _validate_data_references, _validate_files


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_MAX_INSTRUCTIONS_BYTES = 64 * 1024
_MAX_MATERIALS = 128


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise KitError(f'{label}无效')
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise KitError(f'{label}必须是 UUID') from exc
    if str(parsed) != value:
        raise KitError(f'{label}必须使用小写标准 UUID')
    return value


def _text(value: object, label: str, maximum: int = 200, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise KitError(f'{label}必须是文本')
    value = value.strip()
    try:
        size = len(value.encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise KitError(f'{label}必须是 UTF-8 文本') from exc
    if size > maximum or (not empty and not value):
        qualifier = '可以为空且' if empty else '不能为空且'
        raise KitError(f'{label}{qualifier}不得超过 {maximum} 字节')
    if any(ord(character) < 32 and character not in '\n\t' for character in value):
        raise KitError(f'{label}不能包含控制字符')
    return value


def _request_id(value: object) -> str:
    return _uuid(value, '请求 ID')


def _request_fingerprint(value: object) -> str:
    encoded = _json(value).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class TaskDelivery:
    """Central records for formal tasks, trial tasks and later supplements.

    ``SubmissionStore`` remains the authoritative immutable-snapshot store.
    This class only adds task metadata, materials and grants.  It deliberately
    uses the store's private connection so a task's material and grant rows
    commit together.  Distribution allocation is a recoverable second phase:
    a crash after the durable task record returns the same task on retry and
    only fills its missing recipient-distribution links.
    """

    def __init__(self, store: object):
        self.store = store
        if not hasattr(store, '_connection') or not hasattr(store, 'get_submission'):
            raise TypeError('TaskDelivery requires the protected submission store')
        self._initialize()

    def _connection(self):
        return self.store._connection()  # type: ignore[attr-defined]

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                for statement in (
                    '''CREATE TABLE IF NOT EXISTS web_task_deliveries (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('formal','trial')),
                        delivery_mode TEXT NOT NULL DEFAULT 'project' CHECK(delivery_mode IN ('project','resources')),
                        family_id TEXT,
                        task_version INTEGER NOT NULL DEFAULT 1 CHECK(task_version >= 1),
                        parent_task_id TEXT REFERENCES web_task_deliveries(id),
                        teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                        baseline_submission_id TEXT NOT NULL,
                        baseline_sha256 TEXT NOT NULL,
                        name TEXT NOT NULL, instructions TEXT NOT NULL,
                        created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS web_task_recipients (
                        task_id TEXT NOT NULL REFERENCES web_task_deliveries(id),
                        target_uid INTEGER NOT NULL, target_username TEXT NOT NULL,
                        target_role TEXT NOT NULL CHECK(target_role IN ('teacher','student')),
                        distribution_id TEXT, project_id TEXT, project_name TEXT,
                        resource_claim_status TEXT CHECK(resource_claim_status IN ('claiming','claimed')),
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(task_id,target_uid),
                        UNIQUE(task_id,target_username))''',
                    '''CREATE TABLE IF NOT EXISTS web_task_supplements (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES web_task_deliveries(id),
                        request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                        instructions TEXT NOT NULL, created_at TEXT NOT NULL,
                        UNIQUE(task_id,request_id))''',
                    '''CREATE TABLE IF NOT EXISTS web_task_supplement_recipients (
                        supplement_id TEXT NOT NULL REFERENCES web_task_supplements(id),
                        target_uid INTEGER NOT NULL,target_username TEXT NOT NULL,
                        PRIMARY KEY(supplement_id,target_uid,target_username))''',
                    '''CREATE TABLE IF NOT EXISTS web_task_materials (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES web_task_deliveries(id),
                        supplement_id TEXT REFERENCES web_task_supplements(id),
                        kind TEXT NOT NULL CHECK(kind IN ('algorithm','dataset','template')),
                        ref TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS web_task_material_files (
                        material_id TEXT NOT NULL REFERENCES web_task_materials(id),
                        path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                        content BLOB NOT NULL, PRIMARY KEY(material_id,path))''',
                    '''CREATE TABLE IF NOT EXISTS web_task_grants (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES web_task_deliveries(id),
                        supplement_id TEXT REFERENCES web_task_supplements(id),
                        material_id TEXT NOT NULL REFERENCES web_task_materials(id),
                        target_uid INTEGER NOT NULL, target_username TEXT NOT NULL,
                        grant_kind TEXT NOT NULL CHECK(grant_kind IN ('algorithm','dataset','template')),
                        resource_ref TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)''',
                    '''CREATE TABLE IF NOT EXISTS web_task_requests (
                        teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
                        request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        task_id TEXT REFERENCES web_task_deliveries(id),
                        supplement_id TEXT REFERENCES web_task_supplements(id),
                        created_at TEXT NOT NULL, PRIMARY KEY(teacher_uid,request_id),
                        CHECK((task_id IS NOT NULL) != (supplement_id IS NOT NULL)))''',
                    'CREATE INDEX IF NOT EXISTS web_task_recipient_target ON web_task_recipients(target_uid,target_username)',
                    'CREATE INDEX IF NOT EXISTS web_task_material_task ON web_task_materials(task_id,supplement_id)',
                    'CREATE INDEX IF NOT EXISTS web_task_grant_target ON web_task_grants(target_uid,target_username,revoked_at)',
                ):
                    connection.execute(statement)
                delivery_columns = {row['name'] for row in connection.execute('PRAGMA table_info(web_task_deliveries)')}
                if 'delivery_mode' not in delivery_columns:
                    connection.execute("ALTER TABLE web_task_deliveries ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'project' "
                                       "CHECK(delivery_mode IN ('project','resources'))")
                # Do not rewrite historical rows: a NULL family_id is
                # intentionally interpreted as that row's own one-version
                # family.  That keeps an upgrade non-destructive and makes
                # old request fingerprints independent of this feature.
                if 'family_id' not in delivery_columns:
                    connection.execute('ALTER TABLE web_task_deliveries ADD COLUMN family_id TEXT')
                if 'task_version' not in delivery_columns:
                    connection.execute('ALTER TABLE web_task_deliveries ADD COLUMN task_version INTEGER NOT NULL DEFAULT 1')
                if 'parent_task_id' not in delivery_columns:
                    connection.execute('ALTER TABLE web_task_deliveries ADD COLUMN parent_task_id TEXT')
                connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS web_task_delivery_family_version
                    ON web_task_deliveries(family_id,task_version) WHERE family_id IS NOT NULL''')
                recipient_columns = {row['name'] for row in connection.execute('PRAGMA table_info(web_task_recipients)')}
                if 'project_id' not in recipient_columns:
                    connection.execute('ALTER TABLE web_task_recipients ADD COLUMN project_id TEXT')
                if 'project_name' not in recipient_columns:
                    connection.execute('ALTER TABLE web_task_recipients ADD COLUMN project_name TEXT')
                if 'resource_claim_status' not in recipient_columns:
                    connection.execute("ALTER TABLE web_task_recipients ADD COLUMN resource_claim_status TEXT "
                                       "CHECK(resource_claim_status IN ('claiming','claimed'))")
                normalize(connection, 'web_task_material_files')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_task_dismissals (
                    task_id TEXT NOT NULL REFERENCES web_task_deliveries(id),
                    target_uid INTEGER NOT NULL,
                    target_username TEXT NOT NULL,
                    dismissed_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, target_uid))''')
                delivery_cols = {row['name'] for row in connection.execute('PRAGMA table_info(web_task_deliveries)')}
                if 'deleted_at' not in delivery_cols:
                    connection.execute('ALTER TABLE web_task_deliveries ADD COLUMN deleted_at TEXT')
                if 'deleted_by_username' not in delivery_cols:
                    connection.execute('ALTER TABLE web_task_deliveries ADD COLUMN deleted_by_username TEXT')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _teacher(principal: Principal) -> tuple[int, str, str]:
        uid, username, role = _principal(principal)
        if role != 'teacher':
            raise KitError('只有教师可以管理任务')
        return uid, username, role

    @staticmethod
    def _targets(targets: object) -> list[Principal]:
        if not isinstance(targets, list) or not targets or len(targets) > 100:
            raise KitError('任务需要 1 至 100 个可信目标账号')
        result: list[Principal] = []
        seen_uids: set[int] = set()
        seen_names: set[str] = set()
        for target in targets:
            if not isinstance(target, Principal):
                raise KitError('任务目标必须由服务端解析')
            uid, username, _role = _principal(target)
            if uid in seen_uids or username in seen_names:
                raise KitError('任务目标不能重复')
            seen_uids.add(uid)
            seen_names.add(username)
            result.append(target)
        return result

    @staticmethod
    def _materials(value: object) -> list[dict[str, object]]:
        """Validate exact, resolved algorithm material bytes from the broker."""
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > _MAX_MATERIALS:
            raise KitError(f'算法材料必须是最多 {_MAX_MATERIALS} 项的列表')
        result: list[dict[str, object]] = []
        refs: set[str] = set()
        for item in value:
            if type(item) is not dict or set(item) - {'ref', 'files', 'metadata'} or not {'ref', 'files'} <= set(item):
                raise KitError('算法材料字段无效')
            ref = _text(item['ref'], '算法引用', 200)
            if '@' not in ref or ref in refs:
                raise KitError('算法材料引用无效或重复')
            files, manifest = _validate_files(item['files'])
            metadata = item.get('metadata', {})
            if type(metadata) is not dict:
                raise KitError('算法材料说明无效')
            try:
                json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise KitError('算法材料说明必须由 JSON 值组成') from exc
            refs.add(ref)
            result.append({'ref': ref, 'files': files, 'manifest': manifest, 'metadata': metadata})
        return result

    @staticmethod
    def _references(value: object) -> list[dict[str, object]]:
        return _validate_data_references(value if value is not None else [])

    @staticmethod
    def _source_baseline(store: object, teacher: Principal, submission_id: object) -> dict[str, object]:
        submission_id = _record_id(submission_id, label='任务基线提交 ID')
        source = store.get_submission(submission_id, teacher)
        if not isinstance(source, dict) or not isinstance(source.get('content_sha256'), str):
            raise KitError('任务基线读取返回无效')
        if not _HASH.fullmatch(source['content_sha256']):
            raise KitError('任务基线摘要无效')
        return source

    @staticmethod
    def _baseline_references(source: dict[str, object]) -> list[dict[str, object]]:
        try:
            return _validate_data_references(source['manifest']['data_references']['references'])
        except (KeyError, TypeError, KitError) as exc:
            raise KitError('任务基线的数据引用无效') from exc

    @staticmethod
    def _merge_references(*groups: Iterable[dict[str, object]]) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for group in groups:
            for reference in group:
                key = str(reference['dataset_id']), str(reference['path'])
                if key not in seen:
                    merged.append(dict(reference))
                    seen.add(key)
        return merged

    @staticmethod
    def _task_request(payload: object) -> dict[str, str]:
        if (type(payload) is not dict or set(payload) - {'request_id', 'name', 'instructions', 'parent_task_id'} or
                'request_id' not in payload):
            raise KitError('任务请求字段无效')
        result = {
            'request_id': _request_id(payload['request_id']),
            'name': _text(payload.get('name', ''), '任务名称'),
            'instructions': _text(payload.get('instructions', ''), '任务说明', _MAX_INSTRUCTIONS_BYTES, empty=True),
        }
        if 'parent_task_id' in payload:
            result['parent_task_id'] = _uuid(payload['parent_task_id'], '原任务 ID')
        return result

    @staticmethod
    def _supplement_request(payload: object) -> dict[str, str]:
        if type(payload) is not dict or set(payload) - {'request_id', 'instructions'} or 'request_id' not in payload:
            raise KitError('补充资源请求字段无效')
        return {
            'request_id': _request_id(payload['request_id']),
            'instructions': _text(payload.get('instructions', ''), '补充说明', _MAX_INSTRUCTIONS_BYTES, empty=True),
        }

    @staticmethod
    def _algorithm_identity(material: dict[str, object]) -> dict[str, object]:
        return {'ref': material['ref'], 'manifest': material['manifest'], 'metadata': material['metadata']}

    @staticmethod
    def _request_body(*, kind: str, delivery_mode: str, baseline: dict[str, object] | None,
                      request: dict[str, str], targets: list[Principal],
                      algorithms: list[dict[str, object]], references: list[dict[str, object]]) -> dict[str, object]:
        if delivery_mode == 'project':
            assert baseline is not None
            # Keep this exact body stable: web_task_requests is durable across
            # upgrades, and old project/trial request IDs must retry without a
            # migration or a false "different content" error.
            result = {
                'kind': kind, 'baseline_submission_id': baseline['submission_id'],
                'baseline_sha256': baseline['content_sha256'], 'name': request['name'],
                'instructions': request['instructions'],
                'targets': [{'uid': target.uid, 'username': target.username, 'role': target.role} for target in targets],
                'algorithms': [TaskDelivery._algorithm_identity(item) for item in algorithms],
                'data_references': references,
            }
        else:
            result = {
            'kind': kind, 'delivery_mode': 'resources',
            'baseline_submission_id': None, 'baseline_sha256': None, 'name': request['name'],
            'instructions': request['instructions'],
            'targets': [{'uid': target.uid, 'username': target.username, 'role': target.role} for target in targets],
            'algorithms': [TaskDelivery._algorithm_identity(item) for item in algorithms],
            'data_references': references,
            }
        # Parentage changes the task semantic identity, but only new-version
        # requests carry it.  Omitting it preserves old serialized bytes.
        if 'parent_task_id' in request:
            result['parent_task_id'] = request['parent_task_id']
        return result

    @staticmethod
    def _parent_version(connection: sqlite3.Connection, teacher: Principal, parent_task_id: str,
                        *, kind: str, delivery_mode: str) -> tuple[str, int]:
        """Resolve a current family head inside the caller's write transaction."""
        parent = connection.execute('SELECT * FROM web_task_deliveries WHERE id=?', (parent_task_id,)).fetchone()
        if parent is None:
            raise KitError('原任务不存在')
        if (parent['teacher_uid'], parent['teacher_username']) != (teacher.uid, teacher.username):
            raise KitError('只能从自己的任务发布新版本')
        if parent['kind'] != kind or parent['delivery_mode'] != delivery_mode:
            raise KitError('新版本必须保持原任务的类型和投递方式')
        family_id = parent['family_id'] or parent['id']
        latest = connection.execute('''SELECT id,task_version FROM web_task_deliveries
            WHERE family_id=? OR (family_id IS NULL AND id=?)
            ORDER BY task_version DESC,created_at DESC,id DESC LIMIT 1''', (family_id, family_id)).fetchone()
        if latest is None:  # pragma: no cover - parent is itself a member
            raise KitError('原任务系列记录已损坏')
        if latest['id'] != parent['id']:
            raise KitError('只能从当前最新任务版本发布新版本')
        version = parent['task_version']
        if not isinstance(version, int) or version < 1:
            raise KitError('原任务版本记录已损坏')
        return str(family_id), version + 1

    @staticmethod
    def _require_distinct_project_baseline(connection: sqlite3.Connection, family_id: str,
                                           baseline_submission_id: str) -> None:
        """Keep one task version from reusing an older version's claim slot.

        ``SubmissionStore`` deliberately treats a source submission and a
        recipient as one idempotent distribution.  A later task version must
        therefore point at a new immutable snapshot; otherwise its recipients
        would silently reopen the older task's project.  Resource-only tasks
        have no distribution and do not need this guard.
        """
        existing = connection.execute('''SELECT id FROM web_task_deliveries
            WHERE (family_id=? OR (family_id IS NULL AND id=?))
              AND baseline_submission_id=? LIMIT 1''',
                                      (family_id, family_id, baseline_submission_id)).fetchone()
        if existing is not None:
            raise KitError('新任务版本需要独立固定基线，请通过资源分发发布新版本')

    def validate_parent(self, teacher: Principal, parent_task_id: object, *, kind: str,
                        delivery_mode: str) -> dict[str, object]:
        """Read-only parent validation used by the assignment preview."""
        self._teacher(teacher)
        parent = _uuid(parent_task_id, '原任务 ID')
        if kind not in {'formal', 'trial'} or delivery_mode not in {'project', 'resources'}:
            raise KitError('任务类型或投递方式无效')
        with self._connection() as connection:
            family_id, task_version = self._parent_version(connection, teacher, parent,
                                                            kind=kind, delivery_mode=delivery_mode)
        return {'parent_task_id': parent, 'family_id': family_id, 'next_task_version': task_version}

    @staticmethod
    def _write_material(connection: sqlite3.Connection, task_id: str, supplement_id: str | None,
                        kind: str, ref: str, metadata: object, files: dict[str, bytes] | None) -> str:
        material_id = str(uuid.uuid4())
        try:
            encoded = _json(metadata)
        except (TypeError, ValueError) as exc:
            raise KitError('任务材料说明无效') from exc
        connection.execute('''INSERT INTO web_task_materials(id,task_id,supplement_id,kind,ref,metadata_json,created_at)
                            VALUES(?,?,?,?,?,?,?)''',
                           (material_id, task_id, supplement_id, kind, ref, encoded, _timestamp()))
        if files is not None:
            _packed, manifest = _validate_files(files)
            connection.executemany('''INSERT INTO web_task_material_files(material_id,path,sha256,size,content)
                                    VALUES(?,?,?,?,?)''',
                                   [(material_id, item['path'], item['sha256'], item['size'], files[item['path']])
                                    for item in manifest])
        return material_id

    @staticmethod
    def _write_grants(connection: sqlite3.Connection, task_id: str, supplement_id: str | None,
                      material_id: str, kind: str, resource_ref: str, targets: list[Principal]) -> None:
        from .web_cleanup import require_available
        require_available(connection,kind,resource_ref)
        now = _timestamp()
        connection.executemany('''INSERT INTO web_task_grants(
                id,task_id,supplement_id,material_id,target_uid,target_username,grant_kind,resource_ref,created_at,revoked_at)
                VALUES(?,?,?,?,?,?,?,?,?,NULL)''',
            [(str(uuid.uuid4()), task_id, supplement_id, material_id, target.uid, target.username,
              kind, resource_ref, now) for target in targets])

    def _existing_request(self, connection: sqlite3.Connection, teacher: Principal, request_id: str,
                          fingerprint: str) -> sqlite3.Row | None:
        row = connection.execute('''SELECT * FROM web_task_requests WHERE teacher_uid=? AND request_id=?''',
                                 (teacher.uid, request_id)).fetchone()
        if row is None:
            return None
        if row['teacher_username'] != teacher.username or row['fingerprint'] != fingerprint:
            raise KitError('同一请求 ID 已用于不同内容')
        return row

    def _insert_task(self, teacher: Principal, request: dict[str, str], *, kind: str, delivery_mode: str,
                     baseline: dict[str, object] | None,
                     targets: list[Principal], algorithms: list[dict[str, object]],
                     extra_references: list[dict[str, object]]) -> tuple[str, bool]:
        if delivery_mode not in {'project', 'resources'}:
            raise KitError('任务投递模式无效')
        if (delivery_mode == 'project') != (baseline is not None):
            raise KitError('任务基线与投递模式不一致')
        baseline_references = [] if baseline is None else self._baseline_references(baseline)
        all_references = self._merge_references(baseline_references, extra_references)
        fingerprint = _request_fingerprint(self._request_body(kind=kind, delivery_mode=delivery_mode, baseline=baseline, request=request,
                                                               targets=targets, algorithms=algorithms,
                                                               references=all_references))
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                known = self._existing_request(connection, teacher, request['request_id'], fingerprint)
                if known is not None:
                    task_id = known['task_id']
                    if task_id is None:
                        raise KitError('任务请求记录已损坏')
                    connection.execute('COMMIT')
                    return str(task_id), False
                parent_task_id = request.get('parent_task_id')
                if parent_task_id is None:
                    task_id = str(uuid.uuid4())
                    family_id, task_version = task_id, 1
                else:
                    family_id, task_version = self._parent_version(connection, teacher, parent_task_id,
                                                                    kind=kind, delivery_mode=delivery_mode)
                    if delivery_mode == 'project':
                        assert baseline is not None
                        self._require_distinct_project_baseline(connection, family_id,
                                                                str(baseline['submission_id']))
                    task_id = str(uuid.uuid4())
                now = _timestamp()
                connection.execute('''INSERT INTO web_task_deliveries(
                    id,kind,delivery_mode,family_id,task_version,parent_task_id,teacher_uid,teacher_username,
                    baseline_submission_id,baseline_sha256,name,instructions,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (task_id, kind, delivery_mode, family_id, task_version, parent_task_id, teacher.uid, teacher.username,
                     '' if baseline is None else baseline['submission_id'], '' if baseline is None else baseline['content_sha256'],
                     request['name'], request['instructions'], now))
                connection.executemany('''INSERT INTO web_task_recipients(
                    task_id,target_uid,target_username,target_role,distribution_id,project_id,project_name,resource_claim_status,created_at)
                    VALUES(?,?,?,?,NULL,NULL,NULL,NULL,?)''',
                    [(task_id, target.uid, target.username, target.role, now) for target in targets])
                if kind == 'formal' and baseline is not None:
                    import yaml
                    document = yaml.safe_load(baseline['files'].get('project.yaml', b''))
                    origin = document.get('origin', {}) if isinstance(document, dict) else {}
                    template_ref = origin.get('template_ref') if isinstance(origin, dict) else None
                    if isinstance(template_ref, str) and template_ref:
                        material_id = self._write_material(connection, task_id, None, 'template', template_ref,
                                                           {'source': 'baseline'}, None)
                        self._write_grants(connection, task_id, None, material_id, 'template', template_ref, targets)
                for material in algorithms:
                    material_id = self._write_material(connection, task_id, None, 'algorithm', str(material['ref']),
                                                       material['metadata'], material['files'])
                    self._write_grants(connection, task_id, None, material_id, 'algorithm', str(material['ref']), targets)
                # Both source-case and extra references are represented as exact
                # dataset material.  The metadata makes it possible to show which
                # ones are inherent in the runnable baseline without changing it.
                baseline_set = {(str(item['dataset_id']), str(item['path'])) for item in baseline_references}
                for reference in all_references:
                    source = 'baseline' if (str(reference['dataset_id']), str(reference['path'])) in baseline_set else 'extra'
                    material_id = self._write_material(connection, task_id, None, 'dataset', str(reference['dataset_id']),
                                                       {'reference': reference, 'source': source}, None)
                    self._write_grants(connection, task_id, None, material_id, 'dataset', str(reference['dataset_id']), targets)
                connection.execute('''INSERT INTO web_task_requests(
                    teacher_uid,teacher_username,request_id,fingerprint,task_id,supplement_id,created_at)
                    VALUES(?,?,?,?,?,NULL,?)''',
                    (teacher.uid, teacher.username, request['request_id'], fingerprint, task_id, now))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return task_id, True

    def _dispatch_task(self, teacher: Principal, task_id: str, *, trial: bool) -> list[dict[str, object]]:
        with self._connection() as connection:
            task = connection.execute('SELECT * FROM web_task_deliveries WHERE id=?', (task_id,)).fetchone()
            if task is None or task['teacher_uid'] != teacher.uid or task['teacher_username'] != teacher.username:
                raise KitError('无权分发该任务')
            rows = connection.execute('SELECT * FROM web_task_recipients WHERE task_id=? ORDER BY target_username',
                                      (task_id,)).fetchall()
        if not rows:
            raise KitError('任务没有接收者')
        targets = [Principal(row['target_uid'], row['target_username'], row['target_role']) for row in rows]
        if trial:
            dispatched = self.store.create_distributions(teacher, task['baseline_submission_id'], targets, trial=True)
        else:
            dispatched = self.store.create_distributions(teacher, task['baseline_submission_id'], targets)
        if not isinstance(dispatched, list):
            raise KitError('任务分发服务返回无效')
        by_target: dict[tuple[int, str], dict[str, object]] = {}
        for item in dispatched:
            if type(item) is not dict or not isinstance(item.get('distribution_id'), str):
                raise KitError('任务分发服务返回无效')
            identity = item.get('target_uid'), item.get('target_username')
            if not isinstance(identity[0], int) or not isinstance(identity[1], str):
                raise KitError('任务分发服务返回无效')
            by_target[(identity[0], identity[1])] = item
        expected = {(target.uid, target.username) for target in targets}
        if set(by_target) != expected:
            raise KitError('任务分发服务未覆盖所有目标账号')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                for target in targets:
                    distribution_id = by_target[(target.uid, target.username)]['distribution_id']
                    from .web_cleanup import require_available
                    for grant in connection.execute('SELECT grant_kind,resource_ref FROM web_task_grants WHERE task_id=? AND target_uid=? AND target_username=? AND revoked_at IS NULL',
                                                    (task_id,target.uid,target.username)).fetchall():
                        require_available(connection,grant['grant_kind'],grant['resource_ref'])
                    connection.execute('''UPDATE web_task_recipients SET distribution_id=?
                        WHERE task_id=? AND target_uid=? AND target_username=?
                          AND (distribution_id IS NULL OR distribution_id=?)''',
                        (distribution_id, task_id, target.uid, target.username, distribution_id))
                    if connection.execute('SELECT changes()').fetchone()[0] != 1:
                        raise KitError('任务分发记录已被不一致地修改')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return [by_target[(target.uid, target.username)] for target in targets]

    def create_task(self, teacher: Principal, payload: object, *, baseline_submission_id: object,
                    targets: object, algorithm_materials: object = None, dataset_references: object = None,
                    trial: bool = False) -> dict[str, object]:
        """Persist one task and dispatch its prebuilt exact baseline.

        ``baseline_submission_id`` must be an immutable, already runnable
        snapshot.  A formal caller normally provides a staging-created baseline;
        a trial caller may provide any fixed student submission.  Additional
        materials are recorded separately and never change that snapshot.
        """
        self._teacher(teacher)
        if not isinstance(trial, bool):
            raise KitError('试用任务标记无效')
        request = self._task_request(payload)
        trusted_targets = self._targets(targets)
        algorithms = self._materials(algorithm_materials)
        references = self._references(dataset_references)
        baseline = self._source_baseline(self.store, teacher, baseline_submission_id)
        task_id, _created = self._insert_task(teacher, request, kind='trial' if trial else 'formal',
                                              delivery_mode='project', baseline=baseline,
                                              targets=trusted_targets, algorithms=algorithms,
                                              extra_references=references)
        distributions = self._dispatch_task(teacher, task_id, trial=trial)
        return self.get_task(teacher, task_id, include_materials=True) | {'distributions': distributions}

    def create_trial(self, teacher: Principal, payload: object, *, source_submission_id: object, targets: object) -> dict[str, object]:
        """Distribute a fixed submission without publishing it as a formal asset."""
        return self.create_task(teacher, payload, baseline_submission_id=source_submission_id, targets=targets,
                                algorithm_materials=None, dataset_references=None, trial=True)

    def create_resource_task(self, teacher: Principal, payload: object, *, targets: object,
                             algorithm_materials: object = None, dataset_references: object = None) -> dict[str, object]:
        """Persist a material-only task without manufacturing a runnable baseline."""
        self._teacher(teacher)
        request = self._task_request(payload)
        trusted_targets = self._targets(targets)
        algorithms = self._materials(algorithm_materials)
        references = self._references(dataset_references)
        if not algorithms and not references:
            raise KitError('资源包至少需要一个算法或数据材料')
        task_id, _created = self._insert_task(teacher, request, kind='formal', delivery_mode='resources',
                                              baseline=None, targets=trusted_targets, algorithms=algorithms,
                                              extra_references=references)
        return self.get_task(teacher, task_id, include_materials=True) | {'distributions': []}

    def supplement(self, teacher: Principal, task_id: object, payload: object, *, targets: object,
                   algorithm_materials: object = None, dataset_references: object = None) -> dict[str, object]:
        self._teacher(teacher)
        task_id = _uuid(task_id, '任务 ID')
        request = self._supplement_request(payload)
        trusted_targets = self._targets(targets)
        algorithms = self._materials(algorithm_materials)
        references = self._references(dataset_references)
        fingerprint = _request_fingerprint({
            'task_id': task_id, 'instructions': request['instructions'],
            'targets': [{'uid': target.uid, 'username': target.username, 'role': target.role} for target in trusted_targets],
            'algorithms': [self._algorithm_identity(item) for item in algorithms], 'data_references': references,
        })
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                task = connection.execute('SELECT * FROM web_task_deliveries WHERE id=?', (task_id,)).fetchone()
                if task is None or task['teacher_uid'] != teacher.uid or task['teacher_username'] != teacher.username:
                    raise KitError('无权补充该任务')
                original = connection.execute('SELECT target_uid,target_username,target_role FROM web_task_recipients WHERE task_id=?',
                                              (task_id,)).fetchall()
                allowed = {(row['target_uid'], row['target_username'], row['target_role']) for row in original}
                if any((target.uid, target.username, target.role) not in allowed for target in trusted_targets):
                    raise KitError('补充资源只能发给原任务接收者')
                known = self._existing_request(connection, teacher, request['request_id'], fingerprint)
                if known is not None:
                    supplement_id = known['supplement_id']
                    if supplement_id is None:
                        raise KitError('补充资源请求记录已损坏')
                    connection.execute('COMMIT')
                    return self._supplement_record(str(supplement_id), teacher)
                supplement_id = str(uuid.uuid4())
                now = _timestamp()
                connection.execute('''INSERT INTO web_task_supplements(
                    id,task_id,request_id,fingerprint,teacher_uid,teacher_username,instructions,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (supplement_id, task_id, request['request_id'], fingerprint, teacher.uid, teacher.username,
                     request['instructions'], now))
                connection.executemany('INSERT INTO web_task_supplement_recipients(supplement_id,target_uid,target_username) VALUES(?,?,?)',
                                       [(supplement_id,target.uid,target.username) for target in trusted_targets])
                for material in algorithms:
                    material_id = self._write_material(connection, task_id, supplement_id, 'algorithm', str(material['ref']),
                                                       material['metadata'], material['files'])
                    self._write_grants(connection, task_id, supplement_id, material_id, 'algorithm', str(material['ref']), trusted_targets)
                for reference in references:
                    material_id = self._write_material(connection, task_id, supplement_id, 'dataset', str(reference['dataset_id']),
                                                       {'reference': reference, 'source': 'supplement'}, None)
                    self._write_grants(connection, task_id, supplement_id, material_id, 'dataset',
                                       str(reference['dataset_id']), trusted_targets)
                connection.execute('''INSERT INTO web_task_requests(
                    teacher_uid,teacher_username,request_id,fingerprint,task_id,supplement_id,created_at)
                    VALUES(?,?,?,?,NULL,?,?)''',
                    (teacher.uid, teacher.username, request['request_id'], fingerprint, supplement_id, now))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return self._supplement_record(supplement_id, teacher)

    @staticmethod
    def _materials_for(connection: sqlite3.Connection, task_id: str, *, target: Principal | None = None,
                       include_files: bool = False) -> list[dict[str, object]]:
        clauses, values = ['materials.task_id=?'], [task_id]
        if target is not None:
            clauses.append('''EXISTS (
                SELECT 1 FROM web_task_grants visible
                WHERE visible.material_id=materials.id AND visible.target_uid=? AND visible.target_username=?
                  AND visible.revoked_at IS NULL)''')
            values.extend((target.uid, target.username))
        rows = connection.execute('''SELECT materials.* FROM web_task_materials materials WHERE ''' + ' AND '.join(clauses) +
                                  ' ORDER BY materials.created_at,materials.id', values).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata_json'])
            except (TypeError, ValueError) as exc:
                raise KitError('任务材料记录已损坏') from exc
            item: dict[str, object] = {'material_id': row['id'], 'kind': row['kind'], 'ref': row['ref'],
                                       'supplement_id': row['supplement_id'], 'metadata': metadata,
                                       'created_at': row['created_at']}
            file_rows = connection.execute('SELECT path,sha256,size,content FROM web_task_material_files WHERE material_id=? ORDER BY path',
                                           (row['id'],)).fetchall()
            item['manifest'] = [{'path': entry['path'], 'sha256': entry['sha256'], 'size': entry['size']}
                                for entry in file_rows]
            if include_files:
                item['files'] = {entry['path']: bytes(entry['content']) for entry in file_rows}
            result.append(item)
        return result

    @staticmethod
    def _task_record(connection: sqlite3.Connection, row: sqlite3.Row, *, recipient: Principal | None = None,
                     include_materials: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            'task_id': row['id'], 'kind': row['kind'], 'delivery_mode': row['delivery_mode'],
            'family_id': row['family_id'] or row['id'], 'task_version': row['task_version'] or 1,
            'parent_task_id': row['parent_task_id'],
            'baseline_submission_id': row['baseline_submission_id'] or None,
            'baseline_sha256': row['baseline_sha256'] or None, 'name': row['name'], 'instructions': row['instructions'],
            'teacher_username': row['teacher_username'], 'created_at': row['created_at'],
            'deleted_by_teacher': bool('deleted_at' in row.keys() and row['deleted_at']),
        }
        source = None if row['delivery_mode'] == 'resources' else connection.execute(
            'SELECT p.owner_username,p.id FROM submissions s JOIN projects p ON p.id=s.project_id WHERE s.id=?',
            (row['baseline_submission_id'],)).fetchone()
        if source is not None:
            result['source_owner_username']=source['owner_username']
            result['source_project_id']=source['id']
        if recipient is not None:
            recipient_row = connection.execute('''SELECT distribution_id,project_id,resource_claim_status FROM web_task_recipients
                WHERE task_id=? AND target_uid=? AND target_username=?''',
                (row['id'], recipient.uid, recipient.username)).fetchone()
            if recipient_row is not None:
                result['distribution_id'] = recipient_row['distribution_id']
                if recipient_row['project_id'] is not None:
                    result['project_id'] = recipient_row['project_id']
                if row['delivery_mode'] == 'resources':
                    result['claim_status'] = recipient_row['resource_claim_status']
                else:
                    distribution=connection.execute('SELECT project_id,status FROM distributions WHERE id=?',(recipient_row['distribution_id'],)).fetchone()
                    if distribution is not None:
                        result['project_id']=distribution['project_id']
                        result['claim_status']=distribution['status']
            supplement_summary = connection.execute('''SELECT COUNT(*) AS count,MAX(s.created_at) AS latest
                FROM web_task_supplements s JOIN web_task_supplement_recipients r ON r.supplement_id=s.id
                WHERE s.task_id=? AND r.target_uid=? AND r.target_username=?''',
                                                    (row['id'], recipient.uid, recipient.username)).fetchone()
            result['supplement_count'] = int(supplement_summary['count'])
            result['latest_supplement_at'] = supplement_summary['latest']
        if include_materials:
            result['materials'] = TaskDelivery._materials_for(connection, row['id'], target=recipient)
            query='SELECT s.id,s.instructions,s.created_at FROM web_task_supplements s WHERE s.task_id=?'
            values=[row['id']]
            if recipient is not None:
                query+=' AND EXISTS(SELECT 1 FROM web_task_supplement_recipients r WHERE r.supplement_id=s.id AND r.target_uid=? AND r.target_username=?)'
                values.extend((recipient.uid,recipient.username))
            supplements=connection.execute(query+' ORDER BY s.created_at,s.id',values).fetchall()
            result['supplements']=[{'supplement_id':item['id'],'instructions':item['instructions'],'created_at':item['created_at']} for item in supplements]
        return result

    def _task_access(self, connection: sqlite3.Connection, principal: Principal, task_id: str,
                     *, personal: bool = False) -> tuple[sqlite3.Row, bool]:
        row = connection.execute('SELECT * FROM web_task_deliveries WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KitError('任务不存在')
        teacher = (not personal and principal.role == 'teacher' and row['teacher_uid'] == principal.uid and
                   row['teacher_username'] == principal.username)
        recipient = connection.execute('''SELECT 1 FROM web_task_recipients
            JOIN web_task_deliveries tasks ON tasks.id=web_task_recipients.task_id
            WHERE web_task_recipients.task_id=? AND target_uid=? AND target_username=?
              AND (tasks.delivery_mode='resources' OR distribution_id IS NOT NULL)''',
            (task_id, principal.uid, principal.username)).fetchone() is not None
        if not teacher and not recipient:
            raise KitError('无权查看该任务')
        return row, teacher

    def get_task(self, principal: Principal, task_id: object, *, include_materials: bool = False,
                 material_files: bool = False, personal: bool = False) -> dict[str, object]:
        task_id = _uuid(task_id, '任务 ID')
        if material_files and not include_materials:
            raise KitError('读取材料文件必须同时请求材料')
        with self._connection() as connection:
            row, teacher = self._task_access(connection, principal, task_id, personal=personal)
            result = self._task_record(connection, row, recipient=None if teacher else principal,
                                       include_materials=include_materials)
            if include_materials and material_files:
                result['materials'] = self._materials_for(connection, task_id,
                                                          target=None if teacher else principal, include_files=True)
            if teacher:
                recipients = connection.execute('''SELECT target_uid,target_username,distribution_id,project_id,resource_claim_status FROM web_task_recipients
                    WHERE task_id=? ORDER BY target_username''', (task_id,)).fetchall()
                result['recipients'] = [{'uid': item['target_uid'], 'username': item['target_username'],
                                         'distribution_id': item['distribution_id'], 'project_id': item['project_id']}
                                        for item in recipients]
        return result

    def list_tasks(self, principal: Principal, *, personal: bool = False) -> list[dict[str, object]]:
        with self._connection() as connection:
            if principal.role == 'teacher' and not personal:
                rows = connection.execute('''SELECT * FROM web_task_deliveries
                    WHERE teacher_uid=? AND teacher_username=? AND deleted_at IS NULL
                    ORDER BY created_at DESC,id DESC''', (principal.uid, principal.username)).fetchall()
                return [self._task_record(connection, row) | {
                    'recipient_count': connection.execute('SELECT COUNT(*) FROM web_task_recipients WHERE task_id=?',
                                                          (row['id'],)).fetchone()[0]
                } for row in rows]
            rows = connection.execute('''SELECT tasks.* FROM web_task_deliveries tasks JOIN web_task_recipients recipients
                ON recipients.task_id=tasks.id WHERE recipients.target_uid=? AND recipients.target_username=?
                AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                AND tasks.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM web_task_dismissals d WHERE d.task_id=tasks.id AND d.target_uid=recipients.target_uid)
                ORDER BY tasks.created_at DESC,tasks.id DESC''',
                (principal.uid, principal.username)).fetchall()
            return [self._task_record(connection, row, recipient=principal) for row in rows]

    def list_legacy_distributions(self, teacher: Principal) -> list[dict[str, object]]:
        """Present old standalone distribution rows in the teacher task manager.

        These rows predate web_task_deliveries, so they deliberately expose no
        copied materials or grants.  A distribution linked to any modern task
        never appears here, including when that task has been deleted.
        """
        uid, username, _role = self._teacher(teacher)
        with self._connection() as connection:
            rows = connection.execute('''SELECT distributions.* FROM distributions
                WHERE teacher_uid=? AND teacher_username=? AND deleted_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM web_task_recipients recipients
                                  WHERE recipients.distribution_id=distributions.id)
                ORDER BY created_at DESC,id DESC''', (uid, username)).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                source = connection.execute('''SELECT projects.owner_username,projects.id
                    FROM submissions JOIN projects ON projects.id=submissions.project_id
                    WHERE submissions.id=?''', (row['source_submission_id'],)).fetchone()
                item: dict[str, object] = {
                    'task_id': row['id'], 'kind': 'formal', 'delivery_mode': 'project',
                    'family_id': row['id'], 'task_version': 1, 'parent_task_id': None,
                    'baseline_submission_id': row['source_submission_id'], 'baseline_sha256': row['baseline_sha256'],
                    'name': row['source_project_name'], 'source_project_name': row['source_project_name'], 'instructions': '',
                    'teacher_username': row['teacher_username'], 'created_at': row['created_at'],
                    'deleted_by_teacher': False, 'legacy_distribution': True,
                    'recipient_count': 1, 'recipients': [{
                        'uid': row['target_uid'], 'username': row['target_username'],
                        'distribution_id': row['id'], 'project_id': row['project_id'], 'claim_status': row['status'],
                    }], 'materials': [], 'grants': [],
                }
                if source is not None:
                    item['source_owner_username'] = source['owner_username']
                    item['source_project_id'] = source['id']
                item['claim_status'] = row['status']
                item['project_id'] = row['project_id']
                result.append(item)
        return result

    def _supplement_record(self, supplement_id: str, teacher: Principal) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM web_task_supplements WHERE id=?', (supplement_id,)).fetchone()
            if row is None or row['teacher_uid'] != teacher.uid or row['teacher_username'] != teacher.username:
                raise KitError('补充资源不存在或无权查看')
            materials = self._materials_for(connection, row['task_id'])
            own = [item for item in materials if item['supplement_id'] == supplement_id]
            targets = connection.execute('''SELECT target_uid,target_username FROM web_task_supplement_recipients
                WHERE supplement_id=? ORDER BY target_username''', (supplement_id,)).fetchall()
        return {'supplement_id': row['id'], 'task_id': row['task_id'], 'instructions': row['instructions'],
                'created_at': row['created_at'], 'materials': own,
                'targets': [{'uid': target['target_uid'], 'username': target['target_username']} for target in targets]}

    def list_active_grants(self, principal: Principal, task_id: object | None = None) -> list[dict[str, object]]:
        """Return exact active grants for broker ACL checks; never accepts a UID from HTTP."""
        if task_id is not None:
            task_id = _uuid(task_id, '任务 ID')
        clauses, values = ['grants.revoked_at IS NULL'], []
        if principal.role == 'teacher':
            clauses.append('tasks.teacher_uid=? AND tasks.teacher_username=?')
            values.extend((principal.uid, principal.username))
        else:
            clauses.append('grants.target_uid=? AND grants.target_username=?')
            values.extend((principal.uid, principal.username))
        if task_id is not None:
            clauses.append('grants.task_id=?')
            values.append(task_id)
        with self._connection() as connection:
            recipient_join = '' if principal.role == 'teacher' else ''' JOIN web_task_recipients recipients
                ON recipients.task_id=grants.task_id AND recipients.target_uid=grants.target_uid
                AND recipients.target_username=grants.target_username
                AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)'''
            rows = connection.execute('''SELECT grants.* FROM web_task_grants grants JOIN web_task_deliveries tasks
                ON tasks.id=grants.task_id''' + recipient_join + ' WHERE ' + ' AND '.join(clauses) +
                ' ORDER BY grants.created_at,grants.id', values).fetchall()
        return [{'grant_id': row['id'], 'task_id': row['task_id'], 'supplement_id': row['supplement_id'],
                 'material_id': row['material_id'], 'target_uid': row['target_uid'],
                 'target_username': row['target_username'], 'kind': row['grant_kind'],
                 'resource_ref': row['resource_ref'], 'created_at': row['created_at']} for row in rows]

    def revoke_grant(self, teacher: Principal, grant_id: object) -> dict[str, object]:
        self._teacher(teacher)
        grant_id = _uuid(grant_id, '任务授权 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('''SELECT grants.*,tasks.teacher_uid,tasks.teacher_username FROM web_task_grants grants
                    JOIN web_task_deliveries tasks ON tasks.id=grants.task_id WHERE grants.id=?''', (grant_id,)).fetchone()
                if row is None or row['teacher_uid'] != teacher.uid or row['teacher_username'] != teacher.username:
                    raise KitError('无权撤销该任务授权')
                if row['revoked_at'] is None:
                    connection.execute('UPDATE web_task_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                                       (_timestamp(), grant_id))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'grant_id': grant_id, 'task_id': row['task_id'], 'target_username': row['target_username'],
                'kind': row['grant_kind'], 'resource_ref': row['resource_ref'], 'revoked': True}

    def claim_resource_project(self, principal: Principal, task_id: object) -> dict[str, object]:
        """Allocate one empty personal project for an exact resource recipient.

        The workspace builds the local directory after this durable broker
        allocation.  Repeated claims return this same project record, so a
        local setup retry never creates a second project or weakens grants.
        """
        uid, username, _role = _principal(principal)
        task_id = _uuid(task_id, '任务 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('''SELECT tasks.name,tasks.deleted_at,recipients.project_id,recipients.project_name,
                    recipients.resource_claim_status
                    FROM web_task_deliveries tasks JOIN web_task_recipients recipients ON recipients.task_id=tasks.id
                    WHERE tasks.id=? AND tasks.delivery_mode='resources'
                      AND recipients.target_uid=? AND recipients.target_username=?''',
                    (task_id, uid, username)).fetchone()
                if row is None:
                    raise KitError('无权领取该资源包任务')
                if row['deleted_at'] is not None and row['project_id'] is None:
                    raise KitError('该布置任务已被管理员删除，无法从此入口启动，建议在列表点击“删除记录”。')
                project_id = row['project_id']
                if project_id is None:
                    project_id = str(uuid.uuid4())
                    project_name = str(row['name'])
                    now = _timestamp()
                    connection.execute('''INSERT INTO projects(id,owner_uid,owner_username,source,name,created_at)
                        VALUES(?,?,?,?,?,?)''',
                        (project_id, uid, username, 'self-created', project_name, now))
                    connection.execute('''UPDATE web_task_recipients
                        SET project_id=?,project_name=?,resource_claim_status='claiming'
                        WHERE task_id=? AND target_uid=? AND target_username=? AND project_id IS NULL''',
                        (project_id, project_name, task_id, uid, username))
                    if connection.execute('SELECT changes()').fetchone()[0] != 1:
                        raise KitError('资源包项目领取记录已被不一致地修改')
                else:
                    project = connection.execute('''SELECT id,owner_uid,owner_username,source,name FROM projects WHERE id=?''',
                                                 (project_id,)).fetchone()
                    if (project is None or project['owner_uid'] != uid or project['owner_username'] != username or
                            project['source'] != 'self-created'):
                        raise KitError('资源包项目领取记录已损坏')
                    project_name = project['name']
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        project = self.store.get_project(project_id, principal)
        if not isinstance(project, dict) or project.get('project_id') != project_id:
            raise KitError('资源包项目领取服务返回无效')
        project['claim_status'] = row['resource_claim_status'] if row['project_id'] is not None else 'claiming'
        return project

    def confirm_resource_project(self, principal: Principal, task_id: object, project_id: object) -> dict[str, object]:
        """Mark one allocated resource workspace ready after local materialization."""
        uid, username, _role = _principal(principal)
        task_id = _uuid(task_id, '任务 ID')
        project_id = _record_id(project_id, label='项目 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('''SELECT recipients.project_id,recipients.resource_claim_status
                    FROM web_task_deliveries tasks JOIN web_task_recipients recipients ON recipients.task_id=tasks.id
                    WHERE tasks.id=? AND tasks.delivery_mode='resources'
                      AND recipients.target_uid=? AND recipients.target_username=?''',
                    (task_id, uid, username)).fetchone()
                if row is None or row['project_id'] != project_id:
                    raise KitError('无权确认该资源包项目')
                project = connection.execute('''SELECT id,owner_uid,owner_username,source FROM projects WHERE id=?''',
                                             (project_id,)).fetchone()
                if (project is None or project['owner_uid'] != uid or project['owner_username'] != username or
                        project['source'] != 'self-created'):
                    raise KitError('资源包项目领取记录已损坏')
                if row['resource_claim_status'] == 'claiming':
                    connection.execute('''UPDATE web_task_recipients SET resource_claim_status='claimed'
                        WHERE task_id=? AND target_uid=? AND target_username=? AND project_id=?
                          AND resource_claim_status='claiming' ''',
                        (task_id, uid, username, project_id))
                elif row['resource_claim_status'] != 'claimed':
                    raise KitError('资源包项目领取状态无效')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        result = self.store.get_project(project_id, principal)
        if not isinstance(result, dict) or result.get('project_id') != project_id:
            raise KitError('资源包项目确认服务返回无效')
        result['claim_status'] = 'claimed'
        return result

    def delete_task(self, teacher: Principal, task_id: object) -> dict[str, object]:
        # Teacher soft-deletes a task while preserving student algorithm/dataset access grants.
        uid, username, _role = self._teacher(teacher)
        task_id = _uuid(task_id, '任务 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT * FROM web_task_deliveries WHERE id=?', (task_id,)).fetchone()
                if row is not None:
                    if row['teacher_uid'] != uid or row['teacher_username'] != username:
                        raise KitError('无权删除该任务')
                    if row['deleted_at'] is None:
                        connection.execute(
                            'UPDATE web_task_deliveries SET deleted_at=?,deleted_by_username=? WHERE id=?',
                            (_timestamp(), username, task_id),
                        )
                else:
                    legacy = connection.execute('''SELECT * FROM distributions WHERE id=?
                        AND teacher_uid=? AND teacher_username=?
                        AND NOT EXISTS (SELECT 1 FROM web_task_recipients recipients
                                        WHERE recipients.distribution_id=distributions.id)''',
                        (task_id, uid, username)).fetchone()
                    if legacy is None:
                        raise KitError('无权删除该任务')
                    if legacy['deleted_at'] is None:
                        connection.execute('UPDATE distributions SET deleted_at=?,deleted_by_username=? WHERE id=?',
                                           (_timestamp(), username, task_id))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'task_id': task_id, 'deleted': True}

    def dismiss_task(self, principal: Principal, task_id: object) -> dict[str, object]:
        # Recipient hides a task entry from their personal list without affecting started projects.
        uid, username, _role = _principal(principal)
        task_id = _uuid(task_id, '任务 ID')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                rec = connection.execute(
                    'SELECT 1 FROM web_task_recipients WHERE task_id=? AND target_uid=? AND target_username=?',
                    (task_id, uid, username),
                ).fetchone()
                if rec is None:
                    raise KitError('无权移除该任务记录')
                connection.execute(
                    '''INSERT INTO web_task_dismissals(task_id,target_uid,target_username,dismissed_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(task_id,target_uid) DO UPDATE SET dismissed_at=excluded.dismissed_at''',
                    (task_id, uid, username, _timestamp()),
                )
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'task_id': task_id, 'dismissed': True}

    def check_distribution_claimable(self, principal: Principal, distribution_id: str) -> None:
        uid, username, _role = _principal(principal)
        with self._connection() as connection:
            row = connection.execute(
                '''SELECT d.status,d.deleted_at,
                   EXISTS(SELECT 1 FROM web_task_recipients r
                          JOIN web_task_deliveries t ON t.id=r.task_id
                          WHERE r.distribution_id=d.id) AS has_task,
                   EXISTS(SELECT 1 FROM web_task_recipients r
                          JOIN web_task_deliveries t ON t.id=r.task_id
                          WHERE r.distribution_id=d.id AND t.deleted_at IS NULL) AS has_active_task
                   FROM distributions d
                   WHERE d.id=? AND d.target_uid=? AND d.target_username=?''',
                (distribution_id, uid, username),
            ).fetchone()
            if row is not None and row['status'] == 'pending' and (
                    row['deleted_at'] is not None or (row['has_task'] and not row['has_active_task'])):
                raise KitError('该布置任务已被管理员删除，无法再次启动新项目；建议点击“删除记录”移除。')
