"""Root-side raw tree cleanup on disposable README publication fixtures."""
import json
import os
import stat
import hashlib

import pytest

from researchkit import web_cleanup_raw as cleanup
from researchkit import web_data_publisher as publisher
from researchkit.files import KitError
from researchkit.web_dataset_access import registered_entry
from researchkit.web_dataset_trees import TreeRegistry
from researchkit.web_dataset_trees import prepare_tree
from test_web_readme_publication import approve, state


def published(s):
    job = approve(s)
    result = publisher.publish(s.policy, s.config, job['candidate'])
    s.manager.worker_finish(job['job_id'], job['lease_token'], result)
    return result['dataset_id']


def request(s, dataset_id, ident='7f3b7138-407b-45dc-9a5e-b5f6b7b07972'):
    return {'request_id': ident, 'dataset_id': dataset_id,
            'expected_entry': registered_entry(s.policy, dataset_id)}


def add_unrelated_tree(s):
    """A normal independent registry/metadata update during cleanup recovery."""
    other = s.raw / 'other'; other.mkdir()
    (other / 'README.md').write_text('other')
    payload = publisher.canonical(prepare_tree(other, readme_mode=True)) + b'\n'
    ident = 'other-v1'; manifest = s.root / 'dataset-manifests' / (ident + '.json')
    manifest.write_bytes(payload)
    entry = {'dataset_id': ident, 'manifest': str(manifest),
             'sha256': hashlib.sha256(payload).hexdigest(), 'total_size': 0,
             'file_count': 0, 'readers': 'platform-users'}
    registry = json.loads(s.policy.dataset_tree_registry.read_text())
    registry['datasets'].append(entry); s.policy.dataset_tree_registry.write_bytes(publisher.canonical(registry) + b'\n')
    metadata = json.loads(s.policy.dataset_metadata.read_text())
    metadata['datasets'][ident] = {'name': 'other'}
    s.policy.dataset_metadata.write_bytes(publisher.canonical(metadata) + b'\n')
    return ident


@pytest.fixture(autouse=True)
def root_service(monkeypatch):
    monkeypatch.setattr(cleanup.os, 'geteuid', lambda: 0)


def test_permanent_cleanup_quarantines_then_removes_only_registered_tree(state):
    s = state; dataset_id = published(s)
    task = request(s, dataset_id)
    manifest = s.root / 'dataset-manifests' / (dataset_id + '.json')
    result = cleanup.purge_raw(s.policy, s.config, task)
    assert result == {'dataset_id': dataset_id, 'deleted': True, 'bytes_removed': len(b'1,2,3\n')}
    assert not s.unit.exists() and s.raw.exists() and not manifest.exists()
    assert TreeRegistry(s.policy.dataset_tree_registry)._load() == {}
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'] == {}
    receipt = s.root / 'data-cleanup-receipts' / '7f3b7138-407b-45dc-9a5e-b5f6b7b07972.json'
    assert stat.S_IMODE(receipt.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert cleanup.purge_raw(s.policy, s.config, task)['reused'] is True


def test_changed_tree_is_not_renamed_or_unregistered(state):
    s = state; dataset_id = published(s)
    (s.unit / 'data.csv').write_text('changed\n')
    with pytest.raises(KitError, match='变化'):
        cleanup.purge_raw(s.policy, s.config, request(s, dataset_id))
    assert s.unit.exists() and dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_unmatched_entry_cannot_target_a_registered_tree(state):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    task['expected_entry'] = {**task['expected_entry'], 'total_size': 0}
    with pytest.raises(KitError, match='登记已变化'):
        cleanup.purge_raw(s.policy, s.config, task)
    assert s.unit.exists() and dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_retry_after_rename_never_deletes_new_directory_at_old_name(state, monkeypatch):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    real = cleanup._write_receipt
    def interrupt(path, receipt):
        if receipt['phase'] == 'quarantined':
            raise OSError('simulated interruption after rename')
        return real(path, receipt)
    monkeypatch.setattr(cleanup, '_write_receipt', interrupt)
    with pytest.raises(OSError, match='after rename'):
        cleanup.purge_raw(s.policy, s.config, task)
    holding = s.raw / ('.researchkit-cleanup-' + task['request_id'])
    assert holding.exists() and not s.unit.exists()
    s.unit.mkdir(); (s.unit / 'README.md').write_text('new data')
    monkeypatch.setattr(cleanup, '_write_receipt', real)
    with pytest.raises(KitError, match='原目录和隔离目录'):
        cleanup.purge_raw(s.policy, s.config, task)
    assert s.unit.exists() and holding.exists()


def test_retry_finishes_if_crash_happens_after_rename_before_phase_receipt(state, monkeypatch):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    real = cleanup._write_receipt
    raised = [False]
    def interrupt(path, receipt):
        if receipt['phase'] == 'quarantined' and not raised[0]:
            raised[0] = True
            raise OSError('simulated interruption after rename')
        return real(path, receipt)
    monkeypatch.setattr(cleanup, '_write_receipt', interrupt)
    with pytest.raises(OSError):
        cleanup.purge_raw(s.policy, s.config, task)
    monkeypatch.setattr(cleanup, '_write_receipt', real)
    assert cleanup.purge_raw(s.policy, s.config, task)['deleted'] is True
    assert not s.unit.exists()


def test_resume_preserves_unrelated_registry_and_metadata_changes(state, monkeypatch):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    real = cleanup._write_receipt
    def interrupt(path, receipt):
        if receipt['phase'] == 'quarantined':
            raise OSError('after isolation')
        return real(path, receipt)
    monkeypatch.setattr(cleanup, '_write_receipt', interrupt)
    with pytest.raises(OSError):
        cleanup.purge_raw(s.policy, s.config, task)
    other = add_unrelated_tree(s)
    monkeypatch.setattr(cleanup, '_write_receipt', real)
    assert cleanup.purge_raw(s.policy, s.config, task)['deleted'] is True
    assert other in TreeRegistry(s.policy.dataset_tree_registry)._load()
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][other] == {'name': 'other'}


def test_resume_stops_if_current_raw_root_changed(state, monkeypatch):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    real = cleanup._write_receipt
    def interrupt(path, receipt):
        if receipt['phase'] == 'quarantined':
            raise OSError('after isolation')
        return real(path, receipt)
    monkeypatch.setattr(cleanup, '_write_receipt', interrupt)
    with pytest.raises(OSError):
        cleanup.purge_raw(s.policy, s.config, task)
    moved = s.root / 'different-raw'; moved.mkdir()
    s.config['raw_root'] = moved
    monkeypatch.setattr(cleanup, '_write_receipt', real)
    with pytest.raises(KitError, match='当前共享数据根目录'):
        cleanup.purge_raw(s.policy, s.config, task)
    assert (s.raw / ('.researchkit-cleanup-' + task['request_id'])).exists()
    assert dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_resume_stops_if_permission_preparation_is_disabled(state, monkeypatch):
    s = state; dataset_id = published(s); task = request(s, dataset_id)
    real = cleanup._write_receipt
    def interrupt(path, receipt):
        if receipt['phase'] == 'quarantined':
            raise OSError('after isolation')
        return real(path, receipt)
    monkeypatch.setattr(cleanup, '_write_receipt', interrupt)
    with pytest.raises(OSError):
        cleanup.purge_raw(s.policy, s.config, task)
    s.config['prepare_permissions'] = False
    monkeypatch.setattr(cleanup, '_write_receipt', real)
    with pytest.raises(KitError, match='当前仍启用权限准备'):
        cleanup.purge_raw(s.policy, s.config, task)
    assert dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_legacy_raw_registry_overlap_blocks_cleanup(state):
    s = state; dataset_id = published(s)
    # The cleanup module must inspect both registry families.  A deliberately
    # malformed legacy file is also fail-closed rather than being ignored.
    legacy = s.root / 'legacy.json'
    legacy.write_text(json.dumps({'schema': 'researchkit.web-datasets/v1', 'datasets': [{
        'dataset_id': 'legacy', 'root': str(s.unit), 'readers': [], 'files': []}]}))
    s.policy.dataset_registry = legacy
    with pytest.raises(KitError, match='包含关系'):
        cleanup.purge_raw(s.policy, s.config, request(s, dataset_id))
    assert s.unit.exists()


@pytest.mark.parametrize('phase',['registry-removed','metadata-removed','manifest-removed','completed'])
def test_resume_after_each_committed_filesystem_phase(state,monkeypatch,phase):
    s=state;dataset_id=published(s);task=request(s,dataset_id)
    original=cleanup._write_receipt;interrupted=[False]
    def fail_once(path,receipt):
        if receipt['phase']==phase and not interrupted[0]:
            interrupted[0]=True;raise OSError('lost phase receipt')
        return original(path,receipt)
    monkeypatch.setattr(cleanup,'_write_receipt',fail_once)
    with pytest.raises(OSError):cleanup.purge_raw(s.policy,s.config,task)
    assert cleanup.purge_raw(s.policy,s.config,task)['deleted']
    assert not s.unit.exists()
    assert not list(s.raw.glob('.researchkit-cleanup-*'))


def test_same_filesystem_bind_mount_is_rejected_before_rename(state,monkeypatch):
    s=state;dataset_id=published(s);task=request(s,dataset_id)
    actual=cleanup.Path.read_text
    def mountinfo(path,*args,**kwargs):
        value=actual(path,*args,**kwargs)
        if str(path)=='/proc/self/mountinfo':
            value+='\n999999 1 0:1 / '+str(s.unit)+'/nested ro - tmpfs tmpfs ro\n'
        return value
    monkeypatch.setattr(cleanup.Path,'read_text',mountinfo)
    with pytest.raises(KitError,match='挂载点'):cleanup.purge_raw(s.policy,s.config,task)
    assert s.unit.exists() and dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_unsafe_root_readme_is_rejected_before_any_registry_changes(state):
    s=state;dataset_id=published(s);task=request(s,dataset_id)
    outside=s.root/'private-target';outside.write_text('keep')
    (s.unit/'README.md').unlink();(s.unit/'README.md').symlink_to(outside)
    with pytest.raises(KitError,match='README'):cleanup.purge_raw(s.policy,s.config,task)
    assert outside.read_text()=='keep' and s.unit.exists()
    assert dataset_id in TreeRegistry(s.policy.dataset_tree_registry)._load()


def test_quarantined_tree_is_not_rediscovered_as_a_new_dataset(state,monkeypatch):
    from researchkit.web_dataset_readme import scan
    s=state;dataset_id=published(s);task=request(s,dataset_id)
    original=cleanup._write_receipt
    def interrupted(path,receipt):
        if receipt['phase']=='quarantined':raise OSError('interrupt after isolation')
        original(path,receipt)
    monkeypatch.setattr(cleanup,'_write_receipt',interrupted)
    with pytest.raises(OSError):cleanup.purge_raw(s.policy,s.config,task)
    assert scan(s.raw,seconds=60)=={}
