from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from researchkit.web_assets import AssetService
from researchkit.web_cleanup import Cleanup
from researchkit.web_identity import Principal
from researchkit.web_lifecycle import AssetLifecycle
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery
from test_web_lifecycle import _asset_project_files


TEACHER = Principal(4101, 'ly', 'teacher')
STUDENT = Principal(4102, 'task-student', 'student')


def _plain_files(*, template_ref: str | None = None) -> dict[str, bytes]:
    origin = ('origin:\n  kind: task\n' +
              (f'  template_ref: {template_ref}\n' if template_ref else ''))
    return {
        'project.yaml': ('''schema: researchkit.project/v1
name: task-base
mode: python
entry: case.py
plugins: {}
parameters: {}
data: {}
requires: {}
''' + origin).encode('utf-8'),
        'case.py': b'def run(context):\n    return {}\n',
    }


def _publish(assets: AssetService, source: dict[str, object], *, kind: str, name: str) -> str:
    candidate = assets.create(TEACHER, source['submission_id'], kind, name, '1.0.0',
                              'scale' if kind == 'algorithm' else None)
    assets.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'ok')
    assets.publish(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'])
    return f'{name}@1.0.0'


@pytest.fixture
def task_state():
    root = Path(tempfile.mkdtemp(prefix='.rk-task-lifecycle-', dir=Path.cwd()))
    library = root / 'library'
    for name in ('algorithms', 'cases', 'contracts'):
        (library / name).mkdir(parents=True)
    for path in (library, library / 'algorithms', library / 'cases', library / 'contracts'):
        os.chown(path, os.geteuid(), -1)
        os.chmod(path, 0o755)
    store = SubmissionStore(root / 'store')
    assets = AssetService(store, library)

    algorithm_project = store.create_project(TEACHER, 'algorithm source')
    algorithm_source = store.submit(algorithm_project['project_id'], TEACHER, 'minor', _asset_project_files(), [])
    algorithm_ref = _publish(assets, algorithm_source, kind='algorithm', name='scale')

    template_project = store.create_project(TEACHER, 'template source')
    template_source = store.submit(template_project['project_id'], TEACHER, 'minor', _plain_files(), [])
    template_ref = _publish(assets, template_source, kind='template', name='task-base')

    baseline_project = store.create_project(TEACHER, 'task baseline')
    baseline = store.submit(baseline_project['project_id'], TEACHER, 'minor', _plain_files(template_ref=template_ref), [])
    lifecycle = AssetLifecycle(store, library)
    cleanup = Cleanup(store, library)
    try:
        yield store, lifecycle, cleanup, TaskDelivery(store), baseline, algorithm_ref, template_ref
    finally:
        shutil.rmtree(root)


def test_active_task_materials_are_exact_blockers_with_targeted_suggestions(task_state):
    store, lifecycle, _cleanup, delivery, baseline, algorithm_ref, template_ref = task_state
    task = delivery.create_task(TEACHER, {
        'request_id': str(uuid.uuid4()), 'name': '降雨任务', 'instructions': '',
    }, baseline_submission_id=baseline['submission_id'], targets=[STUDENT])
    supplement = delivery.supplement(TEACHER, task['task_id'], {
        'request_id': str(uuid.uuid4()), 'instructions': '补充算法。',
    }, targets=[STUDENT], algorithm_materials=[{
        'ref': algorithm_ref, 'metadata': {'label': 'scale'},
        'files': {'plugin.yaml': b'schema: hydro.plugin/v1\nname: scale\nversion: 1.0.0\n',
                  'plugin.py': b'def run(inputs, parameters, context):\n return {}\n'},
    }])

    algorithm_report = lifecycle.report(TEACHER, 'algorithm', algorithm_ref)
    active = next(item for item in algorithm_report['references'] if item['type'] == 'task-material')
    assert active['task_id'] == task['task_id']
    assert active['task_name'] == '降雨任务'
    assert active['target_username'] == STUDENT.username
    assert active['supplement_id'] == supplement['supplement_id']

    template_report = lifecycle.report(TEACHER, 'template', template_ref)
    base_material = next(item for item in template_report['references'] if item['type'] == 'task-material')
    assert base_material['task_id'] == task['task_id'] and base_material['supplement_id'] == ''

    lifecycle.change(TEACHER, 'algorithm', algorithm_ref, 'trash', algorithm_report['revision'])
    plan = _cleanup.preview(TEACHER, [{'kind': 'algorithm', 'ref': algorithm_ref}])
    blocker = next(item for item in plan['items'][0]['blockers'] if item['label'] == '任务材料仍授权此资源')
    assert blocker['task_id'] == task['task_id'] and blocker['target_username'] == STUDENT.username
    assert '撤销这位接收者' in blocker['suggestion']
    assert not plan['can_execute']


def test_resource_only_algorithm_grants_are_visible_revocable_and_cleanup_blockers(task_state):
    from researchkit.files import KitError
    from researchkit.web_sharing import ComponentService
    store, lifecycle, cleanup, delivery, _baseline, algorithm_ref, _template_ref = task_state
    components = ComponentService(store, lifecycle.library_root)
    access = next(row for row in components.access(TEACHER) if row['kind'] == 'algorithm' and row['ref'] == algorithm_ref)
    components.set_access(TEACHER, 'algorithm', algorithm_ref, False, [], access['revision'])
    with pytest.raises(KitError):
        components.component(STUDENT, algorithm_ref)
    item = components.component(TEACHER, algorithm_ref)
    task = delivery.create_resource_task(TEACHER, {
        'request_id':str(uuid.uuid4()), 'name':'只有算法', 'instructions':''},
        targets=[STUDENT], algorithm_materials=[{'ref':algorithm_ref, 'files':item['files'], 'metadata':{}}])
    assert components.component(STUDENT, algorithm_ref)['files'] == item['files']
    report = lifecycle.report(TEACHER, 'algorithm', algorithm_ref)
    active = next(row for row in report['references'] if row['type'] == 'task-material')
    assert active['task_id'] == task['task_id'] and active['target_username'] == STUDENT.username
    lifecycle.change(TEACHER, 'algorithm', algorithm_ref, 'trash', report['revision'])
    plan = cleanup.preview(TEACHER, [{'kind':'algorithm', 'ref':algorithm_ref}])
    assert any(row['label'] == '任务材料仍授权此资源' for row in plan['items'][0]['blockers'])
    grant = delivery.list_active_grants(STUDENT, task['task_id'])[0]
    delivery.revoke_grant(TEACHER, grant['grant_id'])
    plan = cleanup.preview(TEACHER, [{'kind':'algorithm', 'ref':algorithm_ref}])
    assert plan['can_execute'], plan


def test_revoked_task_material_is_history_only_and_cleanup_keeps_its_bytes(task_state):
    store, lifecycle, cleanup, delivery, baseline, algorithm_ref, _template_ref = task_state
    task = delivery.create_task(TEACHER, {
        'request_id': str(uuid.uuid4()), 'name': '测试任务', 'instructions': '',
    }, baseline_submission_id=baseline['submission_id'], targets=[STUDENT])
    delivery.supplement(TEACHER, task['task_id'], {
        'request_id': str(uuid.uuid4()), 'instructions': '',
    }, targets=[STUDENT], algorithm_materials=[{
        'ref': algorithm_ref, 'metadata': {},
        'files': {'plugin.yaml': b'schema: hydro.plugin/v1\nname: scale\nversion: 1.0.0\n',
                  'plugin.py': b'def run(inputs, parameters, context):\n return {}\n'},
    }])
    grant = next(item for item in delivery.list_active_grants(STUDENT, task['task_id'])
                 if item['kind'] == 'algorithm')
    with store._connection() as connection:
        material_id = connection.execute('SELECT material_id FROM web_task_grants WHERE id=?',
                                         (grant['grant_id'],)).fetchone()['material_id']
        before = connection.execute('SELECT count(*) FROM web_task_material_files WHERE material_id=?',
                                    (material_id,)).fetchone()[0]
    delivery.revoke_grant(TEACHER, grant['grant_id'])

    report = lifecycle.report(TEACHER, 'algorithm', algorithm_ref)
    assert any(item['type'] == 'task-material-history' for item in report['references'])
    lifecycle.change(TEACHER, 'algorithm', algorithm_ref, 'trash', report['revision'])
    plan = cleanup.preview(TEACHER, [{'kind': 'algorithm', 'ref': algorithm_ref}])
    assert plan['can_execute'], plan
    assert any('材料代码副本' in item for item in plan['items'][0]['advisories'])
    assert cleanup.execute(TEACHER, [{'kind': 'algorithm', 'ref': algorithm_ref}],
                           plan['plan_token'], '永久清理')['status'] == 'completed'
    with store._connection() as connection:
        assert connection.execute('SELECT count(*) FROM web_task_material_files WHERE material_id=?',
                                  (material_id,)).fetchone()[0] == before


def test_private_preparation_snapshots_do_not_make_template_cleanup_unverifiable(task_state):
    store, lifecycle, _cleanup, _delivery, _baseline, _algorithm_ref, template_ref = task_state
    internal_project = store.create_project(TEACHER, 'internal preparation')
    internal = store.submit(internal_project['project_id'], TEACHER, 'minor', {
        'case.py': b'pass\n',
    }, [])
    with store._connection() as connection:
        connection.execute('''CREATE TABLE web_submission_preparations (
            preparation_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, source_project_id TEXT NOT NULL)''')
        connection.execute('INSERT INTO web_submission_preparations VALUES (?,?,?)',
                           (str(uuid.uuid4()), internal['submission_id'], internal_project['project_id']))
        connection.execute('DELETE FROM submission_files WHERE submission_id=?', (internal['submission_id'],))
    report = lifecycle.report(TEACHER, 'template', template_ref)
    assert not any('缺少 project.yaml' in note for note in report['notes'])


def test_running_private_verification_protects_data_but_failure_releases_it(task_state):
    from researchkit.web_submission_flow import SubmissionFlow
    from researchkit.web_jobs import Jobs
    store,lifecycle,*_=task_state
    project=store.create_project(TEACHER,'private verification')
    flow=SubmissionFlow(store)
    ref={'dataset_id':'queued-private-data','path':'data.csv','size':1,'sha256':'a'*64}
    prep=flow.prepare(TEACHER,project['project_id'],files=_plain_files(),data_references=[ref],
        kind='major',case_version='1.0',request_id=str(uuid.uuid4()),request_fingerprint='b'*64)
    with store._connection() as con:
        references,notes=lifecycle._submission_refs(con,'dataset',ref['dataset_id'])
        assert not notes and len(references)==1 and references[0]['type']=='active-execution'
        snapshot=con.execute('SELECT snapshot_id FROM web_submission_preparations WHERE preparation_id=?',
                             (prep['preparation_id'],)).fetchone()[0]
    jobs=Jobs(store);jobs.start_worker('lifecycle-worker',4103)
    jobs.claim('lifecycle-worker');jobs.finish('lifecycle-worker',prep['check_job']['run_id'],'failed',1,'bad input')
    flow.advance_snapshot(snapshot)
    with store._connection() as con:
        references,notes=lifecycle._submission_refs(con,'dataset',ref['dataset_id'])
        assert references==[] and notes==[]
