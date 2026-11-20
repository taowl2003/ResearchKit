"""Teacher review queue for root-published shared datasets.

The Web process can review a source snapshot but cannot publish it.  A small
root-owned publisher claims immutable jobs from this store and performs the
expensive content hashing.  This separation keeps user-facing HTTP processes
away from raw paths and makes restarts/audit records explicit.
"""
from __future__ import annotations

import hashlib
import json
import pwd
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .files import KitError
from .web_dataset_discovery import discover, load_management_config
from .web_dataset_metadata import _FIELDS, normalize_tags
from .web_identity import Principal, IdentityError

MAX_ITEMS = 4096
MAX_SCAN_BYTES = 8 * 1024 * 1024
_STATUSES = {'pending', 'queued', 'publishing', 'published', 'ignored', 'failed', 'missing', 'error', 'withdrawn'}
_IMMUTABLE_METADATA = {'wiki_ref', 'wiki_fact_state', 'provenance_ref'}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _read_json(value: str | None, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise KitError('数据管理存储记录无效') from exc


def _teacher(principal: object) -> Principal:
    if not isinstance(principal, Principal) or principal.role != 'teacher' or principal.username != 'ly':
        raise KitError('只有教师可以管理共享数据集')
    return principal


def _text_metadata(value: object, *, partial: bool = True, editable_only: bool = False) -> dict[str, object]:
    if value is None:
        return {}
    if type(value) is not dict or set(value) - _FIELDS or (editable_only and set(value) & _IMMUTABLE_METADATA):
        raise KitError('数据集说明字段无效')
    if not partial and not value:
        raise KitError('数据集说明不能为空')
    result: dict[str, object] = {}
    for key, text in value.items():
        if key == 'tags':
            result[key] = normalize_tags(text)
        elif not isinstance(text, str) or len(text.encode('utf-8')) > 16384:
            raise KitError('数据集说明文本无效')
        else:
            result[key] = text
    return result


class DataManagement:
    """SQLite data-management tables, owned by an existing SubmissionStore."""

    def __init__(self, store: object, policy: object):
        self.store = store
        self.policy = policy
        self.config = load_management_config(getattr(policy, 'dataset_management_config', None))
        self._initialize()

    def _connection(self):
        connection = getattr(self.store, '_connection', None)
        if not callable(connection):
            raise KitError('数据管理需要受信任的提交存储')
        return connection()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS data_management_candidates (
                    candidate_id TEXT PRIMARY KEY, relative_root TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, source_fingerprint_json TEXT,
                    metadata_digest TEXT NOT NULL, original_metadata_json TEXT NOT NULL,
                    current_metadata_json TEXT NOT NULL, draft_metadata_json TEXT NOT NULL,
                    seed_state TEXT NOT NULL, registered_dataset_id TEXT, dataset_id TEXT,
                    readers_json TEXT, review_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS data_management_jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('scan','publish')),
                    candidate_id TEXT, candidate_revision INTEGER, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
                    lease_token TEXT, lease_expires_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT
                )''')
                connection.execute('CREATE INDEX IF NOT EXISTS data_management_candidates_status ON data_management_candidates(status, updated_at DESC)')
                connection.execute('CREATE INDEX IF NOT EXISTS data_management_jobs_active ON data_management_jobs(status, created_at)')
                connection.execute('''CREATE TABLE IF NOT EXISTS data_management_access_requests (
                    job_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL
                )''')
                connection.execute('CREATE INDEX IF NOT EXISTS data_management_access_candidate ON data_management_access_requests(candidate_id)')
                connection.execute('''CREATE TABLE IF NOT EXISTS data_management_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    action TEXT NOT NULL, principal_uid INTEGER, principal_username TEXT,
                    detail_json TEXT NOT NULL, created_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS data_management_worker_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), instance_id TEXT NOT NULL,
                    started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, heartbeat_epoch REAL NOT NULL
                )''')
                from .web_readme_cache import initialize
                initialize(connection)
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def _configured(self) -> dict[str, object]:
        if self.config is None:
            raise KitError('共享数据集管理尚未由管理员配置')
        return self.config

    def _audit(self, connection: Any, candidate_id: str, revision: int, action: str,
               principal: Principal | None, detail: object) -> None:
        connection.execute('INSERT INTO data_management_audit(candidate_id, revision, action, principal_uid, principal_username, detail_json, created_at) VALUES(?,?,?,?,?,?,?)',
                           (candidate_id, revision, action, None if principal is None else principal.uid,
                            None if principal is None else principal.username, _json(detail), _now()))

    @staticmethod
    def _row(row: Any, *, detail: bool = False) -> dict[str, object]:
        original = _read_json(row['original_metadata_json'], {})
        current = _read_json(row['current_metadata_json'], {})
        draft = _read_json(row['draft_metadata_json'], {})
        item: dict[str, object] = {
            'candidate_id': row['candidate_id'], 'revision': row['revision'], 'kind': row['kind'],
            'status': row['status'], 'relative_root': row['relative_root'], 'dataset_id': row['dataset_id'],
            'registered_dataset_id': row['registered_dataset_id'], 'error': row['error'],
            'updated_at': row['updated_at'], 'name': current.get('name') or draft.get('name') or original.get('name', row['relative_root'].split('/')[-1]),
            'description': (draft.get('description') or current.get('description') or original.get('description', ''))[:240],
            'source_metadata_changed': bool(row['review_json'] and _read_json(row['review_json'], {}).get('source_metadata_changed')),
        }
        item['readme_managed'] = row['seed_state'] == 'readme'
        if item['readme_managed']:
            item['description'] = original.get('description', '')[:240]
        tags = next((metadata.get('tags') for metadata in (original, current, draft)
                     if isinstance(metadata.get('tags'), list)), [])
        item['tags'] = normalize_tags(tags)
        if detail:
            item.update({'source_fingerprint': _read_json(row['source_fingerprint_json'], None),
                         'original_metadata': original, 'current_metadata': current, 'draft_metadata': draft,
                         'seed_state': row['seed_state'], 'readers': _read_json(row['readers_json'], None),
                         'review': _read_json(row['review_json'], None), 'created_at': row['created_at']})
            review = _read_json(row['review_json'], {})
            item['previous_original_metadata'] = review.get('previous_original_metadata', {}) if isinstance(review, dict) else {}
        return item

    def status(self, principal: object) -> dict[str, object]:
        _teacher(principal)
        if self.config is None:
            return {'configured': False, 'worker_online': False, 'last_scan': None, 'last_error': None,
                    'counts': {}, 'active_job': None}
        with self._connection() as connection:
            rows = connection.execute('SELECT status, count(*) AS count FROM data_management_candidates GROUP BY status').fetchall()
            scan = connection.execute("SELECT status, updated_at, error FROM data_management_jobs WHERE kind='scan' ORDER BY created_at DESC LIMIT 1").fetchone()
            active = connection.execute("SELECT job_id, kind, status, candidate_id, created_at FROM data_management_jobs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            worker = connection.execute('SELECT heartbeat_epoch FROM data_management_worker_state WHERE singleton=1').fetchone()
        counts = {row['status']: row['count'] for row in rows}
        counts['attention'] = sum(counts.get(key, 0) for key in ('failed', 'missing', 'error'))
        return {'configured': True, 'worker_online': bool(worker and worker['heartbeat_epoch'] >= time.time() - 150),
                'discovery_mode': self.config.get('discovery_mode', 'wiki'),
                'last_scan': None if scan is None else scan['updated_at'], 'last_error': None if scan is None else scan['error'],
                'counts': counts,
                'active_job': None if active is None else dict(active)}

    def list_candidates(self, principal: object, *, status: str = 'pending', query: str = '', offset: int = 0,
                        limit: int = 50, include_inactive: bool = False, tags: object = None,
                        tag_mode: str = 'all') -> dict[str, object]:
        _teacher(principal); self._configured()
        if type(include_inactive) is not bool:
            raise KitError('include_inactive 必须是布尔值')
        from .web_lifecycle import AssetLifecycle
        lifecycle = AssetLifecycle(self.store)
        aliases = {'active': ('queued', 'publishing'), 'attention': ('failed', 'missing', 'error')}
        if status != 'all' and status not in _STATUSES and status not in aliases:
            raise KitError('候选状态无效')
        if not isinstance(query, str) or len(query) > 256 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise KitError('候选列表参数无效')
        if tags is None:
            tags = []
        try:
            selected_tags = normalize_tags(tags, label='标签筛选')
        except KitError as exc:
            raise KitError(str(exc)) from exc
        if not isinstance(tag_mode, str) or tag_mode not in {'any', 'all'}:
            raise KitError('标签筛选方式无效')
        clauses, params = [], []
        lifecycle_clause = "NOT EXISTS (SELECT 1 FROM web_asset_lifecycle l WHERE l.kind='dataset' AND l.ref=registered_dataset_id AND l.state <> 'active')"
        if status != 'withdrawn':
            clauses.append("status <> 'withdrawn'")
        if not include_inactive:
            clauses.append(lifecycle_clause)
        if status in aliases:
            clauses.append('status IN (' + ','.join('?' for _ in aliases[status]) + ')'); params.extend(aliases[status])
        elif status != 'all':
            clauses.append('status=?'); params.append(status)
        if query:
            clauses.append('(relative_root LIKE ? ESCAPE \'\\\' OR original_metadata_json LIKE ? ESCAPE \'\\\' OR current_metadata_json LIKE ? ESCAPE \'\\\' OR draft_metadata_json LIKE ? ESCAPE \'\\\')')
            escaped = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'; params += [escaped, escaped, escaped, escaped]
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._connection() as connection:
            total = connection.execute('SELECT count(*) FROM data_management_candidates' + where, params).fetchone()[0]
            if total > MAX_ITEMS:
                raise KitError('候选筛选范围过大，请缩小状态或搜索条件')
            rows = connection.execute('SELECT * FROM data_management_candidates' + where + ' ORDER BY updated_at DESC, candidate_id', params).fetchall()
            # Keep the tag vocabulary visible when status/search filters
            # remove datasets. Respect the existing history/lifecycle scope.
            tag_clauses = [] if include_inactive else [lifecycle_clause]
            if status != 'withdrawn':
                tag_clauses.append("status <> 'withdrawn'")
            else:
                tag_clauses.append("status = 'withdrawn'")
            tag_where = ' WHERE ' + ' AND '.join(tag_clauses) if tag_clauses else ''
            all_tags = {}
            for record in connection.execute('SELECT original_metadata_json,current_metadata_json,draft_metadata_json FROM data_management_candidates' + tag_where):
                sources = [_read_json(record[key], {}) for key in ('original_metadata_json', 'current_metadata_json', 'draft_metadata_json')]
                source_tags = next((metadata.get('tags') for metadata in sources if isinstance(metadata.get('tags'), list)), [])
                for tag in normalize_tags(source_tags):
                    all_tags.setdefault(tag.casefold(), tag)
        candidates = [self._row(row) for row in rows]
        available = {}
        for item in candidates:
            for tag in item['tags']:
                available.setdefault(tag.casefold(), tag)
        available_tags = sorted(available.values(), key=str.casefold)
        wanted = {tag.casefold() for tag in selected_tags}
        def selected(item: dict[str, object], mode: str) -> bool:
            if not wanted:
                return True
            actual = {tag.casefold() for tag in item['tags']}
            return wanted <= actual if mode == 'all' else bool(wanted & actual)
        mode_totals = {mode: sum(selected(item, mode) for item in candidates) for mode in ('any', 'all')}
        candidates = [item for item in candidates if selected(item, tag_mode)]
        # This union is computed before pagination: a tag on page two must
        # remain selectable. With ALL matching, each enabled addition shares
        # at least one currently matching dataset with the existing choices.
        selectable_tags = {}
        for item in candidates:
            for tag in item['tags']:
                selectable_tags.setdefault(tag.casefold(), tag)
        total = len(candidates)
        items = candidates[offset:offset + limit]
        for item in items:
            if item.get('registered_dataset_id'):
                item['lifecycle'] = lifecycle.summary('dataset', item['registered_dataset_id'])
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit, 'status': status,
                'tags': selected_tags, 'tag_mode': tag_mode, 'available_tags': available_tags,
                'all_tags': sorted(all_tags.values(), key=str.casefold),
                'selectable_tags': sorted(selectable_tags.values(), key=str.casefold),
                'tag_mode_totals': mode_totals}

    def get_candidate(self, principal: object, candidate_id: str) -> dict[str, object]:
        _teacher(principal); self._configured()
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM data_management_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
            access_jobs = connection.execute('''SELECT j.* FROM data_management_jobs j
                JOIN data_management_access_requests a ON a.job_id=j.job_id
                WHERE a.candidate_id=? ORDER BY j.rowid DESC LIMIT 10''', (candidate_id,)).fetchall()
        if row is None:
            raise KitError('数据集候选不存在')
        result = self._row(row, detail=True)
        if result['readme_managed']:
            from .web_readme_cache import get
            source = result['original_metadata']
            result['readme'] = get(self, source.get('provenance_ref', ''), source.get('description', ''), candidate_id=candidate_id)
        if row['registered_dataset_id']:
            from .web_lifecycle import AssetLifecycle
            result['lifecycle'] = AssetLifecycle(self.store).summary('dataset', row['registered_dataset_id'])
        result['access_history'] = []
        for job in access_jobs:
            payload = _read_json(job['payload_json'], {})
            result['access_history'].append({'job_id': job['job_id'], 'status': job['status'],
                'before_readers': payload['expected_entry']['readers'], 'readers': payload['readers'],
                'reviewer_username': payload['reviewer_username'], 'created_at': job['created_at'],
                'updated_at': job['updated_at'], 'error': job['error']})
        result['access_token'] = None
        if row['registered_dataset_id'] and getattr(self.policy, 'dataset_tree_registry', None) is not None:
            from .web_dataset_access import registered_entry, access_token
            try:
                entry = registered_entry(self.policy, row['registered_dataset_id'])
                result.update(readers=entry['readers'], access_token=access_token(entry))
            except KitError as error:
                result['readers'] = None
                result['access_error'] = str(error)
        return result

    def change_access(self, principal: object, candidate_id: str, revision: int, *,
                      access_token: str, readers: object) -> dict[str, object]:
        principal = _teacher(principal); self._configured()
        from .web_dataset_access import registered_entry, access_token as token_for
        if type(revision) is not int or revision < 1 or not isinstance(access_token, str) or len(access_token) != 64:
            raise KitError('授权修改请求无效，请刷新')
        if type(readers) is list and len(readers) > 128:
            raise KitError('指定账号不能超过128个')
        resolved = self._resolve_readers(readers)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT * FROM data_management_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
                if row is None or not row['registered_dataset_id']:
                    raise KitError('只能修改已登记的数据集授权')
                from .web_cleanup import require_available
                require_available(connection, 'dataset', row['registered_dataset_id'])
                if row['revision'] != revision:
                    raise KitError('候选已更新，请刷新后重新修改授权')
                pending = connection.execute("SELECT j.* FROM data_management_jobs j WHERE j.candidate_id=? AND j.status IN ('queued','running') ORDER BY j.rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                if pending is not None:
                    payload = _read_json(pending['payload_json'], {})
                    if (payload.get('kind') == 'access-change' and payload.get('readers') == resolved
                            and token_for(payload['expected_entry']) == access_token):
                        connection.execute('COMMIT')
                        return {'job_id': pending['job_id'], 'status': pending['status'], 'reused': True}
                    raise KitError('该数据集已有任务正在处理，请等待完成后再修改授权')
                entry = registered_entry(self.policy, row['registered_dataset_id'])
                if token_for(entry) != access_token:
                    raise KitError('数据授权已变化，请刷新后重新修改')
                if entry['readers'] == resolved:
                    connection.execute('COMMIT')
                    return {'status': 'unchanged', 'dataset_id': row['registered_dataset_id']}
                job_id = str(uuid.uuid4()); now = _now()
                payload = {'kind': 'access-change', 'request_id': job_id, 'candidate_id': candidate_id,
                    'revision': revision, 'relative_root': row['relative_root'], 'dataset_id': row['registered_dataset_id'],
                    'expected_entry': entry, 'readers': resolved, 'reviewer_uid': principal.uid,
                    'reviewer_username': principal.username}
                connection.execute("INSERT INTO data_management_jobs(job_id,kind,candidate_id,candidate_revision,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                                   (job_id, 'publish', candidate_id, revision, _json(payload), now, now))
                connection.execute('INSERT INTO data_management_access_requests(job_id,candidate_id) VALUES(?,?)', (job_id, candidate_id))
                self._audit(connection, candidate_id, revision, 'access-requested', principal,
                            {'job_id': job_id, 'before_readers': entry['readers'], 'readers': resolved})
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'job_id': job_id, 'status': 'queued', 'dataset_id': row['registered_dataset_id']}

    def request_scan(self, principal: object) -> dict[str, object]:
        principal = _teacher(principal); self._configured()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                previous = connection.execute("SELECT job_id, status FROM data_management_jobs WHERE kind='scan' AND status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
                if previous is not None:
                    connection.execute('COMMIT'); return {'job_id': previous['job_id'], 'status': previous['status'], 'reused': True}
                threshold = (datetime.now(UTC).replace(microsecond=0).timestamp() - 60)
                cutoff = datetime.fromtimestamp(threshold, UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
                recent = connection.execute("SELECT job_id,status FROM data_management_jobs WHERE kind='scan' AND created_at>=? ORDER BY created_at DESC LIMIT 1", (cutoff,)).fetchone()
                if recent is not None:
                    connection.execute('COMMIT'); return {'job_id': recent['job_id'], 'status': recent['status'], 'reused': True}
                job_id = str(uuid.uuid4()); now = _now()
                connection.execute("INSERT INTO data_management_jobs(job_id,kind,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                   (job_id, 'scan', '{}', 'queued', now, now))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'job_id': job_id, 'status': 'queued', 'reused': False}

    def _resolve_readers(self, readers: object) -> object:
        if readers == 'platform-users':
            return readers
        if type(readers) is not list or not readers or len(readers) > 4096 or any(not isinstance(name, str) for name in readers):
            raise KitError('数据集授权无效')
        if len(set(readers)) != len(readers):
            raise KitError('数据集授权账户不在平台策略中')
        result = []
        for name in readers:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', name):
                raise KitError('数据集授权账户不在平台策略中')
            try:
                account = pwd.getpwnam(name)
                principal = self.policy.principal(account.pw_uid)
            except (KeyError, AttributeError, OSError, ValueError, IdentityError) as exc:
                raise KitError('数据集授权账户不在平台策略中') from exc
            if principal.username != name:
                raise KitError('数据集授权账户身份不一致')
            result.append({'uid': principal.uid, 'username': principal.username})
        return result

    def _readers(self, readers: object, *, old: object = None, kind: str) -> object:
        if kind == 'metadata-change':
            if readers is None:
                return old
            resolved = self._resolve_readers(readers)
            if resolved != old:
                raise KitError('仅更新说明时不能修改数据集授权')
            return old
        return self._resolve_readers(readers)

    def review(self, principal: object, candidate_id: str, revision: int, *, decision: str = 'approve',
               metadata: object = None, readers: object = None) -> dict[str, object]:
        principal = _teacher(principal); self._configured()
        if decision not in {'approve', 'ignore', 'retry'} or type(revision) is not int or revision < 1:
            raise KitError('审核请求无效')
        patch = _text_metadata(metadata)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT * FROM data_management_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
                if row is None: raise KitError('数据集候选不存在')
                from .web_cleanup import require_available
                for identity in (row['registered_dataset_id'], row['dataset_id']):
                    if identity:
                        require_available(connection, 'dataset', identity)
                if row['revision'] != revision: raise KitError('候选已更新，请重新查看后审核')
                if connection.execute("SELECT 1 FROM data_management_access_requests a JOIN data_management_jobs j ON a.job_id=j.job_id WHERE a.candidate_id=? AND j.status IN ('queued','running')", (candidate_id,)).fetchone():
                    raise KitError('使用范围正在修改，请等待完成后再审核')
                if row['status'] == 'publishing': raise KitError('候选正在发布，不能修改审核')
                if row['status'] == 'withdrawn':
                    raise KitError('此目录已不再是当前数据集边界，不能审核登记；请重新扫描并选择当前目录')
                kind = row['kind']; old_readers = _read_json(row['readers_json'], None)
                if decision == 'ignore':
                    connection.execute("UPDATE data_management_jobs SET status='cancelled', updated_at=?, error='教师暂不纳入' WHERE candidate_id=? AND status='queued'", (_now(), candidate_id))
                    connection.execute("UPDATE data_management_candidates SET status='ignored', error=NULL, updated_at=? WHERE candidate_id=?", (_now(), candidate_id))
                    self._audit(connection, candidate_id, revision, 'ignore', principal, {})
                    connection.execute('COMMIT'); return {'candidate_id': candidate_id, 'revision': revision, 'status': 'ignored'}
                if decision == 'retry' and row['status'] != 'failed':
                    raise KitError('只有失败的候选可以重试')
                if kind in {'missing', 'error', 'unchanged'}:
                    raise KitError('此候选需要重新扫描后才能审核')
                if kind == 'data-change':
                    raise KitError('原始目录已变化，请先保留旧版本并在新目录登记，不能覆盖旧引用')
                resolved_readers = self._readers(readers, old=old_readers, kind=kind)
                original = _read_json(row['original_metadata_json'], {})
                # The UI may submit all nine fields for a simple form.  Keep
                # source-evidence fields only when they exactly echo the
                # immutable Wiki snapshot; never let a review rewrite them.
                for key in _IMMUTABLE_METADATA | {'tags'}:
                    if key in patch and patch[key] != original.get(key):
                        raise KitError('来源说明和标签字段只能由来源扫描更新')
                    patch.pop(key, None)
                base = _read_json(row['draft_metadata_json'], {}) or _read_json(row['current_metadata_json'], {}) or original
                source_fields = _IMMUTABLE_METADATA | ({'tags'} if 'tags' in original else set())
                current = {**base, **patch, **{key: value for key, value in original.items() if key in source_fields}}
                if set(current) - _FIELDS: raise KitError('数据集说明字段无效')
                dataset_id = row['registered_dataset_id'] if kind == 'metadata-change' else row['dataset_id']
                if not dataset_id: raise KitError('候选缺少将要发布的数据集身份')
                payload = {'candidate_id': candidate_id, 'revision': revision, 'relative_root': row['relative_root'], 'kind': kind,
                           'dataset_id': dataset_id, 'registered_dataset_id': row['registered_dataset_id'],
                           'source_fingerprint': _read_json(row['source_fingerprint_json'], None),
                           'original_metadata': original, 'metadata_digest': row['metadata_digest'],
                           'current_metadata': current, 'metadata': current, 'readers': resolved_readers}
                now = _now(); job_id = str(uuid.uuid4())
                connection.execute("UPDATE data_management_jobs SET status='cancelled', updated_at=?, error='审核版本被替换' WHERE candidate_id=? AND status='queued'", (now, candidate_id))
                connection.execute("UPDATE data_management_candidates SET status='queued', current_metadata_json=?, readers_json=?, review_json=?, error=NULL, updated_at=? WHERE candidate_id=?",
                                   (_json(current), _json(resolved_readers), _json({'reviewer_uid': principal.uid, 'reviewer_username': principal.username,
                                     'reviewed_at': now, 'source_metadata_changed': False}), now, candidate_id))
                connection.execute("INSERT INTO data_management_jobs(job_id,kind,candidate_id,candidate_revision,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                   (job_id, 'publish', candidate_id, revision, _json(payload), 'queued', now, now))
                self._audit(connection, candidate_id, revision, 'approve' if decision == 'approve' else 'retry', principal, {'job_id': job_id})
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'candidate_id': candidate_id, 'revision': revision, 'status': 'queued', 'job_id': job_id,
                'dataset_id': dataset_id, 'registered_dataset_id': row['registered_dataset_id']}

    def _dataset_id(self, relative_root: str, kind: str, registered_dataset_id: str | None, revision: int = 1) -> str | None:
        if kind in {'metadata-change', 'unchanged'}: return registered_dataset_id
        if kind == 'data-change': return None
        path = Path(self._configured()['raw_root']) / relative_root
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        # A revised source gets a fresh unpublished identity, including after
        # a crash left an uncommitted manifest. Published roots retain their ID.
        return f'shared-{digest}-v{revision}'

    def _upsert_scan(self, connection: Any, items: object) -> int:
        if type(items) is not list or len(items) > MAX_ITEMS or len(_json(items).encode()) > MAX_SCAN_BYTES:
            raise KitError('扫描结果超过限制')
        now = _now(); count = 0; seen = set()
        readme_mode = self._configured().get('discovery_mode') == 'readme'
        for value in items:
            if type(value) is not dict: raise KitError('扫描候选无效')
            from .web_cleanup import reserved
            if any(reserved(connection, 'dataset', value.get(key)) for key in ('registered_dataset_id', 'dataset_id') if value.get(key)):
                continue
            if type(value) is not dict: raise KitError('扫描候选无效')
            needed = {'candidate_id', 'relative_root', 'original_metadata', 'metadata_digest', 'source_fingerprint', 'kind'}
            if not needed <= set(value) or not isinstance(value['candidate_id'], str) or not isinstance(value['relative_root'], str):
                raise KitError('扫描候选字段无效')
            seen.add(value['candidate_id'])
            automatic_notes = readme_mode and value.get('readme_managed') is True
            kind = value['kind']
            if kind not in {'new', 'metadata-change', 'data-change', 'unchanged', 'missing', 'error'}: raise KitError('扫描候选类别无效')
            original = _text_metadata(value['original_metadata']); current = _text_metadata(value.get('current_metadata'))
            row = connection.execute('SELECT * FROM data_management_candidates WHERE candidate_id=?', (value['candidate_id'],)).fetchone()
            if row is not None and row['status'] == 'publishing':
                continue
            # A previously observed, registered directory with identical stat
            # fingerprint can still need a review when only its Wiki snapshot
            # changed.  Do not create that task for an unknown old registry:
            # there is no trustworthy prior source revision to compare.
            if (not readme_mode and kind == 'unchanged' and row is not None and row['registered_dataset_id'] and
                    row['metadata_digest'] != value['metadata_digest']):
                kind = 'metadata-change'
            fingerprint = value.get('source_fingerprint'); source_json = _json(fingerprint) if fingerprint is not None else None
            changed = row is None or row['source_fingerprint_json'] != source_json or row['metadata_digest'] != value['metadata_digest']
            if row is not None and readme_mode and (row['status'] == 'withdrawn' or
                    (row['status'] == 'ignored' and row['kind'] == 'missing' and
                     row['registered_dataset_id'] is None and
                     str(row['error']).startswith('未找到有效 README 标记，已从待审核候选收起'))):
                # The boundary may return with identical bytes. It is a new
                # review revision, not a permanent manual ignore.
                changed = True
            if row is not None and not changed:
                # Older scans stored no explanation for a changed registered
                # tree. Refresh the diagnostic without changing its revision,
                # status, review or jobs when the actual source is unchanged.
                if kind == 'data-change' and row['kind'] == kind and row['error'] != value.get('error'):
                    connection.execute('UPDATE data_management_candidates SET error=?,updated_at=? WHERE candidate_id=?',
                                       (value.get('error'), now, value['candidate_id']))
                continue
            revision = 1 if row is None else row['revision'] + 1
            seed = _text_metadata(value.get('draft_metadata'))
            # Existing registered datasets are already usable.  A supplied
            # matching Chinese seed is the only reason to queue an initial
            # metadata-only review; otherwise record the baseline as published.
            if not readme_mode and kind == 'unchanged' and row is None and value.get('registered_dataset_id'):
                kind = 'metadata-change' if seed and seed != value.get('current_metadata', {}) else 'unchanged'
            status = 'published' if kind == 'unchanged' else ('pending' if kind in {'new','metadata-change','data-change'} else kind)
            old_current = {} if row is None else _read_json(row['current_metadata_json'], {})
            draft = seed or old_current or current
            if automatic_notes:
                draft = {key: value for key, value in draft.items() if key != 'tags'}
                draft.update(description=original.get('description', ''), tags=original.get('tags', []))
            dataset_id = self._dataset_id(value['relative_root'], kind, value.get('registered_dataset_id'), revision)
            if dataset_id is None and row is not None:
                dataset_id = row['dataset_id']
            if row is None:
                connection.execute('''INSERT INTO data_management_candidates(candidate_id,relative_root,revision,kind,status,source_fingerprint_json,metadata_digest,original_metadata_json,current_metadata_json,draft_metadata_json,seed_state,registered_dataset_id,dataset_id,readers_json,review_json,error,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (value['candidate_id'], value['relative_root'], revision, kind, status, source_json, value['metadata_digest'], _json(original), _json(current), _json(draft), value.get('seed_state','missing'), value.get('registered_dataset_id'), dataset_id, _json(value.get('registered_readers')) if value.get('registered_readers') is not None else None, None, value.get('error'), now, now))
            else:
                if changed:
                    connection.execute("UPDATE data_management_jobs SET status='cancelled', updated_at=?, error='来源已更新，审核失效' WHERE candidate_id=? AND status='queued' AND NOT EXISTS (SELECT 1 FROM data_management_access_requests a WHERE a.job_id=data_management_jobs.job_id)", (now, value['candidate_id']))
                old_review = _read_json(row['review_json'], {})
                previous_original = (old_review.get('previous_original_metadata') if isinstance(old_review, dict) else None) or _read_json(row['original_metadata_json'], {})
                review = {'source_metadata_changed': not automatic_notes and previous_original != original,
                          'previous_original_metadata': previous_original}
                connection.execute('''UPDATE data_management_candidates SET revision=?,kind=?,status=?,source_fingerprint_json=?,metadata_digest=?,original_metadata_json=?,current_metadata_json=?,draft_metadata_json=?,seed_state=?,registered_dataset_id=?,dataset_id=?,review_json=?,error=?,updated_at=? WHERE candidate_id=?''',
                    (revision, kind, status, source_json, value['metadata_digest'], _json(original), _json(current or old_current), _json(draft), value.get('seed_state','missing'), value.get('registered_dataset_id'), dataset_id, _json(review), value.get('error'), now, value['candidate_id']))
            self._audit(connection, value['candidate_id'], revision, 'scan', None, {'kind': kind, 'changed': changed}); count += 1
        if readme_mode:
            # A completed scan describes the current boundary set. Disappeared
            # *unregistered* candidates belong to history, not manual ignores.
            # Keep registered IDs and live publication leases completely intact.
            boundaries = {value['relative_root'] for value in items
                          if value.get('readme_managed') is True}
            old_rows = connection.execute('SELECT * FROM data_management_candidates WHERE registered_dataset_id IS NULL').fetchall()
            for old in old_rows:
                if old['candidate_id'] in seen or old['status'] == 'publishing':
                    continue
                parents = [path.as_posix() for path in Path(old['relative_root']).parents]
                covering = next((parent for parent in parents if parent in boundaries), None)
                message = (f'已由上层数据集目录 {covering} 覆盖，已自动收起；没有登记或删除数据。'
                           if covering else '该目录已不再是当前 README 划定的数据集边界，已自动收起；原文件保留。')
                if old['status'] == 'withdrawn' and old['error'] == message:
                    continue
                revision = old['revision'] + 1
                review = _read_json(old['review_json'], {})
                review['automatic_withdrawal'] = {'covered_by': covering,
                    'reason': 'covered-by-parent' if covering else 'boundary-no-longer-discovered'}
                connection.execute("UPDATE data_management_jobs SET status='cancelled',updated_at=?,error='数据集边界已更新，旧审核失效' WHERE candidate_id=? AND status='queued'", (now,old['candidate_id']))
                connection.execute("UPDATE data_management_candidates SET revision=?,kind='boundary-change',status='withdrawn',review_json=?,error=?,updated_at=? WHERE candidate_id=?", (revision,_json(review),message,now,old['candidate_id']))
                self._audit(connection,old['candidate_id'],revision,'auto-withdraw',None,{'covered_by':covering})
                count += 1
        return count

    def worker_start(self, instance_id: str) -> dict[str, object]:
        """Publisher calls after holding its own lifetime flock."""
        if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 128: raise KitError('发布器实例无效')
        config = self._configured(); now = time.time(); stamp = _now()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute("UPDATE data_management_jobs SET status='queued',lease_token=NULL,lease_expires_at=NULL,updated_at=?,error=coalesce(error,'发布器重启后重新排队') WHERE status='running'", (stamp,))
                connection.execute("UPDATE data_management_candidates SET status='queued',updated_at=? WHERE status='publishing'", (stamp,))
                connection.execute('''INSERT INTO data_management_worker_state(singleton,instance_id,started_at,heartbeat_at,heartbeat_epoch)
                    VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET instance_id=excluded.instance_id,
                    started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,heartbeat_epoch=excluded.heartbeat_epoch''',
                                   (instance_id, stamp, stamp, now))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'instance_id': instance_id, 'lease_seconds': int(config['seconds_per_dataset']) + 60, 'started_at': stamp}

    def worker_heartbeat(self, instance_id: str | None = None) -> dict[str, object]:
        config = self._configured()
        if instance_id is not None and (not isinstance(instance_id, str) or not instance_id): raise KitError('发布器实例无效')
        stamp, epoch = _now(), time.time()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT instance_id FROM data_management_worker_state WHERE singleton=1').fetchone()
                if row is None or (instance_id is not None and row['instance_id'] != instance_id):
                    raise KitError('发布器实例尚未启动或已被替换')
                instance_id = row['instance_id']
                connection.execute('UPDATE data_management_worker_state SET heartbeat_at=?,heartbeat_epoch=? WHERE singleton=1', (stamp, epoch))
                # One scan can include many bounded directory observations.
                # Keep its lease alive while the sole publisher is actually
                # running; service restart resets jobs after its lifetime lock.
                connection.execute("UPDATE data_management_jobs SET lease_expires_at=? WHERE status='running' AND lease_expires_at>=?",
                                   (epoch + int(config['seconds_per_dataset']) + 60, epoch))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'instance_id': instance_id, 'server_time': stamp}

    def worker_request_scan(self) -> dict[str, object]:
        """Timer-safe equivalent of a teacher request; always idempotent."""
        self._configured()
        system = next((item for item in getattr(self.policy, 'principals', {}).values()
                       if isinstance(item, Principal) and item.role == 'teacher' and item.username == 'ly'), None)
        if system is None:
            try:
                system = self.policy.principal(pwd.getpwnam('ly').pw_uid)
            except (AttributeError, KeyError, OSError) as exc:
                raise KitError('平台策略没有可信教师身份') from exc
        return self.request_scan(system)

    def worker_claim(self) -> dict[str, object] | None:
        config = self._configured(); now = time.time(); stamp = _now()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute("UPDATE data_management_jobs SET status='queued',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE status='running' AND lease_expires_at<?", (stamp, now))
                job = connection.execute("SELECT * FROM data_management_jobs WHERE status='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
                if job is None:
                    connection.execute('COMMIT'); return None
                token = uuid.uuid4().hex; expires = now + int(config['seconds_per_dataset']) + 60
                connection.execute("UPDATE data_management_jobs SET status='running',lease_token=?,lease_expires_at=?,attempts=attempts+1,updated_at=? WHERE job_id=?", (token, expires, stamp, job['job_id']))
                if job['kind'] == 'publish' and connection.execute('SELECT 1 FROM data_management_access_requests WHERE job_id=?', (job['job_id'],)).fetchone() is None:
                    connection.execute("UPDATE data_management_candidates SET status='publishing',updated_at=? WHERE candidate_id=? AND revision=?", (stamp, job['candidate_id'], job['candidate_revision']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        result = {'job_id': job['job_id'], 'kind': job['kind'], 'lease_token': token,
                  'lease_expires_at': expires, 'payload': _read_json(job['payload_json'], {})}
        if job['kind'] == 'publish': result['candidate'] = result['payload']
        return result

    def worker_finish(self, job_id: str, lease_token: str, result: object = None, error: str = '') -> dict[str, object]:
        if not isinstance(job_id, str) or not isinstance(lease_token, str) or not isinstance(error, str) or len(error) > 4096:
            raise KitError('发布任务回传无效')
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                job = connection.execute('SELECT * FROM data_management_jobs WHERE job_id=?', (job_id,)).fetchone()
                if job is None or job['status'] != 'running' or job['lease_token'] != lease_token or (job['lease_expires_at'] or 0) < time.time():
                    raise KitError('发布任务租约无效或已过期')
                now = _now()
                access_job = connection.execute('SELECT 1 FROM data_management_access_requests WHERE job_id=?', (job_id,)).fetchone() is not None
                payload = _read_json(job['payload_json'], {})
                if access_job:
                    if not error and (type(result) is not dict or result.get('dataset_id') != payload['dataset_id'] or result.get('readers') != payload['readers']):
                        raise KitError('授权修改回传与任务不一致')
                    connection.execute('UPDATE data_management_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',
                                       ('failed' if error else 'succeeded', error or None, now, job_id))
                    if not error:
                        connection.execute('UPDATE data_management_candidates SET readers_json=?,updated_at=? WHERE registered_dataset_id=?',
                                           (_json(result['readers']), now, payload['dataset_id']))
                    actor = Principal(payload['reviewer_uid'], payload['reviewer_username'], 'teacher')
                    self._audit(connection, job['candidate_id'], job['candidate_revision'], 'access-failed' if error else 'access-applied', actor,
                                {'job_id': job_id, 'dataset_id': payload['dataset_id'], 'before_readers': payload['expected_entry']['readers'],
                                 'readers': payload['readers'], 'error': error})
                    connection.execute('COMMIT')
                    return {'job_id': job_id, 'status': 'failed' if error else 'succeeded'}
                if error:
                    connection.execute("UPDATE data_management_jobs SET status='failed',error=?,updated_at=? WHERE job_id=?", (error, now, job_id))
                    if job['kind'] == 'publish':
                        connection.execute("UPDATE data_management_candidates SET status='failed',error=?,updated_at=? WHERE candidate_id=? AND revision=?", (error, now, job['candidate_id'], job['candidate_revision']))
                elif job['kind'] == 'scan':
                    items = result.get('items') if type(result) is dict else None
                    self._upsert_scan(connection, items)
                    connection.execute("UPDATE data_management_jobs SET status='succeeded',error=NULL,updated_at=? WHERE job_id=?", (now, job_id))
                else:
                    payload = _read_json(job['payload_json'], {})
                    row = connection.execute('SELECT * FROM data_management_candidates WHERE candidate_id=?', (job['candidate_id'],)).fetchone()
                    if row is None or row['revision'] != job['candidate_revision'] or row['status'] != 'publishing':
                        raise KitError('候选在发布时已改变')
                    if type(result) is not dict or result.get('dataset_id') != payload.get('dataset_id'):
                        raise KitError('发布器回传的数据集身份与审核任务不一致')
                    published_metadata = result.get('published_metadata', payload.get('metadata'))
                    published_metadata = _text_metadata(published_metadata)
                    connection.execute("UPDATE data_management_jobs SET status='succeeded',error=NULL,updated_at=? WHERE job_id=?", (now, job_id))
                    connection.execute("UPDATE data_management_candidates SET status='published',kind='unchanged',registered_dataset_id=?,current_metadata_json=?,error=NULL,updated_at=? WHERE candidate_id=?",
                                       (result['dataset_id'], _json(published_metadata), now, job['candidate_id']))
                    self._audit(connection, job['candidate_id'], job['candidate_revision'], 'published', None, result or {})
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction: connection.execute('ROLLBACK')
                raise
        return {'job_id': job_id, 'status': 'failed' if error else 'succeeded'}
