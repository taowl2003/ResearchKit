"""Focused, non-root tests for the runner installer transaction."""
import contextlib
import importlib.util
import io
import json
import subprocess
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'install-web-runner.py'
SPEC = importlib.util.spec_from_file_location('install_web_runner', TOOL)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class RunnerInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = self.root / 'policy.json'
        self.unit = self.root / 'researchkit-runner.service'
        self.source = self.root / 'source.service'
        self.source.write_text('[Service]\nExecStart=/bin/true\n', encoding='utf-8')
        self.initial = {
            'schema': 'researchkit.web-policy/v1',
            'broker': {'uid': 222, 'username': 'researchkit-web'},
            'keep': {'a': [1, 2]},
        }
        self.policy.write_text(json.dumps(self.initial), encoding='utf-8')
        self.policy.chmod(0o644)
        self.account = types.SimpleNamespace(
            pw_name='researchkit-runner', pw_uid=223, pw_gid=223,
            pw_shell='/usr/sbin/nologin')
        self.group = types.SimpleNamespace(gr_name='researchkit-runner', gr_gid=223, gr_mem=[])
        self.saved = {}
        for name in ('POLICY', 'UNIT', 'UNIT_SOURCE', 'RUNNER_STATE', 'INSTALL', 'UPDATE_LOCK'):
            self.saved[name] = getattr(installer, name)
        installer.POLICY = self.policy
        installer.UNIT = self.unit
        installer.UNIT_SOURCE = self.source
        installer.RUNNER_STATE = self.root / 'state'
        installer.INSTALL = self.root
        installer.UPDATE_LOCK = self.root / '.web-update.lock'
        self.original_geteuid = installer.os.geteuid
        self.original_chown = installer.os.chown
        self.original_getpwnam = installer.pwd.getpwnam
        self.original_getgrgid = installer.grp.getgrgid
        self.original_getgrnam = installer.grp.getgrnam
        self.original_acquire = installer._acquire_update_lock
        self.original_release = installer._release_update_lock
        installer.os.geteuid = lambda: 0
        installer.os.chown = lambda *args: None
        installer.pwd.getpwnam = lambda name: self.account
        installer.grp.getgrgid = lambda gid: self.group
        installer._acquire_update_lock = lambda: None
        installer._release_update_lock = lambda descriptor: None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(installer, name, value)
        installer.os.geteuid = self.original_geteuid
        installer.os.chown = self.original_chown
        installer.pwd.getpwnam = self.original_getpwnam
        installer.grp.getgrgid = self.original_getgrgid
        installer.grp.getgrnam = self.original_getgrnam
        installer._acquire_update_lock = self.original_acquire
        installer._release_update_lock = self.original_release
        self.temporary.cleanup()

    def _args(self, check=False, dry_run=False):
        return types.SimpleNamespace(check=check, dry_run=dry_run)

    def _ready_report(self):
        return {'ready': True}, dict(self.initial)

    def test_success_preserves_policy_and_starts_services_in_order(self):
        calls = []
        old_report, old_run = installer.report, installer.run
        installer.report = lambda args: self._ready_report()
        installer.run = lambda command: calls.append(command)
        try:
            self.assertEqual(installer.install(self._args()), 0)
        finally:
            installer.report, installer.run = old_report, old_run

        changed = json.loads(self.policy.read_text(encoding='utf-8'))
        self.assertEqual(changed['keep'], self.initial['keep'])
        self.assertEqual(changed['runner'], {'uid': 223, 'username': 'researchkit-runner'})
        self.assertEqual(self.unit.read_text(encoding='utf-8'), self.source.read_text(encoding='utf-8'))
        backups = list(self.root.glob('policy.json.runner-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), json.dumps(self.initial))
        self.assertEqual(calls, [
            ['systemctl', 'stop', installer.BROKER_SERVICE],
            ['systemctl', 'daemon-reload'],
            ['systemctl', 'enable', installer.SERVICE],
            ['systemctl', 'restart', installer.BROKER_SERVICE],
            ['systemctl', 'is-active', '--quiet', installer.BROKER_SERVICE],
            ['systemctl', 'start', installer.SERVICE],
            ['systemctl', 'is-active', '--quiet', installer.SERVICE],
        ])

    def test_start_failure_restores_policy_stops_runner_and_recovers_broker(self):
        calls = []
        old_report, old_run = installer.report, installer.run
        installer.report = lambda args: self._ready_report()

        def failing_run(command):
            calls.append(command)
            if command == ['systemctl', 'start', installer.SERVICE]:
                raise RuntimeError('runner self-check failed')

        installer.run = failing_run
        try:
            self.assertEqual(installer.install(self._args()), 1)
        finally:
            installer.report, installer.run = old_report, old_run

        self.assertEqual(json.loads(self.policy.read_text(encoding='utf-8')), self.initial)
        self.assertFalse(self.unit.exists())
        self.assertIn(['systemctl', 'stop', installer.SERVICE], calls)
        self.assertIn(['systemctl', 'restart', installer.BROKER_SERVICE], calls)
        self.assertTrue(list(self.root.glob('policy.json.runner-backup-*')))

    def test_rejects_broker_or_login_account(self):
        broker = {'uid': 222, 'username': 'researchkit-web'}
        shared = types.SimpleNamespace(
            pw_name='researchkit-runner', pw_uid=222, pw_gid=222,
            pw_shell='/usr/sbin/nologin')
        with self.assertRaisesRegex(RuntimeError, 'distinct from the broker'):
            installer.validate_account(shared, broker)
        login = types.SimpleNamespace(
            pw_name='researchkit-runner', pw_uid=223, pw_gid=223,
            pw_shell='/bin/bash')
        with self.assertRaisesRegex(RuntimeError, 'nologin'):
            installer.validate_account(login, broker)

    def test_reuses_an_empty_dedicated_group_after_prior_useradd_failure(self):
        calls = []
        old_getpwnam, old_getgrnam, old_run = installer.pwd.getpwnam, installer.grp.getgrnam, installer.run
        created = [False]

        def getpwnam(name):
            if not created[0]:
                raise KeyError(name)
            return self.account

        def run(command):
            calls.append(command)
            if command[0] == 'useradd':
                created[0] = True

        installer.pwd.getpwnam = getpwnam
        installer.grp.getgrnam = lambda name: self.group
        installer.run = run
        try:
            account = installer._create_account(self.initial)
        finally:
            installer.pwd.getpwnam, installer.grp.getgrnam, installer.run = old_getpwnam, old_getgrnam, old_run
        self.assertEqual(account.pw_uid, 223)
        self.assertEqual(calls, [[
            'useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin',
            '--gid', 'researchkit-runner', 'researchkit-runner',
        ]])

    def test_check_report_is_read_only_and_explains_its_limits(self):
        old_load = installer._load_policy
        old_paths = installer._path_errors
        old_source = installer._source_errors
        old_bwrap = installer._bwrap_errors
        old_state = installer._systemd_state
        installer._load_policy = lambda: (dict(self.initial), [])
        installer._path_errors = lambda path, kind='regular-file': []
        installer._source_errors = lambda: []
        installer._bwrap_errors = lambda: []
        installer._systemd_state = lambda service: {'available': True, 'state': {'ActiveState': 'inactive'}}
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                result, _policy = installer.report(self._args(check=True))
        finally:
            installer._load_policy = old_load
            installer._path_errors = old_paths
            installer._source_errors = old_source
            installer._bwrap_errors = old_bwrap
            installer._systemd_state = old_state
        encoded = json.loads(output.getvalue())
        self.assertTrue(result['ready'])
        self.assertEqual(encoded['schema'], 'researchkit.web-runner-install-check/v1')
        self.assertIn('does not establish runner UID isolation', ' '.join(encoded['notes']))
        self.assertFalse(self.unit.exists())
        self.assertEqual(stat.S_IMODE(self.policy.stat().st_mode), 0o644)

    def test_check_refuses_existing_runner_configuration_or_unit(self):
        old_load = installer._load_policy
        old_paths = installer._path_errors
        old_source = installer._source_errors
        old_bwrap = installer._bwrap_errors
        old_state = installer._systemd_state
        configured = dict(self.initial)
        configured['runner'] = {'uid': 223, 'username': 'researchkit-runner'}
        self.unit.write_text('[Service]\n', encoding='utf-8')
        installer._load_policy = lambda: (configured, [])
        installer._path_errors = lambda path, kind='regular-file': []
        installer._source_errors = lambda: []
        installer._bwrap_errors = lambda: []
        installer._systemd_state = lambda service: {'available': False}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result, _policy = installer.report(self._args(check=True))
        finally:
            installer._load_policy = old_load
            installer._path_errors = old_paths
            installer._source_errors = old_source
            installer._bwrap_errors = old_bwrap
            installer._systemd_state = old_state
        self.assertFalse(result['ready'])
        self.assertEqual(len(result['conflicts']), 2)

    def test_partial_unit_write_is_removed_during_rollback(self):
        calls = []
        old_report, old_run, old_chown = installer.report, installer.run, installer.os.chown
        installer.report = lambda args: self._ready_report()
        installer.run = lambda command: calls.append(command)

        def fail_unit_chown(path, *args):
            if Path(path) == self.unit:
                raise OSError('simulated unit ownership failure')

        installer.os.chown = fail_unit_chown
        try:
            self.assertEqual(installer.install(self._args()), 1)
        finally:
            installer.report, installer.run, installer.os.chown = old_report, old_run, old_chown
        self.assertFalse(self.unit.exists())
        self.assertEqual(json.loads(self.policy.read_text(encoding='utf-8')), self.initial)
        self.assertIn(['systemctl', 'stop', installer.SERVICE], calls)

    def test_existing_runner_state_must_match_runner_uid_and_mode(self):
        installer.RUNNER_STATE.mkdir(mode=0o700)
        old_paths = installer._path_errors
        installer._path_errors = lambda path, kind='regular-file': []
        try:
            errors = installer._runner_state_errors(self.account)
        finally:
            installer._path_errors = old_paths
        self.assertIn('runner state directory must be owned by the runner UID', errors)

    def test_shared_update_lock_rejects_another_process(self):
        old_paths = installer._path_errors
        old_errors = installer._lock_file_errors
        installer._acquire_update_lock = self.original_acquire
        installer._path_errors = lambda path, kind='regular-file': []
        installer._lock_file_errors = lambda info: []
        child = subprocess.Popen([
            sys.executable, '-c',
            'import fcntl,sys,time; f=open(sys.argv[1], "a+"); '
            'fcntl.flock(f, fcntl.LOCK_EX); print("locked", flush=True); time.sleep(10)',
            str(installer.UPDATE_LOCK)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                installer._acquire_update_lock()
        finally:
            child.terminate()
            child.wait(timeout=5)
            child.stdout.close()
            installer._path_errors = old_paths
            installer._lock_file_errors = old_errors

    def test_unconfirmed_runner_stop_retains_new_unit_and_policy_for_recovery(self):
        calls = []
        old_report, old_run = installer.report, installer.run
        installer.report = lambda args: self._ready_report()

        def failing_run(command):
            calls.append(command)
            if command == ['systemctl', 'start', installer.SERVICE]:
                raise RuntimeError('runner start failed')
            if command == ['systemctl', 'stop', installer.SERVICE]:
                raise RuntimeError('runner stop unavailable')

        installer.run = failing_run
        output = io.StringIO()
        try:
            with contextlib.redirect_stderr(output):
                self.assertEqual(installer.install(self._args()), 1)
        finally:
            installer.report, installer.run = old_report, old_run
        self.assertTrue(self.unit.exists())
        self.assertIn('runner', json.loads(self.policy.read_text(encoding='utf-8')))
        self.assertNotIn(['systemctl', 'disable', installer.SERVICE], calls)
        self.assertIn('stop was not confirmed', output.getvalue())


if __name__ == '__main__':
    unittest.main()
