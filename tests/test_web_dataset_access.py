"""Real synthetic registry changes across the teacher/worker boundary."""
from types import SimpleNamespace
import pytest
from researchkit.files import KitError
from researchkit import web_data_publisher as publisher
from researchkit.web_broker import BrokerServer, BrokerError
from researchkit.web_dataset_trees import TreeRegistry
from researchkit.web_identity import Principal
from test_web_data_publisher import state, approve


@pytest.fixture
def published(state, monkeypatch):
    root, policy, manager, teacher, student, config = state
    import pwd
    lookup = pwd.getpwnam
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=student.uid) if name == student.username
                        else SimpleNamespace(pw_uid=teacher.uid) if name == 'ly' else lookup(name))
    job = approve(state)
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    yield state, job['candidate']['candidate_id']


def request(published, readers):
    state, candidate_id = published
    _, _, manager, teacher, _, _ = state
    detail = manager.get_candidate(teacher, candidate_id)
    return manager.change_access(teacher, candidate_id, detail['revision'], access_token=detail['access_token'], readers=readers)


def finish(published):
    state, _ = published
    _, policy, manager, _, _, config = state
    job = manager.worker_claim()
    assert job['candidate']['kind'] == 'access-change'
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    return job, result


def test_revoke_restore_and_all_users_keep_exact_data_version(published):
    state, candidate_id = published
    root, policy, manager, teacher, student, config = state
    registry = TreeRegistry(policy.dataset_tree_registry)
    reference = registry.catalog(student)[0]['files'][0]
    manifest = next((root/'dataset-manifests').iterdir())
    untouched = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                 for path in [manifest, policy.dataset_metadata, root/'raw/sonar/input.bin']}
    requested = request(published, ['ly'])
    assert registry.catalog(student) and requested['status'] == 'queued'
    detail = manager.get_candidate(teacher, candidate_id)
    assert detail['status'] == 'published' and detail['readers'] == 'platform-users'
    assert detail['access_history'][0]['status'] == 'queued'
    finish(published)
    assert registry.catalog(student) == [] and registry.catalog(teacher)[0]['files'][0] == reference
    with pytest.raises(KitError, match='无权'):
        registry.authorize([student], [reference])
    with pytest.raises(KitError, match='无权'):
        with registry.open_references([student], [reference]):
            pytest.fail('revoked queued run must not start')
    request(published, ['ly', student.username]); finish(published)
    assert registry.catalog(student)[0]['files'][0] == reference
    request(published, 'platform-users'); finish(published)
    assert registry.catalog(Principal(198765, 'other', 'student'))[0]['files'][0] == reference
    assert untouched == {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in untouched}
    detail = manager.get_candidate(teacher, candidate_id)
    assert detail['readers'] == 'platform-users' and len(detail['access_history']) == 3
    with manager._connection() as conn:
        assert conn.execute("SELECT count(*) FROM data_management_audit WHERE action='access-applied'").fetchone()[0] == 3
    assert list((root/'data-management-backups').iterdir())


def test_stale_form_duplicate_request_and_concurrent_edits(published):
    state, candidate_id = published
    _, _, manager, teacher, _, _ = state
    detail = manager.get_candidate(teacher, candidate_id)
    first = request(published, ['ly'])
    duplicate = request(published, ['ly'])
    assert duplicate['reused'] and duplicate['job_id'] == first['job_id']
    with pytest.raises(KitError, match='正在处理'):
        request(published, 'platform-users')
    with pytest.raises(KitError, match='正在修改'):
        manager.review(teacher, candidate_id, detail['revision'], decision='ignore')
    finish(published)
    with pytest.raises(KitError, match='授权已变化'):
        manager.change_access(teacher, candidate_id, detail['revision'], access_token=detail['access_token'], readers='platform-users')
    assert request(published, ['ly'])['status'] == 'unchanged'


@pytest.mark.parametrize('readers', [[], ['not-a-real-rk-user'], ['ly','ly'], ['root'], ['bad\x00name'], ['ly']*129])
def test_invalid_accounts_do_not_queue_or_mutate(published, readers):
    state, _ = published
    _, policy, manager, teacher, student, _ = state
    before = policy.dataset_tree_registry.read_bytes()
    with pytest.raises(KitError): request(published, readers)
    assert policy.dataset_tree_registry.read_bytes() == before
    assert manager.worker_claim() is None


def test_student_and_identity_spoofing_rejected_by_broker(published):
    state, candidate_id = published
    _, policy, manager, teacher, student, _ = state
    server = BrokerServer(policy)
    detail = manager.get_candidate(teacher, candidate_id)
    params = {'candidate_id': candidate_id, 'revision': detail['revision'], 'access_token': detail['access_token'], 'readers':['ly']}
    with pytest.raises(BrokerError, match='教师'):
        server._dispatch(student, {'method':'data_management_access','params':params})
    with pytest.raises(BrokerError):
        server._dispatch(teacher, {'method':'data_management_access','params':{**params, 'raw_root':'/etc'}})
    assert server._dispatch(teacher, {'method':'data_management_access','params':params})['status'] == 'queued'


def test_changed_source_scan_does_not_cancel_access_change(published):
    state, candidate_id = published
    root, policy, manager, teacher, _, config = state
    original = manager.get_candidate(teacher, candidate_id)
    request(published, ['ly'])
    (root/'raw/sonar/input.bin').write_bytes(b'changed in synthetic test')
    from researchkit.web_dataset_discovery import discover
    with manager._connection() as conn:
        conn.execute('BEGIN IMMEDIATE'); manager._upsert_scan(conn, discover(config, policy)); conn.execute('COMMIT')
    assert manager.get_candidate(teacher, candidate_id)['revision'] > original['revision']
    finish(published)
    detail = manager.get_candidate(teacher, candidate_id)
    assert detail['kind'] == 'data-change' and detail['status'] == 'pending'
    assert detail['access_history'][0]['status'] == 'succeeded'


def test_restart_after_registry_commit_recovers_without_rewriting(published):
    state, candidate_id = published
    _, policy, manager, teacher, _, config = state
    request(published, ['ly']); job = manager.worker_claim()
    publisher.publish(policy, config, job['candidate'])
    committed = policy.dataset_tree_registry.stat().st_ino
    manager.worker_start('restarted')
    retry, result = finish(published)
    assert retry['job_id'] == job['job_id'] and result['reused']
    assert policy.dataset_tree_registry.stat().st_ino == committed
    request(published, 'platform-users'); finish(published)
    with pytest.raises(KitError, match='已完成'):
        publisher.publish(policy, config, job['candidate'])
    assert manager.get_candidate(teacher, candidate_id)['readers'] == 'platform-users'


def test_atomic_failure_preserves_registry_and_can_retry_prepared_receipt(published, monkeypatch):
    state, candidate_id = published
    _, policy, manager, teacher, _, config = state
    request(published, ['ly']); job = manager.worker_claim()
    before = policy.dataset_tree_registry.read_bytes()
    atomic = publisher._atomic
    def fail(path, raw):
        if path == policy.dataset_tree_registry: raise OSError('simulated disk error')
        return atomic(path, raw)
    monkeypatch.setattr(publisher, '_atomic', fail)
    with pytest.raises(OSError): publisher.publish(policy, config, job['candidate'])
    assert policy.dataset_tree_registry.read_bytes() == before
    monkeypatch.setattr(publisher, '_atomic', atomic)
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    assert manager.get_candidate(teacher, candidate_id)['access_history'][0]['status'] == 'succeeded'


def test_worker_rechecks_uid_and_does_not_change_readers_on_failure(published, monkeypatch):
    state, candidate_id = published
    _, policy, manager, teacher, student, config = state
    request(published, [student.username]); job = manager.worker_claim()
    before = policy.dataset_tree_registry.read_bytes()
    monkeypatch.setattr('pwd.getpwnam', lambda name: SimpleNamespace(pw_uid=12345678))
    with pytest.raises(KitError, match='身份已变化'):
        publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], error='账号身份已变化')
    assert policy.dataset_tree_registry.read_bytes() == before
    detail = manager.get_candidate(teacher, candidate_id)
    assert detail['status'] == 'published' and detail['access_history'][0]['status'] == 'failed'


def test_revocation_during_input_verification_is_rechecked_before_code(published, monkeypatch):
    state, _ = published
    _, policy, manager, _, student, config = state
    registry = TreeRegistry(policy.dataset_tree_registry)
    refs = registry.catalog(student)[0]['files']
    request(published, ['ly']); job = manager.worker_claim()
    verify = TreeRegistry._verify
    changed = []
    def revoke(fd, document, cancelled):
        verify(fd, document, cancelled)
        if not changed:
            changed.append(True)
            result = publisher.publish(policy, config, job['candidate'])
            manager.worker_finish(job['job_id'], job['lease_token'], result)
    monkeypatch.setattr(TreeRegistry, '_verify', staticmethod(revoke))
    with pytest.raises(KitError, match='无权'):
        with registry.open_references([student], refs):
            pytest.fail('must not begin execution after permission is revoked')


def test_registry_changed_after_queue_is_not_overwritten(published):
    import json
    state, candidate_id = published
    _, policy, manager, teacher, student, config = state
    request(published, ['ly']); job = manager.worker_claim()
    registry = json.loads(policy.dataset_tree_registry.read_text())
    registry['datasets'][0]['readers'] = [{'uid':student.uid, 'username':student.username}]
    policy.dataset_tree_registry.write_text(json.dumps(registry))
    before = policy.dataset_tree_registry.read_bytes()
    with pytest.raises(KitError, match='授权已变化'):
        publisher.publish(policy, config, job['candidate'])
    assert policy.dataset_tree_registry.read_bytes() == before


def test_publisher_rejects_injected_fields_and_receipt_paths(published):
    state, _ = published
    _, policy, manager, _, _, config = state
    request(published, ['ly']); job = manager.worker_claim(); task=job['candidate']
    before = policy.dataset_tree_registry.read_bytes()
    with pytest.raises(KitError, match='字段'):
        publisher.publish(policy, config, {**task, 'path':'/etc/passwd'})
    with pytest.raises(KitError, match='身份'):
        publisher.publish(policy, config, {**task, 'request_id':'../../untrusted'})
    assert policy.dataset_tree_registry.read_bytes() == before
