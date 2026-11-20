"""Reversible lifecycle state and conservative dependency reports for Web assets.

This module deliberately owns *only* visibility metadata.  In particular, a
``trash`` operation never unlinks a shared dataset, a published asset, or an
immutable submission.  That makes a mistaken cleanup recoverable and keeps
old fixed submissions executable while an administrator reviews the report.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, reference
from .web_asset_library import AssetLibrary
from .web_datasets import DatasetRegistry
from .web_dataset_trees import TreeRegistry
from .web_identity import Principal

_KINDS = {'algorithm', 'template', 'dataset'}
_STATES = {'active', 'retired', 'trashed'}
_ACTIONS = {'retire', 'restore', 'trash'}
_MAX_USAGE = 256 * 1024
_MAX_FILE = 512 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _teacher(principal: object) -> tuple[int, str]:
    if not isinstance(principal, Principal):
        raise KitError('只有教师 ly 可以管理资产生命周期')
    if (type(principal.uid) is not int or principal.uid < 1 or
            principal.username != 'ly' or principal.role != 'teacher'):
        raise KitError('只有教师 ly 可以管理资产生命周期')
    return principal.uid, principal.username


def _check_kind_ref(kind: object, ref: object) -> tuple[str, str]:
    if not isinstance(kind, str) or kind not in _KINDS or not isinstance(ref, str) or not ref:
        raise KitError('资产类型或引用无效')
    try:
        too_long = len(ref.encode('utf-8')) > 256
    except UnicodeEncodeError as exc:
        raise KitError('资产类型或引用无效') from exc
    if too_long:
        raise KitError('资产类型或引用无效')
    if kind in {'algorithm', 'template'}:
        try:
            reference(ref)
        except KitError as exc:
            raise KitError('组件引用必须是精确名称@版本') from exc
    elif any(part in {'', '.', '..'} for part in ref.split('/')):
        raise KitError('数据集 ID 无效')
    return str(kind), ref


def _safe_json(raw: object, label: str) -> object:
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > _MAX_USAGE:
        raise ValueError(label)
    return json.loads(raw)


def _yaml(data: object) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)) or len(data) > _MAX_FILE:
        raise ValueError('metadata too large')
    parsed = yaml.safe_load(bytes(data).decode('utf-8'))
    if not isinstance(parsed, dict):
        raise ValueError('metadata not mapping')
    return parsed


def _table(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)).fetchone() is not None


class AssetLifecycle:
    """A small additive lifecycle store, with fail-closed destructive checks."""

    def __init__(self, store, library_root: Path | str | None = None, policy=None):
        self.store = store
        self.library_root = Path(library_root) if library_root is not None else None
        self.policy = policy
        self._library: AssetLibrary | None = None
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('''CREATE TABLE IF NOT EXISTS web_asset_lifecycle (
                    kind TEXT NOT NULL CHECK(kind IN ('algorithm','template','dataset')),
                    ref TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','retired','trashed')),
                    generation INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    updated_by_uid INTEGER NOT NULL, updated_by_username TEXT NOT NULL,
                    PRIMARY KEY(kind,ref))''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_asset_lifecycle_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('retire','restore','trash')),
                    actor_uid INTEGER NOT NULL, actor_username TEXT NOT NULL, created_at TEXT NOT NULL)''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_project_usage (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id), owner_uid INTEGER NOT NULL,
                    owner_username TEXT NOT NULL, usage_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
                con.execute('CREATE INDEX IF NOT EXISTS web_asset_lifecycle_history_asset ON web_asset_lifecycle_history(kind,ref,id DESC)')
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    @staticmethod
    def _row(con, kind: str, ref: str):
        return con.execute('SELECT * FROM web_asset_lifecycle WHERE kind=? AND ref=?', (kind, ref)).fetchone()

    def _state(self, con, kind: str, ref: str) -> tuple[str, int]:
        row = self._row(con, kind, ref)
        return ('active', 0) if row is None else (str(row['state']), int(row['generation']))

    @staticmethod
    def _revision(kind: str, ref: str, state: str, generation: int) -> str:
        return hashlib.sha256(_json({'kind': kind, 'ref': ref, 'state': state, 'generation': generation}).encode()).hexdigest()

    def summary(self, kind: object, ref: object) -> dict[str, object]:
        """Return lightweight state without touching source registries."""
        kind, ref = _check_kind_ref(kind, ref)
        with self.store._connection() as con:
            state, generation = self._state(con, kind, ref)
        return {'state': state, 'revision': self._revision(kind, ref, state, generation)}

    def _verify_exists(self, con, kind: str, ref: str) -> None:
        if kind == 'dataset':
            policy = self.policy
            seen: set[str] = set()
            tree = getattr(policy, 'dataset_tree_registry', None) if policy is not None else None
            legacy = getattr(policy, 'dataset_registry', None) if policy is not None else None
            try:
                seen.update(TreeRegistry(tree)._load())
                seen.update(DatasetRegistry(legacy)._load())
            except KitError:
                raise
            derived_public = (_table(con, 'web_derived_datasets') and con.execute(
                "SELECT 1 FROM web_derived_datasets WHERE dataset_id=? AND visibility='public'", (ref,)
            ).fetchone() is not None)
            if ref not in seen and not derived_public:
                raise KitError('已登记数据集不存在')
            return
        # Published Web assets can be verified without trusting a filesystem
        # tree; installed assets require the configured immutable library root.
        published = _table(con, 'web_asset_published') and con.execute(
            'SELECT 1 FROM web_asset_published WHERE kind=? AND ref=?', (kind, ref)).fetchone() is not None
        if published:
            return
        if self.library_root is None:
            raise KitError('组件库未配置，无法确认组件存在')
        try:
            catalog = self._prepare_library().catalog()
        except (KitError, OSError) as exc:
            raise KitError('组件库不可读取，无法执行生命周期操作') from exc
        key = 'plugins' if kind == 'algorithm' else 'templates'
        if any(row.get('ref') == ref for row in catalog[key]):
            return
        raise KitError('组件不存在或已不是该精确版本')

    def _prepare_library(self) -> AssetLibrary | None:
        """Construct the composite library before a report/mutation transaction.

        ``AssetLibrary`` initializes additive publication tables.  Keeping that
        initialization outside the lifecycle write transaction prevents SQLite
        self-locking, and it also ensures raw-dataset reports see installed and
        Web-published template dependencies.
        """
        if self.library_root is None:
            return None
        if self._library is None:
            self._library = AssetLibrary(self.store, self.library_root)
        return self._library

    @staticmethod
    def _entry(kind: str, label: str, row: Any, *, detail: str = '') -> dict[str, object]:
        result: dict[str, object] = {'type': kind, 'label': label}
        for key in ('owner_username', 'project_id', 'submission_id'):
            try:
                value = row[key]
            except (KeyError, IndexError):
                value = None
            if value is not None:
                result[key] = str(value)
        if detail:
            result['detail'] = detail
        return result

    @staticmethod
    def _refs_from_manifest(raw: object) -> list[str]:
        try:
            doc = _safe_json(raw, 'manifest')
            refs = doc['data_references']['references']
            if type(refs) is not list:
                raise ValueError('references')
            result = []
            for item in refs:
                if type(item) is not dict or not isinstance(item.get('dataset_id'), str):
                    raise ValueError('reference')
                result.append(item['dataset_id'])
            return result
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise ValueError('固定提交数据引用记录损坏') from exc

    @staticmethod
    def _plugins_from_files(rows: list[Any]) -> set[str]:
        files = {str(row['path']): bytes(row['content']) for row in rows if str(row['path']) in {'project.yaml'} or str(row['path']).endswith('/plugin.yaml')}
        project = _yaml(files.get('project.yaml', b''))
        plugins = project.get('plugins')
        if not isinstance(plugins, dict):
            raise ValueError('plugins')
        result: set[str] = set()
        for alias, spec in plugins.items():
            if not isinstance(alias, str) or not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
                raise ValueError('plugin specification')
            path = spec['path'].strip('/') + '/plugin.yaml'
            meta = _yaml(files.get(path, b''))
            name, version = meta.get('name'), meta.get('version')
            if not isinstance(name, str) or not isinstance(version, str):
                raise ValueError('plugin identity')
            exact = f'{name}@{version}'
            reference(exact)
            result.add(exact)
        return result

    def _submission_refs(self, con, kind: str, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        if not (_table(con, 'submissions') and _table(con, 'projects')):
            return refs, ['提交记录表尚不存在，未发现可核对的固定提交。']
        rows = con.execute('''SELECT submissions.id AS submission_id, submissions.project_id, submissions.manifest_json,
                              projects.owner_username, projects.name AS project_name FROM submissions JOIN projects ON projects.id=submissions.project_id
                              ORDER BY submissions.created_at DESC, submissions.id DESC''').fetchall()
        for row in rows:
            # Failed/abandoned submission preparations and historical asset
            # validation snapshots are service-only records.  Their file rows
            # may deliberately have been removed while retaining job logs, so
            # they are neither an incoming user reference nor a reason to
            # block cleanup of an unrelated published asset.
            if self.store._internal_submission(con, str(row['submission_id'])):
                if kind=='dataset' and _table(con,'web_jobs') and con.execute(
                        "SELECT 1 FROM web_jobs WHERE submission_id=? AND status IN ('queued','running') LIMIT 1",
                        (row['submission_id'],)).fetchone():
                    try:
                        if ref in self._refs_from_manifest(row['manifest_json']):
                            refs.append(self._entry('active-execution','提交前验证或内部验证仍在使用该数据',row))
                    except (ValueError,KitError) as exc:
                        notes.append(f"运行中的验证 {row['submission_id']} 的数据引用无法核对：{exc}")
                continue
            try:
                hit = ref in self._refs_from_manifest(row['manifest_json']) if kind == 'dataset' else False
                if kind == 'algorithm':
                    files = con.execute('SELECT path,content FROM submission_files WHERE submission_id=? AND (path=? OR path LIKE ?)',
                                        (row['submission_id'], 'project.yaml', '%/plugin.yaml')).fetchall()
                    hit = ref in self._plugins_from_files(files)
                if hit:
                    refs.append(self._entry('fixed-submission', str(row['project_name']) + ' · 固定提交引用', row))
            except (ValueError, KitError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
                # A damaged formal record must never make a trash decision look safe.
                notes.append(f"固定提交 {row['submission_id']} 的元数据无法完整核对：{exc}")
        return refs, notes

    def _task_refs(self, con, kind: str, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        """Report exact delivered task materials without inferring broad ACLs.

        A material grant must name the requested exact component/data ID, be
        unrevoked, and belong to a delivered recipient (a project distribution
        or an atomically recorded material-only task). Revoked grants remain visible as history only:
        their copied bytes are deliberately preserved and must never be
        mistaken for a live authorization or deleted as part of a platform
        asset cleanup.
        """
        required = ('web_task_grants', 'web_task_materials', 'web_task_deliveries', 'web_task_recipients')
        if not all(_table(con, table) for table in required):
            return [], []
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        rows = con.execute('''SELECT grants.id AS grant_id, grants.revoked_at, grants.grant_kind,
                grants.resource_ref, materials.kind AS material_kind, materials.ref AS material_ref,
                materials.metadata_json, materials.supplement_id, tasks.id AS task_id,
                tasks.name AS task_name, recipients.target_uid, recipients.target_username,
                recipients.distribution_id, tasks.delivery_mode
            FROM web_task_grants grants
            JOIN web_task_materials materials ON materials.id=grants.material_id
            JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
            JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
              AND recipients.target_uid=grants.target_uid
              AND recipients.target_username=grants.target_username
            WHERE grants.grant_kind=? AND grants.resource_ref=? AND materials.kind=?
            ORDER BY tasks.created_at DESC, grants.id DESC''', (kind, ref, kind)).fetchall()
        for row in rows:
            try:
                if row['material_ref'] != ref:
                    raise ValueError('material ref')
                if kind == 'dataset':
                    metadata = _safe_json(row['metadata_json'], 'task dataset metadata')
                    if (not isinstance(metadata, dict) or not isinstance(metadata.get('reference'), dict) or
                            metadata['reference'].get('dataset_id') != ref):
                        raise ValueError('dataset reference')
                if row['delivery_mode'] != 'resources' and row['distribution_id'] is None:
                    # An interrupted project task was not delivered and has
                    # no user-visible resource to block a cleanup.
                    continue
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
                notes.append(f"任务材料授权 {row['grant_id']} 无法完整核对：{exc}")
                continue
            item = {
                'type': 'task-material' if row['revoked_at'] is None else 'task-material-history',
                'label': '任务材料仍授权此资源' if row['revoked_at'] is None else '已撤销任务材料的保留历史',
                'task_id': str(row['task_id']), 'task_name': str(row['task_name']),
                'target_username': str(row['target_username']),
                'supplement_id': str(row['supplement_id']) if row['supplement_id'] is not None else '',
                'kind': kind, 'ref': ref,
                'detail': ('补充资源 ' + str(row['supplement_id'])) if row['supplement_id'] is not None else '任务原始材料',
            }
            refs.append(item)
        return refs, notes

    def _candidate_refs(self, con, kind: str, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        if not _table(con, 'web_asset_candidates'):
            return [], []
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        rows = con.execute('''SELECT candidate_id,kind,ref,status,submission_id,data_references_json FROM web_asset_candidates
                              ORDER BY created_at DESC,candidate_id DESC''').fetchall()
        for row in rows:
            try:
                if kind == 'dataset':
                    parsed = _safe_json(row['data_references_json'], 'candidate references')
                    if not isinstance(parsed, list) or any(not isinstance(x, dict) or not isinstance(x.get('dataset_id'), str) for x in parsed):
                        raise ValueError('candidate references')
                    hit = any(x.get('dataset_id') == ref for x in parsed)
                elif kind == 'algorithm' and row['kind'] == 'template':
                    files = con.execute('SELECT path,content FROM web_asset_candidate_files WHERE candidate_id=? AND (path=? OR path LIKE ?)',
                                        (row['candidate_id'], 'project.yaml', '%/plugin.yaml')).fetchall()
                    hit = ref in self._plugins_from_files(files)
                else:
                    hit = False
                if hit:
                    refs.append({'type': 'asset-candidate', 'label': '资产候选正在引用此资产',
                                 'submission_id': str(row['submission_id']), 'kind': row['kind'], 'ref': row['ref'], 'candidate_id': row['candidate_id'], 'detail': f"{row['kind']} {row['ref']}（{row['status']}）"})
            except (ValueError, KitError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
                notes.append(f"资产候选 {row['candidate_id']} 无法完整核对：{exc}")
        return refs, notes

    def _installed_refs(self, kind: str, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        """Inspect trusted installed template metadata without reading a user workspace."""
        if self._library is None:
            return [], []
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        try:
            templates = self._library.catalog()['templates']
        except (KitError, OSError) as exc:
            return [], [f'平台案例库无法完整核对：{exc}']
        for row in templates:
            template_ref = row.get('ref')
            if not isinstance(template_ref, str):
                notes.append('平台案例库含无效案例引用')
                continue
            try:
                _meta, files, _digest = self._library._template_source(template_ref)
                if kind == 'algorithm':
                    hit = ref in self._plugins_from_files([
                        {'path': path, 'content': content} for path, content in files.items()
                    ])
                else:
                    raw = files.get('DATA-REFERENCES.json', b'[]')
                    parsed = json.loads(raw.decode('utf-8'))
                    if not isinstance(parsed, list) or any(not isinstance(x, dict) or not isinstance(x.get('dataset_id'), str) for x in parsed):
                        raise ValueError('template references')
                    hit = any(x['dataset_id'] == ref for x in parsed)
                if hit:
                    refs.append({'type': 'platform-template', 'label': '平台案例模板正在引用此资产',
                                 'detail': template_ref, 'kind': 'template', 'ref': template_ref})
            except (ValueError, KitError, json.JSONDecodeError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
                notes.append(f'平台案例模板 {template_ref} 无法完整核对：{exc}')
        return refs, notes

    def _template_refs(self, con, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        name, version = ref.rsplit('@', 1)
        # Administrator-installed legacy templates have no immutable Web
        # publication provenance.  Do not infer that history from a matching
        # name: keep a visible limitation instead of claiming a safe purge.
        if self._library is not None:
            try:
                document, _files, _digest = self._library._template_source(ref)
                origin = document.get('origin')
                tracked = (isinstance(origin, dict) and
                           (isinstance(origin.get('template_ref'), str) or
                            (origin.get('kind') == 'web-asset-source/v1' and isinstance(origin.get('submission_id'), str))))
                builtin = (_table(con, 'web_central_builtin_assets') and con.execute(
                    "SELECT 1 FROM web_central_builtin_assets WHERE kind='template' AND ref=?", (ref,)).fetchone() is not None)
                if not tracked and not builtin:
                    notes.append('该平台案例模板没有可追溯的 Web 模板来源；系统不会按名称猜测其历史引用。')
            except (KitError, OSError, ValueError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
                notes.append(f'平台案例模板 {ref} 无法读取来源信息：{exc}')
        if not (_table(con, 'submissions') and _table(con, 'submission_files')):
            return refs, notes
        source_id = None
        if _table(con, 'web_asset_published') and _table(con, 'web_asset_candidates'):
            source = con.execute('''SELECT c.submission_id FROM web_asset_published p JOIN web_asset_candidates c
                                    ON c.candidate_id=p.candidate_id WHERE p.kind='template' AND p.ref=?''', (ref,)).fetchone()
            source_id = None if source is None else str(source['submission_id'])
        # New Web-created projects retain an exact template origin.  Older
        # source-submission origins are followed only when their immutable ID
        # exactly matches a published template source; names are never guessed.
        rows = con.execute('''SELECT submissions.id AS submission_id,submissions.project_id,projects.owner_username,projects.name AS project_name
                              FROM submissions JOIN projects ON projects.id=submissions.project_id
                              ORDER BY submissions.created_at DESC''').fetchall()
        for row in rows:
            if self.store._internal_submission(con, str(row['submission_id'])):
                continue
            if source_id is not None and str(row['submission_id']) == source_id:
                # The source is outgoing provenance of the template, not an
                # inbound use which should prevent a recoverable cleanup.
                continue
            data = con.execute('SELECT content FROM submission_files WHERE submission_id=? AND path=?',
                               (row['submission_id'], 'project.yaml')).fetchone()
            if data is None:
                notes.append(f"固定提交 {row['submission_id']} 缺少 project.yaml")
                continue
            try:
                origin = _yaml(data['content']).get('origin', {})
                legacy_case = isinstance(origin, dict) and isinstance(origin.get('case'), str) and origin.get('case') in {ref, name}
                legacy_version = isinstance(origin, dict) and origin.get('version') == version
                nested = origin.get('source_origin') if isinstance(origin, dict) else None
                source_match = (source_id is not None and isinstance(origin, dict) and
                                ((origin.get('kind') == 'web-asset-source/v1' and origin.get('submission_id') == source_id) or
                                 (isinstance(nested, dict) and nested.get('submission_id') == source_id)))
                if (isinstance(origin, dict) and
                        (origin.get('template_ref') == ref or origin.get('case') == ref or
                         (legacy_case and legacy_version) or source_match)):
                    refs.append(self._entry('template-instance', str(row['project_name']) + ' · 源自该案例模板', row))
            except (ValueError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
                notes.append(f"固定提交 {row['submission_id']} 的模板来源无法核对：{exc}")
        # Published template itself has an immutable source submission.  The
        # distribution/baseline/package records are material operational links.
        if source_id is not None:
            sid = source_id
            for table, label, query in (
                ('distributions', '分发任务以该模板来源提交为基线', 'SELECT id,project_id,target_username FROM distributions WHERE source_submission_id=?'),
                ('package_bundles', '导出包以该模板来源提交为基线', 'SELECT bundle_id FROM package_bundles WHERE source_submission_id=?'),
            ):
                if _table(con, table):
                    for item in con.execute(query, (sid,)).fetchall():
                        refs.append({'type': table[:-1], 'label': label, 'submission_id': str(sid),
                                     'project_id': str(item['project_id']) if 'project_id' in item.keys() and item['project_id'] else '',
                                     'owner_username': str(item['target_username']) if 'target_username' in item.keys() else '',
                                     'detail': str(item[0])})
            if _table(con, 'project_trusted_sources'):
                for item in con.execute('SELECT project_id,distribution_id FROM project_trusted_sources WHERE baseline_submission_id=?', (sid,)).fetchall():
                    refs.append({'type': 'trusted-baseline', 'label': '项目仍信任该模板来源提交作为基线',
                                 'submission_id': str(sid), 'project_id': str(item['project_id']),
                                 'detail': str(item['distribution_id'])})
            if _table(con, 'project_package_sources'):
                for item in con.execute('SELECT project_id,bundle_id FROM project_package_sources WHERE baseline_submission_id=?', (sid,)).fetchall():
                    refs.append({'type': 'package-baseline', 'label': '项目仍信任该模板来源提交的导入包基线',
                                 'submission_id': str(sid), 'project_id': str(item['project_id']),
                                 'detail': str(item['bundle_id'])})
        return refs, notes

    def _derived_refs(self, con, dataset_id: str) -> tuple[list[dict[str, object]], list[str]]:
        if not _table(con, 'web_derived_datasets'):
            return [], []
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        for row in con.execute('SELECT dataset_id,owner_username,source_submission_id,source_refs_json FROM web_derived_datasets').fetchall():
            try:
                values = _safe_json(row['source_refs_json'], 'derived source references')
                if not isinstance(values, list) or any(not isinstance(x, dict) or not isinstance(x.get('dataset_id'), str) for x in values):
                    raise ValueError('derived source references')
                if any(x['dataset_id'] == dataset_id for x in values):
                    refs.append({'type': 'derived-dataset', 'label': '派生数据记录了该数据集为来源',
                                 'owner_username': str(row['owner_username']), 'submission_id': str(row['source_submission_id'] or ''),
                                 'detail': str(row['dataset_id']), 'kind': 'dataset', 'ref': str(row['dataset_id'])})
            except (ValueError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
                notes.append(f"派生数据 {row['dataset_id']} 的来源记录无法完整核对：{exc}")
        return refs, notes

    def _usage_refs(self, con, kind: str, ref: str) -> tuple[list[dict[str, object]], list[str]]:
        if not _table(con, 'web_project_usage'):
            return [], []
        refs: list[dict[str, object]] = []
        notes: list[str] = []
        for row in con.execute('SELECT project_id,owner_username,usage_json FROM web_project_usage').fetchall():
            try:
                value = _safe_json(row['usage_json'], 'project usage')
                key = {'dataset': 'dataset_ids', 'algorithm': 'algorithm_refs', 'template': 'template_refs'}[kind]
                if not isinstance(value, dict) or not isinstance(value.get(key), list):
                    raise ValueError('project usage')
                if ref in value[key]:
                    refs.append({'type': 'web-project-summary', 'label': '当前 Web 项目摘要正在引用此资产',
                                 'owner_username': str(row['owner_username']), 'project_id': str(row['project_id'])})
            except (ValueError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
                notes.append(f"项目 {row['project_id']} 的 Web 使用摘要无法完整核对：{exc}")
        return refs, notes

    def _history(self, con, kind: str, ref: str) -> list[dict[str, object]]:
        return [{'action': row['action'], 'actor_username': row['actor_username'], 'created_at': row['created_at']}
                for row in con.execute('''SELECT action,actor_username,created_at FROM web_asset_lifecycle_history
                                           WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 10''', (kind, ref)).fetchall()]

    def _report(self, con, kind: str, ref: str, offset: int, limit: int) -> dict[str, object]:
        state, generation = self._state(con, kind, ref)
        references: list[dict[str, object]] = []
        notes = ['引用清单覆盖固定提交、平台候选、分发/基线和已登记的 Web 项目摘要；未提交的 IDE 修改及平台外程序无法由服务器保证覆盖。回收站只隐藏新选择，保留原始文件和历史记录。']
        for getter in (self._submission_refs, self._candidate_refs, self._task_refs, self._usage_refs):
            found, warnings = getter(con, kind, ref)
            references.extend(found); notes.extend(warnings)
        if kind in {'algorithm', 'dataset'}:
            found, warnings = self._installed_refs(kind, ref)
            references.extend(found); notes.extend(warnings)
        if kind == 'template':
            found, warnings = self._template_refs(con, ref); references.extend(found); notes.extend(warnings)
        if kind == 'dataset':
            found, warnings = self._derived_refs(con, ref); references.extend(found); notes.extend(warnings)
        # Stable de-duplication makes pagination deterministic even when the
        # same immutable submission is found through several preserved links.
        unique: dict[str, dict[str, object]] = {}
        for item in references:
            key = _json(item)
            unique.setdefault(key, item)
        all_refs = sorted(unique.values(), key=lambda x: _json(x))
        return {'kind': kind, 'ref': ref, 'state': state, 'revision': self._revision(kind, ref, state, generation),
                'reference_count': len(all_refs), 'references': all_refs[offset:offset + limit],
                'notes': notes, 'can_trash': len(notes) == 1,
                'history': self._history(con, kind, ref), 'offset': offset, 'limit': limit}

    def report(self, principal: object, kind: object, ref: object, offset: int = 0, limit: int = 50) -> dict[str, object]:
        _teacher(principal)
        kind, ref = _check_kind_ref(kind, ref)
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
            raise KitError('引用清单分页参数无效')
        self._prepare_library()
        with self.store._connection() as con:
            self._verify_exists(con, kind, ref)
            return self._report(con, kind, ref, offset, limit)

    def change(self, principal: object, kind: object, ref: object, action: object, expected_revision: object) -> dict[str, object]:
        uid, username = _teacher(principal)
        kind, ref = _check_kind_ref(kind, ref)
        if not isinstance(action, str) or action not in _ACTIONS or not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise KitError('生命周期操作或版本摘要无效')
        # AssetLibrary/AssetService may initialize their own additive tables.
        # Do that before this transaction: SQLite cannot upgrade two separate
        # write transactions on the same database without a self-deadlock.
        self._prepare_library()
        with self.store._connection() as verified:
            self._verify_exists(verified, kind, ref)
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                state, generation = self._state(con, kind, ref)
                if expected_revision != self._revision(kind, ref, state, generation):
                    raise KitError('资产状态冲突，请先重新查看引用清单')
                provisional = self._report(con, kind, ref, 0, 100)
                from .web_cleanup import require_available
                require_available(con, kind, ref)
                if action == 'trash' and not provisional['can_trash']:
                    raise KitError('存在无法核对的记录，不能移入回收站')
                next_state = 'active' if action == 'restore' else ('retired' if action == 'retire' else 'trashed')
                generation += 1
                con.execute('''INSERT INTO web_asset_lifecycle(kind,ref,state,generation,updated_at,updated_by_uid,updated_by_username)
                               VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,ref) DO UPDATE SET state=excluded.state,
                               generation=excluded.generation,updated_at=excluded.updated_at,updated_by_uid=excluded.updated_by_uid,
                               updated_by_username=excluded.updated_by_username''',
                            (kind, ref, next_state, generation, _now(), uid, username))
                con.execute('INSERT INTO web_asset_lifecycle_history(kind,ref,action,actor_uid,actor_username,created_at) VALUES (?,?,?,?,?,?)',
                            (kind, ref, action, uid, username, _now()))
                result = self._report(con, kind, ref, 0, 50)
                con.execute('COMMIT')
                return result
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    @staticmethod
    def extract_usage(files: dict[str, bytes], references: object = None) -> dict[str, list[str]]:
        """Parse only immutable in-memory metadata; never open a workspace."""
        try:
            doc = _yaml(files.get('project.yaml', b''))
            plugins = doc.get('plugins', {})
            if not isinstance(plugins, dict):
                raise ValueError('plugins')
            template_refs = [doc['origin']['template_ref']] if isinstance(doc.get('origin'), dict) and isinstance(doc['origin'].get('template_ref'), str) else []
            algorithm_refs: list[str] = []
            for spec in plugins.values():
                if not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
                    raise ValueError('plugin path')
                meta = _yaml(files.get(spec['path'].strip('/') + '/plugin.yaml', b''))
                ref = f"{meta.get('name')}@{meta.get('version')}"; reference(ref); algorithm_refs.append(ref)
            raw = references if references is not None else []
            if not isinstance(raw, list):
                raise ValueError('references')
            ids = []
            for item in raw:
                if not isinstance(item, dict) or not isinstance(item.get('dataset_id'), str):
                    raise ValueError('dataset reference')
                ids.append(item['dataset_id'])
            return {'dataset_ids': sorted(set(ids)), 'algorithm_refs': sorted(set(algorithm_refs)), 'template_refs': sorted(set(template_refs))}
        except (ValueError, KitError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
            raise KitError('项目使用摘要元数据无效') from exc

    def record_usage(self, principal: object, project_id: object, usage: object) -> dict[str, object]:
        if not isinstance(principal, Principal) or type(principal.uid) is not int or principal.uid < 1:
            raise KitError('项目使用摘要需要可信身份')
        if not isinstance(project_id, str) or not project_id or len(project_id) > 128 or not isinstance(usage, dict):
            raise KitError('项目使用摘要无效')
        keys = {'dataset_ids', 'algorithm_refs', 'template_refs'}
        if set(usage) != keys or any(type(usage[key]) is not list or len(usage[key]) > 4096 or any(not isinstance(x, str) or len(x) > 256 for x in usage[key]) for key in keys):
            raise KitError('项目使用摘要无效')
        canonical = {key: sorted(set(usage[key])) for key in keys}
        try:
            for value in canonical['algorithm_refs']:
                _check_kind_ref('algorithm', value)
            for value in canonical['template_refs']:
                _check_kind_ref('template', value)
            for value in canonical['dataset_ids']:
                _check_kind_ref('dataset', value)
            if len(_json(canonical).encode('utf-8')) > _MAX_USAGE:
                raise ValueError('too large')
        except (UnicodeEncodeError, ValueError, KitError) as exc:
            raise KitError('项目使用摘要无效') from exc
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                from .web_cleanup import require_available
                for dataset_id in canonical['dataset_ids']:
                    require_available(con, 'dataset', dataset_id)
                row = con.execute('SELECT owner_uid,owner_username FROM projects WHERE id=?', (project_id,)).fetchone()
                if row is None or row['owner_uid'] != principal.uid or row['owner_username'] != principal.username:
                    raise KitError('只能登记自己的项目使用摘要')
                con.execute('''INSERT INTO web_project_usage(project_id,owner_uid,owner_username,usage_json,updated_at) VALUES (?,?,?,?,?)
                               ON CONFLICT(project_id) DO UPDATE SET usage_json=excluded.usage_json,updated_at=excluded.updated_at''',
                            (project_id, principal.uid, principal.username, _json(canonical), _now()))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        return {'project_id': project_id, **canonical}


def is_active(store, kind: object, ref: object) -> bool:
    return AssetLifecycle(store).summary(kind, ref)['state'] == 'active'


def states(store, kind: object | None = None) -> dict[str, object]:
    if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
        raise KitError('资产类型无效')
    lifecycle = AssetLifecycle(store)
    with store._connection() as con:
        where, values = (' WHERE kind=?', (kind,)) if kind is not None else ('', ())
        rows = con.execute('SELECT kind,ref,state,generation FROM web_asset_lifecycle' + where, values).fetchall()
    if kind is not None:
        return {str(row['ref']): str(row['state']) for row in rows}
    return {f"{row['kind']}:{row['ref']}": {'state': row['state'], 'revision': lifecycle._revision(row['kind'], row['ref'], row['state'], row['generation'])} for row in rows}
