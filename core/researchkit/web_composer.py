"""Static, Web-facing project composition.

This module deliberately reads plugin metadata and source files as data.  It
does not import a plugin, run an entry point, or inspect a teacher's private
workspace.  ``Workspace._project_guard`` serializes its public operations with
ordinary Web editing; the small journal below additionally makes an interrupted
multi-file update recoverable without overwriting a later IDE edit.
"""
from __future__ import annotations

import base64
import ctypes
import errno
import re
import hashlib
import json
import math
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, identifier, reference
from .model import check_type, validate_parameters
from .web_store import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from .web_workspace import MAX_EDIT_BYTES, Workspace, _relative, _secure_directory


_SCHEMA = 'researchkit.web-composition/v1'
_JOURNAL_SCHEMA = 'researchkit.web-composition-journal/v1'
_MAX_STEPS = 32
_MAX_VALUE_NODES = 2048
_TYPES = {'number', 'integer', 'string', 'boolean', 'mapping', 'sequence', 'any'}
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.renameat2
    except AttributeError as exc:
        raise _fail('当前系统不支持安全创建编排文件') from exc
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(parent_fd, source.encode(), parent_fd, destination.encode(), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise _fail('项目冲突：编排新文件已被 IDE 创建；恢复记录已保留')
        raise OSError(code, os.strerror(code))


def _fail(message: str) -> KitError:
    return KitError(message)


def _revision(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        encoded = path.encode('utf-8')
        digest.update(len(encoded).to_bytes(4, 'big'))
        digest.update(encoded)
        digest.update(len(files[path]).to_bytes(8, 'big'))
        digest.update(files[path])
    return digest.hexdigest()


def _json_value(value: Any, *, nodes: list[int] | None = None, depth: int = 0, active: set[int] | None = None) -> None:
    """Accept a finite, bounded, acyclic JSON subset without recursion leaks."""
    count = nodes if nodes is not None else [0]
    stack = active if active is not None else set()
    count[0] += 1
    if count[0] > _MAX_VALUE_NODES or depth > 32:
        raise _fail('参数或输入值过于复杂')
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            try:
                if len(value.encode('utf-8')) > MAX_EDIT_BYTES:
                    raise _fail('字符串过长')
            except UnicodeEncodeError as exc:
                raise _fail('字符串必须是有效 UTF-8') from exc
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail('参数和输入不能使用 NaN 或无穷值')
        return
    if isinstance(value, (list, dict)):
        marker = id(value)
        if marker in stack:
            raise _fail('参数和输入不能包含循环引用')
        stack.add(marker)
        try:
            if isinstance(value, list):
                for item in value:
                    _json_value(item, nodes=count, depth=depth + 1, active=stack)
            else:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _fail('映射键必须是有限长度字符串')
                    _json_value(key, nodes=count, depth=depth + 1, active=stack)
                    if len(key.encode('utf-8')) > 200:
                        raise _fail('映射键必须是有限长度字符串')
                    _json_value(item, nodes=count, depth=depth + 1, active=stack)
        finally:
            stack.remove(marker)
        return
    raise _fail('参数和输入必须是 JSON 值')

def _yaml_bytes(value: dict[str, Any]) -> bytes:
    try:
        text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    except (TypeError, ValueError) as exc:
        raise _fail('无法写入项目配置') from exc
    return text.encode('utf-8')


class Composer:
    """Read and update the declarative Web composition of one Workspace."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._journal_root = workspace.root / '.researchkit-web-composer-journals'

    # Shared components are served by the UID-authenticated broker.
    def catalog(self) -> dict[str, Any]:
        return self.workspace.store.request('get_component_catalog')

    def _plugin_source(self, ref: str) -> tuple[dict[str, Any], dict[str, bytes], str]:
        result = self.workspace.store.request('get_shared_component', ref=ref)
        files = {path[7:]: content for path, content in result['files'].items() if path.startswith('plugin/')}
        return result['metadata'], files, _revision(files)

    def _contract_source(self, ref: str) -> tuple[dict[str, Any], bytes]:
        result = self.workspace.store.request('get_shared_contract', ref=ref)
        return result['metadata'], result['files']['contract.yaml']

    def _safe_yaml(self, data: bytes, label: str) -> dict[str, Any]:
        try:
            document = yaml.safe_load(data.decode('utf-8'))
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            raise _fail(f'{label} 必须是 UTF-8 YAML 映射') from exc
        if not isinstance(document, dict):
            raise _fail(f'{label} 顶层必须是映射')
        return document

    # Project parsing and validation ------------------------------------
    def _snapshot(self, project_id: str) -> tuple[Path, dict[str, bytes]]:
        root, files, _references = self.workspace._snapshot(project_id)
        return root, files

    def _project_document(self, files: dict[str, bytes]) -> dict[str, Any]:
        if 'project.yaml' not in files:
            raise _fail('项目缺少 project.yaml')
        document = self._safe_yaml(files['project.yaml'], 'project.yaml')
        if document.get('schema') != 'researchkit.project/v1':
            raise _fail('project.yaml 不是 ResearchKit 项目')
        if document.get('mode') != 'python' or document.get('entry', 'case.py') != 'case.py':
            raise _fail('Web 编排目前只支持以 case.py 为入口的 Python 项目')
        if not isinstance(document.get('plugins', {}), dict) or not isinstance(document.get('parameters', {}), dict):
            raise _fail('项目插件或参数配置无效')
        _json_value(document.get('parameters', {}))
        return document

    def _composition(self, document: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
        value = document.get('web_composition')
        if value is None:
            return False, [], []
        if not isinstance(value, dict) or value.get('schema') != _SCHEMA:
            raise _fail('Web 编排记录格式无效')
        steps, drafts = value.get('steps'), value.get('draft_contracts')
        if not isinstance(steps, list) or not isinstance(drafts, list):
            raise _fail('Web 编排记录格式无效')
        _json_value(steps)
        _json_value(drafts)
        for step in steps:
            if (not isinstance(step, dict) or set(step) != {'alias', 'ref', 'parameters', 'inputs'}
                    or not isinstance(step['parameters'], dict) or not isinstance(step['inputs'], dict)):
                raise _fail('Web 编排步骤格式无效')
            identifier(step['alias'])
            reference(step['ref'])
            for source in step['inputs'].values():
                if not isinstance(source, dict) or not isinstance(source.get('kind'), str):
                    raise _fail('Web 编排输入格式无效')
                kind = source['kind']
                if kind == 'literal' and set(source) == {'kind', 'value'}:
                    continue
                if kind == 'parameter' and set(source) == {'kind', 'name'} and isinstance(source['name'], str):
                    continue
                if (kind == 'output' and set(source) == {'kind', 'step', 'port'}
                        and isinstance(source['step'], str) and isinstance(source['port'], str)):
                    continue
                raise _fail('Web 编排输入格式无效')
        for draft in drafts:
            if (not isinstance(draft, dict) or set(draft) != {'id', 'type', 'description'}
                    or not all(isinstance(draft[field], str) for field in ('id', 'type', 'description'))):
                raise _fail('Web 草稿契约格式无效')
            reference(draft['id'])
            if draft['type'] not in _TYPES:
                raise _fail('Web 草稿契约类型无效')
        return True, steps, drafts

    def _read_result(self, project_id: str, files: dict[str, bytes]) -> dict[str, Any]:
        document = self._project_document(files)
        managed, steps, drafts = self._composition(document)
        entry = 'case.py'
        raw = files.get(entry)
        if raw is None:
            raise _fail('项目缺少 case.py')
        if len(raw) > MAX_EDIT_BYTES:
            raise _fail('case.py 超过 Web 查看上限')
        try:
            entry_text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise _fail('case.py 必须为 UTF-8') from exc
        return {'project_id': project_id, 'revision': _revision(files), 'parameters': document.get('parameters', {}),
                'steps': steps, 'draft_contracts': drafts, 'entry': entry, 'entry_text': entry_text,
                'managed': managed}

    def read(self, project_id: str) -> dict[str, Any]:
        self.recover(project_id)
        _root, files = self._snapshot(project_id)
        return self._read_result(project_id, files)

    def _drafts(self, values: Any) -> tuple[list[dict[str, Any]], dict[str, bytes], dict[str, dict[str, Any]]]:
        if not isinstance(values, list) or len(values) > 64:
            raise _fail('草稿契约必须是有限列表')
        rows: list[dict[str, Any]] = []
        data: dict[str, bytes] = {}
        definitions: dict[str, dict[str, Any]] = {}
        for raw in values:
            if not isinstance(raw, dict) or set(raw) != {'id', 'type', 'description'}:
                raise _fail('草稿契约字段无效')
            ref, kind, description = raw['id'], raw['type'], raw['description']
            if not isinstance(ref, str) or not isinstance(kind, str):
                raise _fail('草稿契约 ID 或类型无效')
            try:
                name, version = reference(ref)
            except KitError as exc:
                raise _fail('草稿契约 ID 必须为名称@x.y.z') from exc
            if kind not in _TYPES or not isinstance(description, str) or len(description.encode('utf-8')) > 4000:
                raise _fail('草稿契约类型或描述无效')
            if ref in definitions:
                raise _fail(f'草稿契约重复：{ref}')
            doc = {'schema': 'hydro.contract/v1', 'id': ref, 'type': kind, 'description': description}
            definitions[ref] = doc
            data[f'.researchkit/contracts/{name}/{version}.yaml'] = _yaml_bytes(doc)
            rows.append({'id': ref, 'type': kind, 'description': description})
        if rows:
            self.workspace.store.request('validate_draft_contracts', contracts=rows)
        return rows, data, definitions

    def _validate_payload(self, payload: Any, document: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes], dict[str, str]]:
        if not isinstance(payload, dict) or set(payload) != {'expected_revision', 'parameters', 'steps', 'draft_contracts', 'generate_entry'}:
            raise _fail('编排请求字段无效')
        _json_value(payload)
        if not isinstance(payload['expected_revision'], str) or len(payload['expected_revision']) != 64:
            raise _fail('缺少当前项目版本；请刷新后重试')
        if not isinstance(payload['parameters'], dict):
            raise _fail('项目参数必须是映射')
        _json_value(payload['parameters'])
        if not isinstance(payload['generate_entry'], bool):
            raise _fail('generate_entry 必须为布尔值')
        raw_steps = payload['steps']
        if not isinstance(raw_steps, list) or len(raw_steps) > _MAX_STEPS:
            raise _fail(f'步骤数不得超过 {_MAX_STEPS}')
        drafts, draft_bytes, draft_defs = self._drafts(payload['draft_contracts'])
        steps: list[dict[str, Any]] = []
        plugin_paths: dict[str, str] = {}
        sources: dict[str, tuple[dict[str, Any], dict[str, bytes], str]] = {}
        seen: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        deps: dict[str, bytes] = dict(draft_bytes)
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict) or set(raw) != {'alias', 'ref', 'parameters', 'inputs'}:
                raise _fail('步骤字段无效')
            alias, ref, parameters, inputs = raw['alias'], raw['ref'], raw['parameters'], raw['inputs']
            if not isinstance(ref, str):
                raise _fail('算法引用无效')
            try:
                identifier(alias)
            except KitError as exc:
                raise _fail('步骤别名无效') from exc
            if alias in seen:
                raise _fail(f'步骤别名重复：{alias}')
            if not isinstance(parameters, dict) or not isinstance(inputs, dict):
                raise _fail(f'{alias} 的参数或输入必须是映射')
            _json_value(parameters)
            if ref not in sources:
                sources[ref] = self._plugin_source(ref)
            meta, plugin_files, content_hash = sources[ref]
            effective_parameters = {key: rule['default'] for key, rule in meta['parameters'].items()
                                    if isinstance(rule, dict) and 'default' in rule}
            effective_parameters.update(parameters)
            try:
                validate_parameters(meta['parameters'], effective_parameters, alias)
            except KitError as exc:
                raise _fail(str(exc)) from exc
            if set(inputs) != set(meta['inputs']):
                raise _fail(f'{alias} 输入端口不匹配')
            for port, source in inputs.items():
                expected = meta['inputs'][port]
                if not isinstance(source, dict) or not isinstance(source.get('kind'), str):
                    raise _fail(f'{alias}.{port} 输入格式无效')
                kind = source['kind']
                if kind == 'literal' and set(source) == {'kind', 'value'}:
                    _json_value(source['value'])
                    contract = draft_defs.get(expected)
                    if contract is None:
                        contract, contract_data = self._contract_source(expected)
                        name, version = reference(expected)
                        deps.setdefault(f'.researchkit/contracts/{name}/{version}.yaml', contract_data)
                    if not check_type(contract['type'], source['value']):
                        raise _fail(f'{alias}.{port} 不符合 {expected}')
                elif kind == 'parameter' and set(source) == {'kind', 'name'}:
                    name = source['name']
                    if not isinstance(name, str) or name not in payload['parameters']:
                        raise _fail(f'{alias}.{port} 引用了不存在的项目参数')
                    contract = draft_defs.get(expected)
                    if contract is None:
                        contract, contract_data = self._contract_source(expected)
                        cname, version = reference(expected)
                        deps.setdefault(f'.researchkit/contracts/{cname}/{version}.yaml', contract_data)
                    if not check_type(contract['type'], payload['parameters'][name]):
                        raise _fail(f'项目参数 {name} 不符合 {expected}')
                elif kind == 'output' and set(source) == {'kind', 'step', 'port'}:
                    prior = source['step']
                    port_name = source['port']
                    if not isinstance(prior, str) or prior not in seen or not isinstance(port_name, str):
                        raise _fail(f'{alias}.{port} 只能引用更早步骤的输出')
                    producer = seen[prior][0]
                    if port_name not in producer['outputs']:
                        raise _fail(f'{alias}.{port} 引用了不存在的输出端口')
                    if producer['outputs'][port_name] != expected:
                        raise _fail(f'{alias}.{port} 与 {prior}.{port_name} 的契约不匹配')
                    # Ensure output contract is installed too when it is only an output.
                    if expected not in draft_defs:
                        _contract, contract_data = self._contract_source(expected)
                        cname, version = reference(expected)
                        deps.setdefault(f'.researchkit/contracts/{cname}/{version}.yaml', contract_data)
                else:
                    raise _fail(f'{alias}.{port} 输入格式无效')
            # Install every contract declared by a selected plugin, including
            # output-only contracts that have no literal input in this graph.
            for used_contract in set(meta['inputs'].values()) | set(meta['outputs'].values()):
                contract_doc = draft_defs.get(used_contract)
                if contract_doc is None:
                    contract_doc, contract_data = self._contract_source(used_contract)
                    cname, version = reference(used_contract)
                    deps.setdefault(f'.researchkit/contracts/{cname}/{version}.yaml', contract_data)
            destination = f'.researchkit/plugins/web-{content_hash}'
            plugin_paths[alias] = destination
            for path, value in plugin_files.items():
                deps[f'{destination}/{path}'] = value
            step = {'alias': alias, 'ref': ref, 'parameters': parameters, 'inputs': inputs}
            steps.append(step)
            seen[alias] = (meta, step)
        return payload['parameters'], steps, drafts, deps, plugin_paths

    def _entry(self, steps: list[dict[str, Any]]) -> str:
        lines = ['"""Generated by ResearchKit Web composition. Edit through Web or replace in the IDE."""', '', '', 'def run(context):', '    _outputs = {}']
        if not steps:
            lines.append('    return {}')
        for step in steps:
            inputs: list[str] = []
            for port, source in step['inputs'].items():
                if source['kind'] == 'literal':
                    value = repr(source['value'])
                elif source['kind'] == 'parameter':
                    value = f"context.parameters[{source['name']!r}]"
                else:
                    value = f"_outputs[{source['step']!r}][{source['port']!r}]"
                inputs.append(f'{port!r}: {value}')
            lines.append(f"    _outputs[{step['alias']!r}] = context.call({step['alias']!r}, inputs={{" + ', '.join(inputs) + '})')
        if steps:
            lines.append(f"    return _outputs[{steps[-1]['alias']!r}]")
        return '\n'.join(lines) + '\n'

    def _lock(self, files: dict[str, bytes]) -> bytes:
        protected: dict[str, str] = {}
        for path, value in files.items():
            if path == 'task' or path.startswith(('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/')):
                protected[path] = hashlib.sha256(value).hexdigest()
        return (json.dumps({'schema': 'researchkit.lock/v1', 'files': dict(sorted(protected.items()))},
                           ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')

    def _current_lock_valid(self, files: dict[str, bytes]) -> None:
        try:
            lock = json.loads(files['.researchkit/lock.json'].decode('utf-8'))
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise _fail('固定依赖锁缺失或无效；请在 IDE 修复后再编排') from exc
        expected = json.loads(self._lock(files).decode('utf-8'))
        if lock != expected:
            raise _fail('固定依赖锁已失效；请在 IDE 修复后再编排')

    # Journal and safe writes -------------------------------------------
    def _journal_path(self, project_id: str) -> Path:
        # Project ids originate at the broker, nevertheless make a filename
        # instead of embedding it in a path supplied by HTTP.
        return self._journal_root / (hashlib.sha256(project_id.encode('utf-8')).hexdigest() + '.json')

    def _journal_directory(self, *, create: bool) -> Path | None:
        try:
            return _secure_directory(self._journal_root, create=create)
        except KitError:
            if not create and not self._journal_root.exists():
                return None
            raise

    def _fsync_journal_directory(self) -> None:
        descriptor = os.open(self._journal_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_journal(self, project_id: str) -> dict[str, Any] | None:
        if self._journal_directory(create=False) is None:
            return None
        path = self._journal_path(project_id)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_JOURNAL_BYTES):
            raise _fail('编排恢复记录不安全')
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                observed = os.fstat(descriptor)
                if (observed.st_dev, observed.st_ino, observed.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise _fail('编排恢复记录在读取时变化')
                chunks: list[bytes] = []
                remaining = info.st_size + 1
                while remaining:
                    block = os.read(descriptor, min(1024 * 1024, remaining))
                    if not block:
                        break
                    chunks.append(block)
                    remaining -= len(block)
                final = os.fstat(descriptor)
                if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                    raise _fail('编排恢复记录在读取时变化')
                doc = json.loads(b''.join(chunks).decode('utf-8'))
            finally:
                os.close(descriptor)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise _fail('编排恢复记录已损坏') from exc
        if (not isinstance(doc, dict) or doc.get('schema') != _JOURNAL_SCHEMA or doc.get('project_id') != project_id
                or not isinstance(doc.get('changes'), list) or len(doc['changes']) > MAX_FILES):
            raise _fail('编排恢复记录格式无效')
        paths: set[str] = set()
        for row in doc['changes']:
            if (not isinstance(row, dict) or set(row) != {'path', 'before', 'after'} or not isinstance(row.get('path'), str)
                    or not isinstance(row.get('after'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['after'])):
                raise _fail('编排恢复记录格式无效')
            path = _relative(row['path'])
            if path in paths:
                raise _fail('编排恢复记录含重复文件')
            paths.add(path)
        return doc

    def _write_journal(self, project_id: str, changes: list[tuple[str, bytes | None, bytes]]) -> None:
        self._journal_directory(create=True)
        rows = [{'path': path, 'before': None if before is None else base64.b64encode(before).decode('ascii'),
                 'after': hashlib.sha256(after).hexdigest()} for path, before, after in changes]
        payload = json.dumps({'schema': _JOURNAL_SCHEMA, 'project_id': project_id, 'changes': rows},
                             ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise _fail('编排恢复记录超过大小上限')
        path = self._journal_path(project_id)
        temporary = path.with_name('.rk-journal-' + uuid.uuid4().hex)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_journal_directory()
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _remove_journal(self, project_id: str) -> None:
        if self._journal_directory(create=False) is None:
            return
        try:
            os.unlink(self._journal_path(project_id))
        except FileNotFoundError:
            return
        self._fsync_journal_directory()

    def _parent(self, root_fd: int, path: str, *, create: bool) -> tuple[int, str]:
        parts = _relative(path).split('/')
        descriptor = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    if not create:
                        raise _fail('编排目标目录不存在')
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise _fail('编排目标目录不安全')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                try:
                    observed = os.fstat(child)
                    if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
                        raise _fail('编排目标目录在打开时发生变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor, parts[-1]
        except Exception:
            os.close(descriptor)
            raise

    def _root_still_current(self, project_id: str, root_fd: int) -> bool:
        original = os.fstat(root_fd)
        fresh = self.workspace._open_project_fd(project_id)
        try:
            observed = os.fstat(fresh)
            return (original.st_dev, original.st_ino) == (observed.st_dev, observed.st_ino)
        finally:
            os.close(fresh)

    def _write_file(self, project_id: str, root_fd: int, path: str, value: bytes, expected: bytes | None) -> None:
        """CAS replace an existing file, or atomically create a new one."""
        if not self._root_still_current(project_id, root_fd):
            raise _fail('项目冲突：项目目录已被 IDE 替换；恢复记录已保留')
        parent_fd, name = self._parent(root_fd, path, create=expected is None)
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        temporary = '.rk-composer-' + uuid.uuid4().hex
        try:
            if expected is not None:
                current, identity = self.workspace._read_regular_from_parent(parent_fd, name, limit=MAX_FILE_BYTES)
                if current != expected or not self._root_still_current(project_id, root_fd):
                    raise _fail('项目冲突：IDE 已修改编排目标；恢复记录已保留')
                self.workspace._replace_regular(root_fd, parent_fd, path, name, identity, value)
                return
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _fail('项目冲突：编排新文件已被 IDE 创建；恢复记录已保留')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=parent_fd)
            try:
                offset = 0
                while offset < len(value):
                    offset += os.write(descriptor, value[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if not self.workspace._parent_still_current(root_fd, path, parent_identity) or not self._root_still_current(project_id, root_fd):
                raise _fail('项目冲突：编排目录已被 IDE 修改；恢复记录已保留')
            _rename_noreplace(parent_fd, temporary, name)
            os.fsync(parent_fd)
        except Exception:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(parent_fd)

    def _delete_file(self, project_id: str, root_fd: int, path: str, expected: bytes) -> None:
        """Remove only a just-written file after a fresh descriptor CAS read."""
        if not self._root_still_current(project_id, root_fd):
            raise _fail('编排恢复冲突：项目目录已被 IDE 替换；恢复记录已保留')
        parent_fd, name = self._parent(root_fd, path, create=False)
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        try:
            current, identity = self.workspace._read_regular_from_parent(parent_fd, name, limit=MAX_FILE_BYTES)
            if current != expected:
                raise _fail('编排恢复冲突：IDE 已修改新文件；恢复记录已保留')
            # The workspace root and parent descriptor are fixed; re-stat just
            # before unlink so a replacement is diagnosed instead of removed.
            final, final_identity = self.workspace._read_regular_from_parent(parent_fd, name, limit=MAX_FILE_BYTES)
            if final != expected or final_identity != identity or not self.workspace._parent_still_current(root_fd, path, parent_identity) or not self._root_still_current(project_id, root_fd):
                raise _fail('编排恢复冲突：IDE 已修改新文件；恢复记录已保留')
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def recover(self, project_id: str) -> None:
        journal = self._read_journal(project_id)
        if journal is None:
            return
        _root, files = self._snapshot(project_id)
        changes: list[tuple[str, bytes | None, str]] = []
        for row in journal['changes']:
            if not isinstance(row, dict) or set(row) != {'path', 'before', 'after'} or not isinstance(row['path'], str) or not isinstance(row['after'], str):
                raise _fail('编排恢复记录格式无效')
            try:
                path = _relative(row['path'])
                before = None if row['before'] is None else base64.b64decode(row['before'], validate=True)
            except (KitError, ValueError, TypeError) as exc:
                raise _fail('编排恢复记录格式无效') from exc
            changes.append((path, before, row['after']))
        for path, before, after in changes:
            current = files.get(path)
            if current is not None and hashlib.sha256(current).hexdigest() == after:
                continue
            if current == before:
                continue
            # A missing original and missing current is also already restored.
            if current is None and before is None:
                continue
            raise _fail('编排恢复冲突：IDE 已修改部分文件；恢复记录已保留')
        root_fd = self.workspace._open_project_fd(project_id)
        try:
            for path, before, after in changes:
                current = files.get(path)
                if current is not None and hashlib.sha256(current).hexdigest() == after:
                    if before is None:
                        self._delete_file(project_id, root_fd, path, current)
                    else:
                        self._write_file(project_id, root_fd, path, before, current)
        finally:
            os.close(root_fd)
        self._remove_journal(project_id)

    # Preview and commit -------------------------------------------------
    def _desired(self, files: dict[str, bytes], payload: Any) -> tuple[dict[str, bytes], str | None]:
        document = self._project_document(files)
        self._current_lock_valid(files)
        managed, old_steps, _old_drafts = self._composition(document)
        if not managed and document.get('plugins'):
            raise _fail('此项目已有 IDE 配置的算法；请继续在 IDE 编排，Web 不会覆盖它们')
        if managed:
            previous_aliases = {step.get('alias') for step in old_steps if isinstance(step, dict)}
            if set(document.get('plugins', {})) != previous_aliases:
                raise _fail('项目插件与 Web 编排记录不一致；请在 IDE 修复后再编排')
        parameters, steps, drafts, dependencies, plugin_paths = self._validate_payload(payload, document)
        desired = dict(files)
        for path, value in dependencies.items():
            current = desired.get(path)
            if current is not None and current != value:
                raise _fail(f'固定依赖内容冲突：{path}')
            desired.setdefault(path, value)
        plugins: dict[str, Any] = {}
        for step in steps:
            plugins[step['alias']] = {'path': plugin_paths[step['alias']], 'parameters': step['parameters']}
        document['parameters'] = parameters
        document['plugins'] = plugins
        document['web_composition'] = {'schema': _SCHEMA, 'steps': steps, 'draft_contracts': drafts}
        desired['project.yaml'] = _yaml_bytes(document)
        entry: str | None = None
        if payload['generate_entry']:
            entry = self._entry(steps)
            desired['case.py'] = entry.encode('utf-8')
        desired['.researchkit/lock.json'] = self._lock(desired)
        return desired, entry

    def preview(self, project_id: str, payload: Any) -> dict[str, Any]:
        self.recover(project_id)
        _root, files = self._snapshot(project_id)
        if not isinstance(payload, dict) or payload.get('expected_revision') != _revision(files):
            raise _fail('项目冲突：已被 IDE 或其他编辑器修改；请刷新后重试')
        desired, entry = self._desired(files, payload)
        changed = sorted(path for path in set(files) | set(desired) if files.get(path) != desired.get(path))
        return {'project_yaml': desired['project.yaml'].decode('utf-8'), 'entry_text': entry,
                'changed_paths': changed, 'revision': _revision(files)}

    def apply(self, project_id: str, payload: Any) -> dict[str, Any]:
        self.recover(project_id)
        _root, before = self._snapshot(project_id)
        if not isinstance(payload, dict) or payload.get('expected_revision') != _revision(before):
            raise _fail('项目冲突：已被 IDE 或其他编辑器修改；请刷新后重试')
        desired, _entry = self._desired(before, payload)
        changes = [(path, before.get(path), desired[path]) for path in sorted(desired)
                   if before.get(path) != desired[path]]
        if not changes:
            return self._read_result(project_id, before)
        if len(desired) > MAX_FILES or sum(map(len, desired.values())) > MAX_TOTAL_BYTES:
            raise _fail('编排后的项目文件数或总大小超过上限')
        # Capture again immediately before the journal/write sequence.  The
        # caller holds the Web lock, while this detects independent IDE edits.
        _root, observed = self._snapshot(project_id)
        if _revision(observed) != _revision(before):
            raise _fail('项目冲突：已被 IDE 或其他编辑器修改；请刷新后重试')
        self._write_journal(project_id, changes)
        root_fd = self.workspace._open_project_fd(project_id)
        expected_state = dict(before)
        try:
            for path, old, value in changes:
                _root, current_state = self._snapshot(project_id)
                if _revision(current_state) != _revision(expected_state):
                    raise _fail('项目冲突：IDE 已在编排过程中修改文件；恢复记录已保留')
                self._write_file(project_id, root_fd, path, value, old)
                expected_state[path] = value
        except Exception:
            # The journal deliberately remains.  ``recover`` only rolls back
            # paths which still contain our recorded bytes, never an IDE edit.
            raise
        finally:
            os.close(root_fd)
        _root, after = self._snapshot(project_id)
        if after != desired:
            raise _fail('项目冲突：提交后 IDE 修改了项目；恢复记录已保留')
        self._remove_journal(project_id)
        return self._read_result(project_id, after)
