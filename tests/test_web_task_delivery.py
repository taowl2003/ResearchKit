from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery


TEACHER = Principal(1000, 'ly', 'teacher')
STUDENT_A = Principal(1001, 'test', 'student')
STUDENT_B = Principal(1002, 'student-b', 'student')
OUTSIDER = Principal(1003, 'outsider', 'student')


def _algorithm(ref: str = 'example.extra@1.0.0') -> dict[str, object]:
    return {
        'ref': ref,
        'metadata': {'display_name': '附加工具'},
        'files': {
            'plugin.yaml': b'schema: hydro.plugin/v1\nname: example.extra\nversion: 1.0.0\n',
            'plugin.py': b'def run(inputs, parameters, context):\n    return {}\n',
        },
    }


def _reference(identifier: str = 'dataset-extra') -> dict[str, object]:
    return {'dataset_id': identifier, 'path': 'data.csv', 'size': 5,
            'sha256': hashlib.sha256(b'a,b\n1').hexdigest()}


def _payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        'request_id': str(uuid.uuid4()), 'name': '降雨练习', 'instructions': '使用基础案例完成练习。',
    }
    value.update(changes)
    return value


@pytest.fixture
def delivery(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'private')
    project = store.create_project(TEACHER, '可运行案例')
    baseline = store.submit(project['project_id'], TEACHER, 'major', {
        'case.py': b'def run(context):\n    return {"status": "ok"}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    return TaskDelivery(store), store, baseline


def test_formal_task_keeps_baseline_immutable_and_retries_without_duplicate(delivery):
    service, store, baseline = delivery
    request = _payload()
    first = service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'],
                                targets=[STUDENT_A, STUDENT_B], algorithm_materials=[_algorithm()],
                                dataset_references=[_reference()])
    second = service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'],
                                 targets=[STUDENT_A, STUDENT_B], algorithm_materials=[_algorithm()],
                                 dataset_references=[_reference()])
    assert second['task_id'] == first['task_id']
    assert {item['target_username'] for item in first['distributions']} == {'test', 'student-b'}
    assert store.get_submission(baseline['submission_id'], TEACHER)['files'] == {
        'case.py': b'def run(context):\n    return {"status": "ok"}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }
    materials = first['materials']
    algorithm = next(item for item in materials if item['kind'] == 'algorithm')
    assert algorithm['supplement_id'] is None
    assert algorithm['ref'] == 'example.extra@1.0.0'
    assert algorithm['manifest'][0]['path'] == 'plugin.py'
    assert all(item['kind'] != 'algorithm' or item['ref'] != 'base' for item in materials)
    assert len(service.list_tasks(STUDENT_A)) == 1


def test_trial_accepts_teacher_review_of_student_source_and_is_separate_from_sender(delivery):
    service, store, _baseline = delivery
    student_project = store.create_project(STUDENT_A, '学生试作')
    submitted = store.submit(student_project['project_id'], STUDENT_A, 'major', {'case.py': b'print("trial")\n'}, [])
    task = service.create_trial(TEACHER, _payload(name='试用学生提交'),
                                source_submission_id=submitted['submission_id'], targets=[STUDENT_B])
    assert task['kind'] == 'trial'
    assert task['baseline_submission_id'] == submitted['submission_id']
    assert task['teacher_username'] == 'ly'
    assert task['distributions'][0]['baseline_submission_id'] == submitted['submission_id']


def test_supplement_is_visible_only_to_selected_original_recipients_and_is_idempotent(delivery):
    service, _store, baseline = delivery
    task = service.create_task(TEACHER, _payload(), baseline_submission_id=baseline['submission_id'],
                               targets=[STUDENT_A, STUDENT_B])
    supplement_request = {'request_id': str(uuid.uuid4()), 'instructions': '补充一个可选算法和数据。'}
    added = service.supplement(TEACHER, task['task_id'], supplement_request, targets=[STUDENT_A],
                               algorithm_materials=[_algorithm('example.supplement@2.0.0')],
                               dataset_references=[_reference('dataset-supplement')])
    again = service.supplement(TEACHER, task['task_id'], supplement_request, targets=[STUDENT_A],
                               algorithm_materials=[_algorithm('example.supplement@2.0.0')],
                               dataset_references=[_reference('dataset-supplement')])
    assert again['supplement_id'] == added['supplement_id']
    visible_a = service.get_task(STUDENT_A, task['task_id'], include_materials=True)
    visible_b = service.get_task(STUDENT_B, task['task_id'], include_materials=True)
    assert {item['ref'] for item in visible_a['materials']} >= {'example.supplement@2.0.0', 'dataset-supplement'}
    assert {item['ref'] for item in visible_b['materials']} == set()
    with pytest.raises(KitError, match='原任务接收者'):
        service.supplement(TEACHER, task['task_id'],
                           {'request_id': str(uuid.uuid4()), 'instructions': ''}, targets=[OUTSIDER])
    with pytest.raises(KitError, match='无权'):
        service.get_task(OUTSIDER, task['task_id'], include_materials=True)


def test_material_files_and_grants_are_exact_and_revocable_without_touching_other_grants(delivery):
    service, _store, baseline = delivery
    task = service.create_task(TEACHER, _payload(), baseline_submission_id=baseline['submission_id'],
                               targets=[STUDENT_A, STUDENT_B], algorithm_materials=[_algorithm()],
                               dataset_references=[_reference()])
    recipient = service.get_task(STUDENT_A, task['task_id'], include_materials=True, material_files=True)
    algorithm = next(item for item in recipient['materials'] if item['kind'] == 'algorithm')
    assert algorithm['files']['plugin.py'].startswith(b'def run')
    grants = service.list_active_grants(STUDENT_A, task['task_id'])
    assert {item['kind'] for item in grants} == {'algorithm', 'dataset'}
    algorithm_grant = next(item for item in grants if item['kind'] == 'algorithm')
    service.revoke_grant(TEACHER, algorithm_grant['grant_id'])
    remaining_a = service.list_active_grants(STUDENT_A, task['task_id'])
    remaining_b = service.list_active_grants(STUDENT_B, task['task_id'])
    assert {item['kind'] for item in remaining_a} == {'dataset'}
    assert {item['kind'] for item in remaining_b} == {'algorithm', 'dataset'}
    after_revoke = service.get_task(STUDENT_A, task['task_id'], include_materials=True, material_files=True)
    assert {item['kind'] for item in after_revoke['materials']} == {'dataset'}


def test_grants_stay_inactive_until_recoverable_distribution_is_attached(delivery, monkeypatch):
    service, store, baseline = delivery
    request = _payload()
    original = store.create_distributions

    def fail_once(*args, **kwargs):
        raise KitError('temporary dispatch failure')

    monkeypatch.setattr(store, 'create_distributions', fail_once)
    with pytest.raises(KitError, match='temporary'):
        service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'], targets=[STUDENT_A],
                            dataset_references=[_reference()])
    assert service.list_active_grants(STUDENT_A) == []
    monkeypatch.setattr(store, 'create_distributions', original)
    recovered = service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'], targets=[STUDENT_A],
                                    dataset_references=[_reference()])
    assert recovered['distributions']
    assert {item['kind'] for item in service.list_active_grants(STUDENT_A)} == {'dataset'}


def test_teacher_personal_task_view_obeys_its_recipient_membership(delivery):
    service, _store, baseline = delivery
    task = service.create_task(TEACHER, _payload(), baseline_submission_id=baseline['submission_id'],
                               targets=[TEACHER, STUDENT_A])
    service.supplement(TEACHER, task['task_id'],
                       {'request_id': str(uuid.uuid4()), 'instructions': '只给 test 的材料。'},
                       targets=[STUDENT_A], algorithm_materials=[_algorithm('example.test-only@1.0.0')])
    manager = service.get_task(TEACHER, task['task_id'], include_materials=True)
    personal = service.get_task(TEACHER, task['task_id'], include_materials=True, personal=True)
    assert 'example.test-only@1.0.0' in {item['ref'] for item in manager['materials']}
    assert 'example.test-only@1.0.0' not in {item['ref'] for item in personal['materials']}
    assert len(service.list_tasks(TEACHER)) == 1
    own = service.list_tasks(TEACHER, personal=True)
    assert [item['task_id'] for item in own] == [task['task_id']]


def test_request_or_target_identity_conflicts_are_rejected(delivery):
    service, _store, baseline = delivery
    request = _payload()
    service.create_task(TEACHER, request, baseline_submission_id=baseline['submission_id'], targets=[STUDENT_A])
    with pytest.raises(KitError, match='不同内容'):
        service.create_task(TEACHER, {**request, 'name': '不同任务'}, baseline_submission_id=baseline['submission_id'],
                            targets=[STUDENT_A])
    with pytest.raises(KitError, match='重复'):
        service.create_task(TEACHER, _payload(), baseline_submission_id=baseline['submission_id'],
                            targets=[STUDENT_A, Principal(STUDENT_A.uid, 'reused-name', 'student')])


def test_resource_task_is_immediately_visible_and_claims_one_empty_project(delivery):
    service, _store, _baseline = delivery
    request = _payload(name='仅材料包')
    task = service.create_resource_task(TEACHER, request, targets=[STUDENT_A],
                                        algorithm_materials=[_algorithm()], dataset_references=[_reference()])
    assert task['delivery_mode'] == 'resources'
    assert task['baseline_submission_id'] is None
    assert task['distributions'] == []
    assert task['recipients'] == [{'uid': STUDENT_A.uid, 'username': STUDENT_A.username,
                                   'distribution_id': None, 'project_id': None}]
    assert service.create_resource_task(TEACHER, request, targets=[STUDENT_A],
                                        algorithm_materials=[_algorithm()], dataset_references=[_reference()])['task_id'] == task['task_id']
    with pytest.raises(KitError, match='不同内容'):
        service.create_resource_task(TEACHER, {**request, 'name': 'changed'}, targets=[STUDENT_A],
                                     algorithm_materials=[_algorithm()], dataset_references=[_reference()])
    personal = service.get_task(STUDENT_A, task['task_id'], include_materials=True)
    assert personal['distribution_id'] is None
    assert personal.get('project_id') is None
    assert personal['claim_status'] is None
    assert {item['kind'] for item in personal['materials']} == {'algorithm', 'dataset'}
    assert {item['kind'] for item in service.list_active_grants(STUDENT_A, task['task_id'])} == {'algorithm', 'dataset'}
    assert [item['task_id'] for item in service.list_tasks(STUDENT_A)] == [task['task_id']]
    with pytest.raises(KitError, match='无权'):
        service.get_task(OUTSIDER, task['task_id'])

    claiming = service.claim_resource_project(STUDENT_A, task['task_id'])
    assert claiming['owner_uid'] == STUDENT_A.uid
    assert claiming['owner_username'] == STUDENT_A.username
    assert claiming['source'] == 'self-created'
    assert claiming['claim_status'] == 'claiming'
    assert service.claim_resource_project(STUDENT_A, task['task_id'])['project_id'] == claiming['project_id']
    retry = service.get_task(STUDENT_A, task['task_id'])
    assert retry['project_id'] == claiming['project_id'] and retry['claim_status'] == 'claiming'
    confirmed = service.confirm_resource_project(STUDENT_A, task['task_id'], claiming['project_id'])
    assert confirmed['claim_status'] == 'claimed'
    assert service.confirm_resource_project(STUDENT_A, task['task_id'], claiming['project_id'])['claim_status'] == 'claimed'
    assert service.get_task(STUDENT_A, task['task_id'])['claim_status'] == 'claimed'
    with pytest.raises(KitError, match='无权'):
        service.claim_resource_project(OUTSIDER, task['task_id'])


def test_resource_task_requires_materials(delivery):
    service, _store, _baseline = delivery
    with pytest.raises(KitError, match='至少需要'):
        service.create_resource_task(TEACHER, _payload(), targets=[STUDENT_A])


def test_task_schema_upgrade_defaults_existing_rows_to_project_mode(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    task_id = str(uuid.uuid4())
    with store._connection() as connection:
        connection.execute('''CREATE TABLE web_task_deliveries (
            id TEXT PRIMARY KEY,kind TEXT NOT NULL,teacher_uid INTEGER NOT NULL,teacher_username TEXT NOT NULL,
            baseline_submission_id TEXT NOT NULL,baseline_sha256 TEXT NOT NULL,name TEXT NOT NULL,
            instructions TEXT NOT NULL,created_at TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE web_task_recipients (
            task_id TEXT NOT NULL,target_uid INTEGER NOT NULL,target_username TEXT NOT NULL,
            target_role TEXT NOT NULL,distribution_id TEXT,created_at TEXT NOT NULL,
            PRIMARY KEY(task_id,target_uid),UNIQUE(task_id,target_username))''')
        connection.execute('''INSERT INTO web_task_deliveries VALUES(?,?,?,?,?,?,?,?,?)''',
                           (task_id, 'formal', TEACHER.uid, TEACHER.username, 'old-baseline', 'a' * 64,
                            'old task', '', 'now'))
    TaskDelivery(store)
    with store._connection() as connection:
        delivery = connection.execute('SELECT delivery_mode FROM web_task_deliveries WHERE id=?', (task_id,)).fetchone()
        recipient_columns = {row['name'] for row in connection.execute('PRAGMA table_info(web_task_recipients)')}
    assert delivery['delivery_mode'] == 'project'
    assert {'project_id', 'project_name', 'resource_claim_status'} <= recipient_columns
