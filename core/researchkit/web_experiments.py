"""Descriptor-safe experiment schemes and IDE-produced artifact catalogues.

This module is deliberately only a file manager.  It never imports a case or
plugin, starts a runner, reads shared dataset bytes, or treats an experiment
record as proof that code was executed.  The common on-disk format is shared
with ``examples/three-cases/_shared/experiment_support.py``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

from .files import KitError
from .web_composer import Composer
from .web_workspace import MAX_EDIT_BYTES, Workspace, _relative


_SCHEMA = 'researchkit.experiment/v1'
_RESULT_SCHEMA = 'researchkit.experiment-result/v1'
_MAX_METADATA_BYTES = min(MAX_EDIT_BYTES, 1024 * 1024)
_MAX_VALUE_NODES = 2048
_MAX_DIRECTORY_ENTRIES = 16384
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
_TYPES = {'number', 'integer', 'string', 'boolean', 'mapping', 'sequence', 'any'}
# 已发出的两种标准入口；只允许显式更新这些准确内容，不覆盖研究者的自定义文件。
_LEGACY_HELPER_SHA256 = {
    '331484702a25f7f021f800d9d016c9dac7beb780086513eba8d8998c14ea8ad6',
    'f62e3fdfa8f6ceac42c8b5a5ff135f9cf99d5d8df6addee4022136c689395d4e',
}


def _helper_status(workspace, root_fd):
    try:
        data = _read_at(workspace, root_fd, 'experiment_support.py', '实验辅助文件')
    except (KitError, OSError):
        return {'result_retention': 'unknown', 'helper_update_available': False}
    current = Path(__file__).with_name('experiment_support.py').read_bytes()
    if data == current:
        return {'result_retention': 'latest-success', 'helper_update_available': False}
    old = _sha(data) in _LEGACY_HELPER_SHA256
    return {'result_retention': 'history' if old else 'custom', 'helper_update_available': old}


def _fail(message: str) -> KitError:
    return KitError(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scheme_id(value: object, label: str = '方案 ID') -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _fail(f'{label}只能包含字母、数字、点、下划线和连字符')
    return value


def _finite_json(value: Any, *, nodes: list[int] | None = None, depth: int = 0,
                 active: set[int] | None = None) -> None:
    """Reject recursive, non-finite and needlessly huge YAML values."""
    count = nodes if nodes is not None else [0]
    parents = active if active is not None else set()
    count[0] += 1
    if count[0] > _MAX_VALUE_NODES or depth > 32:
        raise _fail('方案参数过于复杂')
    if value is None or type(value) in (bool, int):
        return
    if isinstance(value, str):
        try:
            if len(value.encode('utf-8')) > _MAX_METADATA_BYTES:
                raise _fail('方案文本过长')
        except UnicodeEncodeError as exc:
            raise _fail('方案文本必须是 UTF-8') from exc
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _fail('方案不能包含 NaN 或无穷值')
        return
    if isinstance(value, (list, dict)):
        marker = id(value)
        if marker in parents:
            raise _fail('方案不能包含循环引用')
        parents.add(marker)
        try:
            iterable = value if isinstance(value, list) else value.items()
            for child in iterable:
                if isinstance(value, dict):
                    key, child = child
                    if not isinstance(key, str):
                        raise _fail('方案映射键必须是字符串')
                    _finite_json(key, nodes=count, depth=depth + 1, active=parents)
                _finite_json(child, nodes=count, depth=depth + 1, active=parents)
        finally:
            parents.remove(marker)
        return
    raise _fail('方案参数必须是有限 JSON 值')


def _yaml_mapping(data: bytes, label: str) -> dict[str, Any]:
    if len(data) > _MAX_METADATA_BYTES:
        raise _fail(f'{label}超过 1 MiB 上限')
    try:
        value = yaml.safe_load(data.decode('utf-8'))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise _fail(f'{label}不是有效 UTF-8 YAML') from exc
    if type(value) is not dict:
        raise _fail(f'{label}必须是映射')
    _finite_json(value)
    return value


def _json_mapping(data: bytes, label: str) -> dict[str, Any]:
    if len(data) > _MAX_METADATA_BYTES:
        raise _fail(f'{label}超过 1 MiB 上限')
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _fail(f'{label}不是有效 UTF-8 JSON') from exc
    if type(value) is not dict:
        raise _fail(f'{label}必须是映射')
    _finite_json(value)
    return value


def _path_parts(path: str) -> list[str]:
    return _relative(path).split('/')


def _safe_dir(parent_fd: int, name: str, *, missing: bool = False) -> int | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return None
        raise _fail('实验目录不存在') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise _fail('实验目录不安全')
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
    observed = os.fstat(descriptor)
    if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
        os.close(descriptor)
        raise _fail('实验目录在打开时发生变化')
    return descriptor


def _open_dir(root_fd: int, parts: list[str], *, missing: bool = False) -> int | None:
    descriptor = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            child = _safe_dir(descriptor, part, missing=missing and index == len(parts) - 1)
            if child is None:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular_info(parent_fd: int, name: str) -> os.stat_result:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _fail('实验文件必须是无链接的普通文件')
    return info


def _entry_names(descriptor: int) -> list[str]:
    names = sorted(os.listdir(descriptor))
    if len(names) > _MAX_DIRECTORY_ENTRIES:
        raise _fail('实验目录文件数过多')
    return names


def _root_current(workspace: Workspace, project_id: str, root_fd: int) -> None:
    before = os.fstat(root_fd)
    fresh = workspace._open_project_fd(project_id)
    try:
        after = os.fstat(fresh)
    finally:
        os.close(fresh)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise _fail('项目目录已被 IDE 替换；请刷新后重试')


def _read_at(workspace: Workspace, parent_fd: int, name: str, label: str) -> bytes:
    try:
        return workspace._read_regular_from_parent(parent_fd, name, limit=_MAX_METADATA_BYTES)[0]
    except KitError as exc:
        raise _fail(f'{label}无法安全读取：{exc}') from exc


def _check_parameter(rule: Any, value: Any, label: str) -> None:
    if type(rule) is not dict:
        raise _fail(f'{label} 的算法参数规则无效')
    kind = rule.get('type', 'any')
    checks = {
        'number': lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
        'integer': lambda x: isinstance(x, int) and not isinstance(x, bool),
        'string': lambda x: isinstance(x, str),
        'boolean': lambda x: isinstance(x, bool),
        'mapping': lambda x: isinstance(x, dict),
        'sequence': lambda x: isinstance(x, list),
        'any': lambda x: True,
    }
    if kind not in _TYPES or not checks[kind](value):
        raise _fail(f'{label} 参数类型无效')
    if 'enum' in rule and (not isinstance(rule['enum'], list) or value not in rule['enum']):
        raise _fail(f'{label} 不在允许值内')
    for key, outside in (('minimum', lambda x, bound: x < bound), ('maximum', lambda x, bound: x > bound)):
        if key in rule:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not isinstance(rule[key], (int, float)):
                raise _fail(f'{label} 的范围规则无效')
            if outside(value, rule[key]):
                raise _fail(f'{label} 超出允许范围')


def _plugin_rules_from_mapping(files: dict[str, bytes], warnings: list[str]) -> dict[str, dict[str, Any]]:
    """Read declarative plugin parameter rules, never plugin source code."""
    try:
        project = _yaml_mapping(files['project.yaml'], 'project.yaml')
    except (KeyError, KitError) as exc:
        warnings.append(f'无法读取项目算法参数规则：{exc}')
        return {}
    plugins = project.get('plugins', {})
    if not isinstance(plugins, dict):
        warnings.append('project.yaml 的 plugins 不是映射，方案算法参数未核对')
        return {}
    rules: dict[str, dict[str, Any]] = {}
    for alias, config in plugins.items():
        if not isinstance(alias, str) or not isinstance(config, dict) or not isinstance(config.get('path'), str):
            warnings.append('project.yaml 含无效算法配置，方案算法参数未完全核对')
            continue
        try:
            plugin_path = _relative(config['path']) + '/plugin.yaml'
            plugin = _yaml_mapping(files[plugin_path], f'{alias} 的 plugin.yaml')
            parameters = plugin.get('parameters', {})
            if not isinstance(parameters, dict):
                raise _fail('parameters 必须是映射')
            rules[alias] = parameters
        except (KeyError, KitError) as exc:
            warnings.append(f'无法读取算法 {alias} 的参数规则：{exc}')
    return rules


def _validate_document(document: Any, plugin_rules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if type(document) is not dict:
        raise _fail('方案必须是映射')
    expected = {'schema', 'name', 'description', 'case_parameters', 'algorithm_parameters'}
    if set(document) != expected or document.get('schema') != _SCHEMA:
        raise _fail('方案字段或 schema 无效')
    if not isinstance(document['name'], str) or not document['name'].strip() or len(document['name'].encode('utf-8')) > 400:
        raise _fail('方案名称无效')
    if not isinstance(document['description'], str) or len(document['description'].encode('utf-8')) > 8000:
        raise _fail('方案说明无效')
    if type(document['case_parameters']) is not dict or type(document['algorithm_parameters']) is not dict:
        raise _fail('方案参数必须是映射')
    _finite_json(document)
    for alias, values in document['algorithm_parameters'].items():
        if not isinstance(alias, str) or not isinstance(values, dict):
            raise _fail('算法方案参数必须是别名到映射')
        if alias not in plugin_rules:
            raise _fail(f'方案引用了未加入项目或无法核对的算法别名：{alias}')
        unknown = set(values) - set(plugin_rules[alias])
        if unknown:
            raise _fail(f'{alias} 含未声明参数：{sorted(unknown)}')
        for key, value in values.items():
            _check_parameter(plugin_rules[alias][key], value, f'{alias}.{key}')
    return document


def _document_from_bytes(data: bytes, plugin_rules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    document = _yaml_mapping(data, '方案 YAML')
    # IDE authors may omit these optional fields; normalize exactly as the
    # project-local launcher does, while still rejecting unknown keys.
    for key, value in (('description', ''), ('case_parameters', {}), ('algorithm_parameters', {})):
        document.setdefault(key, value)
    return _validate_document(document, plugin_rules)


def _file_row(path: str, info: os.stat_result) -> dict[str, Any]:
    return {'path': path, 'size': int(info.st_size), 'modified_ns': int(info.st_mtime_ns)}


def _walk_result_tree(descriptor: int, prefix: str, *, budget: list[int] | None = None) -> tuple[list[dict[str, Any]], dict[str, tuple[int, int, int, int, int, int]]]:
    rows: list[dict[str, Any]] = []
    identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    remaining = budget if budget is not None else [20000]

    def visit(fd: int, rel_prefix: str, depth: int = 0) -> None:
        if depth > 32:
            raise _fail('实验结果目录层级过深，请在 IDE 整理后刷新')
        for name in _entry_names(fd):
            remaining[0] -= 1
            if remaining[0] < 0:
                raise _fail('实验结果条目超过网页浏览上限，请在 IDE 整理后刷新')
            rel = f'{rel_prefix}/{name}' if rel_prefix else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise _fail(f'实验结果不能包含符号链接：{rel}')
            if stat.S_ISDIR(info.st_mode):
                if info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise _fail(f'实验结果目录不安全：{rel}')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                try:
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise _fail('实验结果目录在扫描时发生变化')
                    identities[rel + '/'] = _identity(current)
                    visit(child, rel, depth + 1)
                finally:
                    os.close(child)
            else:
                _regular_info(fd, name)
                identities[rel] = _identity(info)
                rows.append(_file_row(rel, info))

    identities['/'] = _identity(os.fstat(descriptor))
    visit(descriptor, prefix)
    return rows, identities


def _revision(identities: dict[str, tuple[int, int, int, int, int, int]]) -> str:
    digest = hashlib.sha256()
    for path in sorted(identities):
        digest.update(path.encode('utf-8'))
        for value in identities[path]:
            digest.update(int(value).to_bytes(16, 'big', signed=True))
    return digest.hexdigest()


def _read_project_rules(workspace: Workspace, root_fd: int, warnings: list[str]) -> dict[str, dict[str, Any]]:
    """Read only project.yaml and plugin.yaml through descriptors."""
    files: dict[str, bytes] = {}
    try:
        files['project.yaml'] = _read_at(workspace, root_fd, 'project.yaml', 'project.yaml')
        project = _yaml_mapping(files['project.yaml'], 'project.yaml')
        plugins = project.get('plugins', {})
        if not isinstance(plugins, dict):
            raise _fail('plugins 必须是映射')
        for alias, config in plugins.items():
            if not isinstance(alias, str) or not isinstance(config, dict) or not isinstance(config.get('path'), str):
                raise _fail('项目算法配置无效')
            path = _relative(config['path']) + '/plugin.yaml'
            parent_fd, name = workspace._open_parent_fd(root_fd, path)
            try:
                files[path] = _read_at(workspace, parent_fd, name, f'{alias} 的 plugin.yaml')
            finally:
                os.close(parent_fd)
    except KitError as exc:
        warnings.append(f'无法核对项目算法参数规则：{exc}')
        return {}
    return _plugin_rules_from_mapping(files, warnings)


def _catalog_schemes(workspace: Workspace, root_fd: int, plugin_rules: dict[str, dict[str, Any],], warnings: list[str]) -> list[dict[str, Any]]:
    descriptor = _open_dir(root_fd, ['experiments'], missing=True)
    if descriptor is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        for name in _entry_names(descriptor):
            try:
                info = _regular_info(descriptor, name)
            except KitError as exc:
                raise _fail(f'experiments/{name} 不安全：{exc}') from exc
            if not name.endswith('.yaml'):
                warnings.append(f'忽略非方案文件：experiments/{name}')
                continue
            try:
                scheme_id = _scheme_id(name[:-5])
                raw = _read_at(workspace, descriptor, name, f'方案 {scheme_id}')
                document = _document_from_bytes(raw, plugin_rules)
            except KitError as exc:
                warnings.append(f'无法读取方案 experiments/{name}：{exc}')
                continue
            rows.append({'id': scheme_id, 'document': document, 'sha256': _sha(raw),
                         'path': f'experiments/{name}', 'size': int(info.st_size)})
    finally:
        os.close(descriptor)
    return rows


def _catalog_results(workspace: Workspace, root_fd: int, warnings: list[str]):
    results_fd = _open_dir(root_fd, ['results'], missing=True)
    if results_fd is None:
        return [], []
    runs, all_files = [], []
    budget = [20000]
    try:
        for scheme_name in _entry_names(results_fd):
            if budget[0] <= 0:
                warnings.append('实验结果条目超过网页浏览上限；请在 IDE 整理后刷新')
                break
            budget[0] -= 1
            try:
                info = os.stat(scheme_name, dir_fd=results_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    _regular_info(results_fd, scheme_name)
                    all_files.append(_file_row(f'results/{scheme_name}', info))
                    continue
                scheme_id = _scheme_id(scheme_name, '结果方案 ID')
                scheme_fd = _safe_dir(results_fd, scheme_name)
                try:
                    for batch_name in _entry_names(scheme_fd):
                        if budget[0] <= 0:
                            break
                        budget[0] -= 1
                        try:
                            info = os.stat(batch_name, dir_fd=scheme_fd, follow_symlinks=False)
                            if not stat.S_ISDIR(info.st_mode):
                                _regular_info(scheme_fd, batch_name)
                                all_files.append(_file_row(f'results/{scheme_id}/{batch_name}', info))
                                continue
                            batch_id = _scheme_id(batch_name, '结果批次 ID')
                            batch_fd = _safe_dir(scheme_fd, batch_name)
                            try:
                                entries, identities = _walk_result_tree(batch_fd, f'results/{scheme_id}/{batch_id}', budget=budget)
                                record = None
                                try:
                                    candidate = _json_mapping(_read_at(workspace, batch_fd, 'experiment.json', '实验记录'), 'experiment.json')
                                    if candidate.get('schema') != _RESULT_SCHEMA:
                                        raise _fail('experiment.json schema 无效')
                                    record = candidate
                                except KitError as exc:
                                    warnings.append(f'结果 {scheme_id}/{batch_id} 的记录无法读取：{exc}')
                                runs.append({'scheme_id': scheme_id, 'batch_id': batch_id, 'record': record,
                                             'files': entries, 'revision': _revision(identities)})
                                all_files.extend(entries)
                            finally:
                                os.close(batch_fd)
                        except (KitError, OSError) as exc:
                            warnings.append(f'跳过无法安全读取的结果 {scheme_id}/{batch_name}：{exc}')
                finally:
                    os.close(scheme_fd)
            except (KitError, OSError) as exc:
                warnings.append(f'跳过无法安全读取的结果目录 {scheme_name}：{exc}')
    finally:
        os.close(results_fd)
    return runs, all_files


def _defaults(document, rules):
    defaults = {'case_parameters': {}, 'algorithm_parameters': {}}
    if type(document) is not dict:
        return defaults
    if type(document.get('parameters')) is dict:
        defaults['case_parameters'] = document['parameters']
    for alias, parameters in rules.items():
        values = {key: rule['default'] for key, rule in parameters.items()
                  if type(rule) is dict and 'default' in rule}
        cfg = document.get('plugins', {}).get(alias, {})
        if type(cfg) is dict and type(cfg.get('parameters')) is dict:
            values.update(cfg['parameters'])
        defaults['algorithm_parameters'][alias] = values
    _finite_json(defaults)
    return defaults


def catalog(workspace: Workspace, pid: str) -> dict[str, Any]:
    """List this principal's schemes and IDE-written result files as metadata."""
    warnings: list[str] = []
    with workspace._project_guard(pid):
        root_fd = workspace._open_project_fd(pid)
        try:
            plugin_rules = _read_project_rules(workspace, root_fd, warnings)
            project = _yaml_mapping(_read_at(workspace, root_fd, 'project.yaml', '项目配置'), 'project.yaml')
            defaults = _defaults(project, plugin_rules)
            try:
                _regular_info(root_fd, 'run_experiment.py')
                has_runner = True
            except (KitError, OSError):
                has_runner = False
            schemes = _catalog_schemes(workspace, root_fd, plugin_rules, warnings)
            runs, result_files = _catalog_results(workspace, root_fd, warnings)
            helper_status = _helper_status(workspace, root_fd)
            _root_current(workspace, pid, root_fd)
        finally:
            os.close(root_fd)
    return {'project_id': pid, 'schemes': schemes, 'runs': runs, 'result_files': result_files,
            'warnings': warnings, 'has_runner': has_runner, 'defaults': defaults, 'algorithm_rules': plugin_rules,
            **helper_status}


def _scheme_path(scheme_id: str) -> str:
    return f'experiments/{_scheme_id(scheme_id)}.yaml'


def _payload(payload: Any, keys: set[str]) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != keys:
        raise _fail('实验方案请求字段无效')
    return payload


def save(workspace: Workspace, pid: str, payload: Any) -> dict[str, Any]:
    """CAS save one scheme, never changing source code or project.yaml."""
    payload = _payload(payload, {'id', 'document', 'expected_sha256'})
    scheme_id = _scheme_id(payload['id'])
    expected = payload['expected_sha256']
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
        raise _fail('expected_sha256 必须是当前方案的 SHA-256，或 null 用于新建')
    _finite_json(payload['document'])
    try:
        encoded = yaml.safe_dump(payload['document'], allow_unicode=True, sort_keys=False).encode('utf-8')
    except (TypeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise _fail('方案无法编码为 YAML') from exc
    if len(encoded) > _MAX_METADATA_BYTES:
        raise _fail('方案超过 1 MiB 上限')
    path = _scheme_path(scheme_id)
    with workspace._project_guard(pid), workspace._file_lock(pid, path):
        root_fd = workspace._open_project_fd(pid)
        try:
            warnings: list[str] = []
            rules = _read_project_rules(workspace, root_fd, warnings)
            document = _validate_document(payload['document'], rules)
            parent_fd, name = Composer(workspace)._parent(root_fd, path, create=True)
            try:
                current: bytes | None
                try:
                    current = _read_at(workspace, parent_fd, name, f'方案 {scheme_id}')
                except KitError as exc:
                    if str(exc).endswith('文件不存在') or '文件不存在' in str(exc):
                        current = None
                    else:
                        raise
                if expected is None:
                    if current is not None:
                        raise _fail('方案已存在；请刷新后再保存或改用新方案 ID')
                else:
                    if current is None or _sha(current) != expected:
                        raise _fail('方案已被 IDE 或其他编辑器修改；请刷新后重试')
                # Validate the canonical bytes as well; this catches YAML's
                # representational edge cases before the descriptor write.
                _document_from_bytes(encoded, rules)
                Composer(workspace)._write_file(pid, root_fd, path, encoded, current)
                _root_current(workspace, pid, root_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)
    return {'id': scheme_id, 'document': document, 'sha256': _sha(encoded), 'path': path, 'size': len(encoded)}


def delete(workspace: Workspace, pid: str, payload: Any) -> dict[str, Any]:
    """Delete only one scheme file.  Existing result batches are retained."""
    payload = _payload(payload, {'id', 'expected_sha256'})
    scheme_id = _scheme_id(payload['id'])
    expected = payload['expected_sha256']
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise _fail('删除方案必须提供当前 SHA-256')
    path = _scheme_path(scheme_id)
    with workspace._project_guard(pid), workspace._file_lock(pid, path):
        root_fd = workspace._open_project_fd(pid)
        try:
            parent_fd, name = workspace._open_parent_fd(root_fd, path)
            try:
                current = _read_at(workspace, parent_fd, name, f'方案 {scheme_id}')
                if _sha(current) != expected:
                    raise _fail('方案已被 IDE 或其他编辑器修改；请刷新后重试')
                Composer(workspace)._delete_file(pid, root_fd, path, current)
                _root_current(workspace, pid, root_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)
    return {'id': scheme_id, 'deleted': True, 'results_retained': True}


def _batch_snapshot(root_fd: int, scheme_id: str, batch_id: str) -> tuple[int, int, list[dict[str, Any]], dict[str, tuple[int, int, int, int, int, int]]]:
    results_fd = _open_dir(root_fd, ['results'])
    assert results_fd is not None
    try:
        scheme_fd = _safe_dir(results_fd, scheme_id)
        assert scheme_fd is not None
        try:
            batch_fd = _safe_dir(scheme_fd, batch_id)
            assert batch_fd is not None
            try:
                files, identities = _walk_result_tree(batch_fd, f'results/{scheme_id}/{batch_id}')
                return results_fd, scheme_fd, files, identities
            finally:
                os.close(batch_fd)
        except Exception:
            os.close(scheme_fd)
            raise
    except Exception:
        os.close(results_fd)
        raise


def _unlink_batch(parent_fd: int, name: str, expected: dict[str, tuple[int, int, int, int, int, int]], prefix: str) -> None:
    """Descriptor-confined removal after a fresh full metadata comparison."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail('结果批次已被替换；请刷新后重试')
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
    try:
        current = os.fstat(descriptor)
        if expected.get('/') != _identity(current):
            raise _fail('结果批次已被 IDE 修改；请刷新后重试')

        def remove(fd: int, rel_prefix: str) -> None:
            for child in _entry_names(fd):
                rel = f'{rel_prefix}/{child}' if rel_prefix else child
                child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(child_info.st_mode):
                    raise _fail('结果批次含符号链接，拒绝删除')
                if stat.S_ISDIR(child_info.st_mode):
                    child_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                    try:
                        observed = os.fstat(child_fd)
                        if expected.get(rel + '/') != _identity(observed):
                            raise _fail('结果批次已被 IDE 修改；请刷新后重试')
                        remove(child_fd, rel)
                    finally:
                        os.close(child_fd)
                    os.rmdir(child, dir_fd=fd)
                else:
                    if expected.get(rel) != _identity(child_info) or not stat.S_ISREG(child_info.st_mode) or child_info.st_nlink != 1:
                        raise _fail('结果批次已被 IDE 修改；请刷新后重试')
                    # Immediate re-stat mirrors the workspace CAS delete rule.
                    final = os.stat(child, dir_fd=fd, follow_symlinks=False)
                    if expected.get(rel) != _identity(final):
                        raise _fail('结果批次已被 IDE 修改；请刷新后重试')
                    os.unlink(child, dir_fd=fd)

        remove(descriptor, prefix)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def delete_result(workspace: Workspace, pid: str, payload: Any) -> dict[str, Any]:
    """Remove one IDE-produced result batch after a metadata revision check."""
    payload = _payload(payload, {'scheme_id', 'batch_id', 'confirmation', 'expected_revision'})
    scheme_id = _scheme_id(payload['scheme_id'], '结果方案 ID')
    batch_id = _scheme_id(payload['batch_id'], '结果批次 ID')
    if payload['confirmation'] != f'{scheme_id}/{batch_id}':
        raise _fail('请在 confirmation 中准确填写 “方案 ID/批次 ID”')
    expected = payload['expected_revision']
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise _fail('删除结果必须提供当前 revision')
    lock_path = f'results/{scheme_id}/{batch_id}'
    with workspace._project_guard(pid), workspace._file_lock(pid, lock_path):
        root_fd = workspace._open_project_fd(pid)
        try:
            results_fd, scheme_fd, _files, identities = _batch_snapshot(root_fd, scheme_id, batch_id)
            try:
                if _revision(identities) != expected:
                    raise _fail('结果批次已被 IDE 修改；请刷新后重试')
                batch_fd = _safe_dir(scheme_fd, batch_id)
                try:
                    try:
                        metadata = _json_mapping(_read_at(workspace, batch_fd, 'experiment.json', '实验状态'), '实验状态')
                    except KitError as exc:
                        raise _fail(f'无法确认实验已停止，请先在 IDE 核对：{exc}') from exc
                    if metadata.get('status') == 'running':
                        raise _fail('实验仍在运行；请先在 IDE 中结束实验，再删除该批产物')
                finally:
                    os.close(batch_fd)
                # A second descriptor scan immediately precedes the deletion.
                _r2, _s2, _files2, observed = _batch_snapshot(root_fd, scheme_id, batch_id)
                try:
                    if observed != identities:
                        raise _fail('结果批次已被 IDE 修改；请刷新后重试')
                finally:
                    os.close(_s2)
                    os.close(_r2)
                _root_current(workspace, pid, root_fd)
                _unlink_batch(scheme_fd, batch_id, identities, f'results/{scheme_id}/{batch_id}')
                _root_current(workspace, pid, root_fd)
            finally:
                os.close(scheme_fd)
                os.close(results_fd)
        finally:
            os.close(root_fd)
    return {'scheme_id': scheme_id, 'batch_id': batch_id, 'deleted': True}


def summarize_snapshot(files: dict[str, bytes]) -> dict[str, Any]:
    """Pure parser for an already-authorized immutable submission file map.

    No ownership decision is made here.  Callers must first authorize access to
    the fixed submission.  Records are descriptive user-environment metadata,
    never a platform execution proof.
    """
    if type(files) is not dict:
        raise _fail('固定内容必须是文件映射')
    normalized: dict[str, bytes] = {}
    for path, data in files.items():
        if not isinstance(path, str) or not isinstance(data, (bytes, bytearray)):
            raise _fail('固定内容文件映射无效')
        normalized[_relative(path)] = bytes(data)
    warnings: list[str] = []
    rules = _plugin_rules_from_mapping(normalized, warnings)
    schemes: list[dict[str, Any]] = []
    runs_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
    result_files: list[dict[str, Any]] = []
    for path, data in sorted(normalized.items()):
        parts = path.split('/')
        if len(parts) == 2 and parts[0] == 'experiments' and parts[1].endswith('.yaml'):
            try:
                scheme_id = _scheme_id(parts[1][:-5])
                document = _document_from_bytes(data, rules)
                schemes.append({'id': scheme_id, 'document': document, 'sha256': _sha(data), 'path': path, 'size': len(data)})
            except KitError as exc:
                warnings.append(f'无法读取方案 {path}：{exc}')
        if len(parts) >= 2 and parts[0] == 'results':
            if len(parts) < 4:
                result_files.append({'path': path, 'size': len(data)})
                continue
            try:
                scheme_id = _scheme_id(parts[1], '结果方案 ID')
                batch_id = _scheme_id(parts[2], '结果批次 ID')
            except KitError as exc:
                warnings.append(f'忽略无效结果路径 {path}：{exc}')
                continue
            row = {'path': path, 'size': len(data)}
            result_files.append(row)
            runs_map.setdefault((scheme_id, batch_id), []).append(row)
    runs: list[dict[str, Any]] = []
    for (scheme_id, batch_id), rows in sorted(runs_map.items()):
        record = None
        target = f'results/{scheme_id}/{batch_id}/experiment.json'
        if target in normalized:
            try:
                candidate = _json_mapping(normalized[target], 'experiment.json')
                if candidate.get('schema') != _RESULT_SCHEMA:
                    raise _fail('experiment.json schema 无效')
                record = candidate
            except KitError as exc:
                warnings.append(f'结果 {scheme_id}/{batch_id} 的 experiment.json 无法读取：{exc}')
        else:
            warnings.append(f'结果 {scheme_id}/{batch_id} 缺少 experiment.json')
        digest = hashlib.sha256()
        for row in rows:
            digest.update(row['path'].encode('utf-8'))
            digest.update(normalized[row['path']])
        runs.append({'scheme_id': scheme_id, 'batch_id': batch_id, 'record': record,
                     'files': rows, 'revision': digest.hexdigest()})
    return {'schemes': schemes, 'runs': runs, 'result_files': result_files,
            'warnings': warnings, 'has_runner': 'run_experiment.py' in normalized,
            'defaults': _defaults(_yaml_mapping(normalized.get('project.yaml', b'{}'), 'project.yaml'), rules),
            'algorithm_rules': rules}


def update_helper(workspace: Workspace, pid: str) -> dict[str, Any]:
    """只在用户明确点击后升级标准辅助文件，不改案例、方案、锁或产物。"""
    helper = Path(__file__).with_name('experiment_support.py').read_bytes()
    with workspace._project_guard(pid):
        composer = Composer(workspace)
        composer.recover(pid)
        root_fd = workspace._open_project_fd(pid)
        try:
            try:
                _regular_info(root_fd, 'run_experiment.py')
            except FileNotFoundError as exc:
                raise _fail('项目实验入口不完整，请先在 IDE 核对 run_experiment.py') from exc
            before = _read_at(workspace, root_fd, 'experiment_support.py', '实验辅助文件')
            if before == helper:
                return {'updated': False}
            if _sha(before) not in _LEGACY_HELPER_SHA256:
                raise _fail('实验辅助文件有自定义修改，未覆盖；请先在 IDE 核对后手动适配')
            changes = [('experiment_support.py', before, helper)]
            composer._write_journal(pid, changes)
            composer._write_file(pid, root_fd, 'experiment_support.py', helper, before)
            _root_current(workspace, pid, root_fd)
            composer._remove_journal(pid)
        finally:
            os.close(root_fd)
    return {'updated': True}


def enable(workspace: Workspace, pid: str) -> dict[str, Any]:
    """Add the optional IDE launcher without modifying the declared case entry."""
    launcher = b'''from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / ".researchkit" / "runtime"))
sys.path.insert(0, str(ROOT))
from experiment_support import main
if __name__ == "__main__":
    raise SystemExit(main(ROOT))
'''
    helper = Path(__file__).with_name('experiment_support.py').read_bytes()
    with workspace._project_guard(pid):
        composer = Composer(workspace)
        composer.recover(pid)
        root_fd = workspace._open_project_fd(pid)
        changes = []
        try:
            project = _yaml_mapping(_read_at(workspace, root_fd, 'project.yaml', '项目配置'), '项目配置')
            if not str(project.get('entry', 'case.py')).endswith('.py'):
                raise _fail('实验入口目前支持 Python 案例')
            for path, data in [('experiment_support.py', helper), ('run_experiment.py', launcher)]:
                try:
                    _regular_info(root_fd, path)
                except FileNotFoundError:
                    changes.append((path, None, data))
                else:
                    # Existing researcher files are never replaced. If both
                    # files already exist they are the project's own adapter.
                    pass
            if not changes:
                return {'enabled': True, 'created': []}
            if len(changes) != 2:
                raise _fail('项目已含部分实验入口文件；请在 IDE 核对 run_experiment.py 和 experiment_support.py，避免覆盖现有代码')
            composer._write_journal(pid, changes)
            for path, before, data in changes:
                composer._write_file(pid, root_fd, path, data, before)
            _root_current(workspace, pid, root_fd)
            composer._remove_journal(pid)
        finally:
            os.close(root_fd)
    return {'enabled': True, 'created': [change[0] for change in changes]}
