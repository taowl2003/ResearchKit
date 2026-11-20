"""Offline maintenance entry point for the Web submission store.

This module is deliberately run by the dedicated broker account while the
broker is stopped.  The administrator-facing install and update tools invoke
it through the installed virtual environment; they do not import application
code through the system Python used to run those tools.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

from .web_asset_library import AssetLibrary
from .web_central_library import CentralLibrary
from .web_content import capacity
from .web_execution_retirement import retire_web_execution
from .web_identity import Policy, load_policy, validate_policy
from .web_jobs import Jobs
from .web_store import SubmissionStore
from .web_submission_flow import SubmissionFlow
from .web_task_delivery import TaskDelivery
from .web_review_flow import ReviewFlow


_FILE_TABLES = (
    'submission_files', 'web_asset_candidate_files', 'web_asset_published_files',
    'web_asset_contracts', 'web_job_artifacts', 'web_central_builtin_files',
    'web_central_builtin_contracts',
)


def _require_broker(policy: Policy) -> None:
    """Ensure this maintenance process cannot be run under an arbitrary UID."""
    if os.geteuid() != policy.broker_uid or os.getuid() != policy.broker_uid:
        raise RuntimeError('维护程序必须以 broker 服务账号运行')


def _safe_database(path: Path, owner_uid: int) -> dict[str, object]:
    """Read only enough metadata to make the default report side-effect free."""
    result: dict[str, object] = {'exists': False, 'tables': {}, 'content_store': False}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return result
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('提交数据库不是受保护的普通 0600 文件')
    result['exists'] = True
    try:
        connection = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)
        try:
            rows = connection.execute(
                "SELECT name,type FROM sqlite_master WHERE name IN (%s)" % ','.join('?' * len(_FILE_TABLES)),
                _FILE_TABLES,
            ).fetchall()
            kinds = {str(name): str(kind) for name, kind in rows}
            result['tables'] = {name: kinds.get(name, 'missing') for name in _FILE_TABLES}
            result['content_store'] = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_content_blobs'"
            ).fetchone() is not None
            if result['content_store']:
                logical, unique_bytes, references, files = connection.execute(
                    '''SELECT COALESCE(SUM(size*ref_count),0), COALESCE(SUM(size),0),
                              COALESCE(SUM(ref_count),0), COUNT(*) FROM web_content_blobs'''
                ).fetchone()
                page_count = connection.execute('PRAGMA page_count').fetchone()[0]
                page_size = connection.execute('PRAGMA page_size').fetchone()[0]
                free_pages = connection.execute('PRAGMA freelist_count').fetchone()[0]
                result['capacity'] = {
                    'logical_bytes': int(logical), 'unique_bytes': int(unique_bytes),
                    'saved_bytes': int(logical) - int(unique_bytes),
                    'file_references': int(references), 'unique_files': int(files),
                    'database_bytes': int(page_count) * int(page_size),
                    'reclaimable_bytes': int(free_pages) * int(page_size),
                }
            result['central_library_imported'] = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_central_library_state'"
            ).fetchone() is not None and connection.execute(
                "SELECT 1 FROM web_central_library_state WHERE marker='builtin-import-v1'"
            ).fetchone() is not None
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError('无法只读检查提交数据库: %s' % exc) from exc
    return result


def report(policy_path: str | Path) -> dict[str, object]:
    """Return a validation report without initializing or changing the store."""
    policy = load_policy(policy_path)
    validate_policy(policy, validate_workspaces=False)
    _require_broker(policy)
    database = policy.submission_store / 'submissions.sqlite3'
    status = _safe_database(database, policy.broker_uid)
    tables = status['tables'] if status['exists'] else {}
    migration_required = (not status['exists'] or not status['content_store']
                          or any(tables.get(name) != 'view' for name in _FILE_TABLES if tables.get(name) != 'missing')
                          or not status.get('central_library_imported', False))
    return {
        'schema': 'researchkit.web-maintenance/v1',
        'policy': str(policy.source or Path(policy_path).absolute()),
        'submission_store': str(policy.submission_store),
        'service_uid': policy.broker_uid,
        'database': str(database),
        'migration_required': migration_required,
        'database_status': status,
    }


def migrate(policy_path: str | Path, *, retire_web_execution_records: bool = False) -> dict[str, object]:
    """Initialize all central Web tables and import builtins exactly once."""
    policy = load_policy(policy_path)
    validate_policy(policy, validate_workspaces=False)
    _require_broker(policy)
    store = SubmissionStore(policy.submission_store)
    # AssetLibrary initializes candidate/published file compatibility views and
    # CentralLibrary makes the one-time trusted builtin copy. Jobs initializes
    # artifact storage. They share the store's transaction-safe content layer.
    library = AssetLibrary(store, policy.code_root / 'library')
    Jobs(store)
    SubmissionFlow(store, policy.code_root / 'library')
    TaskDelivery(store)
    ReviewFlow(store, policy.code_root / 'library')
    result = {
        'schema': 'researchkit.web-maintenance/v1',
        'policy': str(policy.source or Path(policy_path).absolute()),
        'submission_store': str(policy.submission_store),
        'service_uid': policy.broker_uid,
        'migrated': True,
        'central_library': library.central.report(),
        'central_library_imported': CentralLibrary.already_imported(store),
        'capacity': capacity(store),
    }
    if retire_web_execution_records:
        # ReviewFlow above has already made old publication run columns nullable.
        # This remains opt-in because it permanently removes service execution
        # evidence after the installer has stopped services and made its backup.
        result['web_execution_retirement'] = retire_web_execution(store, dry_run=False)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='inspect or migrate ResearchKit Web central storage')
    parser.add_argument('--policy', required=True, help='root-owned Web policy JSON')
    parser.add_argument('--migrate', action='store_true', help='perform the offline schema/library migration')
    parser.add_argument('--retire-web-execution', action='store_true',
                        help='with --migrate, permanently remove retired Web execution records')
    args = parser.parse_args(argv)
    try:
        if args.retire_web_execution and not args.migrate:
            parser.error('--retire-web-execution requires --migrate')
        result = (migrate(args.policy, retire_web_execution_records=args.retire_web_execution)
                  if args.migrate else report(args.policy))
    except Exception as exc:
        print('maintenance failed: %s' % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
