from pathlib import Path
import hashlib
import sqlite3
import uuid

import pytest

from researchkit.files import KitError
from researchkit.web_store import SubmissionStore
from researchkit.web_identity import Principal
from researchkit.web_content import capacity, normalize, register

TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'test', 'student')


def test_same_bytes_share_storage_across_snapshots_and_file_paths(tmp_path):
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, 'research')
    body = b'unchanged scientific code' * 1000
    a = store.submit(project['project_id'], STUDENT, 'major', {'a.py':body,'b.py':body}, [])
    b = store.submit(project['project_id'], STUDENT, 'minor', {'a.py':body,'c.py':b'new'}, [])
    assert a['submission_id'] != b['submission_id']
    stats = capacity(store)
    assert stats['unique_bytes'] == len(body) + 3
    assert stats['logical_bytes'] == 3*len(body) + 3
    assert stats['file_references'] == 4
    with store._connection() as con:
        con.execute('DELETE FROM submission_files WHERE submission_id=?', (a['submission_id'],))
    assert store.get_submission(b['submission_id'], STUDENT)['files']['a.py'] == body
    assert capacity(store)['unique_bytes'] == len(body) + 3
    with store._connection() as con:
        con.execute('DELETE FROM submission_files WHERE submission_id=?', (b['submission_id'],))
    assert capacity(store)['unique_bytes'] == 0


def test_rollback_and_tamper_do_not_change_shared_snapshots(tmp_path):
    store = SubmissionStore(tmp_path / 'store')
    p = store.create_project(STUDENT)
    a = store.submit(p['project_id'], STUDENT, 'minor', {'a':b'same'}, [])
    b = store.submit(p['project_id'], STUDENT, 'minor', {'a':b'same'}, [])
    with store._connection() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('UPDATE submission_files SET content=? WHERE submission_id=?', (b'wrong',a['submission_id']))
        con.execute('ROLLBACK')
    assert capacity(store)['unique_files'] == 1
    with store._connection() as con:
        con.execute('UPDATE submission_files SET content=? WHERE submission_id=?', (b'wrong',a['submission_id']))
    with pytest.raises(KitError):
        store.get_submission(a['submission_id'], STUDENT)
    assert store.get_submission(b['submission_id'], STUDENT)['files'] == {'a':b'same'}


def test_legacy_table_conversion_is_atomic_and_rejects_corruption(tmp_path):
    con = sqlite3.connect(':memory:',isolation_level=None)
    con.row_factory = sqlite3.Row
    register(con)
    con.execute('CREATE TABLE old_files (id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,content BLOB NOT NULL)')
    con.execute('INSERT INTO old_files VALUES(?,?,?,?)', ('1',hashlib.sha256(b'original').hexdigest(),8,b'tampered'))
    con.execute('BEGIN IMMEDIATE')
    with pytest.raises(KitError, match='校验失败'):
        normalize(con,'old_files')
    con.execute('ROLLBACK')
    assert con.execute("SELECT type FROM sqlite_master WHERE name='old_files'").fetchone()[0] == 'table'
    con.execute('UPDATE old_files SET content=?', (b'original',))
    con.execute('BEGIN IMMEDIATE'); normalize(con,'old_files'); con.execute('COMMIT')
    assert bytes(con.execute('SELECT content FROM old_files').fetchone()[0]) == b'original'
    con.execute('INSERT OR IGNORE INTO old_files VALUES(?,?,?,?)', ('1',hashlib.sha256(b'original').hexdigest(),8,b'original'))
    assert con.execute('SELECT ref_count FROM web_content_blobs').fetchone()[0] == 1


def test_assignment_baseline_atomic_idempotence_and_batch_rollback(tmp_path):
    store = SubmissionStore(tmp_path / 'store')
    p = store.create_project(TEACHER,'任务基线')
    request_id = str(uuid.uuid4()); fingerprint = 'a'*64
    a = store.submit(p['project_id'],TEACHER,'major',{'a':b'1'},[],assignment_request_id=request_id,assignment_fingerprint=fingerprint)
    b = store.submit(p['project_id'],TEACHER,'major',{'a':b'1'},[],assignment_request_id=request_id,assignment_fingerprint=fingerprint)
    assert a['submission_id'] == b['submission_id']
    with pytest.raises(KitError,match='其他内容'):
        store.submit(p['project_id'],TEACHER,'major',{'a':b'2'},[],assignment_request_id=request_id,assignment_fingerprint='b'*64)
    first = store.create_distributions(TEACHER,a['submission_id'],[TEACHER,STUDENT])
    second = store.create_distributions(TEACHER,a['submission_id'],[TEACHER,STUDENT])
    assert [x['distribution_id'] for x in first] == [x['distribution_id'] for x in second]
    p2 = store.create_project(TEACHER,'other')
    snap = store.submit(p2['project_id'],TEACHER,'major',{'x':b'2'},[])
    store.create_distribution(TEACHER,snap['submission_id'],STUDENT)
    with pytest.raises(KitError,match='复用'):
        store.create_distributions(TEACHER,snap['submission_id'],[TEACHER,Principal(STUDENT.uid,'other','student')])
    assert not any(row['baseline_submission_id'] == snap['submission_id'] and row['target_username']=='ly'
                   for row in store.list_distributions(TEACHER))
