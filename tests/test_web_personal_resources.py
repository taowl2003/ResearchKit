from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_personal_resources import enrich_versions, project_jobs, relate_dataset, baseline_tools
from researchkit.web_store import SubmissionStore
from researchkit.web_submission_flow import SubmissionFlow
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_workspace import Workspace
from researchkit.web_workspace_materials import add_algorithms, apply


TEACHER = Principal(17001, 'ly', 'teacher')
STUDENT = Principal(17002, 'test', 'student')


def test_dataset_versions_explicit_identity_and_immutable_mapping(tmp_path):
    store = SubmissionStore(tmp_path / 'store')
    catalog = [{'dataset_id': key, 'name': 'same display name'} for key in ['raw-v1', 'raw-v2', 'other']]
    assert enrich_versions(store, catalog) == catalog
    payload = {'dataset_id': 'raw-v2', 'parent_dataset_id': 'raw-v1', 'version_label': '2.0'}
    result = relate_dataset(store, TEACHER, payload, catalog)
    assert result['family_id'] == 'raw-v1'
    assert relate_dataset(store, TEACHER, payload, catalog) == result
    enriched = enrich_versions(store, catalog)
    assert [row['dataset_id'] for row in enriched] == ['raw-v1', 'raw-v2', 'other']
    assert [row.get('family_id', row['dataset_id']) for row in enriched] == ['raw-v1', 'raw-v1', 'other']
    for invalid in [dict(payload, parent_dataset_id='other'), dict(payload, version_label='3.0')]:
        with pytest.raises(KitError, match='已经关联'):
            relate_dataset(store, TEACHER, invalid, catalog)
    with pytest.raises(KitError, match='版本名称'):
        relate_dataset(store, TEACHER, dict(payload, dataset_id='other'), catalog)
    with pytest.raises(KitError, match='只有教师'):
        relate_dataset(store, STUDENT, payload, catalog)
    with pytest.raises(KitError, match='两个不同'):
        relate_dataset(store, TEACHER, dict(payload, parent_dataset_id='not-visible'), catalog)


def test_project_jobs_include_failed_preparation_and_owner_identity(tmp_path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'research')
    files = {'project.yaml': b'schema: researchkit.project/v1\nname: research\nplugins: {}\n',
             'case.py': b'def run(context):\n    return {}\n'}
    flow = SubmissionFlow(store)
    prepared = flow.prepare(STUDENT, project['project_id'], files=files, data_references=[], kind='minor',
                            case_version='0.1', request_id=str(uuid.uuid4()), request_fingerprint='a'*64)
    jobs = Jobs(store)
    jobs.start_worker('test-worker', 19001)
    first = jobs.claim('test-worker')
    jobs.finish('test-worker', first['run_id'], 'failed', 1, 'expected failure')
    flow.advance_snapshot(first['submission_id'])
    rows = project_jobs(store, STUDENT, project['project_id'])
    assert [(row['run_id'], row['status']) for row in rows] == [(first['run_id'], 'failed')]
    assert rows[0]['preparation_id'] == prepared['preparation_id']
    assert store.list_submissions(STUDENT, personal=True) == []
    for outsider in [TEACHER, Principal(STUDENT.uid, 'reused-uid', 'student')]:
        with pytest.raises(KitError, match='无权'):
            project_jobs(store, outsider, project['project_id'])
    request = str(uuid.uuid4())
    again = flow.prepare(STUDENT, project['project_id'], files=files, data_references=[], kind='minor',
                         case_version='0.1', request_id=request, request_fingerprint=hashlib.sha256(request.encode()).hexdigest())
    for _ in range(2):
        current = jobs.claim('test-worker')
        jobs.finish('test-worker', current['run_id'], 'succeeded', 0, None)
        flow.advance_snapshot(current['submission_id'])
    finished = flow.get(STUDENT, again['preparation_id'])
    assert finished['submission_id']
    rows = project_jobs(store, STUDENT, project['project_id'])
    assert len(rows) == 3 and sum(row['status'] == 'failed' for row in rows) == 1
    assert {row['operation'] for row in rows if row['submission_id'] == finished['submission_id']} == {'check', 'run'}


@pytest.fixture
def personal_workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-personal-resources-', dir=Path.cwd()))
    principal = Principal(os.getuid(), 'ly', 'teacher')
    store = SubmissionStore(root/'store')
    workspace = Workspace(principal, root/'workspace', LocalStoreClient(store, principal))
    try:
        yield workspace, store, principal
    finally:
        shutil.rmtree(root)


def test_algorithms_added_to_existing_project_without_binding_or_repeated_copy(personal_workspace):
    workspace, store, principal = personal_workspace
    project = workspace.create_project('existing research')
    root = Path(project['path'])
    before = {name: (root/name).read_bytes() for name in ['case.py', 'project.yaml']}
    payload = {'algorithm_refs': ['example.scale@1.0.0']}
    first = add_algorithms(workspace, project['project_id'], payload)
    assert first['project_id'] == project['project_id']
    plugin = next(root.glob('materials/algorithms/*/plugin/plugin.py'))
    plugin.write_text('# personal edits\n')
    add_algorithms(workspace, project['project_id'], payload)
    assert plugin.read_text() == '# personal edits\n'
    assert len(list(root.glob('materials/algorithms/*/plugin/plugin.py'))) == 1
    task = TaskDelivery(store).create_resource_task(principal,
        {'request_id': str(uuid.uuid4()), 'name':'tools', 'instructions':''}, targets=[principal],
        algorithm_materials=[{'ref':'example.scale@1.0.0','files':{'plugin/plugin.py':b'# fixed tool\n'},'metadata':{}}])
    apply(workspace, project['project_id'], {'task_id':task['task_id']})
    assert not list(root.glob('materials/tasks/*/*/plugin/plugin.py'))
    assert plugin.read_text() == '# personal edits\n'
    assert {name: (root/name).read_bytes() for name in before} == before
    assert len(workspace.list_projects()) == 1
    with pytest.raises(KitError):
        add_algorithms(workspace, project['project_id'], {'algorithm_refs':['missing@1.0.0']})


def test_cross_task_material_dedup_preserves_prior_task_files(personal_workspace):
    workspace, store, principal = personal_workspace
    project = workspace.create_project('one project')
    service = TaskDelivery(store)
    tasks = [service.create_resource_task(principal,
        {'request_id':str(uuid.uuid4()),'name':name,'instructions':''}, targets=[principal],
        algorithm_materials=[{'ref':'tool@'+version,'files':{'plugin.py':b'# fixed\n'},'metadata':{}}])
        for name, version in [('first','1.0.0'),('second','1.0.0'),('new version','2.0.0')]]
    for task in tasks:
        apply(workspace, project['project_id'], {'task_id':task['task_id']})
    root = Path(project['path'])
    assert len(list(root.glob('materials/tasks/*/*/plugin.py'))) == 2
    assert len(list(root.glob('materials/tasks/*/TASK.json'))) == 3
    before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    apply(workspace, project['project_id'], {'task_id':tasks[1]['task_id']})
    assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_baseline_algorithms_are_extracted_inertly_without_changing_case(personal_workspace):
    workspace, store, principal = personal_workspace
    project = workspace.create_project('base', 'hello@1.0.0')
    files = workspace._snapshot(project['project_id'])[1]
    before = dict(files)
    server = SimpleNamespace(store=store, policy=SimpleNamespace(code_root=workspace.library_root.parent))
    tools = baseline_tools(server, principal, {'files':files}, [])
    assert [row['ref'] for row in tools] == ['example.scale@1.0.0']
    assert tools[0]['metadata']['source'] == 'baseline'
    assert tools[0]['files']['plugin/plugin.py']
    assert tools[0]['files']['contracts/scalar/1.0.0.yaml']
    assert files == before and workspace._snapshot(project['project_id'])[1] == before


@pytest.mark.parametrize('receipt', [[], {'materials':'broken'}, {'materials':[None]},
    {'materials':[{'kind':'algorithm','ref':'tool@1.0.0','path':'../other'}]}])
def test_invalid_algorithm_receipt_rejects_without_project_changes(personal_workspace, receipt):
    workspace, _store, _principal = personal_workspace
    project = workspace.create_project('personal')
    root = Path(project['path'])
    (root/'materials/algorithms').mkdir(parents=True)
    (root/'materials/algorithms/RESOURCES.json').write_text(json.dumps(receipt))
    before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(KitError, match='记录无法核对'):
        add_algorithms(workspace, project['project_id'], {'algorithm_refs':['example.scale@1.0.0']})
    assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
