"""Claim a task in the authenticated user's workspace without replacing edits."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .files import KitError, atomic_text, write_yaml
from .projects import install_runtime, seal_lock
from .web_workspace import MAX_FILE_BYTES


def _empty_files(workspace, project):
    """Build a local starter project; a resource package has no fixed baseline."""
    root = Path(tempfile.mkdtemp(prefix='.rk-task-empty-', dir=workspace.root))
    try:
        write_yaml(root / 'project.yaml', {
            'schema': 'researchkit.project/v1', 'name': project['name'], 'mode': 'python',
            'entry': 'case.py', 'plugins': {}, 'parameters': {}, 'data': {},
            'requires': {'python': '3.11', 'modules': [], 'minimums': {}},
            'origin': {'kind': 'self-created'},
        })
        atomic_text(root / 'case.py', '"""在这里使用领取的算法工具和数据。"""\n\ndef run(context):\n    return {}\n')
        atomic_text(root / 'README.md', '# ' + project['name'] + '\n\n这是当前账号的个人项目。任务材料位于 materials/tasks/。\n')
        install_runtime(root)
        seal_lock(root)
        descriptor = workspace._open_absolute_directory(root)
        try:
            entries = workspace._check_tree(descriptor, collect=True)
            return {item['path']: workspace._read_regular(descriptor, item['path'], limit=MAX_FILE_BYTES)
                    for item in entries}
        finally:
            os.close(descriptor)
    finally:
        shutil.rmtree(root)


def claim(workspace, task_id, payload):
    if type(payload) is not dict or payload:
        raise KitError('领取完整任务不接受其他字段')
    detail = workspace.store.request('task_materials', task_id=task_id, personal=True)
    if detail.get('delivery_mode', 'project') == 'resources':
        # The broker allocates one project atomically for this exact recipient.
        # A failed local write can retry the same ID rather than creating copies.
        project = workspace.store.request('claim_resource_task', task_id=task_id)
        project_id = project['project_id']
        with workspace._state_lock():
            projects = workspace._load_state()
            if project_id not in projects:
                files = _empty_files(workspace, project)
                destination = workspace._materialize_distribution(project_id, files)
                projects[project_id] = {'path': destination.relative_to(workspace.root).as_posix(),
                                        'name': project['name']}
                workspace._save_state(projects)
            else:
                workspace._project_path(project_id)
        project = workspace.describe_project(project_id)
    else:
        distribution_id = detail.get('distribution_id')
        if not isinstance(distribution_id, str) or not distribution_id:
            raise KitError('完整任务尚未准备好领取，请稍后重试')
        project = workspace.claim_distribution(distribution_id)
        project_id = project['project_id']
    # Only the server-selected, currently authorized materials are applied.
    # Existing case.py/plugin bindings remain intact; repeats preserve edits.
    from .web_workspace_materials import apply
    result = apply(workspace, project_id, {'task_id': task_id}, preserve_existing=True)
    if detail.get('delivery_mode', 'project') == 'resources':
        workspace.store.request('confirm_resource_task', task_id=task_id, project_id=project_id)
    return {**result, 'project': workspace.describe_project(project_id)}
