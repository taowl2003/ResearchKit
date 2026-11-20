"""Transactional, content-addressed file bodies with compatible SQL file views.

Metadata and immutable snapshot identities stay separate. Each reference owns
one count; deleting a reference can remove bytes only when its last user leaves.
All entry points use the same SQLite transaction as their caller.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .files import KitError


def register(connection):
    connection.create_function('rk_content_sha256', 1,
                               lambda value: hashlib.sha256(bytes(value)).hexdigest(), deterministic=True)


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS web_content_blobs (
        sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, content BLOB NOT NULL,
        ref_count INTEGER NOT NULL DEFAULT 0 CHECK(ref_count >= 0))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS web_content_tables (
        table_name TEXT PRIMARY KEY)''')


def backup_before_migration(connection, database):
    """One private, consistent backup before the first file-table migration."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_content_blobs'").fetchone()
    occupied = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone()
    if exists or not occupied:
        return
    if not connection.execute('SELECT 1 FROM submissions LIMIT 1').fetchone():
        return
    backup = Path(str(database) + '.before-unified.sqlite3')
    # An existing backup is never overwritten. A failed prior migration can be
    # retried without losing the original. No arbitrary paths are accepted.
    import os
    import stat
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        info = backup.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise KitError('迁移备份文件权限不可信')
        with sqlite3.connect(backup) as check:
            if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise KitError('已有迁移备份不完整，请先由管理员检查')
        return
    else:
        os.close(fd)
    try:
        with sqlite3.connect(backup) as target:
            connection.backup(target)
        with backup.open('rb') as stream:
            os.fsync(stream.fileno())
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def normalize(connection, table):
    """Replace a file table with a writable view, preserving its public columns.

    Called only for fixed application-owned names during schema initialization;
    caller must hold BEGIN IMMEDIATE. Existing corrupt bodies stop migration.
    """
    if not re.fullmatch(r'[a-z][a-z0-9_]*', table):
        raise ValueError('invalid internal table name')
    initialize(connection)
    source = connection.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
    if source is None or source['type'] == 'view':
        return
    columns = list(connection.execute(f'PRAGMA table_info({table})'))
    names = [row['name'] for row in columns]
    if 'content' not in names or 'sha256' not in names:
        raise KitError('文件存储迁移结构不兼容')
    keys = [row['name'] for row in sorted(columns, key=lambda row: row['pk']) if row['pk']]
    if not keys:
        raise KitError('文件存储缺少固定身份')
    refs = 'web_content_refs_' + table
    sql = re.sub(r'(?i)^CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["`\[]?' + re.escape(table) + r'["`\]]?',
                 'CREATE TABLE ' + refs, source['sql'], count=1)
    sql, changed = re.subn(r'\bcontent\s+BLOB\s+NOT\s+NULL\b',
                          'blob_sha256 TEXT NOT NULL REFERENCES web_content_blobs(sha256)', sql, flags=re.I)
    if changed != 1:
        raise KitError('文件存储迁移字段不兼容')
    connection.execute(sql)
    metadata = [name for name in names if name != 'content']
    dest = metadata + ['blob_sha256']
    for row in connection.execute(f'SELECT * FROM {table}'):
        content = bytes(row['content'])
        digest = hashlib.sha256(content).hexdigest()
        if digest != row['sha256'] or ('size' in names and len(content) != row['size']):
            raise KitError('旧文件内容校验失败，已停止存储迁移；原库保留')
        connection.execute('INSERT OR IGNORE INTO web_content_blobs(sha256,size,content) VALUES(?,?,?)',
                           (digest, len(content), content))
        existing = connection.execute('SELECT size,content FROM web_content_blobs WHERE sha256=?', (digest,)).fetchone()
        if existing['size'] != len(content) or bytes(existing['content']) != content:
            raise KitError('文件内容摘要冲突，已停止存储迁移')
        connection.execute(f'INSERT INTO {refs}({",".join(dest)}) VALUES({",".join("?" for _ in dest)})',
                           [row[name] for name in metadata] + [digest])
        connection.execute('UPDATE web_content_blobs SET ref_count=ref_count+1 WHERE sha256=?', (digest,))
    connection.execute(f'DROP TABLE {table}')
    selection = ','.join('b.content AS content' if name == 'content' else 'r.' + name for name in names)
    connection.execute(f'CREATE VIEW {table} AS SELECT {selection} FROM {refs} r JOIN web_content_blobs b ON b.sha256=r.blob_sha256')
    connection.execute('INSERT INTO web_content_tables(table_name) VALUES(?)', (table,))
    # Count changes on the real reference table. This also handles SQL callers
    # using INSERT OR IGNORE on the compatibility view without leaking counts.
    connection.execute(f'''CREATE TRIGGER {refs}_add AFTER INSERT ON {refs} BEGIN
      UPDATE web_content_blobs SET ref_count=ref_count+1 WHERE sha256=NEW.blob_sha256; END''')
    connection.execute(f'''CREATE TRIGGER {refs}_remove AFTER DELETE ON {refs} BEGIN
      UPDATE web_content_blobs SET ref_count=ref_count-1 WHERE sha256=OLD.blob_sha256;
      DELETE FROM web_content_blobs WHERE sha256=OLD.blob_sha256 AND ref_count=0; END''')
    connection.execute(f'''CREATE TRIGGER {refs}_change AFTER UPDATE OF blob_sha256 ON {refs}
      WHEN NEW.blob_sha256 != OLD.blob_sha256 BEGIN
      UPDATE web_content_blobs SET ref_count=ref_count+1 WHERE sha256=NEW.blob_sha256;
      UPDATE web_content_blobs SET ref_count=ref_count-1 WHERE sha256=OLD.blob_sha256;
      DELETE FROM web_content_blobs WHERE sha256=OLD.blob_sha256 AND ref_count=0; END''')
    body = '''INSERT OR IGNORE INTO web_content_blobs(sha256,size,content)
        VALUES(rk_content_sha256(NEW.content),length(NEW.content),NEW.content);
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM web_content_blobs
        WHERE sha256=rk_content_sha256(NEW.content) AND content=NEW.content)
        THEN RAISE(ABORT,'content hash conflict') END;'''
    values = ','.join('NEW.' + name for name in metadata) + ',rk_content_sha256(NEW.content)'
    connection.execute(f'''CREATE TRIGGER {table}_insert INSTEAD OF INSERT ON {table} BEGIN
      {body} INSERT INTO {refs}({','.join(dest)}) VALUES({values});
      DELETE FROM web_content_blobs WHERE sha256=rk_content_sha256(NEW.content) AND ref_count=0; END''')
    where = ' AND '.join(f'{name}=OLD.{name}' for name in keys)
    connection.execute(f'''CREATE TRIGGER {table}_delete INSTEAD OF DELETE ON {table} BEGIN
      DELETE FROM {refs} WHERE {where}; END''')
    assignments = ','.join(f'{name}=NEW.{name}' for name in metadata)
    connection.execute(f'''CREATE TRIGGER {table}_update INSTEAD OF UPDATE ON {table} BEGIN
      {body} UPDATE {refs} SET {assignments},blob_sha256=rk_content_sha256(NEW.content) WHERE {where}; END''')


def capacity(store):
    with store._connection() as con:
        initialize(con)
        row = con.execute('''SELECT COALESCE(SUM(size*ref_count),0) logical,
            COALESCE(SUM(size),0) unique_bytes,COALESCE(SUM(ref_count),0) refs,
            COUNT(*) files FROM web_content_blobs''').fetchone()
        page_count = con.execute('PRAGMA page_count').fetchone()[0]
        page_size = con.execute('PRAGMA page_size').fetchone()[0]
        free_pages = con.execute('PRAGMA freelist_count').fetchone()[0]
    return {'logical_bytes': row['logical'], 'unique_bytes': row['unique_bytes'],
            'saved_bytes': row['logical'] - row['unique_bytes'],
            'file_references': row['refs'], 'unique_files': row['files'],
            'database_bytes': page_count * page_size, 'reclaimable_bytes': free_pages * page_size}
