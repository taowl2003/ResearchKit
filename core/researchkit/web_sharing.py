"""Authorization for static, administrator-installed Web components.

The broker supplies a trusted :class:`Principal`; this module never accepts a
workspace path or loads a user's library.  Component files are read only from
``SharedLibrary`` and are returned as bytes for the caller to copy.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, reference
from .web_identity import Principal
from .web_asset_library import AssetLibrary
from .web_library import _TYPES
from .web_store import SubmissionStore


_KINDS = {'algorithm', 'template'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_DEFAULT_PUBLIC = {
    ('algorithm', 'example.scale@1.0.0'),
    ('template', 'hello@1.0.0'),
}


def _principal(value: object) -> tuple[int, str, str]:
    """Accept only the broker's immutable identity value, never a dict."""
    if not isinstance(value, Principal):
        raise KitError('共享组件需要经 broker 验证的 Principal')
    uid, username, role = value.uid, value.username, value.role
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise KitError('可信身份缺少有效 UID')
    if (not isinstance(username, str) or not username or len(username) > 32 or
            any(ord(char) < 32 or ord(char) == 127 for char in username)):
        raise KitError('可信身份缺少用户名')
    if role not in {'teacher', 'student'} or (role == 'teacher') != (username == 'ly'):
        raise KitError('可信身份角色无效')
    return uid, username, role


def _policy_revision(kind: str, ref: str, public: bool,
                     targets: list[dict[str, object]]) -> str:
    value = json.dumps({'kind': kind, 'ref': ref, 'public': public, 'targets': targets},
                       ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


class ComponentService:
    """Persist exact component grants alongside a ``SubmissionStore`` database."""

    def __init__(self, store: SubmissionStore, library_root: Path | str):
        if not isinstance(store, SubmissionStore):
            raise KitError('共享组件服务需要 SubmissionStore')
        self.store = store
        self.library = AssetLibrary(store, library_root)
        self._initialize()

    def _initialize(self) -> None:
        """Additive, lazy migration which preserves a prior revocation."""
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS component_policies (
                    kind TEXT NOT NULL CHECK (kind IN ('algorithm', 'template')),
                    ref TEXT NOT NULL, public INTEGER NOT NULL CHECK (public IN (0, 1)),
                    PRIMARY KEY(kind, ref)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS grants (
                    kind TEXT NOT NULL, ref TEXT NOT NULL,
                    target_uid INTEGER NOT NULL, target_username TEXT NOT NULL,
                    PRIMARY KEY(kind, ref, target_uid, target_username),
                    FOREIGN KEY(kind, ref) REFERENCES component_policies(kind, ref)
                        ON DELETE CASCADE
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_case_cascaded_grants (
                    template_ref TEXT NOT NULL,
                    resource_kind TEXT NOT NULL CHECK(resource_kind IN ('algorithm','dataset')),
                    resource_ref TEXT NOT NULL,
                    reference_json TEXT NOT NULL DEFAULT '{}',
                    public INTEGER NOT NULL DEFAULT 0,
                    target_uid INTEGER NOT NULL DEFAULT 0,
                    target_username TEXT NOT NULL DEFAULT '',
                    grantor_uid INTEGER NOT NULL,
                    grantor_username TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_case_cascaded_lookup
                    ON web_case_cascaded_grants(resource_kind, resource_ref, target_uid, target_username)''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_dataset_policies (
                    dataset_id TEXT PRIMARY KEY,
                    public INTEGER NOT NULL DEFAULT 1 CHECK(public IN (0, 1)),
                    updated_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_dataset_grants (
                    dataset_id TEXT NOT NULL,
                    target_uid INTEGER NOT NULL,
                    target_username TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, target_uid, target_username)
                )''')
                for kind, ref in sorted(_DEFAULT_PUBLIC):
                    # INSERT OR IGNORE is deliberate: a false policy survives
                    # a broker restart and a legacy database migration.
                    connection.execute(
                        'INSERT OR IGNORE INTO component_policies(kind, ref, public) VALUES (?, ?, 1)',
                        (kind, ref))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _component(kind: object, ref: object) -> tuple[str, str]:
        if not isinstance(kind, str) or kind not in _KINDS:
            raise KitError('共享组件类型必须是 algorithm 或 template')
        if not isinstance(ref, str):
            raise KitError('共享组件引用必须是字符串')
        try:
            reference(ref)
        except KitError as exc:
            raise KitError('共享组件引用必须是名称@x.y.z') from exc
        return kind, ref

    @staticmethod
    def _teacher(principal: object) -> tuple[int, str, str]:
        identity = _principal(principal)
        if identity[2] != 'teacher':
            raise KitError('只有教师可以管理共享组件授权')
        return identity

    @staticmethod
    def _targets(value: object) -> list[dict[str, object]]:
        if type(value) is not list:
            raise KitError('授权目标必须是 Principal 列表')
        rows: list[dict[str, object]] = []
        seen_uids: set[int] = set()
        seen_names: set[str] = set()
        for target in value:
            uid, username, _role = _principal(target)
            if uid in seen_uids or username in seen_names:
                raise KitError('授权目标不能重复 UID 或用户名')
            seen_uids.add(uid)
            seen_names.add(username)
            rows.append({'uid': uid, 'username': username})
        return sorted(rows, key=lambda row: (int(row['uid']), str(row['username'])))

    @staticmethod
    def _row(kind: str, ref: str, public: bool,
             targets: list[dict[str, object]]) -> dict[str, object]:
        return {'kind': kind, 'ref': ref, 'public': public, 'targets': targets,
                'revision': _policy_revision(kind, ref, public, targets)}

    @staticmethod
    def _db_row(connection: Any, kind: str, ref: str) -> tuple[bool, list[dict[str, object]]]:
        policy = connection.execute(
            'SELECT public FROM component_policies WHERE kind = ? AND ref = ?', (kind, ref)).fetchone()
        public = bool(policy['public']) if policy is not None else (kind, ref) in _DEFAULT_PUBLIC
        grants = connection.execute(
            '''SELECT target_uid, target_username FROM grants
               WHERE kind = ? AND ref = ? ORDER BY target_uid, target_username''', (kind, ref)).fetchall()
        return public, [{'uid': row['target_uid'], 'username': row['target_username']} for row in grants]

    def _allowed(self, principal: object, kind: str, ref: str) -> bool:
        uid, username, role = _principal(principal)
        if role == 'teacher':
            return True
        with self.store._connection() as connection:
            policy = connection.execute(
                'SELECT public FROM component_policies WHERE kind = ? AND ref = ?', (kind, ref)).fetchone()
            if policy is not None and bool(policy['public']):
                return True
            grant = connection.execute(
                '''SELECT 1 FROM grants WHERE kind = ? AND ref = ?
                   AND target_uid = ? AND target_username = ?''',
                (kind, ref, uid, username)).fetchone()
            if grant is not None:
                return True
            try:
                cascaded = connection.execute('''SELECT 1 FROM web_case_cascaded_grants
                    WHERE resource_kind = ? AND resource_ref = ?
                      AND (public = 1 OR (target_uid = ? AND target_username = ?)) LIMIT 1''',
                    (kind, ref, uid, username)).fetchone()
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                cascaded = None
            if cascaded is not None:
                return True
            # Task material is an exact, revocable relationship.  Project
            # tasks need a dispatched recipient; material-only tasks are
            # deliberately active as soon as their atomic grant is recorded.
            # Older databases do not have task tables; retain the established
            # public/manual policy rather than attempting a lazy migration in
            # this read-only authorization path.
            try:
                task_grant = connection.execute('''SELECT 1 FROM web_task_grants task_grants
                    JOIN web_task_materials materials ON materials.id=task_grants.material_id
                    JOIN web_task_deliveries tasks ON tasks.id=task_grants.task_id
                    JOIN web_task_recipients recipients ON recipients.task_id=task_grants.task_id
                      AND recipients.target_uid=task_grants.target_uid
                      AND recipients.target_username=task_grants.target_username
                    WHERE task_grants.target_uid=? AND task_grants.target_username=?
                      AND task_grants.grant_kind=? AND task_grants.resource_ref=?
                      AND task_grants.revoked_at IS NULL
                      AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                      AND materials.kind=? LIMIT 1''',
                    (uid, username, kind, ref, kind)).fetchone()
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                task_grant = None
            return task_grant is not None

    def _require_allowed(self, principal: object, kind: str, ref: str) -> None:
        if not self._allowed(principal, kind, ref):
            raise KitError('无权读取该共享组件')
        from .web_lifecycle import is_active
        if not is_active(self.store, kind, ref):
            raise KitError('该组件已下架或移入回收站，不能用于新项目；已有副本和固定提交保留')

    def _installed(self, kind: str, ref: str) -> bool:
        catalog = self.library.catalog()
        key = 'plugins' if kind == 'algorithm' else 'templates'
        return any(row['ref'] == ref for row in catalog[key])

    def _task_algorithm_source(self, connection: sqlite3.Connection, material: sqlite3.Row) -> dict[str, object]:
        """Read one task-owned exact algorithm package and verify every byte."""
        ref = material['ref']
        if not isinstance(ref, str):
            raise KitError('任务算法材料记录已损坏')
        try:
            reference(ref)
        except KitError as exc:
            raise KitError('任务算法材料引用无效') from exc
        rows = connection.execute('''SELECT path,sha256,size,content FROM web_task_material_files
            WHERE material_id=? ORDER BY path''', (material['id'],)).fetchall()
        files: dict[str, bytes] = {}
        for row in rows:
            path, digest, size, content = row['path'], row['sha256'], row['size'], row['content']
            if (not isinstance(path, str) or not isinstance(digest, str) or not _HASH.fullmatch(digest) or
                    isinstance(size, bool) or not isinstance(size, int) or size < 0 or
                    not isinstance(content, (bytes, bytearray))):
                raise KitError('任务算法材料文件记录已损坏')
            value = bytes(content)
            if len(value) != size or hashlib.sha256(value).hexdigest() != digest or path in files:
                raise KitError('任务算法材料文件摘要不匹配')
            files[path] = value
        plugin_files: dict[str, bytes] = {}
        contract_files: dict[str, bytes] = {}
        for path, value in files.items():
            if path.startswith('plugin/') and len(path) > len('plugin/'):
                plugin_files[path[len('plugin/'):]] = value
            elif path.startswith('contracts/') and len(path) > len('contracts/'):
                contract_files[path] = value
            else:
                raise KitError('任务算法材料文件路径无效')
        metadata = self.library._plugin_from_files(plugin_files, ref)
        try:
            required_contracts = set(metadata['inputs'].values()) | set(metadata['outputs'].values())
        except TypeError as exc:
            raise KitError('任务算法材料的契约声明无效') from exc
        expected_paths: dict[str, str] = {}
        for contract_ref in required_contracts:
            name, version = reference(contract_ref)
            expected_paths[f'contracts/{name}/{version}.yaml'] = contract_ref
        if set(contract_files) != set(expected_paths):
            raise KitError('任务算法材料的契约文件与算法声明不一致')
        for path, contract_ref in expected_paths.items():
            document = self.library._safe_yaml(contract_files[path], '任务算法契约')
            if (document.get('schema') != 'hydro.contract/v1' or document.get('id') != contract_ref or
                    document.get('type') not in _TYPES or not isinstance(document.get('description', ''), str)):
                raise KitError('任务算法材料的契约无效')
        return {'ref': ref, 'metadata': metadata, 'files': files}

    def _task_algorithms(self, principal: object, *, active_only: bool) -> dict[str, dict[str, object]]:
        """Return only this principal's effective exact task algorithm grants.

        This deliberately bypasses the teacher-wide formal-library shortcut:
        task-only material is visible only when the teacher is also its named
        recipient.  The query is entirely broker-side and never accepts a
        browser identity or task ID.
        """
        uid, username, _role = _principal(principal)
        try:
            with self.store._connection() as connection:
                rows = connection.execute('''SELECT DISTINCT materials.id,materials.ref
                    FROM web_task_grants task_grants
                    JOIN web_task_materials materials ON materials.id=task_grants.material_id
                    JOIN web_task_deliveries tasks ON tasks.id=task_grants.task_id
                    JOIN web_task_recipients recipients ON recipients.task_id=task_grants.task_id
                      AND recipients.target_uid=task_grants.target_uid
                      AND recipients.target_username=task_grants.target_username
                    WHERE task_grants.target_uid=? AND task_grants.target_username=?
                      AND task_grants.grant_kind='algorithm' AND materials.kind='algorithm'
                      AND task_grants.revoked_at IS NULL
                      AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                    ORDER BY materials.ref,materials.id''', (uid, username)).fetchall()
                sources = [self._task_algorithm_source(connection, row) for row in rows]
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            return {}
        if active_only:
            from .web_lifecycle import is_active
            sources = [source for source in sources if is_active(self.store, 'algorithm', str(source['ref']))]
        result: dict[str, dict[str, object]] = {}
        for source in sources:
            ref = str(source['ref'])
            existing = result.get(ref)
            if existing is not None and not self._same_component_content(existing, source):
                raise KitError('同一算法版本由多个任务提供了不同内容；请使用新的版本号')
            result.setdefault(ref, source)
        return result

    def _ordinary_component(self, ref: str) -> dict[str, object]:
        metadata, plugin_files, _revision = self.library._plugin_source(ref)
        files = {f'plugin/{path}': content for path, content in plugin_files.items()}
        for contract_ref in sorted(set(metadata['inputs'].values()) | set(metadata['outputs'].values())):
            _document, content = self.library._contract_source(contract_ref)
            name, version = reference(contract_ref)
            files[f'contracts/{name}/{version}.yaml'] = content
        return {'ref': ref, 'metadata': metadata, 'files': files}

    @staticmethod
    def _same_contract_content(left: bytes, right: bytes) -> bool:
        """YAML layout is not contract identity; its parsed document is."""
        try:
            return yaml.safe_load(left.decode('utf-8')) == yaml.safe_load(right.decode('utf-8'))
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
            return False

    def _same_component_content(self, left: dict[str, object], right: dict[str, object]) -> bool:
        """Code bytes must match; equivalent plugin/contract YAML is accepted."""
        if left['metadata'] != right['metadata']:
            return False
        left_files, right_files = left['files'], right['files']
        if not isinstance(left_files, dict) or not isinstance(right_files, dict) or set(left_files) != set(right_files):
            return False
        for path in left_files:
            left_content, right_content = left_files[path], right_files[path]
            if not isinstance(left_content, bytes) or not isinstance(right_content, bytes):
                return False
            if path == 'plugin/plugin.yaml':
                continue  # parsed metadata above captures semantic YAML identity
            if path.startswith('contracts/'):
                if not self._same_contract_content(left_content, right_content):
                    return False
            elif left_content != right_content:
                return False
        return True

    @staticmethod
    def _task_contracts(sources: dict[str, dict[str, object]]) -> dict[str, tuple[dict[str, Any], bytes]]:
        result: dict[str, tuple[dict[str, Any], bytes]] = {}
        for source in sources.values():
            metadata, files = source['metadata'], source['files']
            assert isinstance(metadata, dict) and isinstance(files, dict)
            for contract_ref in set(metadata['inputs'].values()) | set(metadata['outputs'].values()):
                name, version = reference(contract_ref)
                path = f'contracts/{name}/{version}.yaml'
                content = files[path]
                try:
                    document = yaml.safe_load(content.decode('utf-8'))
                except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:  # already validated at material read
                    raise KitError('任务算法材料的契约无效') from exc
                if not isinstance(document, dict):
                    raise KitError('任务算法材料的契约无效')
                current = result.get(contract_ref)
                if current is not None and not ComponentService._same_contract_content(current[1], content):
                    raise KitError('同一契约版本由多个任务提供了不同内容；请使用新的版本号')
                result.setdefault(contract_ref, (document, content))
        return result

    def _allowed_contracts(self, principal: object,
                           task_sources: dict[str, dict[str, object]] | None = None) -> set[str]:
        _uid, _username, role = _principal(principal)
        catalog = self.library.catalog()
        if role == 'teacher':
            allowed = {str(row['id']) for row in catalog['contracts']}
        else:
            allowed = set()
            for plugin in catalog['plugins']:
                ref = str(plugin['ref'])
                if not self._allowed(principal, 'algorithm', ref):
                    continue
                allowed.update(str(contract) for contract in plugin['inputs'].values())
                allowed.update(str(contract) for contract in plugin['outputs'].values())
        if task_sources is None:
            task_sources = self._task_algorithms(principal, active_only=True)
        for source in task_sources.values():
            metadata = source['metadata']
            assert isinstance(metadata, dict)
            allowed.update(str(contract) for contract in metadata['inputs'].values())
            allowed.update(str(contract) for contract in metadata['outputs'].values())
        return allowed

    # Read operations ---------------------------------------------------
    def catalog(self, principal: Principal) -> dict[str, list[dict[str, Any]]]:
        _uid, _username, role = _principal(principal)
        catalog = self.library.catalog()
        from .web_lifecycle import states
        plugin_states, template_states = states(self.store, 'algorithm'), states(self.store, 'template')
        catalog['plugins'] = [row for row in catalog['plugins'] if plugin_states.get(str(row['ref']), 'active') == 'active']
        catalog['templates'] = [row for row in catalog['templates'] if template_states.get(str(row['ref']), 'active') == 'active']
        ordinary = {str(row['ref']): row for row in catalog['plugins']}
        plugins = list(catalog['plugins']) if role == 'teacher' else [
            row for row in catalog['plugins'] if self._allowed(principal, 'algorithm', str(row['ref']))]
        by_ref = {str(row['ref']): row for row in plugins}
        task_sources = self._task_algorithms(principal, active_only=True)
        for ref, source in task_sources.items():
            if ref in ordinary:
                if not self._same_component_content(self._ordinary_component(ref), source):
                    raise KitError('正式算法与任务材料使用相同版本但内容不同；请使用新的版本号')
                by_ref.setdefault(ref, ordinary[ref])
                continue
            metadata = source['metadata']
            assert isinstance(metadata, dict)
            by_ref[ref] = {'ref': ref, 'name': metadata['name'], 'version': metadata['version'],
                           'inputs': metadata['inputs'], 'outputs': metadata['outputs'],
                           'parameters': metadata['parameters'], 'source': 'task'}
        contracts_needed = {str(contract) for plugin in by_ref.values()
                            for contract in list(plugin['inputs'].values()) + list(plugin['outputs'].values())}
        task_contracts = self._task_contracts(task_sources)
        ordinary_contracts = {str(row['id']): row for row in catalog['contracts']}
        if role == 'teacher':
            contracts_needed.update(ordinary_contracts)
        contracts: list[dict[str, Any]] = []
        for ref in sorted(contracts_needed):
            task_contract = task_contracts.get(ref)
            ordinary_contract = ordinary_contracts.get(ref)
            if task_contract is not None and ordinary_contract is not None:
                _document, ordinary_content = self.library._contract_source(ref)
                if not self._same_contract_content(ordinary_content, task_contract[1]):
                    raise KitError('正式契约与任务材料使用相同版本但内容不同；请使用新的版本号')
            if ordinary_contract is not None:
                contracts.append(ordinary_contract)
            elif task_contract is not None:
                document, _content = task_contract
                contracts.append({'id': ref, 'type': document['type'], 'description': document.get('description', '')})
            else:  # a formal plugin has already passed its library validation
                raise KitError('算法声明的契约不存在')
        return {'plugins': [by_ref[ref] for ref in sorted(by_ref)], 'contracts': contracts,
                'templates': list(catalog['templates']) if role == 'teacher' else [
                    row for row in catalog['templates'] if self._allowed(principal, 'template', str(row['ref']))]}

    def component(self, principal: Principal, ref: str) -> dict[str, object]:
        _kind, ref = self._component('algorithm', ref)
        # Authorization deliberately precedes a filesystem lookup, so a
        # student cannot enumerate private refs by probing source paths.
        task_source = self._task_algorithms(principal, active_only=False).get(ref)
        if task_source is None:
            self._require_allowed(principal, 'algorithm', ref)
            return self._ordinary_component(ref)
        from .web_lifecycle import is_active
        if not is_active(self.store, 'algorithm', ref):
            raise KitError('该组件已下架或移入回收站，不能用于新项目；已有副本和固定提交保留')
        try:
            ordinary = self._ordinary_component(ref)
        except KitError as exc:
            if '不存在' not in str(exc):
                raise
        else:
            if not self._same_component_content(ordinary, task_source):
                raise KitError('正式算法与任务材料使用相同版本但内容不同；请使用新的版本号')
            self._require_allowed(principal, 'algorithm', ref)
            return ordinary
        return task_source

    def contract(self, principal: Principal, ref: str) -> dict[str, object]:
        _kind, ref = self._component('algorithm', ref)
        _uid, _username, role = _principal(principal)
        task_sources = self._task_algorithms(principal, active_only=True)
        task_contracts = self._task_contracts(task_sources)
        # Teachers receive the library's ordinary "not installed" result;
        # Composer relies on that distinction when creating a local draft.
        # Students must pass the exact allowed-algorithm relationship first.
        if role != 'teacher' and ref not in self._allowed_contracts(principal, task_sources):
            raise KitError('无权读取该共享契约')
        task_contract = task_contracts.get(ref)
        try:
            metadata, content = self.library._contract_source(ref)
        except KitError as exc:
            if task_contract is None or '不存在' not in str(exc):
                raise
            metadata, content = task_contract
        else:
            if task_contract is not None and not self._same_contract_content(content, task_contract[1]):
                raise KitError('正式契约与任务材料使用相同版本但内容不同；请使用新的版本号')
        return {'metadata': metadata, 'files': {'contract.yaml': content}}

    def template(self, principal: Principal, ref: str) -> dict[str, object]:
        _kind, ref = self._component('template', ref)
        self._require_allowed(principal, 'template', ref)
        _metadata, files, _revision = self.library._template_source(ref)
        return {'ref': ref, 'files': files}

    def validate_drafts(self, principal: Principal, contracts: object) -> dict[str, bool]:
        """Reserve installed contract IDs without disclosing private definitions.

        A student may create a new local draft without being able to fetch every
        installed contract. This check returns no source or private metadata.
        """
        _principal(principal)
        if not isinstance(contracts, list) or len(contracts) > 64:
            raise KitError('草稿契约必须是有限列表')
        seen: set[str] = set()
        for row in contracts:
            if (type(row) is not dict or set(row) != {'id', 'type', 'description'}
                    or not all(isinstance(row[key], str) for key in row)):
                raise KitError('草稿契约格式无效')
            reference(row['id'])
            if row['id'] in seen:
                raise KitError('草稿契约重复')
            seen.add(row['id'])
            if row['type'] not in {'number', 'integer', 'string', 'boolean', 'mapping', 'sequence', 'any'}:
                raise KitError('草稿契约类型无效')
            if len(row['description']) > 4000:
                raise KitError('草稿契约描述过长')
            try:
                existing, _content = self.library._contract_source(row['id'])
            except KitError as exc:
                if '不存在' not in str(exc):
                    raise
            else:
                expected = {'schema': 'hydro.contract/v1', **row}
                if existing != expected:
                    raise KitError('草稿契约与共享契约内容冲突；请使用新的名称或版本')
        return {'valid': True}

    # Authorization administration ------------------------------------
    def _template_dependencies(self, ref: str, installed_algorithms: set[str] | None = None) -> tuple[list[str], list[dict[str, object]]]:
        try:
            doc, files, _rev = self.library._template_source(ref)
        except KitError:
            return [], []
        if installed_algorithms is None:
            try:
                installed_algorithms = {str(r['ref']) for r in self.library.catalog()['plugins']}
            except KitError:
                installed_algorithms = set()
        algs: list[str] = []
        seen_alg: set[str] = set()
        for alias, cfg in (doc.get('plugins') or {}).items():
            if not isinstance(cfg, dict) or not isinstance(cfg.get('path'), str):
                continue
            prefix = cfg['path'].rstrip('/')
            raw_yaml = files.get(prefix + '/plugin.yaml')
            if raw_yaml is None:
                continue
            try:
                pmeta = yaml.safe_load(raw_yaml.decode('utf-8')) or {}
            except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
                continue
            if isinstance(pmeta, dict) and pmeta.get('name') and pmeta.get('version'):
                aref = f"{pmeta['name']}@{pmeta['version']}"
                if aref in installed_algorithms and aref not in seen_alg:
                    seen_alg.add(aref)
                    algs.append(aref)
        datasets: list[dict[str, object]] = []
        seen_ds: set[str] = set()
        raw_refs = files.get('DATA-REFERENCES.json')
        if raw_refs:
            try:
                parsed = json.loads(raw_refs.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict) and isinstance(entry.get('dataset_id'), str):
                        did = entry['dataset_id']
                        if did not in seen_ds:
                            seen_ds.add(did)
                            datasets.append(entry)
        return algs, datasets

    def access(self, principal: Principal, *, include_inactive: bool = False) -> list[dict[str, object]]:
        self._teacher(principal)
        if type(include_inactive) is not bool:
            raise KitError('include_inactive 必须是布尔值')
        from .web_lifecycle import AssetLifecycle
        lifecycle = AssetLifecycle(self.store, self.library.library_root)
        catalog = self.library.catalog()
        meta_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for row in catalog['plugins']:
            meta_by_pair[('algorithm', str(row['ref']))] = row
        for row in catalog['templates']:
            meta_by_pair[('template', str(row['ref']))] = row
        installed_algs = {str(row['ref']) for row in catalog['plugins']}
        installed = [('algorithm', str(row['ref'])) for row in catalog['plugins']]
        installed += [('template', str(row['ref'])) for row in catalog['templates']]
        with self.store._connection() as connection:
            result = [self._row(kind, ref, *self._db_row(connection, kind, ref))
                      for kind, ref in sorted(installed)]
            c_rows = connection.execute('''SELECT template_ref, resource_kind, resource_ref, public, target_uid, target_username
                FROM web_case_cascaded_grants''').fetchall()
        cascaded_map: dict[tuple[str, str], dict[str, Any]] = {}
        for crow in c_rows:
            key = (str(crow['resource_kind']), str(crow['resource_ref']))
            info = cascaded_map.setdefault(key, {'public': False, 'targets': {}, 'from': set()})
            info['from'].add(str(crow['template_ref']))
            if crow['public']:
                info['public'] = True
            elif crow['target_uid'] and crow['target_username']:
                info['targets'][(int(crow['target_uid']), str(crow['target_username']))] = {
                    'uid': int(crow['target_uid']), 'username': str(crow['target_username'])
                }
        for row in result:
            row['lifecycle'] = lifecycle.summary(row['kind'], row['ref'])
            cinfo = meta_by_pair.get((row['kind'], row['ref']), {})
            row['description'] = cinfo.get('description', '')
            row['tags'] = list(cinfo.get('tags', []))
            row['family_id'] = cinfo.get('family_id', f"{row['kind']}:{row['ref']}")
            row['parent_ref'] = cinfo.get('parent_ref')
            if cinfo.get('display_name'):
                row['display_name'] = cinfo['display_name']
            cas = cascaded_map.get((row['kind'], row['ref']))
            row['cascaded_public'] = bool(cas and cas['public'])
            row['cascaded_targets'] = sorted(cas['targets'].values(), key=lambda item: (item['uid'], item['username'])) if cas else []
            row['cascaded_from'] = sorted(cas['from']) if cas else []
            if row['kind'] == 'template':
                # Source account names belong to the teacher management view,
                # never to the broadly visible student resource catalog.
                with self.store._connection() as con:
                    source = con.execute('''SELECT c.submission_id, c.source_sha256,
                        s.project_id, s.version_major, s.version_minor,
                        pr.name AS project_name, pr.owner_username
                        FROM web_asset_published p
                        JOIN web_asset_candidates c ON c.candidate_id=p.candidate_id
                        LEFT JOIN submissions s ON s.id=c.submission_id
                        LEFT JOIN projects pr ON pr.id=s.project_id
                        WHERE p.kind='template' AND p.ref=?''', (row['ref'],)).fetchone()
                row['source'] = dict(source) if source is not None else None
                algs, dsets = self._template_dependencies(str(row['ref']), installed_algs)
                row['included_algorithms'] = algs
                row['included_datasets'] = [str(d['dataset_id']) for d in dsets]
        return [row for row in result if include_inactive or row['lifecycle']['state'] == 'active']

    def set_access(self, principal: Principal, kind: str, ref: str, public: bool,
                   targets: list[Principal], expected_revision: str, *, cascade: bool = True) -> dict[str, object]:
        uid, username, _role = self._teacher(principal)
        kind, ref = self._component(kind, ref)
        if type(public) is not bool:
            raise KitError('public 必须是布尔值')
        target_rows = self._targets(targets)
        if not isinstance(expected_revision, str) or not _HASH.fullmatch(expected_revision):
            raise KitError('授权版本必须是 SHA-256')
        if not self._installed(kind, ref):
            raise KitError('共享组件尚未安装')
        deps_algs: list[str] = []
        deps_datasets: list[dict[str, object]] = []
        if kind == 'template' and cascade:
            deps_algs, deps_datasets = self._template_dependencies(ref)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                current_public, current_targets = self._db_row(connection, kind, ref)
                current = self._row(kind, ref, current_public, current_targets)
                if current['revision'] != expected_revision:
                    raise KitError('共享组件授权冲突，请重新读取后再保存')
                connection.execute(
                    '''INSERT INTO component_policies(kind, ref, public) VALUES (?, ?, ?)
                       ON CONFLICT(kind, ref) DO UPDATE SET public = excluded.public''',
                    (kind, ref, int(public)))
                connection.execute('DELETE FROM grants WHERE kind = ? AND ref = ?', (kind, ref))
                for target in target_rows:
                    connection.execute(
                        '''INSERT INTO grants(kind, ref, target_uid, target_username)
                           VALUES (?, ?, ?, ?)''',
                        (kind, ref, target['uid'], target['username']))
                if kind == 'template':
                    connection.execute('DELETE FROM web_case_cascaded_grants WHERE template_ref = ?', (ref,))
                    if cascade and (public or target_rows):
                        for alg_ref in deps_algs:
                            if public:
                                connection.execute(
                                    '''INSERT INTO web_case_cascaded_grants
                                       (template_ref, resource_kind, resource_ref, reference_json, public, target_uid, target_username, grantor_uid, grantor_username, updated_at)
                                       VALUES (?, 'algorithm', ?, '{}', 1, 0, '', ?, ?, ?)''',
                                    (ref, alg_ref, uid, username, now))
                            for target in target_rows:
                                connection.execute(
                                    '''INSERT INTO web_case_cascaded_grants
                                       (template_ref, resource_kind, resource_ref, reference_json, public, target_uid, target_username, grantor_uid, grantor_username, updated_at)
                                       VALUES (?, 'algorithm', ?, '{}', 0, ?, ?, ?, ?, ?)''',
                                    (ref, alg_ref, target['uid'], target['username'], uid, username, now))
                        for ds_ref in deps_datasets:
                            did = str(ds_ref['dataset_id'])
                            rjson = json.dumps(ds_ref, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                            if public:
                                connection.execute(
                                    '''INSERT INTO web_case_cascaded_grants
                                       (template_ref, resource_kind, resource_ref, reference_json, public, target_uid, target_username, grantor_uid, grantor_username, updated_at)
                                       VALUES (?, 'dataset', ?, ?, 1, 0, '', ?, ?, ?)''',
                                    (ref, did, rjson, uid, username, now))
                            for target in target_rows:
                                connection.execute(
                                    '''INSERT INTO web_case_cascaded_grants
                                       (template_ref, resource_kind, resource_ref, reference_json, public, target_uid, target_username, grantor_uid, grantor_username, updated_at)
                                       VALUES (?, 'dataset', ?, ?, 0, ?, ?, ?, ?, ?)''',
                                    (ref, did, rjson, target['uid'], target['username'], uid, username, now))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        for item in self.access(principal, include_inactive=True):
            if item.get('kind') == kind and item.get('ref') == ref:
                return item
        from .web_lifecycle import AssetLifecycle
        result = self._row(kind, ref, public, target_rows)
        result['lifecycle'] = AssetLifecycle(self.store).summary(kind, ref)
        return result

    def set_dataset_access(self, principal: Principal, dataset_id: str, public: bool,
                           targets: list[Principal]) -> dict[str, object]:
        self._teacher(principal)
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise KitError('数据集 ID 无效')
        if type(public) is not bool:
            raise KitError('public 必须是布尔值')
        target_rows = self._targets(targets)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute(
                    "SELECT dataset_id FROM web_derived_datasets WHERE dataset_id=? AND visibility='public'",
                    (dataset_id,)).fetchone() if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_derived_datasets'").fetchone() else None
                if row is None:
                    raise KitError('只能配置已公开的派生数据集权限')
                connection.execute(
                    '''INSERT INTO web_dataset_policies(dataset_id, public, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(dataset_id) DO UPDATE SET public=excluded.public, updated_at=excluded.updated_at''',
                    (dataset_id, int(public), now))
                connection.execute('DELETE FROM web_dataset_grants WHERE dataset_id=?', (dataset_id,))
                for target in target_rows:
                    connection.execute(
                        'INSERT INTO web_dataset_grants(dataset_id, target_uid, target_username) VALUES (?, ?, ?)',
                        (dataset_id, target['uid'], target['username']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'dataset_id': dataset_id, 'public': public, 'targets': target_rows}

    def dataset_access_map(self, principal: Principal) -> dict[str, dict[str, object]]:
        self._teacher(principal)
        out: dict[str, dict[str, object]] = {}
        with self.store._connection() as connection:
            for row in connection.execute('SELECT * FROM web_dataset_policies').fetchall():
                did = str(row['dataset_id'])
                grants = connection.execute(
                    'SELECT target_uid, target_username FROM web_dataset_grants WHERE dataset_id=? ORDER BY target_uid, target_username',
                    (did,)).fetchall()
                out[did] = {
                    'public': bool(row['public']),
                    'targets': [{'uid': r['target_uid'], 'username': r['target_username']} for r in grants],
                    'cascaded_public': False, 'cascaded_targets': [], 'cascaded_from': [],
                }
            for crow in connection.execute(
                "SELECT template_ref, resource_ref, public, target_uid, target_username FROM web_case_cascaded_grants WHERE resource_kind='dataset'"
            ).fetchall():
                did = str(crow['resource_ref'])
                entry = out.setdefault(did, {'public': True, 'targets': [], 'cascaded_public': False, 'cascaded_targets': [], 'cascaded_from': []})
                if crow['template_ref'] not in entry['cascaded_from']:
                    entry['cascaded_from'].append(str(crow['template_ref']))
                if crow['public']:
                    entry['cascaded_public'] = True
                elif crow['target_uid'] and crow['target_username']:
                    t = {'uid': int(crow['target_uid']), 'username': str(crow['target_username'])}
                    if t not in entry['cascaded_targets']:
                        entry['cascaded_targets'].append(t)
        return out
