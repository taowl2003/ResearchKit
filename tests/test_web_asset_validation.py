from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_asset_validation import AssetValidationService
from researchkit.web_assets import AssetService
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'student', 'student')


def _files(*, template: bool = False) -> dict[str, bytes]:
    files = {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: source
plugins:
  scale:
    path: plugins/scale
    parameters:
      factor: 2
parameters: {}
data: {}
requires: {}
''',
        # An algorithm validation must not copy or execute this source case.
        'case.py': (b'def run(context):\n    return {}\n' if template else
                    b'def run(context):\n    raise RuntimeError("source case must not run")\n'),
        'plugins/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: scale
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs:
  result: scalar@1.0.0
parameters:
  factor:
    type: number
    required: true
''',
        'plugins/scale/plugin.py': b'''def run(inputs, parameters, context):
    return {'result': inputs['value'] * parameters['factor']}
''',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
    }
    if template:
        files.update({'.researchkit/runtime/old.py': b'old runtime', 'results/old.json': b'{}'})
    return files


def _candidate(tmp_path: Path, *, kind: str = 'algorithm'):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'asset source')
    submission = store.submit(project['project_id'], TEACHER, 'major', _files(template=kind == 'template'), [])
    assets = AssetService(store, tmp_path / 'library')
    candidate = assets.create(TEACHER, submission['submission_id'], kind, 'validation.asset', '1.0.0',
                              'scale' if kind == 'algorithm' else None)
    return store, assets, candidate


def _case(value=3, factor=2, expected=6):
    return {'inputs': {'value': value}, 'parameters': {'factor': factor},
            'expected_outputs': {'result': expected}}


def _run_snapshot(files: dict[str, bytes]) -> tuple[int, dict[str, object], dict[str, object]]:
    """Exercise the generated project through the trusted worker entry point."""
    import sys
    from researchkit.engine import _worker
    with tempfile.TemporaryDirectory(prefix='rk-asset-validation-run-') as folder:
        root = Path(folder)
        for path, data in files.items():
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        outcome = root / 'outcome.json'
        # A real runner uses a fresh interpreter per isolated snapshot.  The
        # direct test helper must clear that process-local loader cache too.
        sys.modules.pop('_researchkit_case.entry', None)
        sys.modules.pop('_researchkit_case', None)
        status = _worker(root, outcome)
        return status, json.loads(outcome.read_text()), json.loads((root / 'results/asset-validation.json').read_text())


def _execute_snapshot(files: dict[str, bytes]) -> tuple[int, dict[str, object]]:
    import sys
    from researchkit.engine import _worker
    with tempfile.TemporaryDirectory(prefix='rk-asset-template-run-') as folder:
        root = Path(folder)
        for path, data in files.items():
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        outcome = root / 'outcome.json'
        sys.modules.pop('_researchkit_case.entry', None)
        sys.modules.pop('_researchkit_case', None)
        status = _worker(root, outcome)
        return status, json.loads(outcome.read_text())


def test_algorithm_validation_is_internal_fixed_snapshot_and_reportable(tmp_path: Path):
    store, _assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    plan = service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case()])
    assert 'plugins/scale/plugin.py' not in plan.files
    assert '.researchkit/plugins/candidate/plugin.py' in plan.files
    assert plan.files['case.py'].startswith(b'"""Generated internal candidate validation')
    assert '.researchkit/runtime/researchkit/engine.py' in plan.files
    record = service.create(TEACHER, plan)
    assert record['kind'] == 'algorithm' and record['operation'] == 'run'
    assert record['job']['run_id'] == record['run_id']
    assert service.list(TEACHER, candidate['candidate_id'])[0]['run_id'] == record['run_id']

    # Normal submission and queue APIs cannot turn an internal snapshot into a
    # personal workspace record.  Direct job detail remains teacher-only.
    assert store.list_submissions(TEACHER) == [
        row for row in store.list_submissions(TEACHER) if row['submission_id'] != record['submission_id']]
    with pytest.raises(KitError, match='内部验证'):
        store.get_submission(record['submission_id'], TEACHER)
    with pytest.raises(KitError, match='内部验证'):
        Jobs(store).request(TEACHER, record['submission_id'], 'run')
    assert Jobs(store).list(TEACHER) == []
    with pytest.raises(KitError, match='无权'):
        Jobs(store).get(STUDENT, record['run_id'])

    jobs = Jobs(store)
    jobs.start_worker('asset-validation-worker', 999)
    claimed = jobs.claim('asset-validation-worker')
    assert claimed is not None and claimed['run_id'] == record['run_id']
    assert claimed['files']['case.py'] == plan.files['case.py']
    assert 'case.py' in claimed['files'] and b'source case must not run' not in claimed['files']['case.py']
    status, outcome, report = _run_snapshot(plan.files)
    assert status == 0 and outcome['status'] == 'success'
    assert report['passed'] is True and report['cases'][0]['status'] == 'passed'


def test_algorithm_cases_reject_bad_json_types_ports_and_wrong_values(tmp_path: Path):
    store, _assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    args = (TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run')
    bad = [
        [{'inputs': {'value': True}, 'parameters': {'factor': 2}, 'expected_outputs': {'result': 2}}],
        [{'inputs': {'value': 1}, 'parameters': {'factor': True}, 'expected_outputs': {'result': 2}}],
        [{'inputs': {'wrong': 1}, 'parameters': {'factor': 2}, 'expected_outputs': {'result': 2}}],
        [{'inputs': {'value': 1}, 'parameters': {'factor': 2}, 'expected_outputs': {'result': True}}],
        [{'inputs': {'value': float('nan')}, 'parameters': {'factor': 2}, 'expected_outputs': {'result': 2}}],
    ]
    for cases in bad:
        with pytest.raises(KitError):
            service.prepare(*args, cases)
    with pytest.raises(KitError, match='1 到 32'):
        service.prepare(*args, [_case() for _ in range(33)])
    with pytest.raises(KitError, match='64 KiB'):
        service.prepare(*args, [{'inputs': {'value': 'x' * (64 * 1024)},
                                'parameters': {'factor': 2}, 'expected_outputs': {'result': 2}}])
    with pytest.raises(KitError):
        service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], [], [_case()])
    with pytest.raises(KitError, match='摘要'):
        service.prepare(TEACHER, candidate['candidate_id'], '0' * 64, 'run', [_case()])
    with pytest.raises(KitError, match='仅支持 run'):
        service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'check', [_case()])
    good = service.prepare(*args, [_case(), _case(value=4, factor=3, expected=999)])
    generated = good.files['case.py'].decode('utf-8')
    assert 'for index, case in enumerate(CASES, 1)' in generated
    assert "context.call('candidate', inputs=case['inputs'])" in generated
    assert 'asset-validation.json' in generated
    status, outcome, report = _run_snapshot(good.files)
    assert status == 1 and outcome['status'] == 'failed'
    assert [item['status'] for item in report['cases']] == ['passed', 'failed']
    strict = service.prepare(*args, [_case(expected=6.0)])
    strict_status, strict_outcome, strict_report = _run_snapshot(strict.files)
    assert strict_status == 1 and strict_outcome['status'] == 'failed'
    assert strict_report['cases'][0]['status'] == 'failed'


def test_template_validation_reinstalls_runtime_and_rejects_cases(tmp_path: Path):
    store, _assets, candidate = _candidate(tmp_path, kind='template')
    service = AssetValidationService(store, tmp_path / 'library')
    for operation in ('check', 'run'):
        plan = service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], operation)
        assert plan.kind == 'template' and plan.cases == []
        assert '.researchkit/runtime/researchkit/engine.py' in plan.files
        assert '.researchkit/runtime/old.py' not in plan.files
        assert 'results/old.json' not in plan.files
        status, outcome = _execute_snapshot(plan.files)
        assert status == 0 and outcome['status'] == 'success'
    failed = dict(plan.files)
    failed['case.py'] = b"def run(context):\n    raise RuntimeError('template validation failure')\n"
    status, outcome = _execute_snapshot(failed)
    assert status == 1 and outcome['status'] == 'failed'
    with pytest.raises(KitError, match='不接受 cases'):
        service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [])


def test_internal_create_rolls_back_when_queue_is_full(tmp_path: Path):
    store, _assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    project = store.create_project(TEACHER, 'ordinary')
    source = store.submit(project['project_id'], TEACHER, 'major', {'case.py': b'x'}, [])
    jobs = Jobs(store)
    for _ in range(4):
        jobs.request(TEACHER, source['submission_id'], 'run')
    plan = service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case()])
    with pytest.raises(KitError, match='上限 4'):
        service.create(TEACHER, plan)
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 2
        assert con.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 2
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 4
        assert con.execute('SELECT COUNT(*) FROM web_asset_validation_snapshots').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM web_asset_candidate_validations').fetchone()[0] == 0


def test_internal_create_rolls_back_when_global_queue_is_full(tmp_path: Path):
    store, _assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    jobs = Jobs(store)
    for index in range(8):
        owner = Principal(1100 + index, f'owner-{index}', 'student')
        project = store.create_project(owner, f'ordinary-{index}')
        source = store.submit(project['project_id'], owner, 'major', {'case.py': b'x'}, [])
        for _ in range(4):
            jobs.request(owner, source['submission_id'], 'run')
    plan = service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case()])
    with pytest.raises(KitError, match='总数已达上限 32'):
        service.create(TEACHER, plan)
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 9
        assert con.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 9
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 32
        assert con.execute('SELECT COUNT(*) FROM web_asset_validation_snapshots').fetchone()[0] == 0


def test_internal_snapshot_rejects_ordinary_export_distribution_extraction_and_uid_reuse(tmp_path: Path):
    store, assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    record = service.create(TEACHER, service.prepare(
        TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case()]))
    reused_uid = Principal(TEACHER.uid, 'reused', 'student')
    jobs = Jobs(store)
    for action in (lambda: jobs.get(reused_uid, record['run_id']),
                   lambda: jobs.cancel(reused_uid, record['run_id']),
                   lambda: jobs.artifacts(reused_uid, record['run_id']),
                   lambda: store.export_package(record['submission_id'], TEACHER),
                   lambda: store.create_distribution(TEACHER, record['submission_id'], STUDENT),
                   lambda: assets.create(TEACHER, record['submission_id'], 'algorithm', 'blocked', '1.0.0', 'candidate')):
        with pytest.raises(KitError):
            action()


def test_validation_records_are_append_only_and_candidate_review_semantics_stay_source_only(tmp_path: Path):
    store, assets, candidate = _candidate(tmp_path)
    service = AssetValidationService(store, tmp_path / 'library')
    first = service.create(TEACHER, service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case()]))
    second = service.create(TEACHER, service.prepare(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'run', [_case(value=2, factor=5, expected=10)]))
    assert [row['run_id'] for row in service.list(TEACHER, candidate['candidate_id'])] == [second['run_id'], first['run_id']]
    reviewed = assets.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'manual source review')
    assert reviewed['validation_run_id'] is None
    with pytest.raises(KitError):
        service.list(STUDENT, candidate['candidate_id'])
