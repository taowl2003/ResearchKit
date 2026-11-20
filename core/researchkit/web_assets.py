"""Inert, teacher-reviewed assets extracted from immutable Web submissions.

This is deliberately a storage and static-validation layer.  It never opens a
workspace, imports a submission module, or invokes the command-line publisher.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, identifier, reference
from .model import validate_parameters, check_type
from .web_composer import _json_value
from .web_identity import Principal
from .web_jobs import Jobs
from .web_library import SharedLibrary, revision
from .web_store import SubmissionStore, _relative_path

_MAX_FILES = 512
_MAX_BYTES = 4 * 1024 * 1024
_SEMVER = re.compile(r"\d+\.\d+\.\d+\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CONTRACT_TYPES = {'number', 'integer', 'string', 'boolean', 'mapping', 'sequence', 'any'}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _teacher(principal: Principal) -> tuple[int, str]:
    if not isinstance(principal, Principal):
        raise KitError('只有教师可以管理发布候选')
    uid = getattr(principal, 'uid', None)
    username = getattr(principal, 'username', None)
    if (isinstance(uid, bool) or not isinstance(uid, int) or uid < 1 or
            not isinstance(username, str) or not username or
            getattr(principal, 'role', None) != 'teacher' or username != 'ly'):
        raise KitError('只有教师可以管理发布候选')
    return uid, username


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise KitError(f'{label} 无效')
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or '/' in value:
        raise KitError('资产名称必须是长度不超过 128 的单个标识符')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise KitError('资产名称必须是长度不超过 128 的单个标识符') from exc
    if len(encoded) > 128:
        raise KitError('资产名称必须是长度不超过 128 的单个标识符')
    try:
        return identifier(value)
    except KitError as exc:
        raise KitError('资产名称必须是长度不超过 128 的单个标识符') from exc


def _version(value: object) -> str:
    if not isinstance(value, str):
        raise KitError('资产版本必须是精确 x.y.z')
    try:
        if len(value.encode('utf-8')) > 32 or not _SEMVER.fullmatch(value):
            raise KitError('资产版本必须是精确 x.y.z')
    except UnicodeEncodeError as exc:
        raise KitError('资产版本必须是精确 x.y.z') from exc
    return value


def _yaml(data: bytes, label: str) -> dict[str, Any]:
    if len(data) > _MAX_BYTES:
        raise KitError(f'{label} 超过候选静态读取上限')
    try:
        value = yaml.safe_load(data.decode('utf-8'))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise KitError(f'{label} 必须是 UTF-8 YAML 映射') from exc
    if not isinstance(value, dict):
        raise KitError(f'{label} 顶层必须是映射')
    # safe_load permits aliases; reject recursive/non-JSON YAML before it can
    # reach canonical JSON, sqlite, or a YAML re-emission.
    _json_value(value)
    return value


def _dump(value: object) -> bytes:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode('utf-8')


def _signature(data: bytes, function: str, names: tuple[str, ...], label: str) -> None:
    if len(data) > 1024**2:
        raise KitError(f'{label} 超过 1 MiB 入口静态检查上限')
    try:
        tree = ast.parse(data.decode('utf-8'), filename=label)
    except (UnicodeDecodeError, SyntaxError, RecursionError) as exc:
        raise KitError(f'{label} 必须是可静态解析的 Python 源码') from exc
    found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function]
    if len(found) != 1 or isinstance(found[0], ast.AsyncFunctionDef):
        raise KitError(f'{label} 必须定义唯一的同步 {function}()')
    args = found[0].args
    actual = tuple(arg.arg for arg in args.posonlyargs + args.args)
    if actual != names or args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.kw_defaults:
        raise KitError(f'{label} 的 {function}() 参数声明不安全')


def _contract(data: bytes, ref: str) -> dict[str, Any]:
    doc = _yaml(data, '契约')
    if doc.get('schema') != 'hydro.contract/v1' or doc.get('id') != ref or not isinstance(doc.get('type'), str) or doc.get('type') not in _CONTRACT_TYPES:
        raise KitError(f'契约身份或类型无效：{ref}')
    if not isinstance(doc.get('description', ''), str):
        raise KitError(f'契约描述无效：{ref}')
    return doc


def _refs(meta: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ('inputs', 'outputs'):
        ports = meta.get(key)
        if not isinstance(ports, dict):
            raise KitError(f'算法 {key} 声明无效')
        for port, item in ports.items():
            try:
                identifier(port)
                reference(item)
            except KitError as exc:
                raise KitError('算法端口契约引用无效') from exc
            result.add(item)
    return result


def _parameter_rules(rules: dict[str, Any]) -> dict[str, Any]:
    defaults = {}
    for key, rule in rules.items():
        identifier(key)
        if (not isinstance(rule, dict) or not isinstance(rule.get('type', 'any'), str)
                or rule.get('type', 'any') not in _CONTRACT_TYPES):
            raise KitError('算法参数规则或类型无效')
        if 'required' in rule and type(rule['required']) is not bool:
            raise KitError('算法参数 required 必须是布尔值')
        if 'enum' in rule and not isinstance(rule['enum'], list):
            raise KitError('算法参数 enum 必须是列表')
        for bound in ('minimum', 'maximum'):
            if bound in rule and not check_type('number', rule[bound]):
                raise KitError('算法参数范围必须是数值')
        if 'minimum' in rule and 'maximum' in rule and rule['minimum'] > rule['maximum']:
            raise KitError('算法参数范围下限高于上限')
        if 'default' in rule:
            validate_parameters({key: rule}, {key: rule['default']}, '算法默认参数')
            defaults[key] = rule['default']
    return defaults


def _safe_prefix(value: object, label: str) -> str:
    return _relative_path(value, label=label).rstrip('/')


def _bounded(files: dict[str, bytes]) -> None:
    if not files or len(files) > _MAX_FILES:
        raise KitError('候选资产文件数超过上限')
    total = 0
    for path, data in files.items():
        _relative_path(path, label='候选资产路径')
        if not isinstance(data, bytes):
            raise KitError('候选资产文件必须是 bytes')
        total += len(data)
        if total > _MAX_BYTES:
            raise KitError('候选资产总大小超过上限')
    names = set(files)
    for path in names:
        parts = path.split('/')
        if any('/'.join(parts[:n]) in names for n in range(1, len(parts))):
            raise KitError('候选资产含文件和目录冲突')


def _asset_hash(files: dict[str, bytes], specification: dict[str, Any]) -> str:
    return hashlib.sha256((_json(specification) + '\n' + revision(files)).encode('utf-8')).hexdigest()


class AssetService:
    def __init__(self, store: SubmissionStore, library_root: Path | str):
        self.store, self.library_root = store, Path(library_root)
        # Do not substitute the published-asset composite library here: these
        # parsers operate only on the bytes passed below.
        self._library = SharedLibrary(self.library_root)
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                for statement in (
                    '''CREATE TABLE IF NOT EXISTS web_asset_names (
                         kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')), name TEXT NOT NULL,
                         asset_id TEXT NOT NULL UNIQUE, PRIMARY KEY(kind,name))''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_candidates (
                         candidate_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
                         name TEXT NOT NULL, version TEXT NOT NULL, ref TEXT NOT NULL, alias TEXT,
                         alias_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','reviewed','published')),
                         submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                         review_notes TEXT, validation_run_id TEXT, candidate_sha256 TEXT NOT NULL,
                         metadata_json TEXT NOT NULL, static_validation_json TEXT NOT NULL,
                         data_references_json TEXT NOT NULL, source_origin_json TEXT NOT NULL,
                         UNIQUE(submission_id,source_sha256,kind,name,version,alias_key))''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_candidate_files (
                         candidate_id TEXT NOT NULL REFERENCES web_asset_candidates(candidate_id), path TEXT NOT NULL,
                         sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                         PRIMARY KEY(candidate_id,path))''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_published (
                         kind TEXT NOT NULL, ref TEXT NOT NULL, candidate_id TEXT NOT NULL UNIQUE,
                         asset_id TEXT NOT NULL, published_at TEXT NOT NULL, PRIMARY KEY(kind,ref))''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_published_files (
                         kind TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                         size INTEGER NOT NULL, content BLOB NOT NULL, PRIMARY KEY(kind,ref,path))''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_contracts (
                         contract_ref TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                         contract_json TEXT NOT NULL, candidate_id TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS web_asset_metadata (
                         kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')),
                         ref TEXT NOT NULL,
                         display_name TEXT NOT NULL DEFAULT '',
                         family_id TEXT NOT NULL,
                         parent_ref TEXT,
                         description TEXT NOT NULL DEFAULT '',
                         tags_json TEXT NOT NULL DEFAULT '[]',
                         updated_at TEXT NOT NULL,
                         PRIMARY KEY(kind, ref))''',
                ):
                    con.execute(statement)
                # Keep the public file-table SQL surface while sharing
                # immutable bodies with submissions and the central library.
                # ``normalize`` is idempotent and leaves already migrated
                # compatibility views intact.
                from .web_content import normalize
                for table in ('web_asset_candidate_files', 'web_asset_published_files',
                              'web_asset_contracts'):
                    normalize(con, table)
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction: con.execute('ROLLBACK')
                raise

    @contextmanager
    def _write(self, connection=None):
        """Use a caller-owned publication transaction when supplied.

        Public AssetService calls retain their normal independent transaction.
        ReviewFlow passes its one transaction so a failed compound publication
        cannot leave candidates, assets, metadata or grants behind.
        """
        if connection is not None:
            if not connection.in_transaction:
                raise KitError('资产写入需要活动事务')
            yield connection
            return
        with self.store.transaction() as con:
            yield con

    def _source(self, principal: Principal, submission_id: str) -> tuple[Any, dict[str, bytes], list[dict[str, object]]]:
        # get_submission is the authorization gate; _source_files subsequently
        # rechecks the immutable manifest while preserving provenance columns.
        source = self.store.get_submission(submission_id, principal)
        with self.store._connection() as con:
            record, files = self.store._source_files(con, submission_id)
        if source['content_sha256'] != record['content_sha256']:
            raise KitError('固定提交摘要不一致')
        manifest = self.store._stored_manifest(record)
        refs = manifest['data_references']['references']
        return record, files, refs

    def _algorithm(self, source: dict[str, bytes], name: str, version: str, alias: object) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any], list[dict[str, object]]]:
        project = _yaml(source.get('project.yaml', b''), 'project.yaml')
        plugins = project.get('plugins')
        if not isinstance(plugins, dict) or not isinstance(alias, str) or alias not in plugins or not isinstance(plugins[alias], dict):
            raise KitError('算法候选必须指定 project.yaml 中的插件别名')
        prefix = _safe_prefix(plugins[alias].get('path'), '插件路径')
        selected: dict[str, bytes] = {}
        bad = ('_runs/', 'results/', 'report/', 'cache/', '__pycache__/')
        for path, data in source.items():
            if path.startswith(prefix + '/'):
                rel = path[len(prefix) + 1:]
                if rel.startswith(bad) or '/_runs/' in rel or '/results/' in rel or '/report/' in rel or '/cache/' in rel:
                    continue
                selected['plugin/' + rel] = data
        if not selected:
            raise KitError('固定提交未包含指定插件目录')
        _bounded(selected)
        meta = _yaml(selected.get('plugin/plugin.yaml', b''), 'plugin.yaml')
        old_ref = f"{meta.get('name')}@{meta.get('version')}"
        # Validate the submitted identity before changing it.  A malformed
        # source must not become publishable merely by rewriting its YAML.
        original_files = {key[7:]: data for key, data in selected.items() if key.startswith('plugin/')}
        self._library._plugin_from_files(original_files, old_ref)
        meta['name'], meta['version'] = name, version
        selected['plugin/plugin.yaml'] = _dump(meta)
        ref = f'{name}@{version}'
        plugin_files = {key[7:]: data for key, data in selected.items() if key.startswith('plugin/')}
        parsed = self._library._plugin_from_files(plugin_files, ref)
        _parameter_rules(parsed['parameters'])
        _signature(plugin_files['plugin.py'], 'run', ('inputs', 'parameters', 'context'), 'plugin.py')
        contracts: list[dict[str, Any]] = []
        for contract_ref in sorted(_refs(parsed)):
            contract_name, contract_version = reference(contract_ref)
            original = f'.researchkit/contracts/{contract_name}/{contract_version}.yaml'
            data = source.get(original)
            if data is None: raise KitError(f'固定提交缺少算法契约：{contract_ref}')
            selected[f'contracts/{contract_name}/{contract_version}.yaml'] = data
            _bounded(selected)
            doc = _contract(data, contract_ref)
            contracts.append({'id': contract_ref, 'type': doc['type'], 'description': doc.get('description', '')})
        return selected, parsed, {'kind': 'algorithm', 'schema': 'hydro.plugin/v1', 'entry': 'plugin.py', 'run_signature': ['inputs','parameters','context'], 'imports_executed': False, 'contracts': contracts, 'parameter_rules_validated': True, 'parameter_values_validated': False}, []

    @staticmethod
    def _strip_template(path: str) -> bool:
        return (path in {'task', 'task.py', '.researchkit/local.json', '.researchkit/last-run.json', '.researchkit/run.lock', '.researchkit/bundle.json', '.researchkit/web-bundle.json', '.researchkit/lock.json'} or
                path.startswith(('.researchkit/runtime/', '_runs/', 'results/', 'report/', 'cache/', '__pycache__/', '.pytest_cache/')) or
                '/cache/' in path or '/results/' in path or '/report/' in path or '/_runs/' in path)

    def _template(self, source: dict[str, bytes], name: str, version: str, submission_id: str, source_sha: str, refs: list[dict[str, object]]) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any], list[dict[str, object]]]:
        files = {path: data for path, data in source.items() if not self._strip_template(path) and path != 'DATA-REFERENCES.json'}
        _bounded(files)
        project = _yaml(files.get('project.yaml', b''), 'project.yaml')
        project.pop('origin', None)
        project['name'] = name
        project['origin'] = {'kind': 'web-asset-source/v1', 'submission_id': submission_id, 'content_sha256': source_sha}
        files['project.yaml'] = _dump(project)
        files['DATA-REFERENCES.json'] = (_json(refs) + '\n').encode('utf-8')
        ref = f'{name}@{version}'
        parsed = self._library._template_from_files(files, ref)
        _signature(files['case.py'], 'run', ('context',), 'case.py')
        contracts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alias, config in parsed['plugins'].items():
            if not isinstance(alias, str) or not isinstance(config, dict): raise KitError('模板插件声明无效')
            identifier(alias)
            prefix = _safe_prefix(config.get('path'), '模板插件路径')
            plugin_files = {path[len(prefix)+1:]: data for path, data in files.items() if path.startswith(prefix + '/')}
            declared = _yaml(plugin_files.get('plugin.yaml', b''), '模板 plugin.yaml')
            plugin_ref = f"{declared.get('name')}@{declared.get('version')}"
            meta = self._library._plugin_from_files(plugin_files, plugin_ref)
            _signature(plugin_files['plugin.py'], 'run', ('inputs', 'parameters', 'context'), '模板 plugin.py')
            values = config.get('parameters', {})
            if not isinstance(values, dict):
                raise KitError('模板插件参数必须是映射')
            defaults = _parameter_rules(meta['parameters'])
            defaults.update(values)
            validate_parameters(meta['parameters'], defaults, f'模板插件 {alias}')
            for contract_ref in sorted(_refs(meta)):
                cn, cv = reference(contract_ref)
                data = files.get(f'.researchkit/contracts/{cn}/{cv}.yaml')
                if data is None: raise KitError(f'模板缺少契约：{contract_ref}')
                doc = _contract(data, contract_ref)
                if contract_ref not in seen:
                    contracts.append({'id': contract_ref, 'type': doc['type'], 'description': doc.get('description', '')})
                    seen.add(contract_ref)
        return files, parsed, {'kind': 'template', 'schema': 'researchkit.project/v1', 'entry': 'case.py', 'run_signature': ['context'], 'imports_executed': False, 'contracts': contracts, 'parameter_values_validated': True}, refs

    @staticmethod
    def _clean_tags(tags: object) -> list[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            raw_list = [part.strip() for part in re.split(r'[,，\n]+', tags) if part.strip()]
        elif isinstance(tags, (list, tuple)):
            raw_list = []
            for item in tags:
                if not isinstance(item, str):
                    raise KitError('资产标签必须是字符串')
                cleaned = item.strip()
                if cleaned:
                    raw_list.append(cleaned)
        else:
            raise KitError('资产标签格式无效')
        result: list[str] = []
        seen: set[str] = set()
        for item in raw_list:
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in item) or len(item.encode('utf-8')) > 64:
                raise KitError('资产标签不能含控制字符且最多 64 字节')
            if item not in seen:
                seen.add(item)
                result.append(item)
        if len(result) > 24:
            raise KitError('资产标签数量不能超过 24 个')
        return result

    @staticmethod
    def _clean_description(value: object) -> str:
        if value is None:
            return ''
        if not isinstance(value, str):
            raise KitError('资产说明必须是字符串')
        text = value.strip()
        if len(text) > 4000:
            raise KitError('资产说明不能超过 4000 字符')
        return text

    def metadata_map(self, kind: str | None = None, *, connection=None) -> dict[tuple[str, str], dict[str, Any]]:
        if connection is None:
            with self.store._connection() as con:
                if kind is not None:
                    rows = con.execute('SELECT * FROM web_asset_metadata WHERE kind=?', (kind,)).fetchall()
                else:
                    rows = con.execute('SELECT * FROM web_asset_metadata').fetchall()
        elif kind is not None:
            rows = connection.execute('SELECT * FROM web_asset_metadata WHERE kind=?', (kind,)).fetchall()
        else:
            rows = connection.execute('SELECT * FROM web_asset_metadata').fetchall()
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            try:
                tags = json.loads(row['tags_json'])
                if not isinstance(tags, list):
                    tags = []
            except (TypeError, ValueError):
                tags = []
            out[(str(row['kind']), str(row['ref']))] = {
                'display_name': row['display_name'],
                'family_id': row['family_id'],
                'parent_ref': row['parent_ref'],
                'description': row['description'],
                'tags': [str(t) for t in tags if isinstance(t, str)],
                'updated_at': row['updated_at'],
            }
        return out

    def set_metadata(self, principal: Principal, kind: str, ref: str, *, description: object = None,
                     tags: object = None, parent_ref: object = None, display_name: object = None,
                     connection=None) -> dict[str, Any]:
        _teacher(principal)
        if kind not in {'algorithm', 'template'}:
            raise KitError('资产类型只能是 algorithm 或 template')
        reference(ref)
        parent = None
        if parent_ref is not None and str(parent_ref).strip():
            parent = str(parent_ref).strip()
            reference(parent)
            if parent == ref:
                raise KitError('新版本的父版本不能是自身')
        desc = None if description is None else self._clean_description(description)
        cleaned_tags = None if tags is None else self._clean_tags(tags)
        dname = '' if display_name is None else str(display_name).strip()[:200]
        now = _now()
        with self._write(connection) as con:
                parent_row = con.execute('SELECT * FROM web_asset_metadata WHERE kind=? AND ref=?', (kind, parent)).fetchone() if parent else None
                existing = con.execute('SELECT * FROM web_asset_metadata WHERE kind=? AND ref=?', (kind, ref)).fetchone()
                family_id = (parent_row['family_id'] if parent_row is not None else (f'{kind}:{parent}' if parent else (existing['family_id'] if existing is not None else f'{kind}:{ref}')))
                if desc is None or (desc == '' and parent is not None):
                    if existing is not None and desc is None:
                        desc = existing['description']
                    elif parent_row is not None:
                        desc = parent_row['description']
                    elif existing is not None:
                        desc = existing['description']
                    else:
                        desc = ''
                if cleaned_tags is None or (len(cleaned_tags) == 0 and parent is not None):
                    if existing is not None and cleaned_tags is None:
                        cleaned_tags = json.loads(existing['tags_json'])
                    elif parent_row is not None:
                        cleaned_tags = json.loads(parent_row['tags_json'])
                    elif existing is not None:
                        cleaned_tags = json.loads(existing['tags_json'])
                    else:
                        cleaned_tags = []
                if not dname:
                    if existing is not None and existing['display_name']:
                        dname = existing['display_name']
                    elif parent_row is not None and parent_row['display_name']:
                        dname = parent_row['display_name']
                con.execute('''INSERT INTO web_asset_metadata(kind,ref,display_name,family_id,parent_ref,description,tags_json,updated_at)
                               VALUES(?,?,?,?,?,?,?,?)
                               ON CONFLICT(kind,ref) DO UPDATE SET
                                 display_name=excluded.display_name,
                                 family_id=excluded.family_id,
                                 parent_ref=COALESCE(excluded.parent_ref, web_asset_metadata.parent_ref),
                                 description=excluded.description,
                                 tags_json=excluded.tags_json,
                                 updated_at=excluded.updated_at''',
                            (kind, ref, dname, family_id, parent, desc, _json(cleaned_tags), now))
                row = con.execute('SELECT * FROM web_asset_metadata WHERE kind=? AND ref=?', (kind, ref)).fetchone()
        return {
            'kind': kind, 'ref': ref, 'display_name': row['display_name'], 'family_id': row['family_id'],
            'parent_ref': row['parent_ref'], 'description': row['description'],
            'tags': json.loads(row['tags_json']), 'updated_at': row['updated_at'],
        }

    def create(self, principal: Principal, submission_id: str, kind: str, name: str, version: str, alias: str | None = None,
               *, source_override: dict[str, bytes] | None = None, refs_override: list[dict[str, object]] | None = None,
               connection=None) -> dict[str, Any]:
        _teacher(principal)
        if not isinstance(kind, str) or kind not in {'algorithm', 'template'}: raise KitError('资产类型只能是 algorithm 或 template')
        name, version = _name(name), _version(version)
        if kind == 'algorithm' and not isinstance(alias, str): raise KitError('算法候选必须指定插件别名')
        if kind == 'template' and alias is not None: raise KitError('模板候选不能指定插件别名')
        record, source, refs = self._source(principal, _uuid(submission_id, '提交 ID'))
        if source_override is not None:
            source = source_override
        if refs_override is not None:
            refs = refs_override
        source_sha, ref = record['content_sha256'], f'{name}@{version}'
        alias_key = alias or ''
        if connection is None:
            with self.store._connection() as con:
                row = con.execute('SELECT * FROM web_asset_candidates WHERE submission_id=? AND source_sha256=? AND kind=? AND name=? AND version=? AND alias_key=?', (submission_id, source_sha, kind, name, version, alias_key)).fetchone()
        else:
            row = connection.execute('SELECT * FROM web_asset_candidates WHERE submission_id=? AND source_sha256=? AND kind=? AND name=? AND version=? AND alias_key=?', (submission_id, source_sha, kind, name, version, alias_key)).fetchone()
        if row is not None and source_override is None: return self._candidate(row, include_files=True, connection=connection)
        if kind == 'algorithm': files, metadata, validation, data_refs = self._algorithm(source, name, version, alias)
        else: files, metadata, validation, data_refs = self._template(source, name, version, submission_id, source_sha, refs)
        _bounded(files)
        specification = {'schema': 'researchkit.web-asset/v1', 'source_submission_id': submission_id, 'source_sha256': source_sha, 'kind': kind, 'name': name, 'version': version, 'alias': alias, 'data_references': data_refs}
        digest = _asset_hash(files, specification)
        candidate_id, created_at = str(uuid.uuid4()), _now()
        origin = {key: record[key] for key in ('project_id','owner_uid','owner_username','source','trusted_baseline_submission_id','trusted_baseline_sha256','package_baseline_submission_id','package_baseline_sha256') if key in record.keys() and record[key] is not None}
        with self._write(connection) as con:
                if row is not None and source_override is not None and row['status'] == 'draft':
                    con.execute('DELETE FROM web_asset_candidate_files WHERE candidate_id=?', (row['candidate_id'],))
                    con.execute('DELETE FROM web_asset_candidates WHERE candidate_id=?', (row['candidate_id'],))
                from .web_cleanup import require_available
                require_available(con, kind, ref)
                for data_ref in data_refs:
                    require_available(con, 'dataset', data_ref['dataset_id'])
                known = con.execute('SELECT asset_id FROM web_asset_names WHERE kind=? AND name=?', (kind,name)).fetchone()
                asset_id = known['asset_id'] if known else str(uuid.uuid4())
                if known is None: con.execute('INSERT INTO web_asset_names(kind,name,asset_id) VALUES (?,?,?)', (kind,name,asset_id))
                try:
                    con.execute('''INSERT INTO web_asset_candidates(candidate_id,asset_id,kind,name,version,ref,alias,alias_key,status,submission_id,source_sha256,created_at,review_notes,validation_run_id,candidate_sha256,metadata_json,static_validation_json,data_references_json,source_origin_json) VALUES (?,?,?,?,?,?,?,?, 'draft',?,?,?,?,?,?,?,?,?,?)''', (candidate_id,asset_id,kind,name,version,ref,alias,alias_key,submission_id,source_sha,created_at,None,None,digest,_json(metadata),_json(validation),_json(data_refs),_json(origin)))
                except Exception as exc:
                    old = con.execute('SELECT * FROM web_asset_candidates WHERE submission_id=? AND source_sha256=? AND kind=? AND name=? AND version=? AND alias_key=?', (submission_id,source_sha,kind,name,version,alias_key)).fetchone()
                    if old is None: raise exc
                    return self._candidate(old, include_files=True, connection=con)
                con.executemany('INSERT INTO web_asset_candidate_files(candidate_id,path,sha256,size,content) VALUES (?,?,?,?,?)', [(candidate_id,path,hashlib.sha256(data).hexdigest(),len(data),data) for path,data in sorted(files.items())])
                row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
        return self._candidate(row, include_files=True, connection=connection)

    def _candidate(self, row: Any, *, include_files: bool, connection=None) -> dict[str, Any]:
        result = {key: row[key] for key in ('candidate_id','asset_id','kind','name','version','ref','status','submission_id','source_sha256','candidate_sha256','alias','created_at','review_notes','validation_run_id')}
        result['metadata'] = json.loads(row['metadata_json']); result['static_validation'] = json.loads(row['static_validation_json']); result['data_references'] = json.loads(row['data_references_json']); result['source_origin'] = json.loads(row['source_origin_json'])
        if include_files:
            if connection is None:
                with self.store._connection() as con:
                    rows = con.execute('SELECT path,sha256,size,content FROM web_asset_candidate_files WHERE candidate_id=? ORDER BY path',(row['candidate_id'],)).fetchall()
            else:
                rows = connection.execute('SELECT path,sha256,size,content FROM web_asset_candidate_files WHERE candidate_id=? ORDER BY path',(row['candidate_id'],)).fetchall()
            result['files'] = self._materialize(row, rows, '候选资产')
        return result

    @staticmethod
    def _candidate_spec(row: Any) -> dict[str, Any]:
        return {'schema': 'researchkit.web-asset/v1', 'source_submission_id': row['submission_id'],
                'source_sha256': row['source_sha256'], 'kind': row['kind'], 'name': row['name'],
                'version': row['version'], 'alias': row['alias'],
                'data_references': json.loads(row['data_references_json'])}

    def _materialize(self, row: Any, rows: list[Any], label: str) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for item in rows:
            data = bytes(item['content'])
            if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise KitError(f'{label}存储已损坏')
            if item['path'] in files:
                raise KitError(f'{label}含重复路径')
            files[item['path']] = data
        _bounded(files)
        if _asset_hash(files, self._candidate_spec(row)) != row['candidate_sha256']:
            raise KitError(f'{label}完整摘要不匹配')
        return files

    @staticmethod
    def _contract_ref(path: str) -> str:
        if path.startswith('contracts/'):
            rest = path[len('contracts/'):]
        elif path.startswith('.researchkit/contracts/'):
            rest = path[len('.researchkit/contracts/'):]
        else:
            raise KitError('契约候选路径无效')
        if not rest.endswith('.yaml') or '/' not in rest:
            raise KitError('契约候选路径无效')
        name, version_file = rest.rsplit('/', 1)
        return f'{name}@{version_file[:-5]}'

    def list_candidates(self, principal: Principal) -> list[dict[str, Any]]:
        _teacher(principal)
        with self.store._connection() as con: rows = con.execute('SELECT * FROM web_asset_candidates ORDER BY created_at DESC LIMIT 200').fetchall()
        return [self._candidate(row, include_files=False) for row in rows]

    def get_candidate(self, principal: Principal, candidate_id: str) -> dict[str, Any]:
        _teacher(principal); candidate_id = _uuid(candidate_id, '候选 ID')
        with self.store._connection() as con: row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        if row is None: raise KitError('候选不存在')
        return self._candidate(row, include_files=True)

    def review(self, principal: Principal, candidate_id: str, expected_sha256: str, notes: str, validation_run_id: str | None = None,
               *, connection=None, validated_evidence: bool = False) -> dict[str, Any]:
        _teacher(principal); candidate_id = _uuid(candidate_id, '候选 ID')
        if not isinstance(notes, str): raise KitError('审核摘要或说明无效')
        notes = notes.strip()
        if not isinstance(expected_sha256,str) or not _HASH.fullmatch(expected_sha256) or not 1 <= len(notes) <= 4000: raise KitError('审核摘要或说明无效')
        if connection is None:
            with self.store._connection() as con:
                row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        else:
            row = connection.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        if row is None: raise KitError('候选不存在')
        if row['candidate_sha256'] != expected_sha256: raise KitError('候选内容已变化或审核摘要不匹配')
        if row['status'] != 'draft': raise KitError('候选已审核或发布')
        self._candidate(row, include_files=True, connection=connection)
        if validation_run_id is not None and not validated_evidence:
            job = Jobs(self.store).get(principal, validation_run_id)
            if job['status'] != 'succeeded' or job['submission_id'] != row['submission_id'] or job['content_sha256'] != row['source_sha256']: raise KitError('验证运行不是该固定提交的成功记录')
        with self._write(connection) as con:
                changed = con.execute("UPDATE web_asset_candidates SET status='reviewed',review_notes=?,validation_run_id=? WHERE candidate_id=? AND status='draft' AND candidate_sha256=?",(notes,validation_run_id,candidate_id,expected_sha256)).rowcount
                if changed != 1: raise KitError('候选审核发生冲突')
                row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        return self._candidate(row, include_files=True, connection=connection)

    def prepare_central(self):
        """Run one-time central-library initialization before a caller transaction."""
        from .web_central_library import CentralLibrary
        if CentralLibrary.already_imported(self.store) or self.library_root.exists():
            return CentralLibrary(self.store, self.library_root)
        return None

    def publish(self, principal: Principal, candidate_id: str, expected_sha256: str, *, connection=None,
                central=None) -> dict[str, Any]:
        _teacher(principal); candidate_id = _uuid(candidate_id, '候选 ID')
        if not isinstance(expected_sha256,str) or not _HASH.fullmatch(expected_sha256): raise KitError('发布摘要无效')
        # CentralLibrary may perform its one-time source import.  It must run
        # before the publication write transaction, otherwise its own schema
        # transaction would contend with this connection under SQLite DELETE
        # journaling.
        if connection is None:
            central = self.prepare_central()
        with self._write(connection) as con:
                row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
                if row is None: raise KitError('候选不存在')
                if row['candidate_sha256'] != expected_sha256: raise KitError('候选内容已变化或发布摘要不匹配')
                from .web_cleanup import require_available
                require_available(con, row['kind'], row['ref'])
                for data_ref in json.loads(row['data_references_json']):
                    require_available(con, 'dataset', data_ref['dataset_id'])
                prior = con.execute('SELECT * FROM web_asset_published WHERE kind=? AND ref=?',(row['kind'],row['ref'])).fetchone()
                if prior is not None:
                    if prior['candidate_id'] != candidate_id: raise KitError('该资产版本已由其他候选发布，不能覆盖')
                    return self._published_metadata(row)
                if row['status'] != 'reviewed': raise KitError('候选必须先完成明确审核')
                files = con.execute('SELECT path,sha256,size,content FROM web_asset_candidate_files WHERE candidate_id=? ORDER BY path',(candidate_id,)).fetchall()
                if not files: raise KitError('候选文件不存在')
                self._materialize(row, files, '候选资产')
                self._installed_conflict(row['kind'], row['ref'], files, central)
                contract_rows = [f for f in files if f['path'].startswith('contracts/') and f['path'].endswith('.yaml')]
                if row['kind'] == 'template': contract_rows += [f for f in files if f['path'].startswith('.researchkit/contracts/') and f['path'].endswith('.yaml')]
                for item in contract_rows:
                    data = bytes(item['content'])
                    contract_ref = self._contract_ref(item['path'])
                    doc = _contract(data, contract_ref); sha = hashlib.sha256(data).hexdigest()
                    known = con.execute('SELECT contract_json FROM web_asset_contracts WHERE contract_ref=?',(contract_ref,)).fetchone()
                    if known is not None and json.loads(known['contract_json']) != doc: raise KitError(f'已发布契约定义冲突：{contract_ref}')
                    if known is None: con.execute('INSERT INTO web_asset_contracts(contract_ref,sha256,content,contract_json,candidate_id) VALUES (?,?,?,?,?)',(contract_ref,sha,data,_json(doc),candidate_id))
                con.execute('INSERT INTO web_asset_published(kind,ref,candidate_id,asset_id,published_at) VALUES (?,?,?,?,?)',(row['kind'],row['ref'],candidate_id,row['asset_id'],_now()))
                con.executemany('INSERT INTO web_asset_published_files(kind,ref,path,sha256,size,content) VALUES (?,?,?,?,?,?)',[(row['kind'],row['ref'],f['path'],f['sha256'],f['size'],bytes(f['content'])) for f in files])
                # Asset refs are private unless a later explicit grant says
                # otherwise.  Clear a stale policy so a removed/recreated
                # candidate cannot inherit old public access.
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_policies'").fetchone()
                if exists is not None:
                    con.execute('INSERT INTO component_policies(kind,ref,public) VALUES (?,?,0) ON CONFLICT(kind,ref) DO UPDATE SET public=0', (row['kind'], row['ref']))
                    con.execute('DELETE FROM grants WHERE kind=? AND ref=?', (row['kind'], row['ref']))
                con.execute("UPDATE web_asset_candidates SET status='published' WHERE candidate_id=?",(candidate_id,))
                row = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        return self._published_metadata(row)

    def _published_metadata(self, row: Any) -> dict[str, Any]:
        detail = self._candidate(row, include_files=False)
        return detail

    def _installed_conflict(self, kind: str, ref: str, files: list[Any], central=None) -> None:
        """Reject a differing exact central ref without reopening CLI files.

        A byte-identical old Web publication and builtin may coexist during an
        upgrade.  The central builtin is canonical for subsequent reads; a
        differing body always fails closed before publication is recorded.
        """
        # ``publish`` initialized this outside its writer transaction.  A
        # None value is the documented standalone/no-library case.
        if central is None:
            return
        candidate_files = {str(item['path']): bytes(item['content']) for item in files}
        installed = central.asset_files(kind, ref)
        if installed is not None and installed != candidate_files:
            raise KitError('中心内置组件使用相同精确版本但内容不同，不能覆盖')
        for item in files:
            path = item['path']
            if not (path.startswith('contracts/') or path.startswith('.researchkit/contracts/')) or not path.endswith('.yaml'):
                continue
            contract_ref = self._contract_ref(path)
            candidate = _contract(bytes(item['content']), contract_ref)
            known = central.contract(contract_ref)
            if known is not None and known[0] != candidate:
                raise KitError(f'中心内置契约定义冲突：{contract_ref}')

    def published_records(self) -> list[dict[str, Any]]:
        with self.store._connection() as con:
            rows = con.execute('SELECT c.* FROM web_asset_published p JOIN web_asset_candidates c ON c.candidate_id=p.candidate_id ORDER BY p.published_at').fetchall()
        result=[]
        for row in rows:
            validation=json.loads(row['static_validation_json'])
            result.append({'kind':row['kind'],'ref':row['ref'],'name':row['name'],'version':row['version'],'asset_id':row['asset_id'],'metadata':json.loads(row['metadata_json']),'contracts':validation.get('contracts',[])})
        return result

    def published_files(self, kind: str, ref: str, *, connection=None) -> dict[str, bytes] | None:
        if not isinstance(kind, str) or kind not in {'algorithm','template'}: raise KitError('资产类型无效')
        try: reference(ref)
        except KitError as exc: raise KitError('资产引用无效') from exc
        query = '''SELECT c.* FROM web_asset_published p
                   JOIN web_asset_candidates c ON c.candidate_id=p.candidate_id
                   WHERE p.kind=? AND p.ref=?'''
        if connection is None:
            with self.store._connection() as con:
                rows=con.execute('SELECT path,sha256,size,content FROM web_asset_published_files WHERE kind=? AND ref=? ORDER BY path',(kind,ref)).fetchall()
                candidate = con.execute(query, (kind, ref)).fetchone()
        else:
            rows=connection.execute('SELECT path,sha256,size,content FROM web_asset_published_files WHERE kind=? AND ref=? ORDER BY path',(kind,ref)).fetchall()
            candidate = connection.execute(query, (kind, ref)).fetchone()
        if not rows:
            if candidate is not None:
                raise KitError('已发布资产文件集已损坏')
            return None
        if candidate is None: raise KitError('已发布资产记录已损坏')
        return self._materialize(candidate, rows, '已发布资产')

    def published_contracts(self) -> dict[str, bytes]:
        with self.store._connection() as con:
            rows=con.execute('''SELECT DISTINCT x.contract_ref,x.sha256,x.content
                                FROM web_asset_contracts x
                                JOIN web_asset_published_files f
                                  ON f.path IN (
                                      'contracts/' || replace(x.contract_ref, '@', '/') || '.yaml',
                                      '.researchkit/contracts/' || replace(x.contract_ref, '@', '/') || '.yaml'
                                  )
                                JOIN web_asset_published p ON p.kind=f.kind AND p.ref=f.ref''').fetchall()
        result={}
        for row in rows:
            data=bytes(row['content'])
            if hashlib.sha256(data).hexdigest()!=row['sha256']: raise KitError('已发布契约存储已损坏')
            result[row['contract_ref']]=data
        return result
