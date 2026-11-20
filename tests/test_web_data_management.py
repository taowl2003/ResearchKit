import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_data_management import DataManagement
from researchkit.web_dataset_discovery import discover, load_management_config, snapshot_difference, snapshot_fingerprint
from researchkit.web_dataset_trees import tree_metadata
from researchkit.web_identity import Principal


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'test', 'student')


class _Store:
    """Only supplies the private Store connection contract under test."""
    def __init__(self, path): self.path = path
    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, isolation_level=None); connection.row_factory = sqlite3.Row
        try: yield connection
        finally: connection.close()


def _config(root: Path, raw: Path, *, wiki: Path | None = None) -> Path:
    path = root / 'management.json'
    payload = {'schema': 'researchkit.web-data-management/v1', 'raw_root': str(raw),
               'scan_interval_seconds': 60, 'seconds_per_dataset': 60}
    if wiki is not None:
        payload['wiki_root'] = str(wiki)
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def managed(tmp_path, monkeypatch):
    # Test directories are not root-owned in the development sandbox.  This is
    # deliberately limited to trusted-reader tests; production keeps the real
    # root ownership predicate.
    monkeypatch.setattr('researchkit.web_datasets._safe_info', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('researchkit.web_dataset_trees._safe_info', lambda *_args, **_kwargs: None)
    raw = tmp_path / 'raw'; raw.mkdir()
    policy = SimpleNamespace(dataset_management_config=_config(tmp_path, raw), dataset_tree_registry=None,
                             dataset_metadata=None, principals={TEACHER.uid: TEACHER, STUDENT.uid: STUDENT})
    policy.principal = lambda uid: policy.principals[uid]
    manager = DataManagement(_Store(tmp_path / 'store.sqlite'), policy)
    return manager, raw


def _item(name='alpha', *, fingerprint='one', digest='m1', kind='new'):
    return {'candidate_id': hashlib.sha256(('/raw/' + name).encode()).hexdigest()[:24], 'relative_root': name,
            'original_metadata': {'name': name, 'wiki_ref': 'card-' + name, 'wiki_fact_state': 'verified'},
            'metadata_digest': digest, 'source_fingerprint': {'root_stat': [1, 2, 3, 4, 5], 'entries_digest': fingerprint},
            'kind': kind, 'draft_metadata': {'description': '中文草稿'}, 'seed_state': 'matched'}


def _scan(manager, items):
    # Simulate the publisher interval without waiting a minute in unit tests.
    with manager._connection() as connection:
        connection.execute("UPDATE data_management_jobs SET created_at='2000-01-01T00:00:00Z' WHERE kind='scan' AND status='succeeded'")
    job = manager.request_scan(TEACHER)
    claimed = manager.worker_claim()
    assert claimed and claimed['job_id'] == job['job_id'] and claimed['kind'] == 'scan'
    manager.worker_finish(claimed['job_id'], claimed['lease_token'], {'items': items})


def test_students_are_rejected_and_scan_is_idempotent(managed):
    manager, _ = managed
    with pytest.raises(KitError, match='教师'):
        manager.status(STUDENT)
    _scan(manager, [_item()])
    first = manager.get_candidate(TEACHER, _item()['candidate_id'])
    _scan(manager, [_item()])
    again = manager.get_candidate(TEACHER, _item()['candidate_id'])
    assert first['revision'] == again['revision'] == 1
    assert manager.request_scan(TEACHER)['reused'] is True


def test_stale_review_source_update_and_immutable_publish_payload(managed):
    manager, _ = managed; source = _item()
    _scan(manager, [source])
    approved = manager.review(TEACHER, source['candidate_id'], 1, metadata={'description': '审核中文'}, readers='platform-users')
    job = manager.worker_claim()
    assert job['kind'] == 'publish' and job['candidate']['current_metadata']['description'] == '审核中文'
    # A running publication owns its immutable payload; a scan must not alter it.
    changed = {**source, 'source_fingerprint': {'root_stat': [1, 2, 9, 4, 5], 'entries_digest': 'two'}}
    with manager._connection() as connection:
        connection.execute('BEGIN IMMEDIATE'); manager._upsert_scan(connection, [changed]); connection.execute('COMMIT')
    assert manager.worker_claim() is None
    manager.worker_finish(job['job_id'], job['lease_token'], {'dataset_id': approved['dataset_id'], 'published_metadata': job['candidate']['metadata']})
    # A later scan changes the source and invalidates previous approval, while
    # stale UI revisions cannot enqueue a replacement job.
    _scan(manager, [changed])
    current = manager.get_candidate(TEACHER, source['candidate_id'])
    assert current['revision'] == 2 and current['status'] == 'pending'
    with pytest.raises(KitError, match='已更新'):
        manager.review(TEACHER, source['candidate_id'], 1, readers='platform-users')


def test_ignore_failure_retry_and_lease_recovery(managed):
    manager, _ = managed; ignored = _item('ignored'); _scan(manager, [ignored])
    assert manager.review(TEACHER, ignored['candidate_id'], 1, decision='ignore')['status'] == 'ignored'
    source = _item('retry'); _scan(manager, [source])
    queued = manager.review(TEACHER, source['candidate_id'], 1, readers='platform-users')
    claimed = manager.worker_claim(); assert claimed['job_id'] == queued['job_id']
    manager.worker_finish(claimed['job_id'], claimed['lease_token'], error='manifest check failed')
    assert manager.get_candidate(TEACHER, source['candidate_id'])['status'] == 'failed'
    retried = manager.review(TEACHER, source['candidate_id'], 1, decision='retry', readers='platform-users')
    assert retried['status'] == 'queued'
    claimed = manager.worker_claim()
    with manager._connection() as connection:
        connection.execute("UPDATE data_management_jobs SET lease_expires_at=0 WHERE job_id=?", (claimed['job_id'],))
    recovered = manager.worker_claim()
    assert recovered and recovered['job_id'] == claimed['job_id'] and recovered['lease_token'] != claimed['lease_token']


def test_original_directory_data_change_cannot_be_published(managed):
    manager, _ = managed
    item = _item('old', kind='data-change'); item['registered_dataset_id'] = 'old-v1'
    _scan(manager, [item])
    record = manager.get_candidate(TEACHER, item['candidate_id'])
    assert record['status'] == 'pending'
    with pytest.raises(KitError, match='不能覆盖旧引用'):
        manager.review(TEACHER, item['candidate_id'], record['revision'], readers='platform-users')


def test_registered_chinese_seed_is_reviewed_once_then_does_not_loop(managed):
    manager, _ = managed
    item = _item('registered', kind='unchanged')
    item.update({'registered_dataset_id': 'registered-v1', 'registered_readers': 'platform-users',
                 'current_metadata': {}, 'draft_metadata': {'description': '中文说明'}})
    _scan(manager, [item])
    before = manager.get_candidate(TEACHER, item['candidate_id'])
    assert before['kind'] == 'metadata-change' and before['status'] == 'pending'
    approved = manager.review(TEACHER, item['candidate_id'], before['revision'], metadata={'description': '中文说明'}, readers='platform-users')
    job = manager.worker_claim()
    manager.worker_finish(job['job_id'], job['lease_token'], {'dataset_id': approved['dataset_id'], 'published_metadata': job['candidate']['metadata']})
    _scan(manager, [item])
    after = manager.get_candidate(TEACHER, item['candidate_id'])
    assert after['kind'] == 'unchanged' and after['status'] == 'published' and after['revision'] == before['revision']


def test_discovery_keeps_approved_deep_subset_and_only_adds_real_new_root(tmp_path, monkeypatch):
    monkeypatch.setattr('researchkit.web_datasets._safe_info', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('researchkit.web_dataset_trees._safe_info', lambda *_args, **_kwargs: None)
    raw = tmp_path / 'raw'; (raw / 'aggregate' / 'class-a').mkdir(parents=True); (raw / 'fresh').mkdir()
    wiki = tmp_path / 'wiki'; (wiki / 'sources').mkdir(parents=True); (wiki / 'catalog').mkdir(); (wiki / 'reports').mkdir()
    (wiki / 'sources' / 'dataset-boundaries.yaml').write_text(
        'adjudicated_by: user\nsource_root: ' + str(raw) + '\nroots:\n- path: ' + str(raw / 'aggregate') +
        '\n  model: aggregating-directory\n  card: dataset-parent\n  subsets: [class-a]\n')
    (wiki / 'catalog' / 'datasets.yaml').write_text('records: []\n')
    (wiki / 'catalog' / 'dataset-subsets.yaml').write_text('records: []\n')
    (wiki / 'reports' / 'ingestion-wave-5-findings.json').write_text('{"units": []}')
    policy = SimpleNamespace(dataset_tree_registry=None, dataset_metadata=None)
    items = discover(load_management_config(_config(tmp_path, raw, wiki=wiki)), policy)
    assert {item['relative_root'] for item in items} == {'aggregate/class-a', 'fresh'}
    assert 'aggregate' not in {item['relative_root'] for item in items}


def test_snapshot_fingerprint_ignores_publication_file_hashes():
    scan = {'root_stat': [1, 2, 3, 4, 5], 'entries': [
        {'path': 'a.csv', 'kind': 'file', 'stat': [1, 9, 3, 4, 5]},
        {'path': 'sub', 'kind': 'directory', 'stat': [1, 10, 0, 4, 5]},
    ]}
    manifest = {'root_stat': [1, 2, 3, 4, 5], 'entries': [
        {'path': 'a.csv', 'kind': 'file', 'stat': [1, 9, 3, 4, 5], 'sha256': 'a' * 64},
        {'path': 'sub', 'kind': 'directory', 'stat': [1, 10, 0, 4, 5]},
    ]}
    assert snapshot_fingerprint(scan) == snapshot_fingerprint(manifest)


def test_snapshot_difference_explains_stat_and_structure_without_hashes():
    old = {'root_stat': [1, 2, 3, 4, 5], 'entries': [
        {'path': 'modified', 'kind': 'file', 'stat': [1, 2, 3, 4, 5]},
        {'path': 'removed', 'kind': 'file', 'stat': [1, 2, 3, 4, 5]},
        {'path': 'unchanged', 'kind': 'file', 'stat': [1, 2, 3, 4, 5], 'sha256': 'secret-hash'},
    ]}
    current = {'root_stat': [1, 9, 3, 8, 5], 'entries': [
        {'path': 'added', 'kind': 'file', 'stat': [1, 2, 3, 4, 5]},
        {'path': 'modified', 'kind': 'file', 'stat': [1, 8, 3, 9, 5]},
        {'path': 'unchanged', 'kind': 'file', 'stat': [1, 2, 3, 4, 5]},
    ]}
    diagnostic = snapshot_difference(old, current)
    assert '未比较文件内容' in diagnostic and '不一定代表内容改变' in diagnostic
    assert '根目录属性变化：inode、修改时间' in diagnostic
    assert '新增 1 项，移除 1 项，属性变化 1 项' in diagnostic
    assert 'added' in diagnostic and 'removed' in diagnostic and 'modified' in diagnostic
    assert 'secret-hash' not in diagnostic


def test_discovery_adds_diagnostic_for_registered_tree_stat_change(tmp_path, monkeypatch):
    monkeypatch.setattr('researchkit.web_datasets._safe_info', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('researchkit.web_dataset_trees._safe_info', lambda *_args, **_kwargs: None)
    raw = tmp_path / 'raw'; dataset = raw / 'sample'; dataset.mkdir(parents=True)
    (dataset / 'numbers.csv').write_text('1,2,3\n')
    fd = os.open(dataset, os.O_RDONLY | os.O_DIRECTORY)
    try:
        manifest = tree_metadata(fd, hash_files=False)
    finally:
        os.close(fd)
    manifest['root_stat'][1] += 1
    manifest['entries'][0]['stat'][3] += 1
    monkeypatch.setattr('researchkit.web_dataset_discovery._registered',
                        lambda _policy: ({str(dataset): ('sample-v1', manifest, 'platform-users')}, {}))
    item = discover(load_management_config(_config(tmp_path, raw)), SimpleNamespace())[0]
    assert item['kind'] == 'data-change'
    assert '根目录属性变化：inode' in item['error']
    assert 'numbers.csv' in item['error'] and '修改时间' in item['error']


def test_legacy_data_change_diagnostic_updates_without_revising_or_reopening(managed):
    manager, _ = managed
    item = _item('legacy', kind='data-change')
    item['registered_dataset_id'] = 'old-v1'
    _scan(manager, [item])
    manager.review(TEACHER, item['candidate_id'], 1, decision='ignore')
    item['error'] = '仅比较目录结构和文件属性，未比较文件内容；根目录属性变化：inode'
    _scan(manager, [item])
    updated = manager.get_candidate(TEACHER, item['candidate_id'])
    assert updated['error'] == item['error']
    assert updated['revision'] == 1 and updated['status'] == 'ignored'
    _scan(manager, [item])
    stable = manager.get_candidate(TEACHER, item['candidate_id'])
    assert stable['updated_at'] == updated['updated_at'] and stable['revision'] == 1


def test_list_active_attention_and_chinese_query(managed):
    manager, _ = managed
    source = _item('query'); source['draft_metadata'] = {'description': '中文可搜索'}
    _scan(manager, [source])
    assert manager.list_candidates(TEACHER, query='中文可搜索')['total'] == 1
    queued = manager.review(TEACHER, source['candidate_id'], 1, readers='platform-users')
    assert manager.list_candidates(TEACHER, status='active')['items'][0]['candidate_id'] == source['candidate_id']
    claimed = manager.worker_claim()
    manager.worker_finish(claimed['job_id'], claimed['lease_token'], error='publish failed')
    attention = manager.list_candidates(TEACHER, status='attention')
    assert attention['total'] == 1 and manager.status(TEACHER)['counts']['attention'] == 1
    assert queued['job_id'] == claimed['job_id']


def test_tag_filters_are_exact_paginated_and_preserve_source_tags_on_review(managed):
    manager, _ = managed
    ocean = _item('ocean', fingerprint='ocean', digest='ocean')
    ocean['original_metadata']['tags'] = ['海洋', '教学']
    climate = _item('climate', fingerprint='climate', digest='climate')
    climate['original_metadata']['tags'] = ['气候', '教学']
    substring = _item('substring', fingerprint='substring', digest='substring')
    substring['original_metadata']['tags'] = ['海洋学']
    _scan(manager, [ocean, climate, substring])

    any_page = manager.list_candidates(TEACHER, status='all', tags=['海洋'], tag_mode='any', limit=1)
    assert any_page['total'] == 1 and [item['candidate_id'] for item in any_page['items']] == [ocean['candidate_id']]
    assert any_page['available_tags'] == ['教学', '气候', '海洋', '海洋学']
    all_page = manager.list_candidates(TEACHER, status='all', tags=['教学', '气候'], tag_mode='all')
    assert [item['candidate_id'] for item in all_page['items']] == [climate['candidate_id']]
    assert manager.list_candidates(TEACHER, status='all', tags=['教学', '海洋'], tag_mode='any')['total'] == 2
    default_page = manager.list_candidates(TEACHER, status='all', tags=['教学', '海洋'])
    assert default_page['tag_mode'] == 'all'
    assert [item['candidate_id'] for item in default_page['items']] == [ocean['candidate_id']]
    assert manager.list_candidates(TEACHER, status='all', tags=['海洋', '气候'])['total'] == 0
    assert manager.get_candidate(TEACHER, ocean['candidate_id'])['tags'] == ['海洋', '教学']

    approved = manager.review(TEACHER, ocean['candidate_id'], 1, metadata={'description': '中文说明'}, readers='platform-users')
    job = manager.worker_claim()
    assert job['candidate']['metadata']['tags'] == ['海洋', '教学']
    manager.worker_finish(job['job_id'], job['lease_token'], {'dataset_id': approved['dataset_id'], 'published_metadata': job['candidate']['metadata']})
    assert manager.get_candidate(TEACHER, ocean['candidate_id'])['tags'] == ['海洋', '教学']
    with pytest.raises(KitError, match='标签筛选方式'):
        manager.list_candidates(TEACHER, tags=['海洋'], tag_mode='bad')


def test_ignored_candidate_stays_ignored_until_source_changes(managed):
    manager, _ = managed; source = _item('ignored-repeat')
    _scan(manager, [source])
    manager.review(TEACHER, source['candidate_id'], 1, decision='ignore')
    _scan(manager, [source])
    assert manager.get_candidate(TEACHER, source['candidate_id'])['status'] == 'ignored'
    changed = {**source, 'source_fingerprint': {'root_stat': [1, 2, 7, 4, 5], 'entries_digest': 'changed'}}
    _scan(manager, [changed])
    assert manager.get_candidate(TEACHER, source['candidate_id'])['status'] == 'pending'


def test_lifecycle_filter_keeps_correct_page_totals_and_restorable_detail(managed):
    from researchkit.web_lifecycle import AssetLifecycle
    manager, _raw = managed
    sources = [_item('active'), _item('retired'), _item('trashed')]
    for row in sources:
        row['registered_dataset_id'] = row['relative_root'] + '-v1'
    _scan(manager, sources)
    lifecycle = AssetLifecycle(manager.store)
    with manager._connection() as connection:
        for name, state in [('retired', 'retired'), ('trashed', 'trashed')]:
            connection.execute('''INSERT INTO web_asset_lifecycle
                (kind,ref,state,generation,updated_at,updated_by_uid,updated_by_username)
                VALUES ('dataset',?,?,1,'2026-10-24',1001,'ly')''', (name + '-v1', state))
    page = manager.list_candidates(TEACHER, status='all', limit=1)
    assert page['total'] == 1
    assert page['items'][0]['registered_dataset_id'] == 'active-v1'
    assert page['items'][0]['lifecycle']['state'] == 'active'
    shown = manager.list_candidates(TEACHER, status='all', include_inactive=True, limit=2)
    more = manager.list_candidates(TEACHER, status='all', include_inactive=True, offset=2)
    assert shown['total'] == more['total'] == 3
    assert len(shown['items']) == 2 and len(more['items']) == 1
    assert manager.get_candidate(TEACHER, _item('trashed')['candidate_id'])['lifecycle']['state'] == 'trashed'
    with pytest.raises(KitError, match='布尔值'):
        manager.list_candidates(TEACHER, include_inactive='true')


def test_readme_parent_boundary_withdraws_old_candidates_from_every_normal_list(managed):
    manager,raw=managed
    manager.config.update(discovery_mode='readme',prepare_permissions=True)
    leaves=['GIS/new/part','GIS/old/part','GIS/newer/part','manual']
    for rel in leaves:
        folder=raw/rel;folder.mkdir(parents=True)
        (folder/'README.md').write_text('notes')
        (folder/'data.csv').write_bytes(b'unchanged')
    sources=discover(manager.config,manager.policy)
    _scan(manager,sources)
    by_path={row['relative_root']:row for row in sources}
    manual=by_path['manual']
    manager.review(TEACHER,manual['candidate_id'],1,decision='ignore')
    # Upgrade the exact old automatic-ignore state observed by the user.
    with manager._connection() as con:
        con.execute("UPDATE data_management_candidates SET status='ignored',kind='missing',error='未找到有效 README 标记，已从待审核候选收起；没有删除目录' WHERE candidate_id=?", (by_path['GIS/old/part']['candidate_id'],))
    for rel in ['GIS/new','GIS/old']:(raw/rel/'README.md').write_text('whole dataset')
    current=discover(manager.config,manager.policy)
    _scan(manager,current)
    normal=manager.list_candidates(TEACHER,status='all')
    assert {row['relative_root'] for row in normal['items']}=={'GIS/new','GIS/old','GIS/newer/part','manual'}
    assert manager.list_candidates(TEACHER,status='ignored')['total']==1
    history=manager.list_candidates(TEACHER,status='withdrawn')
    assert {row['relative_root'] for row in history['items']}=={'GIS/new/part','GIS/old/part'}
    counts=manager.status(TEACHER)['counts']
    assert counts['ignored']==1 and counts['withdrawn']==2
    old=manager.get_candidate(TEACHER,by_path['GIS/new/part']['candidate_id'])
    assert old['revision']==2 and old['registered_dataset_id'] is None
    assert old['review']['automatic_withdrawal']['covered_by']=='GIS/new'
    assert 'GIS/new' in old['error']
    with pytest.raises(KitError,match='已更新'):
        manager.review(TEACHER,old['candidate_id'],1,readers='platform-users')
    with pytest.raises(KitError,match='当前数据集边界'):
        manager.review(TEACHER,old['candidate_id'],old['revision'],readers='platform-users')
    _scan(manager,current)
    assert manager.get_candidate(TEACHER,old['candidate_id'])['revision']==old['revision']
    # Removing the parent marker makes the very same child bytes eligible
    # again; a prior automatic withdrawal must not become a permanent ignore.
    (raw/'GIS/new/README.md').unlink()
    _scan(manager,discover(manager.config,manager.policy))
    returned=manager.get_candidate(TEACHER,old['candidate_id'])
    assert returned['status']=='pending' and returned['revision']==3
    assert returned['source_fingerprint']==old['source_fingerprint']
    assert manager.get_candidate(TEACHER,manual['candidate_id'])['status']=='ignored'
    for rel in leaves:assert (raw/rel/'data.csv').read_bytes()==b'unchanged'


def test_readme_boundary_history_preserves_registered_rows_and_live_publish_leases(managed):
    manager,_raw=managed
    manager.config.update(discovery_mode='readme',prepare_permissions=True)
    registered={**_item('GIS/old/registered',kind='unchanged'),'registered_dataset_id':'registered-v1','readme_managed':True}
    queued={**_item('GIS/new/queued'),'readme_managed':True}
    running={**_item('GIS/new/running'),'readme_managed':True}
    _scan(manager,[registered,queued,running])
    active=manager.review(TEACHER,running['candidate_id'],1,readers='platform-users')
    claimed=manager.worker_claim()
    assert claimed['job_id']==active['job_id']
    pending=manager.review(TEACHER,queued['candidate_id'],1,readers='platform-users')
    parent={**_item('GIS'),'readme_managed':True}
    with manager._connection() as con:
        con.execute('BEGIN IMMEDIATE');manager._upsert_scan(con,[parent]);con.execute('COMMIT')
        assert con.execute('SELECT status FROM data_management_jobs WHERE job_id=?',(pending['job_id'],)).fetchone()[0]=='cancelled'
    assert manager.get_candidate(TEACHER,queued['candidate_id'])['status']=='withdrawn'
    assert manager.get_candidate(TEACHER,running['candidate_id'])['status']=='publishing'
    previous=manager.get_candidate(TEACHER,registered['candidate_id'])
    assert previous['registered_dataset_id']=='registered-v1' and previous['status']=='published'


def test_readme_refresh_does_not_reopen_manually_ignored_unreadable_candidate(managed):
    manager,_raw=managed;manager.config.update(discovery_mode='readme')
    source={**_item('unreadable',kind='missing'),'readme_managed':True,'source_fingerprint':None,'error':'temporary permission error'}
    _scan(manager,[source])
    manager.review(TEACHER,source['candidate_id'],1,decision='ignore')
    _scan(manager,[source])
    current=manager.get_candidate(TEACHER,source['candidate_id'])
    assert current['status']=='ignored' and current['revision']==1


def test_tag_choices_keep_unavailable_labels_and_use_all_filtered_pages(managed):
    manager, _ = managed
    items = []
    for name, tags in [('ocean', ['海洋', '教学']), ('climate', ['气候', '教学']),
                       ('other', ['海洋学']), ('ignored-tag', ['另一个状态'])]:
        item = _item(name, fingerprint=name, digest=name)
        item['original_metadata']['tags'] = tags
        items.append(item)
    _scan(manager, items)
    manager.review(TEACHER, items[-1]['candidate_id'], 1, decision='ignore')
    # The full vocabulary stays visible when a status/query removes its rows.
    pending = manager.list_candidates(TEACHER, status='pending', limit=1)
    assert pending['total'] == 3 and len(pending['items']) == 1
    assert set(pending['selectable_tags']) == {'海洋', '气候', '海洋学', '教学'}
    assert set(pending['all_tags']) == {'海洋', '气候', '海洋学', '教学', '另一个状态'}
    assert '另一个状态' not in pending['selectable_tags']
    narrow = manager.list_candidates(TEACHER, status='pending', tags=['海洋'], tag_mode='all')
    assert set(narrow['all_tags']) == set(pending['all_tags'])
    assert set(narrow['selectable_tags']) == {'海洋', '教学'}
    assert narrow['tag_mode_totals'] == {'any': 1, 'all': 1}
    disjoint = manager.list_candidates(TEACHER, status='pending', tags=['海洋', '气候'], tag_mode='any', limit=1)
    assert disjoint['total'] == 2 and len(disjoint['items']) == 1
    assert set(disjoint['selectable_tags']) == {'海洋', '教学', '气候'}
    assert disjoint['tag_mode_totals'] == {'any': 2, 'all': 0}
    missing = manager.list_candidates(TEACHER, status='pending', query='no-such-directory', tags=['海洋'])
    assert missing['total'] == 0 and missing['selectable_tags'] == []
    assert missing['tags'] == ['海洋']  # canceling a selected tag remains possible
    assert set(missing['all_tags']) == set(pending['all_tags'])
