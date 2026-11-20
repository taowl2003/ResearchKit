from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_derived import DerivedDatasets
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_review_flow import ReviewFlow
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'student-a', 'student')
OTHER = Principal(1003, 'student-b', 'student')


def _files(*, changed: bool = False) -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: batch source
plugins:
  scale:
    path: algorithms/scale
    parameters: {}
  offset:
    path: algorithms/offset
    parameters: {}
parameters: {}
data: {}
requires: {}
''',
        'case.py': b'def run(context):\n    return {"value": 2}\n' + (b'# changed\n' if changed else b''),
        'algorithms/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: batch.scale
version: 1.0.0
entry: plugin.py
inputs: {}
outputs: {}
parameters: {}
''',
        'algorithms/scale/plugin.py': b'def run(inputs, parameters, context):\n    return {"value": 2}\n',
        'algorithms/offset/plugin.yaml': b'''schema: hydro.plugin/v1
name: batch.offset
version: 1.0.0
entry: plugin.py
inputs: {}
outputs: {}
parameters: {}
''',
        'algorithms/offset/plugin.py': b'def run(inputs, parameters, context):\n    return {"value": 3}\n',
    }


def _complete(store: SubmissionStore, submission_id: str) -> None:
    jobs = Jobs(store)
    jobs.start_worker('batch-review-worker', 991)
    for operation in ('check', 'run'):
        job = jobs.request(TEACHER, submission_id, operation)
        assert jobs.claim('batch-review-worker')['run_id'] == job['run_id']
        jobs.finish('batch-review-worker', job['run_id'], 'succeeded', 0, None)


def _subject(tmp_path: Path, *, files: dict[str, bytes] | None = None,
             references: list[dict[str, object]] | None = None) -> tuple[SubmissionStore, ReviewFlow, dict[str, object]]:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'batch source')
    submission = store.submit(project['project_id'], STUDENT, 'minor', files or _files(), references or [])
    _complete(store, str(submission['id']))
    return store, ReviewFlow(store, tmp_path / 'library'), submission


def _private_derived(store: SubmissionStore, root: Path, owner: Principal) -> tuple[dict[str, object], dict[str, object]]:
    root.mkdir(mode=0o700, exist_ok=True)
    service = DerivedDatasets(store, root)
    data = b'batch derived payload\n'
    digest = hashlib.sha256(data).hexdigest()
    started = service.begin_upload(owner, 'batch-derived', len(data), digest, [])
    service.append_upload(owner, started['upload_id'], 0, data)
    record = service.commit_upload(owner, started['upload_id'], digest)
    return record, record['files'][0]


def _algorithm(alias: str, *, description: str = 'algorithm') -> dict[str, object]:
    return {'kind': 'algorithm', 'alias': alias, 'tags': ['batch'], 'description': description}


def _template(*, name: str = 'batch.case', version: str = '1.0.0', description: str = 'case') -> dict[str, object]:
    return {'kind': 'template', 'name': name, 'version': version, 'tags': ['batch'], 'description': description}


def test_batch_publishes_multiple_algorithms_then_case_and_reuses_exact_resources(tmp_path: Path) -> None:
    store, flow, submission = _subject(tmp_path)
    items = [_template(), _algorithm('scale'), _algorithm('offset')]
    result = flow.publish_batch(TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']), items=items)

    assert result['ok'] is True
    assert [item['kind'] for item in result['items']] == ['template', 'algorithm', 'algorithm']
    assert [item['status'] for item in result['items']] == ['published', 'published', 'published']
    assert {row['ref'] for row in result['published']} == {
        'batch.case@1.0.0', 'batch.scale@1.0.0', 'batch.offset@1.0.0',
    }
    case = result['items'][0]
    assert {row['ref'] for row in case['promoted_algorithms']} == {'batch.scale@1.0.0', 'batch.offset@1.0.0'}

    again = flow.publish_batch(TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']), items=items)
    assert again['ok'] is True
    assert all(item['status'] == 'reused' for item in again['items'])
    with store._connection() as con:
        assert con.execute("SELECT COUNT(*) FROM web_asset_published").fetchone()[0] == 3


def test_batch_derived_must_be_referenced_private_data_and_case_reuses_it(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    owned, reference = _private_derived(store, tmp_path / 'derived', STUDENT)
    unrelated, _ = _private_derived(store, tmp_path / 'derived', OTHER)
    project = store.create_project(STUDENT, 'dataset batch')
    submission = store.submit(project['project_id'], STUDENT, 'minor', _files(), [reference])
    _complete(store, str(submission['id']))
    flow = ReviewFlow(store, tmp_path / 'library')

    with pytest.raises(KitError, match='实际引用'):
        flow.publish_batch(
            TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']),
            items=[{'kind': 'derived', 'dataset_id': unrelated['dataset_id'], 'tags': ['batch'], 'description': 'bad'}],
        )

    result = flow.publish_batch(
        TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']),
        items=[
            _template(name='dataset.case'),
            {'kind': 'derived', 'dataset_id': owned['dataset_id'], 'tags': ['batch'], 'description': 'published from source'},
        ],
    )
    assert result['ok'] is True
    assert result['items'][1]['published'][0]['dataset_id'] == owned['dataset_id']
    assert result['items'][0]['promoted_datasets'][0]['dataset_id'] == owned['dataset_id']
    with store._connection() as con:
        assert con.execute('SELECT visibility FROM web_derived_datasets WHERE dataset_id=?', (owned['dataset_id'],)).fetchone()['visibility'] == 'public'
        assert con.execute('SELECT visibility FROM web_derived_datasets WHERE dataset_id=?', (unrelated['dataset_id'],)).fetchone()['visibility'] == 'private'

    again = flow.publish_batch(
        TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']),
        items=[{'kind': 'derived', 'dataset_id': owned['dataset_id'], 'tags': ['ignored'], 'description': 'ignored'}],
    )
    assert again['ok'] is True and again['items'][0]['status'] == 'reused'


def test_batch_rejects_duplicate_selections_and_rolls_back_earlier_publish(tmp_path: Path) -> None:
    store, flow, first = _subject(tmp_path)
    assert flow.publish(TEACHER, str(first['id']), 'case', None, str(first['content_sha256']),
                        name='collision.case', version='1.0.0', tags=['old'], description='old')['published']
    project_id = str(first['project_id'])
    second = store.submit(project_id, STUDENT, 'minor', _files(changed=True), [])
    _complete(store, str(second['id']))
    result = flow.publish_batch(
        TEACHER, str(second['id']), expected_sha256=str(second['content_sha256']),
        items=[_algorithm('scale'), _template(name='collision.case', version='1.0.0', description='different')],
    )
    assert result == {
        'ok': False, 'rolled_back': True, 'published': [], 'reused': False, 'items': [],
        'failures': [{'alias': None, 'ref': 'collision.case@1.0.0', 'error': '同一精确版本已存在不同内容；请修改案例版本后重新提交'}],
    }
    with store._connection() as con:
        assert con.execute("SELECT 1 FROM web_asset_published WHERE kind='algorithm' AND ref='batch.scale@1.0.0'").fetchone() is None

    with pytest.raises(KitError, match='重复选择'):
        flow.publish_batch(
            TEACHER, str(second['id']), expected_sha256=str(second['content_sha256']),
            items=[_algorithm('scale'), _algorithm('scale')],
        )


def test_batch_forces_modified_platform_algorithm_to_one_upgraded_version_for_case(tmp_path: Path) -> None:
    store, flow, first = _subject(tmp_path)
    assert flow.publish(TEACHER, str(first['id']), 'algorithm', ['scale'], str(first['content_sha256']),
                        tags=['base'], description='base')['published']
    changed = _files()
    changed['project.yaml'] = changed['project.yaml'].replace(
        b'path: algorithms/scale\n    parameters:',
        b'path: algorithms/scale\n    source_ref: batch.scale@1.0.0\n    parameters:',
    )
    changed['algorithms/scale/plugin.py'] = b'def run(inputs, parameters, context):\n    return {"value": 99}\n'
    second = store.submit(str(first['project_id']), STUDENT, 'minor', changed, [])
    _complete(store, str(second['id']))

    result = flow.publish_batch(
        TEACHER, str(second['id']), expected_sha256=str(second['content_sha256']),
        items=[_algorithm('scale', description='upgraded'), _template(name='modified.case')],
    )
    assert result['ok'] is True
    assert result['items'][1]['promoted_algorithms'] == [
        {'alias': 'scale', 'ref': 'batch.scale@1.0.1', 'mode': 'forced_new_version'}
    ]
    with store._connection() as con:
        assert con.execute("SELECT COUNT(*) FROM web_asset_published WHERE kind='algorithm' AND ref='batch.scale@1.0.1'").fetchone()[0] == 1


def test_batch_mixes_two_private_datasets_two_algorithms_and_case(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    first, first_ref = _private_derived(store, tmp_path / 'derived', STUDENT)
    second, second_ref = _private_derived(store, tmp_path / 'derived', STUDENT)
    project = store.create_project(STUDENT, 'many resources')
    submission = store.submit(project['project_id'], STUDENT, 'minor', _files(), [first_ref, second_ref])
    _complete(store, str(submission['id']))
    flow = ReviewFlow(store, tmp_path / 'library')
    result = flow.publish_batch(
        TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']),
        items=[
            _algorithm('scale'),
            {'kind': 'derived', 'dataset_id': first['dataset_id'], 'tags': ['one'], 'description': 'first output'},
            _template(name='many.case'),
            _algorithm('offset'),
            {'kind': 'derived', 'dataset_id': second['dataset_id'], 'tags': ['two'], 'description': 'second output'},
        ],
    )
    assert result['ok'] is True
    assert [row['kind'] for row in result['items']] == ['algorithm', 'derived', 'template', 'algorithm', 'derived']
    assert {row['ref'] for row in result['published'] if row['kind'] != 'derived'} == {
        'batch.scale@1.0.0', 'batch.offset@1.0.0', 'many.case@1.0.0',
    }
    with store._connection() as con:
        rows = con.execute('SELECT dataset_id,visibility FROM web_derived_datasets WHERE dataset_id IN (?,?)',
                           (first['dataset_id'], second['dataset_id'])).fetchall()
    assert {(row['dataset_id'], row['visibility']) for row in rows} == {
        (first['dataset_id'], 'public'), (second['dataset_id'], 'public'),
    }


def test_batch_last_failure_rolls_back_multiple_derived_metadata_and_unrelated_owner(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    first, first_ref = _private_derived(store, tmp_path / 'derived', STUDENT)
    second, second_ref = _private_derived(store, tmp_path / 'derived', STUDENT)
    untouched, _ = _private_derived(store, tmp_path / 'derived', OTHER)
    project = store.create_project(STUDENT, 'rollback all resources')
    submission = store.submit(project['project_id'], STUDENT, 'minor', _files(changed=True), [first_ref, second_ref])
    _complete(store, str(submission['id']))
    flow = ReviewFlow(store, tmp_path / 'library')

    existing_project = store.create_project(STUDENT, 'existing case')
    existing = store.submit(existing_project['project_id'], STUDENT, 'minor', _files(), [])
    _complete(store, str(existing['id']))
    assert flow.publish(TEACHER, str(existing['id']), 'template', None, str(existing['content_sha256']),
                        name='rollback.case', version='1.0.0', tags=['old'], description='old case')['published']
    with store._connection() as con:
        before = {
            row['dataset_id']: tuple(row[key] for key in ('visibility', 'name', 'description', 'tags_json'))
            for row in con.execute('SELECT dataset_id,visibility,name,description,tags_json FROM web_derived_datasets '
                                   'WHERE dataset_id IN (?,?,?)', (first['dataset_id'], second['dataset_id'], untouched['dataset_id']))
        }

    failed = flow.publish_batch(
        TEACHER, str(submission['id']), expected_sha256=str(submission['content_sha256']),
        items=[
            {'kind': 'derived', 'dataset_id': first['dataset_id'], 'name': 'mutated first', 'tags': ['new'], 'description': 'new first'},
            _algorithm('scale'),
            {'kind': 'derived', 'dataset_id': second['dataset_id'], 'name': 'mutated second', 'tags': ['new'], 'description': 'new second'},
            _algorithm('offset'),
            _template(name='rollback.case', version='1.0.0', description='conflicting last item'),
        ],
    )
    assert failed['ok'] is False and failed['rolled_back'] is True and failed['published'] == []
    with store._connection() as con:
        after = {
            row['dataset_id']: tuple(row[key] for key in ('visibility', 'name', 'description', 'tags_json'))
            for row in con.execute('SELECT dataset_id,visibility,name,description,tags_json FROM web_derived_datasets '
                                   'WHERE dataset_id IN (?,?,?)', (first['dataset_id'], second['dataset_id'], untouched['dataset_id']))
        }
        algorithms = con.execute("SELECT ref FROM web_asset_published WHERE kind='algorithm' AND ref IN ('batch.scale@1.0.0','batch.offset@1.0.0')").fetchall()
    assert after == before
    assert algorithms == []


def test_batch_rejects_invalid_shapes_student_and_missing_new_derived_metadata(tmp_path: Path) -> None:
    store, flow, submission = _subject(tmp_path)
    common = {'expected_sha256': str(submission['content_sha256'])}
    for items in ([], [_template(), _template(name='second.case')],
                  [{**_algorithm('scale'), 'unexpected': True}]):
        with pytest.raises(KitError, match='1 到 32|最多选择一个案例|不适用的字段'):
            flow.publish_batch(TEACHER, str(submission['id']), items=items, **common)
    with pytest.raises(KitError, match='教师'):
        flow.publish_batch(STUDENT, str(submission['id']), items=[_algorithm('scale')], **common)

    private, reference = _private_derived(store, tmp_path / 'derived', STUDENT)
    project = store.create_project(STUDENT, 'missing metadata')
    source = store.submit(project['project_id'], STUDENT, 'minor', _files(), [reference])
    _complete(store, str(source['id']))
    with pytest.raises(KitError, match='标签|说明'):
        flow.publish_batch(
            TEACHER, str(source['id']), expected_sha256=str(source['content_sha256']),
            items=[{'kind': 'derived', 'dataset_id': private['dataset_id']}],
        )


def test_batch_new_algorithm_version_inherits_parent_metadata(tmp_path: Path) -> None:
    store, flow, first = _subject(tmp_path)
    assert flow.publish(TEACHER, str(first['id']), 'algorithm', ['scale'], str(first['content_sha256']),
                        tags=['parent'], description='parent description')['published']
    changed = _files()
    changed['algorithms/scale/plugin.py'] = b'def run(inputs, parameters, context):\n    return {"value": 123}\n'
    second = store.submit(str(first['project_id']), STUDENT, 'minor', changed, [])
    _complete(store, str(second['id']))
    published = flow.publish_batch(
        TEACHER, str(second['id']), expected_sha256=str(second['content_sha256']),
        items=[{'kind': 'algorithm', 'alias': 'scale', 'publish_mode': 'new_version',
                'parent_ref': 'batch.scale@1.0.0', 'version': '1.0.1'}],
    )
    assert published['ok'] is True and published['items'][0]['published'][0]['ref'] == 'batch.scale@1.0.1'
    metadata = flow.assets.metadata_map('algorithm')[('algorithm', 'batch.scale@1.0.1')]
    assert metadata['tags'] == ['parent'] and metadata['description'] == 'parent description'
