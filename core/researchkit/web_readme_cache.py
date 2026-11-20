"""Bounded README bodies supplied by the trusted publisher, fetched on demand.

Keep the existing small discovery/catalog descriptions. Full bodies are sent
one at a time while scanning, rather than multiplying the scan RPC or metadata
file size. No browser-supplied filesystem path is ever opened here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from .files import KitError
from .web_dataset_readme import MAX_README, _frontmatter

_REF = re.compile(r'README:(?i:readme\.md)#sha256=[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{24}\Z')


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS data_management_readme_bodies (
        candidate_id TEXT NOT NULL, provenance_ref TEXT NOT NULL,
        original TEXT NOT NULL,
        PRIMARY KEY(candidate_id,provenance_ref))''')
    connection.execute('CREATE INDEX IF NOT EXISTS data_management_readme_source ON data_management_readme_bodies(provenance_ref)')


def put(manager, *, job_id, lease_token, candidate_id, provenance_ref, original):
    if (not isinstance(job_id, str) or not 0 < len(job_id) <= 128 or
            not isinstance(lease_token, str) or not 0 < len(lease_token) <= 128 or
            not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id) or
            not isinstance(provenance_ref, str) or not _REF.fullmatch(provenance_ref) or
            not isinstance(original, str)):
        raise KitError('README 正文回传字段无效')
    try:
        raw = original.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise KitError('README 正文必须使用 UTF-8') from exc
    if len(raw) > MAX_README:
        raise KitError('README 正文超过 1 MiB 上限')
    digest = hashlib.sha256(raw).hexdigest()
    if digest != provenance_ref.rsplit('=', 1)[1]:
        raise KitError('README 来源与内容不一致')
    _frontmatter(original.removeprefix('\ufeff'))
    with manager._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            job = connection.execute('SELECT * FROM data_management_jobs WHERE job_id=?', (job_id,)).fetchone()
            if (job is None or job['kind'] != 'scan' or job['status'] != 'running' or
                    job['lease_token'] != lease_token or (job['lease_expires_at'] or 0) < time.time()):
                raise KitError('README 正文回传的扫描租约无效或已过期')
            connection.execute('INSERT OR IGNORE INTO data_management_readme_bodies VALUES(?,?,?)',
                               (candidate_id, provenance_ref, original))
            # Preserve the current candidate's exact old body during a scan or
            # a failed scan. Drop intermediate bodies rather than accumulating
            # every edit to a README forever.
            candidate = connection.execute('SELECT original_metadata_json FROM data_management_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
            current = json.loads(candidate['original_metadata_json']).get('provenance_ref', '') if candidate else ''
            connection.execute('DELETE FROM data_management_readme_bodies WHERE candidate_id=? AND provenance_ref NOT IN (?,?)',
                               (candidate_id, provenance_ref, current))
            connection.execute('COMMIT')
        except Exception:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
    return {'stored': True}


def get(store, provenance_ref, fallback='', *, candidate_id=None):
    """Caller must already authorize the candidate or the exact dataset."""
    result = {'markdown': fallback, 'source_ref': provenance_ref,
              'complete': '[README 较长，网页仅显示前 16 KiB' not in fallback}
    if not isinstance(provenance_ref, str) or not _REF.fullmatch(provenance_ref):
        return result
    with store._connection() as connection:
        try:
            query = 'SELECT original FROM data_management_readme_bodies WHERE provenance_ref=?'
            params = [provenance_ref]
            if candidate_id is not None:
                query += ' AND candidate_id=?'; params.append(candidate_id)
            row = connection.execute(query + ' LIMIT 1', params).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            row = None
    if row is not None:
        raw = row['original'].encode('utf-8')
        if len(raw) > MAX_README or hashlib.sha256(raw).hexdigest() != provenance_ref.rsplit('=', 1)[1]:
            raise KitError('README 正文缓存校验失败，请刷新扫描')
        result.update(markdown=_frontmatter(row['original'].removeprefix('\ufeff'))[0], original=row['original'], complete=True)
    if not result['complete']:
        result['notice'] = '尚未补齐完整正文。教师刷新扫描后，可在此重试查看；数据内容和授权不受影响。'
    return result
