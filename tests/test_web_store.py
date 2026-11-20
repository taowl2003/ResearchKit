"""Submission-store tests use fabricated Principals, not real OS isolation."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore


TEACHER = Principal(uid=1000, username='ly', role='teacher')
STUDENT_A = Principal(uid=1001, username='student-a', role='student')
STUDENT_B = Principal(uid=1002, username='student-b', role='student')


def _process_submit(directory: str, project_id: str, uid: int, username: str, index: int, queue) -> None:
    store = SubmissionStore(directory)
    result = store.submit(project_id, Principal(uid=uid, username=username, role='student'), 'minor',
                          {f'process-{index}.py': f'broken {index}'.encode()}, [])
    queue.put(result['sequence'])


@pytest.fixture
def store(tmp_path: Path) -> SubmissionStore:
    return SubmissionStore(tmp_path / 'private-submissions')


def test_self_created_project_permissions_and_fixed_submission_reads(store: SubmissionStore):
    # These UID/role values model the broker's trusted identity input.  They do
    # not claim to exercise Linux account or workspace isolation.
    teacher_project = store.create_project(TEACHER, 'teacher study')
    student_project = store.create_project(STUDENT_A, 'student study')
    bad_code = b'def incomplete(:\n'
    submission = store.submit(teacher_project['project_id'], TEACHER, 'minor', {'case.py': bad_code}, [])
    assert submission['version'] == '0.1'
    assert submission['data_status'] == 'unverified'
    assert submission['project'] == store.get_project(teacher_project['project_id'], TEACHER)
    assert store.get_submission(submission['submission_id'], TEACHER)['files'] == {'case.py': bad_code}
    read_project = store.get_submission(submission['submission_id'], TEACHER)['project']
    assert read_project == store.get_project(teacher_project['project_id'], TEACHER)
    with pytest.raises(KitError, match='无权'):
        store.get_submission(submission['submission_id'], STUDENT_B)
    with pytest.raises(KitError, match='无权'):
        store.get_project(student_project['project_id'], TEACHER)
    with pytest.raises(KitError, match='无权'):
        store.get_project(student_project['project_id'], STUDENT_B)
    student_submission = store.submit(student_project['project_id'], STUDENT_A, 'minor', {'main.py': b'bad code'}, [])
    assert [item['submission_id'] for item in store.list_submissions(STUDENT_A)] == [student_submission['submission_id']]
    assert {item['submission_id'] for item in store.list_submissions(TEACHER)} == {
        submission['submission_id'], student_submission['submission_id']}
    with pytest.raises(KitError, match='自己的项目'):
        store.submit(student_project['project_id'], TEACHER, 'minor', {'main.py': b'x'}, [])


def test_version_chain_is_immutable_and_input_maps_cannot_change_old_snapshot(store: SubmissionStore):
    project = store.create_project(STUDENT_A)
    source = {'code.py': b'first'}
    first = store.submit(project['project_id'], STUDENT_A, 'minor', source, [])
    source['code.py'] = b'changed later'
    second = store.submit(project['project_id'], STUDENT_A, 'major', source, [])
    source['new.py'] = b'also later'
    third = store.submit(project['project_id'], STUDENT_A, 'minor', source, [])
    fourth = store.submit(project['project_id'], STUDENT_A, 'major', source, [])
    assert [(item['version'], item['parent_submission_id']) for item in (first, second, third, fourth)] == [
        ('0.1', None), ('1.0', first['submission_id']), ('1.1', second['submission_id']),
        ('2.0', third['submission_id'])]
    assert store.get_submission(first['submission_id'], STUDENT_A)['files'] == {'code.py': b'first'}
    assert store.get_submission(second['submission_id'], STUDENT_A)['files'] == {'code.py': b'changed later'}
    assert store.get_submission(third['submission_id'], STUDENT_A)['files'] == source
    assert fourth['submission_id'] != third['submission_id']
    listed = store.list_submissions(STUDENT_A, project['project_id'])
    assert [item['kind'] for item in listed[:2]] == ['major', 'major']
    assert [item['version'] for item in store.list_submissions(STUDENT_A, project['project_id'], include_minor=False)] == ['2.0', '1.0']


@pytest.mark.parametrize('path', [
    '', '/etc/passwd', '//server/share', 'C:/windows', '../outside.py', 'a/../outside.py',
    'a//b.py', 'a\\b.py', './a.py', '.researchkit/local.json', '.researchkit/last-run.json',
    '_runs/log.txt', '_submission.json',
])
def test_rejects_untrusted_paths_and_limits(store: SubmissionStore, path: str, monkeypatch):
    project = store.create_project(STUDENT_A)
    with pytest.raises(KitError):
        store.submit(project['project_id'], STUDENT_A, 'minor', {path: b'x'}, [])
    with pytest.raises(KitError, match='冲突'):
        store.submit(project['project_id'], STUDENT_A, 'minor', {'a': b'x', 'a/b.py': b'y'}, [])
    monkeypatch.setattr('researchkit.web_store.MAX_FILE_BYTES', 2)
    with pytest.raises(KitError, match='单个'):
        store.submit(project['project_id'], STUDENT_A, 'minor', {'a.py': b'123'}, [])
    monkeypatch.setattr('researchkit.web_store.MAX_FILE_BYTES', 100)
    monkeypatch.setattr('researchkit.web_store.MAX_TOTAL_BYTES', 3)
    with pytest.raises(KitError, match='总量'):
        store.submit(project['project_id'], STUDENT_A, 'minor', {'a.py': b'12', 'b.py': b'34'}, [])


def test_data_references_are_metadata_only_and_validated(store: SubmissionStore):
    project = store.create_project(STUDENT_A)
    reference = {'dataset_id': 'sonar.raw-2026', 'path': 'trials/a.bin', 'size': 12,
                 'sha256': 'a' * 64}
    result = store.submit(project['project_id'], STUDENT_A, 'minor', {'main.py': b'x'}, [reference])
    manifest = store.get_submission(result['submission_id'], STUDENT_A)['manifest']
    assert manifest['data_references']['status'] == 'unverified'
    assert manifest['data_references']['references'] == [reference]
    for bad in ({'dataset_id': 'x', 'path': '/host/path', 'size': 1, 'sha256': 'a' * 64},
                {'dataset_id': 'x', 'path': 'data.bin', 'size': 1, 'sha256': 'A' * 64},
                {'dataset_id': 'x', 'path': 'data.bin', 'size': -1, 'sha256': 'a' * 64}):
        with pytest.raises(KitError):
            store.submit(project['project_id'], STUDENT_A, 'minor', {'other.py': b'x'}, [bad])


def test_concurrent_threads_and_processes_are_serialized_without_overwrite(store: SubmissionStore):
    project = store.create_project(STUDENT_A)
    def submit_thread(index: int) -> int:
        return store.submit(project['project_id'], STUDENT_A, 'minor', {f'thread-{index}.py': b'x'}, [])['sequence']

    with ThreadPoolExecutor(max_workers=6) as executor:
        thread_sequences = list(executor.map(submit_thread, range(6)))
    assert sorted(thread_sequences) == list(range(1, 7))
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    processes = [context.Process(target=_process_submit,
                                 args=(str(store.directory), project['project_id'], STUDENT_A.uid,
                                       STUDENT_A.username, index, queue)) for index in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in processes) == [7, 8, 9]
    versions = store.list_submissions(STUDENT_A, project['project_id'])
    assert len(versions) == 9
    assert len({item['submission_id'] for item in versions}) == 9


def test_private_storage_modes_and_symlinks_are_rejected(tmp_path: Path):
    directory = tmp_path / 'private'
    store = SubmissionStore(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database.stat().st_mode) == 0o600
    target = tmp_path / 'target'
    target.mkdir()
    linked_directory = tmp_path / 'linked-directory'
    linked_directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(KitError, match='非链接'):
        SubmissionStore(linked_directory)
    linked_parent = tmp_path / 'linked-parent'
    linked_parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(KitError, match='父路径'):
        SubmissionStore(linked_parent / 'nested')
    database_link = tmp_path / 'database-link'
    database_link.mkdir(mode=0o700)
    (database_link / 'submissions.sqlite3').symlink_to(tmp_path / 'outside.sqlite3')
    with pytest.raises(KitError, match='非链接'):
        SubmissionStore(database_link)


def test_existing_insecure_storage_and_corrupt_blobs_are_rejected(tmp_path: Path):
    insecure_directory = tmp_path / 'insecure-directory'
    insecure_directory.mkdir(mode=0o700)
    os.chmod(insecure_directory, 0o755)
    with pytest.raises(KitError, match='0700'):
        SubmissionStore(insecure_directory)
    database_directory = tmp_path / 'insecure-database'
    database_directory.mkdir(mode=0o700)
    database = database_directory / 'submissions.sqlite3'
    database.write_bytes(b'')
    os.chmod(database, 0o644)
    with pytest.raises(KitError, match='0600'):
        SubmissionStore(database_directory)

    store = SubmissionStore(tmp_path / 'intact')
    project = store.create_project(STUDENT_A)
    submission = store.submit(project['project_id'], STUDENT_A, 'minor', {'code.py': b'original'}, [])
    connection = store._connect()
    try:
        connection.execute('UPDATE submission_files SET content = ? WHERE submission_id = ?',
                           (b'tampered', submission['submission_id']))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(KitError, match='快照已损坏'):
        store.get_submission(submission['submission_id'], STUDENT_A)


def _distribution_source(store: SubmissionStore) -> tuple[dict[str, object], dict[str, object]]:
    project = store.create_project(TEACHER, 'teacher baseline')
    frozen = store.submit(project['project_id'], TEACHER, 'major',
                          {'case.py': b'fixed baseline', 'data.bin': b'\x00\xff'}, [])
    return project, frozen


def test_teacher_can_assign_and_claim_own_fixed_version(store: SubmissionStore):
    project, frozen = _distribution_source(store)
    distribution = store.create_distribution(TEACHER, frozen['submission_id'], TEACHER)
    claim = store.claim_distribution(distribution['distribution_id'], TEACHER)
    assert claim['project']['project_id'] != project['project_id']
    assert claim['project']['source'] == 'teacher-assigned'
    assert claim['project']['baseline_submission_id'] == frozen['submission_id']
    assert claim['files'] == {'case.py': b'fixed baseline', 'data.bin': b'\x00\xff'}


def test_distribution_isolation_confirmation_idempotence_and_trusted_baseline(store: SubmissionStore):
    _project, frozen = _distribution_source(store)
    distribution = store.create_distribution(TEACHER, frozen['submission_id'], STUDENT_A)
    assert store.create_distribution(TEACHER, frozen['submission_id'], STUDENT_A) == distribution
    distribution_id = distribution['distribution_id']
    assert [item['distribution_id'] for item in store.list_distributions(STUDENT_A)] == [distribution_id]
    assert store.list_distributions(STUDENT_B) == []
    with pytest.raises(KitError, match='无权'):
        store.claim_distribution(distribution_id, STUDENT_B)
    with pytest.raises(KitError, match='无权'):
        store.get_distribution_baseline(distribution_id, STUDENT_B)
    claim = store.claim_distribution(distribution_id, STUDENT_A)
    with pytest.raises(KitError, match='无权'):
        store.confirm_distribution(distribution_id, STUDENT_B)
    first = store.confirm_distribution(distribution_id, STUDENT_A)
    second = store.confirm_distribution(distribution_id, STUDENT_A)
    assert first == second
    assert first['status'] == 'claimed'
    baseline = store.get_distribution_baseline(distribution_id, STUDENT_A)
    assert baseline['submission_id'] == frozen['submission_id']
    assert baseline['files'] == claim['files']


def test_concurrent_claims_share_one_recipient_project(store: SubmissionStore):
    _project, frozen = _distribution_source(store)
    distribution = store.create_distribution(TEACHER, frozen['submission_id'], STUDENT_A)

    def claim() -> dict[str, object]:
        return store.claim_distribution(distribution['distribution_id'], STUDENT_A)

    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(lambda _index: claim(), range(6)))
    project_ids = {item['project']['project_id'] for item in claims}
    assert len(project_ids) == 1
    with store._connection() as connection:
        count = connection.execute('SELECT COUNT(*) FROM projects WHERE owner_uid = ?', (STUDENT_A.uid,)).fetchone()[0]
    assert count == 1


def test_v02_existing_submission_remains_intact_after_distribution_schema_upgrade(tmp_path: Path):
    directory = tmp_path / 'v02-submissions'
    store = SubmissionStore(directory)
    project = store.create_project(STUDENT_A, 'v0.2 project')
    original = store.submit(project['project_id'], STUDENT_A, 'major',
                            {'old.py': b'kept', 'binary.bin': b'\x00\xff'}, [])
    original_snapshot = store.get_submission(original['submission_id'], STUDENT_A)
    with store._connection() as connection:
        # Remove every additive feature table to retain an actual v0.2 schema,
        # including its existing project/submission rows and their exact bytes.
        # Materialize the current view back into the actual pre-upgrade file
        # table; this fixture must represent a genuine old database on disk.
        legacy_files = list(connection.execute('SELECT * FROM submission_files'))
        connection.execute('DROP VIEW submission_files')
        connection.execute('DROP TABLE web_content_refs_submission_files')
        connection.execute('DROP TABLE web_content_tables')
        connection.execute('DROP TABLE web_content_blobs')
        connection.execute('DROP TABLE web_assignment_baselines')
        connection.execute('''CREATE TABLE submission_files (
            submission_id TEXT NOT NULL REFERENCES submissions(id),path TEXT NOT NULL,
            sha256 TEXT NOT NULL,size INTEGER NOT NULL,content BLOB NOT NULL,
            PRIMARY KEY(submission_id,path))''')
        connection.executemany('INSERT INTO submission_files VALUES(?,?,?,?,?)',
                              [tuple(row) for row in legacy_files])
        connection.execute('DROP TABLE package_import_receipts')
        connection.execute('DROP TABLE project_package_sources')
        connection.execute('DROP TABLE package_bundles')
        connection.execute('DROP TABLE project_trusted_sources')
        connection.execute('DROP TABLE distributions')
        old_schema = {row['name']: row['sql'] for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()}
        old_file_rows = connection.execute(
            'SELECT path, size, content FROM submission_files WHERE submission_id = ? ORDER BY path',
            (original['submission_id'],),
        ).fetchall()
    assert set(old_schema) == {'projects', 'submission_files', 'submissions'}
    assert all('CREATE TABLE' in statement for statement in old_schema.values())
    assert [(row['path'], row['size'], bytes(row['content'])) for row in old_file_rows] == [
        ('binary.bin', 2, b'\x00\xff'), ('old.py', 4, b'kept')]
    upgraded = SubmissionStore(directory)
    recovered = upgraded.get_submission(original['submission_id'], STUDENT_A)
    assert recovered['version'] == original_snapshot['version']
    assert recovered['content_sha256'] == original_snapshot['content_sha256']
    assert recovered['manifest'] == original_snapshot['manifest']
    assert recovered['files'] == original_snapshot['files']
    next_submission = upgraded.submit(project['project_id'], STUDENT_A, 'minor', {'next.py': b'new'}, [])
    assert next_submission['version'] == '1.1'
    with upgraded._connection() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'distributions'").fetchone()



def test_only_teacher_may_distribute_own_submissions(store: SubmissionStore):
    student_project = store.create_project(STUDENT_A, 'student source')
    student_submission = store.submit(student_project['project_id'], STUDENT_A, 'minor', {'student.py': b'x'}, [])
    with pytest.raises(KitError, match='只有教师'):
        store.create_distribution(STUDENT_A, student_submission['submission_id'], STUDENT_B)
    with pytest.raises(KitError, match='自己的固定提交'):
        store.create_distribution(TEACHER, student_submission['submission_id'], STUDENT_B)


def test_tampered_distribution_baseline_or_source_is_rejected_on_claim(store: SubmissionStore):
    _project, frozen = _distribution_source(store)
    baseline_tampered = store.create_distribution(TEACHER, frozen['submission_id'], STUDENT_A)
    with store._connection() as connection:
        connection.execute('UPDATE distributions SET baseline_sha256 = ? WHERE id = ?',
                           ('0' * 64, baseline_tampered['distribution_id']))
    with pytest.raises(KitError, match='基线快照已损坏'):
        store.claim_distribution(baseline_tampered['distribution_id'], STUDENT_A)

    source_tampered = store.create_distribution(TEACHER, frozen['submission_id'], STUDENT_B)
    with store._connection() as connection:
        connection.execute('UPDATE submission_files SET content = ? WHERE submission_id = ? AND path = ?',
                           (b'tampered', frozen['submission_id'], 'case.py'))
    with pytest.raises(KitError, match='快照已损坏'):
        store.claim_distribution(source_tampered['distribution_id'], STUDENT_B)
