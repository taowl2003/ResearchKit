"""Inert, owner-scoped inventory of a personal project's copied dependencies."""
from __future__ import annotations

import json
import os

import yaml

from .files import KitError
from .web_store import _relative_path


def _object(data, parser):
    try:
        value = parser(data.decode('utf-8'))
    except (UnicodeError, ValueError, yaml.YAMLError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def summarize(workspace, project_id):
    # Inspect declarations through the same safe descriptors as the editor.
    # This must also work when a referenced dataset is no longer available;
    # listing dependencies never validates or opens the shared data files.
    with workspace._project_guard(project_id):
        project = workspace._broker_project(project_id)
        descriptor = workspace._open_project_fd(project_id)
        try:
            root_info = os.fstat(descriptor)
            before = workspace._tree_metadata(descriptor)
            entries = workspace._check_tree(descriptor, collect=True)
            present = {row['path'] for row in entries}
            files = {path: workspace._read_regular(descriptor, path) for path in
                     ('project.yaml', '.researchkit/origins.json', 'DATA-REFERENCES.json') if path in present}
            document = _object(files.get('project.yaml', b''), yaml.safe_load)
            if document.get('schema') != 'researchkit.project/v1':
                raise KitError('无法读取有效的 project.yaml，请修复项目配置后重新核对依赖')
            plugins = document.get('plugins', {})
            if not isinstance(plugins, dict):
                raise KitError('项目算法配置无效，无法列出依赖')
            for config in plugins.values():
                if not isinstance(config, dict):
                    raise KitError('项目算法配置无效，无法列出依赖')
                path = _relative_path(config.get('path'), label='项目算法路径') + '/plugin.yaml'
                if path in present:
                    files[path] = workspace._read_regular(descriptor, path)
            if before != workspace._tree_metadata(descriptor):
                raise KitError('项目在读取依赖时发生变化，请重新核对')
            current_descriptor = workspace._open_project_fd(project_id)
            try:
                current_info = os.fstat(current_descriptor)
                if (root_info.st_dev, root_info.st_ino) != (current_info.st_dev, current_info.st_ino):
                    raise KitError('项目目录在读取依赖时发生变化，请重新核对')
            finally:
                os.close(current_descriptor)
            references = workspace._references_document(files['DATA-REFERENCES.json']) if 'DATA-REFERENCES.json' in files else []
        finally:
            os.close(descriptor)
    document = _object(files.get('project.yaml', b''), yaml.safe_load)
    origins = _object(files.get('.researchkit/origins.json', b'{}'), json.loads)
    origin_algorithms = origins.get('algorithms', {})
    origin_algorithms = origin_algorithms if isinstance(origin_algorithms, dict) else {}
    plugins = document.get('plugins', {})
    if not isinstance(plugins, dict):
        raise KitError('项目算法配置无效，无法列出依赖')
    algorithms = []
    for alias, config in plugins.items():
        if not isinstance(alias, str) or not isinstance(config, dict):
            raise KitError('项目算法配置无效，无法列出依赖')
        path = config.get('path')
        if (not isinstance(path, str) or not path or '\\' in path
                or path.startswith('/') or any(part in {'', '.', '..'} for part in path.split('/'))):
            raise KitError('项目算法路径无效，无法列出依赖')
        path = _relative_path(path, label='项目算法路径')
        meta = _object(files.get(path + '/plugin.yaml', b''), yaml.safe_load)
        origin = origin_algorithms.get(alias, {})
        origin = origin if isinstance(origin, dict) else {}
        source_ref = config.get('source_ref') or origin.get('ref')
        source_ref = source_ref if isinstance(source_ref, str) else None
        platform_copy = bool(source_ref or path.startswith('.researchkit/plugins/'))
        name, version = meta.get('name'), meta.get('version')
        algorithms.append({'alias': alias, 'name': name, 'version': version,
                           'ref': f'{name}@{version}' if isinstance(name, str) and isinstance(version, str) else None,
                           'source_ref': source_ref, 'path': path,
                           'kind': 'platform_copy' if platform_copy else 'local',
                           'modified': None,
                           'parameters': config.get('parameters', {})})
    # Enrichment only queries registration metadata, never opens shared data.
    datasets = workspace._reference_groups(references)
    for row in datasets:
        row['files'] = [dict(ref) for ref in references if ref['dataset_id'] == row['dataset_id']]
    warning = None
    if datasets:
        try:
            information = workspace.store.request('check_dataset_existence', dataset_ids=[row['dataset_id'] for row in datasets])
            if isinstance(information, dict):
                for row in datasets:
                    info = information.get(str(row['dataset_id']))
                    if isinstance(info, dict):
                        row['name'] = info.get('name') or row.get('name')
                        row['registered'] = bool(info.get('exists', True))
        except KitError:
            warning = '暂未读取到数据登记状态；下面仍显示项目中保存的准确引用。'
    origin = document.get('origin', {})
    origin = origin if isinstance(origin, dict) else {}
    return {'project_id': project_id, 'project_name': project.get('name'),
            'source_case_ref': origin.get('template_ref'),
            'source_submission_id': origin.get('submission_id'),
            'baseline_submission_id': project.get('baseline_submission_id'),
            'algorithms': algorithms, 'datasets': datasets, 'warning': warning}
