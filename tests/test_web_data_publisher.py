"""End-to-end synthetic teacher review, root publication and retry receipts."""
from dataclasses import replace
import json
import os
from pathlib import Path
import pwd
import shutil
import tempfile
from types import MappingProxyType

import pytest
import yaml

from researchkit.files import KitError
from researchkit import web_datasets, web_data_publisher as publisher
from researchkit.web_data_management import DataManagement
from researchkit.web_dataset_discovery import discover, load_management_config
from researchkit.web_dataset_trees import TreeRegistry
from researchkit.web_dataset_metadata import SCHEMA
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_broker import BrokerServer, BrokerError


@pytest.fixture
def state(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-publisher-test-', dir=Path.cwd()))
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: True)
    try:
        raw = root / 'raw'; raw.mkdir()
        unit = raw / 'sonar'; unit.mkdir(); (unit / 'input.bin').write_bytes(b'not executed')
        wiki = root / 'wiki'; (wiki / 'catalog').mkdir(parents=True); (wiki / 'sources').mkdir()
        (wiki / 'sources/dataset-boundaries.yaml').write_text(yaml.safe_dump({'source_root': str(raw), 'adjudicated_by': 'user',
            'roots': [{'path': str(unit), 'card': 'dataset-sonar', 'model': 'single-dataset'}]}))
        (wiki / 'catalog/datasets.yaml').write_text(yaml.safe_dump({'records': [{
            'id': 'dataset-sonar', 'name': 'sonar', 'description': 'Original sonar description', 'fact_state': 'verified'}]}))
        (wiki / 'catalog/dataset-subsets.yaml').write_text('records: []\n')
        (wiki / 'reports').mkdir()
        (wiki / 'reports/ingestion-wave-5-findings.json').write_text('{"units": []}\n')
        config_path = root / 'data-management.json'
        config_path.write_text(json.dumps({'schema': 'researchkit.web-data-management/v1', 'raw_root': str(raw),
                                          'wiki_root': str(wiki), 'scan_interval_seconds': 60, 'seconds_per_dataset': 60}))
        registry = root / 'dataset-trees.json'; registry.write_text(json.dumps({'schema': 'researchkit.web-dataset-trees/v1', 'datasets': []}))
        metadata = root / 'metadata.json'; metadata.write_text(json.dumps({'schema': SCHEMA, 'datasets': {}}))
        teacher = Principal(os.getuid(), 'ly', 'teacher'); student = Principal(100123, 'student', 'student')
        policy = Policy(MappingProxyType({teacher.uid: teacher, student.uid: student}), MappingProxyType({}),
                        os.getuid(), pwd.getpwuid(os.getuid()).pw_name, root / 'store', root,
                        dataset_tree_registry=registry, dataset_metadata=metadata, dataset_management_config=config_path)
        store = SubmissionStore(root / 'store')
        manager = DataManagement(store, policy)
        manager.worker_start('publisher-one')
        yield root, policy, manager, teacher, student, load_management_config(config_path)
    finally:
        shutil.rmtree(root)


def scan(state):
    root, policy, manager, teacher, _, config = state
    # Advance the synthetic scan throttle without sleeping or touching user data.
    with manager._connection() as connection:
        connection.execute("UPDATE data_management_jobs SET created_at='2000-01-01T00:00:00Z' WHERE kind='scan'")
    manager.request_scan(teacher)
    job = manager.worker_claim()
    assert job['kind'] == 'scan'
    manager.worker_finish(job['job_id'], job['lease_token'], {'items': discover(config, policy)})
    return manager.list_candidates(teacher, status='all')['items'][0]


def approve(state):
    row = scan(state)
    _, _, manager, teacher, _, _ = state
    manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                   metadata={'name': '声呐练习', 'description': '声呐数据中文说明'}, readers='platform-users')
    job = manager.worker_claim()
    assert job['kind'] == 'publish'
    return job


def test_teacher_approval_is_not_activation_and_root_receipt_retry_keeps_reference(state):
    root, policy, manager, teacher, student, config = state
    job = approve(state)
    assert TreeRegistry(policy.dataset_tree_registry).catalog(student) == []
    result = publisher.publish(policy, config, job['candidate'])
    pinned = TreeRegistry(policy.dataset_tree_registry).catalog(student)
    assert len(pinned) == 1
    assert json.loads(policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['description'] == '声呐数据中文说明'
    again = publisher.publish(policy, config, job['candidate'])
    assert again['reused'] is True
    assert TreeRegistry(policy.dataset_tree_registry).catalog(student) == pinned
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    row = scan(state)
    assert row['status'] == 'published'  # English source vs Chinese output must not create another review.
    assert row['dataset_id'] == result['dataset_id']


def test_changed_wiki_needs_only_metadata_review_and_keeps_manifest_and_acl(state):
    root, policy, manager, teacher, student, config = state
    job = approve(state)
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    registry_before = policy.dataset_tree_registry.read_bytes()
    catalog_before = TreeRegistry(policy.dataset_tree_registry).catalog(student)
    source = root / 'wiki/catalog/datasets.yaml'
    value = yaml.safe_load(source.read_text()); value['records'][0]['description'] = 'New source description'
    source.write_text(yaml.safe_dump(value))
    row = scan(state)
    assert row['kind'] == 'metadata-change' and row['status'] == 'pending'
    detail = manager.get_candidate(teacher, row['candidate_id'])
    assert detail['previous_original_metadata']['description'] == 'Original sonar description'
    assert detail['original_metadata']['description'] == 'New source description'
    manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                   metadata={'description': '更新后的中文说明'})
    task = manager.worker_claim(); assert task['kind'] == 'publish'
    update = publisher.publish(policy, config, task['candidate'])
    manager.worker_finish(task['job_id'], task['lease_token'], update)
    assert update['dataset_id'] == result['dataset_id']
    assert policy.dataset_tree_registry.read_bytes() == registry_before
    assert TreeRegistry(policy.dataset_tree_registry).catalog(student) == catalog_before
    assert scan(state)['status'] == 'published'


@pytest.mark.parametrize('change', ['wiki', 'data'])
def test_publication_rejects_source_changes_after_approval(state, change):
    root, policy, manager, teacher, student, config = state
    job = approve(state)
    if change == 'wiki':
        source = root / 'wiki/catalog/datasets.yaml'
        doc = yaml.safe_load(source.read_text()); doc['records'][0]['description'] = 'Changed after review'
        source.write_text(yaml.safe_dump(doc))
    else:
        (root / 'raw/sonar/input.bin').write_bytes(b'changed contents')
    with pytest.raises(KitError, match='变化'):
        publisher.publish(policy, config, job['candidate'])
    assert TreeRegistry(policy.dataset_tree_registry).catalog(student) == []
    assert json.loads(policy.dataset_metadata.read_text())['datasets'] == {}


def test_partial_commit_never_exposes_dataset_and_retry_completes_same_id(state, monkeypatch):
    root, policy, manager, teacher, student, config = state
    job = approve(state)
    original = publisher._backup_and_update
    def fail_registry(path, expected, raw):
        if path == policy.dataset_tree_registry:
            raise OSError('simulated interruption before registry commit')
        return original(path, expected, raw)
    monkeypatch.setattr(publisher, '_backup_and_update', fail_registry)
    with pytest.raises(OSError):
        publisher.publish(policy, config, job['candidate'])
    assert TreeRegistry(policy.dataset_tree_registry).catalog(student) == []
    monkeypatch.setattr(publisher, '_backup_and_update', original)
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    assert len(TreeRegistry(policy.dataset_tree_registry).catalog(student)) == 1
    assert result['dataset_id'] == job['candidate']['dataset_id']


def test_changed_unpublished_source_after_partial_commit_uses_fresh_version(state, monkeypatch):
    root, policy, manager, teacher, student, config = state
    job = approve(state)
    original = publisher._backup_and_update
    def fail_registry(path, expected, raw):
        if path == policy.dataset_tree_registry:
            raise OSError('interrupted before registry commit')
        return original(path, expected, raw)
    monkeypatch.setattr(publisher, '_backup_and_update', fail_registry)
    with pytest.raises(OSError):
        publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], error='interrupted')
    monkeypatch.setattr(publisher, '_backup_and_update', original)
    (root / 'raw/sonar/input.bin').write_bytes(b'new unpublished contents')
    row = scan(state)
    assert row['dataset_id'] != job['candidate']['dataset_id'] and row['kind'] == 'new'
    manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                   metadata={'description': '重新审核的未发布数据'}, readers='platform-users')
    retry = manager.worker_claim()
    result = publisher.publish(policy, config, retry['candidate'])
    manager.worker_finish(retry['job_id'], retry['lease_token'], result)
    ids = [item['dataset_id'] for item in TreeRegistry(policy.dataset_tree_registry).catalog(student)]
    assert ids == [row['dataset_id']]


def test_wiki_reader_rejects_links_to_other_routes(state):
    from researchkit.web_dataset_discovery import _wiki_bytes
    root = state[0]
    outside = root / 'outside'; outside.mkdir(); (outside / 'private.yaml').write_text('secret')
    linked = root / 'wiki' / 'linked'; linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(KitError, match='链接'):
        _wiki_bytes(linked / 'private.yaml', label='Wiki说明')


def test_broker_separates_teacher_student_runner_and_root_protocol(state):
    _, policy, manager, teacher, student, _ = state
    server = BrokerServer(policy)
    request = lambda method, params={}: {'method': method, 'params': params}
    assert server._dispatch(teacher, request('data_management_status'))['configured'] is True
    with pytest.raises(BrokerError, match='教师'):
        server._dispatch(student, request('data_management_status'))
    with pytest.raises(BrokerError):
        server._dispatch(teacher, request('publisher_claim'))
    root = Principal(0, 'root', 'data-publisher')
    import hashlib
    manager.request_scan(teacher)
    job = manager.worker_claim()
    original = '# 完整说明\n'
    params = {'job_id': job['job_id'], 'lease_token': job['lease_token'], 'candidate_id': 'a'*24,
              'provenance_ref': 'README:README.md#sha256=' + hashlib.sha256(original.encode()).hexdigest(),
              'original': original}
    assert server._dispatch(root, request('publisher_readme', params)) == {'stored': True}
    for user in (teacher, student):
        with pytest.raises(BrokerError):
            server._dispatch(user, request('publisher_readme', params))
    with pytest.raises(BrokerError, match='发布账号'):
        server._dispatch(root, request('get_submission', {'submission_id': 'any'}))
    with pytest.raises(BrokerError, match='身份'):
        server._dispatch(teacher, request('data_management_scan', {'path': '/etc'}))
    old_policy = replace(policy, dataset_management_config=None)
    server.policy = old_policy
    with pytest.raises(BrokerError, match='未配置'):
        server._dispatch(root, request('publisher_claim'))


def test_root_publisher_does_not_run_as_ordinary_user(state):
    # The actual service entrypoint rejects our real ly UID before doing I/O.
    if os.geteuid() == 0:
        pytest.skip('requires ordinary account')
    with pytest.raises(KitError, match='独立服务'):
        publisher.serve(state[0] / 'irrelevant-policy.json', check=True)


def test_selected_accounts_use_verified_uid_and_metadata_update_keeps_reader_acl(state, monkeypatch):
    root, policy, manager, teacher, student, config = state
    row = scan(state)
    from types import SimpleNamespace
    original_lookup = pwd.getpwnam
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=student.uid)
                        if name == 'student' else original_lookup(name))
    manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                   metadata={'description': '仅授权学生'}, readers=['student'])
    job = manager.worker_claim()
    assert job['candidate']['readers'] == [{'uid': student.uid, 'username': student.username}]
    result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    assert len(TreeRegistry(policy.dataset_tree_registry).catalog(student)) == 1
    assert TreeRegistry(policy.dataset_tree_registry).catalog(Principal(12345, 'other', 'student')) == []
    value = yaml.safe_load((root / 'wiki/catalog/datasets.yaml').read_text())
    value['records'][0]['description'] = 'Changed description'
    (root / 'wiki/catalog/datasets.yaml').write_text(yaml.safe_dump(value))
    row = scan(state)
    with pytest.raises(KitError, match='授权'):
        manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                       metadata={'description': '公开'}, readers='platform-users')
    registry_before = policy.dataset_tree_registry.read_bytes()
    manager.review(teacher, row['candidate_id'], row['revision'], decision='approve',
                   metadata={'description': '仍只授权指定学生'})
    job = manager.worker_claim(); result = publisher.publish(policy, config, job['candidate'])
    manager.worker_finish(job['job_id'], job['lease_token'], result)
    assert policy.dataset_tree_registry.read_bytes() == registry_before


@pytest.mark.parametrize('preexisting', [False, True])
def test_manifest_directory_is_traversable_with_private_service_umask(state, preexisting):
    import stat
    root, policy, _manager, _teacher, student, config = state
    job = approve(state)
    manifests = root / 'dataset-manifests'
    if preexisting:
        manifests.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    try:
        result = publisher.publish(policy, config, job['candidate'])
        # The operation must not relax the process-wide service mask.
        observed = os.umask(0o077)
        assert observed == 0o077
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(manifests.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / 'data-publish-receipts').stat().st_mode) == 0o700
    manifest = manifests / (result['dataset_id'] + '.json')
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o644
    records = TreeRegistry(policy.dataset_tree_registry).catalog(student)
    assert records[0]['dataset_id'] == result['dataset_id']
    assert TreeRegistry(policy.dataset_tree_registry).list_files(student, result['dataset_id'])['items'][0]['path'] == 'input.bin'


def test_publisher_does_not_chmod_an_untrusted_manifest_symlink(state):
    import stat
    root, policy, _manager, _teacher, _student, config = state
    job = approve(state)
    outside = root / 'private-unrelated'
    outside.mkdir(mode=0o700)
    (root / 'dataset-manifests').symlink_to(outside, target_is_directory=True)
    with pytest.raises((KitError, OSError)):
        publisher.publish(policy, config, job['candidate'])
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700
