from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from researchkit import web_execution_retirement
from researchkit.web_derived import DerivedDatasets
from researchkit.web_execution_retirement import clean_runner_temporary_files, retire_web_execution
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_review_flow import ReviewFlow
from researchkit.web_store import SubmissionStore
from researchkit.web_submission_flow import SubmissionFlow


TEACHER = Principal(701, 'ly', 'teacher')
STUDENT = Principal(702, 'student', 'student')


def _files(value: int = 1) -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: manual-review
plugins: {}
parameters: {}
data: {}
requires: {}
''',
        'case.py': f'def run(context): return {{"value": {value}}}\n'.encode(),
    }


def test_manual_review_needs_no_job_and_batch_rolls_back(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'manual source')
    first = store.submit(project['project_id'], STUDENT, 'minor', _files(), [])
    flow = ReviewFlow(store, tmp_path / 'library')

    result = flow.publish_batch(
        TEACHER, first['id'], expected_sha256=first['content_sha256'],
        items=[{'kind': 'template', 'name': 'manual.case', 'version': '1.0.0',
                'tags': ['manual'], 'description': 'human-reviewed'}],
    )
    assert result['ok'] is True
    with store._connection() as con:
        row = con.execute('SELECT review_type,check_run_id,run_id FROM web_direct_review_publications').fetchone()
        assert tuple(row) == ('manual', None, None)
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='web_jobs'").fetchone() is None

    second = store.submit(project['project_id'], STUDENT, 'minor', _files(2), [])
    failed = flow.publish_batch(
        TEACHER, second['id'], expected_sha256=second['content_sha256'],
        items=[
            {'kind': 'template', 'name': 'manual.case', 'version': '1.0.0',
             'tags': ['manual'], 'description': 'different content'},
        ],
    )
    assert failed['ok'] is False and failed['rolled_back'] is True


def test_old_publication_schema_migrates_and_retirement_is_idempotent(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    with store._connection() as con:
        con.execute('''CREATE TABLE web_direct_review_publications (
            submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, kind TEXT NOT NULL,
            alias TEXT NOT NULL, ref TEXT NOT NULL, candidate_id TEXT, status TEXT NOT NULL,
            reviewer_uid INTEGER NOT NULL, reviewer_username TEXT NOT NULL,
            check_run_id TEXT NOT NULL, run_id TEXT NOT NULL, review_note TEXT NOT NULL,
            published_at TEXT NOT NULL, error TEXT,
            PRIMARY KEY(submission_id,kind,alias,ref))''')
        con.execute('''INSERT INTO web_direct_review_publications VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    ('old', '0' * 64, 'template', '', 'old@1.0.0', None, 'published',
                     701, 'ly', 'check', 'run', 'legacy', 'now', None))
    ReviewFlow(store, tmp_path / 'library')
    with store._connection() as con:
        row = con.execute('SELECT check_run_id,run_id,review_type FROM web_direct_review_publications').fetchone()
        assert tuple(row) == ('check', 'run', 'legacy_execution')

    report = retire_web_execution(store, dry_run=True)
    assert report['dry_run'] is True
    retire_web_execution(store, dry_run=False)
    again = retire_web_execution(store, dry_run=False)
    assert again['web_jobs'] == 0
    with store._connection() as con:
        assert tuple(con.execute('SELECT check_run_id,run_id FROM web_direct_review_publications').fetchone()) == (None, None)


def test_retirement_preserves_derived_review_authorization_without_jobs(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    data_root = tmp_path / 'derived'
    data_root.mkdir(mode=0o700)
    derived = DerivedDatasets(store, data_root)
    payload = b'private result'
    digest = hashlib.sha256(payload).hexdigest()
    upload = derived.begin_upload(STUDENT, 'result', len(payload), digest)
    derived.append_upload(STUDENT, upload['upload_id'], 0, payload)
    record = derived.commit_upload(STUDENT, upload['upload_id'], digest,
                                   source_kind='run', source_run_id='legacy-run', source_submission_id='legacy')

    source_project = store.create_project(STUDENT, 'source project')
    formal = store.submit(source_project['project_id'], STUDENT, 'minor', _files(), [record['files'][0]])
    # Recreate the old service-only snapshot shape.  It has identical immutable
    # content but belongs to a separate project, which makes deletion safe.
    snapshot_project = store.create_project(STUDENT, 'temporary execution snapshot')
    snapshot = store.submit(snapshot_project['project_id'], STUDENT, 'minor', _files(), [record['files'][0]])
    with store._connection() as con:
        con.execute('UPDATE submissions SET version_major=0,version_minor=0 WHERE id=?', (snapshot['id'],))
    jobs = Jobs(store)
    jobs.start_worker('retirement-test-worker', 1)
    job = jobs.request(TEACHER, snapshot['id'], 'run')
    assert jobs.claim('retirement-test-worker')['run_id'] == job['run_id']
    jobs.finish('retirement-test-worker', job['run_id'], 'succeeded', 0, None,
                {'results/out.txt': b'old log artifact'})
    SubmissionFlow(store)
    with store._connection() as con:
        con.execute('''INSERT INTO web_submission_preparations(
            preparation_id,owner_uid,owner_username,source_project_id,expected_parent_id,kind,case_version,
            request_id,request_fingerprint,snapshot_id,check_run_id,run_run_id,final_submission_id,status,
            reason,created_at,updated_at,mode,snapshot_sha256,reused_preparation_id)
            VALUES ('prep',702,'student',?,NULL,'minor','0.1','request','fingerprint',?,NULL,NULL,NULL,
                    'checking','', 'now','now','formal',?,NULL)''',
                    (source_project['project_id'], snapshot['id'], snapshot['content_sha256']))
        con.execute('UPDATE web_derived_datasets SET source_submission_id=? WHERE dataset_id=?',
                    (snapshot['id'], record['dataset_id']))
        con.execute('''INSERT INTO web_derived_review_grants(
            dataset_id,submission_id,teacher_uid,teacher_username,created_at)
            VALUES(?,?,?,?,?)''', (record['dataset_id'], snapshot['id'], TEACHER.uid, TEACHER.username, 'now'))

    report = retire_web_execution(store, dry_run=False)
    assert any(row['dataset_id'] == record['dataset_id'] for row in derived.catalog(TEACHER))
    assert report['converted_derived_grants'] == 1
    with store._connection() as con:
        assert con.execute('SELECT 1 FROM submissions WHERE id=?', (formal['id'],)).fetchone() is not None
        assert con.execute('SELECT 1 FROM submissions WHERE id=?', (snapshot['id'],)).fetchone() is None
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0
        assert con.execute('SELECT 1 FROM web_content_refs_submission_files WHERE submission_id=?',
                           (snapshot['id'],)).fetchone() is None
        assert con.execute('SELECT 1 FROM web_content_refs_submission_files WHERE submission_id=?',
                           (formal['id'],)).fetchone() is not None
        stored = con.execute('SELECT source_run_id,source_metadata_json FROM web_derived_datasets WHERE dataset_id=?',
                             (record['dataset_id'],)).fetchone()
        assert stored['source_run_id'] is None
        assert json.loads(stored['source_metadata_json'])['legacy_execution']['reviewed'] is True
    mounts = derived.claim_mounts(TEACHER, TEACHER, formal['id'], [record['files'][0]])
    assert mounts[0]['reference'] == record['files'][0]
    assert derived.catalog(Principal(703, 'unrelated', 'student')) == []
    assert any(item['dataset_id'] == record['dataset_id'] for item in derived.catalog_page(TEACHER)['items'])
    changed = store.submit(source_project['project_id'], STUDENT, 'minor', _files(2), [record['files'][0]])
    with pytest.raises(Exception, match='授权|无权'):
        derived.claim_mounts(TEACHER, TEACHER, changed['id'], [record['files'][0]])


def test_retirement_refuses_preparation_snapshot_referenced_by_a_published_asset(tmp_path: Path) -> None:
    store = SubmissionStore(tmp_path / 'store')
    source = store.create_project(STUDENT, 'source')
    snapshot_project = store.create_project(STUDENT, 'temporary')
    snapshot = store.submit(snapshot_project['project_id'], STUDENT, 'minor', _files(), [])
    with store._connection() as con:
        con.execute('UPDATE submissions SET version_major=0,version_minor=0 WHERE id=?', (snapshot['id'],))
    SubmissionFlow(store)
    with store._connection() as con:
        con.execute('''INSERT INTO web_submission_preparations(
            preparation_id,owner_uid,owner_username,source_project_id,expected_parent_id,kind,case_version,
            request_id,request_fingerprint,snapshot_id,check_run_id,run_run_id,final_submission_id,status,
            reason,created_at,updated_at,mode,snapshot_sha256,reused_preparation_id)
            VALUES ('protected',702,'student',?,NULL,'minor','0.1','request','fingerprint',?,NULL,NULL,NULL,
                    'checking','', 'now','now','formal',?,NULL)''',
                    (source['project_id'], snapshot['id'], snapshot['content_sha256']))
        con.execute('''CREATE TABLE web_asset_candidates (
            candidate_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL)''')
        con.execute('INSERT INTO web_asset_candidates VALUES (?,?,?)',
                    ('candidate', snapshot['id'], snapshot['content_sha256']))
    preview = retire_web_execution(store, dry_run=True)
    assert preview['unsafe_preparation_ids'] == ['protected']
    assert preview['unsafe_preparations_detail'][0]['reason'] == 'durable_reference:web_asset_candidates'
    with pytest.raises(Exception, match='无法确认'):
        retire_web_execution(store, dry_run=False)
    with store._connection() as con:
        assert con.execute('SELECT 1 FROM submissions WHERE id=?', (snapshot['id'],)).fetchone() is not None


def test_runner_temporary_cleanup_is_policy_scoped_and_rejects_links(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / 'runner'
    root.mkdir(mode=0o700)
    (root / 'worker.lock').write_text('keep')
    stage = root / 'staged-deadbeef'
    stage.mkdir()
    (stage / 'result').write_text('discard')
    monkeypatch.setattr(web_execution_retirement, 'RUNNER_STATE_ROOT', root)
    planned = clean_runner_temporary_files(dry_run=True, runner_uid=__import__('os').geteuid())
    assert planned['runner_temporary_files'] == 1 and stage.exists()
    cleaned = clean_runner_temporary_files(dry_run=False, runner_uid=__import__('os').geteuid())
    assert cleaned['runner_temporary_files'] == 1 and not stage.exists()
    assert (root / 'worker.lock').read_text() == 'keep'

    broken = root / 'staged-link'
    broken.symlink_to(root / 'worker.lock')
    with pytest.raises(Exception, match='符号链接'):
        clean_runner_temporary_files(dry_run=False, runner_uid=__import__('os').geteuid())


def test_retirement_accepts_an_already_promoted_legacy_receipt(tmp_path):
    # Use the old real lifecycle: promotion deletes the private snapshot but
    # retains the idempotency receipt, which must not block the upgrade.
    from test_web_submission_flow import _flow, _prepare, _files as old_files, _finish
    from test_web_submission_flow import STUDENT as old_student
    store, flow, project = _flow(tmp_path)
    prepared = _prepare(flow, project, old_files())
    jobs = Jobs(store)
    jobs.start_worker('upgrade-legacy', 991)
    _finish(jobs, 'upgrade-legacy', prepared['check_job']['run_id'])
    running = flow.get(old_student, prepared['preparation_id'])
    _finish(jobs, 'upgrade-legacy', running['run_job']['run_id'])
    finished = flow.get(old_student, prepared['preparation_id'])
    assert finished['status'] == 'succeeded'
    assert retire_web_execution(store)['unsafe_preparation_ids'] == []
    retire_web_execution(store, dry_run=False)
    assert store.get_submission(finished['submission_id'], old_student)['files'] == old_files()
    with store._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM web_submission_preparations').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0
