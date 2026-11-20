"""Synthetic publication integration: mocked ownership, real validation and I/O."""
import json
import hashlib
import os
from pathlib import Path
import pwd
import shutil
import tempfile
from types import SimpleNamespace

import pytest

from researchkit import web_datasets, web_data_publisher as publisher, web_dataset_permissions as permissions
from researchkit.files import KitError
from researchkit.web_data_management import DataManagement
from researchkit.web_dataset_discovery import discover, load_management_config
from researchkit.web_dataset_trees import TreeRegistry, TREE_SCHEMA_README
from researchkit.web_identity import Principal
from test_web_data_management import _Store


@pytest.fixture
def state(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-readme-test-', dir=Path.cwd()))
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid in {0, os.getuid(), 65534})
    monkeypatch.setattr(permissions, '_require_root', lambda: None)
    monkeypatch.setattr(permissions.os, 'fchown', lambda fd, uid, gid: None)
    teacher = Principal(os.getuid(), 'ly', 'teacher')
    student = Principal(100123, 'test', 'student')
    original_lookup = pwd.getpwnam
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=teacher.uid) if name == 'ly' else original_lookup(name))
    try:
        raw = root / 'raw'; raw.mkdir()
        unit = raw / 'practice'; unit.mkdir()
        (unit / 'README.md').write_text('说明一', encoding='utf-8')
        (unit / 'data.csv').write_text('1,2,3\n')
        conf = root / 'management.json'
        conf.write_text(json.dumps({'schema':'researchkit.web-data-management/v1', 'raw_root':str(raw),
                                    'discovery_mode':'readme', 'prepare_permissions':True,
                                    'scan_interval_seconds':60, 'seconds_per_dataset':60}))
        registry = root / 'trees.json'; registry.write_text(json.dumps({'schema':'researchkit.web-dataset-trees/v1','datasets':[]}))
        metadata = root / 'metadata.json'; metadata.write_text(json.dumps({'schema':'researchkit.web-dataset-metadata/v1','datasets':{}}))
        policy = SimpleNamespace(dataset_management_config=conf, dataset_tree_registry=registry,
                                 dataset_registry=None, dataset_metadata=metadata)
        policy.principal = lambda uid: {teacher.uid: teacher, student.uid: student}[uid]
        manager = DataManagement(_Store(root / 'store.sqlite'), policy)
        manager.worker_start('test')
        yield SimpleNamespace(root=root, raw=raw, unit=unit, teacher=teacher, student=student,
                              policy=policy, manager=manager, config=load_management_config(conf))
    finally:
        shutil.rmtree(root)


def scan(s):
    with s.manager._connection() as conn:
        conn.execute("UPDATE data_management_jobs SET created_at='2000-01-01T00:00:00Z' WHERE kind='scan'")
    s.manager.request_scan(s.teacher)
    job = s.manager.worker_claim()
    assert job['kind'] == 'scan'
    from researchkit.web_readme_cache import put
    def cache(relative, provenance_ref, original):
        candidate_id = hashlib.sha256(str(s.config['raw_root'] / relative).encode()).hexdigest()[:24]
        put(s.manager, job_id=job['job_id'], lease_token=job['lease_token'], candidate_id=candidate_id,
            provenance_ref=provenance_ref, original=original)
    items = publisher.sync_readme_metadata(s.policy, s.config, discover(s.config, s.policy, on_readme=cache))
    s.manager.worker_finish(job['job_id'], job['lease_token'], {'items':items})
    return s.manager.list_candidates(s.teacher, status='all')['items']


def approve(s):
    row = next(r for r in scan(s) if r['relative_root'] == 'practice')
    s.manager.review(s.teacher, row['candidate_id'], row['revision'], metadata={'description':'伪造'}, readers='platform-users')
    job = s.manager.worker_claim()
    assert job['kind'] == 'publish'
    return job


def test_full_review_publication_sync_and_data_verification(state):
    s = state; job = approve(s)
    reg = TreeRegistry(s.policy.dataset_tree_registry)
    assert reg.catalog(s.student) == []
    result = publisher.publish(s.policy, s.config, job['candidate'])
    s.manager.worker_finish(job['job_id'], job['lease_token'], result)
    item = reg._load()[result['dataset_id']]
    assert reg._manifest(item)['schema'] == TREE_SCHEMA_README
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['description'] == '说明一'
    refs = [reg.reference(item)]
    with reg.open_references([s.teacher, s.student], refs):
        pass
    (s.unit / 'README.md').write_text('---\ntags: [气象, 教学]\n---\n说明二', encoding='utf-8')
    row = next(r for r in scan(s) if r['relative_root'] == 'practice')
    assert row['status'] == 'published'
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['description'] == '说明二'
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['tags'] == ['气象', '教学']
    assert [reg.reference(reg._load()[result['dataset_id']])] == refs
    (s.unit / 'README.md').write_text('')
    scan(s)
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['description'] == ''
    (s.unit / 'data.csv').write_text('changed')
    with pytest.raises(KitError, match='发生变化'):
        with reg.open_references([s.teacher, s.student], refs):
            pass


def test_retry_after_registry_failure_reuses_prepared_receipt(state, monkeypatch):
    s = state; job = approve(s); original = publisher._backup_and_update
    def fail(path, expected, raw):
        if path == s.policy.dataset_tree_registry:
            raise OSError('simulated registry failure')
        return original(path, expected, raw)
    monkeypatch.setattr(publisher, '_backup_and_update', fail)
    with pytest.raises(OSError):
        publisher.publish(s.policy, s.config, job['candidate'])
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []
    assert list((s.root / 'data-prepare-receipts').glob('*.json'))
    monkeypatch.setattr(publisher, '_backup_and_update', original)
    result = publisher.publish(s.policy, s.config, job['candidate'])
    assert result['dataset_id'] == job['candidate']['dataset_id']
    refs = TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student)
    assert publisher.publish(s.policy, s.config, job['candidate'])['reused']
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == refs


def test_old_write_descriptor_cannot_change_sealed_data(state):
    s = state; job = approve(s)
    old_fd = os.open(s.unit / 'data.csv', os.O_RDWR)
    try:
        result = publisher.publish(s.policy, s.config, job['candidate'])
        os.write(old_fd, b'overwrite')
        registry = TreeRegistry(s.policy.dataset_tree_registry)
        ref = registry.reference(registry._load()[result['dataset_id']])
        with registry.open_references([s.teacher], [ref]):
            assert (s.unit / 'data.csv').read_text() == '1,2,3\n'
    finally:
        os.close(old_fd)


def test_subsecond_remaining_budget_is_not_rounded_to_expired(state, monkeypatch):
    s = state; job = approve(s)
    s.config['seconds_per_dataset'] = 1
    ticks = iter([100.0])
    monkeypatch.setattr(publisher.time, 'monotonic', lambda: next(ticks, 100.1))
    result = publisher.publish(s.policy, s.config, job['candidate'])
    assert result['dataset_id'] == job['candidate']['dataset_id']


@pytest.mark.parametrize('link_kind', ['hard', 'symbolic'])
def test_links_rejected_without_mutating_outside_file(state, link_kind):
    s = state
    outside = s.root / 'outside'; outside.write_text('outside')
    if link_kind == 'hard':
        os.link(outside, s.unit / 'linked')
    else:
        (s.unit / 'linked').symlink_to(outside)
    row = scan(s)[0]
    assert row['kind'] == 'error'
    assert outside.read_text() == 'outside'
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []


def test_removed_unregistered_marker_is_retired_registered_is_retained(state):
    s = state; job = approve(s)
    result = publisher.publish(s.policy, s.config, job['candidate'])
    s.manager.worker_finish(job['job_id'], job['lease_token'], result)
    other = s.raw / 'other'; other.mkdir(); (other / 'README.md').write_text('other')
    rows = scan(s)
    assert any(r['relative_root'] == 'other' and r['status'] == 'pending' for r in rows)
    (other / 'README.md').unlink(); (s.unit / 'README.md').unlink()
    rows = scan(s)
    assert not any(r['relative_root'] == 'other' for r in rows)
    history = s.manager.list_candidates(s.teacher, status='withdrawn')['items']
    assert next(r for r in history if r['relative_root'] == 'other')['status'] == 'withdrawn'
    registered = next(r for r in rows if r['relative_root'] == 'practice')
    assert registered['status'] == 'published'
    assert json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['description'] == '说明一'


@pytest.mark.parametrize('link_kind', ['hard', 'symbolic'])
def test_links_added_after_review_are_rejected_before_outside_changes(state, link_kind):
    s = state; job = approve(s)
    outside = s.root / 'outside'; outside.write_text('unchanged')
    if link_kind == 'hard':
        os.link(outside, s.unit / 'linked')
    else:
        (s.unit / 'linked').symlink_to(outside)
    with pytest.raises(KitError):
        publisher.publish(s.policy, s.config, job['candidate'])
    assert outside.read_text() == 'unchanged'
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []


def test_legacy_registration_does_not_need_metadata_reapproval_after_switch(state):
    import hashlib
    from researchkit.web_dataset_trees import prepare_tree
    from researchkit.web_dataset_discovery import snapshot_fingerprint
    s = state
    (s.unit / 'README.md').unlink()
    document = prepare_tree(s.unit)
    raw = publisher.canonical(document) + b'\n'
    manifest = s.root / 'legacy-manifest.json'; manifest.write_bytes(raw)
    entry = {'dataset_id':'legacy-v1','manifest':str(manifest),'sha256':hashlib.sha256(raw).hexdigest(),
             'total_size':len((s.unit/'data.csv').read_bytes()),'file_count':1,'readers':'platform-users'}
    s.policy.dataset_tree_registry.write_bytes(publisher.canonical({'schema':'researchkit.web-dataset-trees/v1','datasets':[entry]}))
    s.policy.dataset_metadata.write_bytes(publisher.canonical({'schema':'researchkit.web-dataset-metadata/v1','datasets':{'legacy-v1':{'name':'old','description':'保留的中文说明'}}}))
    item = discover(s.config,s.policy)[0]
    # Simulate an earlier Wiki-source review row, whose source digest differs
    # from the stored reviewed Chinese notes. Switching discovery must not ask
    # for an impossible README review on this immutable legacy directory.
    s.manager.config['discovery_mode']='wiki'
    previous={**item,'metadata_digest':'old-wiki-digest','draft_metadata':{},'original_metadata':{'name':'old','description':'English source'}}
    with s.manager._connection() as conn:s.manager._upsert_scan(conn,[previous])
    s.manager.config['discovery_mode']='readme'
    registry_before=s.policy.dataset_tree_registry.read_bytes()
    row=scan(s)[0]
    assert row['kind']=='unchanged' and row['status']=='published'
    assert row['description']=='保留的中文说明'
    assert not row['readme_managed']
    assert s.policy.dataset_tree_registry.read_bytes()==registry_before


def test_long_readme_is_cached_whole_without_enlarging_summary_or_changing_dataset(state):
    from researchkit.web_readme_cache import get
    s = state
    body = '# 长说明\n\n' + ('完整说明测试，保留正文。\n' * 2000) + '\n## 最后的章节\n\n**末尾内容不能丢失**\n'
    original = '\ufeff---\r\ntags: [教学]\r\n---\r\n' + body
    (s.unit / 'README.md').write_bytes(original.encode('utf-8'))
    job = approve(s)
    result = publisher.publish(s.policy, s.config, job['candidate'])
    s.manager.worker_finish(job['job_id'], job['lease_token'], result)
    row = scan(s)[0]
    assert len(row['description']) <= 240
    detail = s.manager.get_candidate(s.teacher, row['candidate_id'])
    assert len(detail['original_metadata']['description'].encode()) <= 16384
    assert detail['readme']['complete'] is True
    assert detail['readme']['markdown'] == body
    assert detail['readme']['original'].encode('utf-8') == original.encode('utf-8')
    assert 'tags:' not in detail['readme']['markdown']
    ref = json.loads(s.policy.dataset_metadata.read_text())['datasets'][result['dataset_id']]['provenance_ref']
    assert get(s.manager, ref)['markdown'] == body
    assert get(s.manager, ref)['original'] == original
    registered = TreeRegistry(s.policy.dataset_tree_registry)
    old_reference = registered.reference(registered._load()[result['dataset_id']])
    old_readers = registered._load()[result['dataset_id']]['readers']
    new_body = body + '\n新的最后一段\n'
    (s.unit / 'README.md').write_text('---\ntags: [教学]\n---\n' + new_body)
    scan(s)
    current = s.manager.get_candidate(s.teacher, row['candidate_id'])
    assert current['readme']['markdown'] == new_body
    assert current['readme']['source_ref'] != ref
    assert 'original' not in row and 'readme' not in row, 'full bodies must not multiply the list response'
    (s.unit / 'README.md').write_text('# 再次维护说明\n')
    scan(s)
    with s.manager._connection() as connection:
        assert connection.execute('SELECT count(*) FROM data_management_readme_bodies WHERE candidate_id=?',
                                  (row['candidate_id'],)).fetchone()[0] <= 2
    assert registered.reference(registered._load()[result['dataset_id']]) == old_reference
    assert registered._load()[result['dataset_id']]['readers'] == old_readers
    with pytest.raises(KitError, match='教师'):
        s.manager.get_candidate(s.student, row['candidate_id'])


def test_body_cache_requires_active_scan_lease_and_rejects_conflicting_sources(state):
    from researchkit.web_readme_cache import put
    s = state
    with s.manager._connection() as connection:
        connection.execute("UPDATE data_management_jobs SET created_at='2000-01-01T00:00:00Z'")
    s.manager.request_scan(s.teacher); job = s.manager.worker_claim()
    kwargs = {'job_id':job['job_id'], 'lease_token':job['lease_token'],
              'candidate_id':'a'*24, 'provenance_ref':'README:README.md#sha256='+hashlib.sha256('# 正文'.encode()).hexdigest(), 'original':'# 正文'}
    put(s.manager, **kwargs)
    with pytest.raises(KitError, match='不一致'):
        put(s.manager, **{**kwargs, 'original':'不同的正文'})
    with pytest.raises(KitError, match='租约'):
        put(s.manager, **{**kwargs, 'lease_token':'wrong'})
    with pytest.raises(KitError, match='上限'):
        put(s.manager, **{**kwargs, 'original':'x'*(1024*1024+1)})
    with pytest.raises(KitError, match='字段'):
        put(s.manager, **{**kwargs, 'candidate_id':'../../etc/passwd'})
    s.manager.worker_finish(job['job_id'], job['lease_token'], {'items':[]})
    with pytest.raises(KitError, match='租约'):
        put(s.manager, **kwargs)
