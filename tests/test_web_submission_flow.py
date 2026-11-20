from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_submission_flow import SubmissionFlow
from researchkit.web_store import SubmissionStore


STUDENT = Principal(2201, 'test', 'student')
OTHER = Principal(2201, 'other', 'student')


def _files(*, changed: bool = False) -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
name: lesson
mode: python
entry: case.py
plugins:
  scale:
    path: algorithms/scale
    parameters:
      factor: 2
parameters: {}
data: {}
''',
        'case.py': b'def run(context):\n    return context.call("scale", inputs={"value": 3})\n' + (b'# changed\n' if changed else b''),
        'algorithms/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: example.scale
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs:
  value: scalar@1.0.0
parameters: {}
''',
        'algorithms/scale/plugin.py': b'def run(inputs, parameters, context):\n    return {"value": inputs["value"]}\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
''',
    }


def _flow(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'lesson')
    return store, SubmissionFlow(store), project


def _prepare(flow, project, files, *, request_id=None, case_version='0.1'):
    request_id = request_id or str(uuid.uuid4())
    return flow.prepare(STUDENT, project['project_id'], files=files, data_references=[], kind='minor',
                        case_version=case_version, request_id=request_id,
                        request_fingerprint=hashlib.sha256(request_id.encode()).hexdigest())


def _snapshot_id(store, preparation_id):
    with store._connection() as con:
        return con.execute('SELECT snapshot_id FROM web_submission_preparations WHERE preparation_id=?',
                           (preparation_id,)).fetchone()['snapshot_id']


def _finish(jobs, worker, run_id, status='succeeded', code=0):
    assert jobs.claim(worker)['run_id'] == run_id
    return jobs.finish(worker, run_id, status, code, None if code == 0 else 'failed')


def test_exact_check_run_then_formal_promotion_releases_private_snapshot(tmp_path: Path):
    store, flow, project = _flow(tmp_path)
    prepared = _prepare(flow, project, _files())
    assert prepared['status'] == 'checking'
    snapshot = _snapshot_id(store, prepared['preparation_id'])
    # Private validation never appears in ordinary personal history.
    assert store.list_submissions(STUDENT, personal=True) == []
    jobs = Jobs(store); jobs.start_worker('flow-worker', 991)
    _finish(jobs, 'flow-worker', prepared['check_job']['run_id'])
    flow.advance_snapshot(snapshot)
    running = flow.get(STUDENT, prepared['preparation_id'])
    assert running['status'] == 'running' and running['run_job'] is not None
    _finish(jobs, 'flow-worker', running['run_job']['run_id'])
    flow.advance_snapshot(snapshot)
    completed = flow.get(STUDENT, prepared['preparation_id'])
    assert completed['status'] == 'succeeded'
    final_id = completed['submission_id']
    assert final_id and store.get_submission(final_id, STUDENT)['version'] == '0.1'
    assert {row['operation'] for row in flow.evidence(STUDENT, final_id)} == {'check', 'run'}
    with store._connection() as con:
        assert con.execute('SELECT 1 FROM submissions WHERE id=?', (snapshot,)).fetchone() is None
        assert con.execute('SELECT 1 FROM submission_files WHERE submission_id=?', (snapshot,)).fetchone() is None
        assert con.execute('SELECT final_submission_id FROM web_submission_preparations WHERE preparation_id=?',
                           (prepared['preparation_id'],)).fetchone()['final_submission_id'] == final_id


def test_invalid_or_nonincrementing_case_version_rejects_before_queue(tmp_path: Path):
    store, flow, project = _flow(tmp_path)
    before_jobs = Jobs(store).list(STUDENT, personal=True)
    with pytest.raises(KitError, match='案例版本'):
        _prepare(flow, project, _files(), case_version='broken')
    with pytest.raises(KitError, match='首次minor'):
        _prepare(flow, project, _files(), case_version='1.0')
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM web_submission_preparations').fetchone()[0] == 0
    assert Jobs(store).list(STUDENT, personal=True) == before_jobs
    # Establish one formal parent, then a changed case cannot queue with its
    # old version number.
    first = _prepare(flow, project, _files())
    snapshot = _snapshot_id(store, first['preparation_id'])
    jobs = Jobs(store); jobs.start_worker('flow-invalid', 992)
    _finish(jobs, 'flow-invalid', first['check_job']['run_id']); flow.advance_snapshot(snapshot)
    run = flow.get(STUDENT, first['preparation_id'])['run_job']
    _finish(jobs, 'flow-invalid', run['run_id']); flow.advance_snapshot(snapshot)
    with pytest.raises(KitError, match='版本必须高于'):
        _prepare(flow, project, _files(changed=True), case_version='0.1')


def test_failed_check_keeps_diagnostics_but_releases_temporary_source_bodies(tmp_path: Path):
    store, flow, project = _flow(tmp_path)
    prepared = _prepare(flow, project, _files())
    snapshot = _snapshot_id(store, prepared['preparation_id'])
    jobs = Jobs(store); jobs.start_worker('flow-failure', 993)
    _finish(jobs, 'flow-failure', prepared['check_job']['run_id'], 'failed', 1)
    flow.advance_snapshot(snapshot)
    failed = flow.get(STUDENT, prepared['preparation_id'])
    assert failed['status'] == 'failed' and failed['submission_id'] is None
    with store._connection() as con:
        assert con.execute('SELECT 1 FROM submissions WHERE id=?', (snapshot,)).fetchone() is not None
        assert con.execute('SELECT COUNT(*) FROM submission_files WHERE submission_id=?', (snapshot,)).fetchone()[0] == 0
        assert con.execute('SELECT status FROM web_jobs WHERE run_id=?',(prepared['check_job']['run_id'],)).fetchone()[0]=='failed'
    assert store.list_submissions(STUDENT, personal=True) == []


def test_parent_compare_and_swap_rejects_late_formal_submission(tmp_path: Path):
    store, flow, project = _flow(tmp_path)
    prepared = _prepare(flow, project, _files())
    snapshot = _snapshot_id(store, prepared['preparation_id'])
    jobs = Jobs(store); jobs.start_worker('flow-cas', 995)
    _finish(jobs, 'flow-cas', prepared['check_job']['run_id']); flow.advance_snapshot(snapshot)
    running = flow.get(STUDENT, prepared['preparation_id'])
    # Simulate another accepted submission arriving while the exact private
    # snapshot is running.  Promotion must never append to the wrong parent.
    late = store.submit(project['project_id'], STUDENT, 'minor', _files(changed=True), [])
    _finish(jobs, 'flow-cas', running['run_job']['run_id']); flow.advance_snapshot(snapshot)
    conflict = flow.get(STUDENT, prepared['preparation_id'])
    assert conflict['status'] == 'conflict' and conflict['submission_id'] is None
    assert store.list_submissions(STUDENT, personal=True)[0]['id'] == late['id']


def test_idempotency_owner_binding_parent_cas_and_queue_capacity_retry(tmp_path: Path, monkeypatch):
    store, flow, project = _flow(tmp_path)
    request_id = str(uuid.uuid4())
    fingerprint = hashlib.sha256(request_id.encode()).hexdigest()
    first = flow.prepare(STUDENT, project['project_id'], files=_files(), data_references=[], kind='minor',
                         case_version='0.1', request_id=request_id, request_fingerprint=fingerprint)
    again = flow.prepare(STUDENT, project['project_id'], files=_files(), data_references=[], kind='minor',
                         case_version='0.1', request_id=request_id, request_fingerprint=fingerprint)
    assert again['preparation_id'] == first['preparation_id']
    with pytest.raises(KitError, match='无权|只能准备自己的项目'):
        flow.find(OTHER, project['project_id'], request_id, fingerprint)
    snapshot = _snapshot_id(store, first['preparation_id'])
    jobs = Jobs(store); jobs.start_worker('flow-retry', 994)
    _finish(jobs, 'flow-retry', first['check_job']['run_id'])
    original = flow.jobs.queue_internal
    monkeypatch.setattr(flow.jobs, 'queue_internal', lambda *args: (_ for _ in ()).throw(KitError('待处理作业总数已达上限 32')))
    flow.advance_snapshot(snapshot)
    waiting = flow.get(STUDENT, first['preparation_id'])
    assert waiting['status'] == 'checking' and waiting['run_job'] is None and '等待运行队列' in waiting['reason']
    monkeypatch.setattr(flow.jobs, 'queue_internal', original)
    flow.advance_snapshot(snapshot)
    assert flow.get(STUDENT, first['preparation_id'])['status'] == 'running'
