"""Full-chain, dependency-ordered safe cleanup by project with audit logging.

Unlike ``web_cleanup`` (which only removes trashed assets in the recycle bin
while preserving projects and submissions), this module plans and executes
cleanup scoped to a single project:
  1. Project-private state: fixed/minor submissions, run records/artifacts,
     preparation snapshots, and usage bindings (plus resetting claimed task /
     distribution status back to ``pending`` when requested).
  2. Produced assets (selectable by admin/teacher): published templates,
     published algorithms, and derived datasets created by this project —
     each checked for external references and blocked when usage is uncertain
     or still referenced outside this purge plan.
  3. Borrowed/shared resources: strictly protected and retained by default.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, reference
from .web_identity import Principal
from .web_cleanup import _preserve_cascaded_grants_for_template
from .web_lifecycle import AssetLifecycle, _json, _now, _safe_json, _table


_CONFIRMATION = '确认清理项目'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_UUID_LIKE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_HOLDING_BLOB_NAME = re.compile(r'^\.project-purge-[A-Za-z0-9_-]{1,64}\.blob\Z')
_PRODUCED_KINDS = {'template', 'algorithm', 'derived_dataset'}
_RECOVERY_LOCK_GUARD = threading.Lock()
_RECOVERY_THREAD_DEPTH: dict[tuple[int, str], int] = {}
_RECOVERY_FALLBACK_LOCK = threading.RLock()


def _is_teacher(principal: object) -> bool:
    return (
        isinstance(principal, Principal)
        and type(principal.uid) is int
        and principal.uid >= 1
        and principal.role == 'teacher'
        and principal.username == 'ly'
    )


def _require_principal(principal: object) -> tuple[int, str, bool]:
    if (
        not isinstance(principal, Principal)
        or type(principal.uid) is not int
        or principal.uid < 1
        or not isinstance(principal.username, str)
        or not principal.username
    ):
        raise KitError('清理操作需要可信登录身份')
    return principal.uid, principal.username, _is_teacher(principal)


def _check_project_id(project_id: object) -> str:
    if not isinstance(project_id, str) or not _UUID_LIKE.fullmatch(project_id):
        raise KitError('项目 ID 格式无效')
    return project_id


def _item_key(kind: str, ref: str) -> str:
    return f'{kind}:{ref}'


def _parse_item_key(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or ':' not in value:
        raise KitError('清理勾选项格式无效')
    kind, ref = value.split(':', 1)
    if kind not in _PRODUCED_KINDS or not ref:
        raise KitError('清理勾选项类型或标识无效')
    if kind in {'algorithm', 'template'}:
        reference(ref)
    elif any(part in {'', '.', '..'} for part in ref.split('/')):
        raise KitError('派生数据集标识无效')
    return kind, ref


class ProjectPurge:
    """Three-step per-project cleanup planner, executor, and audit store."""

    def __init__(self, store, library_root: Path | str | None = None, policy=None):
        self.store = store
        self.policy = policy
        self.lifecycle = AssetLifecycle(store, library_root, policy)
        self.lifecycle._prepare_library()
        self.derived = None
        if policy is not None and getattr(policy, 'derived_dataset_root', None) is not None:
            from .web_derived import DerivedDatasets
            self.derived = DerivedDatasets(store, policy.derived_dataset_root)
        self._initialize()
        self.recover_pending()

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('''CREATE TABLE IF NOT EXISTS web_project_purge_audit (
                    audit_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    owner_username TEXT NOT NULL,
                    actor_uid INTEGER NOT NULL,
                    actor_username TEXT NOT NULL,
                    plan_token TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deleted_json TEXT NOT NULL,
                    retained_json TEXT NOT NULL,
                    failed_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )''')
                con.execute('''CREATE INDEX IF NOT EXISTS web_project_purge_audit_time
                               ON web_project_purge_audit(created_at DESC, audit_id DESC)''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_project_purge_recovery (
                    audit_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    staged_blobs_json TEXT NOT NULL DEFAULT '[]',
                    deleted_json TEXT NOT NULL DEFAULT '[]',
                    failed_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )''')
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    def _recovery_dir(self) -> Path | None:
        return getattr(self.store, 'directory', None)

    def _recovery_file(self, audit_id: str) -> Path | None:
        root = self._recovery_dir()
        if root is None:
            return None
        return Path(root) / f'.purge-recovery-{audit_id}.json'

    def _write_recovery_file(self, audit_id: str, payload: dict[str, object]) -> None:
        rfile = self._recovery_file(audit_id)
        if rfile is None:
            return
        tmp = rfile.with_suffix('.tmp.' + uuid.uuid4().hex)
        tmp.write_text(_json(payload), encoding='utf-8')
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, rfile)

    def _remove_recovery_file(self, audit_id: str) -> None:
        rfile = self._recovery_file(audit_id)
        if rfile is not None and os.path.lexists(rfile):
            try:
                rfile.unlink()
            except OSError:
                pass

    @contextmanager
    def _recovery_lock(self):
        rdir = self._recovery_dir()
        if rdir is None or not Path(rdir).is_dir():
            with _RECOVERY_FALLBACK_LOCK:
                yield
            return
        lock_path = (Path(rdir) / '.purge-recovery.lock').resolve()
        key = (threading.get_ident(), str(lock_path))
        with _RECOVERY_LOCK_GUARD:
            depth = _RECOVERY_THREAD_DEPTH.get(key, 0)
            if depth > 0:
                _RECOVERY_THREAD_DEPTH[key] = depth + 1
                reentered = True
            else:
                reentered = False
        if reentered:
            try:
                yield
            finally:
                with _RECOVERY_LOCK_GUARD:
                    _RECOVERY_THREAD_DEPTH[key] -= 1
            return

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
                raise KitError('清理恢复锁文件不安全')
            fcntl.flock(fd, fcntl.LOCK_EX)
            with _RECOVERY_LOCK_GUARD:
                _RECOVERY_THREAD_DEPTH[key] = 1
            try:
                yield
            finally:
                with _RECOVERY_LOCK_GUARD:
                    _RECOVERY_THREAD_DEPTH.pop(key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _resolve_staged_blob_pair(self, item: object) -> tuple[Path, Path] | None:
        """Strictly validate a recovery staged-blob pair within `derived.root` (no symlinks/escapes)."""
        if self.derived is None:
            return None
        self.derived._ensure_root()
        derived_root = self.derived.root
        derived_real = derived_root.resolve()
        orig: Path | None = None
        holding: Path | None = None
        if isinstance(item, dict):
            orig_rel = item.get('orig_rel')
            holding_name = item.get('holding_name')
            if not isinstance(orig_rel, str) or not orig_rel or '\x00' in orig_rel or not isinstance(holding_name, str):
                return None
            if not _HOLDING_BLOB_NAME.fullmatch(holding_name):
                return None
            rel_p = Path(orig_rel)
            if rel_p.is_absolute() or '..' in rel_p.parts or any(part in ('', '.') or part.startswith('.project-purge-') for part in rel_p.parts):
                return None
            orig = derived_real.joinpath(*rel_p.parts)
            holding = orig.parent / holding_name
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            o_str, h_str = item[0], item[1]
            if not isinstance(o_str, str) or not isinstance(h_str, str) or not o_str or not h_str:
                return None
            if '\x00' in o_str or '\x00' in h_str:
                return None
            o_raw, h_raw = Path(o_str), Path(h_str)
            if not o_raw.is_absolute() or not h_raw.is_absolute() or '..' in o_raw.parts or '..' in h_raw.parts:
                return None
            if not _HOLDING_BLOB_NAME.fullmatch(h_raw.name) or o_raw.name.startswith('.'):
                return None
            try:
                o_rel = o_raw.relative_to(derived_root)
            except ValueError:
                try:
                    o_rel = o_raw.relative_to(derived_real)
                except ValueError:
                    return None
            if not o_rel.parts or any(part in ('', '.') or part.startswith('.project-purge-') for part in o_rel.parts):
                return None
            try:
                h_rel = h_raw.relative_to(derived_root)
            except ValueError:
                try:
                    h_rel = h_raw.relative_to(derived_real)
                except ValueError:
                    return None
            if h_rel.parts[:-1] != o_rel.parts[:-1]:
                return None
            orig = derived_real.joinpath(*o_rel.parts)
            holding = orig.parent / h_raw.name
        else:
            return None

        cur = derived_real
        rel_parts = orig.relative_to(derived_real).parts
        for part in rel_parts[:-1]:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                return None
        parent_real = cur.resolve()
        try:
            if parent_real != derived_real:
                parent_real.relative_to(derived_real)
        except ValueError:
            return None

        if os.path.lexists(orig):
            st_o = os.lstat(orig)
            if stat.S_ISLNK(st_o.st_mode) or not stat.S_ISREG(st_o.st_mode) or st_o.st_nlink != 1 or st_o.st_uid != os.geteuid():
                return None
        if os.path.lexists(holding):
            st_h = os.lstat(holding)
            if stat.S_ISLNK(st_h.st_mode) or not stat.S_ISREG(st_h.st_mode) or st_h.st_nlink != 1 or st_h.st_uid != os.geteuid():
                return None
        return orig, holding

    def recover_pending(self) -> list[dict[str, object]]:
        """Finish or roll back any interrupted project purges from DB + disk recovery journals."""
        recovered: list[dict[str, object]] = []
        with self._recovery_lock():
            rdir = self._recovery_dir()
            if rdir is not None and rdir.is_dir():
                for path in sorted(Path(rdir).glob('.purge-recovery-*.json')):
                    try:
                        pst = os.lstat(path)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISREG(pst.st_mode) or pst.st_nlink != 1 or pst.st_uid != os.geteuid():
                        recovered.append({'audit_id': '', 'action': 'rejected_unsafe_journal', 'journal': path.name})
                        continue
                    try:
                        data = json.loads(path.read_text(encoding='utf-8'))
                    except Exception:
                        continue
                    if not isinstance(data, dict) or not isinstance(data.get('audit_id'), str):
                        continue
                    audit_id = str(data['audit_id'])
                    if not _UUID_LIKE.fullmatch(audit_id) or path.name != f'.purge-recovery-{audit_id}.json':
                        recovered.append({'audit_id': audit_id, 'action': 'rejected_unsafe_journal', 'journal': path.name})
                        continue
                    pid = str(data.get('project_id') or '')
                    raw_blobs = (
                        data.get('staged_blob_entries')
                        if isinstance(data.get('staged_blob_entries'), list)
                        else (data.get('staged_blobs') if isinstance(data.get('staged_blobs'), list) else [])
                    )
                    pairs: list[tuple[Path, Path]] = []
                    unsafe_blob = False
                    for item in raw_blobs:
                        pair = self._resolve_staged_blob_pair(item)
                        if pair is None:
                            unsafe_blob = True
                            break
                        pairs.append(pair)
                    if unsafe_blob:
                        recovered.append({'audit_id': audit_id, 'action': 'rejected_unsafe_journal', 'journal': path.name})
                        continue

                    with self.store._connection() as con:
                        proj_exists = con.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone() is not None if pid else False
                        audit_exists = _table(con, 'web_project_purge_audit') and (
                            con.execute('SELECT 1 FROM web_project_purge_audit WHERE audit_id=?', (audit_id,)).fetchone() is not None
                        )
                    if proj_exists and not audit_exists:
                        for orig, holding in pairs:
                            if os.path.lexists(holding) and not os.path.lexists(orig):
                                try:
                                    os.rename(holding, orig)
                                except OSError:
                                    pass
                        self._remove_recovery_file(audit_id)
                        recovered.append({'audit_id': audit_id, 'action': 'rolled_back_staged_blobs'})
                    elif audit_exists:
                        extra_failed: list[dict[str, object]] = []
                        for _orig, holding in pairs:
                            if os.path.lexists(holding):
                                try:
                                    os.unlink(holding)
                                except OSError as exc:
                                    extra_failed.append({'kind': 'derived_blob', 'ref': holding.name, 'error': str(exc)})
                        with self.store._connection() as con:
                            con.execute('BEGIN IMMEDIATE')
                            try:
                                row = con.execute('SELECT status, failed_json FROM web_project_purge_audit WHERE audit_id=?', (audit_id,)).fetchone()
                                if row is not None and extra_failed:
                                    cur_failed = json.loads(row['failed_json'])
                                    cur_failed.extend(extra_failed)
                                    con.execute('UPDATE web_project_purge_audit SET status=?, failed_json=? WHERE audit_id=?', ('partial', _json(cur_failed), audit_id))
                                if _table(con, 'web_project_purge_recovery'):
                                    con.execute('DELETE FROM web_project_purge_recovery WHERE audit_id=?', (audit_id,))
                                con.execute('COMMIT')
                            except Exception:
                                if con.in_transaction:
                                    con.execute('ROLLBACK')
                                raise
                        self._remove_recovery_file(audit_id)
                        recovered.append({'audit_id': audit_id, 'action': 'finalized_from_file'})

            with self.store._connection() as con:
                if not _table(con, 'web_project_purge_recovery'):
                    return recovered
                rows = con.execute('SELECT * FROM web_project_purge_recovery ORDER BY updated_at ASC').fetchall()
            for r in rows:
                audit_id = str(r['audit_id'])
                raw_blobs = json.loads(r['staged_blobs_json'] or '[]')
                pairs = []
                unsafe_blob = False
                for item in raw_blobs:
                    pair = self._resolve_staged_blob_pair(item)
                    if pair is None:
                        unsafe_blob = True
                        break
                    pairs.append(pair)
                if unsafe_blob:
                    recovered.append({'audit_id': audit_id, 'action': 'rejected_unsafe_journal'})
                    continue
                failed_extra: list[dict[str, object]] = []
                for _orig, holding in pairs:
                    if os.path.lexists(holding):
                        try:
                            os.unlink(holding)
                        except OSError as exc:
                            failed_extra.append({'kind': 'derived_blob', 'ref': holding.name, 'error': str(exc)})
                with self.store._connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    try:
                        if failed_extra:
                            row = con.execute('SELECT failed_json FROM web_project_purge_audit WHERE audit_id=?', (audit_id,)).fetchone()
                            if row is not None:
                                cur_failed = json.loads(row['failed_json'])
                                cur_failed.extend(failed_extra)
                                con.execute('UPDATE web_project_purge_audit SET status=?, failed_json=? WHERE audit_id=?', ('partial', _json(cur_failed), audit_id))
                        con.execute('DELETE FROM web_project_purge_recovery WHERE audit_id=?', (audit_id,))
                        con.execute('COMMIT')
                    except Exception:
                        if con.in_transaction:
                            con.execute('ROLLBACK')
                        raise
                self._remove_recovery_file(audit_id)
                recovered.append({'audit_id': audit_id, 'action': 'finalized_from_db'})
        return recovered

    @staticmethod
    def _authorize_project(con: sqlite3.Connection, principal: Principal, project_id: str) -> sqlite3.Row:
        uid, username, is_teacher = _require_principal(principal)
        row = con.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if row is None:
            raise KitError('项目不存在或已被清理')
        if _table(con, 'web_submission_preparations') and con.execute(
            'SELECT 1 FROM web_submission_preparations prep JOIN submissions s ON s.id=prep.snapshot_id WHERE s.project_id=? LIMIT 1',
            (project_id,),
        ).fetchone() is not None:
            raise KitError('内部临时检查项目不可直接作为个人项目清理')
        if _table(con, 'web_asset_validation_snapshots') and con.execute(
            'SELECT 1 FROM web_asset_validation_snapshots WHERE project_id=? LIMIT 1',
            (project_id,),
        ).fetchone() is not None:
            raise KitError('资产验证快照项目不可直接作为个人项目清理')
        if not is_teacher:
            raise KitError('只有管理员可执行平台项目永久清理；个人工作区副本请使用“删除个人项目副本”')
        return row

    def list_projects(self, principal: Principal) -> list[dict[str, object]]:
        uid, username, is_teacher = _require_principal(principal)
        if not is_teacher:
            raise KitError('只有管理员可查看或执行平台项目永久清理')
        with self.store._connection() as con:
            sql = '''SELECT p.* FROM projects p
                     WHERE NOT EXISTS (
                         SELECT 1 FROM web_asset_validation_snapshots v WHERE v.project_id=p.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM web_submission_preparations prep
                         JOIN submissions s ON s.id=prep.snapshot_id
                         WHERE s.project_id=p.id
                     )'''
            params: tuple[object, ...] = ()
            if not is_teacher:
                sql += ' AND p.owner_uid=? AND p.owner_username=?'
                params = (uid, username)
            sql += ' ORDER BY p.created_at DESC, p.id DESC'
            rows = con.execute(sql, params).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                pid = str(row['id'])
                sub_rows = con.execute('SELECT id, kind FROM submissions WHERE project_id=?', (pid,)).fetchall()
                sub_ids = [str(r['id']) for r in sub_rows]
                fixed_count = sum(1 for r in sub_rows if r['kind'] == 'major')
                job_count = 0
                if sub_ids and _table(con, 'web_jobs'):
                    marks = ','.join('?' for _ in sub_ids)
                    job_count = int(con.execute(
                        f'SELECT count(*) FROM web_jobs WHERE submission_id IN ({marks})',
                        sub_ids,
                    ).fetchone()[0])
                derived_count = 0
                if sub_ids and _table(con, 'web_derived_datasets'):
                    marks = ','.join('?' for _ in sub_ids)
                    derived_count = int(con.execute(
                        f'SELECT count(*) FROM web_derived_datasets WHERE source_submission_id IN ({marks})',
                        sub_ids,
                    ).fetchone()[0])
                pub_count = 0
                if sub_ids and _table(con, 'web_asset_candidates') and _table(con, 'web_asset_published'):
                    marks = ','.join('?' for _ in sub_ids)
                    pub_count = int(con.execute(
                        f'''SELECT count(*) FROM web_asset_published p
                            JOIN web_asset_candidates c ON c.candidate_id=p.candidate_id
                            WHERE c.submission_id IN ({marks})''',
                        sub_ids,
                    ).fetchone()[0])
                has_task = bool(
                    (_table(con, 'web_task_recipients') and con.execute(
                        'SELECT 1 FROM web_task_recipients WHERE project_id=? LIMIT 1', (pid,)
                    ).fetchone() is not None)
                    or con.execute('SELECT 1 FROM distributions WHERE project_id=? LIMIT 1', (pid,)).fetchone() is not None
                )
                result.append({
                    'project_id': pid,
                    'name': str(row['name']),
                    'owner_uid': int(row['owner_uid']),
                    'owner_username': str(row['owner_username']),
                    'created_at': str(row['created_at']),
                    'submission_count': len(sub_ids),
                    'fixed_submission_count': fixed_count,
                    'job_count': job_count,
                    'derived_dataset_count': derived_count,
                    'published_asset_count': pub_count,
                    'has_task_link': has_task,
                })
        return result

    def _collect_project_context(self, con: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        sub_rows = con.execute(
            'SELECT id, sequence, version_major, version_minor, kind, content_sha256, manifest_json, created_at '
            'FROM submissions WHERE project_id=? ORDER BY sequence ASC',
            (project_id,),
        ).fetchall()
        submission_ids = [str(r['id']) for r in sub_rows]

        prep_rows = []
        snapshot_sub_ids: list[str] = []
        snapshot_project_ids: list[str] = []
        if _table(con, 'web_submission_preparations'):
            prep_rows = con.execute(
                'SELECT * FROM web_submission_preparations WHERE source_project_id=?',
                (project_id,),
            ).fetchall()
            for prow in prep_rows:
                sid = prow['snapshot_id']
                if sid:
                    sub = con.execute('SELECT id, project_id FROM submissions WHERE id=?', (sid,)).fetchone()
                    if sub is not None:
                        snapshot_sub_ids.append(str(sub['id']))
                        if str(sub['project_id']) != project_id:
                            snapshot_project_ids.append(str(sub['project_id']))

        all_sub_ids = list(dict.fromkeys(submission_ids + snapshot_sub_ids))

        file_count = 0
        file_bytes = 0
        if all_sub_ids:
            marks = ','.join('?' for _ in all_sub_ids)
            row = con.execute(
                f'SELECT count(*), coalesce(sum(size), 0) FROM submission_files WHERE submission_id IN ({marks})',
                all_sub_ids,
            ).fetchone()
            file_count, file_bytes = int(row[0]), int(row[1])

        job_rows: list[sqlite3.Row] = []
        run_ids: list[str] = []
        artifact_count = 0
        if all_sub_ids and _table(con, 'web_jobs'):
            marks = ','.join('?' for _ in all_sub_ids)
            job_rows = con.execute(
                f'SELECT run_id, submission_id, operation, status, created_at FROM web_jobs WHERE submission_id IN ({marks}) ORDER BY created_at DESC',
                all_sub_ids,
            ).fetchall()
            run_ids = [str(r['run_id']) for r in job_rows]
            if run_ids and _table(con, 'web_job_artifacts'):
                rmarks = ','.join('?' for _ in run_ids)
                artifact_count = int(con.execute(
                    f'SELECT count(*) FROM web_job_artifacts WHERE run_id IN ({rmarks})',
                    run_ids,
                ).fetchone()[0])

        task_links: list[dict[str, object]] = []
        dist_rows = con.execute('SELECT * FROM distributions WHERE project_id=?', (project_id,)).fetchall()
        for d in dist_rows:
            task_links.append({
                'kind': 'distribution',
                'distribution_id': str(d['id']),
                'source_project_name': str(d['source_project_name']),
                'source_version': str(d['source_version']),
                'target_username': str(d['target_username']),
            })
        if _table(con, 'web_task_recipients'):
            rec_rows = con.execute(
                '''SELECT r.*, t.name AS task_name, t.delivery_mode
                   FROM web_task_recipients r
                   JOIN web_task_deliveries t ON t.id=r.task_id
                   WHERE r.project_id=?''',
                (project_id,),
            ).fetchall()
            for rec in rec_rows:
                task_links.append({
                    'kind': 'task_recipient',
                    'task_id': str(rec['task_id']),
                    'task_name': str(rec['task_name']),
                    'delivery_mode': str(rec['delivery_mode']),
                    'distribution_id': rec['distribution_id'],
                    'target_username': str(rec['target_username']),
                })

        outgoing_blockers: list[dict[str, object]] = []
        if submission_ids:
            marks = ','.join('?' for _ in submission_ids)
            out_dists = con.execute(
                f'SELECT id, target_username, status, project_id FROM distributions WHERE source_submission_id IN ({marks})',
                submission_ids,
            ).fetchall()
            for od in out_dists:
                outgoing_blockers.append({
                    'type': 'outgoing-distribution',
                    'label': '该项目曾作为教师基线分发给账号',
                    'detail': f"分发 {od['id']} -> {od['target_username']}（状态 {od['status']}）",
                    'suggestion': '此项目属于教师分发基线，被分发记录引用，不能直接整项目删除。',
                })
            if _table(con, 'web_task_deliveries'):
                out_tasks = con.execute(
                    f'SELECT id, name FROM web_task_deliveries WHERE baseline_submission_id IN ({marks})',
                    submission_ids,
                ).fetchall()
                for ot in out_tasks:
                    outgoing_blockers.append({
                        'type': 'outgoing-task-baseline',
                        'label': '该项目固定提交被教师任务用作基线',
                        'detail': f"任务 {ot['name']}（{ot['id']}）",
                        'suggestion': '此项目作为教师任务基线，不能直接删除。',
                    })
            if _table(con, 'package_bundles'):
                out_pkgs = con.execute(
                    f'SELECT bundle_id FROM package_bundles WHERE source_submission_id IN ({marks})',
                    submission_ids,
                ).fetchall()
                for op in out_pkgs:
                    in_use = con.execute(
                        'SELECT project_id FROM project_package_sources WHERE bundle_id=? AND project_id<>?',
                        (op['bundle_id'], project_id),
                    ).fetchall()
                    if in_use:
                        outgoing_blockers.append({
                            'type': 'outgoing-package-bundle',
                            'label': '该项目导出的项目包正被其他项目引用',
                            'detail': f"包 {op['bundle_id']} -> 项目 {in_use[0]['project_id']}",
                            'suggestion': '请先清理引用该导出包的项目。',
                        })

        return {
            'sub_rows': sub_rows,
            'submission_ids': submission_ids,
            'snapshot_sub_ids': snapshot_sub_ids,
            'snapshot_project_ids': list(dict.fromkeys(snapshot_project_ids)),
            'all_sub_ids': all_sub_ids,
            'prep_rows': prep_rows,
            'file_count': file_count,
            'file_bytes': file_bytes,
            'job_rows': job_rows,
            'run_ids': run_ids,
            'artifact_count': artifact_count,
            'task_links': task_links,
            'outgoing_blockers': outgoing_blockers,
        }

    def _collect_produced_and_shared(
        self, con: sqlite3.Connection, project_id: str, project_row: sqlite3.Row,
        ctx: dict[str, Any], *, is_teacher: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        sub_ids: list[str] = ctx['submission_ids']
        run_ids: list[str] = ctx['run_ids']
        all_sub_set = set(ctx['all_sub_ids'])
        verification_errors: list[str] = []

        produced_map: dict[tuple[str, str], dict[str, Any]] = {}
        if sub_ids:
            marks = ','.join('?' for _ in sub_ids)
            if _table(con, 'web_direct_review_publications'):
                for row in con.execute(
                    f'''SELECT submission_id, kind, alias, ref, candidate_id, status, published_at
                        FROM web_direct_review_publications
                        WHERE submission_id IN ({marks}) AND status IN ('published', 'reused')''',
                    sub_ids,
                ).fetchall():
                    key = (str(row['kind']), str(row['ref']))
                    produced_map.setdefault(key, {
                        'kind': str(row['kind']),
                        'ref': str(row['ref']),
                        'name': str(row['ref']),
                        'candidate_ids': set(),
                        'submission_ids': set(),
                        'is_published': False,
                        'reused': str(row['status']) == 'reused',
                    })
                    produced_map[key]['submission_ids'].add(str(row['submission_id']))
                    if row['candidate_id']:
                        produced_map[key]['candidate_ids'].add(str(row['candidate_id']))
            if _table(con, 'web_asset_candidates'):
                for row in con.execute(
                    f'SELECT candidate_id, kind, ref, status, submission_id FROM web_asset_candidates WHERE submission_id IN ({marks})',
                    sub_ids,
                ).fetchall():
                    key = (str(row['kind']), str(row['ref']))
                    produced_map.setdefault(key, {
                        'kind': str(row['kind']),
                        'ref': str(row['ref']),
                        'name': str(row['ref']),
                        'candidate_ids': set(),
                        'submission_ids': set(),
                        'is_published': False,
                        'reused': False,
                    })
                    produced_map[key]['submission_ids'].add(str(row['submission_id']))
                    produced_map[key]['candidate_ids'].add(str(row['candidate_id']))

        for (kind, ref), item in list(produced_map.items()):
            pub = (_table(con, 'web_asset_published') and con.execute(
                'SELECT candidate_id FROM web_asset_published WHERE kind=? AND ref=?',
                (kind, ref),
            ).fetchone())
            item['is_published'] = pub is not None
            if pub is not None and _table(con, 'web_asset_candidates'):
                orig_cand = con.execute(
                    'SELECT submission_id FROM web_asset_candidates WHERE candidate_id=?',
                    (pub['candidate_id'],),
                ).fetchone()
                if orig_cand is not None and str(orig_cand['submission_id']) not in all_sub_set:
                    item['owned_by_other_project'] = True
                    item['source_submission_id'] = str(orig_cand['submission_id'])
                else:
                    item['owned_by_other_project'] = False
            else:
                item['owned_by_other_project'] = False

        derived_map: dict[str, dict[str, Any]] = {}
        if _table(con, 'web_derived_datasets'):
            clauses: list[str] = []
            params: list[object] = []
            if sub_ids:
                smarks = ','.join('?' for _ in sub_ids)
                clauses.append(f'source_submission_id IN ({smarks})')
                params.extend(sub_ids)
            if run_ids:
                rmarks = ','.join('?' for _ in run_ids)
                clauses.append(f'source_run_id IN ({rmarks})')
                params.extend(run_ids)
            if clauses:
                drows = con.execute(
                    f"SELECT * FROM web_derived_datasets WHERE ({' OR '.join(clauses)}) ORDER BY created_at DESC",
                    params,
                ).fetchall()
                for d in drows:
                    did = str(d['dataset_id'])
                    derived_map[did] = {
                        'kind': 'derived_dataset',
                        'ref': did,
                        'name': str(d['name']),
                        'size': int(d['size']),
                        'blob_name': str(d['blob_name']),
                        'visibility': str(d['visibility']),
                        'source_submission_id': d['source_submission_id'],
                        'source_run_id': d['source_run_id'],
                    }

        produced_template_refs = {k[1] for k, v in produced_map.items() if k[0] == 'template' and not v.get('owned_by_other_project')}
        produced_derived_ids = set(derived_map.keys())
        produced_algo_refs = {k[1] for k, v in produced_map.items() if k[0] == 'algorithm' and not v.get('owned_by_other_project')}

        produced_items: list[dict[str, Any]] = []

        def _evaluate_published_item(kind: str, ref: str, meta: dict[str, Any]) -> dict[str, Any]:
            key = _item_key(kind, ref)
            blockers: list[dict[str, object]] = []
            advisories: list[str] = []
            sibling_deps: list[str] = []
            uncertain = False

            if meta.get('owned_by_other_project'):
                blockers.append({
                    'type': 'reused-external-asset',
                    'label': '该已入库资产来源于其他项目（本项目审核时复用了既有版本）',
                    'suggestion': '不应在清理本项目时连带删除他人版本，已自动保护保留。',
                })
            elif not is_teacher:
                blockers.append({
                    'type': 'permission',
                    'label': '已入库平台资产仅教师管理员有权清理',
                    'suggestion': '您的权限可清理个人项目自身内容，已入库版本将保留。',
                })

            exists_in_lib = True
            try:
                self.lifecycle._verify_exists(con, kind, ref)
            except KitError:
                exists_in_lib = False

            if exists_in_lib and not meta.get('owned_by_other_project'):
                report = self.lifecycle._report(con, kind, ref, 0, 1_000_000)
                if len(report['notes']) > 1:
                    uncertain = True
                    for note in report['notes'][1:]:
                        verification_errors.append(note)
                        blockers.append({
                            'type': 'uncertain-usage',
                            'label': '无法完整核对用途（系统阻止删除）',
                            'detail': note,
                            'suggestion': '无法确认外部用途时默认保留，请先修复记录异常。',
                        })
                for ritem in report['references']:
                    rtype = ritem.get('type')
                    if rtype == 'task-material-history':
                        continue
                    if ritem.get('project_id') == project_id or ritem.get('submission_id') in all_sub_set:
                        continue
                    if rtype == 'derived-dataset' and ritem.get('detail') in produced_derived_ids:
                        sibling_deps.append(_item_key('derived_dataset', str(ritem['detail'])))
                        continue
                    if rtype in {'platform-template', 'asset-candidate'} and ritem.get('kind') == 'template' and ritem.get('ref') in produced_template_refs:
                        sibling_deps.append(_item_key('template', str(ritem['ref'])))
                        continue
                    detail = ' · '.join(str(ritem.get(k, '')) for k in ('owner_username', 'project_id', 'submission_id', 'task_name', 'detail') if ritem.get(k))
                    blockers.append({
                        'type': str(rtype),
                        'label': str(ritem.get('label') or '外部引用'),
                        'detail': detail,
                        'suggestion': '被其他项目、固定提交或任务授权引用，默认保留；若确需清理，请先解除外部引用。',
                    })
                if meta['is_published']:
                    advisories.append('清理后将删除该版本的入库副本与候选记录，并释放此版本号以便重新入库。')
                else:
                    advisories.append('清理后将删除尚未正式发布的资产候选副本。')
            elif not exists_in_lib:
                advisories.append('仅存在候选/发布记录（当前库中已无活跃副本），可一并清理历史标记。')

            return {
                'key': key,
                'kind': kind,
                'ref': ref,
                'name': ref,
                'status': 'published' if meta['is_published'] else 'candidate',
                'estimated_bytes': 0,
                'uncertain': uncertain,
                'can_delete': len(blockers) == 0,
                'blockers': blockers,
                'advisories': advisories,
                'sibling_deps': sorted(set(sibling_deps)),
                'owned_by_other_project': bool(meta.get('owned_by_other_project')),
            }

        for (kind, ref) in sorted(produced_map.keys(), key=lambda x: (0 if x[0] == 'template' else 1, x[1])):
            produced_items.append(_evaluate_published_item(kind, ref, produced_map[(kind, ref)]))

        for did in sorted(derived_map.keys()):
            dmeta = derived_map[did]
            key = _item_key('derived_dataset', did)
            blockers = []
            advisories = ['清理后将删除该衍生数据集登记记录与托管数据文件（不影响原始共享数据集）。']
            uncertain = False
            sibling_deps: list[str] = []

            try:
                report = self.lifecycle._report(con, 'dataset', did, 0, 1_000_000)
            except KitError as exc:
                report = {'notes': ['', f'衍生数据集 {did} 状态异常：{exc}'], 'references': []}

            if len(report['notes']) > 1:
                uncertain = True
                for note in report['notes'][1:]:
                    verification_errors.append(note)
                    blockers.append({
                        'type': 'uncertain-usage',
                        'label': '衍生数据集用途核对不完整（系统阻止删除）',
                        'detail': note,
                        'suggestion': '无法确认外部引用时默认阻止删除。',
                    })

            for ritem in report['references']:
                rtype = ritem.get('type')
                if rtype == 'task-material-history':
                    continue
                if ritem.get('project_id') == project_id or ritem.get('submission_id') in all_sub_set:
                    continue
                if rtype in {'platform-template', 'asset-candidate'} and ritem.get('kind') == 'template' and ritem.get('ref') in produced_template_refs:
                    sibling_deps.append(_item_key('template', str(ritem['ref'])))
                    continue
                if rtype == 'derived-dataset' and ritem.get('detail') in produced_derived_ids:
                    sibling_deps.append(_item_key('derived_dataset', str(ritem['detail'])))
                    continue
                detail = ' · '.join(str(ritem.get(k, '')) for k in ('owner_username', 'project_id', 'submission_id', 'task_name', 'detail') if ritem.get(k))
                blockers.append({
                    'type': str(rtype),
                    'label': str(ritem.get('label') or '外部引用'),
                    'detail': detail,
                    'suggestion': '其他项目或任务正在使用此衍生数据集，默认保留。',
                })

            if self.derived is not None:
                try:
                    self.derived._ensure_root()
                    blob = self.derived._blob_path(dmeta['blob_name'])
                    if not os.path.lexists(blob):
                        uncertain = True
                        blockers.append({
                            'type': 'missing-blob',
                            'label': '衍生数据集托管文件缺失（需核对）',
                            'detail': dmeta['blob_name'],
                            'suggestion': '请管理员先确认托管文件状态。',
                        })
                except (KitError, OSError) as exc:
                    uncertain = True
                    blockers.append({
                        'type': 'derived-root-error',
                        'label': '托管数据目录不可读取',
                        'detail': str(exc),
                        'suggestion': '请通过具有托管区权限的管理员账户或脚本执行清理。',
                    })

            produced_items.append({
                'key': key,
                'kind': 'derived_dataset',
                'ref': did,
                'name': dmeta['name'],
                'status': dmeta['visibility'],
                'estimated_bytes': dmeta['size'],
                'blob_name': dmeta['blob_name'],
                'uncertain': uncertain,
                'can_delete': len(blockers) == 0,
                'blockers': blockers,
                'advisories': advisories,
                'sibling_deps': sorted(set(sibling_deps)),
                'owned_by_other_project': False,
            })

        shared_datasets: set[str] = set()
        shared_algorithms: set[str] = set()
        shared_templates: set[str] = set()

        if _table(con, 'web_project_usage'):
            urow = con.execute('SELECT usage_json FROM web_project_usage WHERE project_id=?', (project_id,)).fetchone()
            if urow is not None:
                try:
                    usage = _safe_json(urow['usage_json'], 'usage')
                    if isinstance(usage, dict):
                        shared_datasets.update(str(x) for x in usage.get('dataset_ids', []) if isinstance(x, str))
                        shared_algorithms.update(str(x) for x in usage.get('algorithm_refs', []) if isinstance(x, str))
                        shared_templates.update(str(x) for x in usage.get('template_refs', []) if isinstance(x, str))
                except (ValueError, json.JSONDecodeError):
                    pass

        for srow in ctx['sub_rows']:
            try:
                manifest = _safe_json(srow['manifest_json'], 'manifest')
                refs = manifest.get('data_references', {}).get('references', [])
                for item in refs:
                    if isinstance(item, dict) and isinstance(item.get('dataset_id'), str):
                        shared_datasets.add(item['dataset_id'])
            except (ValueError, json.JSONDecodeError):
                pass
            files = con.execute(
                'SELECT path, content FROM submission_files WHERE submission_id=? AND (path=? OR path LIKE ?)',
                (srow['id'], 'project.yaml', '%/plugin.yaml'),
            ).fetchall()
            try:
                shared_algorithms.update(AssetLifecycle._plugins_from_files(files))
            except (ValueError, KitError, yaml.YAMLError, UnicodeError, RecursionError):
                pass
            pfile = next((f for f in files if f['path'] == 'project.yaml'), None)
            if pfile is not None:
                try:
                    origin = yaml.safe_load(bytes(pfile['content']).decode('utf-8')).get('origin', {})
                    if isinstance(origin, dict) and isinstance(origin.get('template_ref'), str):
                        shared_templates.add(origin['template_ref'])
                except (ValueError, yaml.YAMLError, UnicodeError, RecursionError):
                    pass

        if _table(con, 'web_task_materials') and _table(con, 'web_task_recipients'):
            mrows = con.execute(
                '''SELECT m.kind, m.ref FROM web_task_materials m
                   JOIN web_task_recipients r ON r.task_id=m.task_id
                   WHERE r.project_id=?''',
                (project_id,),
            ).fetchall()
            for mr in mrows:
                if mr['kind'] == 'dataset':
                    shared_datasets.add(str(mr['ref']))
                elif mr['kind'] == 'algorithm':
                    shared_algorithms.add(str(mr['ref']))
                elif mr['kind'] == 'template':
                    shared_templates.add(str(mr['ref']))

        retained_shared: list[dict[str, object]] = []
        for did in sorted(shared_datasets - produced_derived_ids):
            retained_shared.append({
                'kind': 'dataset',
                'ref': did,
                'name': did,
                'reason': '共享原始数据集或他人衍生数据：默认永久保留，仅解除本项目绑定',
            })
        for aref in sorted(shared_algorithms - produced_algo_refs):
            retained_shared.append({
                'kind': 'algorithm',
                'ref': aref,
                'name': aref,
                'reason': '平台/他人共享算法：默认永久保留，仅随本项目代码副本解除引用',
            })
        for tref in sorted(shared_templates - produced_template_refs):
            retained_shared.append({
                'kind': 'template',
                'ref': tref,
                'name': tref,
                'reason': '平台/来源案例模板：默认永久保留，不受本项目清理影响',
            })

        return produced_items, retained_shared, verification_errors

    @staticmethod
    def _compute_plan_token(
        pid: str,
        ctx: dict[str, Any],
        *,
        reset_task: bool,
        selected_keys: set[str],
        produced_items: list[dict[str, Any]],
        global_blockers: list[dict[str, object]],
    ) -> str:
        token_material = {
            'project_id': pid,
            'submissions': ctx['all_sub_ids'],
            'jobs': ctx['run_ids'],
            'task_links': ctx['task_links'],
            'reset_task': bool(reset_task),
            'selected_keys': sorted(selected_keys),
            'produced_status': [
                (i['key'], i['can_delete'], i['owned_by_other_project'], i['sibling_deps'])
                for i in produced_items
            ],
            'global_blockers': global_blockers,
        }
        return hashlib.sha256(_json(token_material).encode('utf-8')).hexdigest()

    def preview(
        self,
        principal: Principal,
        project_id: object,
        *,
        selected_keys: object = None,
        reset_task: object = True,
        workspace_info: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        _, _, is_teacher = _require_principal(principal)
        pid = _check_project_id(project_id)
        if type(reset_task) is not bool:
            raise KitError('reset_task 必须是布尔值')

        explicit_selected: set[str] | None = None
        if selected_keys is not None:
            if type(selected_keys) is not list:
                raise KitError('selected_keys 必须是列表')
            explicit_selected = set()
            for entry in selected_keys:
                kind, ref = _parse_item_key(entry)
                explicit_selected.add(_item_key(kind, ref))

        with self.store._connection() as con:
            project_row = self._authorize_project(con, principal, pid)
            ctx = self._collect_project_context(con, pid)
            produced_items, retained_shared, verification_errors = self._collect_produced_and_shared(
                con, pid, project_row, ctx, is_teacher=is_teacher,
            )

            by_key = {item['key']: item for item in produced_items}

            if explicit_selected is None:
                chosen = {item['key'] for item in produced_items if item['can_delete']}
                changed = True
                while changed:
                    changed = False
                    for key in list(chosen):
                        deps = set(by_key[key]['sibling_deps']) & set(by_key.keys())
                        if not deps.issubset(chosen):
                            chosen.remove(key)
                            changed = True
            else:
                unknown = explicit_selected - set(by_key.keys())
                if unknown:
                    raise KitError('选择了不属于本项目产出的资产：' + ', '.join(sorted(unknown)))
                chosen = set(explicit_selected)

            global_blockers = list(ctx['outgoing_blockers'])
            active_jobs = [r for r in ctx['job_rows'] if r['status'] in {'queued', 'running'}]
            if active_jobs:
                global_blockers.append({
                    'type': 'active-jobs',
                    'label': '该项目仍有排队或运行中的沙箱任务',
                    'detail': ', '.join(str(r['run_id']) for r in active_jobs),
                    'suggestion': '请等待运行完成或先取消该运行，再清理项目。',
                })

            for item in produced_items:
                missing_siblings = [
                    dep for dep in item['sibling_deps']
                    if dep in by_key and dep not in chosen
                ]
                item['selected'] = item['key'] in chosen
                item['missing_siblings'] = missing_siblings
                if item['selected'] and missing_siblings:
                    item['can_delete_with_selection'] = False
                    item['selection_blocker'] = (
                        '本项目发布的案例模板仍引用此项：'
                        + ', '.join(missing_siblings)
                        + '。请同时勾选该案例模板，或取消勾选本项。'
                    )
                elif item['selected'] and not item['can_delete']:
                    item['can_delete_with_selection'] = False
                    item['selection_blocker'] = item['blockers'][0]['label'] if item['blockers'] else '存在外部引用或状态异常'
                else:
                    item['can_delete_with_selection'] = item['can_delete'] and not any(
                        dep in by_key and not by_key[dep]['can_delete'] for dep in item['sibling_deps']
                    )
                    item['selection_blocker'] = None

            selected_blockers = [
                item['key'] for item in produced_items
                if item['selected'] and not item['can_delete_with_selection']
            ]
            can_execute = (len(global_blockers) == 0 and len(selected_blockers) == 0)

            will_delete_produced = [item for item in produced_items if item['selected']]
            will_retain_produced = [item for item in produced_items if not item['selected']]

            execution_order: list[dict[str, object]] = []
            for kind in ('template', 'algorithm', 'derived_dataset'):
                for item in will_delete_produced:
                    if item['kind'] == kind:
                        label_prefix = {'template': '已发布案例', 'algorithm': '已发布算法', 'derived_dataset': '衍生数据集'}[kind]
                        execution_order.append({
                            'step': f'delete_{kind}',
                            'label': f"{label_prefix}：{item['ref']}",
                            'key': item['key'],
                        })
            if ctx['task_links'] and reset_task:
                execution_order.append({
                    'step': 'reset_task',
                    'label': f"重置 {len(ctx['task_links'])} 条任务/分发领取状态为待领取（保留教师任务本身）",
                })
            if ctx['job_rows']:
                execution_order.append({
                    'step': 'delete_jobs',
                    'label': f"清理 {len(ctx['job_rows'])} 条运行记录与 {ctx['artifact_count']} 个产物索引",
                })
            if ctx['all_sub_ids']:
                execution_order.append({
                    'step': 'delete_submissions',
                    'label': f"清理 {len(ctx['submission_ids'])} 个版本提交及 {ctx['file_count']} 个快照文件记录（共 {ctx['file_bytes']} 字节）",
                })
            execution_order.append({
                'step': 'delete_project',
                'label': f"删除项目中央记录「{project_row['name']}」（保留各用户个人工作区本地目录）",
            })

            project_scope = {
                'project_id': pid,
                'project_name': str(project_row['name']),
                'owner_uid': int(project_row['owner_uid']),
                'owner_username': str(project_row['owner_username']),
                'created_at': str(project_row['created_at']),
                'submission_count': len(ctx['submission_ids']),
                'fixed_submission_count': sum(1 for r in ctx['sub_rows'] if r['kind'] == 'major'),
                'submission_ids': ctx['submission_ids'],
                'file_count': ctx['file_count'],
                'file_bytes': ctx['file_bytes'],
                'job_count': len(ctx['job_rows']),
                'artifact_count': ctx['artifact_count'],
                'preparation_count': len(ctx['prep_rows']),
                'task_links': ctx['task_links'],
                'has_task_link': len(ctx['task_links']) > 0,
                'reset_task': bool(reset_task),
                'workspace': workspace_info or {},
            }

            plan_token = self._compute_plan_token(
                pid, ctx, reset_task=bool(reset_task), selected_keys=chosen,
                produced_items=produced_items, global_blockers=global_blockers,
            )

            return {
                'project': project_scope,
                'produced_items': produced_items,
                'retained_shared_items': retained_shared,
                'will_delete_produced': will_delete_produced,
                'will_retain_produced': will_retain_produced,
                'global_blockers': global_blockers,
                'verification_errors': verification_errors,
                'execution_order': execution_order,
                'can_execute': can_execute,
                'can_purge_published': is_teacher,
                'confirmation_phrase': _CONFIRMATION,
                'plan_token': plan_token,
            }

    def execute(
        self,
        principal: Principal,
        project_id: object,
        *,
        plan_token: object,
        confirmation: object,
        selected_keys: object = None,
        reset_task: object = True,
        workspace_info: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        actor_uid, actor_username, is_teacher = _require_principal(principal)
        pid = _check_project_id(project_id)
        if confirmation != _CONFIRMATION or not isinstance(plan_token, str) or not _HASH.fullmatch(plan_token):
            raise KitError(f'必须先预览清理范围，并准确输入“{_CONFIRMATION}”后再执行')
        if type(reset_task) is not bool:
            raise KitError('reset_task 必须是布尔值')

        provisional = self.preview(
            principal, pid, selected_keys=selected_keys, reset_task=reset_task, workspace_info=workspace_info,
        )
        if provisional['plan_token'] != plan_token:
            raise KitError('项目内容、引用关系或勾选项已发生变化，请重新预览确认范围')
        if not provisional['can_execute']:
            raise KitError('当前清理计划存在阻挡项（外部引用或运行中任务），不能执行清理')

        selected_items = [item for item in provisional['produced_items'] if item['selected']]
        selected_set = {item['key'] for item in selected_items}
        retained_summary = [
            {'kind': item['kind'], 'ref': item['ref'], 'name': item['name'], 'reason': '管理员选择保留或存在外部引用'}
            for item in provisional['produced_items'] if not item['selected']
        ] + list(provisional['retained_shared_items'])

        staged_blob_removals: list[tuple[Path, Path]] = []
        audit_id = str(uuid.uuid4())
        deleted_log: list[dict[str, object]] = []
        failed_log: list[dict[str, object]] = []

        with self._recovery_lock():
            return self._execute_locked(
                principal=principal,
                actor_uid=actor_uid, actor_username=actor_username, is_teacher=is_teacher,
                pid=pid, plan_token=plan_token, selected_keys=selected_keys,
                reset_task=bool(reset_task), selected_items=selected_items,
                selected_set=selected_set, retained_summary=retained_summary,
                staged_blob_removals=staged_blob_removals, audit_id=audit_id,
                deleted_log=deleted_log, failed_log=failed_log,
            )

    def _execute_locked(
        self,
        *,
        principal: Principal,
        actor_uid: int,
        actor_username: str,
        is_teacher: bool,
        pid: str,
        plan_token: str,
        selected_keys: object,
        reset_task: bool,
        selected_items: list[dict[str, Any]],
        selected_set: set[str],
        retained_summary: list[dict[str, object]],
        staged_blob_removals: list[tuple[Path, Path]],
        audit_id: str,
        deleted_log: list[dict[str, object]],
        failed_log: list[dict[str, object]],
    ) -> dict[str, Any]:
        with self.store._connection() as con:
            con.execute('PRAGMA secure_delete=ON')
            con.execute('BEGIN IMMEDIATE')
            try:
                project_row = self._authorize_project(con, principal, pid)
                ctx = self._collect_project_context(con, pid)
                fresh_produced, _, _ = self._collect_produced_and_shared(
                    con, pid, project_row, ctx, is_teacher=is_teacher,
                )
                fresh_global_blockers = list(ctx['outgoing_blockers'])
                fresh_active_jobs = [r for r in ctx['job_rows'] if r['status'] in {'queued', 'running'}]
                if fresh_active_jobs:
                    fresh_global_blockers.append({
                        'type': 'active-jobs',
                        'label': '该项目仍有排队或运行中的沙箱任务',
                        'detail': ', '.join(str(r['run_id']) for r in fresh_active_jobs),
                        'suggestion': '请等待运行完成或先取消该运行，再清理项目。',
                    })
                if fresh_global_blockers:
                    raise KitError('执行前二次复核失败：存在运行中任务或新增分发基线依赖')
                fresh_token = self._compute_plan_token(
                    pid, ctx, reset_task=bool(reset_task), selected_keys=selected_set,
                    produced_items=fresh_produced, global_blockers=fresh_global_blockers,
                )
                if fresh_token != plan_token:
                    raise KitError('项目内容、提交记录、运行状态或引用关系在预览后发生了变化，请重新预览确认范围')

                fresh_by_key = {item['key']: item for item in fresh_produced}
                if selected_keys is not None and not selected_set.issubset(set(fresh_by_key.keys())):
                    raise KitError('执行前二次复核失败：所选产出资产已变化，请重新预览')
                for key in selected_set:
                    fitem = fresh_by_key[key]
                    if not fitem['can_delete']:
                        raise KitError(f"执行前二次复核阻止删除 {fitem['ref']}：{fitem['blockers'][0]['label']}")
                    missing = [d for d in fitem['sibling_deps'] if d in fresh_by_key and d not in selected_set]
                    if missing:
                        raise KitError(f"执行前二次复核阻止删除 {fitem['ref']}：仍被未勾选案例 {', '.join(missing)} 引用")

                if self.derived is not None:
                    pending_blobs: list[tuple[Path, Path]] = []
                    for item in selected_items:
                        if item['kind'] == 'derived_dataset':
                            self.derived._ensure_root()
                            blob = self.derived._blob_path(item['blob_name'])
                            holding = blob.parent / ('.project-purge-' + uuid.uuid4().hex + '.blob')
                            fd = os.open(blob, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                            try:
                                st = os.fstat(fd)
                                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
                                    raise KitError(f"衍生数据集托管文件身份不安全：{item['ref']}")
                            finally:
                                os.close(fd)
                            pending_blobs.append((blob, holding))
                    if pending_blobs and self.derived is not None:
                        derived_real = self.derived.root.resolve()
                        staged_entries = [
                            {
                                'orig_rel': b.resolve().relative_to(derived_real).as_posix(),
                                'holding_name': h.name,
                            }
                            for b, h in pending_blobs
                        ]
                        self._write_recovery_file(audit_id, {
                            'audit_id': audit_id,
                            'project_id': pid,
                            'phase': 'staged',
                            'staged_blob_entries': staged_entries,
                        })
                        for blob, holding in pending_blobs:
                            os.rename(blob, holding)
                            staged_blob_removals.append((blob, holding))

                for kind in ('template', 'algorithm'):
                    for item in [i for i in selected_items if i['kind'] == kind]:
                        ref = item['ref']
                        cands = [r['candidate_id'] for r in con.execute(
                            'SELECT candidate_id FROM web_asset_candidates WHERE kind=? AND ref=?', (kind, ref),
                        ).fetchall()]
                        for cid in cands:
                            if _table(con, 'web_asset_candidate_validations'):
                                con.execute('DELETE FROM web_asset_candidate_validations WHERE candidate_id=?', (cid,))
                            con.execute('DELETE FROM web_asset_candidate_files WHERE candidate_id=?', (cid,))
                        if _table(con, 'web_asset_published_files'):
                            con.execute('DELETE FROM web_asset_published_files WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_asset_published'):
                            con.execute('DELETE FROM web_asset_published WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_asset_candidates'):
                            con.execute('DELETE FROM web_asset_candidates WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_direct_review_publications'):
                            con.execute('DELETE FROM web_direct_review_publications WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_asset_lifecycle'):
                            con.execute('DELETE FROM web_asset_lifecycle WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_asset_lifecycle_history'):
                            con.execute('DELETE FROM web_asset_lifecycle_history WHERE kind=? AND ref=?', (kind, ref))
                        if _table(con, 'web_cleanup_deleted'):
                            con.execute('DELETE FROM web_cleanup_deleted WHERE kind=? AND ref=?', (kind, ref))
                        if kind == 'template':
                            _preserve_cascaded_grants_for_template(con, ref, _now())
                            if _table(con, 'web_case_cascaded_grants'):
                                con.execute('DELETE FROM web_case_cascaded_grants WHERE template_ref=?', (ref,))
                        elif _table(con, 'web_case_cascaded_grants'):
                            con.execute('DELETE FROM web_case_cascaded_grants WHERE resource_kind=? AND resource_ref=?', (kind, ref))
                        for tbl in ('grants', 'component_policies'):
                            if _table(con, tbl):
                                con.execute(f'DELETE FROM {tbl} WHERE kind=? AND ref=?', (kind, ref))
                        deleted_log.append({'kind': kind, 'ref': ref, 'name': ref})

                for item in [i for i in selected_items if i['kind'] == 'derived_dataset']:
                    did = item['ref']
                    if _table(con, 'web_derived_review_grants'):
                        con.execute('DELETE FROM web_derived_review_grants WHERE dataset_id=?', (did,))
                    if _table(con, 'web_preserved_dataset_grants'):
                        con.execute('DELETE FROM web_preserved_dataset_grants WHERE resource_ref=?', (did,))
                    if _table(con, 'web_case_cascaded_grants'):
                        con.execute("DELETE FROM web_case_cascaded_grants WHERE resource_kind='dataset' AND resource_ref=?", (did,))
                    if _table(con, 'web_derived_legacy_source_grants'):
                        con.execute('DELETE FROM web_derived_legacy_source_grants WHERE dataset_id=?', (did,))
                    if _table(con, 'web_derived_datasets'):
                        con.execute('DELETE FROM web_derived_datasets WHERE dataset_id=?', (did,))
                    if _table(con, 'web_dataset_versions'):
                        con.execute('DELETE FROM web_dataset_versions WHERE dataset_id=? OR parent_dataset_id=?', (did, did))
                    if _table(con, 'web_job_staged_artifacts'):
                        con.execute('UPDATE web_job_staged_artifacts SET retained_dataset_id=NULL WHERE retained_dataset_id=?', (did,))
                    if _table(con, 'web_asset_lifecycle'):
                        con.execute("DELETE FROM web_asset_lifecycle WHERE kind='dataset' AND ref=?", (did,))
                    if _table(con, 'web_asset_lifecycle_history'):
                        con.execute("DELETE FROM web_asset_lifecycle_history WHERE kind='dataset' AND ref=?", (did,))
                    if _table(con, 'web_cleanup_deleted'):
                        con.execute("DELETE FROM web_cleanup_deleted WHERE kind='dataset' AND ref=?", (did,))
                    for tbl in ('grants', 'component_policies'):
                        if _table(con, tbl):
                            con.execute(f"DELETE FROM {tbl} WHERE kind='dataset' AND ref=?", (did,))
                    deleted_log.append({'kind': 'derived_dataset', 'ref': did, 'name': item['name']})

                reset_count = 0
                if ctx['task_links']:
                    con.execute('DELETE FROM project_trusted_sources WHERE project_id=?', (pid,))
                    if reset_task:
                        cur = con.execute(
                            "UPDATE distributions SET status='pending', project_id=NULL WHERE project_id=?",
                            (pid,),
                        )
                        reset_count += cur.rowcount
                        if _table(con, 'web_task_recipients'):
                            cur2 = con.execute(
                                'UPDATE web_task_recipients SET project_id=NULL, project_name=NULL, resource_claim_status=NULL WHERE project_id=?',
                                (pid,),
                            )
                            reset_count += cur2.rowcount
                        deleted_log.append({'kind': 'task_reset', 'ref': pid, 'name': f'已重置 {reset_count} 条任务领取记录为待领取'})
                    else:
                        con.execute('UPDATE distributions SET project_id=NULL WHERE project_id=?', (pid,))
                        if _table(con, 'web_task_recipients'):
                            con.execute('UPDATE web_task_recipients SET project_id=NULL WHERE project_id=?', (pid,))

                run_ids = ctx['run_ids']
                all_sub_ids = ctx['all_sub_ids']
                if all_sub_ids and _table(con, 'web_asset_candidate_validations'):
                    smarks = ','.join('?' for _ in all_sub_ids)
                    con.execute(f'DELETE FROM web_asset_candidate_validations WHERE submission_id IN ({smarks})', all_sub_ids)
                if run_ids:
                    rmarks = ','.join('?' for _ in run_ids)
                    if _table(con, 'web_asset_candidate_validations'):
                        con.execute(f'DELETE FROM web_asset_candidate_validations WHERE run_id IN ({rmarks})', run_ids)
                    for tbl in ('web_job_artifacts', 'web_job_staged_artifacts', 'web_job_artifact_retention'):
                        if _table(con, tbl):
                            con.execute(f'DELETE FROM {tbl} WHERE run_id IN ({rmarks})', run_ids)
                    con.execute(f'DELETE FROM web_jobs WHERE run_id IN ({rmarks})', run_ids)
                    deleted_log.append({'kind': 'project_jobs', 'ref': pid, 'name': f'运行记录 {len(run_ids)} 条'})

                if _table(con, 'web_submission_preparations'):
                    con.execute('DELETE FROM web_submission_preparations WHERE source_project_id=?', (pid,))
                if _table(con, 'web_direct_submission_receipts'):
                    con.execute('DELETE FROM web_direct_submission_receipts WHERE source_project_id=?', (pid,))
                if _table(con, 'web_derived_legacy_source_grants'):
                    con.execute('DELETE FROM web_derived_legacy_source_grants WHERE source_project_id=?', (pid,))
                if all_sub_ids:
                    smarks = ','.join('?' for _ in all_sub_ids)
                    for tbl in (
                        'web_direct_review_publications',
                        'web_derived_review_grants',
                        'web_assignment_baselines',
                        'web_asset_validation_snapshots',
                    ):
                        if _table(con, tbl):
                            con.execute(f'DELETE FROM {tbl} WHERE submission_id IN ({smarks})', all_sub_ids)
                    if _table(con, 'web_derived_datasets'):
                        con.execute(
                            f'UPDATE web_derived_datasets SET source_submission_id=NULL WHERE source_submission_id IN ({smarks})',
                            all_sub_ids,
                        )
                    if run_ids and _table(con, 'web_derived_datasets'):
                        rmarks = ','.join('?' for _ in run_ids)
                        con.execute(
                            f'UPDATE web_derived_datasets SET source_run_id=NULL WHERE source_run_id IN ({rmarks})',
                            run_ids,
                        )
                    con.execute(f'DELETE FROM submission_files WHERE submission_id IN ({smarks})', all_sub_ids)
                    con.execute(f'UPDATE submissions SET parent_submission_id=NULL WHERE id IN ({smarks})', all_sub_ids)
                    con.execute(f'DELETE FROM submissions WHERE id IN ({smarks})', all_sub_ids)
                    deleted_log.append({'kind': 'project_submissions', 'ref': pid, 'name': f'版本提交 {len(ctx["submission_ids"])} 条'})

                for tbl in (
                    'project_trusted_sources',
                    'project_package_sources',
                    'package_import_receipts',
                    'web_assignment_baselines',
                    'web_project_usage',
                ):
                    if _table(con, tbl):
                        con.execute(f'DELETE FROM {tbl} WHERE project_id=?', (pid,))
                con.execute('DELETE FROM projects WHERE id=?', (pid,))
                for spid in ctx['snapshot_project_ids']:
                    con.execute('DELETE FROM projects WHERE id=?', (spid,))
                deleted_log.append({'kind': 'project_record', 'ref': pid, 'name': str(project_row['name'])})

                now = _now()
                con.execute(
                    '''INSERT INTO web_project_purge_audit(
                        audit_id, project_id, project_name, owner_username,
                        actor_uid, actor_username, plan_token, status,
                        deleted_json, retained_json, failed_json, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        audit_id, pid, str(project_row['name']), str(project_row['owner_username']),
                        actor_uid, actor_username, plan_token, 'completed',
                        _json(deleted_log), _json(retained_summary), _json(failed_log), now,
                    ),
                )
                staged_entries_db = (
                    [
                        {
                            'orig_rel': b.resolve().relative_to(self.derived.root.resolve()).as_posix(),
                            'holding_name': h.name,
                        }
                        for b, h in staged_blob_removals
                    ]
                    if self.derived is not None else []
                )
                con.execute(
                    '''INSERT INTO web_project_purge_recovery(
                        audit_id, project_id, phase, staged_blobs_json, deleted_json, failed_json, updated_at
                    ) VALUES(?,?,?,?,?,?,?)''',
                    (
                        audit_id, pid, 'db_committed',
                        _json(staged_entries_db),
                        _json(deleted_log), _json(failed_log), now,
                    ),
                )
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                for original, holding in reversed(staged_blob_removals):
                    if os.path.lexists(holding) and not os.path.lexists(original):
                        try:
                            os.rename(holding, original)
                        except OSError:
                            pass
                self._remove_recovery_file(audit_id)
                raise

        for _original, holding in staged_blob_removals:
            try:
                if os.path.lexists(holding):
                    os.unlink(holding)
            except OSError as exc:
                failed_log.append({'kind': 'derived_blob', 'ref': holding.name, 'error': str(exc)})

        final_status = 'completed' if not failed_log else 'partial'
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                if failed_log:
                    con.execute(
                        'UPDATE web_project_purge_audit SET status=?, failed_json=? WHERE audit_id=?',
                        (final_status, _json(failed_log), audit_id),
                    )
                con.execute('DELETE FROM web_project_purge_recovery WHERE audit_id=?', (audit_id,))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        self._remove_recovery_file(audit_id)

        return {
            'audit_id': audit_id,
            'project_id': pid,
            'project_name': str(project_row['name']),
            'status': final_status,
            'deleted': deleted_log,
            'retained': retained_summary,
            'failed': failed_log,
            'created_at': now,
        }

    def update_audit(
        self,
        principal: Principal,
        audit_id: object,
        *,
        deleted_extra: object = None,
        failed_extra: object = None,
    ) -> dict[str, object]:
        _, _, is_teacher = _require_principal(principal)
        if not is_teacher:
            raise KitError('只有管理员可以更新项目清理审计记录')
        if not isinstance(audit_id, str) or not audit_id:
            raise KitError('审计记录 ID 无效')
        if deleted_extra is not None and type(deleted_extra) is not list:
            raise KitError('deleted_extra 必须是列表')
        if failed_extra is not None and type(failed_extra) is not list:
            raise KitError('failed_extra 必须是列表')
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                row = con.execute('SELECT * FROM web_project_purge_audit WHERE audit_id=?', (audit_id,)).fetchone()
                if row is None:
                    raise KitError('审计记录不存在')
                deleted = json.loads(row['deleted_json'])
                failed = json.loads(row['failed_json'])
                if deleted_extra:
                    deleted.extend(deleted_extra)
                if failed_extra:
                    failed.extend(failed_extra)
                status = 'completed' if not failed else 'partial'
                con.execute(
                    'UPDATE web_project_purge_audit SET status=?, deleted_json=?, failed_json=? WHERE audit_id=?',
                    (status, _json(deleted), _json(failed), audit_id),
                )
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        return {'audit_id': audit_id, 'status': status, 'deleted': deleted, 'failed': failed}

    def list_audit(self, principal: Principal, *, limit: int = 30) -> list[dict[str, object]]:
        uid, username, is_teacher = _require_principal(principal)
        if not is_teacher:
            raise KitError('只有管理员可查看平台项目清理审计记录')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise KitError('审计记录数量限制无效')
        with self.store._connection() as con:
            if not _table(con, 'web_project_purge_audit'):
                return []
            if is_teacher:
                rows = con.execute(
                    'SELECT * FROM web_project_purge_audit ORDER BY created_at DESC, audit_id DESC LIMIT ?',
                    (limit,),
                ).fetchall()
            else:
                rows = con.execute(
                    'SELECT * FROM web_project_purge_audit WHERE owner_username=? OR (actor_uid=? AND actor_username=?) '
                    'ORDER BY created_at DESC, audit_id DESC LIMIT ?',
                    (username, uid, username, limit),
                ).fetchall()
            return [
                {
                    'audit_id': str(r['audit_id']),
                    'project_id': str(r['project_id']),
                    'project_name': str(r['project_name']),
                    'owner_username': str(r['owner_username']),
                    'actor_username': str(r['actor_username']),
                    'status': str(r['status']),
                    'deleted': json.loads(r['deleted_json']),
                    'retained': json.loads(r['retained_json']),
                    'failed': json.loads(r['failed_json']),
                    'created_at': str(r['created_at']),
                }
                for r in rows
            ]
