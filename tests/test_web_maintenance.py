from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit import web_maintenance


def _policy(tmp_path: Path):
    store = tmp_path / 'store'
    store.mkdir()
    return SimpleNamespace(
        broker_uid=os.geteuid(), source=tmp_path / 'policy.json',
        submission_store=store, code_root=tmp_path / 'app',
    )


def test_report_is_read_only_and_identifies_unmigrated_store(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    database = policy.submission_store / 'submissions.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE submission_files (id TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, content BLOB)')
    connection.commit()
    connection.close()
    database.chmod(0o600)
    before = database.read_bytes()
    monkeypatch.setattr(web_maintenance, 'load_policy', lambda path: policy)
    monkeypatch.setattr(web_maintenance, 'validate_policy', lambda *args, **kwargs: None)

    result = web_maintenance.report('ignored.json')

    assert result['migration_required']
    assert result['database_status']['tables']['submission_files'] == 'table'
    assert database.read_bytes() == before


def test_report_refuses_a_wrong_service_uid(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    policy.broker_uid += 1
    monkeypatch.setattr(web_maintenance, 'load_policy', lambda path: policy)
    monkeypatch.setattr(web_maintenance, 'validate_policy', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='broker'):
        web_maintenance.report('ignored.json')


def test_migrate_uses_all_central_initializers(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    calls = []
    store = object()

    class Library:
        def __init__(self, actual_store, root):
            assert actual_store is store
            assert root == policy.code_root / 'library'
            calls.append('library')
            self.central = SimpleNamespace(report=lambda: {'imported': True, 'assets': 2, 'contracts': 1})

    monkeypatch.setattr(web_maintenance, 'load_policy', lambda path: policy)
    monkeypatch.setattr(web_maintenance, 'validate_policy', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_maintenance, 'SubmissionStore', lambda path: store)
    monkeypatch.setattr(web_maintenance, 'AssetLibrary', Library)
    monkeypatch.setattr(web_maintenance, 'Jobs', lambda actual_store: calls.append('jobs'))
    monkeypatch.setattr(web_maintenance, 'SubmissionFlow', lambda actual_store, root: calls.append('submissions'))
    monkeypatch.setattr(web_maintenance, 'TaskDelivery', lambda actual_store: calls.append('tasks'))
    monkeypatch.setattr(web_maintenance, 'ReviewFlow', lambda actual_store, root: calls.append('review'))
    monkeypatch.setattr(web_maintenance.CentralLibrary, 'already_imported', lambda actual_store: True)
    monkeypatch.setattr(web_maintenance, 'capacity', lambda actual_store: {'unique_bytes': 4})

    result = web_maintenance.migrate('ignored.json')

    assert calls == ['library', 'jobs', 'submissions', 'tasks', 'review']
    assert result['migrated'] is True
    assert result['central_library_imported'] is True
    assert result['capacity'] == {'unique_bytes': 4}


def test_retirement_requires_explicit_migrate_option(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    calls = []
    store = object()

    class Library:
        def __init__(self, actual_store, root):
            self.central = SimpleNamespace(report=lambda: {})

    monkeypatch.setattr(web_maintenance, 'load_policy', lambda path: policy)
    monkeypatch.setattr(web_maintenance, 'validate_policy', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_maintenance, 'SubmissionStore', lambda path: store)
    monkeypatch.setattr(web_maintenance, 'AssetLibrary', Library)
    monkeypatch.setattr(web_maintenance, 'Jobs', lambda actual_store: calls.append('jobs'))
    monkeypatch.setattr(web_maintenance, 'SubmissionFlow', lambda actual_store, root: calls.append('submissions'))
    monkeypatch.setattr(web_maintenance, 'TaskDelivery', lambda actual_store: calls.append('tasks'))
    monkeypatch.setattr(web_maintenance, 'ReviewFlow', lambda actual_store, root: calls.append('review'))
    monkeypatch.setattr(web_maintenance.CentralLibrary, 'already_imported', lambda actual_store: True)
    monkeypatch.setattr(web_maintenance, 'capacity', lambda actual_store: {})
    monkeypatch.setattr(web_maintenance, 'retire_web_execution',
                        lambda actual_store, *, dry_run: calls.append(('retire', dry_run)) or {'web_jobs': 2})

    ordinary = web_maintenance.migrate('ignored.json')
    assert 'web_execution_retirement' not in ordinary
    retired = web_maintenance.migrate('ignored.json', retire_web_execution_records=True)
    assert calls[-1] == ('retire', False)
    assert retired['web_execution_retirement'] == {'web_jobs': 2}

    with pytest.raises(SystemExit):
        web_maintenance.main(['--policy', 'ignored.json', '--retire-web-execution'])
