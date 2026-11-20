"""Pure snapshot version planning for Web submissions.

This module deliberately knows nothing about a workspace, a database, or a
published library.  It compares two packed project snapshots and returns a
plan that a caller may present before it freezes the exact same bytes.  Keeping
it pure is important: a version suggestion must never inspect a mutable home
directory or make a formerly sealed runtime editable as a side effect.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

import yaml

from .files import KitError, identifier, reference


_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_CASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\Z")
_LOCK_PATH = '.researchkit/lock.json'
_PROTECTED_PREFIXES = ('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/')
_IGNORED_EXACT = {
    _LOCK_PATH, 'task', 'task.py', '.researchkit/local.json',
    '.researchkit/last-run.json', '.researchkit/run.lock',
    '.researchkit/bundle.json', '.researchkit/web-bundle.json',
    '.researchkit/review.json', '.researchkit/review.md', '_submission.json',
}
_IGNORED_PREFIXES = ('_runs/', 'results/', 'report/', 'cache/', '__pycache__/', '.pytest_cache/')
_MAX_FILES = 1_000
_MAX_TOTAL = 64 * 1024 * 1024


def _fail(message: str) -> KitError:
    return KitError(message)


def _relative(value: object, label: str = '项目文件路径') -> str:
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        raise _fail(f'{label} 必须是非空 POSIX 相对路径')
    if value.startswith('/') or value.startswith('//') or ':' in value.split('/', 1)[0]:
        raise _fail(f'{label} 不能是绝对路径')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise _fail(f'{label} 包含非法路径')
    try:
        if len(value.encode('utf-8')) > 4096:
            raise _fail(f'{label} 过长')
    except UnicodeEncodeError as exc:
        raise _fail(f'{label} 必须是 UTF-8') from exc
    return value


def _files(value: object, label: str = '项目快照') -> dict[str, bytes]:
    if type(value) is not dict or not value or len(value) > _MAX_FILES:
        raise _fail(f'{label} 必须是有限的非空 dict[str, bytes]')
    result: dict[str, bytes] = {}
    total = 0
    for path, data in value.items():
        path = _relative(path, f'{label}文件路径')
        if not isinstance(data, bytes):
            raise _fail(f'{label}文件必须是 bytes')
        total += len(data)
        if total > _MAX_TOTAL:
            raise _fail(f'{label}过大')
        result[path] = data
    return result


def _digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        name = path.encode('utf-8')
        digest.update(len(name).to_bytes(4, 'big'))
        digest.update(name)
        digest.update(len(files[path]).to_bytes(8, 'big'))
        digest.update(files[path])
    return digest.hexdigest()


def _yaml(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(data.decode('utf-8'))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise _fail(f'{label} 必须是 UTF-8 YAML 映射') from exc
    if not isinstance(value, dict):
        raise _fail(f'{label} 顶层必须是映射')
    return value


def _dump(value: dict[str, Any]) -> bytes:
    try:
        return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).encode('utf-8')
    except (TypeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise _fail('无法写入版本化项目配置') from exc


def _canonical_yaml(value: dict[str, Any]) -> bytes:
    """Stable enough for comparison while retaining arbitrary YAML values."""
    try:
        return yaml.safe_dump(value, sort_keys=True, allow_unicode=True).encode('utf-8')
    except (TypeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise _fail('项目 YAML 不能用于版本比较') from exc


def _is_ignored(path: str) -> bool:
    return (path in _IGNORED_EXACT or path.startswith(_IGNORED_PREFIXES)
            or '/_runs/' in path or '/results/' in path or '/report/' in path
            or '/cache/' in path or '/__pycache__/' in path)


def _semver(value: object, label: str = '算法版本') -> str:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        raise _fail(f'{label}必须是精确 x.y.z')
    return value


def _case_version(value: object, label: str = '案例版本') -> str:
    if not isinstance(value, str) or not _CASE_VERSION.fullmatch(value):
        raise _fail(f'{label}必须是精确 x.y')
    return value


def _bump_algorithm(version: str) -> str:
    major, minor, patch = (int(part) for part in _semver(version).split('.'))
    return f'{major}.{minor}.{patch + 1}'


def _bump_case(version: str, kind: str) -> str:
    major, minor = (int(part) for part in _case_version(version).split('.'))
    if kind == 'major':
        return f'{major + 1}.0'
    if kind != 'minor':
        raise _fail('案例版本建议类型只能是 minor 或 major')
    return f'{major}.{minor + 1}'


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(piece) for piece in version.split('.'))


def _lock(files: dict[str, bytes]) -> bytes:
    protected = {
        path: hashlib.sha256(data).hexdigest()
        for path, data in files.items()
        if path == 'task' or path.startswith(_PROTECTED_PREFIXES)
    }
    return (json.dumps({'schema': 'researchkit.lock/v1', 'files': dict(sorted(protected.items()))},
                       ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def _require_lock(files: dict[str, bytes]) -> None:
    try:
        current = json.loads(files[_LOCK_PATH].decode('utf-8'))
        expected = json.loads(_lock(files).decode('utf-8'))
    except (KeyError, UnicodeDecodeError, ValueError) as exc:
        raise _fail('固定依赖锁缺失或无效；请在 IDE 修复后再提交版本') from exc
    if current != expected:
        raise _fail('固定依赖锁已失效；不能把可能被篡改的运行时升级为可编辑算法')


def _under(path: str, root: str) -> bool:
    return path.startswith(root + '/')


def _plugin_contracts(meta: dict[str, Any]) -> tuple[str, ...]:
    refs: set[str] = set()
    for field in ('inputs', 'outputs'):
        values = meta.get(field, {})
        if not isinstance(values, dict):
            raise _fail(f'plugin.yaml 的 {field} 必须是映射')
        for item in values.values():
            if not isinstance(item, str):
                raise _fail('plugin.yaml 的契约引用必须是字符串')
            name, version = reference(item)
            refs.add(f'{name}@{version}')
    return tuple(sorted(refs))


def _normal_plugin_file(rel: str) -> bool:
    return not _is_ignored(rel)


def _canonical_plugin_files(files: dict[str, bytes], root: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path, data in files.items():
        if _under(path, root):
            rel = path[len(root) + 1:]
            if _normal_plugin_file(rel):
                result[rel] = data
    if 'plugin.yaml' not in result:
        raise _fail('项目插件目录缺少 plugin.yaml')
    meta = _yaml(result['plugin.yaml'], 'plugin.yaml')
    # Version labels do not make an algorithm implementation different.
    meta = copy.deepcopy(meta)
    meta['version'] = '__VERSION__'
    result['plugin.yaml'] = _canonical_yaml(meta)
    return result


def _descriptor(files: dict[str, bytes], project: dict[str, Any], alias: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        identifier(alias)
    except KitError as exc:
        raise _fail('项目插件别名无效') from exc
    path = _relative(config.get('path'), '插件路径')
    plugin_yaml = f'{path}/plugin.yaml'
    if plugin_yaml not in files:
        raise _fail(f'插件 {alias} 缺少 plugin.yaml')
    meta = _yaml(files[plugin_yaml], f'插件 {alias} 的 plugin.yaml')
    try:
        name = identifier(meta.get('name'))
    except KitError as exc:
        raise _fail(f'插件 {alias} 的名称无效') from exc
    version = _semver(meta.get('version'))
    contracts = _plugin_contracts(meta)
    canonical = _canonical_plugin_files(files, path)
    for ref in contracts:
        name_part, version_part = reference(ref)
        contract_path = f'.researchkit/contracts/{name_part}/{version_part}.yaml'
        data = files.get(contract_path)
        if data is None:
            raise _fail(f'插件 {alias} 缺少契约：{ref}')
        canonical[f'contracts/{name_part}/{version_part}.yaml'] = data
    return {
        'alias': alias, 'name': name, 'current_version': version, 'path': path,
        'contracts': list(contracts), 'fingerprint': _digest(canonical),
        '_canonical': canonical,
    }


def _project(files: dict[str, bytes]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _yaml(files.get('project.yaml', b''), 'project.yaml')
    if document.get('schema') != 'researchkit.project/v1':
        raise _fail('project.yaml 不是 ResearchKit 项目')
    plugins = document.get('plugins', {})
    if not isinstance(plugins, dict):
        raise _fail('project.yaml 的 plugins 必须是映射')
    descriptors: list[dict[str, Any]] = []
    roots: list[str] = []
    for alias in sorted(plugins):
        config = plugins[alias]
        if not isinstance(config, dict):
            raise _fail('项目插件配置必须是映射')
        row = _descriptor(files, document, alias, config)
        roots.append(row['path'])
        descriptors.append(row)
    for left in roots:
        for right in roots:
            if left != right and _under(left, right):
                raise _fail('项目插件路径不能相互包含')
    return document, descriptors


def _canonical_project(document: dict[str, Any]) -> bytes:
    document = copy.deepcopy(document)
    plugins = document.get('plugins', {})
    if isinstance(plugins, dict):
        for alias, config in plugins.items():
            if isinstance(config, dict):
                config['path'] = f'__PLUGIN_PATH__/{alias}'
    composition = document.get('web_composition')
    if isinstance(composition, dict) and isinstance(composition.get('steps'), list):
        for step in composition['steps']:
            if isinstance(step, dict) and isinstance(step.get('ref'), str) and '@' in step['ref']:
                name, _version = step['ref'].rsplit('@', 1)
                step['ref'] = name + '@__VERSION__'
    # A case submission version is storage metadata, not a second project YAML
    # version field.  Ignore legacy optional fields if a project happens to have
    # one, so merely applying a label does not require another submission.
    document.pop('case_version', None)
    return _canonical_yaml(document)


def _case_fingerprint(files: dict[str, bytes], document: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    roots = [row['path'] for row in rows]
    canonical: dict[str, bytes] = {}
    for path, data in files.items():
        if _is_ignored(path) or any(_under(path, root) for root in roots):
            continue
        if path == 'project.yaml':
            canonical[path] = _canonical_project(document)
        else:
            canonical[path] = data
    for row in rows:
        for rel, data in row['_canonical'].items():
            canonical[f"plugins/{row['alias']}/{rel}"] = data
    return _digest(canonical)


def _comparison(previous: dict[str, bytes] | None, baseline: dict[str, bytes] | None) -> tuple[dict[str, bytes] | None, str | None]:
    if previous is not None:
        return _files(previous, '上一固定提交'), 'previous'
    if baseline is not None:
        return _files(baseline, '领取基线'), 'baseline'
    return None, None


def _match_previous(current: dict[str, Any], old: list[dict[str, Any]]) -> dict[str, Any] | None:
    # An alias is a local handle, not an identity.  Reusing ``scale`` for a
    # different plugin must start a new algorithm instead of inheriting the
    # other plugin's release history.
    exact = [row for row in old if row['alias'] == current['alias'] and row['name'] == current['name']]
    if len(exact) == 1:
        return exact[0]
    # An alias rename may be a refactor.  It is safe to match by name only when
    # exactly one old plugin has that name; two aliases of one implementation
    # must not silently become each other.
    named = [row for row in old if row['name'] == current['name']]
    return named[0] if len(named) == 1 else None


def preview_versions(files: dict[str, bytes], previous: dict[str, bytes] | None,
                     baseline: dict[str, bytes] | None = None,
                     previous_version: str | None = None, kind: str = 'minor') -> dict[str, Any]:
    """Compare packed snapshots and propose case/algorithm versions.

    ``previous`` is the owner's immediately preceding fixed submission.
    ``baseline`` is used only when there is no previous submission, which lets a
    first submission from a claimed task compare with its immutable baseline.
    """
    current_files = _files(files)
    document, current_algorithms = _project(current_files)
    current_case = _case_fingerprint(current_files, document, current_algorithms)
    compared, compared_from = _comparison(previous, baseline)
    old_algorithms: list[dict[str, Any]] = []
    old_case: str | None = None
    old_revision: str | None = None
    if compared is not None:
        old_document, old_algorithms = _project(compared)
        old_case = _case_fingerprint(compared, old_document, old_algorithms)
        old_revision = _digest(compared)
    if previous_version is not None:
        previous_version = _case_version(previous_version)
    if kind not in {'minor', 'major'}:
        raise _fail('案例版本建议类型只能是 minor 或 major')
    # A formal version binds every packed byte, including small output files
    # and metadata. Even a change with identical algorithm semantics needs a
    # new case label; otherwise promotion would collide with its old version.
    case_changed = old_case is None or current_case != old_case or _digest(current_files) != old_revision
    # Formal submission versions use the store's major.minor sequence: the
    # first ordinary/minor submission is 0.1, while an explicitly major first
    # submission begins at 1.0.
    case_suggestion = (('1.0' if kind == 'major' else '0.1') if previous_version is None else
                       (_bump_case(previous_version, kind) if case_changed else previous_version))
    algorithms: list[dict[str, Any]] = []
    for row in current_algorithms:
        old = _match_previous(row, old_algorithms)
        changed = (old is None or row['fingerprint'] != old['fingerprint'] or
                   row['current_version'] != old['current_version'])
        previous_algorithm_version = old['current_version'] if old is not None else None
        if changed:
            suggested = _bump_algorithm(previous_algorithm_version) if previous_algorithm_version else row['current_version']
        else:
            suggested = previous_algorithm_version or row['current_version']
        algorithms.append({
            'alias': row['alias'], 'name': row['name'], 'path': row['path'],
            'current_version': row['current_version'], 'previous_version': previous_algorithm_version,
            'changed': changed, 'suggested_version': suggested,
            'contracts': list(row['contracts']), 'fingerprint': row['fingerprint'],
        })
    return {
        'schema': 'researchkit.web-version-plan/v1',
        # The current snapshot value is deliberately a single stable token so
        # the HTTP/UI caller can return it without reconstructing a summary.
        'snapshot_revision': _digest(current_files),
        'comparison': {'previous_revision': old_revision, 'source': compared_from},
        'case': {'current_version': previous_version, 'changed': case_changed,
                 'suggested_version': case_suggestion, 'fingerprint': current_case},
        'algorithms': algorithms,
    }


def _choices(plan: dict[str, Any], choices: object) -> tuple[str, dict[str, str]]:
    if not isinstance(choices, dict):
        raise _fail('版本选择必须是映射')
    case = plan.get('case')
    algorithms = plan.get('algorithms')
    if not isinstance(case, dict) or not isinstance(algorithms, list):
        raise _fail('版本计划无效')
    case_version = choices.get('case_version', case.get('suggested_version'))
    case_version = _case_version(case_version)
    # ``algorithm_versions`` is the public UI/SubmissionFlow spelling.  Keep
    # ``algorithms`` for callers that consumed the first pure-module draft.
    selection = choices.get('algorithm_versions', choices.get('algorithms', {}))
    if selection is None:
        selection = {}
    if not isinstance(selection, dict):
        raise _fail('算法版本选择必须是按别名的映射')
    result: dict[str, str] = {}
    for row in algorithms:
        if not isinstance(row, dict) or not isinstance(row.get('alias'), str):
            raise _fail('版本计划算法无效')
        alias = row['alias']
        selected = selection.get(alias, choices.get(alias, row.get('suggested_version')))
        selected = _semver(selected)
        old = row.get('previous_version')
        current = row.get('current_version')
        changed = row.get('changed')
        if not isinstance(changed, bool) or not isinstance(current, str):
            raise _fail('版本计划算法无效')
        if changed:
            if isinstance(old, str) and _version_key(selected) <= _version_key(_semver(old)):
                raise _fail(f'已变化算法 {alias} 必须使用高于原版本的新版本号')
            if old is None and _version_key(selected) < _version_key(_semver(current)):
                raise _fail(f'新算法 {alias} 不能使用低于当前声明的版本号')
        elif selected != current:
            raise _fail(f'未变化算法 {alias} 必须保持版本 {current}')
        result[alias] = selected
    unknown = set(selection) - set(result)
    if unknown:
        raise _fail('算法版本选择包含未知别名')
    return case_version, result


def _validate_plan(files: dict[str, bytes], plan: object) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(plan, dict) or plan.get('schema') != 'researchkit.web-version-plan/v1':
        raise _fail('版本计划无效')
    revision = plan.get('snapshot_revision')
    if not isinstance(revision, str) or revision != _digest(files):
        raise _fail('项目已变化；请重新比较版本')
    document, rows = _project(files)
    planned = plan.get('algorithms')
    if not isinstance(planned, list) or len(planned) != len(rows):
        raise _fail('版本计划与项目插件不一致')
    by_alias = {row['alias']: row for row in planned if isinstance(row, dict) and isinstance(row.get('alias'), str)}
    if len(by_alias) != len(rows):
        raise _fail('版本计划算法无效')
    for row in rows:
        candidate = by_alias.get(row['alias'])
        if candidate is None or any(candidate.get(key) != row[key] for key in ('name', 'path', 'current_version', 'fingerprint')):
            raise _fail('版本计划与当前算法不一致；请重新比较版本')
    return plan, document, rows


def _update_composition(document: dict[str, Any], versions: dict[str, str], rows: list[dict[str, Any]]) -> None:
    known = {row['alias']: row['name'] for row in rows}
    composition = document.get('web_composition')
    if not isinstance(composition, dict) or not isinstance(composition.get('steps'), list):
        return
    for step in composition['steps']:
        if not isinstance(step, dict) or not isinstance(step.get('alias'), str):
            continue
        alias = step['alias']
        if alias in versions:
            step['ref'] = f"{known[alias]}@{versions[alias]}"


def apply_versions(files: dict[str, bytes], plan: dict[str, Any], choices: dict[str, Any]) -> dict[str, bytes]:
    """Return a new snapshot with selected algorithm labels safely applied.

    The caller owns freezing the returned mapping.  No input mapping is mutated.
    """
    original = _files(files)
    plan, document, rows = _validate_plan(original, plan)
    _case, versions = _choices(plan, choices)
    desired = dict(original)
    changed_rows = [row for row in rows if versions[row['alias']] != row['current_version']]
    if any(row['path'].startswith('.researchkit/plugins/') for row in changed_rows):
        _require_lock(original)
    plugin_configs = document.get('plugins')
    assert isinstance(plugin_configs, dict)  # established by _project
    for row in changed_rows:
        alias, old_path, target_version = row['alias'], row['path'], versions[row['alias']]
        # Use original bytes instead of the canonical values when copying, so
        # source formatting and non-version metadata are retained.
        source_paths = {path: data for path, data in original.items()
                        if _under(path, old_path) and _normal_plugin_file(path[len(old_path) + 1:])}
        if old_path.startswith('.researchkit/plugins/'):
            new_path = f'algorithms/{alias}'
            if any(_under(path, new_path) or path == new_path for path in desired):
                raise _fail(f'可编辑算法目录已存在：{new_path}')
            for path, data in source_paths.items():
                desired[new_path + path[len(old_path):]] = data
            plugin_yaml = f'{new_path}/plugin.yaml'
            config = copy.deepcopy(plugin_configs[alias])
            config['path'] = new_path
            plugin_configs[alias] = config
        else:
            plugin_yaml = f'{old_path}/plugin.yaml'
        meta = _yaml(desired[plugin_yaml], f'插件 {alias} 的 plugin.yaml')
        # Re-validate identity; a selected version never changes an algorithm
        # name or turns a malformed directory into a publishable plugin.
        try:
            if identifier(meta.get('name')) != row['name']:
                raise _fail('算法名称在版本应用时发生变化')
        except KitError:
            raise _fail('算法名称在版本应用时无效')
        meta['version'] = target_version
        desired[plugin_yaml] = _dump(meta)
    if changed_rows:
        _update_composition(document, versions, rows)
        desired['project.yaml'] = _dump(document)
        # Recreate from the exact desired bytes.  The protected source has
        # already been validated before any editable copy was made.
        desired[_LOCK_PATH] = _lock(desired)
    return desired
