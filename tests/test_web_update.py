from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    'researchkit_update_web', Path(__file__).parents[1] / 'tools' / 'update-web.py'
)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
REAL_RUNNER_CONFIGURED = updater.runner_configured
REAL_PUBLISHER_PREFLIGHT = updater.publisher_preflight


@pytest.mark.parametrize('active', ['active', 'activating', 'failed'])
def test_data_publisher_must_be_stopped_before_code_update(monkeypatch, tmp_path, active):
    policy = tmp_path / 'policy.json'
    policy.write_text('{"dataset_management_config":"/etc/researchkit-web/data-management.json"}')
    monkeypatch.setattr(updater, 'POLICY', policy)
    monkeypatch.setattr(updater, 'path_errors', lambda *a, **k: [])
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=active))
    with pytest.raises(RuntimeError, match='systemctl stop researchkit-data-manager.service'):
        REAL_PUBLISHER_PREFLIGHT()


def test_data_publisher_inactive_and_legacy_policy_are_accepted(monkeypatch, tmp_path):
    policy = tmp_path / 'policy.json'
    policy.write_text('{"dataset_management_config":"/etc/researchkit-web/data-management.json"}')
    monkeypatch.setattr(updater, 'POLICY', policy)
    monkeypatch.setattr(updater, 'path_errors', lambda *a, **k: [])
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='inactive'))
    assert REAL_PUBLISHER_PREFLIGHT() is True
    policy.write_text('{}')
    assert REAL_PUBLISHER_PREFLIGHT() is False


@pytest.fixture(autouse=True)
def _no_runner_for_existing_update_cases(monkeypatch):
    """The historic upgrade tests model pre-runner policies."""
    monkeypatch.setattr(updater, 'runner_configured', lambda: False)
    monkeypatch.setattr(updater, 'publisher_preflight', lambda: False)


def _layout(monkeypatch, tmp_path: Path):
    root = tmp_path / 'source'
    source_package = root / 'core' / 'researchkit'
    source_package.mkdir(parents=True)
    (source_package / 'web_app.py').write_text('VALUE = 2\n', encoding='utf-8')
    lock = root / 'deploy' / 'requirements-web.lock'
    lock.parent.mkdir()
    lock.write_text('tornado==6.5.10\n', encoding='utf-8')
    install = tmp_path / 'install'
    package = install / 'app' / 'core' / 'researchkit'
    package.mkdir(parents=True)
    (package / 'web_app.py').write_text('VALUE = 1\n', encoding='utf-8')
    installed_lock = install / 'app' / 'deploy' / 'requirements-web.lock'
    installed_lock.parent.mkdir()
    installed_lock.write_text(lock.read_text(encoding='utf-8'), encoding='utf-8')
    monkeypatch.setattr(updater, 'ETC', tmp_path / 'etc')
    monkeypatch.setattr(updater, 'ROOT', root)
    monkeypatch.setattr(updater, 'INSTALL', install)
    monkeypatch.setattr(updater, 'APP', install / 'app')
    monkeypatch.setattr(updater, 'PACKAGE', package)
    monkeypatch.setattr(updater, 'LOCK', lock)
    monkeypatch.setattr(updater, 'INSTALLED_LOCK', installed_lock)
    monkeypatch.setattr(updater, 'UPDATE_LOCK', install / '.web-update.lock')
    # Update cases must never inspect the host installation's service data.
    # Most old-layout cases have no database, which intentionally exercises
    # the no-preexisting-store compatibility path.
    store = tmp_path / 'store'
    store.mkdir()
    monkeypatch.setattr(updater, 'STORE', store)
    return root, install, package


def test_check_has_no_side_effects(monkeypatch, tmp_path: Path):
    _root, install, _package = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, 'prerequisite_errors', lambda: ([], SimpleNamespace(pw_uid=222)))
    before = sorted(str(path.relative_to(install)) for path in install.rglob('*'))
    assert updater.update(argparse.Namespace(check=True, dry_run=False)) == 0
    after = sorted(str(path.relative_to(install)) for path in install.rglob('*'))
    assert after == before


def test_source_validation_rejects_symlink_and_hardlink(monkeypatch, tmp_path: Path):
    root, _install, _package = _layout(monkeypatch, tmp_path)
    target = root / 'core' / 'researchkit' / 'web_app.py'
    (root / 'core' / 'researchkit' / 'bad.py').symlink_to(target)
    assert any('bad.py' in error for error in updater.source_tree_errors())
    (root / 'core' / 'researchkit' / 'bad.py').unlink()
    os.link(target, root / 'core' / 'researchkit' / 'hard.py')
    assert any('hard.py' in error for error in updater.source_tree_errors())


def test_success_switches_package_keeps_unique_backup_and_cleans_staging(monkeypatch, tmp_path: Path):
    _root, install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, 'service_state', lambda: 'active')
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: None)
    monkeypatch.setattr(updater, '_start_and_verify', lambda uid: None)
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 0
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 2\n'
    backups = list((install / 'backups').iterdir())
    assert len(backups) == 1
    assert (backups[0] / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'
    assert not list(install.glob('.update-staging-*'))


def test_start_failure_restores_old_code_and_retains_evidence(monkeypatch, tmp_path: Path):
    _root, install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    states = iter(('active', 'inactive'))
    monkeypatch.setattr(updater, 'service_state', lambda: next(states, 'inactive'))
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: None)
    calls = {'start': 0}
    def start(uid):
        calls['start'] += 1
        if calls['start'] == 1:
            raise RuntimeError('new broker fails')
    monkeypatch.setattr(updater, '_start_and_verify', start)
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'
    assert list((install / 'backups').glob('researchkit-*'))
    assert list((install / 'backups').glob('failed-new-*'))


@pytest.mark.parametrize('failure', ['migration', 'start'])
def test_migration_or_new_broker_failure_restores_code_and_database(monkeypatch, tmp_path: Path, failure):
    _root, install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    database = updater.STORE / 'submissions.sqlite3'
    database.write_bytes(b'old immutable snapshots')
    restored = []
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    states = iter(('active', 'inactive'))
    monkeypatch.setattr(updater, 'service_state', lambda: next(states, 'inactive'))
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: None)

    def snapshot(code_backup, uid):
        backup = code_backup.parent / 'snapshot.sqlite3'
        backup.write_bytes(database.read_bytes())
        return backup

    def restore(backup, uid, evidence_directory=None):
        database.write_bytes(Path(backup).read_bytes())
        restored.append(Path(backup))
        return True, 'test database restored'

    monkeypatch.setattr(updater, '_snapshot_database', snapshot)
    monkeypatch.setattr(updater, '_restore_database', restore)

    def maintenance():
        database.write_bytes(b'partly migrated schema')
        if failure == 'migration':
            raise RuntimeError('migration failed')

    monkeypatch.setattr(updater, '_run_maintenance', maintenance)
    starts = {'count': 0}

    def start(uid):
        starts['count'] += 1
        if failure == 'start' and starts['count'] == 1:
            raise RuntimeError('new broker fails')

    monkeypatch.setattr(updater, '_start_and_verify', start)
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert restored and database.read_bytes() == b'old immutable snapshots'
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'


def test_database_snapshot_and_restore_retains_failed_new_copy(monkeypatch, tmp_path: Path):
    _root, install, _package = _layout(monkeypatch, tmp_path)
    uid = os.geteuid()
    database = updater.STORE / 'submissions.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE marker (value TEXT)')
    connection.execute("INSERT INTO marker(value) VALUES ('old sqlite snapshot')")
    connection.commit()
    connection.close()
    database.chmod(0o600)
    # This test exercises copy/rename behavior without requiring root in the
    # test process; ownership trust itself is covered by the helper below.
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_database_file_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater.os, 'fchown', lambda *args: None)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    code_backup = updater._new_backup_path()
    backup = updater._snapshot_database(code_backup, uid)
    assert backup is not None
    connection = sqlite3.connect(database)
    connection.execute("UPDATE marker SET value='new migrated sqlite snapshot'")
    connection.commit()
    connection.close()
    ok, detail = updater._restore_database(backup, uid)
    assert ok and 'restored' in detail
    connection = sqlite3.connect(database)
    assert connection.execute('SELECT value FROM marker').fetchone()[0] == 'old sqlite snapshot'
    connection.close()
    assert list((install / 'backups').glob('failed-new-database-*.sqlite3'))


def test_migration_capacity_reserves_snapshot_and_first_migration_headroom(monkeypatch, tmp_path: Path):
    _root, _install, _package = _layout(monkeypatch, tmp_path)
    database = updater.STORE / 'submissions.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE submission_files (id TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, content BLOB)')
    connection.commit()
    connection.close()
    database.chmod(0o600)
    account = SimpleNamespace(pw_uid=os.geteuid())

    first = updater.migration_capacity_report(account)

    assert first['ready'] and not first['content_layer_exists']
    assert sum(item['required_bytes'] for item in first['requirements']) >= database.stat().st_size * 4

    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE web_content_blobs (sha256 TEXT PRIMARY KEY, size INTEGER, content BLOB, ref_count INTEGER)')
    connection.execute('DROP TABLE submission_files')
    connection.execute('CREATE VIEW submission_files AS SELECT 1 AS id, "" AS sha256, 0 AS size, X\'\' AS content WHERE 0')
    connection.commit()
    connection.close()
    current = updater.migration_capacity_report(account)
    assert current['ready'] and current['content_layer_exists']
    assert sum(item['required_bytes'] for item in current['requirements']) == database.stat().st_size


def test_stop_failure_does_not_change_code(monkeypatch, tmp_path: Path):
    _root, install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, 'service_state', lambda: 'active')
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: (_ for _ in ()).throw(RuntimeError('stop failed')))
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'
    assert not (install / 'backups').exists()


def test_staging_compile_failure_removes_its_own_temporary_directory(monkeypatch, tmp_path: Path):
    _root, install, _package = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater.subprocess, 'run',
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            subprocess.CalledProcessError(1, args[0])))
    with pytest.raises(subprocess.CalledProcessError):
        updater._stage_source()
    assert not list(install.glob('.update-staging-*'))


def test_compile_failure_before_stop_does_not_touch_broker(monkeypatch, tmp_path: Path):
    _root, _install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater, '_stage_source',
                        lambda: (_ for _ in ()).throw(RuntimeError('static compilation failed')))
    monkeypatch.setattr(updater, '_stop_and_confirm',
                        lambda: pytest.fail('broker must not be stopped after staging failure'))
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'


def test_lock_file_validation_requires_root_regular_unlinked_mode_0600():
    good = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
    assert updater._lock_file_errors(good) == []
    unsafe = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1000, st_nlink=2)
    errors = updater._lock_file_errors(unsafe)
    assert any('root owned' in error for error in errors)
    assert any('hard links' in error for error in errors)
    assert any('0600' in error for error in errors)


def test_second_update_lock_instance_is_rejected_without_waiting(monkeypatch, tmp_path: Path):
    _root, install, _package = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater.os, 'fchown', lambda *args: None)
    monkeypatch.setattr(updater.os, 'fchmod', lambda *args: None)
    monkeypatch.setattr(updater, '_lock_file_errors', lambda info: [])
    first = updater._acquire_update_lock()
    try:
        with pytest.raises(RuntimeError, match='already running'):
            updater._acquire_update_lock()
    finally:
        os.close(first)


def test_lock_rejects_an_unprotected_install_directory(monkeypatch, tmp_path: Path):
    _root, install, _package = _layout(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: ['not root protected'])
    with pytest.raises(RuntimeError, match='unsafe update lock directory'):
        updater._acquire_update_lock()
    assert not (install / '.web-update.lock').exists()


def test_runner_preflight_rejects_an_active_configured_runner(monkeypatch, tmp_path: Path):
    _root, _install, _package = _layout(monkeypatch, tmp_path)
    policy = tmp_path / 'policy.json'
    policy.write_text('{"runner":{"uid":223,"username":"researchkit-runner"}}', encoding='utf-8')
    monkeypatch.setattr(updater, 'POLICY', policy)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, 'runner_configured', REAL_RUNNER_CONFIGURED)
    monkeypatch.setattr(updater, 'runner_service_state', lambda: 'active')
    with pytest.raises(RuntimeError, match='systemctl stop researchkit-runner.service'):
        updater.runner_preflight()


def test_runner_preflight_allows_inactive_configured_runner(monkeypatch, tmp_path: Path):
    _root, _install, _package = _layout(monkeypatch, tmp_path)
    policy = tmp_path / 'policy.json'
    policy.write_text('{"runner":{"uid":223,"username":"researchkit-runner"}}', encoding='utf-8')
    monkeypatch.setattr(updater, 'POLICY', policy)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, 'runner_configured', REAL_RUNNER_CONFIGURED)
    monkeypatch.setattr(updater, 'runner_service_state', lambda: 'inactive')
    assert updater.runner_preflight() is True


def test_runner_reactivated_after_preflight_refuses_before_broker_stop(monkeypatch, tmp_path: Path):
    _root, install, package = _layout(monkeypatch, tmp_path)
    account = SimpleNamespace(pw_uid=222)
    states = iter(('inactive', 'active'))
    monkeypatch.setattr(updater, 'runner_configured', lambda: True)
    monkeypatch.setattr(updater, 'runner_service_state', lambda: next(states))

    def report(args):
        assert updater.runner_preflight() is True
        return {'ready': True}, account

    staging = install / '.update-staging-test'
    staged_package = staging / 'researchkit'

    def stage():
        staged_package.mkdir(parents=True)
        (staged_package / 'web_app.py').write_text('VALUE = 2\n', encoding='utf-8')
        return staging, staged_package

    monkeypatch.setattr(updater, 'report', report)
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater, '_stage_source', stage)
    monkeypatch.setattr(updater, 'service_state', lambda: 'active')
    monkeypatch.setattr(updater, '_stop_and_confirm',
                        lambda: pytest.fail('broker must not stop when runner restarted'))
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'
    assert not staging.exists()


def test_manifest_mode_check_is_read_only_and_repair_is_local_idempotent(monkeypatch, tmp_path):
    etc = tmp_path / 'etc'; etc.mkdir()
    manifests = etc / 'dataset-manifests'; manifests.mkdir(mode=0o700)
    receipt = etc / 'data-publish-receipts'; receipt.mkdir(mode=0o700)
    nested = manifests / 'untouched'; nested.mkdir(mode=0o700)
    content = manifests / 'version.json'; content.write_bytes(b'unchanged\n'); content.chmod(0o644)
    monkeypatch.setattr(updater, 'ETC', etc)
    # Only root ownership/ancestor trust is fabricated for this non-root test.
    monkeypatch.setattr(updater, 'path_errors', lambda *a, **k: [])
    old_umask = os.umask(0o077)
    try:
        status = updater.manifest_directory_status()
        assert status['repair_required'] and status['mode'] == '0700'
        assert stat.S_IMODE(manifests.stat().st_mode) == 0o700
        repaired = updater.manifest_directory_status(repair=True)
        assert repaired['repaired'] and repaired['mode'] == '0755'
        assert not updater.manifest_directory_status(repair=True).get('repaired')
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert content.read_bytes() == b'unchanged\n'
    assert stat.S_IMODE(content.stat().st_mode) == 0o644


@pytest.mark.parametrize('unsafe', ['symlink', 'writable', 'file', 'untrusted-parent', 'replaced'])
def test_manifest_repair_rejects_unsafe_paths_before_chmod(monkeypatch, tmp_path, unsafe):
    etc = tmp_path / 'etc'; etc.mkdir()
    directory = etc / 'dataset-manifests'
    outside = tmp_path / 'private'; outside.mkdir(mode=0o700)
    if unsafe == 'symlink':
        directory.symlink_to(outside, target_is_directory=True)
    elif unsafe == 'file':
        directory.write_text('private')
    else:
        directory.mkdir(mode=0o700)
        if unsafe == 'writable': directory.chmod(0o777)
    monkeypatch.setattr(updater, 'ETC', etc)
    monkeypatch.setattr(updater, 'path_errors', lambda *a, **k: ['unsafe parent'] if unsafe == 'untrusted-parent' else [])
    if unsafe == 'replaced':
        real_open = updater.os.open
        def replaced(path, flags):
            directory.rename(etc / 'old-manifests')
            directory.mkdir(mode=0o700)
            return real_open(path, flags)
        monkeypatch.setattr(updater.os, 'open', replaced)
    monkeypatch.setattr(updater.os, 'fchmod', lambda *args: pytest.fail('unsafe target must never be chmodded'))
    with pytest.raises(RuntimeError, match='unsafe|changed'):
        updater.manifest_directory_status(repair=True)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700


def test_missing_manifest_directory_is_not_created(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, 'ETC', tmp_path / 'absent')
    assert updater.manifest_directory_status(repair=True)['exists'] is False
    assert not (tmp_path / 'absent').exists()


def test_update_repairs_existing_manifest_before_restarting_broker(monkeypatch, tmp_path):
    _root, _install, _package = _layout(monkeypatch, tmp_path)
    updater.ETC.mkdir()
    manifests = updater.ETC / 'dataset-manifests'; manifests.mkdir(mode=0o700)
    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, 'service_state', lambda: 'active')
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: None)
    def started(_uid):
        assert stat.S_IMODE(manifests.stat().st_mode) == 0o755
    monkeypatch.setattr(updater, '_start_and_verify', started)
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 0


def test_update_check_reports_manifest_repair_without_changing_permissions(monkeypatch, tmp_path, capsys):
    _root, _install, _package = _layout(monkeypatch, tmp_path)
    updater.ETC.mkdir()
    manifests = updater.ETC / 'dataset-manifests'; manifests.mkdir(mode=0o700)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, 'prerequisite_errors', lambda: ([], SimpleNamespace(pw_uid=222)))
    assert updater.update(argparse.Namespace(check=True, dry_run=False)) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result['ready'] and result['manifest_directory']['repair_required']
    assert stat.S_IMODE(manifests.stat().st_mode) == 0o700


def test_database_rollback_uses_same_directory_replace_across_mounts(monkeypatch,tmp_path):
    _root,install,_package=_layout(monkeypatch,tmp_path)
    uid=os.geteuid()
    backup_dir=install/'backups';backup_dir.mkdir(mode=0o700)
    backup=backup_dir/'before.sqlite3';backup.write_bytes(b'old database');backup.chmod(0o600)
    database=updater.STORE/'submissions.sqlite3';database.write_bytes(b'failed new database');database.chmod(0o600)
    monkeypatch.setattr(updater,'path_errors',lambda *a,**k:[])
    monkeypatch.setattr(updater,'_database_file_errors',lambda *a,**k:[])
    monkeypatch.setattr(updater.os,'fchown',lambda *a:None)
    def cross_mount(*args):
        import errno
        raise OSError(errno.EXDEV,'cross-device rename')
    monkeypatch.setattr(updater.os,'rename',cross_mount)
    ok,detail=updater._restore_database(backup,uid)
    assert ok,detail
    assert database.read_bytes()==b'old database'
    assert next(backup_dir.glob('failed-new-database-*')).read_bytes()==b'failed new database'


def test_rollback_without_prior_database_preserves_failed_evidence(monkeypatch,tmp_path):
    _root,install,_package=_layout(monkeypatch,tmp_path)
    evidence=install/'backups';evidence.mkdir(mode=0o700)
    database=updater.STORE/'submissions.sqlite3';database.write_bytes(b'new incompatible schema');database.chmod(0o600)
    monkeypatch.setattr(updater,'path_errors',lambda *a,**k:[])
    monkeypatch.setattr(updater,'_database_file_errors',lambda *a,**k:[])
    monkeypatch.setattr(updater.os,'fchown',lambda *a:None)
    ok,detail=updater._restore_database(None,os.geteuid(),evidence)
    assert ok,detail
    assert not database.exists()
    assert next(evidence.glob('failed-new-database-*')).read_bytes()==b'new incompatible schema'


def test_private_copy_never_removes_preexisting_evidence(monkeypatch,tmp_path):
    source=tmp_path/'source';source.write_bytes(b'new');source.chmod(0o600)
    target=tmp_path/'existing';target.write_bytes(b'previous evidence')
    monkeypatch.setattr(updater,'_database_file_errors',lambda *a,**k:[])
    with pytest.raises(FileExistsError):
        updater._copy_private_database(source,target,os.geteuid(),os.geteuid(),os.getgid())
    assert target.read_bytes()==b'previous evidence'


def test_source_validation_checks_rkweb_purge_cli(monkeypatch, tmp_path: Path):
    root, _install, _package = _layout(monkeypatch, tmp_path)
    purge_src = root / 'tools' / 'rkweb-purge.py'
    purge_src.parent.mkdir(parents=True, exist_ok=True)
    # Sym-linked or hard-linked tools/rkweb-purge.py must be rejected
    target = root / 'core' / 'researchkit' / 'web_app.py'
    purge_src.symlink_to(target)
    assert any('rkweb-purge.py' in e for e in updater.source_tree_errors())
    purge_src.unlink()
    os.link(target, purge_src)
    assert any('rkweb-purge.py' in e for e in updater.source_tree_errors())
    purge_src.unlink()
    purge_src.write_text('#!/usr/bin/env python3\nprint("ok")\n', encoding='utf-8')
    assert updater.source_tree_errors() == []
    # Under production /opt/researchkit-web target, missing tools/rkweb-purge.py must also be flagged
    purge_src.unlink()
    monkeypatch.setattr(updater, 'INSTALL', Path('/opt/researchkit-web'))
    assert any('rkweb-purge.py is missing' in e for e in updater.source_tree_errors())


@pytest.mark.parametrize('had_prior', [True, False])
def test_start_failure_restores_or_removes_purge_cli(monkeypatch, tmp_path: Path, had_prior):
    root, install, package = _layout(monkeypatch, tmp_path)
    purge_src = root / 'tools' / 'rkweb-purge.py'
    purge_src.parent.mkdir(parents=True, exist_ok=True)
    purge_src.write_text('#!/usr/bin/env python3\n# new purge cli\n', encoding='utf-8')
    sbin_dir = tmp_path / 'sbin'
    sbin_dir.mkdir(mode=0o755)
    purge_cli = sbin_dir / 'rkweb-purge'
    if had_prior:
        purge_cli.write_text('#!/usr/bin/env python3\n# prior purge cli\n', encoding='utf-8')
        purge_cli.chmod(0o755)
    monkeypatch.setattr(updater, 'PURGE_CLI', purge_cli)

    account = SimpleNamespace(pw_uid=222)
    monkeypatch.setattr(updater, 'report', lambda args: ({'ready': True}, account))
    monkeypatch.setattr(updater.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(updater.os, 'chown', lambda *args: None)
    monkeypatch.setattr(updater, 'path_errors', lambda *args, **kwargs: [])
    monkeypatch.setattr(updater, '_acquire_update_lock', lambda: None)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *args, **kwargs: None)
    states = iter(('active', 'inactive'))
    monkeypatch.setattr(updater, 'service_state', lambda: next(states, 'inactive'))
    monkeypatch.setattr(updater, '_stop_and_confirm', lambda: None)
    calls = {'start': 0}

    def start(_uid):
        calls['start'] += 1
        if calls['start'] == 1:
            raise RuntimeError('simulated startup failure after PURGE_CLI update')

    monkeypatch.setattr(updater, '_start_and_verify', start)
    assert updater.update(argparse.Namespace(check=False, dry_run=False)) == 1
    assert (package / 'web_app.py').read_text(encoding='utf-8') == 'VALUE = 1\n'
    if had_prior:
        assert purge_cli.exists()
        assert '# prior purge cli' in purge_cli.read_text(encoding='utf-8')
        assert stat.S_IMODE(purge_cli.stat().st_mode) == 0o755
    else:
        assert not purge_cli.exists()
