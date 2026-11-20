from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_derived import DerivedDatasets
from researchkit.web_runner import execute_claimed
from researchkit.web_store import SubmissionStore
from researchkit.web_task_access import TaskAccess
from researchkit.web_task_delivery import TaskDelivery


TEACHER = Principal(1000, 'ly', 'teacher')
STUDENT = Principal(1001, 'test', 'student')
OTHER = Principal(1002, 'other', 'student')


def _reference(identifier: str = 'raw-task') -> dict[str, object]:
    return {'dataset_id': identifier, 'path': 'data.csv', 'size': 4,
            'sha256': hashlib.sha256(b'x,y\n').hexdigest()}


class Registry:
    def __init__(self, readers):
        self.readers = readers

    def authorize(self, principals, references):
        for principal in principals:
            for reference in references:
                if (principal.uid, principal.username) not in self.readers.get(reference['dataset_id'], set()):
                    raise KitError('denied')
        return references

    def catalog(self, principal):
        items = []
        for dataset_id, readers in self.readers.items():
            if (principal.uid, principal.username) in readers:
                items.append({'dataset_id': dataset_id, 'files': [_reference(dataset_id)]})
        return items

    @contextmanager
    def open_references(self, principals, references, *, cancelled=lambda: False):
        self.authorize(principals, references)
        yield []


class Trees(Registry):
    pass


class Derived:
    enabled = False

    def catalog(self, principal):
        return []


def _payload():
    return {'request_id': str(uuid.uuid4()), 'name': '任务', 'instructions': ''}


@pytest.fixture
def access(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(TEACHER, '基础案例')
    baseline = store.submit(project['project_id'], TEACHER, 'major', {
        'case.py': b'def run(context):\n return {}\n',
        'project.yaml': b'schema: researchkit.project/v1\nname: base\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\norigin: {kind: test}\n',
    }, [])
    reference = _reference()
    raw = Registry({reference['dataset_id']: {(TEACHER.uid, TEACHER.username)}})
    service = TaskDelivery(store)
    task = service.create_task(TEACHER, _payload(), baseline_submission_id=baseline['submission_id'],
                               targets=[STUDENT], dataset_references=[reference])
    return store, service, TaskAccess(store, raw, Trees({}), Derived()), task, reference


def test_exact_task_reference_delegates_only_to_dispatched_target_and_catalogs_it(access):
    _store, _delivery, service, task, reference = access
    assert service.principal_for(STUDENT, reference) == TEACHER
    assert service.catalog(STUDENT) == [{'dataset_id': 'raw-task', 'files': [reference]}]
    with pytest.raises(KitError, match='无权'):
        service.principal_for(OTHER, reference)
    issued = service.authorize_job(STUDENT, STUDENT, str(uuid.uuid4()), [reference])
    assert issued['derived_mounts'] == []
    assert issued['dataset_authorizations'] == [{'reference': reference, 'principals': [
        {'uid': TEACHER.uid, 'username': TEACHER.username, 'role': 'teacher'}]}]


def test_deleted_task_keeps_existing_data_grant_readable(access):
    _store, delivery, service, task, reference = access
    delivery.delete_task(TEACHER, task['task_id'])
    assert service.principal_for(STUDENT, reference) == TEACHER
    assert service.catalog(STUDENT) == [{'dataset_id': 'raw-task', 'files': [reference]}]


def test_revoking_one_task_grant_does_not_remove_another_source(access):
    _store, delivery, service, task, reference = access
    second = delivery.create_task(TEACHER, _payload(), baseline_submission_id=task['baseline_submission_id'],
                                  targets=[STUDENT], dataset_references=[reference])
    grants = delivery.list_active_grants(STUDENT)
    first = next(item for item in grants if item['task_id'] == task['task_id'])
    delivery.revoke_grant(TEACHER, first['grant_id'])
    # The second independent task grant is still an active exact route.
    assert service.principal_for(STUDENT, reference) == TEACHER
    assert second['task_id'] != task['task_id']


def test_resource_task_dataset_grant_is_active_without_a_distribution(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    reference = _reference('resource-only')
    raw = Registry({reference['dataset_id']: {(TEACHER.uid, TEACHER.username)}})
    delivery = TaskDelivery(store)
    task = delivery.create_resource_task(TEACHER, _payload(), targets=[STUDENT], dataset_references=[reference])
    access = TaskAccess(store, raw, Trees({}), Derived())
    assert task['distributions'] == []
    assert access.principal_for(STUDENT, reference) == TEACHER
    assert access.catalog(STUDENT) == [{'dataset_id': 'resource-only', 'files': [reference]}]
    grant = delivery.list_active_grants(STUDENT, task['task_id'])[0]
    delivery.revoke_grant(TEACHER, grant['grant_id'])
    with pytest.raises(KitError, match='无权'):
        access.principal_for(STUDENT, reference)


def test_private_derived_review_right_cannot_be_redelegated(tmp_path: Path):
    store = SubmissionStore(tmp_path / 'store')
    reference = {'dataset_id': str(uuid.uuid4()), 'path': 'data.bin', 'size': 3,
                 'sha256': hashlib.sha256(b'abc').hexdigest()}
    with store._connection() as connection:
        connection.execute('''CREATE TABLE web_derived_datasets (
            dataset_id TEXT PRIMARY KEY,owner_uid INTEGER,owner_username TEXT,name TEXT,visibility TEXT,
            created_at TEXT,size INTEGER,sha256 TEXT,blob_name TEXT,source_kind TEXT,source_run_id TEXT,
            source_submission_id TEXT,source_refs_json TEXT)''')
        connection.execute('''INSERT INTO web_derived_datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (reference['dataset_id'], OTHER.uid, OTHER.username, 'private', 'private', 'now',
                            reference['size'], reference['sha256'], str(uuid.uuid4()), 'upload', None, None, '[]'))

    class ReviewDerived:
        enabled = True
        def catalog(self, principal):
            if (principal.uid, principal.username) in {(OTHER.uid, OTHER.username), (TEACHER.uid, TEACHER.username)}:
                return [{'dataset_id': reference['dataset_id'], 'files': [reference]}]
            return []

    service = TaskAccess(store, Registry({}), Trees({}), ReviewDerived())
    with pytest.raises(KitError, match='审核权限不可转授'):
        service.validate_transfer(TEACHER, [STUDENT], [reference])
    assert service.validate_transfer(OTHER, [STUDENT], [reference]) == [reference]


def test_runner_rejects_forged_or_duplicate_per_reference_mapping(tmp_path: Path):
    reference = _reference()
    job = {'owner_uid': STUDENT.uid, 'owner_username': STUDENT.username,
           'requester_uid': STUDENT.uid, 'requester_username': STUDENT.username,
           'data_references': [reference], 'derived_mounts': [], 'operation': 'check',
           'dataset_authorizations': [
               {'reference': reference, 'principals': [{'uid': TEACHER.uid, 'username': 'ly', 'role': 'teacher'}]},
               {'reference': reference, 'principals': [{'uid': TEACHER.uid, 'username': 'ly', 'role': 'teacher'}]},
           ]}
    with pytest.raises(KitError, match='映射'):
        execute_claimed(job, tmp_path, registry=Registry({}), tree_registry=Trees({}), derived_root=None,
                        code_root=tmp_path, environment=tmp_path, log=lambda text: None, cancelled=lambda: False)


def test_trial_review_grant_only_reaches_the_claimed_project_chain(tmp_path: Path):
    """ly's review of a student blob must not unlock unrelated ly projects."""
    store = SubmissionStore(tmp_path / 'store')
    root = tmp_path / 'derived'
    root.mkdir(mode=0o710)
    derived = DerivedDatasets(store, root)
    reference = {'dataset_id': str(uuid.uuid4()), 'path': 'data.bin', 'size': 3,
                 'sha256': hashlib.sha256(b'abc').hexdigest()}
    with store._connection() as connection:
        connection.execute('''INSERT INTO web_derived_datasets(
            dataset_id,owner_uid,owner_username,name,visibility,created_at,size,sha256,blob_name,
            source_kind,source_run_id,source_submission_id,source_refs_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (reference['dataset_id'], STUDENT.uid, STUDENT.username, 'student private', 'private', 'now',
             reference['size'], reference['sha256'], str(uuid.uuid4()), 'upload', None, None, '[]'))
    project = store.create_project(STUDENT, 'student source')
    source = store.submit(project['project_id'], STUDENT, 'minor', {'case.py': b'pass\n'}, [reference])
    derived.bind_submission(STUDENT, source['submission_id'], [reference], TEACHER)

    task = TaskDelivery(store).create_trial(TEACHER, _payload(), source_submission_id=source['submission_id'],
                                             targets=[TEACHER])
    claimed = store.claim_distribution(task['distributions'][0]['distribution_id'], TEACHER)
    permitted = store.submit(claimed['project']['project_id'], TEACHER, 'minor', {'case.py': b'pass\n'}, [reference])
    assert derived.claim_mounts(TEACHER, TEACHER, permitted['submission_id'], [reference])[0]['reference'] == reference

    unrelated_project = store.create_project(TEACHER, 'unrelated')
    unrelated = store.submit(unrelated_project['project_id'], TEACHER, 'minor', {'case.py': b'pass\n'}, [reference])
    with pytest.raises(KitError, match='不具备派生数据读取授权'):
        derived.claim_mounts(TEACHER, TEACHER, unrelated['submission_id'], [reference])
