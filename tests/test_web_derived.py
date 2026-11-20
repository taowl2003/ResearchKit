import hashlib
import os
import stat
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_derived import DerivedDatasets
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_task_delivery import TaskDelivery


TEACHER = Principal(1011, 'ly', 'teacher')
STUDENT = Principal(1012, 'student', 'student')


@pytest.fixture
def derived(tmp_path):
    store = SubmissionStore(tmp_path / 'submissions')
    root = tmp_path / 'derived'
    root.mkdir(mode=0o700)
    return store, DerivedDatasets(store, root, max_file_bytes=1024, user_quota_bytes=2048)


def _upload(service, principal, payload=b'hello', name='processed'):
    digest = hashlib.sha256(payload).hexdigest()
    begun = service.begin_upload(principal, name, len(payload), digest)
    assert service.append_upload(principal, begun['upload_id'], 0, payload) == {'received': len(payload)}
    return service.commit_upload(principal, begun['upload_id'], digest)


def test_stream_commit_is_immutable_and_private(derived):
    _store, service = derived
    record = _upload(service, STUDENT)
    reference = record['files'][0]
    assert service.catalog(STUDENT) == [record]
    assert service.catalog(TEACHER) == []
    with service.open_file(STUDENT, record['dataset_id'], 'data.bin') as (fd, returned):
        assert returned == reference
        assert os.read(fd, 16) == b'hello'
    with pytest.raises(KitError, match='无权'):
        with service.open_file(TEACHER, record['dataset_id'], 'data.bin'):
            pass
    # The public record intentionally does not reveal a broker path.
    assert 'blob_name' not in record
    assert reference['path'] == 'data.bin'


@pytest.mark.parametrize('source_kind', ['upload', 'run'])
def test_commit_assigns_shared_group_before_publication_under_private_umask(derived, monkeypatch, source_kind):
    store, service = derived
    payload = b'processed rainfall\n1,2,3\n'
    digest = hashlib.sha256(payload).hexdigest()
    upload = service.begin_upload(STUDENT, 'group regression', len(payload), digest)
    service.append_upload(STUDENT, upload['upload_id'], 0, payload)
    actual_lstat, actual_fchown = os.lstat, os.fchown
    actual_gid = service.root.stat().st_gid
    # The development account has one OS group. Simulate the production
    # broker:runner group difference at the root metadata/syscall boundary;
    # all upload, rename, mode, hashing and transaction work remains real.
    shared_gid = actual_gid + 10000
    def lstat(path, *args, **kwargs):
        if path == service.root:
            return SimpleNamespace(st_gid=shared_gid)
        return actual_lstat(path, *args, **kwargs)
    calls = []
    def fchown(fd, uid, gid):
        assert uid == -1 and gid == shared_gid
        assert os.fstat(fd).st_gid == actual_gid
        with store._connection() as con:
            assert con.execute('SELECT count(*) FROM web_derived_datasets').fetchone()[0] == 0
        calls.append(gid)
        actual_fchown(fd, uid, actual_gid)
    monkeypatch.setattr(os, 'lstat', lstat)
    monkeypatch.setattr(os, 'fchown', fchown)
    previous = os.umask(0o077)
    try:
        options = {} if source_kind == 'upload' else {'source_kind': 'run', 'source_run_id': 'run', 'source_submission_id': 'submission'}
        record = service.commit_upload(STUDENT, upload['upload_id'], digest, **options)
        current = os.umask(0o077)
        assert current == 0o077
    finally:
        os.umask(previous)
    assert calls == [shared_gid]
    blobs = list((service.root / 'blobs').glob('*.blob'))
    assert len(blobs) == 1
    assert stat.S_IMODE(blobs[0].stat().st_mode) == 0o640
    assert blobs[0].read_bytes() == payload
    assert record['files'][0]['sha256'] == digest and record['visibility'] == 'private'


def test_group_assignment_failure_rolls_back_and_upload_can_retry(derived, monkeypatch):
    store, service = derived
    digest = hashlib.sha256(b'abc').hexdigest()
    upload = service.begin_upload(STUDENT, 'retry', 3, digest)
    service.append_upload(STUDENT, upload['upload_id'], 0, b'abc')
    actual = os.fchown
    def denied(*args):
        raise PermissionError('not a member of runner group')
    monkeypatch.setattr(os, 'fchown', denied)
    with pytest.raises(KitError, match='服务组配置'):
        service.commit_upload(STUDENT, upload['upload_id'], digest)
    with store._connection() as con:
        assert con.execute('SELECT count(*) FROM web_derived_datasets').fetchone()[0] == 0
        row = con.execute('SELECT * FROM web_derived_uploads WHERE upload_id=?', (upload['upload_id'],)).fetchone()
    assert service._upload_path(row['temp_name']).read_bytes() == b'abc'
    assert not list((service.root / 'blobs').glob('*.blob'))
    monkeypatch.setattr(os, 'fchown', actual)
    assert service.commit_upload(STUDENT, upload['upload_id'], digest)['files'][0]['sha256'] == digest


def test_submission_bound_teacher_review_grant_and_publication(derived):
    store, service = derived
    record = _upload(service, STUDENT, b'abc')
    reference = record['files'][0]
    project = store.create_project(STUDENT, 'private derived')
    service.validate_submission_references(STUDENT, [reference])
    submission = store.submit(project['project_id'], STUDENT, 'major', {'case.py': b'pass'}, [reference])
    service.bind_submission(STUDENT, submission['submission_id'], [reference], TEACHER)
    mounts = service.claim_mounts(STUDENT, TEACHER, submission['submission_id'], [reference])
    assert mounts[0]['reference'] == reference
    with service.open_file(TEACHER, record['dataset_id'], 'data.bin') as (fd, _ref):
        assert os.read(fd, 3) == b'abc'
    published = service.publish(TEACHER, record['dataset_id'])
    assert published['visibility'] == 'public'


def test_upload_is_bound_to_identity_offset_and_hash(derived):
    _store, service = derived
    begun = service.begin_upload(STUDENT, 'bad', 3)
    with pytest.raises(KitError, match='无权'):
        service.append_upload(TEACHER, begun['upload_id'], 0, b'abc')
    with pytest.raises(KitError, match='偏移'):
        service.append_upload(STUDENT, begun['upload_id'], 1, b'a')
    service.append_upload(STUDENT, begun['upload_id'], 0, b'abc')
    with pytest.raises(KitError, match='SHA'):
        service.commit_upload(STUDENT, begun['upload_id'], hashlib.sha256(b'wrong').hexdigest())


def test_teacher_review_data_runs_only_in_exact_trial_chain_or_when_public(derived):
    store,service=derived
    record=_upload(service,STUDENT,b'abc');refs=record['files']
    source=store.create_project(STUDENT,'source')
    frozen=store.submit(source['project_id'],STUDENT,'minor',{'case.py':b'pass'},refs)
    service.bind_submission(STUDENT,frozen['submission_id'],refs,TEACHER)
    # ly can inspect the reviewed evidence, but this must not turn into a
    # general-purpose source right for arbitrary ly projects.
    service.validate_submission_references(TEACHER,refs)
    with service.open_file(TEACHER,record['dataset_id'],'data.bin') as (fd,_ref):
        assert os.read(fd,3)==b'abc'
    teacher_project=store.create_project(TEACHER,'maintenance copy')
    teacher_version=store.submit(teacher_project['project_id'],TEACHER,'minor',{'case.py':b'pass'},refs)
    with pytest.raises(KitError,match='不具备派生数据读取授权'):
        service.claim_mounts(TEACHER,TEACHER,teacher_version['submission_id'],refs)

    # A teacher may instead send the exact reviewed source as a temporary
    # trial to ly.  Claiming creates the protected baseline chain used here.
    delivery=TaskDelivery(store)
    trial=delivery.create_trial(TEACHER,{'request_id':'d3c2f168-c156-4ea0-8e40-3f1a693a9be9',
                                         'name':'review trial','instructions':''},
                                source_submission_id=frozen['submission_id'],targets=[TEACHER])
    claimed=store.claim_distribution(trial['distributions'][0]['distribution_id'],TEACHER)
    trial_version=store.submit(claimed['project']['project_id'],TEACHER,'minor',{'case.py':b'pass'},refs)
    assert service.claim_mounts(TEACHER,TEACHER,trial_version['submission_id'],refs)[0]['reference']==refs[0]

    other=Principal(1013,'other','student')
    with pytest.raises(KitError,match='无权'):
        service.validate_submission_references(other,refs)
    service.publish(TEACHER,record['dataset_id'])
    service.validate_submission_references(other,refs)
    public_project=store.create_project(other,'public reuse')
    public_version=store.submit(public_project['project_id'],other,'minor',{'case.py':b'pass'},refs)
    service.bind_submission(other,public_version['submission_id'],refs,TEACHER)
    assert service.claim_mounts(other,other,public_version['submission_id'],refs)[0]['reference']==refs[0]


@pytest.mark.parametrize('resources', [False, True])
def test_active_task_grant_makes_teacher_owned_private_data_visible_to_recipient(derived, resources):
    store, service = derived
    record = _upload(service, TEACHER, b'abc')
    references = record['files']
    source = store.create_project(TEACHER, 'teacher source')
    baseline = store.submit(source['project_id'], TEACHER, 'minor', {'case.py': b'pass'}, references)
    recipient = Principal(1013, 'other', 'student')
    delivery = TaskDelivery(store)
    payload = {'request_id':'6534ebb3-13c0-491f-a979-7298e3379fc2', 'name':'private data task','instructions':''}
    task = (delivery.create_resource_task(TEACHER, payload, targets=[recipient], dataset_references=references)
            if resources else delivery.create_task(TEACHER, payload,
                baseline_submission_id=baseline['submission_id'], targets=[recipient]))
    assert [item['dataset_id'] for item in service.catalog(recipient)] == [record['dataset_id']]
    assert [item['dataset_id'] for item in service.catalog_page(recipient)['items']] == [record['dataset_id']]
    with service.open_file(recipient, record['dataset_id'], 'data.bin') as (descriptor, reference):
        assert os.read(descriptor, 3) == b'abc' and reference == references[0]
    grant = next(item for item in delivery.list_active_grants(recipient) if item['kind'] == 'dataset')
    delivery.revoke_grant(TEACHER, grant['grant_id'])
    assert service.catalog(recipient) == []


def test_saved_content_drift_is_rejected_without_changing_read_offset(derived):
    _store,service=derived
    record=_upload(service,STUDENT,b'abc')
    with service.open_file(STUDENT,record['dataset_id'],'data.bin') as (fd,ref):
        assert os.read(fd,1)==b'a'
        service._verify_content(fd,ref)
        assert os.read(fd,2)==b'bc'
    blob=next((service.root/'blobs').glob('*.blob'))
    blob.write_bytes(b'xyz')
    with pytest.raises(KitError,match='变化或损坏'):
        with service.open_file(STUDENT,record['dataset_id'],'data.bin'):pass


def test_public_dataset_lifecycle_hides_new_selection_but_retains_authorized_read(derived):
    from researchkit.web_lifecycle import AssetLifecycle
    store, service = derived
    record = _upload(service, TEACHER, b'abc')
    ident = record['dataset_id']
    service.publish(TEACHER, ident)
    lifecycle = AssetLifecycle(store)
    report = lifecycle.report(TEACHER, 'dataset', ident)
    assert report['can_trash']
    trashed = lifecycle.change(TEACHER, 'dataset', ident, 'trash', report['revision'])
    assert service.catalog_page(STUDENT)['items'] == []
    page = service.catalog_page(TEACHER)
    assert page['items'][0]['lifecycle']['state'] == 'trashed'
    # The pinned reference and original bytes remain readable under the same
    # grants; retirement is not an authorization revocation.
    with service.open_file(STUDENT, ident, record['files'][0]['path']) as (fd, ref):
        assert os.read(fd, 3) == b'abc'
    assert ref == record['files'][0]
    lifecycle.change(TEACHER, 'dataset', ident, 'restore', trashed['revision'])
    assert service.catalog_page(STUDENT)['items'][0]['dataset_id'] == ident
