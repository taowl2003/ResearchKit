"""Jobs tests use fabricated Principals; they do not model OS UID isolation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1000, 'ly', 'teacher')
ALICE = Principal(1001, 'alice', 'student')
BOB = Principal(1002, 'bob', 'student')


@pytest.fixture
def store(tmp_path):
    return SubmissionStore(tmp_path / 'private-submissions')


def _submission(store: SubmissionStore, principal: Principal = ALICE, content: bytes = b'first'):
    project = store.create_project(principal, 'job source')
    return store.submit(project['project_id'], principal, 'minor', {'case.py': content},
                        [{'dataset_id': 'input', 'path': 'input.csv', 'size': 3, 'sha256': 'a' * 64}])


def test_request_authorization_visibility_and_fixed_submission_bytes(store):
    jobs = Jobs(store)
    first = _submission(store)
    own = jobs.request(ALICE, first['submission_id'], 'check')
    teacher = jobs.request(TEACHER, first['submission_id'], 'run')
    with pytest.raises(KitError, match='无权'):
        jobs.request(BOB, first['submission_id'], 'run')
    assert {row['run_id'] for row in jobs.list(ALICE)} == {own['run_id'], teacher['run_id']}
    assert jobs.list(BOB) == []
    with pytest.raises(KitError, match='无权'):
        jobs.get(BOB, teacher['run_id'])

    # A later submission cannot change the captured immutable source.
    store.submit(first['project_id'], ALICE, 'minor', {'case.py': b'later'}, [])
    jobs.start_worker('worker-a', 4444)
    claimed = jobs.claim('worker-a')
    assert claimed is not None
    assert claimed['files'] == {'case.py': b'first'}
    assert claimed['content_sha256'] == first['content_sha256']
    assert claimed['data_references'][0]['dataset_id'] == 'input'


def test_cancel_finish_and_worker_restart_recovery(store):
    jobs = Jobs(store)
    queued = jobs.request(ALICE, _submission(store)['submission_id'], 'check')
    assert jobs.cancel(ALICE, queued['run_id'])['status'] == 'cancelled'

    running = jobs.request(ALICE, _submission(store, content=b'running')['submission_id'], 'run')
    jobs.start_worker('worker-a', 4444)
    claim = jobs.claim('worker-a')
    assert claim is not None and claim['run_id'] == running['run_id']
    assert jobs.cancel(ALICE, running['run_id'])['cancel_requested'] is True
    assert jobs.poll('worker-a', running['run_id']) == {'cancel_requested': True}
    finished = jobs.finish('worker-a', running['run_id'], 'succeeded', 0, None)
    assert finished['status'] == 'cancelled'

    interrupted = jobs.request(ALICE, _submission(store, content=b'interrupted')['submission_id'], 'run')
    claim = jobs.claim('worker-a')
    assert claim is not None and claim['run_id'] == interrupted['run_id']
    jobs.start_worker('worker-b', 5555)
    assert jobs.get(ALICE, interrupted['run_id'])['status'] == 'interrupted'
    with pytest.raises(KitError, match='worker'):
        jobs.append_log('worker-a', interrupted['run_id'], 'late')
    assert jobs.worker_status() == {'online': True, 'execution_uid': 5555}


def test_log_limit_worker_ownership_and_concurrent_claims(store, monkeypatch):
    import researchkit.web_jobs as web_jobs

    jobs = Jobs(store)
    submissions = [_submission(store, content=f'job-{index}'.encode()) for index in range(4)]
    queued = [jobs.request(ALICE, item['submission_id'], 'run') for item in submissions]
    jobs.start_worker('worker-a', 4444)
    with ThreadPoolExecutor(max_workers=5) as executor:
        claimed = list(executor.map(lambda _index: jobs.claim('worker-a'), range(5)))
    ids = [row['run_id'] for row in claimed if row is not None]
    assert set(ids) == {row['run_id'] for row in queued}
    assert len(ids) == 4

    monkeypatch.setattr(web_jobs, '_MAX_LOG_BYTES', 8)
    jobs.append_log('worker-a', ids[0], 'abcdef')
    jobs.append_log('worker-a', ids[0], 'ghijk')
    detail = jobs.get(ALICE, ids[0])
    assert len(detail['log'].encode()) <= 8
    assert detail['log_truncated'] is True
    with pytest.raises(KitError, match='worker'):
        jobs.finish('other-worker', ids[0], 'failed', 1, 'wrong owner')


def test_queue_limits_and_owner_username_binding(store):
    jobs = Jobs(store)
    source = _submission(store)
    for _ in range(4):
        jobs.request(ALICE, source['submission_id'], 'check')
    with pytest.raises(KitError, match='上限 4'):
        jobs.request(ALICE, source['submission_id'], 'check')

    # Four Alice jobs plus seven independent owners fill the global queue.
    for index in range(7):
        owner = Principal(1100 + index, f'owner-{index}', 'student')
        item = _submission(store, owner, f'owner-{index}'.encode())
        for _ in range(4):
            jobs.request(owner, item['submission_id'], 'check')
    final_owner = Principal(1200, 'last-owner', 'student')
    final = _submission(store, final_owner, b'last')
    with pytest.raises(KitError, match='总数已达上限 32'):
        jobs.request(final_owner, final['submission_id'], 'check')

    # UID alone cannot expose jobs when a durable username binding differs.
    reused_uid = Principal(ALICE.uid, 'other-name', 'student')
    assert jobs.list(reused_uid) == []
    with pytest.raises(KitError, match='无权'):
        jobs.get(reused_uid, jobs.list(ALICE)[0]['run_id'])


def _running_job(store: SubmissionStore) -> tuple[Jobs, dict[str, object]]:
    jobs = Jobs(store)
    job = jobs.request(ALICE, _submission(store)['submission_id'], 'run')
    jobs.start_worker('worker-a', 4444)
    claimed = jobs.claim('worker-a')
    assert claimed is not None and claimed['run_id'] == job['run_id']
    return jobs, job


def test_terminal_artifacts_are_private_verified_and_not_in_lists(store):
    jobs, job = _running_job(store)
    finished = jobs.finish('worker-a', job['run_id'], 'succeeded', 0, 'completed', {
        'results/value.json': b'{"value": 6}', 'report/summary.txt': b'ok',
    })
    assert finished['status'] == 'succeeded'
    detail = jobs.get(ALICE, job['run_id'])
    assert detail['artifacts'] == [
        {'path': 'report/summary.txt', 'size': 2,
         'sha256': '2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df'},
        {'path': 'results/value.json', 'size': 12,
         'sha256': 'e13bd7510843f1667b7516f19564e4008fe4579d2782b46ec37099aa8698d629'},
    ]
    assert jobs.artifacts(ALICE, job['run_id']) == {
        'results/value.json': b'{"value": 6}', 'report/summary.txt': b'ok'}
    assert jobs.artifacts(TEACHER, job['run_id'])['report/summary.txt'] == b'ok'
    assert 'artifacts' not in jobs.list(ALICE)[0]
    with pytest.raises(KitError, match='无权'):
        jobs.artifacts(BOB, job['run_id'])


def test_artifact_validation_terminal_immutability_and_damage_detection(store, monkeypatch):
    import researchkit.web_jobs as web_jobs

    jobs, job = _running_job(store)
    with pytest.raises(KitError, match='results'):
        jobs.finish('worker-a', job['run_id'], 'failed', 1, 'bad path', {'outside.txt': b'x'})
    # Validation precedes transition, so a valid terminal result can follow.
    monkeypatch.setattr(web_jobs, '_MAX_ARTIFACT_FILE_BYTES', 2)
    with pytest.raises(KitError, match='单个'):
        jobs.finish('worker-a', job['run_id'], 'failed', 1, 'too big', {'results/a.txt': b'123'})
    monkeypatch.setattr(web_jobs, '_MAX_ARTIFACT_FILE_BYTES', 2)
    monkeypatch.setattr(web_jobs, '_MAX_ARTIFACT_TOTAL_BYTES', 3)
    with pytest.raises(KitError, match='总量'):
        jobs.finish('worker-a', job['run_id'], 'failed', 1, 'too many',
                    {'results/a.txt': b'ab', 'report/b.txt': b'cd'})
    jobs.finish('worker-a', job['run_id'], 'failed', 1, 'partial', {'results/a.txt': b'ab'})
    with pytest.raises(KitError, match='已经结束'):
        jobs.finish('worker-a', job['run_id'], 'failed', 1, 'overwrite', {'results/a.txt': b'xx'})
    with store._connection() as connection:
        connection.execute('UPDATE web_job_artifacts SET content = ? WHERE run_id = ?', (b'bad', job['run_id']))
    with pytest.raises(KitError, match='损坏'):
        jobs.artifacts(ALICE, job['run_id'])
