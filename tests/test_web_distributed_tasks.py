from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_task_rpc import request


TEACHER = Principal(1000, 'ly', 'teacher')
OTHER_TEACHER = Principal(1004, 'other-teacher', 'teacher')
STUDENT_A = Principal(1001, 'student-a', 'student')
STUDENT_B = Principal(1002, 'student-b', 'student')


@pytest.fixture
def distributed(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, 'fixed case')
    baseline = store.submit(project['project_id'], TEACHER, 'major', {
        'case.py': b'def run(context):\n return {}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    return store, TaskDelivery(store), baseline


def _task(delivery: TaskDelivery, baseline: dict[str, object]):
    return delivery.create_task(TEACHER, {
        'request_id': str(uuid.uuid4()), 'name': 'modern task', 'instructions': 'finish it',
    }, baseline_submission_id=baseline['submission_id'], targets=[STUDENT_A, STUDENT_B])


def test_teacher_lists_and_deletes_legacy_distribution_without_touching_claimed_project(distributed):
    store, delivery, baseline = distributed
    legacy = store.create_distribution(TEACHER, baseline['submission_id'], STUDENT_A)
    modern_baseline = store.submit(baseline['project']['project_id'], TEACHER, 'minor', {
        'case.py': b'def run(context):\n return {"revision": 2}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    modern = _task(delivery, modern_baseline)

    listed = request(SimpleNamespace(store=store), TEACHER, 'list_tasks', {'personal': False})['items']
    legacy_row = next(row for row in listed if row['task_id'] == legacy['distribution_id'])
    assert legacy_row['legacy_distribution'] is True
    assert legacy_row['materials'] == [] and legacy_row['grants'] == []
    assert legacy_row['recipients'] == [{'uid': STUDENT_A.uid, 'username': STUDENT_A.username,
                                         'distribution_id': legacy['distribution_id'], 'project_id': None,
                                         'claim_status': 'pending'}]
    assert any(row['task_id'] == modern['task_id'] for row in listed)

    claimed = store.claim_distribution(legacy['distribution_id'], STUDENT_A)
    assert delivery.delete_task(TEACHER, legacy['distribution_id']) == {
        'task_id': legacy['distribution_id'], 'deleted': True}
    assert delivery.delete_task(TEACHER, legacy['distribution_id'])['deleted'] is True
    assert all(row['task_id'] != legacy['distribution_id'] for row in request(
        SimpleNamespace(store=store), TEACHER, 'list_tasks', {'personal': False})['items'])
    assert not any(row['distribution_id'] == legacy['distribution_id']
                   for row in store.list_distributions(STUDENT_A))
    assert store.claim_distribution(legacy['distribution_id'], STUDENT_A)['project']['project_id'] == \
        claimed['project']['project_id']
    with pytest.raises(KitError, match='身份|无权删除'):
        delivery.delete_task(OTHER_TEACHER, legacy['distribution_id'])


def test_deleted_task_is_hidden_and_pending_claim_is_blocked_in_store_and_transaction(distributed, monkeypatch):
    store, delivery, baseline = distributed
    task = _task(delivery, baseline)
    distribution = task['distributions'][0]
    started_distribution = task['distributions'][1]
    started = store.claim_distribution(started_distribution['distribution_id'], STUDENT_B)

    assert [row['task_id'] for row in delivery.list_tasks(STUDENT_A, personal=True)] == [task['task_id']]
    delivery.delete_task(TEACHER, task['task_id'])
    assert delivery.list_tasks(STUDENT_A, personal=True) == []
    assert delivery.list_tasks(STUDENT_B, personal=True) == []
    assert all(row['distribution_id'] != distribution['distribution_id'] for row in store.list_distributions(STUDENT_A))
    assert all(row['distribution_id'] != started_distribution['distribution_id'] for row in store.list_distributions(STUDENT_B))
    assert store.claim_distribution(started_distribution['distribution_id'], STUDENT_B)['project']['project_id'] == \
        started['project']['project_id']
    manager_items = request(SimpleNamespace(store=store), TEACHER, 'list_tasks', {'personal': False})['items']
    assert not any(row['task_id'] == task['task_id'] or row.get('legacy_distribution') and
                   row['task_id'] == distribution['distribution_id'] for row in manager_items)
    with pytest.raises(KitError, match='删除'):
        store.claim_distribution(distribution['distribution_id'], STUDENT_A)
    with pytest.raises(KitError, match='删除'):
        delivery.check_distribution_claimable(STUDENT_A, distribution['distribution_id'])

    racing_baseline = store.submit(baseline['project']['project_id'], TEACHER, 'minor', {
        'case.py': b'def run(context):\n return {"race": True}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    racing = _task(delivery, racing_baseline)
    race_distribution = racing['distributions'][0]['distribution_id']
    original_get_submission = store.get_submission
    deleted = False

    def delete_between_store_reads(*args, **kwargs):
        nonlocal deleted
        if not deleted:
            deleted = True
            delivery.delete_task(TEACHER, racing['task_id'])
        return original_get_submission(*args, **kwargs)

    monkeypatch.setattr(store, 'get_submission', delete_between_store_reads)
    with pytest.raises(KitError, match='删除'):
        store.claim_distribution(race_distribution, STUDENT_A)
    assert deleted is True
    with store._connection() as connection:
        assert connection.execute('SELECT project_id FROM distributions WHERE id=?',
                                  (race_distribution,)).fetchone()['project_id'] is None


def test_pending_legacy_delete_blocks_only_pending_and_keeps_baseline_readable(distributed):
    store, delivery, baseline = distributed
    legacy = store.create_distribution(TEACHER, baseline['submission_id'], STUDENT_A)
    delivery.delete_task(TEACHER, legacy['distribution_id'])
    assert store.get_distribution_baseline(legacy['distribution_id'], STUDENT_A)['submission_id'] == baseline['submission_id']
    with pytest.raises(KitError, match='删除'):
        store.claim_distribution(legacy['distribution_id'], STUDENT_A)


def test_shared_distribution_remains_claimable_until_its_last_task_is_deleted(distributed):
    store, delivery, baseline = distributed
    first = _task(delivery, baseline)
    second = _task(delivery, baseline)
    first_by_user = {row['target_username']: row for row in first['distributions']}
    second_by_user = {row['target_username']: row for row in second['distributions']}
    assert first_by_user['student-a']['distribution_id'] == second_by_user['student-a']['distribution_id']
    assert first_by_user['student-b']['distribution_id'] == second_by_user['student-b']['distribution_id']

    delivery.delete_task(TEACHER, first['task_id'])
    assert [row['task_id'] for row in delivery.list_tasks(STUDENT_A, personal=True)] == [second['task_id']]
    shared_a = first_by_user['student-a']['distribution_id']
    shared_b = first_by_user['student-b']['distribution_id']
    assert any(row['distribution_id'] == shared_a for row in store.list_distributions(STUDENT_A))
    assert store.claim_distribution(shared_a, STUDENT_A)['distribution']['distribution_id'] == shared_a

    delivery.delete_task(TEACHER, second['task_id'])
    assert not any(row['distribution_id'] == shared_b for row in store.list_distributions(STUDENT_B))
    with pytest.raises(KitError, match='删除'):
        store.claim_distribution(shared_b, STUDENT_B)
