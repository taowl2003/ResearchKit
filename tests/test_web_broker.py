"""Broker protocol tests use one real current-UID AF_UNIX socket.

They do not claim to establish a multi-account OS deployment; cross-account
authorization is injected at the broker boundary while peer credential parsing
is covered by the identity tests.
"""
from __future__ import annotations

import json
import os
import pwd
import socket
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from researchkit.files import KitError
from researchkit import web_broker
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore


def _validation_files() -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: validation-source
plugins:
  scale:
    path: plugins/scale
    parameters:
      factor: 2
parameters: {}
data: {}
requires: {}
''',
        'case.py': b'def run(context):\n    return {}\n',
        'plugins/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: scale
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs:
  result: scalar@1.0.0
parameters:
  factor:
    type: number
    required: true
''',
        'plugins/scale/plugin.py': b"def run(inputs, parameters, context):\n    return {'result': inputs['value'] * parameters['factor']}\n",
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
    }


def _validation_candidate(server, teacher: Principal, *, kind: str = 'algorithm', references=None):
    from researchkit.web_assets import AssetService
    project = server.store.create_project(teacher, 'validation source')
    source = server.store.submit(project['project_id'], teacher, 'major', _validation_files(), references or [])
    return AssetService(server.store, server.policy.code_root / 'library').create(
        teacher, source['submission_id'], kind, 'broker.validation', '1.0.0', 'scale' if kind == 'algorithm' else None)


def _policy(tmp_path: Path) -> tuple[Policy, Principal]:
    uid = os.geteuid()
    username = pwd.getpwuid(uid).pw_name
    role = 'teacher' if username == 'ly' else 'student'
    principal = Principal(uid=uid, username=username, role=role)
    private = tmp_path / 'broker-store'
    private.mkdir(mode=0o700)
    policy = Policy(
        principals=MappingProxyType({uid: principal}), workspaces=MappingProxyType({}),
        broker_uid=uid, broker_username=username, submission_store=private,
        code_root=Path('/'), broker_socket=tmp_path / 'broker.sock',
    )
    return policy, principal


def _seed_submission(server, principal: Principal, project_id: str, kind: str,
                     files: dict[str, bytes], references: list[dict[str, object]] | None = None):
    """Create historical immutable records without exercising the public submit RPC.

    The public broker route deliberately rejects direct submissions now: normal
    user content must first go through the version preview, isolated check and
    isolated run flow.  Protocol tests that need an existing old submission
    seed the protected store directly, just as a migrated deployment already
    contains formal historical records.
    """
    return server.store.submit(project_id, principal, kind, files, references or [])


@pytest.fixture
def broker(tmp_path: Path, monkeypatch):
    # Store route checks are separately tested.  This keeps broker protocol
    # tests portable in sandboxed runners that map root-owned /tmp as nobody.
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    monkeypatch.setattr(web_broker, '_trusted_socket_route', lambda _path, _uid, require_socket: None)
    policy, principal = _policy(tmp_path)
    server = web_broker.BrokerServer(policy, workers=8)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
    thread.start()
    client = web_broker.BrokerClient(server.socket_path, policy.broker_uid)
    try:
        yield server, client, principal
    finally:
        server.close()
        thread.join(timeout=5)


def _raw_request(path: Path, request: bytes) -> dict:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(path))
        connection.sendall(struct.pack('!I', len(request)) + request)
        size = struct.unpack('!I', web_broker._recv_exact(connection, 4))[0]
        return json.loads(web_broker._recv_exact(connection, size).decode('utf-8'))
    finally:
        connection.close()


def test_direct_submit_is_rejected_and_historical_snapshot_bytes_round_trip(broker):
    server, client, principal = broker
    project = client.request('create_project', name='bad code is allowed')
    with pytest.raises(KitError, match='预览版本'):
        client.request('submit', project_id=project['project_id'], kind='minor',
                       files={'case.py': b'def broken(:\n', 'note.txt': b'\x00\xff'}, data_references=[])
    assert client.request('list_submissions', project_id=project['project_id']) == []
    frozen = _seed_submission(server, principal, project['project_id'], 'minor',
                              {'case.py': b'def broken(:\n', 'note.txt': b'\x00\xff'})
    assert isinstance(frozen, dict)
    read = client.request('get_submission', submission_id=frozen['submission_id'])
    assert isinstance(read, dict)
    assert read['files'] == {'case.py': b'def broken(:\n', 'note.txt': b'\x00\xff'}
    assert read['data_status'] == 'unverified'
    assert client.request('list_submissions', project_id=project['project_id'])[0]['submission_id'] == frozen['submission_id']
    assert client.request('list_submissions', project_id=None)[0]['submission_id'] == frozen['submission_id']
    

def test_submission_browser_cursor_survives_separate_socket_requests(broker):
    server, client, principal = broker
    project_ids = set()
    for name in ('browser first', 'browser second'):
        project = client.request('create_project', name=name)
        project_ids.add(project['project_id'])
        _seed_submission(server, principal, project['project_id'], 'major', {'case.py': b'pass'})
    first = client.request('list_submission_projects', limit=1)
    assert first['next_cursor']
    second = client.request('list_submission_projects', limit=1, cursor=first['next_cursor'])
    assert {page['items'][0]['project']['project_id'] for page in (first, second)} == project_ids
    assert second['next_cursor'] is None
    project = first['items'][0]['project']
    assert project['owner_username'] == principal.username
    history = client.request('list_project_versions', project_id=project['project_id'])
    assert history['items'][0]['submission_id'] == first['items'][0]['preview']['submission_id']
    assert 'manifest' not in history['items'][0]
    with pytest.raises(KitError):
        client.request('list_submission_projects', owner_username='forged')


def test_identity_payload_and_unknown_or_duplicate_json_are_rejected(broker):
    server, client, _principal = broker
    with pytest.raises(KitError, match='身份'):
        client.request('create_project', uid=9999)
    impersonation = _raw_request(server.socket_path, b'{"method":"create_project","params":{"uid":9999}}')
    assert impersonation['ok'] is False and '身份' in impersonation['error']
    duplicate = _raw_request(server.socket_path, b'{"method":"create_project","method":"submit","params":{}}')
    assert duplicate['ok'] is False and '重复' in duplicate['error']
    unknown = _raw_request(server.socket_path, b'{"method":"run","params":{}}')
    assert unknown['ok'] is False and '不支持' in unknown['error']


def test_frame_limit_rejects_header_before_body_allocation(broker):
    server, _client, _principal = broker
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(server.socket_path))
        connection.sendall(struct.pack('!I', web_broker.MAX_FRAME + 1))
        size = struct.unpack('!I', web_broker._recv_exact(connection, 4))[0]
        response = json.loads(web_broker._recv_exact(connection, size).decode('utf-8'))
    finally:
        connection.close()
    assert response == {'ok': False, 'error': '请求帧超过 96 MiB 上限'}


def test_frame_has_one_total_deadline_and_client_rejects_oversize_before_connect(broker, monkeypatch):
    _server, client, _principal = broker
    monkeypatch.setattr(web_broker, 'SOCKET_TIMEOUT', 0.05)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    def drip() -> None:
        try:
            right.sendall(struct.pack('!I', 3) + b'{')
            time.sleep(0.035)
            right.sendall(b'}')
            time.sleep(0.035)
            right.sendall(b' ')
        except OSError:
            pass
    sender = threading.Thread(target=drip)
    sender.start()
    try:
        with pytest.raises(KitError, match='超时'):
            web_broker._recv_frame(left)
    finally:
        left.close()
        right.close()
        sender.join(timeout=1)
    monkeypatch.setattr(web_broker, 'MAX_FRAME', 64)
    with pytest.raises(KitError, match='超过'):
        client.request('create_project', name='x' * 100)


def test_cross_project_permission_is_enforced_at_broker_dispatch(broker, monkeypatch):
    server, _client, principal = broker
    actor = Principal(uid=principal.uid + 200_000, username='caller-student', role='student')
    other = Principal(uid=principal.uid + 100_000, username='other-student', role='student')
    project = server.store.create_project(other, 'other')
    frozen = server.store.submit(project['project_id'], other, 'minor', {'x.py': b'x'}, [])
    monkeypatch.setattr(web_broker, 'peer_principal', lambda _connection, _policy: actor)
    response = _raw_request(server.socket_path, json.dumps({
        'method': 'get_submission', 'params': {'submission_id': frozen['submission_id']},
    }).encode())
    assert response['ok'] is False and '无权' in response['error']


def test_simultaneous_direct_submits_are_all_rejected_without_snapshots(broker):
    _server, client, _principal = broker
    project = client.request('create_project', name='parallel')
    assert isinstance(project, dict)
    errors, results = [], []
    def submit(index: int) -> None:
        try:
            results.append(client.request('submit', project_id=project['project_id'], kind='minor',
                                          files={f'{index}.py': b'broken'}, data_references=[]))
        except Exception as exc:  # pragma: no cover - failure is asserted below
            errors.append(exc)
    threads = [threading.Thread(target=submit, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not results
    assert len(errors) == 6
    assert all('预览版本' in str(error) for error in errors)
    assert client.request('list_submissions', project_id=project['project_id']) == []


def test_client_rejects_unexpected_peer_uid_and_socket_replacement(broker, tmp_path: Path):
    _server, client, _principal = broker
    with pytest.raises(KitError, match='expected_broker_uid'):
        web_broker.BrokerClient(client.socket_path, 0)
    replacement = tmp_path / 'not-a-socket'
    replacement.write_text('x')
    with pytest.raises(KitError):
        web_broker.BrokerClient(replacement, client.expected_broker_uid).request('list_submissions')


def test_distribution_routes_round_trip_binary_snapshots_over_socket(broker):
    server, client, principal = broker
    if principal.role != 'teacher':
        pytest.skip('self-distribution needs the configured teacher account')
    project = client.request('create_project', name='socket baseline')
    frozen = _seed_submission(server, principal, project['project_id'], 'major',
                              {'case.py': b'fixed', 'blob.bin': b'\x00\xff'})
    sent = client.request('create_distribution', submission_id=frozen['submission_id'],
                          target_username=principal.username)
    assert client.request('list_distributions') == [sent]
    claim = client.request('claim_distribution', distribution_id=sent['distribution_id'])
    assert claim['files'] == {'blob.bin': b'\x00\xff', 'case.py': b'fixed'}
    assert client.request('confirm_distribution', distribution_id=sent['distribution_id'])['status'] == 'claimed'
    baseline = client.request('get_distribution_baseline', distribution_id=sent['distribution_id'])
    assert baseline['submission_id'] == frozen['submission_id']
    assert baseline['files'] == claim['files']


def test_distribution_route_schema_and_target_policy_are_enforced(broker, monkeypatch):
    server, client, principal = broker
    if principal.role != 'teacher':
        pytest.skip('distribution creation needs the configured teacher account')
    project = client.request('create_project', name='schema baseline')
    frozen = _seed_submission(server, principal, project['project_id'], 'minor', {'case.py': b'fixed'})
    malformed = [
        ('create_distribution', {'submission_id': frozen['submission_id']}),
        ('list_distributions', {'unexpected': True}),
        ('claim_distribution', {'distribution_id': 1}),
        ('confirm_distribution', {'distribution_id': frozen['submission_id'], 'uid': principal.uid}),
        ('get_distribution_baseline', {}),
    ]
    for method, params in malformed:
        response = _raw_request(server.socket_path, json.dumps({'method': method, 'params': params}).encode())
        assert response['ok'] is False

    class OutsideAccount:
        pw_uid = principal.uid + 10_000
        pw_name = 'outside-policy'

    monkeypatch.setattr(web_broker.pwd, 'getpwnam', lambda _name: OutsideAccount())
    denied = _raw_request(server.socket_path, json.dumps({
        'method': 'create_distribution',
        'params': {'submission_id': frozen['submission_id'], 'target_username': 'outside-policy'},
    }).encode())
    assert denied['ok'] is False
    assert '不是允许的 Web 用户' in denied['error']



def test_injected_other_student_peer_cannot_claim_or_read_another_task(broker, monkeypatch):
    server, _client, principal = broker
    if principal.role != 'teacher':
        pytest.skip('distribution source needs the configured teacher account')
    recipient = Principal(uid=principal.uid + 100_001, username='assigned-student', role='student')
    attacker = Principal(uid=principal.uid + 100_002, username='other-student', role='student')
    source_project = server.store.create_project(principal, 'private assignment source')
    frozen = server.store.submit(source_project['project_id'], principal, 'minor', {'case.py': b'fixed'}, [])
    distribution = server.store.create_distribution(principal, frozen['submission_id'], recipient)
    monkeypatch.setattr(web_broker, 'peer_principal', lambda _connection, _policy: attacker)
    for method in ('claim_distribution', 'get_distribution_baseline'):
        response = _raw_request(server.socket_path, json.dumps({
            'method': method, 'params': {'distribution_id': distribution['distribution_id']},
        }).encode())
        assert response['ok'] is False
        assert '无权' in response['error']


def test_dataset_authorization_still_requires_both_owner_and_requester_grants_while_jobs_are_retired(broker, monkeypatch):
    from dataclasses import replace
    import hashlib
    from researchkit.web_datasets import DatasetRegistry
    server, _client, _principal = broker
    teacher = Principal(1011, 'ly', 'teacher')
    student = Principal(1012, 'student', 'student')
    reference = {'dataset_id': 'numbers-v1', 'path': 'input.txt', 'size': 1,
                 'sha256': hashlib.sha256(b'1').hexdigest()}
    record = {'dataset_id': reference['dataset_id'], 'root': '/srv/datasets/numbers-v1',
              'readers': [], 'files': [{key: reference[key] for key in ('path', 'size', 'sha256')}]}
    monkeypatch.setattr(DatasetRegistry, '_read_bytes', lambda self: json.dumps({
        'schema': 'researchkit.web-datasets/v1', 'datasets': [record]}).encode())
    server.policy = replace(server.policy, dataset_registry=Path('/unused/registry.json'))
    project = server.store.create_project(student, 'student fixed source')
    submission = server.store.submit(project['project_id'], student, 'major', {'case.py': b'fixed'}, [reference])
    access = server._task_access()
    for reader in (teacher, student):
        record['readers'] = [{'uid': reader.uid, 'username': reader.username}]
        with pytest.raises(KitError, match='无权'):
            access.authorize_job(student, teacher, submission['submission_id'], [reference])
    record['readers'] = [{'uid': item.uid, 'username': item.username} for item in (teacher, student)]
    assert access.authorize_job(student, teacher, submission['submission_id'], [reference])['dataset_authorizations']
    request = {'method': 'request_job', 'params': {'submission_id': submission['submission_id'], 'operation': 'run'}}
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(student, request)


def test_asset_validation_broker_methods_are_retired_before_schema_or_role_work(broker):
    server, _client, _principal = broker
    teacher = Principal(1011, 'ly', 'teacher')
    student = Principal(1012, 'student', 'student')
    candidate = _validation_candidate(server, teacher)
    request = {'method': 'create_asset_candidate_validation', 'params': {
        'candidate_id': candidate['candidate_id'], 'expected_sha256': candidate['candidate_sha256'],
        'operation': 'run', 'cases': [{'inputs': {'value': 3}, 'parameters': {'factor': 2},
                                       'expected_outputs': {'result': 6}}],
    }}
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(teacher, request)
    with server.store._connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_asset_validation_snapshots'").fetchone() is None
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_asset_candidate_validations'").fetchone() is None
    for params in ({'candidate_id': candidate['candidate_id']},
                   {**request['params'], 'extra': True},
                   {**request['params'], 'operation': 'check'}):
        with pytest.raises(KitError, match='网页检查、运行.*移除'):
            server._dispatch(teacher, {'method': 'create_asset_candidate_validation', 'params': params})
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(student, {'method': 'list_asset_candidate_validations',
                                   'params': {'candidate_id': candidate['candidate_id']}})


def test_asset_validation_retirement_precedes_dataset_acl_and_never_writes_snapshot(broker, monkeypatch):
    import hashlib
    from researchkit.web_datasets import DatasetRegistry

    server, _client, _principal = broker
    teacher = Principal(1011, 'ly', 'teacher')
    reference = {'dataset_id': 'numbers-v1', 'path': 'input.txt', 'size': 1,
                 'sha256': hashlib.sha256(b'1').hexdigest()}
    candidate = _validation_candidate(server, teacher, kind='template', references=[reference])
    dataset = {'dataset_id': reference['dataset_id'], 'root': '/srv/datasets/numbers-v1', 'readers': [],
               'files': [{key: reference[key] for key in ('path', 'size', 'sha256')}]}
    monkeypatch.setattr(DatasetRegistry, '_read_bytes', lambda self: json.dumps({
        'schema': 'researchkit.web-datasets/v1', 'datasets': [dataset]}).encode())
    server.policy = replace(server.policy, dataset_registry=Path('/unused/registry.json'))
    request = {'method': 'create_asset_candidate_validation', 'params': {
        'candidate_id': candidate['candidate_id'], 'expected_sha256': candidate['candidate_sha256'], 'operation': 'run'}}
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(teacher, request)
    with server.store._connection() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_asset_validation_snapshots'").fetchone() is None
    dataset['readers'] = [{'uid': teacher.uid, 'username': teacher.username}]
    with pytest.raises(KitError, match='网页检查、运行.*移除'):
        server._dispatch(teacher, request)


def test_direct_assignment_batch_checks_all_data_grants_before_dispatch(broker, monkeypatch):
    import hashlib
    import uuid
    server,client,teacher=broker
    if teacher.role!='teacher':pytest.skip('teacher account fixture')
    student=Principal(teacher.uid+2000,'task-reader','student')
    names={teacher.username:teacher,student.username:student}
    monkeypatch.setattr(server,'_target_principal',lambda name:names[name])
    ref={'dataset_id':'rain-v1','path':'rain.csv','size':1,'sha256':hashlib.sha256(b'1').hexdigest()}
    monkeypatch.setattr(server,'_dataset_catalog',lambda principal,**kwargs:[{'dataset_id':'rain-v1','files':[ref]}] if principal==teacher else [])
    monkeypatch.setattr(server,'_authorized_source_references',lambda _principal,_refs: _refs)
    from researchkit.web_task_access import TaskAccess
    monkeypatch.setattr(TaskAccess,'validate_transfer',lambda _self,_sender,_targets,refs: refs)
    checked=client.request('validate_assignment_targets',target_usernames=[teacher.username,student.username],data_references=[ref])
    assert len(checked['targets'])==2
    assert not server.store.list_distributions(teacher)
    project=client.request('create_project',name='task batch')
    request_id=str(uuid.uuid4())
    submitted=client.request('submit_assignment_baseline',project_id=project['project_id'],kind='major',
                            files={'case.py':b'def run(context): return {}'},data_references=[],request_id=request_id,fingerprint='a'*64)
    repeated=client.request('submit_assignment_baseline',project_id=project['project_id'],kind='major',
                           files={'case.py':b'def run(context): return {}'},data_references=[],request_id=request_id,fingerprint='a'*64)
    assert submitted['submission_id']==repeated['submission_id']
    result=client.request('create_assignment_distributions',submission_id=submitted['submission_id'],
                          target_usernames=[teacher.username,student.username],request_id=request_id)
    assert len(result['distributions'])==2
    assert client.request('get_storage_capacity')['file_references']>=1
    with pytest.raises(KitError,match='教师'):
        server._dispatch(student,{'method':'create_assignment_distributions','params':{
            'submission_id':submitted['submission_id'],'target_usernames':[teacher.username]}})
