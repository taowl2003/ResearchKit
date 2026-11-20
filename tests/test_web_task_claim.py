"""Whole-task claiming uses one project and preserves independent user edits."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
import tempfile
import shutil

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_assignments import AssignmentService
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_task_claim import claim
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_workspace import Workspace


@pytest.fixture
def task_workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-task-claim-', dir=Path.cwd()))
    principal = Principal(os.getuid(), 'ly', 'teacher')
    store = SubmissionStore(root / 'store')
    workspace = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        yield workspace, store, principal
    finally:
        shutil.rmtree(root)


def _resources(workspace):
    return AssignmentService(workspace).create({
        'request_id': str(uuid.uuid4()), 'name': '可选算法资源', 'template_ref': '',
        'algorithm_refs': ['example.scale@1.0.0'], 'dataset_ids': [],
        'targets': ['ly'], 'instructions': '工具可以自行接入已有项目。',
    })


def test_whole_project_claims_initial_tools_and_keeps_case_and_tool_edits(task_workspace):
    workspace, store, principal = task_workspace
    base = workspace.create_project('基础案例')
    fixed = workspace.submit(base['project_id'], 'major')
    task = TaskDelivery(store).create_task(principal,
        {'request_id': str(uuid.uuid4()), 'name': '完整任务', 'instructions': '保留入口。'},
        baseline_submission_id=fixed['submission_id'], targets=[principal],
        algorithm_materials=[{'ref': 'tool@1.0.0', 'files': {'plugin.py': b'# optional tool\n'}, 'metadata': {}}])
    expected = (Path(base['path']) / 'case.py').read_bytes()
    first = claim(workspace, task['task_id'], {})
    project = first['project']
    assert (Path(project['path']) / 'case.py').read_bytes() == expected
    material = next(Path(project['path']).glob('materials/tasks/*/*/plugin.py'))
    material.write_bytes(b'# personal tool edit\n')
    (Path(project['path']) / 'case.py').write_bytes(b'# personal case edit\n')
    second = claim(workspace, task['task_id'], {})
    assert second['project']['project_id'] == project['project_id']
    assert material.read_bytes() == b'# personal tool edit\n'
    assert (Path(project['path']) / 'case.py').read_bytes() == b'# personal case edit\n'
    assert (Path(project['path']) / 'materials/tasks' / task['task_id'] / 'README.md').is_file()
    assert store.get_submission(fixed['submission_id'], principal)['files']['case.py'] == expected
    with pytest.raises(KitError, match='字段'):
        claim(workspace, task['task_id'], {'target_username': 'test'})


def test_resource_package_creates_one_empty_project_and_no_fixed_submission(task_workspace):
    workspace, store, principal = task_workspace
    task = _resources(workspace)
    first = claim(workspace, task['task_id'], {})
    project = first['project']
    assert store.get_project(project['project_id'], principal)['owner_username'] == 'ly'
    assert project.get('baseline_submission_id') is None
    root = Path(project['path'])
    assert (root / 'task').is_file()
    assert list((root / 'materials/tasks').rglob('plugin.py'))
    (root / 'case.py').write_text('# keep my work\n')
    again = claim(workspace, task['task_id'], {})
    assert again['project']['project_id'] == project['project_id']
    assert (root / 'case.py').read_text() == '# keep my work\n'
    personal = TaskDelivery(store).get_task(principal, task['task_id'], personal=True)
    assert personal['claim_status'] == 'claimed'
    assert personal['project_id'] == project['project_id']
    with store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 1
    receipt = json.loads((root / 'materials/tasks' / task['task_id'] / 'TASK.json').read_text())
    assert receipt['applied_material_ids']


def test_resource_claim_recovers_local_index_failure_using_same_broker_project(task_workspace, monkeypatch):
    workspace, store, principal = task_workspace
    task = _resources(workspace)
    with monkeypatch.context() as patch:
        patch.setattr(workspace, '_save_state', lambda _state: (_ for _ in ()).throw(OSError('disk failure')))
        with pytest.raises(OSError, match='disk failure'):
            claim(workspace, task['task_id'], {})
    pending = TaskDelivery(store).get_task(principal, task['task_id'], personal=True)
    assert pending['claim_status'] == 'claiming'
    recovered = claim(workspace, task['task_id'], {})
    assert recovered['project']['project_id'] == pending['project_id']
    assert len(list(workspace.projects_root.glob('assigned-*'))) == 1
    with store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 1
