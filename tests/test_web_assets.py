from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_assets import AssetService
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'student', 'student')


def _files(*, plugin_path: str = 'plugins/scale', task: bool = False) -> dict[str, bytes]:
    result = {
        'project.yaml': f'''schema: researchkit.project/v1
mode: python
entry: case.py
name: source
plugins:
  scale:
    path: {plugin_path}
parameters: {{}}
data: {{}}
requires: {{}}
'''.encode(),
        'case.py': b'def run(context):\n    return {}\n',
        'plugins/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: old
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs: {}
parameters: {}
''',
        # If extraction imports this, the test itself fails before a candidate
        # can be returned.  Static AST validation must leave it inert.
        'plugins/scale/plugin.py': b'raise RuntimeError("must not execute")\ndef run(inputs, parameters, context):\n    return {}\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
    }
    if task:
        result.update({'task.py': b'raise RuntimeError()', '.researchkit/runtime/x': b'x', 'results/x': b'x'})
    return result


def _service(tmp_path: Path) -> tuple[SubmissionStore, AssetService, dict[str, object]]:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'source')
    submission = store.submit(project['project_id'], TEACHER, 'minor', _files(), [])
    return store, AssetService(store, tmp_path / 'library'), submission


def _submission(store: SubmissionStore, files: dict[str, bytes]) -> dict[str, object]:
    project = store.create_project(TEACHER, 'source')
    return store.submit(project['project_id'], TEACHER, 'minor', files, [])


def _review(service: AssetService, candidate: dict[str, object]) -> None:
    service.review(TEACHER, str(candidate['candidate_id']), str(candidate['candidate_sha256']), 'reviewed')


def test_candidate_is_teacher_only_and_static(tmp_path: Path) -> None:
    _store, service, submission = _service(tmp_path)
    with pytest.raises(KitError):
        service.create(STUDENT, submission['id'], 'algorithm', 'published', '1.0.0', 'scale')
    candidate = service.create(TEACHER, submission['id'], 'algorithm', 'published', '1.0.0', 'scale')
    assert candidate['files']['plugin/plugin.py'].startswith(b'raise RuntimeError')
    assert candidate['files']['plugin/plugin.yaml'].find(b'name: published') >= 0
    assert candidate['static_validation']['imports_executed'] is False
    with pytest.raises(KitError): service.get_candidate(STUDENT, candidate['candidate_id'])
    assert service.list_candidates(TEACHER)[0]['candidate_id'] == candidate['candidate_id']
    assert 'files' not in service.list_candidates(TEACHER)[0]


def test_asset_id_stable_review_and_non_overwrite(tmp_path: Path) -> None:
    store, service, submission = _service(tmp_path)
    one = service.create(TEACHER, submission['id'], 'algorithm', 'same', '1.0.0', 'scale')
    assert service.create(TEACHER, submission['id'], 'algorithm', 'same', '1.0.0', 'scale')['candidate_id'] == one['candidate_id']
    two = service.create(TEACHER, submission['id'], 'algorithm', 'same', '1.0.1', 'scale')
    assert one['asset_id'] == two['asset_id'] and one['candidate_id'] != two['candidate_id']
    with pytest.raises(KitError): service.publish(TEACHER, one['candidate_id'], one['candidate_sha256'])
    with pytest.raises(KitError): service.review(TEACHER, one['candidate_id'], '0' * 64, 'review')
    reviewed = service.review(TEACHER, one['candidate_id'], one['candidate_sha256'], 'reviewed source')
    assert reviewed['status'] == 'reviewed'
    published = service.publish(TEACHER, one['candidate_id'], one['candidate_sha256'])
    assert published['status'] == 'published'
    assert service.published_contracts()['scalar@1.0.0'].startswith(b'schema: hydro.contract')
    assert service.publish(TEACHER, one['candidate_id'], one['candidate_sha256'])['candidate_id'] == one['candidate_id']
    changed = dict(_files())
    changed['plugins/scale/plugin.py'] = b'def run(inputs, parameters, context):\n    return {"changed": True}\n'
    next_submission = store.submit(submission['project']['project_id'], TEACHER, 'minor', changed, [])
    other = service.create(TEACHER, next_submission['id'], 'algorithm', 'same', '1.0.0', 'scale')
    service.review(TEACHER, other['candidate_id'], other['candidate_sha256'], 'other')
    # Different candidate, same exact published ref cannot replace it.
    with pytest.raises(KitError): service.publish(TEACHER, other['candidate_id'], other['candidate_sha256'])


def test_template_strips_runtime_and_regenerates_references(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'source')
    ref = {'dataset_id': 'rainfall-v1', 'path': 'rain.csv', 'size': 2, 'sha256': 'a' * 64}
    submitted = store.submit(project['project_id'], TEACHER, 'minor', _files(task=True), [ref])
    service = AssetService(store, tmp_path / 'library')
    candidate = service.create(TEACHER, submitted['id'], 'template', 'lesson', '1.0.0')
    assert 'task.py' not in candidate['files'] and 'results/x' not in candidate['files']
    assert candidate['files']['DATA-REFERENCES.json'] == b'[{"dataset_id":"rainfall-v1","path":"rain.csv","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":2}]\n'
    assert candidate['data_references'] == [ref]


def test_bad_alias_and_published_blob_damage_rejected(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'source')
    bad = store.submit(project['project_id'], TEACHER, 'minor', _files(plugin_path='../outside'), [])
    service = AssetService(store, tmp_path / 'library')
    with pytest.raises(KitError): service.create(TEACHER, bad['id'], 'algorithm', 'bad', '1.0.0', 'scale')
    good_project = store.create_project(TEACHER, 'good')
    good = store.submit(good_project['project_id'], TEACHER, 'minor', _files(), [])
    candidate = service.create(TEACHER, good['id'], 'algorithm', 'ok', '1.0.0', 'scale')
    service.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'ok')
    service.publish(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'])
    with store._connection() as con:
        con.execute("UPDATE web_asset_published_files SET content=x'00' WHERE kind='algorithm' AND ref='ok@1.0.0' AND path='plugin/plugin.py'")
    with pytest.raises(KitError): service.published_files('algorithm', 'ok@1.0.0')
    with store._connection() as con:
        con.execute("DELETE FROM web_asset_published_files WHERE kind='algorithm' AND ref='ok@1.0.0' AND path='contracts/scalar/1.0.0.yaml'")
    with pytest.raises(KitError): service.published_files('algorithm', 'ok@1.0.0')


def test_review_notes_cannot_be_whitespace(tmp_path: Path) -> None:
    _store, service, submission = _service(tmp_path)
    candidate = service.create(TEACHER, submission['id'], 'algorithm', 'words', '1.0.0', 'scale')
    with pytest.raises(KitError):
        service.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], ' \n ')


def test_concurrent_same_ref_publishes_once_without_byte_replacement(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    service = AssetService(store, tmp_path / 'library')
    first = _submission(store, _files())
    changed = _files()
    changed['plugins/scale/plugin.py'] = b'def run(inputs, parameters, context):\n    return {"second": 1}\n'
    second = _submission(store, changed)
    candidates = [
        service.create(TEACHER, first['id'], 'algorithm', 'race', '1.0.0', 'scale'),
        service.create(TEACHER, second['id'], 'algorithm', 'race', '1.0.0', 'scale'),
    ]
    for candidate in candidates:
        _review(service, candidate)

    def publish(candidate: dict[str, object]) -> tuple[str, object]:
        try:
            return 'ok', service.publish(TEACHER, str(candidate['candidate_id']), str(candidate['candidate_sha256']))
        except KitError as exc:
            return 'error', exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, candidates))
    winners = [value for state, value in results if state == 'ok']
    assert len(winners) == 1
    winner = winners[0]
    assert service.published_files('algorithm', 'race@1.0.0') == service.get_candidate(TEACHER, winner['candidate_id'])['files']
    assert sum(1 for state, _ in results if state == 'error') == 1


def test_missing_candidate_and_all_published_files_are_integrity_failures(tmp_path: Path) -> None:
    store, service, submission = _service(tmp_path)
    review_candidate = service.create(TEACHER, submission['id'], 'algorithm', 'missing-review', '1.0.0', 'scale')
    with store._connection() as con:
        con.execute("DELETE FROM web_asset_candidate_files WHERE candidate_id=? AND path='plugin/plugin.py'", (review_candidate['candidate_id'],))
    with pytest.raises(KitError):
        _review(service, review_candidate)

    publish_candidate = service.create(TEACHER, submission['id'], 'algorithm', 'missing-publish', '1.0.0', 'scale')
    _review(service, publish_candidate)
    with store._connection() as con:
        con.execute("DELETE FROM web_asset_candidate_files WHERE candidate_id=? AND path='plugin/plugin.py'", (publish_candidate['candidate_id'],))
    with pytest.raises(KitError):
        service.publish(TEACHER, publish_candidate['candidate_id'], publish_candidate['candidate_sha256'])

    whole = service.create(TEACHER, submission['id'], 'algorithm', 'missing-all', '1.0.0', 'scale')
    _review(service, whole)
    service.publish(TEACHER, whole['candidate_id'], whole['candidate_sha256'])
    with store._connection() as con:
        con.execute("DELETE FROM web_asset_published_files WHERE kind='algorithm' AND ref='missing-all@1.0.0'")
    with pytest.raises(KitError):
        service.published_files('algorithm', 'missing-all@1.0.0')


def test_contract_semantics_reuse_but_type_change_is_atomic_conflict(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    service = AssetService(store, tmp_path / 'library')
    source_one = _submission(store, _files())
    formatted = _files()
    formatted['.researchkit/contracts/scalar/1.0.0.yaml'] = b'description: scalar\ntype: number\nid: scalar@1.0.0\nschema: hydro.contract/v1\n'
    source_two = _submission(store, formatted)
    changed_type = _files()
    changed_type['.researchkit/contracts/scalar/1.0.0.yaml'] = b'schema: hydro.contract/v1\nid: scalar@1.0.0\ntype: string\ndescription: scalar\n'
    source_three = _submission(store, changed_type)
    one = service.create(TEACHER, source_one['id'], 'algorithm', 'one', '1.0.0', 'scale')
    two = service.create(TEACHER, source_two['id'], 'algorithm', 'two', '1.0.0', 'scale')
    conflict = service.create(TEACHER, source_three['id'], 'algorithm', 'three', '1.0.0', 'scale')
    for candidate in (one, two, conflict):
        _review(service, candidate)
    service.publish(TEACHER, one['candidate_id'], one['candidate_sha256'])
    service.publish(TEACHER, two['candidate_id'], two['candidate_sha256'])
    with pytest.raises(KitError):
        service.publish(TEACHER, conflict['candidate_id'], conflict['candidate_sha256'])
    assert service.published_files('algorithm', 'three@1.0.0') is None
    assert {row['ref'] for row in service.published_records()} == {'one@1.0.0', 'two@1.0.0'}


def test_template_contract_then_algorithm_exposes_algorithm_contracts(tmp_path: Path) -> None:
    store, service, submission = _service(tmp_path)
    template = service.create(TEACHER, submission['id'], 'template', 'lesson', '1.0.0')
    _review(service, template)
    service.publish(TEACHER, template['candidate_id'], template['candidate_sha256'])
    assert 'scalar@1.0.0' in service.published_contracts()
    algorithm = service.create(TEACHER, submission['id'], 'algorithm', 'reusable', '1.0.0', 'scale')
    _review(service, algorithm)
    service.publish(TEACHER, algorithm['candidate_id'], algorithm['candidate_sha256'])
    assert service.published_contracts()['scalar@1.0.0'] == algorithm['files']['contracts/scalar/1.0.0.yaml']


def test_review_validation_run_must_match_successful_fixed_submission(tmp_path: Path) -> None:
    store, service, submission = _service(tmp_path)
    candidate = service.create(TEACHER, submission['id'], 'algorithm', 'validated', '1.0.0', 'scale')
    jobs = Jobs(store)
    jobs.start_worker('asset-test-worker', 999)

    failed = jobs.request(TEACHER, submission['id'], 'check')
    assert jobs.claim('asset-test-worker')['run_id'] == failed['run_id']
    jobs.finish('asset-test-worker', failed['run_id'], 'failed', 1, 'failed')
    with pytest.raises(KitError):
        service.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'failed run', failed['run_id'])

    wrong_source = _submission(store, _files())
    wrong = jobs.request(TEACHER, wrong_source['id'], 'check')
    assert jobs.claim('asset-test-worker')['run_id'] == wrong['run_id']
    jobs.finish('asset-test-worker', wrong['run_id'], 'succeeded', 0, None)
    with pytest.raises(KitError):
        service.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'wrong source', wrong['run_id'])

    good = jobs.request(TEACHER, submission['id'], 'check')
    assert jobs.claim('asset-test-worker')['run_id'] == good['run_id']
    jobs.finish('asset-test-worker', good['run_id'], 'succeeded', 0, None)
    assert service.review(TEACHER, candidate['candidate_id'], candidate['candidate_sha256'], 'good source', good['run_id'])['validation_run_id'] == good['run_id']


def test_template_parameter_defaults_and_static_yaml_bounds(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    valid = _files()
    valid['plugins/scale/plugin.yaml'] = valid['plugins/scale/plugin.yaml'].replace(
        b'parameters: {}',
        b'parameters:\n  gain:\n    type: number\n    required: true\n    default: 1\n',
    )
    good = _submission(store, valid)
    service = AssetService(store, tmp_path / 'library')
    candidate = service.create(TEACHER, good['id'], 'template', 'defaults', '1.0.0')
    assert candidate['static_validation']['parameter_values_validated'] is True

    invalid = dict(valid)
    invalid['plugins/scale/plugin.yaml'] = invalid['plugins/scale/plugin.yaml'].replace(b'type: number', b'type: nonsense')
    bad = _submission(store, invalid)
    with pytest.raises(KitError):
        service.create(TEACHER, bad['id'], 'template', 'bad-rules', '1.0.0')

    cyclic = _files()
    cyclic['project.yaml'] = b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: cyclic
plugins: &loop
  scale: *loop
parameters: {}
data: {}
requires: {}
'''
    cycle = _submission(store, cyclic)
    with pytest.raises(KitError):
        service.create(TEACHER, cycle['id'], 'algorithm', 'cyclic', '1.0.0', 'scale')

    too_large = _files()
    too_large['plugins/scale/large.bin'] = b'x' * (4 * 1024 * 1024 + 1)
    oversized = _submission(store, too_large)
    with pytest.raises(KitError):
        service.create(TEACHER, oversized['id'], 'algorithm', 'oversized', '1.0.0', 'scale')
