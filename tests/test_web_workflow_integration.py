"""End-to-end broker checks for formal submission and review boundaries."""
from __future__ import annotations

import os
import pwd
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType
import threading
import uuid

import pytest

from researchkit import web_broker
from researchkit.files import KitError
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def _files(*, factor: int = 2) -> dict[str, bytes]:
    return {
        'project.yaml': b'schema: researchkit.project/v1\nmode: python\nentry: case.py\nname: workflow-source\nplugins:\n  scale:\n    path: plugins/scale\n    parameters:\n      factor: 2\nparameters: {}\ndata: {}\nrequires: {}\n',
        'case.py': b'def run(context):\n    return {}\n',
        'plugins/scale/plugin.yaml': b'schema: hydro.plugin/v1\nname: scale\nversion: 1.0.0\nentry: plugin.py\ninputs:\n  value: scalar@1.0.0\noutputs:\n  result: scalar@1.0.0\nparameters:\n  factor:\n    type: number\n    required: true\n',
        'plugins/scale/plugin.py': b"def run(inputs, parameters, context):\n    return {'result': inputs['value'] * " + str(factor).encode() + b"}\n",
        '.researchkit/contracts/scalar/1.0.0.yaml': b'schema: hydro.contract/v1\nid: scalar@1.0.0\ntype: number\ndescription: scalar\n',
    }


def _policy(tmp_path: Path) -> tuple[Policy, Principal]:
    uid = os.geteuid()
    username = pwd.getpwuid(uid).pw_name
    if username != 'ly':
        pytest.skip('workflow integration checks require the ly test account')
    teacher = Principal(uid=uid, username=username, role='teacher')
    private = tmp_path / 'broker-store'
    private.mkdir(mode=0o700)
    return Policy(
        principals=MappingProxyType({uid: teacher}), workspaces=MappingProxyType({}),
        broker_uid=uid, broker_username=username, submission_store=private,
        code_root=tmp_path / 'code', broker_socket=tmp_path / 'broker.sock',
    ), teacher


@pytest.fixture
def workflow_broker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    monkeypatch.setattr(web_broker, '_trusted_socket_route', lambda _path, _uid, require_socket: None)
    policy, teacher = _policy(tmp_path)
    server = web_broker.BrokerServer(policy, workers=2)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
    thread.start()
    client = web_broker.BrokerClient(server.socket_path, policy.broker_uid)
    try:
        yield server, client, teacher
    finally:
        server.close()
        thread.join(timeout=5)


def _prepare(client, project_id: str, files: dict[str, bytes], *, request_id: str | None = None,
             algorithm_versions: dict[str, str] | None = None):
    from researchkit.web_versions import apply_versions
    plan = client.request('preview_submission_versions', project_id=project_id, files=files, kind='major')
    selected = (algorithm_versions if algorithm_versions is not None else
                {item['alias']: item['suggested_version'] for item in plan['algorithms']})
    desired = apply_versions(files, plan, {'case_version': plan['case']['suggested_version'],
                                           'algorithm_versions': selected})
    return client.request(
        'prepare_submission', project_id=project_id, files=desired, data_references=[], kind='major',
        case_version=plan['case']['suggested_version'],
        request_id=request_id or str(uuid.uuid4()), request_fingerprint='a' * 64,
    )


def test_direct_prepare_saves_exact_snapshot_and_teacher_review_is_manual(workflow_broker):
    server, client, teacher = workflow_broker
    project = client.request('create_project', name='workflow source')
    files = _files()
    request_id = str(uuid.uuid4())
    plan = client.request('preview_submission_versions', project_id=project['project_id'], files=files, kind='major')
    prepared = client.request('prepare_submission', project_id=project['project_id'], files=files,
        data_references=[], kind='major', case_version=plan['case']['suggested_version'],
        request_id=request_id, request_fingerprint='b' * 64)
    assert prepared['mode'] == 'direct' and prepared['status'] == 'succeeded'
    assert prepared['submission_id']
    repeated = client.request('prepare_submission', project_id=project['project_id'], files=files,
        data_references=[], kind='major', case_version=plan['case']['suggested_version'],
        request_id=request_id, request_fingerprint='b' * 64)
    assert repeated['receipt_id'] == prepared['receipt_id']
    submission_id = prepared['submission_id']
    assert client.request('get_submission', submission_id=submission_id)['files'] == files
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        client.request('list_jobs', submission_id=submission_id)
    release = server._dispatch(teacher, {'method': 'review_release', 'params': {'submission_id': submission_id}})
    assert release['review_mode'] == 'manual'


def test_web_execution_is_not_a_submission_gate_and_changed_algorithm_requires_new_version(workflow_broker):
    server, client, teacher = workflow_broker
    project = client.request('create_project', name='failure source')
    prepared = _prepare(client, project['project_id'], _files())
    assert prepared['status'] == 'succeeded' and prepared['submission_id']
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        client.request('list_project_jobs', project_id=project['project_id'])
    formal = client.request('get_submission', submission_id=prepared['submission_id'])
    changed = _files(factor=3)
    plan = client.request('preview_submission_versions', project_id=project['project_id'], files=changed, kind='major')
    scale = next(item for item in plan['algorithms'] if item['alias'] == 'scale')
    assert scale['changed'] is True and scale['suggested_version'] == '1.0.1'
    with pytest.raises(KitError, match='高于原版本'):
        _prepare(client, project['project_id'], changed, algorithm_versions={'scale': '1.0.0'})
    listed = client.request('list_submissions', project_id=project['project_id'])
    assert [item['submission_id'] for item in listed] == [formal['submission_id']]


def test_workspace_public_preview_and_prepare_freeze_the_same_snapshot(workflow_broker, tmp_path):
    _server, client, teacher = workflow_broker
    root = Path(tempfile.mkdtemp(prefix='.rk-workflow-workspace-', dir=Path.cwd()))
    try:
        workspace = Workspace(teacher, root / 'workspace', client, library_root=tmp_path / 'library')
        project = workspace.create_project('workspace submission')
        preview = workspace.submission_preview(project['project_id'], {'kind': 'minor'})
        before = workspace._snapshot(project['project_id'])[1]
        prepared = workspace.submission_prepare(project['project_id'], {
            'kind': 'minor', 'expected_revision': preview['snapshot_revision'],
            'case_version': preview['case']['suggested_version'], 'algorithm_versions': {},
            'request_id': str(uuid.uuid4()),
        })
        assert prepared['mode'] == 'direct' and prepared['status'] == 'succeeded'
        assert client.request('get_submission', submission_id=prepared['submission_id'])['files'] == before
    finally:
        shutil.rmtree(root)


def test_personal_routes_hide_student_records_but_teacher_review_can_read_without_execute(workflow_broker):
    server, client, teacher = workflow_broker
    student = Principal(teacher.uid + 700_001, 'workflow-student', 'student')
    project = server.store.create_project(student, 'student private')
    submission = server.store.submit(project['project_id'], student, 'major', _files(), [])
    assert client.request('list_submissions') == []
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        client.request('list_project_jobs', project_id=project['project_id'])
    with pytest.raises(KitError, match='个人固定提交'):
        client.request('get_submission', submission_id=submission['submission_id'])
    review = server._dispatch(teacher, {'method': 'review_submission', 'params': {'submission_id': submission['submission_id']}})
    assert web_broker._decode_files(review['files']) == _files()
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(teacher, {'method': 'request_job', 'params': {'submission_id': submission['submission_id'], 'operation': 'run'}})
    with pytest.raises(KitError, match='只有教师'):
        server._dispatch(student, {'method': 'review_submission', 'params': {'submission_id': submission['submission_id']}})
