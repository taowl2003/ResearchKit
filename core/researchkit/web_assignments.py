"""Teacher task preparation without exposing a teacher-side work project.

An assignment is assembled in a private, short-lived workspace.  Its fixed
submission is the task baseline; recipients claim that existing distribution
through the ordinary workflow.  The broker is still the authority for target
Linux accounts, data permissions, immutable baseline de-duplication and the
atomic distribution batch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, atomic_text
from .model import validate_parameters
from .projects import seal_lock
from .web_store import _validate_data_references, _validate_files
from .web_workspace import MAX_EDIT_BYTES, Workspace, _digest, _error, _relative, _secure_directory


_STATE_NAME = '.researchkit-web-assignments.json'
_STATE_SCHEMA = 'researchkit.web-assignments/v1'
_STAGING_DIRECTORY = '.researchkit-assignment-staging'
_MAX_INSTRUCTIONS_BYTES = 64 * 1024


def _canonical_request_id(value: object) -> str:
    if not isinstance(value, str):
        raise _error('任务请求 ID 无效')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _error('任务请求 ID 必须是 UUID') from exc
    if str(parsed) != value:
        raise _error('任务请求 ID 必须使用小写标准 UUID')
    return value


def _nonempty_text(value: object, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip().encode('utf-8')) > maximum:
        raise _error(f'{label}不能为空且不得超过 {maximum} 字节')
    return value.strip()


class AssignmentService:
    """Prepare and dispatch a single immutable task baseline for a teacher."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._state_path = workspace.root / _STATE_NAME
        self._staging_parent = workspace.root / _STAGING_DIRECTORY

    def _require_teacher(self) -> None:
        if self.workspace.principal.role != 'teacher':
            raise _error('只有教师可以布置任务')

    def _normalize(self, payload: object) -> tuple[dict[str, object], str]:
        required = {'request_id', 'name', 'template_ref', 'dataset_ids', 'targets', 'instructions'}
        if (type(payload) is not dict or not required <= set(payload) or
                set(payload) - (required | {'algorithm_refs', 'composition', 'parent_task_id',
                                             'include_baseline_algorithms'})):
            raise _error('布置任务请求字段无效')
        request_id = _canonical_request_id(payload['request_id'])
        parent_task_id = payload.get('parent_task_id')
        if parent_task_id is not None:
            parent_task_id = _canonical_request_id(parent_task_id)
        include_baseline_algorithms = payload.get('include_baseline_algorithms', False)
        if not isinstance(include_baseline_algorithms, bool):
            raise _error('是否提供案例内算法必须为布尔值')
        name = _nonempty_text(payload['name'], '任务名称')
        template_value = payload['template_ref']
        if template_value is None or (isinstance(template_value, str) and not template_value.strip()):
            template_ref: str | None = None
        else:
            template_ref = _nonempty_text(template_value, '案例引用')
        dataset_ids = payload['dataset_ids']
        if (not isinstance(dataset_ids, list) or len(dataset_ids) > 128 or
                any(not isinstance(item, str) or not item for item in dataset_ids) or
                len(set(dataset_ids)) != len(dataset_ids)):
            raise _error('数据集必须是最多 128 个不重复的数据集 ID')
        algorithm_refs = payload.get('algorithm_refs', [])
        if (not isinstance(algorithm_refs, list) or len(algorithm_refs) > 128 or
                any(not isinstance(item, str) or not item.strip() or len(item.strip().encode('utf-8')) > 200
                    for item in algorithm_refs) or len({item.strip() for item in algorithm_refs}) != len(algorithm_refs)):
            raise _error('附加算法必须是最多 128 个不重复的算法引用')
        targets = payload['targets']
        if (not isinstance(targets, list) or not targets or len(targets) > 100 or
                any(not isinstance(item, str) or not item.strip() or len(item.strip()) > 64 for item in targets)):
            raise _error('目标账号必须是 1 至 100 个非空账号名')
        normalized_targets = sorted({item.strip() for item in targets})
        if len(normalized_targets) != len(targets):
            raise _error('目标账号不能重复')
        instructions = payload['instructions']
        if not isinstance(instructions, str) or len(instructions.encode('utf-8')) > _MAX_INSTRUCTIONS_BYTES:
            raise _error('任务说明必须是 UTF-8 文本且不得超过 64 KiB')
        # Older callers may keep the field during their transition, but a
        # teacher task must never rewrite the chosen case's composition or
        # generate a replacement case.py.  Extra algorithms are materials,
        # resolved separately by the broker task-delivery service.
        if payload.get('composition') is not None:
            raise _error('布置任务不支持编排或重新生成 case.py')
        if template_ref is None and not algorithm_refs and not dataset_ids:
            raise _error('无案例的资源包至少需要一个算法或数据集')
        normalized: dict[str, object] = {
            'request_id': request_id,
            'name': name,
            'template_ref': template_ref,
            'algorithm_refs': sorted(item.strip() for item in algorithm_refs),
            'dataset_ids': sorted(dataset_ids),
            'targets': normalized_targets,
            'instructions': instructions,
        }
        # Keep the old normalized bytes unchanged when this is a new task.
        # Existing durable request fingerprints must continue to retry exactly.
        if parent_task_id is not None:
            normalized['parent_task_id'] = parent_task_id
        if include_baseline_algorithms:
            normalized['include_baseline_algorithms'] = True
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return normalized, hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _load_state(self) -> dict[str, dict[str, object]]:
        try:
            info = os.lstat(self._state_path)
        except FileNotFoundError:
            return {}
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_EDIT_BYTES):
            raise _error('任务布置索引不安全')
        try:
            value = json.loads(self._state_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _error('任务布置索引已损坏') from exc
        if (type(value) is not dict or set(value) != {'schema', 'uid', 'username', 'requests'} or
                value['schema'] != _STATE_SCHEMA or value['uid'] != self.workspace.principal.uid or
                value['username'] != self.workspace.principal.username or type(value['requests']) is not dict):
            raise _error('任务布置索引格式或所属账号无效')
        values: dict[str, dict[str, object]] = {}
        for request_id, record in value['requests'].items():
            _canonical_request_id(request_id)
            if (type(record) is not dict or set(record) != {'fingerprint', 'result'} or
                    not isinstance(record['fingerprint'], str) or len(record['fingerprint']) != 64 or
                    not isinstance(record['result'], dict)):
                raise _error('任务布置索引格式无效')
            values[request_id] = record
        return values

    def _save_state(self, requests: dict[str, dict[str, object]]) -> None:
        value = {'schema': _STATE_SCHEMA, 'uid': self.workspace.principal.uid,
                 'username': self.workspace.principal.username, 'requests': requests}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n'
        # This is only a retry cache. The durable central request marker is
        # authoritative, so old local entries can be evicted without losing
        # assignments or making a successful dispatch fail after long use.
        while len(encoded.encode('utf-8')) > MAX_EDIT_BYTES and len(requests) > 1:
            requests.pop(next(iter(requests)))
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n'
        if len(encoded.encode('utf-8')) > MAX_EDIT_BYTES:
            raise _error('单个任务回执超过本地索引容量')
        atomic_text(self._state_path, encoded)
        os.chmod(self._state_path, 0o600)

    def _known_result(self, request_id: str, fingerprint: str) -> dict[str, object] | None:
        with self.workspace._state_lock():
            record = self._load_state().get(request_id)
        if record is None:
            return None
        if record['fingerprint'] != fingerprint:
            raise _error('同一任务请求 ID 已用于内容不同的任务')
        return dict(record['result'])

    def _remember_result(self, request_id: str, fingerprint: str, result: dict[str, object]) -> dict[str, object]:
        """Cache only a completed dispatch; the broker remains crash authority."""
        with self.workspace._state_lock():
            requests = self._load_state()
            existing = requests.get(request_id)
            if existing is not None:
                if existing['fingerprint'] != fingerprint:
                    raise _error('同一任务请求 ID 已用于内容不同的任务')
                return dict(existing['result'])
            requests[request_id] = {'fingerprint': fingerprint, 'result': result}
            self._save_state(requests)
        return result

    def _template_and_references(self, request: dict[str, object]) -> tuple[dict[str, bytes], list[dict[str, object]]]:
        if request['template_ref'] is None:
            catalog = self.workspace._catalog_datasets()
            selected = request['dataset_ids']
            assert isinstance(selected, list)
            missing = [dataset_id for dataset_id in selected if dataset_id not in catalog]
            if missing:
                raise _error(f'数据集不存在、未登记或当前账号未获授权：{missing[0]}')
            return {}, []
        template = self.workspace.store.request('get_shared_template', ref=request['template_ref'])
        if not isinstance(template, dict) or not isinstance(template.get('files'), dict):
            raise _error('案例服务返回无效')
        files, _manifest = _validate_files(template['files'])
        raw_references = files.get('DATA-REFERENCES.json')
        if raw_references is None:
            references: list[dict[str, object]] = []
        else:
            references = Workspace._references_document(raw_references)
        catalog = self.workspace._catalog_datasets()
        selected = request['dataset_ids']
        assert isinstance(selected, list)
        missing = [dataset_id for dataset_id in selected if dataset_id not in catalog]
        if missing:
            raise _error(f'数据集不存在、未登记或当前账号未获授权：{missing[0]}')
        # Selected datasets are additional task materials.  They are not
        # silently merged into DATA-REFERENCES.json: the base case continues
        # to run with precisely its own fixed references.
        return files, _validate_data_references(references)

    @staticmethod
    def _algorithm_bindings(files: dict[str, bytes], document: dict[str, object]) -> dict[str, dict[str, object]]:
        """Expose only declared plugin metadata; never execute plugin code."""
        plugins = document.get('plugins', {})
        if not isinstance(plugins, dict):
            raise _error('案例 project.yaml 的算法绑定无效')
        bindings: dict[str, dict[str, object]] = {}
        for alias, config in plugins.items():
            if not isinstance(alias, str) or not isinstance(config, dict):
                raise _error('案例 project.yaml 的算法绑定无效')
            path, parameters = config.get('path'), config.get('parameters', {})
            if not isinstance(parameters, dict):
                raise _error(f'案例算法绑定无效：{alias}')
            # Published cases may carry editable plugins under algorithms/ or
            # another validated project-relative directory. Their fixed bytes
            # are already in the central template; never reopen a host path.
            path = _relative(path, label='案例算法路径')
            raw = files.get(path.rstrip('/') + '/plugin.yaml')
            if raw is None:
                raise _error(f'案例算法缺少插件说明：{alias}')
            try:
                metadata = yaml.safe_load(raw.decode('utf-8'))
            except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
                raise _error(f'案例算法说明无效：{alias}') from exc
            if (not isinstance(metadata, dict) or metadata.get('schema') != 'hydro.plugin/v1' or
                    not isinstance(metadata.get('name'), str) or not isinstance(metadata.get('version'), str) or
                    not isinstance(metadata.get('parameters'), dict)):
                raise _error(f'案例算法说明无效：{alias}')
            bindings[alias] = {
                'ref': metadata['name'] + '@' + metadata['version'], 'parameters': parameters,
                'parameter_schema': metadata['parameters'],
            }
        return bindings

    @classmethod
    def _apply_algorithm_parameters(cls, workspace: Workspace, project_id: str,
                                    updates: dict[str, object]) -> None:
        if not updates:
            return
        opened = workspace.read_text(project_id, 'project.yaml')
        raw = opened.get('text')
        if not isinstance(raw, str) or not isinstance(opened.get('sha256'), str):
            raise _error('任务项目配置不是可编辑 UTF-8 文本')
        try:
            document = yaml.safe_load(raw)
        except (yaml.YAMLError, RecursionError) as exc:
            raise _error('任务项目配置无效') from exc
        if not isinstance(document, dict) or document.get('schema') != 'researchkit.project/v1':
            raise _error('任务项目配置无效')
        if document.get('web_composition') is not None:
            raise _error('Web 编排案例请在编排中修改算法参数')
        _root, files, _references = workspace._snapshot(project_id)
        bindings = cls._algorithm_bindings(files, document)
        plugins = document.get('plugins')
        assert isinstance(plugins, dict)
        for alias, values in updates.items():
            if alias not in bindings or not isinstance(values, dict):
                raise _error(f'案例没有算法别名：{alias}')
            config = plugins[alias]
            assert isinstance(config, dict)
            effective = dict(bindings[alias]['parameters'])
            effective.update(values)
            schema = bindings[alias]['parameter_schema']
            assert isinstance(schema, dict)
            validate_parameters(schema, effective, alias)
            config['parameters'] = effective
        try:
            encoded = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        except (TypeError, ValueError) as exc:
            raise _error('无法保存任务项目配置') from exc
        workspace.save_text(project_id, 'project.yaml', encoded, opened['sha256'])
        # The ordinary project creator seals before returning.  Parameter edits
        # are made before the fixed baseline, so seal the exact final tree.
        seal_lock(workspace._project_path(project_id))

    def _preflight(self, request: dict[str, object], references: list[dict[str, object]]) -> dict[str, object]:
        targets = request['targets']
        assert isinstance(targets, list)
        result = self.workspace.store.request('validate_assignment_targets', target_usernames=targets,
                                              data_references=references, task_payload=request)
        if not isinstance(result, dict) or not isinstance(result.get('targets'), list):
            raise _error('任务目标预检查返回无效')
        returned = result['targets']
        usernames = []
        for target in returned:
            if type(target) is not dict or not isinstance(target.get('username'), str):
                raise _error('任务目标预检查返回无效')
            usernames.append(target['username'])
        if sorted(usernames) != sorted(targets) or len(usernames) != len(targets):
            raise _error('任务目标预检查未覆盖所有选定账号')
        return result

    def _new_staging_workspace(self) -> tuple[Workspace, Path]:
        _secure_directory(self._staging_parent, create=True)
        root = Path(tempfile.mkdtemp(prefix='assignment-', dir=self._staging_parent))
        os.chmod(root, 0o700)
        return Workspace(self.workspace.principal, root, self.workspace.store,
                         library_root=self.workspace.library_root), root

    @staticmethod
    def _composition_payload(current: dict[str, object], requested: dict[str, object]) -> dict[str, object]:
        # The server, rather than a browser-provided revision, binds this edit
        # to the just-created isolated baseline directory.
        return {'expected_revision': current['revision'], 'parameters': requested['parameters'],
                'steps': requested['steps'], 'draft_contracts': requested['draft_contracts'],
                'generate_entry': requested['generate_entry']}

    @staticmethod
    def _write_instructions(workspace: Workspace, project_id: str, instructions: str) -> None:
        if not instructions.strip():
            return
        addition = '# 教师任务说明\n\n' + instructions.strip() + '\n'
        try:
            opened = workspace.read_text(project_id, 'TASK.md')
        except KitError as exc:
            if str(exc) != '文件不存在':
                raise
            with workspace._project_guard(project_id):
                root_fd = workspace._open_project_fd(project_id)
                try:
                    workspace._create_regular(root_fd, root_fd, 'TASK.md', 'TASK.md', addition.encode('utf-8'))
                finally:
                    os.close(root_fd)
            return
        existing = opened['text']
        if not isinstance(existing, str) or not isinstance(opened.get('sha256'), str):
            raise _error('TASK.md 不是可编辑 UTF-8 文本')
        workspace.save_text(project_id, 'TASK.md', existing.rstrip() + '\n\n' + addition,
                            opened['sha256'])

    def preview(self, payload: object) -> dict[str, object]:
        """Validate selectable inputs without creating a project or submission."""
        self._require_teacher()
        request, fingerprint = self._normalize(payload)
        files, references = self._template_and_references(request)
        if request['template_ref'] is None:
            target_summary = self._preflight(request, references)
            return {
                'request_id': request['request_id'], 'fingerprint': fingerprint,
                'template_ref': None, 'delivery_mode': 'resources', 'composition': None,
                'composition_editable': False, 'algorithm_bindings': {}, 'source_algorithms': [], 'source_data': [],
                'algorithm_refs': request['algorithm_refs'], 'dataset_ids': request['dataset_ids'],
                'catalog': self.workspace.composition_catalog(), 'datasets': [], 'data_references': [],
                'targets': request['targets'], 'target_details': target_summary.get('targets'),
            }
        try:
            document = yaml.safe_load(files['project.yaml'].decode('utf-8'))
        except (KeyError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            raise _error('案例 project.yaml 无效') from exc
        if not isinstance(document, dict) or document.get('schema') != 'researchkit.project/v1':
            raise _error('案例 project.yaml 无效')
        plugin_bindings = self._algorithm_bindings(files, document)
        target_summary = self._preflight(request, references)
        catalog = self.workspace.composition_catalog()
        return {
            'request_id': request['request_id'], 'fingerprint': fingerprint,
            'template_ref': request['template_ref'], 'composition': None,
            'composition_editable': False, 'algorithm_bindings': plugin_bindings,
            'source_algorithms': [{'alias':alias,'ref':item['ref']} for alias,item in plugin_bindings.items()],
            'source_data': Workspace._reference_groups(references),
            'algorithm_refs': request['algorithm_refs'], 'dataset_ids': request['dataset_ids'],
            'catalog': catalog, 'datasets': Workspace._reference_groups(references),
            'data_references': references, 'targets': request['targets'],
            'target_details': target_summary.get('targets'),
        }

    def create(self, payload: object) -> dict[str, object]:
        self._require_teacher()
        request, fingerprint = self._normalize(payload)
        known = self._known_result(str(request['request_id']), fingerprint)
        if known is not None:
            return known
        if request['template_ref'] is None:
            self._template_and_references(request)
            self._preflight(request, [])
            dispatched = self.workspace.store.request('create_resource_task', task_payload=request)
            if (not isinstance(dispatched, dict) or not isinstance(dispatched.get('task_id'), str) or
                    dispatched.get('delivery_mode') != 'resources' or dispatched.get('distributions') != []):
                raise _error('资源包任务服务返回无效')
            result = {'request_id': request['request_id']} | dispatched
            return self._remember_result(str(request['request_id']), fingerprint, result)
        # A previous process can have committed the baseline just before its
        # local state write.  Ask the central marker first so a restart does
        # not create a second staging project or a second immutable snapshot.
        recovered = self.workspace.store.request('get_assignment_baseline', request_id=str(request['request_id']),
                                                 fingerprint=fingerprint)
        if recovered is not None:
            if not isinstance(recovered, dict) or not isinstance(recovered.get('submission_id'), str):
                raise _error('任务基线恢复服务返回无效')
            try:
                references = _validate_data_references(
                    recovered['manifest']['data_references']['references'])
            except (KeyError, TypeError, KitError) as exc:
                raise _error('已恢复任务基线缺少有效数据引用') from exc
            self._preflight(request, references)
            targets = request['targets']
            assert isinstance(targets, list)
            dispatched = self.workspace.store.request(
                'create_assignment_distributions', submission_id=recovered['submission_id'], target_usernames=targets,
                request_id=str(request['request_id']), task_payload=request,
            )
            if not isinstance(dispatched, dict) or not isinstance(dispatched.get('distributions'), list):
                raise _error('任务分发服务返回无效')
            result = {'request_id': request['request_id'], 'baseline_submission_id': recovered['submission_id'],
                      'baseline_content_sha256': recovered.get('content_sha256'),
                      'distributions': dispatched['distributions']}
            result.update({key: value for key, value in dispatched.items() if key != 'distributions'})
            return self._remember_result(str(request['request_id']), fingerprint, result)
        # Validate the immutable case and all whole-dataset references before
        # creating the private staging project.  The broker resolves target
        # principals and tests their data access; browser-supplied names never
        # become principals in this process.
        _template_files, preflight_references = self._template_and_references(request)
        self._preflight(request, preflight_references)
        staging, staging_root = self._new_staging_workspace()
        try:
            created = staging.create_project(str(request['name']), str(request['template_ref']))
            project_id = str(created['project_id'])
            # The staged project only installs and seals the published case.
            # Extra algorithms, datasets and instructions belong to the task
            # delivery record; writing them here would change the runnable
            # case and make a task's advertised baseline misleading.
            references = staging.project_datasets(project_id)['references']
            assert isinstance(references, list)
            self._preflight(request, references)
            baseline = staging.submit(project_id, 'major', assignment_request_id=str(request['request_id']),
                                      assignment_fingerprint=fingerprint)
            submission_id = baseline.get('submission_id')
            if not isinstance(submission_id, str):
                raise _error('任务基线提交服务返回无效')
            targets = request['targets']
            assert isinstance(targets, list)
            dispatched = self.workspace.store.request(
                'create_assignment_distributions', submission_id=submission_id, target_usernames=targets,
                request_id=str(request['request_id']), task_payload=request,
            )
            if not isinstance(dispatched, dict) or not isinstance(dispatched.get('distributions'), list):
                raise _error('任务分发服务返回无效')
            result = {'request_id': request['request_id'], 'baseline_submission_id': submission_id,
                      'baseline_content_sha256': baseline.get('content_sha256'),
                      'distributions': dispatched['distributions']}
            result.update({key: value for key, value in dispatched.items() if key != 'distributions'})
        finally:
            # This path was created by mkdtemp directly below a private,
            # fixed-name directory owned by the current user.  It contains no
            # user project: the durable source is the broker snapshot.
            if staging_root.exists():
                shutil.rmtree(staging_root)
        # Retain only successful completed requests locally.  A crash before
        # this write is still safe: the broker assignment marker returns the
        # same baseline for the same teacher/request/fingerprint.
        return self._remember_result(str(request['request_id']), fingerprint, result)
