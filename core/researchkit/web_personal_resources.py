"""Personal project records and explicit resource versions; never infer by name."""
from __future__ import annotations

import sqlite3
import yaml

from .files import KitError, reference
from .web_store import _principal


def project_jobs(store, principal, project_id):
    store.get_project(project_id, principal)  # always owner-only, including LY
    from .web_jobs import Jobs
    jobs = Jobs(store)
    columns = ('run_id,submission_id,content_sha256,owner_uid,owner_username,requester_uid,requester_username,'
               'operation,created_at,started_at,finished_at,status,worker_instance,execution_uid,exit_code,'
               'reason,cancel_requested,log_truncated')
    with store._connection() as con:
        prep = con.execute("SELECT 1 FROM sqlite_master WHERE name='web_submission_preparations'").fetchone()
        extra = ('LEFT JOIN web_submission_preparations p ON p.snapshot_id=j.submission_id ' if prep else '')
        where = '(s.project_id=? OR p.source_project_id=?)' if prep else 's.project_id=?'
        values = [project_id, project_id] if prep else [project_id]
        query = 'SELECT ' + ','.join('j.' + name for name in columns.split(','))
        query += (',p.preparation_id,p.final_submission_id' if prep else ',NULL preparation_id,NULL final_submission_id')
        query += ' FROM web_jobs j JOIN submissions s ON s.id=j.submission_id ' + extra
        query += ' WHERE ' + where + ' AND j.owner_uid=? AND j.owner_username=? ORDER BY j.created_at DESC,j.run_id DESC LIMIT 200'
        rows = con.execute(query, [*values, principal.uid, principal.username]).fetchall()
    return [{**jobs._job(row), 'project_id': project_id, 'preparation_id': row['preparation_id'],
             'final_submission_id': row['final_submission_id']} for row in rows]


def enrich_versions(store, items):
    with store._connection() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='web_dataset_versions'").fetchone():
            return [dict(row) for row in items]
        versions = {row['dataset_id']: dict(row) for row in con.execute('SELECT * FROM web_dataset_versions')}
    return [{**row, **({key: versions[row['dataset_id']][key] for key in ('family_id', 'version_label', 'parent_dataset_id')}
                      if row['dataset_id'] in versions else {})}
            for row in items]


def relate_dataset(store, principal, payload, catalog):
    if _principal(principal)[2] != 'teacher':
        raise KitError('只有教师可以管理数据版本')
    if type(payload) is not dict or set(payload) != {'dataset_id', 'parent_dataset_id', 'version_label'}:
        raise KitError('数据版本字段无效')
    dataset, parent, label = (payload[key] for key in ('dataset_id', 'parent_dataset_id', 'version_label'))
    if any(not isinstance(value, str) or not value.strip() or len(value.encode()) > 256 for value in (dataset, parent, label)):
        raise KitError('数据版本标识及版本名称不能为空或过长')
    if any(ord(char) < 32 for char in label) or len(label.encode()) > 64:
        raise KitError('版本名称不能含控制字符且最多64字节')
    label = label.strip()
    available = {row['dataset_id'] for row in catalog}
    if dataset == parent or dataset not in available or parent not in available:
        raise KitError('请选择两个不同的、已登记且当前可用的数据版本')
    with store._connection() as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS web_dataset_versions (
                dataset_id TEXT PRIMARY KEY, family_id TEXT NOT NULL, parent_dataset_id TEXT,
                version_label TEXT NOT NULL, UNIQUE(family_id,version_label))''')
            origin = con.execute('SELECT * FROM web_dataset_versions WHERE dataset_id=?', (parent,)).fetchone()
            if origin is None:
                con.execute('INSERT INTO web_dataset_versions VALUES(?,?,NULL,?)', (parent, parent, '初始版本'))
                family = parent
            else:
                family = origin['family_id']
            existing = con.execute('SELECT * FROM web_dataset_versions WHERE dataset_id=?', (dataset,)).fetchone()
            if existing is not None:
                if (existing['family_id'], existing['parent_dataset_id'], existing['version_label']) != (family, parent, label):
                    raise KitError('这个数据版本已经关联，不能改写既有版本关系')
            else:
                con.execute('INSERT INTO web_dataset_versions VALUES(?,?,?,?)', (dataset, family, parent, label))
            con.execute('COMMIT')
        except sqlite3.IntegrityError as exc:
            con.execute('ROLLBACK')
            raise KitError('同一数据集已有这个版本名称，请使用不同版本名称') from exc
        except Exception:
            if con.in_transaction:
                con.execute('ROLLBACK')
            raise
    return {'dataset_id': dataset, 'family_id': family, 'parent_dataset_id': parent, 'version_label': label}


def baseline_tools(server, principal, source, existing):
    """Offer the fixed case's embedded tools without publishing or executing them."""
    from .web_assets import AssetService
    files = source['files']
    try:
        document = yaml.safe_load(files.get('project.yaml', b'')) or {}
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as exc:
        raise KitError('基础案例项目说明无效') from exc
    if not isinstance(document, dict) or not isinstance(document.get('plugins', {}), dict):
        raise KitError('基础案例算法绑定无效')
    result = {row['ref']: row for row in existing}
    # Use the same inert extraction and contract validation as asset review.
    assets = AssetService(server.store, server.policy.code_root / 'library')
    for alias, config in document.get('plugins', {}).items():
        if not isinstance(config, dict) or not isinstance(config.get('path'), str):
            raise KitError('基础案例算法绑定无效')
        metadata = yaml.safe_load(files.get(config['path'].rstrip('/') + '/plugin.yaml', b'')) or {}
        if not isinstance(metadata, dict):
            raise KitError('基础案例算法说明无效')
        name, version = metadata.get('name'), metadata.get('version')
        ref = str(name) + '@' + str(version)
        reference(ref)
        packed, parsed, _validation, _refs = assets._algorithm(files, name, version, alias)
        # Keep the exact original YAML; _algorithm already validated it.
        packed['plugin/plugin.yaml'] = files[config['path'].rstrip('/') + '/plugin.yaml']
        material = {'ref': ref, 'metadata': {**parsed, 'source': 'baseline'}, 'files': packed}
        if ref in result and result[ref]['files'] != packed:
            raise KitError('同一算法版本的案例内容与附加工具不同，请先修正算法版本')
        result.setdefault(ref, material)
    return list(result.values())
