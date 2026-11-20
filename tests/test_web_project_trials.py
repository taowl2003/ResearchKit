"""Regression coverage for the retired Web-execution boundary.

The earlier module tested queued browser trials.  Browser execution has been
removed: a fixed submission is written directly, while running remains an IDE
responsibility.  These tests retain snapshot, ownership, version and
idempotency guarantees without reviving the retired protocol.
"""
from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from researchkit import web_broker
from researchkit.files import KitError
from researchkit.web_identity import Policy, Principal
from researchkit.web_submission_flow import SubmissionFlow
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


STUDENT = Principal(8421, 'trial-student', 'student')
OTHER = Principal(8421, 'trial-other', 'student')


def _files(*, changed: bool = False) -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
name: trial lesson
mode: python
entry: case.py
plugins:
  scale:
    path: algorithms/scale
    parameters: {}
parameters: {}
data: {}
''',
        'case.py': b'def run(context):\n    return {}\n' + (b'# changed\n' if changed else b''),
        'algorithms/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: example.scale
version: 1.0.0
entry: plugin.py
inputs: {}
outputs: {}
parameters: {}
''',
        'algorithms/scale/plugin.py': b'def run(inputs, parameters, context):\n    return {}\n',
    }


def _flow(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'trial lesson')
    return store, SubmissionFlow(store), project


def _direct(flow: SubmissionFlow, project: dict[str, object], files: dict[str, bytes], *,
            request_id: str | None = None, fingerprint: str | None = None,
            kind: str = 'minor') -> dict[str, object]:
    plan = flow.preview(STUDENT, str(project['project_id']), files, kind)
    return flow.direct(
        STUDENT, str(project['project_id']), files=files, data_references=[], kind=kind,
        case_version=str(plan['case']['suggested_version']),
        request_id=request_id or str(uuid.uuid4()),
        request_fingerprint=fingerprint or hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )


def test_direct_submission_is_immutable_idempotent_and_creates_no_jobs(tmp_path: Path):
    store, flow, project = _flow(tmp_path)
    request_id = str(uuid.uuid4())
    fingerprint = hashlib.sha256(b'fixed snapshot').hexdigest()
    first = _direct(flow, project, _files(), request_id=request_id, fingerprint=fingerprint)
    assert first['mode'] == 'direct' and first['status'] == 'succeeded'
    assert first['submission_id'] == first['final_submission_id']
    repeated = _direct(flow, project, _files(), request_id=request_id, fingerprint=fingerprint)
    assert repeated['receipt_id'] == first['receipt_id']
    saved = store.get_submission(str(first['submission_id']), STUDENT)
    assert saved['files'] == _files()
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM web_submission_preparations').fetchone()[0] == 0


def test_direct_submission_request_and_snapshot_are_bound_to_owner_and_content(tmp_path: Path):
    _store, flow, project = _flow(tmp_path)
    request_id = str(uuid.uuid4())
    fingerprint = hashlib.sha256(b'content one').hexdigest()
    saved = _direct(flow, project, _files(), request_id=request_id, fingerprint=fingerprint)
    with pytest.raises(KitError, match='无权|只能提交自己的项目'):
        flow.find_direct(OTHER, str(project['project_id']), request_id, fingerprint)
    with pytest.raises(KitError, match='不同的内容'):
        _direct(flow, project, _files(changed=True), request_id=request_id,
                fingerprint=hashlib.sha256(b'content two').hexdigest())
    assert flow.get_direct(STUDENT, str(saved['receipt_id']))['submission_id'] == saved['submission_id']


def test_direct_submission_requires_saved_versions_and_parent_compare_and_swap(tmp_path: Path):
    _store, flow, project = _flow(tmp_path)
    first = _direct(flow, project, _files())
    changed = _files(changed=True)
    stale_plan = flow.preview(STUDENT, str(project['project_id']), changed, 'minor')
    second = _direct(flow, project, changed)
    assert second['submission_id'] != first['submission_id']
    further = dict(changed)
    further['note.txt'] = b'changed after the preview'
    with pytest.raises(KitError, match='版本必须高于|新的提交'):
        flow.direct(
            STUDENT, str(project['project_id']), files=further, data_references=[], kind='minor',
            case_version=str(stale_plan['case']['suggested_version']), request_id=str(uuid.uuid4()),
            request_fingerprint=hashlib.sha256(b'stale').hexdigest(),
        )


def test_old_preparation_schema_migrates_without_losing_receipt(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'old receipt')
    with store._connection() as con:
        con.execute('''CREATE TABLE web_submission_preparations (
            preparation_id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL, owner_username TEXT NOT NULL,
            source_project_id TEXT NOT NULL, expected_parent_id TEXT, kind TEXT NOT NULL,
            case_version TEXT NOT NULL, request_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
            snapshot_id TEXT UNIQUE, check_run_id TEXT, run_run_id TEXT, final_submission_id TEXT,
            status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE(owner_uid,owner_username,request_id))''')
        con.execute('''INSERT INTO web_submission_preparations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    ('old-receipt', STUDENT.uid, STUDENT.username, project['project_id'], None, 'minor',
                     '0.1', str(uuid.uuid4()), 'a' * 64, None, None, None, None, 'failed',
                     'old diagnostics', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
    SubmissionFlow(store)
    with store._connection() as con:
        row = con.execute('SELECT mode,snapshot_sha256,reused_preparation_id,reason FROM web_submission_preparations WHERE preparation_id=?',
                          ('old-receipt',)).fetchone()
    assert dict(row) == {'mode': 'formal', 'snapshot_sha256': '', 'reused_preparation_id': None,
                         'reason': 'old diagnostics'}


def test_public_trial_and_runner_protocols_are_retired_but_workspace_direct_submit_works(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    monkeypatch.setattr(web_broker, '_trusted_socket_route', lambda _path, _uid, require_socket: None)
    uid = os.geteuid()
    username = pwd.getpwuid(uid).pw_name
    principal = Principal(uid, username, 'teacher')
    policy = Policy(
        principals=MappingProxyType({uid: principal}), workspaces=MappingProxyType({}),
        broker_uid=uid, broker_username=username, submission_store=tmp_path / 'store',
        code_root=tmp_path / 'code', broker_socket=tmp_path / 'broker.sock',
    )
    server = web_broker.BrokerServer(policy, workers=2)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
    thread.start()
    client = web_broker.BrokerClient(server.socket_path, uid)
    workspace_base = Path(tempfile.mkdtemp(prefix='.rk-direct-workspace-', dir=Path.cwd()))
    try:
        project = client.request('create_project', name='retired browser trial')
        with pytest.raises(KitError, match='网页检查、运行.*移除'):
            client.request('prepare_project_trial', project_id=project['project_id'], files=_files(),
                           data_references=[], request_id=str(uuid.uuid4()), request_fingerprint='a' * 64)
        runner = Principal(uid + 900_000, 'retired-runner', 'runner')
        server.policy = replace(server.policy, runner_uid=runner.uid, runner_username=runner.username)
        with pytest.raises(Exception, match='停用'):
            server._dispatch(runner, {'method': 'runner_start', 'params': {'instance_id': 'retired'}})
        workspace = Workspace(principal, workspace_base / 'workspace', client, library_root=tmp_path / 'library')
        local = workspace.create_project('IDE first submit')
        with pytest.raises(KitError, match='网页运行已移除'):
            workspace.project_trial(local['project_id'], {'request_id': str(uuid.uuid4())})
        preview = workspace.submission_preview(local['project_id'], {'kind': 'minor'})
        receipt = workspace.submission_prepare(local['project_id'], {
            'kind': 'minor', 'expected_revision': preview['snapshot_revision'],
            'case_version': preview['case']['suggested_version'], 'algorithm_versions': {},
            'request_id': str(uuid.uuid4()),
        })
        assert receipt['mode'] == 'direct' and receipt['status'] == 'succeeded'
    finally:
        server.close()
        thread.join(timeout=5)
        shutil.rmtree(workspace_base)
