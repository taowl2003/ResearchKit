"""One-way retirement of the former Web execution queue.

The caller is responsible for stopping the broker and making a complete backup
before calling this module.  It deliberately defaults to a dry run, and only
deletes snapshots whose relationship to a preparation receipt proves that they
are service-created temporary snapshots.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files import KitError
from .web_store import SubmissionStore


RUNNER_STATE_ROOT = Path('/var/lib/researchkit-runner')
_RUNNER_TEMPORARY = re.compile(r'^(?:job-|probe-|output-|staged-).+')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exists(connection: Any, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: Any, table: str) -> set[str]:
    return {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}


def _retirement_schema(connection: Any) -> None:
    """Install only the derived-data migration state required for retirement."""
    if not _exists(connection, 'web_derived_datasets'):
        return
    columns = _columns(connection, 'web_derived_datasets')
    if 'source_metadata_json' not in columns:
        connection.execute("ALTER TABLE web_derived_datasets ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute('''CREATE TABLE IF NOT EXISTS web_derived_legacy_source_grants (
        dataset_id TEXT NOT NULL REFERENCES web_derived_datasets(dataset_id),
        source_project_id TEXT NOT NULL REFERENCES projects(id),
        source_sha256 TEXT NOT NULL,
        teacher_uid INTEGER NOT NULL, teacher_username TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(dataset_id, source_project_id, source_sha256,
                    teacher_uid, teacher_username)
    )''')
    connection.execute('''CREATE INDEX IF NOT EXISTS web_derived_legacy_source_grants_lookup
        ON web_derived_legacy_source_grants(dataset_id, source_project_id, source_sha256)''')


def _migrate_publication_records(connection: Any) -> None:
    """Make legacy publication run columns nullable before clearing them."""
    if not _exists(connection, 'web_direct_review_publications'):
        return
    columns = _columns(connection, 'web_direct_review_publications')
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='web_direct_review_publications'").fetchone()
    needs_rebuild = (
        'review_type' not in columns
        or ('check_run_id' in columns and bool(next(row['notnull'] for row in connection.execute(
            'PRAGMA table_info(web_direct_review_publications)') if row['name'] == 'check_run_id')))
        or ('run_id' in columns and bool(next(row['notnull'] for row in connection.execute(
            'PRAGMA table_info(web_direct_review_publications)') if row['name'] == 'run_id')))
        or "'derived'" not in str(schema['sql'])
    )
    if not needs_rebuild:
        return
    connection.execute('ALTER TABLE web_direct_review_publications RENAME TO _retired_old_publications')
    connection.execute('''CREATE TABLE web_direct_review_publications (
        submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('template','algorithm','derived')),
        alias TEXT NOT NULL, ref TEXT NOT NULL, candidate_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('published','reused','failed')),
        reviewer_uid INTEGER NOT NULL, reviewer_username TEXT NOT NULL,
        check_run_id TEXT, run_id TEXT,
        review_type TEXT NOT NULL CHECK(review_type IN ('manual','legacy_execution')),
        review_note TEXT NOT NULL, published_at TEXT NOT NULL, error TEXT,
        PRIMARY KEY(submission_id,kind,alias,ref)
    )''')
    old = _columns(connection, '_retired_old_publications')
    def value(name: str, default: str) -> str:
        return name if name in old else default
    target = ('submission_id,source_sha256,kind,alias,ref,candidate_id,status,reviewer_uid,reviewer_username,'
              'check_run_id,run_id,review_type,review_note,published_at,error')
    select = ','.join((
        value('submission_id', "''"), value('source_sha256', "''"), value('kind', "'template'"),
        value('alias', "''"), value('ref', "''"), value('candidate_id', 'NULL'),
        value('status', "'failed'"), value('reviewer_uid', '0'), value('reviewer_username', "''"),
        value('check_run_id', 'NULL'), value('run_id', 'NULL'),
        value('review_type', "'legacy_execution'"), value('review_note', "''"),
        value('published_at', "''"), value('error', 'NULL'),
    ))
    connection.execute(f'INSERT INTO web_direct_review_publications({target}) SELECT {select} FROM _retired_old_publications')
    connection.execute('DROP TABLE _retired_old_publications')


def clean_runner_temporary_files(*, dry_run: bool = True, runner_uid: int, root: Path | None = None) -> dict[str, object]:
    """Remove only retired runner state from its fixed, policy-owned directory.

    This API is intentionally separate from broker maintenance: the normal
    runner directory is mode 0700 and owned by the runner account.  A caller
    must stop that service and execute this function as that account (or an
    equivalently privileged administrator) after passing the policy UID.
    """
    if not isinstance(dry_run, bool) or isinstance(runner_uid, bool) or not isinstance(runner_uid, int) or runner_uid < 1:
        raise KitError('执行临时目录清理参数无效')
    actual = RUNNER_STATE_ROOT if root is None else Path(root)
    if actual != RUNNER_STATE_ROOT or not actual.is_absolute():
        raise KitError('只能清理策略指定的执行临时目录')
    try:
        info = os.lstat(actual)
    except FileNotFoundError:
        return {'dry_run': dry_run, 'runner_temporary_paths': [], 'runner_temporary_files': 0}
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != runner_uid or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise KitError('执行临时目录所有权、权限或路径不可信')
    candidates = [path for path in actual.iterdir() if _RUNNER_TEMPORARY.fullmatch(path.name)]
    for candidate in candidates:
        candidate_info = os.lstat(candidate)
        if stat.S_ISLNK(candidate_info.st_mode):
            raise KitError('执行临时目录包含符号链接，拒绝清理')
        if not (stat.S_ISDIR(candidate_info.st_mode) or stat.S_ISREG(candidate_info.st_mode)):
            raise KitError('执行临时目录包含非普通文件或目录，拒绝清理')
        walk = os.walk(candidate, topdown=True, followlinks=False) if stat.S_ISDIR(candidate_info.st_mode) else []
        for parent, directories, files in walk:
            if any((Path(parent) / name).is_symlink() for name in [*directories, *files]):
                raise KitError('执行临时目录包含符号链接，拒绝清理')
    paths = [str(path) for path in candidates]
    if not dry_run:
        for candidate in candidates:
            info = os.lstat(candidate)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(candidate)
            elif stat.S_ISREG(info.st_mode):
                candidate.unlink()
            else:
                raise KitError('执行临时目录包含非普通文件或目录，拒绝清理')
    return {'dry_run': dry_run, 'runner_temporary_paths': paths, 'runner_temporary_files': len(paths)}


class WebExecutionRetirement:
    """Inspect and optionally remove legacy Web execution records."""

    def __init__(self, store: SubmissionStore):
        if not isinstance(store, SubmissionStore):
            raise KitError('执行记录退役需要 SubmissionStore')
        self.store = store

    @staticmethod
    def _durable_submission_reference(connection: Any, submission_id: str) -> str | None:
        """Return a non-execution record that makes snapshot deletion unsafe."""
        exempt = {
            'submission_files', 'web_content_refs_submission_files', 'web_jobs',
            'web_submission_preparations', 'web_derived_review_grants',
            'web_asset_validation_snapshots', 'web_asset_candidate_validations',
        }
        for table_row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            table = str(table_row['name'])
            if table.startswith('sqlite_') or table in exempt or not re.fullmatch(r'[a-z][a-z0-9_]*', table):
                continue
            for foreign in connection.execute(f'PRAGMA foreign_key_list({table})').fetchall():
                if foreign['table'] != 'submissions' or not re.fullmatch(r'[a-z][a-z0-9_]*', foreign['from']):
                    continue
                if connection.execute(f'SELECT 1 FROM {table} WHERE {foreign["from"]}=? LIMIT 1',
                                      (submission_id,)).fetchone() is not None:
                    return table
        # Older review/publication schemas have no foreign key.  They are
        # nevertheless durable publication evidence and must never be deleted.
        for table, column in (('web_asset_candidates', 'submission_id'),
                              ('web_direct_review_publications', 'submission_id'),
                              ('web_direct_submission_receipts', 'final_submission_id')):
            if _exists(connection, table) and column in _columns(connection, table):
                if connection.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                                      (submission_id,)).fetchone() is not None:
                    return table
        return None

    @classmethod
    def _plan(cls, connection: Any) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
        tables = ('web_content_refs_web_job_artifacts', 'web_job_artifact_retention',
                  'web_job_artifacts', 'web_job_staged_artifacts', 'web_job_worker', 'web_jobs',
                  'web_asset_candidate_validations', 'web_asset_validation_snapshots')
        report = {table: 0 for table in tables}
        for table in tables:
            if _exists(connection, table):
                report[table] = int(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])

        confirmed: list[dict[str, Any]] = []
        unsafe: list[dict[str, Any]] = []
        if _exists(connection, 'web_submission_preparations'):
            rows = connection.execute('''SELECT p.preparation_id,p.snapshot_id,p.source_project_id,
                p.final_submission_id,p.owner_uid,p.owner_username,p.snapshot_sha256,
                s.project_id AS snapshot_project_id,project.owner_uid AS snapshot_owner_uid,
                project.owner_username AS snapshot_owner_username,s.sequence,s.version_major,s.version_minor,
                s.kind,s.parent_submission_id,s.content_sha256,project.source AS snapshot_project_source,
                (SELECT COUNT(*) FROM submissions other WHERE other.project_id=s.project_id) AS snapshot_project_submissions
                FROM web_submission_preparations p
                LEFT JOIN submissions s ON s.id=p.snapshot_id
                LEFT JOIN projects project ON project.id=s.project_id''').fetchall()
            for row in rows:
                item = {key: row[key] for key in row.keys()}
                if row['snapshot_id'] is None:
                    confirmed.append(item)
                else:
                    reason = None
                    if row['snapshot_project_id'] is None:
                        # Successful legacy promotion already removed its private
                        # snapshot. Only a receipt is left, so there are no
                        # snapshot bytes to risk deleting.
                        item['snapshot_already_removed'] = True
                    elif row['snapshot_project_id'] == row['source_project_id']:
                        reason = 'snapshot_is_source_project'
                    elif row['snapshot_project_source'] != 'self-created':
                        reason = 'snapshot_project_source'
                    elif (row['snapshot_owner_uid'], row['snapshot_owner_username']) != (row['owner_uid'], row['owner_username']):
                        reason = 'snapshot_owner'
                    elif (row['sequence'], row['version_major'], row['version_minor'], row['kind'], row['parent_submission_id']) != (1, 0, 0, 'minor', None):
                        reason = 'snapshot_shape'
                    elif row['snapshot_project_submissions'] != 1:
                        reason = 'snapshot_project_has_other_submissions'
                    elif row['snapshot_sha256'] and row['snapshot_sha256'] != row['content_sha256']:
                        reason = 'snapshot_hash'
                    else:
                        reference = cls._durable_submission_reference(connection, str(row['snapshot_id']))
                        if reference is not None:
                            reason = 'durable_reference:' + reference
                    if reason is None:
                        confirmed.append(item)
                    else:
                        item['reason'] = reason
                        unsafe.append(item)
        report['confirmed_preparations'] = len(confirmed)
        report['unsafe_preparations'] = len(unsafe)
        report['temporary_submission_files'] = sum(
            int(connection.execute('SELECT COUNT(*) FROM submission_files WHERE submission_id=?',
                                   (item['snapshot_id'],)).fetchone()[0])
            for item in confirmed if item.get('snapshot_id')
        )
        return report, confirmed, unsafe

    @staticmethod
    def _preserve_derived_authorization(connection: Any, confirmed: list[dict[str, Any]]) -> int:
        if not (_exists(connection, 'web_derived_review_grants') and _exists(connection, 'web_derived_datasets')):
            return 0
        snapshot_ids = [str(row['snapshot_id']) for row in confirmed if row.get('snapshot_id')]
        if not snapshot_ids:
            return 0
        marks = ','.join('?' for _ in snapshot_ids)
        rows = connection.execute(f'''SELECT g.dataset_id,p.source_project_id,s.content_sha256,
            g.teacher_uid,g.teacher_username,g.created_at
            FROM web_derived_review_grants g
            JOIN web_submission_preparations p ON p.snapshot_id=g.submission_id
            JOIN submissions s ON s.id=p.snapshot_id
            WHERE g.submission_id IN ({marks})''', snapshot_ids).fetchall()
        for row in rows:
            connection.execute('''INSERT OR IGNORE INTO web_derived_legacy_source_grants(
                dataset_id,source_project_id,source_sha256,teacher_uid,teacher_username,created_at)
                VALUES(?,?,?,?,?,?)''', tuple(row))
        if rows:
            connection.execute(f'DELETE FROM web_derived_review_grants WHERE submission_id IN ({marks})', snapshot_ids)
        return len(rows)

    @staticmethod
    def _retire_derived_provenance(connection: Any, confirmed: list[dict[str, Any]]) -> int:
        if not _exists(connection, 'web_derived_datasets'):
            return 0
        snapshots = {str(item['snapshot_id']): item for item in confirmed if item.get('snapshot_id')}
        rows = connection.execute('''SELECT dataset_id,source_run_id,source_submission_id,source_metadata_json
            FROM web_derived_datasets WHERE source_run_id IS NOT NULL''').fetchall()
        count = 0
        for row in rows:
            metadata: dict[str, Any]
            try:
                loaded = json.loads(row['source_metadata_json'] or '{}')
                metadata = loaded if isinstance(loaded, dict) else {}
            except (TypeError, ValueError):
                metadata = {}
            source_id = row['source_submission_id']
            snapshot = snapshots.get(str(source_id)) if source_id is not None else None
            retired: dict[str, Any] = {'reviewed': True, 'retired_at': _now()}
            replacement = source_id
            if snapshot is not None:
                retired['source_project_id'] = snapshot['source_project_id']
                # A final submission is durable provenance.  Otherwise retain
                # only the project/content authorization mapping, never a run id.
                replacement = snapshot['final_submission_id']
                if replacement is None:
                    source = connection.execute('SELECT content_sha256 FROM submissions WHERE id=?', (source_id,)).fetchone()
                    if source is not None:
                        retired['source_sha256'] = source['content_sha256']
            metadata['legacy_execution'] = retired
            connection.execute('''UPDATE web_derived_datasets
                SET source_run_id=NULL, source_submission_id=?, source_metadata_json=?
                WHERE dataset_id=?''', (replacement, json.dumps(metadata, ensure_ascii=False, sort_keys=True), row['dataset_id']))
            count += 1
        return count

    def retire(self, *, dry_run: bool = True) -> dict[str, Any]:
        if not isinstance(dry_run, bool):
            raise KitError('dry_run 必须为布尔值')
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                # Compute the plan while holding the same writer lock used for
                # deletion; a runner cannot add a record between inspection and
                # retirement once services have been stopped.
                report, confirmed, unsafe = self._plan(connection)
                report['dry_run'] = dry_run
                report['unsafe_preparation_ids'] = [item['preparation_id'] for item in unsafe]
                report['unsafe_preparations_detail'] = [
                    {'preparation_id': item['preparation_id'], 'reason': item.get('reason', 'unknown')}
                    for item in unsafe
                ]
                if dry_run:
                    connection.execute('ROLLBACK')
                    return report
                if unsafe:
                    raise KitError('存在无法确认的临时准备快照；未执行删除：' + ', '.join(report['unsafe_preparation_ids']))
                _retirement_schema(connection)
                _migrate_publication_records(connection)
                report['converted_derived_grants'] = self._preserve_derived_authorization(connection, confirmed)
                report['retired_derived_provenance'] = self._retire_derived_provenance(connection, confirmed)
                if _exists(connection, 'web_direct_review_publications'):
                    columns = _columns(connection, 'web_direct_review_publications')
                    unset = [column for column in ('check_run_id', 'run_id') if column in columns]
                    if unset:
                        connection.execute('UPDATE web_direct_review_publications SET ' + ', '.join(f'{column}=NULL' for column in unset))
                # Children must go first; SQLite foreign keys are enabled by
                # SubmissionStore connections.
                # Candidate validation records reference jobs and their
                # service-only snapshots.  They are execution evidence, never
                # a published asset, so remove them before the queue.
                validation_ids: list[str] = []
                if _exists(connection, 'web_asset_validation_snapshots'):
                    validation_ids = [str(row['submission_id']) for row in connection.execute(
                        'SELECT submission_id FROM web_asset_validation_snapshots').fetchall()]
                if _exists(connection, 'web_asset_candidate_validations'):
                    connection.execute('DELETE FROM web_asset_candidate_validations')
                if _exists(connection, 'web_asset_validation_snapshots'):
                    connection.execute('DELETE FROM web_asset_validation_snapshots')
                for table in ('web_content_refs_web_job_artifacts', 'web_job_artifact_retention',
                              'web_job_artifacts', 'web_job_staged_artifacts', 'web_job_worker', 'web_jobs'):
                    if _exists(connection, table):
                        connection.execute(f'DELETE FROM {table}')
                snapshot_ids = [str(item['snapshot_id']) for item in confirmed if item.get('snapshot_id')]
                if _exists(connection, 'web_submission_preparations'):
                    connection.execute('DELETE FROM web_submission_preparations')
                delete_ids = [*snapshot_ids, *validation_ids]
                if delete_ids:
                    marks = ','.join('?' for _ in delete_ids)
                    connection.execute(f'DELETE FROM submission_files WHERE submission_id IN ({marks})', delete_ids)
                    projects = connection.execute(f'''SELECT DISTINCT project_id FROM submissions
                        WHERE id IN ({marks})''', delete_ids).fetchall()
                    connection.execute(f'DELETE FROM submissions WHERE id IN ({marks})', delete_ids)
                    for project in projects:
                        connection.execute('DELETE FROM projects WHERE id=? AND NOT EXISTS '
                                           '(SELECT 1 FROM submissions WHERE project_id=?)',
                                           (project['project_id'], project['project_id']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return report


def retire_web_execution(store: SubmissionStore, *, dry_run: bool = True) -> dict[str, Any]:
    """Convenience entry point used by the installer after backup and shutdown."""
    return WebExecutionRetirement(store).retire(dry_run=dry_run)


def main(argv=None) -> int:
    """Post-update cleanup, run as the stopped runner account, never as root."""
    import argparse
    from .web_identity import load_policy
    parser = argparse.ArgumentParser(description='inspect/remove the retired Web runner temporary files')
    parser.add_argument('--policy', required=True)
    parser.add_argument('--apply', action='store_true', help='remove vetted temporary files; default is read-only')
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if policy.runner_uid is None:
            result = {'runner_temporary_files': 0, 'reason': 'no runner configured', 'dry_run': not args.apply}
        else:
            if os.geteuid() != policy.runner_uid or os.getuid() != policy.runner_uid:
                raise KitError('请以已停止的 runner 服务账号执行临时文件清理，不能以 root 直接清理')
            result = clean_runner_temporary_files(dry_run=not args.apply, runner_uid=policy.runner_uid)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KitError, OSError, ValueError) as exc:
        print(f'旧执行临时文件未清理：{exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
