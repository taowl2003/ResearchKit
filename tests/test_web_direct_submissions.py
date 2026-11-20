"""Direct Web submission receipts never queue or depend on Web execution."""
from __future__ import annotations

from contextlib import contextmanager
import uuid

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_submission_flow import SubmissionFlow
from researchkit.web_store import SubmissionStore
from researchkit.web_versions import apply_versions, preview_versions
from researchkit.web_workspace_flow import prepare as workspace_prepare
from researchkit.web_workspace_flow import preview as workspace_preview
from researchkit.web_workflow_rpc import request as workflow_request


STUDENT = Principal(2201, 'direct-student', 'student')
OTHER = Principal(2201, 'direct-other', 'student')
TEACHER = Principal(2202, 'ly', 'teacher')


def _files(*, changed: bool = False) -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
name: direct
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


def _direct(store, principal, project_id, files, *, request_id=None, case_version='0.1',
            ready=False, authorize=None, bind=None):
    return workflow_request(
        store, None, principal, 'prepare_submission', {
            'project_id': project_id,
            'files': files,
            'data_references': [],
            'kind': 'minor',
            'case_version': case_version,
            'request_id': request_id or str(uuid.uuid4()),
            'request_fingerprint': 'a' * 64,
        },
        ready=ready,
        authorize=authorize,
        bind=bind,
    )


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    return SubmissionStore(tmp_path / 'store')


def test_direct_receipt_is_immediate_idempotent_and_has_no_execution_records(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    project = store.create_project(STUDENT, 'direct project')
    authorized, bound = [], []
    request_id = str(uuid.uuid4())

    first = _direct(
        store, STUDENT, project['project_id'], _files(), request_id=request_id,
        # A stopped runner must not affect a fixed IDE snapshot submission.
        ready=False, authorize=authorized.append, bind=bound.append,
    )
    assert first['mode'] == 'direct'
    assert first['status'] == 'succeeded'
    assert first['submission_id'] == first['final_submission_id']
    assert first['reason'] == ''
    assert authorized == [[]]
    assert bound == [first['submission_id']]

    retry = _direct(store, STUDENT, project['project_id'], _files(), request_id=request_id)
    assert retry['receipt_id'] == first['receipt_id']
    assert retry['submission_id'] == first['submission_id']
    assert workflow_request(store, None, STUDENT, 'get_submission_preparation', {
        'preparation_id': first['preparation_id'],
    }) == first

    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM web_direct_submission_receipts').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM web_submission_preparations').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1


def test_direct_submission_rechecks_owner_parent_and_version_cas(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    project = store.create_project(STUDENT, 'owned project')
    first = _direct(store, STUDENT, project['project_id'], _files())

    with pytest.raises(KitError, match='无权查看该项目'):
        _direct(store, OTHER, project['project_id'], _files(), case_version='0.1')

    # This mirrors a browser that previewed the first source revision, then
    # another fixed version became its parent before it submitted.
    with pytest.raises(KitError, match='版本必须高于'):
        _direct(store, STUDENT, project['project_id'], _files(changed=True), case_version='0.1')

    unchanged = _direct(store, STUDENT, project['project_id'], _files(), case_version='0.1')
    assert unchanged['status'] == 'unchanged'
    assert unchanged['submission_id'] == first['submission_id']
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 1


def test_direct_submission_requires_and_persists_algorithm_version_upgrade(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    project = store.create_project(STUDENT, 'versioned project')
    _direct(store, STUDENT, project['project_id'], _files())
    changed = _files(changed=True)
    changed['algorithms/scale/plugin.py'] += b'# implementation changed\n'
    plan = SubmissionFlow(store).preview(STUDENT, project['project_id'], changed, 'minor')
    scale = next(row for row in plan['algorithms'] if row['alias'] == 'scale')
    assert scale['suggested_version'] == '1.0.1'
    selected = apply_versions(changed, plan, {
        'case_version': plan['case']['suggested_version'],
        'algorithm_versions': {'scale': scale['suggested_version']},
    })

    result = _direct(store, STUDENT, project['project_id'], selected,
                     case_version=plan['case']['suggested_version'])
    fixed = store.get_submission(result['submission_id'], STUDENT)
    assert result['status'] == 'succeeded'
    assert fixed['version'] == '0.2'
    assert b'version: 1.0.1' in fixed['files']['algorithms/scale/plugin.yaml']


def test_removed_web_execution_routes_reject_and_direct_get_does_not_read_legacy_preparations(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    project = store.create_project(STUDENT, 'legacy boundary')
    files = _files()
    request_id = str(uuid.uuid4())

    with pytest.raises(KitError, match='网页试运行已移除'):
        workflow_request(store, None, STUDENT, 'prepare_project_trial', {
            'project_id': project['project_id'], 'files': files, 'data_references': [],
            'request_id': request_id, 'request_fingerprint': 'a' * 64,
        })
    with pytest.raises(KitError, match='网页执行记录已移除'):
        workflow_request(store, None, TEACHER, 'review_jobs', {})
    with pytest.raises(KitError, match='网页执行记录已移除'):
        workflow_request(store, None, TEACHER, 'review_job', {'run_id': str(uuid.uuid4())})

    legacy = SubmissionFlow(store).prepare(
        STUDENT, project['project_id'], files=files, data_references=[], kind='minor',
        case_version='0.1', request_id=str(uuid.uuid4()), request_fingerprint='b' * 64,
    )
    with pytest.raises(KitError, match='无权查看直接提交回执'):
        workflow_request(store, None, STUDENT, 'get_submission_preparation', {
            'preparation_id': legacy['preparation_id'],
        })


class _WorkspaceStore:
    def __init__(self):
        self.calls = []

    def request(self, method, **params):
        self.calls.append((method, params))
        if method == 'find_submission_preparation':
            return None
        if method == 'preview_submission_versions':
            return preview_versions(params['files'], None, kind=params['kind'])
        if method == 'prepare_submission':
            return {'preparation_id': 'direct-receipt', 'status': 'succeeded',
                    'submission_id': 'fixed-submission'}
        raise AssertionError(method)


class _Workspace:
    def __init__(self, files):
        self.files = files
        self.store = _WorkspaceStore()
        self.selected = []

    @contextmanager
    def _project_guard(self, project_id):
        assert project_id == 'project-id'
        yield

    def _snapshot(self, project_id, *, result_paths=None):
        self.selected.append(result_paths)
        selected = set(result_paths or [])
        files = {path: value for path, value in self.files.items()
                 if not path.startswith('results/') or path in selected}
        return None, files, []

    def _sync_usage(self, project_id):
        assert project_id == 'project-id'


def test_workspace_passes_the_same_result_selection_to_preview_and_direct_snapshot():
    files = {**_files(), 'results/kept.csv': b'value\n1\n', 'results/omitted.csv': b'value\n2\n'}
    workspace = _Workspace(files)
    selection = ['results/kept.csv']
    plan = workspace_preview(workspace, 'project-id', {'kind': 'minor', 'result_paths': selection})
    result = workspace_prepare(workspace, 'project-id', {
        'kind': 'minor', 'expected_revision': plan['snapshot_revision'],
        'case_version': plan['case']['suggested_version'],
        'algorithm_versions': {row['alias']: row['current_version'] for row in plan['algorithms']},
        'request_id': str(uuid.uuid4()), 'result_paths': selection,
    })
    assert result['status'] == 'succeeded'
    direct = next(params for method, params in workspace.store.calls if method == 'prepare_submission')
    assert 'results/kept.csv' in direct['files']
    assert 'results/omitted.csv' not in direct['files']
    assert workspace.selected == [selection, selection]
