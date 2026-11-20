"""Task-provided exact algorithms are reusable without becoming publications."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery


TEACHER = Principal(7201, 'ly', 'teacher')
STUDENT = Principal(7202, 'test', 'student')
OTHER = Principal(7203, 'other', 'student')


def _owned(root: Path) -> None:
    for path in (root, *root.rglob('*')):
        os.chown(path, os.geteuid(), -1)


@pytest.fixture
def resources():
    root = Path(tempfile.mkdtemp(prefix='.rk-task-components-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        library = root / 'library'
        source = Path(__file__).resolve().parents[1] / 'library'
        for name in ('algorithms', 'contracts', 'cases'):
            shutil.copytree(source / name, library / name)
        (library / 'contracts/unused').mkdir()
        (library / 'contracts/unused/1.0.0.yaml').write_text('schema: hydro.contract/v1\nid: unused@1.0.0\ntype: number\n')
        _owned(library)
        store = SubmissionStore(root / 'store')
        yield root, store, ComponentService(store, library), TaskDelivery(store)
    finally:
        shutil.rmtree(root)


def _request(name: str = '算法材料') -> dict[str, str]:
    return {'request_id': str(uuid.uuid4()), 'name': name, 'instructions': ''}


def _tool(ref: str = 'task.tool@1.0.0', *, code: bytes = b'def run(inputs, parameters, context): return {"value": inputs["value"]}\n',
          yaml_suffix: str = '') -> dict[str, object]:
    name, version = ref.split('@', 1)
    plugin = (
        'schema: hydro.plugin/v1\nname: ' + name + '\nversion: ' + version + '\nentry: plugin.py\n'
        'inputs:\n  value: task.scalar@1.0.0\noutputs:\n  value: task.scalar@1.0.0\nparameters: {}\n' + yaml_suffix
    ).encode()
    contract = b'schema: hydro.contract/v1\nid: task.scalar@1.0.0\ntype: number\ndescription: task scalar\n'
    return {'ref': ref, 'metadata': {'source': 'baseline'}, 'files': {
        'plugin/plugin.yaml': plugin, 'plugin/plugin.py': code,
        'contracts/task.scalar/1.0.0.yaml': contract,
    }}


def _task(delivery: TaskDelivery, material: dict[str, object], target: Principal = STUDENT):
    return delivery.create_resource_task(TEACHER, _request(), targets=[target], algorithm_materials=[material])


def test_task_only_tool_is_visible_to_its_recipient_with_embedded_contract(resources):
    _root, _store, service, delivery = resources
    task = _task(delivery, _tool())
    assert 'task.tool@1.0.0' in {row['ref'] for row in service.catalog(STUDENT)['plugins']}
    assert 'task.tool@1.0.0' not in {row['ref'] for row in service.catalog(OTHER)['plugins']}
    component = service.component(STUDENT, 'task.tool@1.0.0')
    assert component['files']['plugin/plugin.py'].startswith(b'def run')
    assert service.contract(STUDENT, 'task.scalar@1.0.0')['metadata']['type'] == 'number'
    with pytest.raises(KitError, match='无权'):
        service.component(OTHER, 'task.tool@1.0.0')
    # The teacher did not receive this student-only task material.
    with pytest.raises(KitError, match='不存在'):
        service.component(TEACHER, 'task.tool@1.0.0')
    assert task['delivery_mode'] == 'resources'
    assert 'unused@1.0.0' in {row['id'] for row in service.catalog(TEACHER)['contracts']}, 'teacher retains unused formal contracts for configuration'
    assert 'unused@1.0.0' not in {row['id'] for row in service.catalog(STUDENT)['contracts']}


def test_same_exact_task_tool_from_multiple_tasks_deduplicates_and_project_dispatch_is_visible(resources):
    _root, store, service, delivery = resources
    material = _tool()
    _task(delivery, material)
    _task(delivery, material)
    project = store.create_project(TEACHER, 'base')
    baseline = store.submit(project['project_id'], TEACHER, 'major', {'case.py': b'pass\n'}, [])
    delivery.create_task(TEACHER, _request('完整任务'), baseline_submission_id=baseline['submission_id'],
                         targets=[STUDENT], algorithm_materials=[material])
    refs = [row['ref'] for row in service.catalog(STUDENT)['plugins']]
    assert refs.count('task.tool@1.0.0') == 1


def test_same_ref_with_different_task_code_fails_closed(resources):
    _root, _store, service, delivery = resources
    _task(delivery, _tool())
    _task(delivery, _tool(code=b'def run(inputs, parameters, context): return {"value": 99}\n'))
    with pytest.raises(KitError, match='不同内容'):
        service.catalog(STUDENT)
    with pytest.raises(KitError, match='不同内容'):
        service.component(STUDENT, 'task.tool@1.0.0')


def test_revoke_hides_task_only_tool_without_affecting_other_recipient(resources):
    _root, _store, service, delivery = resources
    task_a = _task(delivery, _tool(), STUDENT)
    task_b = _task(delivery, _tool(), OTHER)
    grant_a = next(row for row in delivery.list_active_grants(STUDENT, task_a['task_id']) if row['kind'] == 'algorithm')
    delivery.revoke_grant(TEACHER, grant_a['grant_id'])
    with pytest.raises(KitError, match='无权'):
        service.component(STUDENT, 'task.tool@1.0.0')
    assert service.component(OTHER, 'task.tool@1.0.0')['ref'] == 'task.tool@1.0.0'
    assert task_b['task_id'] != task_a['task_id']


def test_task_material_hash_corruption_is_rejected(resources):
    _root, store, service, delivery = resources
    task = _task(delivery, _tool())
    with store._connection() as connection:
        material_id = connection.execute('''SELECT material_id FROM web_task_grants
            WHERE task_id=? AND grant_kind='algorithm' ''', (task['task_id'],)).fetchone()[0]
        connection.execute('''UPDATE web_task_material_files SET content=?
            WHERE material_id=? AND path='plugin/plugin.py' ''', (b'forged\n', material_id))
    with pytest.raises(KitError, match='摘要不匹配'):
        service.component(STUDENT, 'task.tool@1.0.0')


def test_formal_same_ref_accepts_yaml_layout_difference_but_rejects_code_change(resources):
    _root, _store, service, delivery = resources
    official = service.component(TEACHER, 'example.scale@1.0.0')
    equivalent = {'ref': official['ref'], 'metadata': {}, 'files': dict(official['files'])}
    equivalent['files']['plugin/plugin.yaml'] += b'# equivalent formatting comment\n'
    equivalent['files']['contracts/scalar/1.0.0.yaml'] += b'# equivalent contract layout\n'
    _task(delivery, equivalent)
    # The public central copy remains the canonical source when semantics and
    # executable bytes agree despite YAML formatting.
    assert service.component(STUDENT, 'example.scale@1.0.0')['files'] == official['files']
    assert service.contract(STUDENT, 'scalar@1.0.0')['files']['contract.yaml'] == official['files']['contracts/scalar/1.0.0.yaml']

    _root2, _store2, service2, delivery2 = resources
    changed = {'ref': official['ref'], 'metadata': {}, 'files': dict(official['files'])}
    changed['files']['plugin/plugin.py'] += b'# changed executable bytes\n'
    _task(delivery2, changed)
    with pytest.raises(KitError, match='不同内容'):
        service2.component(STUDENT, 'example.scale@1.0.0')
