"""Trusted-package store tests use fabricated broker identities only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore

TEACHER = Principal(uid=1000, username='ly', role='teacher')
STUDENT = Principal(uid=1001, username='student-a', role='student')
OTHER = Principal(uid=1002, username='student-b', role='student')


@pytest.fixture
def store(tmp_path):
    return SubmissionStore(tmp_path / 'private')


def _source(store):
    project = store.create_project(TEACHER, 'teacher bundle')
    submission = store.submit(project['project_id'], TEACHER, 'major',
                              {'case.py': b'fixed', 'data.bin': b'\x00\xff'}, [])
    return submission, store.export_package(submission['submission_id'], TEACHER)


def test_export_is_teacher_owned_stable_and_never_uses_workspace_files(store):
    submission, package = _source(store)
    repeated = store.export_package(submission['submission_id'], TEACHER)
    assert repeated == package
    assert package['bundle']['schema'] == 'researchkit.web-bundle/v1'
    assert len(package['bundle']['bundle_id']) == 32
    with pytest.raises(KitError, match='教师'):
        store.export_package(submission['submission_id'], STUDENT)
    student_project = store.create_project(STUDENT, 'not teacher')
    student_submission = store.submit(student_project['project_id'], STUDENT, 'major', {'x.py': b'x'}, [])
    with pytest.raises(KitError, match='自己的'):
        store.export_package(student_submission['submission_id'], TEACHER)


def test_known_package_requires_exact_bytes_and_unassigned_baseline_is_private(store):
    submission, package = _source(store)
    imported = store.import_package(STUDENT, 'renamed by recipient', package['files'], package['bundle'])
    assert imported['source'] == 'teacher-unassigned'
    assert imported['bundle_id'] == package['bundle']['bundle_id']
    baseline = store.get_package_baseline(imported['project_id'], STUDENT)
    assert baseline['files'] == package['files']
    bad_marker = dict(package['bundle'], baseline_sha256='f' * 64)
    with pytest.raises(KitError, match='摘要不匹配'):
        store.import_package(STUDENT, 'retry with altered marker', package['files'], bad_marker)
    with pytest.raises(KitError, match='无权'):
        store.get_package_baseline(imported['project_id'], OTHER)
    bad = dict(package['files'])
    bad['case.py'] = b'edited package'
    with pytest.raises(KitError, match='不等于'):
        store.import_package(STUDENT, 'bad', bad, package['bundle'])
    # This does not expand ordinary submission visibility.
    with pytest.raises(KitError, match='无权'):
        store.get_submission(submission['submission_id'], STUDENT)


def test_assigned_package_import_reuses_claim_project_and_is_idempotent(store):
    submission, package = _source(store)
    distribution = store.create_distribution(TEACHER, submission['submission_id'], STUDENT)
    first = store.import_package(STUDENT, 'any label', package['files'], package['bundle'])
    assert first['source'] == 'teacher-assigned'
    assert first['bundle_id'] == package['bundle']['bundle_id']
    claimed = store.claim_distribution(distribution['distribution_id'], STUDENT)
    assert claimed['project']['project_id'] == first['project_id']
    assert claimed['files'] == package['files']
    again = store.import_package(STUDENT, 'changed ignored on retry', package['files'], package['bundle'])
    assert again['project_id'] == first['project_id']
    with ThreadPoolExecutor(max_workers=4) as workers:
        projects = list(workers.map(lambda _: store.import_package(STUDENT, 'retry', package['files'], package['bundle'])['project_id'], range(4)))
    assert set(projects) == {first['project_id']}


def test_unknown_and_no_marker_are_self_created_and_marker_is_strict(store):
    _submission, package = _source(store)
    unknown = {'schema': 'researchkit.web-bundle/v1', 'bundle_id': 'f' * 32, 'baseline_sha256': 'a' * 64}
    imported = store.import_package(STUDENT, 'ordinary import', package['files'], unknown)
    assert imported['source'] == 'self-created'
    without = store.import_package(OTHER, 'ordinary no marker', package['files'])
    assert without['source'] == 'self-created'
    with pytest.raises(KitError, match='格式'):
        store.import_package(STUDENT, 'bad marker', package['files'], {'bundle_id': 'x'})


def test_later_assignment_does_not_relabel_existing_unassigned_project(store):
    submission, package = _source(store)
    imported = store.import_package(STUDENT, 'unassigned copy', package['files'], package['bundle'])
    distribution = store.create_distribution(TEACHER, submission['submission_id'], STUDENT)
    repeated = store.import_package(STUDENT, 'retry', package['files'], package['bundle'])
    assert repeated['project_id'] == imported['project_id']
    assert repeated['source'] == 'teacher-unassigned'
    claimed = store.claim_distribution(distribution['distribution_id'], STUDENT)['project']
    assert claimed['project_id'] != imported['project_id']
    assert claimed['source'] == 'teacher-assigned'


def test_reserved_file_and_draft_package_claim_are_rejected_or_safe(store):
    project = store.create_project(TEACHER, 'reserved')
    submission = store.submit(project['project_id'], TEACHER, 'major',
                              {'.researchkit/web-bundle.json': b'fake', 'case.py': b'x'}, [])
    with pytest.raises(KitError, match='保留'):
        store.export_package(submission['submission_id'], TEACHER)
    with pytest.raises(KitError, match='元文件'):
        store.import_package(STUDENT, 'bad', {'.researchkit/web-bundle.json': b'x'})
