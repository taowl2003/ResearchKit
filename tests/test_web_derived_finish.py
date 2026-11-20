from __future__ import annotations

import hashlib
import threading
import time
from types import MappingProxyType

import pytest

from researchkit.files import KitError
from researchkit.web_derived import DerivedDatasets
from researchkit.web_broker import BrokerError, BrokerServer
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1011, 'ly', 'teacher')
STUDENT = Principal(1012, 'student', 'student')
OTHER = Principal(1013, 'other', 'student')


@pytest.fixture
def service(tmp_path):
    root = tmp_path / 'derived'
    root.mkdir(mode=0o700)
    return DerivedDatasets(SubmissionStore(tmp_path / 'store'), root, max_file_bytes=8 * 1024**2,
                           user_quota_bytes=16 * 1024**2)


def _begin_complete(service, principal, data: bytes, name: str):
    digest = hashlib.sha256(data).hexdigest()
    begun = service.begin_upload(principal, name, len(data), digest)
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + 1024 * 1024]
        service.append_upload(principal, begun['upload_id'], offset, chunk)
        offset += len(chunk)
    return begun, digest


def test_catalog_page_filters_visibility_and_keeps_legacy_catalog_list(service):
    first, first_hash = _begin_complete(service, STUDENT, b'a', 'alpha')
    one = service.commit_upload(STUDENT, first['upload_id'], first_hash)
    second, second_hash = _begin_complete(service, STUDENT, b'b', 'beta')
    two = service.commit_upload(STUDENT, second['upload_id'], second_hash)
    teacher, teacher_hash = _begin_complete(service, TEACHER, b'c', 'published')
    public = service.publish(TEACHER, service.commit_upload(TEACHER, teacher['upload_id'], teacher_hash)['dataset_id'])

    assert isinstance(service.catalog(STUDENT), list)
    mine = service.catalog_page(STUDENT, visibility='mine', limit=1)
    assert len(mine['items']) == 1 and mine['next_cursor']
    next_page = service.catalog_page(STUDENT, visibility='mine', limit=1, cursor=mine['next_cursor'])
    assert {item['dataset_id'] for item in mine['items'] + next_page['items']} == {one['dataset_id'], two['dataset_id']}
    assert service.catalog_page(STUDENT, visibility='public')['items'] == [public]
    assert service.catalog_page(STUDENT, q='beta')['items'] == [two]
    with pytest.raises(KitError):
        service.catalog_page(STUDENT, visibility='private')
    with pytest.raises(KitError):
        service.catalog_page(STUDENT, cursor='forged')


def test_hashing_holds_only_the_upload_session_lock(service, monkeypatch):
    data = b'x' * (1024 * 1024 + 1)
    begun, digest = _begin_complete(service, STUDENT, data, 'large')
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    original = hashlib.sha256

    class SlowHash:
        def __init__(self):
            self.value = original()

        def update(self, block):
            started.set()
            assert release.wait(3)
            self.value.update(block)

        def hexdigest(self):
            return self.value.hexdigest()

    monkeypatch.setattr('researchkit.web_derived.hashlib.sha256', SlowHash)
    result: list[object] = []

    def commit():
        try:
            result.append(service.commit_upload(STUDENT, begun['upload_id'], digest))
        finally:
            done.set()

    worker = threading.Thread(target=commit)
    worker.start()
    assert started.wait(1)
    # A distinct session acquires and records its reservation while the first
    # session is hashing outside SQLite's write transaction.
    other = service.begin_upload(STUDENT, 'other', 0)
    blocked_append = threading.Event()

    def append_same_session():
        try:
            service.append_upload(STUDENT, begun['upload_id'], len(data), b'')
        except KitError:
            pass
        finally:
            blocked_append.set()

    same = threading.Thread(target=append_same_session)
    same.start()
    time.sleep(0.05)
    assert not blocked_append.is_set()
    release.set()
    assert done.wait(3) and blocked_append.wait(3)
    worker.join(timeout=1); same.join(timeout=1)
    assert result and result[0]['dataset_id']
    assert service.abort_upload(STUDENT, other['upload_id']) == {'aborted': True}


def test_dataset_file_browse_uses_current_derived_acl_only(service, tmp_path):
    begun, digest = _begin_complete(service, STUDENT, b'private', 'private file')
    record = service.commit_upload(STUDENT, begun['upload_id'], digest)
    policy = Policy(principals=MappingProxyType({STUDENT.uid: STUDENT, OTHER.uid: OTHER}),
                    workspaces=MappingProxyType({}), broker_uid=STUDENT.uid, broker_username=STUDENT.username,
                    submission_store=service.store.directory, code_root=tmp_path,
                    derived_dataset_root=service.root, derived_max_file_bytes=8 * 1024**2,
                    derived_user_quota_bytes=16 * 1024**2)
    server = object.__new__(BrokerServer)
    server.policy, server.store = policy, service.store
    response = server._dispatch(STUDENT, {'method': 'get_dataset_files', 'params': {
        'dataset_id': record['dataset_id'], 'q': 'data', 'offset': 0, 'limit': 1}})
    assert response == {'items': [record['files'][0]], 'total': 1, 'next_offset': None}
    with pytest.raises(BrokerError, match='未获授权'):
        server._dispatch(OTHER, {'method': 'get_dataset_files', 'params': {
            'dataset_id': record['dataset_id'], 'q': '', 'offset': 0, 'limit': 1}})
    with pytest.raises(BrokerError, match='参数'):
        server._dispatch(STUDENT, {'method': 'get_dataset_files', 'params': {
            'dataset_id': record['dataset_id'], 'q': '', 'offset': -1, 'limit': 1}})
