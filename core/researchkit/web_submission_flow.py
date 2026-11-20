"""Private immutable checks followed by atomic, evidence-bound formal submission."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import yaml

from .files import KitError
from .web_identity import Principal
from .web_jobs import Jobs
from .web_store import _json, _principal, _record_id, _timestamp, _validate_files, _validate_data_references


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_CASE_VERSION = re.compile(r'\d+\.\d+\Z')


class SubmissionFlow:
    def __init__(self, store, library_root=None):
        self.store, self.library_root = store, library_root
        self.jobs = Jobs(store)
        with store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('''CREATE TABLE IF NOT EXISTS web_submission_preparations (
                    preparation_id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL, owner_username TEXT NOT NULL,
                    source_project_id TEXT NOT NULL REFERENCES projects(id), expected_parent_id TEXT,
                    kind TEXT NOT NULL, case_version TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL, snapshot_id TEXT UNIQUE,
                    check_run_id TEXT, run_run_id TEXT, final_submission_id TEXT,
                    status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'formal',
                    snapshot_sha256 TEXT NOT NULL DEFAULT '', reused_preparation_id TEXT,
                    UNIQUE(owner_uid,owner_username,request_id))''')
                columns = {row['name'] for row in con.execute('PRAGMA table_info(web_submission_preparations)')}
                # Existing installations already have receipts from the formal
                # submission flow.  Additive columns keep those receipts valid
                # while distinguishing newly introduced project trials.
                for name, declaration in (
                    ('mode', "TEXT NOT NULL DEFAULT 'formal'"),
                    ('snapshot_sha256', "TEXT NOT NULL DEFAULT ''"),
                    ('reused_preparation_id', 'TEXT'),
                ):
                    if name not in columns:
                        con.execute(f'ALTER TABLE web_submission_preparations ADD COLUMN {name} {declaration}')
                con.execute('CREATE INDEX IF NOT EXISTS web_submission_preparations_owner ON web_submission_preparations(owner_uid,owner_username,created_at)')
                con.execute('CREATE INDEX IF NOT EXISTS web_submission_preparations_trial_lookup ON web_submission_preparations(owner_uid,owner_username,source_project_id,mode,status,snapshot_sha256,created_at)')
                con.execute('''CREATE TABLE IF NOT EXISTS web_direct_submission_receipts (
                    receipt_id TEXT PRIMARY KEY, owner_uid INTEGER NOT NULL, owner_username TEXT NOT NULL,
                    source_project_id TEXT NOT NULL REFERENCES projects(id), expected_parent_id TEXT,
                    kind TEXT NOT NULL, case_version TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    final_submission_id TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner_uid,owner_username,request_id))''')
                con.execute('CREATE INDEX IF NOT EXISTS web_direct_submission_receipts_owner ON web_direct_submission_receipts(owner_uid,owner_username,created_at)')
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    @staticmethod
    def _case_version(plan, selected, kind):
        """Check a case label before an internal check job is queued."""
        if not isinstance(selected, str) or not _CASE_VERSION.fullmatch(selected):
            raise KitError('案例版本格式应为主版本.小版本')
        major, minor = (int(part) for part in selected.split('.'))
        if max(major, minor) > 1_000_000:
            raise KitError('案例版本格式应为主版本.小版本')
        case = plan.get('case') if isinstance(plan, dict) else None
        if not isinstance(case, dict) or not isinstance(case.get('changed'), bool):
            raise KitError('案例版本比较结果无效')
        previous = case.get('current_version')
        if previous is None:
            expected = '1.0' if kind == 'major' else '0.1'
            if selected != expected:
                raise KitError(f'首次{kind}提交案例版本应为 {expected}')
        else:
            if not isinstance(previous, str) or not _CASE_VERSION.fullmatch(previous):
                raise KitError('案例版本比较结果无效')
            old = tuple(int(part) for part in previous.split('.'))
            if case['changed'] and (major, minor) <= old:
                raise KitError('案例内容已变化，版本必须高于原提交')
            if not case['changed'] and selected != previous:
                raise KitError('案例内容未变化，应复用原案例版本')
        return selected

    def _project(self, principal, project_id):
        project = self.store.get_project(project_id, principal)
        if (project['owner_uid'], project['owner_username']) != _principal(principal)[:2]:
            raise KitError('只能准备自己的项目提交')
        return project

    def comparison(self, principal, project_id, files):
        self._project(principal, project_id)
        with self.store._connection() as con:
            last = con.execute('SELECT id,version_major,version_minor FROM submissions WHERE project_id=? ORDER BY sequence DESC LIMIT 1', (project_id,)).fetchone()
            prior = None if last is None else self.store._source_files(con, last['id'])[1]
            trusted = con.execute('SELECT baseline_submission_id FROM project_trusted_sources WHERE project_id=?', (project_id,)).fetchone()
            baseline = None if trusted is None else self.store._source_files(con, trusted['baseline_submission_id'])[1]
        if baseline is None and prior is None:
            try:
                document = yaml.safe_load(files['project.yaml'].decode())
                origin = document.get('origin', {})
                ref = origin.get('template_ref') if isinstance(origin, dict) else None
                if ref and self.library_root is not None:
                    from .web_sharing import ComponentService
                    baseline = ComponentService(self.store, self.library_root).template(principal, ref)['files']
            except (ValueError, KeyError, yaml.YAMLError, KitError):
                baseline = None
        version = None if last is None else f"{last['version_major']}.{last['version_minor']}"
        return prior, baseline, version, None if last is None else last['id']

    def preview(self, principal, project_id, files, kind='minor'):
        from .web_versions import preview_versions
        packed, _ = _validate_files(files)
        prior, baseline, version, parent = self.comparison(principal, project_id, packed)
        result = preview_versions(packed, prior, baseline, version, kind)
        result['expected_parent_id'] = parent
        return result

    def _comparison_in_connection(self, con, principal, project_id, files, *, template_baseline=None):
        last = con.execute('SELECT id,version_major,version_minor FROM submissions WHERE project_id=? ORDER BY sequence DESC LIMIT 1',
                           (project_id,)).fetchone()
        prior = None if last is None else self.store._source_files(con, last['id'])[1]
        trusted = con.execute('SELECT baseline_submission_id FROM project_trusted_sources WHERE project_id=?',
                              (project_id,)).fetchone()
        baseline = None if trusted is None else self.store._source_files(con, trusted['baseline_submission_id'])[1]
        if baseline is None and prior is None:
            try:
                document = yaml.safe_load(files['project.yaml'].decode())
                origin = document.get('origin', {})
                ref = origin.get('template_ref') if isinstance(origin, dict) else None
                if ref and template_baseline is not None:
                    # Only a version-comparison baseline, not a resource grant
                    # or a source of submitted bytes. Load this immutable copy
                    # before the writer lock: template/lifecycle constructors
                    # may run lazy migrations on their own connections.
                    baseline = template_baseline
            except (ValueError, KeyError, yaml.YAMLError, KitError):
                baseline = None
        version = None if last is None else f"{last['version_major']}.{last['version_minor']}"
        return prior, baseline, version, None if last is None else last['id']

    def find_direct(self, principal, project_id, request_id, request_fingerprint):
        self._project(principal, project_id)
        uid, username, _ = _principal(principal)
        request_id = _record_id(request_id, label='提交请求ID')
        if not isinstance(request_fingerprint, str) or not _HASH.fullmatch(request_fingerprint):
            raise KitError('提交请求摘要无效')
        with self.store._connection() as con:
            row = con.execute('''SELECT * FROM web_direct_submission_receipts
                WHERE owner_uid=? AND owner_username=? AND request_id=?''',
                              (uid, username, request_id)).fetchone()
        if row is None:
            return None
        if row['source_project_id'] != project_id or row['request_fingerprint'] != request_fingerprint:
            raise KitError('提交请求ID已用于不同的内容，请重新预览')
        return self._direct_view(row)

    def _direct_view(self, row):
        return {
            'receipt_id': row['receipt_id'], 'preparation_id': row['receipt_id'],
            'mode': 'direct', 'project_id': row['source_project_id'],
            'source_project_id': row['source_project_id'], 'submission_id': row['final_submission_id'],
            'final_submission_id': row['final_submission_id'], 'status': row['status'],
            'reason': '', 'case_version': row['case_version'], 'content_sha256': row['content_sha256'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
        }

    def get_direct(self, principal, receipt_id):
        uid, username, _ = _principal(principal)
        receipt_id = _record_id(receipt_id, label='直接提交回执ID')
        with self.store._connection() as con:
            row = con.execute('''SELECT * FROM web_direct_submission_receipts
                WHERE receipt_id=? AND owner_uid=? AND owner_username=?''',
                              (receipt_id, uid, username)).fetchone()
        if row is None:
            raise KitError('无权查看直接提交回执')
        return self._direct_view(row)

    def direct(self, principal, project_id, *, files, data_references, kind, case_version,
               request_id, request_fingerprint):
        """Atomically create an immutable user-selected version without running code."""
        from .web_versions import preview_versions, apply_versions
        uid, username, _ = _principal(principal)
        known = self.find_direct(principal, project_id, request_id, request_fingerprint)
        if known is not None:
            return known
        if kind not in {'major', 'minor'}:
            raise KitError('提交类型无效')
        if not isinstance(request_fingerprint, str) or not _HASH.fullmatch(request_fingerprint):
            raise KitError('提交请求摘要无效')
        packed, manifest_files = _validate_files(files)
        refs = _validate_data_references(data_references)
        manifest, content_hash = self._manifest(packed, manifest_files, refs)
        template_baseline = None
        if self.library_root is not None:
            try:
                document = yaml.safe_load(packed.get('project.yaml', b''))
                origin = document.get('origin', {}) if isinstance(document, dict) else {}
                if isinstance(origin, dict) and origin.get('template_ref'):
                    from .web_sharing import ComponentService
                    template_baseline = ComponentService(self.store, self.library_root).template(
                        principal, origin['template_ref'])['files']
            except (ValueError, yaml.YAMLError, KitError):
                # As in preview, a no-longer-available template must not
                # prevent the owner from submitting their own code copy.
                template_baseline = None
        receipt_id = str(uuid.uuid4())
        now = _timestamp()
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                existing = con.execute('''SELECT * FROM web_direct_submission_receipts
                    WHERE owner_uid=? AND owner_username=? AND request_id=?''',
                                       (uid, username, request_id)).fetchone()
                if existing is not None:
                    if (existing['source_project_id'] != project_id
                            or existing['request_fingerprint'] != request_fingerprint):
                        raise KitError('提交请求ID已用于不同的内容')
                    con.execute('COMMIT')
                    return self._direct_view(existing)
                # Revalidate the exact submitted material in the transaction;
                # never turn a browser-side version preview into authority.
                verified, verified_manifest = _validate_files(files)
                verified_refs = _validate_data_references(data_references)
                verified_manifest_obj, verified_hash = self._manifest(verified, verified_manifest, verified_refs)
                if verified != packed or verified_refs != refs or verified_hash != content_hash:
                    raise KitError('固定提交内容在写入前发生变化，请重新预览')
                project = con.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
                if project is None or self.store._internal_project(con, project_id):
                    raise KitError('项目不存在或不能直接提交')
                if (project['owner_uid'], project['owner_username']) != (uid, username):
                    raise KitError('只能提交自己的项目')
                prior, baseline, version, parent = self._comparison_in_connection(
                    con, principal, project_id, packed, template_baseline=template_baseline)
                plan = preview_versions(packed, prior, baseline, version, kind)
                selected_case_version = self._case_version(plan, case_version, kind)
                choices = {'case_version': selected_case_version,
                           'algorithm_versions': {item['alias']: item['current_version'] for item in plan['algorithms']}}
                if apply_versions(packed, plan, choices) != packed:
                    raise KitError('算法版本配置尚未保存，请重新预览并确认版本')
                from .web_cleanup import require_available
                for reference in refs:
                    require_available(con, 'dataset', reference['dataset_id'])
                if parent is not None and con.execute('SELECT id FROM submissions WHERE id=?', (parent,)).fetchone() is None:
                    raise KitError('项目已出现新的提交，请重新预览版本')
                previous = con.execute('SELECT * FROM submissions WHERE project_id=? ORDER BY sequence DESC LIMIT 1',
                                        (project_id,)).fetchone()
                current_parent = None if previous is None else previous['id']
                if current_parent != parent:
                    raise KitError('项目已出现新的提交，请重新预览版本')
                if previous is not None and previous['content_sha256'] == content_hash:
                    final_id, status = previous['id'], 'unchanged'
                else:
                    major, minor = map(int, selected_case_version.split('.'))
                    if previous is not None and (major, minor) <= (previous['version_major'], previous['version_minor']):
                        raise KitError('案例内容已变化，版本必须高于原提交')
                    final_id, status = str(uuid.uuid4()), 'succeeded'
                    con.execute('''INSERT INTO submissions(id,project_id,sequence,version_major,version_minor,kind,parent_submission_id,content_sha256,manifest_json,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',
                                (final_id, project_id, 1 if previous is None else previous['sequence'] + 1,
                                 major, minor, kind, parent, content_hash, _json(verified_manifest_obj), now))
                    con.executemany('INSERT INTO submission_files(submission_id,path,sha256,size,content) VALUES(?,?,?,?,?)',
                                    [(final_id, item['path'], item['sha256'], item['size'], packed[item['path']])
                                     for item in manifest_files])
                con.execute('''INSERT INTO web_direct_submission_receipts(
                    receipt_id,owner_uid,owner_username,source_project_id,expected_parent_id,kind,case_version,
                    request_id,request_fingerprint,content_sha256,final_submission_id,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (receipt_id, uid, username, project_id, parent, kind, selected_case_version,
                             request_id, request_fingerprint, content_hash, final_id, status, now, now))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        with self.store._connection() as con:
            row = con.execute('SELECT * FROM web_direct_submission_receipts WHERE receipt_id=?',
                              (receipt_id,)).fetchone()
        return self._direct_view(row)

    def find(self, principal, project_id, request_id, request_fingerprint):
        """Find an idempotent formal-submission receipt only."""
        return self._find(principal, project_id, request_id, request_fingerprint, mode='formal')

    def find_trial(self, principal, project_id, request_id, request_fingerprint):
        """Find an idempotent project-trial receipt only."""
        return self._find(principal, project_id, request_id, request_fingerprint, mode='trial')

    def _find(self, principal, project_id, request_id, request_fingerprint, *, mode):
        self._project(principal, project_id)
        uid, username, _ = _principal(principal)
        request_id = _record_id(request_id, label='提交请求ID')
        with self.store._connection() as con:
            row = con.execute('SELECT * FROM web_submission_preparations WHERE owner_uid=? AND owner_username=? AND request_id=?', (uid,username,request_id)).fetchone()
        if row is None:
            return None
        if (row['source_project_id'] != project_id or row['request_fingerprint'] != request_fingerprint
                or row['mode'] != mode):
            raise KitError('提交请求ID已用于不同的内容，请重新预览')
        return self.get(principal, row['preparation_id'])

    @staticmethod
    def _manifest(files, manifest_files, refs):
        manifest = {'schema':'researchkit.web-submission/v1',
                    'snapshot':'all listed small files are stored in the submission database',
                    'files':manifest_files,
                    'data_references':{'status':'unverified','references':refs,
                      'snapshot':'external data is referenced only and was not traversed or verified'}}
        return manifest, hashlib.sha256(_json(manifest).encode()).hexdigest()

    @staticmethod
    def _insert_snapshot(con, *, snapshot_id, internal_project, principal, project, manifest_files,
                         files, manifest, content_hash, now):
        uid, username, _ = _principal(principal)
        con.execute('INSERT INTO projects(id,owner_uid,owner_username,source,name,created_at) VALUES(?,?,?,?,?,?)',
                    (internal_project,uid,username,'self-created',project['name'],now))
        con.execute("INSERT INTO submissions(id,project_id,sequence,version_major,version_minor,kind,parent_submission_id,content_sha256,manifest_json,created_at) VALUES(?,?,1,0,0,'minor',NULL,?,?,?)",
                    (snapshot_id,internal_project,content_hash,_json(manifest),now))
        con.executemany('INSERT INTO submission_files(submission_id,path,sha256,size,content) VALUES(?,?,?,?,?)',
                        [(snapshot_id,item['path'],item['sha256'],item['size'],files[item['path']])
                         for item in manifest_files])

    @staticmethod
    def _ready_trial(con, *, uid, username, project_id, content_hash):
        """Return a retained, exact successful trial owned by this project only."""
        row = con.execute('''SELECT p.* FROM web_submission_preparations p
            JOIN submissions s ON s.id=p.snapshot_id
            WHERE p.owner_uid=? AND p.owner_username=? AND p.source_project_id=?
              AND p.mode='trial' AND p.status='ready' AND p.snapshot_sha256=?
              AND s.content_sha256=?
            ORDER BY p.created_at DESC LIMIT 1''',
                          (uid, username, project_id, content_hash, content_hash)).fetchone()
        if row is None:
            return None
        for column in ('check_run_id', 'run_run_id'):
            job = con.execute('SELECT status,exit_code,content_sha256 FROM web_jobs WHERE run_id=?',
                              (row[column],)).fetchone()
            if (job is None or job['status'] != 'succeeded' or job['exit_code'] != 0
                    or job['content_sha256'] != content_hash):
                return None
        return row

    def prepare(self, principal, project_id, *, files, data_references, kind, case_version,
                request_id, request_fingerprint):
        from .web_versions import preview_versions, apply_versions
        uid, username, _ = _principal(principal)
        known = self.find(principal, project_id, request_id, request_fingerprint)
        if known is not None:
            return known
        if kind not in {'major','minor'}:
            raise KitError('提交类型无效')
        if not isinstance(request_fingerprint,str) or not _HASH.fullmatch(request_fingerprint):
            raise KitError('提交请求摘要无效')
        packed, manifest_files = _validate_files(files)
        refs = _validate_data_references(data_references)
        prior, baseline, version, parent = self.comparison(principal,project_id,packed)
        plan = preview_versions(packed,prior,baseline,version,kind)
        case_version = self._case_version(plan, case_version, kind)
        choices = {'case_version':case_version, 'algorithm_versions':{item['alias']:item['current_version'] for item in plan['algorithms']}}
        validated = apply_versions(packed,plan,choices)
        if validated != packed:
            raise KitError('算法版本配置尚未保存，请重新预览并确认版本')
        project = self._project(principal,project_id)
        manifest, content_hash = self._manifest(packed, manifest_files, refs)
        prep_id, snapshot_id, internal_project = (str(uuid.uuid4()) for _ in range(3))
        now = _timestamp()
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                existing = con.execute('SELECT * FROM web_submission_preparations WHERE owner_uid=? AND owner_username=? AND request_id=?',(uid,username,request_id)).fetchone()
                if existing is not None:
                    if (existing['request_fingerprint'] != request_fingerprint
                            or existing['source_project_id'] != project_id
                            or existing['mode'] != 'formal'):
                        raise KitError('提交请求ID已用于不同的内容')
                    con.execute('COMMIT')
                    return self.get(principal,existing['preparation_id'])
                current = con.execute('SELECT id FROM submissions WHERE project_id=? ORDER BY sequence DESC LIMIT 1',(project_id,)).fetchone()
                if (None if current is None else current['id']) != parent:
                    raise KitError('项目已出现新的提交，请重新预览版本')
                active=con.execute("SELECT count(*) FROM web_submission_preparations WHERE owner_uid=? AND owner_username=? AND status IN ('checking','running')",(uid,username)).fetchone()[0]
                if active>=4:raise KitError('正在验证的提交已达上限4，请等待完成')
                from .web_cleanup import require_available
                for reference in refs:
                    require_available(con,'dataset',reference['dataset_id'])
                self._insert_snapshot(con, snapshot_id=snapshot_id, internal_project=internal_project,
                                      principal=principal, project=project, manifest_files=manifest_files,
                                      files=packed, manifest=manifest, content_hash=content_hash, now=now)
                reused = self._ready_trial(con, uid=uid, username=username, project_id=project_id,
                                           content_hash=content_hash)
                if reused is not None:
                    con.execute('''INSERT INTO web_submission_preparations(
                        preparation_id,owner_uid,owner_username,source_project_id,expected_parent_id,
                        kind,case_version,request_id,request_fingerprint,snapshot_id,check_run_id,run_run_id,
                        status,created_at,updated_at,mode,snapshot_sha256,reused_preparation_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                (prep_id,uid,username,project_id,parent,kind,case_version,request_id,
                                 request_fingerprint,snapshot_id,reused['check_run_id'],reused['run_run_id'],
                                 'ready',now,now,'formal',content_hash,reused['preparation_id']))
                else:
                    con.execute('''INSERT INTO web_submission_preparations(
                        preparation_id,owner_uid,owner_username,source_project_id,expected_parent_id,
                        kind,case_version,request_id,request_fingerprint,snapshot_id,status,created_at,
                        updated_at,mode,snapshot_sha256)
                        VALUES(?,?,?,?,?,?,?,?,?,?,'checking',?,?,?,?)''',
                                (prep_id,uid,username,project_id,parent,kind,case_version,request_id,
                                 request_fingerprint,snapshot_id,now,now,'formal',content_hash))
                    job=self.jobs.queue_internal(con,principal,snapshot_id,content_hash,'check')
                    con.execute('UPDATE web_submission_preparations SET check_run_id=? WHERE preparation_id=?',(job['run_id'],prep_id))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:con.execute('ROLLBACK')
                raise
        return self.get(principal,prep_id)

    def prepare_trial(self, principal, project_id, *, files, data_references, request_id,
                      request_fingerprint):
        """Queue a private check/run pair without making a formal submission.

        The immutable snapshot deliberately remains after a successful run.  A
        later formal submission can reuse those exact bytes, but never a
        merely similar mutable workspace state.
        """
        uid, username, _ = _principal(principal)
        known = self.find_trial(principal, project_id, request_id, request_fingerprint)
        if known is not None:
            return known
        if not isinstance(request_fingerprint, str) or not _HASH.fullmatch(request_fingerprint):
            raise KitError('项目试运行请求摘要无效')
        packed, manifest_files = _validate_files(files)
        refs = _validate_data_references(data_references)
        project = self._project(principal, project_id)
        manifest, content_hash = self._manifest(packed, manifest_files, refs)
        prep_id, snapshot_id, internal_project = (str(uuid.uuid4()) for _ in range(3))
        now = _timestamp()
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                existing = con.execute('SELECT * FROM web_submission_preparations WHERE owner_uid=? AND owner_username=? AND request_id=?',
                                       (uid, username, request_id)).fetchone()
                if existing is not None:
                    if (existing['request_fingerprint'] != request_fingerprint
                            or existing['source_project_id'] != project_id or existing['mode'] != 'trial'):
                        raise KitError('项目试运行请求ID已用于不同的内容')
                    con.execute('COMMIT')
                    return self.get(principal, existing['preparation_id'])
                active = con.execute("SELECT count(*) FROM web_submission_preparations WHERE owner_uid=? AND owner_username=? AND status IN ('checking','running')",
                                     (uid, username)).fetchone()[0]
                if active >= 4:
                    raise KitError('正在验证的项目已达上限4，请等待完成')
                from .web_cleanup import require_available
                for reference in refs:
                    require_available(con, 'dataset', reference['dataset_id'])
                self._insert_snapshot(con, snapshot_id=snapshot_id, internal_project=internal_project,
                                      principal=principal, project=project, manifest_files=manifest_files,
                                      files=packed, manifest=manifest, content_hash=content_hash, now=now)
                con.execute('''INSERT INTO web_submission_preparations(
                    preparation_id,owner_uid,owner_username,source_project_id,expected_parent_id,
                    kind,case_version,request_id,request_fingerprint,snapshot_id,status,created_at,
                    updated_at,mode,snapshot_sha256)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'checking',?,?,?,?)''',
                            (prep_id,uid,username,project_id,None,'trial','0.0',request_id,
                             request_fingerprint,snapshot_id,now,now,'trial',content_hash))
                job = self.jobs.queue_internal(con, principal, snapshot_id, content_hash, 'check')
                con.execute('UPDATE web_submission_preparations SET check_run_id=? WHERE preparation_id=?',
                            (job['run_id'], prep_id))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        return self.get(principal, prep_id)

    def _view(self, row, con):
        project = con.execute('SELECT name FROM projects WHERE id=?', (row['source_project_id'],)).fetchone()
        result={key:row[key] for key in ('preparation_id','source_project_id','status','reason','case_version','created_at','updated_at')}
        result['mode'] = row['mode'] if row['mode'] in {'trial', 'formal'} else 'formal'
        result['project_id'] = row['source_project_id']
        result['project_name'] = None if project is None else project['name']
        result['snapshot_sha256'] = row['snapshot_sha256'] or None
        result['reused_preparation_id'] = row['reused_preparation_id']
        result['submission_id']=row['final_submission_id']
        for key,column in [('check_job','check_run_id'),('run_job','run_run_id')]:
            job=None if not row[column] else con.execute('SELECT * FROM web_jobs WHERE run_id=?',(row[column],)).fetchone()
            result[key]=None if job is None else Jobs._job(job, include_log=True)
        return result

    def get(self, principal, preparation_id):
        uid,username,_=_principal(principal)
        preparation_id=_record_id(preparation_id,label='提交验证ID')
        with self.store._connection() as con:
            row=con.execute('SELECT * FROM web_submission_preparations WHERE preparation_id=? AND owner_uid=? AND owner_username=?',(preparation_id,uid,username)).fetchone()
            if row is None:raise KitError('无权查看该提交验证')
        self.advance(preparation_id)
        with self.store._connection() as con:
            row=con.execute('SELECT * FROM web_submission_preparations WHERE preparation_id=?',(preparation_id,)).fetchone()
            return self._view(row,con)

    def advance_snapshot(self,snapshot_id):
        with self.store._connection() as con:
            row=con.execute('SELECT preparation_id FROM web_submission_preparations WHERE snapshot_id=?',(snapshot_id,)).fetchone()
        if row is not None:self.advance(row['preparation_id'])

    def advance(self,preparation_id):
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                row=con.execute('SELECT * FROM web_submission_preparations WHERE preparation_id=?',(preparation_id,)).fetchone()
                if row is None or row['status'] not in {'checking','running','ready'}:
                    con.execute('COMMIT');return
                mode = row['mode'] if row['mode'] in {'trial', 'formal'} else 'formal'
                if row['status'] == 'ready':
                    # `ready` is terminal for a trial.  A formal receipt in
                    # this state instead points at a retained exact trial and
                    # is promoted under the normal parent CAS transaction.
                    if mode == 'trial':
                        con.execute('COMMIT');return
                    source, _ = self.store._source_files(con, row['snapshot_id'])
                    if source['content_sha256'] != row['snapshot_sha256']:
                        raise KitError('复用试运行摘要不一致')
                    reused = None if not row['reused_preparation_id'] else con.execute(
                        'SELECT * FROM web_submission_preparations WHERE preparation_id=?',
                        (row['reused_preparation_id'],)).fetchone()
                    if (reused is None or reused['mode'] != 'trial' or reused['status'] != 'ready'
                            or reused['owner_uid'] != row['owner_uid']
                            or reused['owner_username'] != row['owner_username']
                            or reused['source_project_id'] != row['source_project_id']
                            or reused['snapshot_sha256'] != source['content_sha256']):
                        raise KitError('可复用试运行记录已失效，请重新检查与运行')
                    for column in ('check_run_id', 'run_run_id'):
                        job = con.execute('SELECT status,exit_code,content_sha256 FROM web_jobs WHERE run_id=?',
                                          (reused[column],)).fetchone()
                        if (job is None or job['status'] != 'succeeded' or job['exit_code'] != 0
                                or job['content_sha256'] != source['content_sha256']):
                            raise KitError('可复用试运行记录不完整，请重新检查与运行')
                    self._promote(con,row,source)
                    terminal=con.execute('SELECT status FROM web_submission_preparations WHERE preparation_id=?',(preparation_id,)).fetchone()
                    if terminal['status']=='conflict':
                        con.execute('DELETE FROM submission_files WHERE submission_id=?',(row['snapshot_id'],))
                    con.execute('COMMIT');return
                column='check_run_id' if row['status']=='checking' else 'run_run_id'
                job=con.execute('SELECT * FROM web_jobs WHERE run_id=?',(row[column],)).fetchone()
                if job is None:raise KitError('提交验证作业记录缺失')
                if job['status'] in {'queued','running'}:
                    con.execute('COMMIT');return
                if job['status']!='succeeded' or job['exit_code']!=0:
                    con.execute("UPDATE web_submission_preparations SET status='failed',reason=?,updated_at=? WHERE preparation_id=?",(job['reason'] or '检查或运行未成功，未形成正式提交',_timestamp(),preparation_id))
                    con.execute('DELETE FROM submission_files WHERE submission_id=?',(row['snapshot_id'],))
                    con.execute('COMMIT');return
                source,files=self.store._source_files(con,row['snapshot_id'])
                if job['content_sha256']!=source['content_sha256']:raise KitError('提交验证摘要不一致')
                if row['status']=='checking':
                    actor=Principal(row['owner_uid'],row['owner_username'],'teacher' if row['owner_username']=='ly' else 'student')
                    try:
                        run=self.jobs.queue_internal(con,actor,row['snapshot_id'],source['content_sha256'],'run')
                    except KitError as exc:
                        # Queue capacity is transient.  The check is complete,
                        # so leave this receipt retryable instead of raising
                        # from runner_finish after it has already persisted.
                        con.execute("UPDATE web_submission_preparations SET reason=?,updated_at=? WHERE preparation_id=?",
                                    ('检查已通过，等待运行队列容量：' + str(exc), _timestamp(), preparation_id))
                        con.execute('COMMIT');return
                    con.execute("UPDATE web_submission_preparations SET status='running',run_run_id=?,reason='',updated_at=? WHERE preparation_id=?",(run['run_id'],_timestamp(),preparation_id))
                else:
                    if mode == 'trial':
                        con.execute("UPDATE web_submission_preparations SET status='ready',reason='检查与运行均已成功；可继续编辑或正式提交',updated_at=? WHERE preparation_id=?",
                                    (_timestamp(), preparation_id))
                    else:
                        self._promote(con,row,source)
                        terminal=con.execute('SELECT status FROM web_submission_preparations WHERE preparation_id=?',(preparation_id,)).fetchone()
                        if terminal['status']=='conflict':
                            con.execute('DELETE FROM submission_files WHERE submission_id=?',(row['snapshot_id'],))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:con.execute('ROLLBACK')
                raise

    def _promote(self,con,row,source):
        project=con.execute('SELECT * FROM projects WHERE id=?',(row['source_project_id'],)).fetchone()
        if project is None or (project['owner_uid'],project['owner_username'])!=(row['owner_uid'],row['owner_username']):
            raise KitError('原项目所有者不一致，未正式提交')
        prior=con.execute('SELECT * FROM submissions WHERE project_id=? ORDER BY sequence DESC LIMIT 1',(row['source_project_id'],)).fetchone()
        parent=None if prior is None else prior['id']
        if parent!=row['expected_parent_id']:
            con.execute("UPDATE web_submission_preparations SET status='conflict',reason='验证期间已有新提交，请重新预览',updated_at=? WHERE preparation_id=?",(_timestamp(),row['preparation_id']));return
        if prior is not None and prior['content_sha256']==source['content_sha256']:
            final_id=prior['id'];status='unchanged'
        else:
            # prepare() checked this before creating jobs.  Retain the CAS
            # comparison below because another formal submission may arrive
            # while the private check/run pair is executing.
            major,minor=map(int,str(row['case_version']).split('.'))
            if prior is not None and (major,minor)<=(prior['version_major'],prior['version_minor']):
                con.execute("UPDATE web_submission_preparations SET status='conflict',reason='案例内容已变化，版本必须高于原提交，请重新确认版本',updated_at=? WHERE preparation_id=?",(_timestamp(),row['preparation_id']));return
            final_id=str(uuid.uuid4());status='succeeded'
            con.execute('''INSERT INTO submissions(id,project_id,sequence,version_major,version_minor,kind,parent_submission_id,content_sha256,manifest_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(final_id,row['source_project_id'],1 if prior is None else prior['sequence']+1,major,minor,row['kind'],parent,source['content_sha256'],source['manifest_json'],_timestamp()))
            con.execute('INSERT INTO submission_files(submission_id,path,sha256,size,content) SELECT ?,path,sha256,size,content FROM submission_files WHERE submission_id=?',(final_id,row['snapshot_id']))
        # Promote the already executed jobs, including logs/results, to the exact
        # formal snapshot. No code or parameters change between these identities.
        con.execute('UPDATE web_jobs SET submission_id=? WHERE run_id IN (?,?)',(final_id,row['check_run_id'],row['run_run_id']))
        con.execute('UPDATE web_submission_preparations SET status=?,final_submission_id=?,updated_at=? WHERE preparation_id=?',(status,final_id,_timestamp(),row['preparation_id']))
        # Logs/artifacts now belong to the formal job identities and the
        # receipt stays for audit/retry lookup.  Only release duplicate
        # private snapshot on a successful promotion. Failed/conflicted jobs
        # retain diagnostic metadata and logs, but release their temporary
        # source bodies; the personal working directory remains untouched.
        con.execute('DELETE FROM submission_files WHERE submission_id=?',(row['snapshot_id'],))
        con.execute('DELETE FROM submissions WHERE id=?',(row['snapshot_id'],))
        con.execute('DELETE FROM projects WHERE id=?',(source['project_id'],))

    def evidence(self,principal,submission_id):
        source=self.store.get_submission(submission_id,principal)
        with self.store._connection() as con:
            rows=con.execute("SELECT * FROM web_jobs WHERE submission_id=? AND content_sha256=? ORDER BY created_at DESC",(submission_id,source['content_sha256'])).fetchall()
        return [Jobs._job(row) for row in rows]
