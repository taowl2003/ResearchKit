"""Stable task families retain old task versions and recipient access."""
from __future__ import annotations

import concurrent.futures
import hashlib
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_assignments import AssignmentService
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_task_rpc import request as task_request


TEACHER = Principal(9100, 'ly', 'teacher')
STUDENT_A = Principal(9102, 'test', 'student')
STUDENT_B = Principal(9103, 'student-b', 'student')


def _request(*, parent_task_id: str | None = None, name: str = '同名任务') -> dict[str, str]:
    value = {'request_id': str(uuid.uuid4()), 'name': name, 'instructions': '版本化任务。'}
    if parent_task_id is not None:
        value['parent_task_id'] = parent_task_id
    return value


def _algorithm(ref: str = 'version.tool@1.0.0') -> dict[str, object]:
    return {'ref': ref, 'metadata': {}, 'files': {'plugin.py': b'# test tool\n'}}


def _reference(identifier: str = 'version-data') -> dict[str, object]:
    return {'dataset_id': identifier, 'path': 'data.csv', 'size': 3,
            'sha256': hashlib.sha256(b'a,b').hexdigest()}


@pytest.fixture
def delivery(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'version baseline')
    baseline = store.submit(project['project_id'], TEACHER, 'major', {
        'case.py': b'def run(context): return {}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: version\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    return TaskDelivery(store), store, baseline


def _formal(service: TaskDelivery, baseline: dict[str, object], request: dict[str, str], targets: list[Principal]):
    return service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'], targets=targets)


def test_new_versions_share_only_explicit_parent_family_and_keep_old_recipient_visible(delivery):
    service, store, baseline = delivery
    original = _formal(service, baseline, _request(), [STUDENT_A])
    unrelated = _formal(service, baseline, _request(), [STUDENT_A])
    second = _formal(service, _next_baseline(store, baseline),
                     _request(parent_task_id=original['task_id']), [STUDENT_B])

    assert original['family_id'] == original['task_id']
    assert original['task_version'] == 1 and original['parent_task_id'] is None
    assert unrelated['name'] == original['name']
    assert unrelated['family_id'] != original['family_id']
    assert second['family_id'] == original['family_id']
    assert second['task_version'] == 2 and second['parent_task_id'] == original['task_id']
    assert [row['task_id'] for row in service.list_tasks(STUDENT_A)] == [unrelated['task_id'], original['task_id']]
    assert [row['task_id'] for row in service.list_tasks(STUDENT_B)] == [second['task_id']]


def test_retry_of_existing_version_wins_before_parent_head_check(delivery):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    request_v2 = _request(parent_task_id=first['task_id'])
    second_baseline = _next_baseline(store, baseline)
    second = _formal(service, second_baseline, request_v2, [STUDENT_A])
    third = _formal(service, _next_baseline(store, second_baseline),
                    _request(parent_task_id=second['task_id']), [STUDENT_A])

    retry = _formal(service, second_baseline, request_v2, [STUDENT_A])
    assert retry['task_id'] == second['task_id']
    assert third['task_version'] == 3


def test_only_one_concurrent_new_version_can_extend_a_current_family_head(delivery):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    requests = [_request(parent_task_id=first['task_id']) for _ in range(2)]
    second_baseline = _next_baseline(store, baseline)

    def create(request):
        return _formal(service, second_baseline, request, [STUDENT_A])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda request: _capture(create, request), requests))
    created = [item for item in outcomes if not isinstance(item, Exception)]
    failed = [item for item in outcomes if isinstance(item, Exception)]
    assert len(created) == 1 and created[0]['task_version'] == 2
    assert len(failed) == 1 and '最新任务版本' in str(failed[0])


def _capture(function, *args):
    try:
        return function(*args)
    except Exception as exc:  # checked by the calling test
        return exc


def test_parent_must_be_current_same_teacher_kind_and_delivery_mode(delivery):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    with pytest.raises(KitError, match='只有教师'):
        service.validate_parent(STUDENT_A, first['task_id'], kind='formal', delivery_mode='project')
    with pytest.raises(KitError, match='类型和投递方式'):
        service.create_resource_task(TEACHER, _request(parent_task_id=first['task_id']), targets=[STUDENT_A],
                                     algorithm_materials=[_algorithm()])
    second = _formal(service, _next_baseline(store, baseline),
                     _request(parent_task_id=first['task_id']), [STUDENT_A])
    with pytest.raises(KitError, match='最新任务版本'):
        service.validate_parent(TEACHER, first['task_id'], kind='formal', delivery_mode='project')
    assert service.validate_parent(TEACHER, second['task_id'], kind='formal', delivery_mode='project') == {
        'parent_task_id': second['task_id'], 'family_id': first['task_id'], 'next_task_version': 3,
    }


def test_project_claim_state_is_read_from_distribution_not_task_recipient_copy(delivery):
    service, store, baseline = delivery
    task = _formal(service, baseline, _request(), [STUDENT_A])
    distribution_id = task['distributions'][0]['distribution_id']
    claimed = store.claim_distribution(distribution_id, STUDENT_A)
    store.confirm_distribution(distribution_id, STUDENT_A)
    # task recipients deliberately keep their legacy project_id NULL; the
    # personal task response still reports the actual claimed distribution.
    with store._connection() as connection:
        assert connection.execute('SELECT project_id FROM web_task_recipients WHERE task_id=?',
                                  (task['task_id'],)).fetchone()[0] is None
    personal = service.get_task(STUDENT_A, task['task_id'], personal=True)
    assert personal['claim_status'] == 'claimed'
    assert personal['project_id'] == claimed['project']['project_id']


def _next_baseline(store: SubmissionStore, baseline: dict[str, object]) -> dict[str, object]:
    """Make the distinct immutable snapshot that a real staged assignment creates."""
    source = store.get_submission(str(baseline['submission_id']), TEACHER)
    return store.submit(str(source['project']['project_id']), TEACHER, 'minor',
                        dict(source['files']), [])


def test_new_project_version_rejects_reusing_old_baseline_without_writing_task_or_grants(delivery):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    with pytest.raises(KitError, match='新任务版本需要独立固定基线'):
        _formal(service, baseline, _request(parent_task_id=first['task_id']), [STUDENT_A])
    assert [row['task_id'] for row in service.list_tasks(TEACHER)] == [first['task_id']]
    with store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM web_task_recipients').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM distributions').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM web_task_grants').fetchone()[0] == 0


def test_new_project_version_with_distinct_snapshot_has_independent_distribution_and_project(delivery):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    second = _formal(service, _next_baseline(store, baseline),
                     _request(parent_task_id=first['task_id']), [STUDENT_A])
    first_distribution = first['distributions'][0]['distribution_id']
    second_distribution = second['distributions'][0]['distribution_id']
    assert first_distribution != second_distribution
    first_project = store.claim_distribution(first_distribution, STUDENT_A)['project']['project_id']
    second_project = store.claim_distribution(second_distribution, STUDENT_A)['project']['project_id']
    assert first_project != second_project


def test_personal_task_summary_counts_only_supplements_addressed_to_that_recipient(delivery):
    service, _store, baseline = delivery
    task = _formal(service, baseline, _request(), [STUDENT_A, STUDENT_B])
    service.supplement(TEACHER, task['task_id'],
                       {'request_id': str(uuid.uuid4()), 'instructions': '只补给 test。'},
                       targets=[STUDENT_A], algorithm_materials=[_algorithm('supplement.one@1.0.0')])
    service.supplement(TEACHER, task['task_id'],
                       {'request_id': str(uuid.uuid4()), 'instructions': '只补给 student-b。'},
                       targets=[STUDENT_B], algorithm_materials=[_algorithm('supplement.two@1.0.0')])
    by_student = {item['task_id']: item for item in service.list_tasks(STUDENT_A)}[task['task_id']]
    by_other = {item['task_id']: item for item in service.list_tasks(STUDENT_B)}[task['task_id']]
    assert by_student['supplement_count'] == 1 and isinstance(by_student['latest_supplement_at'], str)
    assert by_other['supplement_count'] == 1 and isinstance(by_other['latest_supplement_at'], str)


def test_upgrade_keeps_old_rows_as_their_own_version_one_family(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    task_id = str(uuid.uuid4())
    with store._connection() as connection:
        connection.execute('''CREATE TABLE web_task_deliveries (
            id TEXT PRIMARY KEY,kind TEXT NOT NULL,teacher_uid INTEGER NOT NULL,teacher_username TEXT NOT NULL,
            baseline_submission_id TEXT NOT NULL,baseline_sha256 TEXT NOT NULL,name TEXT NOT NULL,
            instructions TEXT NOT NULL,created_at TEXT NOT NULL)''')
        connection.execute('''INSERT INTO web_task_deliveries VALUES(?,?,?,?,?,?,?,?,?)''',
                           (task_id, 'formal', TEACHER.uid, TEACHER.username, 'old', 'a' * 64,
                            'old task', '', 'now'))
    service = TaskDelivery(store)
    with store._connection() as connection:
        row = connection.execute('SELECT family_id,task_version,parent_task_id FROM web_task_deliveries WHERE id=?',
                                 (task_id,)).fetchone()
    assert tuple(row) == (None, 1, None)
    listed = service.list_tasks(TEACHER)
    assert listed[0]['family_id'] == task_id
    assert listed[0]['task_version'] == 1 and listed[0]['parent_task_id'] is None


def test_assignment_payload_adds_version_and_baseline_tool_flags_only_when_enabled():
    base = {
        'request_id': str(uuid.uuid4()), 'name': 'assignment', 'template_ref': 'case@1.0.0',
        'algorithm_refs': [], 'dataset_ids': [], 'targets': ['test'], 'instructions': '',
    }
    old, old_fingerprint = AssignmentService._normalize(None, base)
    disabled, disabled_fingerprint = AssignmentService._normalize(
        None, {**base, 'include_baseline_algorithms': False})
    parent = str(uuid.uuid4())
    enabled, enabled_fingerprint = AssignmentService._normalize(
        None, {**base, 'parent_task_id': parent, 'include_baseline_algorithms': True})
    assert old == disabled and old_fingerprint == disabled_fingerprint
    assert enabled['parent_task_id'] == parent
    assert enabled['include_baseline_algorithms'] is True
    assert enabled_fingerprint != old_fingerprint


def test_preview_rpc_rejects_non_current_parent_before_any_task_write(delivery, tmp_path: Path, monkeypatch):
    service, store, baseline = delivery
    first = _formal(service, baseline, _request(), [STUDENT_A])
    _formal(service, _next_baseline(store, baseline), _request(parent_task_id=first['task_id']), [STUDENT_A])
    class EmptyComponents:
        def __init__(self, *_args):
            pass

        def component(self, *_args):  # pragma: no cover - no algorithms requested
            raise AssertionError('not used')

    monkeypatch.setattr('researchkit.web_task_rpc.ComponentService', EmptyComponents)
    server = SimpleNamespace(
        store=store,
        policy=SimpleNamespace(code_root=tmp_path),
        _dataset_catalog=lambda _teacher: [],
        _assignment_targets=lambda _teacher, names, refs: ([STUDENT_A for _name in names], refs),
    )
    payload = {
        'request_id': str(uuid.uuid4()), 'name': 'next', 'template_ref': 'case@1.0.0',
        'algorithm_refs': [], 'dataset_ids': [], 'targets': ['test'], 'instructions': '',
        'parent_task_id': first['task_id'],
    }
    with pytest.raises(KitError, match='最新任务版本'):
        task_request(server, TEACHER, 'validate_assignment_targets', {
            'target_usernames': ['test'], 'data_references': [], 'task_payload': payload,
        })
    assert len(service.list_tasks(TEACHER)) == 2
