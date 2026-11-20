#!/usr/bin/env python3
"""Install the isolated ResearchKit Web execution worker.

This installer deliberately only adds the worker account, policy entry, and
systemd unit after the regular Web updater has installed the worker code.  It
is compatible with the RHEL 9 system Python 3.9 and never installs packages.
"""
import argparse
import fcntl
import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = Path('/opt/researchkit-web')
APP = INSTALL / 'app'
PACKAGE = APP / 'core' / 'researchkit'
PYTHON = INSTALL / 'venv' / 'bin' / 'python'
ETC = Path('/etc/researchkit-web')
POLICY = ETC / 'policy.json'
UNIT_SOURCE = ROOT / 'deploy' / 'researchkit-runner.service'
UNIT = Path('/etc/systemd/system/researchkit-runner.service')
BWRAP = Path('/usr/bin/bwrap')
RUNNER_STATE = Path('/var/lib/researchkit-runner')
UPDATE_LOCK = INSTALL / '.web-update.lock'
SERVICE = 'researchkit-runner.service'
BROKER_SERVICE = 'researchkit-broker.service'
SERVICE_USER = 'researchkit-runner'
REQUIRED_INSTALLED = (
    PACKAGE / 'web_runner.py', PACKAGE / 'web_jobs.py',
    PACKAGE / 'web_run_entry.py', PACKAGE / 'web_datasets.py', PYTHON,
)


def run(command):
    subprocess.run(command, check=True)


def _path_errors(path, kind='regular-file'):
    """Require an existing path and every resolved component to be root-safe."""
    path = Path(path)
    if not os.path.lexists(str(path)):
        return ['missing: %s' % path]
    try:
        original = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        return ['cannot resolve: %s' % path]
    if stat.S_ISLNK(original.st_mode) or resolved != path:
        return ['symbolic link is not allowed: %s' % path]
    if kind == 'regular-file' and not stat.S_ISREG(original.st_mode):
        return ['not a regular file: %s' % path]
    if kind == 'directory' and not stat.S_ISDIR(original.st_mode):
        return ['not a directory: %s' % path]
    current = Path('/')
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError:
            return ['cannot inspect: %s' % current]
        if stat.S_ISLNK(info.st_mode):
            return ['symbolic link is not allowed: %s' % current]
        if info.st_uid != 0:
            return ['not root-owned: %s' % current]
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return ['group or world writable: %s' % current]
    return []


def _source_errors():
    """Reject source links and hard links before a root copy of the unit."""
    for ancestor in (UNIT_SOURCE,) + tuple(UNIT_SOURCE.parents):
        if ancestor == Path('/'):
            break
        try:
            info = ancestor.lstat()
        except OSError:
            return ['missing unit source path: %s' % ancestor]
        if stat.S_ISLNK(info.st_mode):
            return ['unit source has a symbolic-link ancestor: %s' % ancestor]
    try:
        info = UNIT_SOURCE.lstat()
    except OSError:
        return ['cannot inspect unit source: %s' % UNIT_SOURCE]
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return ['unit source must be a regular, unlinked file: %s' % UNIT_SOURCE]
    return []


def _load_policy():
    errors = _path_errors(POLICY)
    if errors:
        return None, errors
    try:
        policy = json.loads(POLICY.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, ['cannot read policy: %s' % exc]
    if not isinstance(policy, dict):
        return None, ['policy must be a JSON object']
    broker = policy.get('broker')
    if not isinstance(broker, dict):
        return None, ['policy lacks broker identity']
    uid, username = broker.get('uid'), broker.get('username')
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        return None, ['policy broker UID is invalid']
    if not isinstance(username, str) or not username:
        return None, ['policy broker username is invalid']
    return policy, []


def _bwrap_errors():
    errors = _path_errors(BWRAP)
    if errors:
        return errors
    try:
        if not os.access(str(BWRAP), os.X_OK):
            return ['bubblewrap is not executable: %s' % BWRAP]
    except OSError as exc:
        return ['cannot inspect bubblewrap: %s' % exc]
    clone = Path('/proc/sys/kernel/unprivileged_userns_clone')
    if clone.exists():
        try:
            if clone.read_text(encoding='ascii').strip() != '1':
                errors.append('unprivileged user namespaces are disabled')
        except OSError as exc:
            errors.append('cannot inspect user namespace setting: %s' % exc)
    maximum = Path('/proc/sys/user/max_user_namespaces')
    if maximum.exists():
        try:
            if int(maximum.read_text(encoding='ascii').strip()) < 1:
                errors.append('user.max_user_namespaces must be positive')
        except (OSError, ValueError) as exc:
            errors.append('cannot inspect user namespace limit: %s' % exc)
    return errors


def _account_status(policy):
    broker = policy['broker']
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return None, []
    try:
        validate_account(account, broker)
    except RuntimeError as exc:
        return account, [str(exc)]
    return account, []


def validate_account(account, broker):
    """Reject a login or shared account before it can run submitted code."""
    shell = Path(account.pw_shell or '').name.lower()
    if account.pw_name != SERVICE_USER:
        raise RuntimeError('runner account name is invalid')
    if account.pw_uid < 1 or account.pw_uid >= 1000:
        raise RuntimeError('runner account must have a non-root system UID (1-999)')
    if account.pw_uid == broker['uid'] or account.pw_name == broker['username']:
        raise RuntimeError('runner account must be distinct from the broker account')
    if shell not in ('false', 'nologin'):
        raise RuntimeError('runner account must have a nologin shell')
    try:
        group = grp.getgrgid(account.pw_gid)
    except KeyError as exc:
        raise RuntimeError('runner account primary group does not exist') from exc
    if group.gr_name != SERVICE_USER or group.gr_gid < 1 or group.gr_gid >= 1000:
        raise RuntimeError('runner account must use a dedicated system primary group')
    foreign_members = [member for member in group.gr_mem if member != SERVICE_USER]
    if foreign_members:
        raise RuntimeError('runner primary group must not contain other accounts')


def _runner_state_errors(account):
    """Never let systemd take ownership of an unknown pre-existing state tree."""
    if not os.path.lexists(str(RUNNER_STATE)):
        return []
    if account is None:
        return ['runner state directory exists but the runner account does not']
    errors = _path_errors(RUNNER_STATE.parent, 'directory')
    try:
        info = RUNNER_STATE.lstat()
    except OSError as exc:
        return errors + ['cannot inspect runner state directory: %s' % exc]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        errors.append('runner state path must be a directory, not a link or special file')
    elif info.st_uid != account.pw_uid:
        errors.append('runner state directory must be owned by the runner UID')
    elif stat.S_IMODE(info.st_mode) != 0o700:
        errors.append('runner state directory mode must be 0700')
    return errors


def _systemd_state(service):
    """Read-only state information for administrators; it is not a health claim."""
    try:
        result = subprocess.run(['systemctl', 'show', '--property=ActiveState',
                                 '--property=SubState', '--no-pager', service],
                                check=False, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {'available': False, 'error': str(exc)}
    if result.returncode != 0:
        return {'available': False, 'error': result.stderr.strip() or 'systemctl show failed'}
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition('=')
        if separator and key in ('ActiveState', 'SubState'):
            values[key] = value
    return {'available': True, 'state': values}


def _lock_file_errors(info):
    errors = []
    if not stat.S_ISREG(info.st_mode):
        errors.append('update lock is not a regular file')
    if info.st_uid != 0:
        errors.append('update lock is not root owned')
    if info.st_nlink != 1:
        errors.append('update lock has unexpected hard links')
    if stat.S_IMODE(info.st_mode) != 0o600:
        errors.append('update lock mode is not 0600')
    return errors


def _acquire_update_lock():
    """Serialize against the Web code updater and any runner installation."""
    parent_errors = _path_errors(INSTALL, 'directory')
    if parent_errors:
        raise RuntimeError('unsafe update lock directory: %s' % '; '.join(parent_errors))
    flags = os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        info = UPDATE_LOCK.lstat()
    except FileNotFoundError:
        try:
            descriptor = os.open(str(UPDATE_LOCK), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(str(UPDATE_LOCK), flags)
    else:
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('update lock is a symbolic link')
        descriptor = os.open(str(UPDATE_LOCK), flags)
    try:
        if created:
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o600)
        errors = _lock_file_errors(os.fstat(descriptor))
        if errors:
            raise RuntimeError('; '.join(errors))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another ResearchKit Web update or runner installation is already running') from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _release_update_lock(descriptor):
    os.close(descriptor)


def report(args):
    policy, policy_errors = _load_policy()
    code_errors = []
    for path in REQUIRED_INSTALLED:
        code_errors.extend(_path_errors(path))
    source_errors = _source_errors()
    unit_destination_errors = _path_errors(UNIT.parent, 'directory')
    bwrap_errors = _bwrap_errors()
    conflicts = []
    account_errors = []
    account = None
    if policy is not None:
        account, account_errors = _account_status(policy)
        if 'runner' in policy:
            conflicts.append('policy already has a runner configuration; refuse to replace it')
    state_errors = _runner_state_errors(account)
    if os.path.lexists(str(UNIT)):
        conflicts.append('runner unit already exists; refuse to replace it')
    errors = (policy_errors + code_errors + source_errors + unit_destination_errors +
              bwrap_errors + account_errors + state_errors)
    result = {
        'schema': 'researchkit.web-runner-install-check/v1',
        'installed_code': {'paths': [str(path) for path in REQUIRED_INSTALLED],
                           'errors': code_errors},
        'policy': str(POLICY),
        'policy_errors': policy_errors,
        'unit_source_errors': source_errors,
        'unit_destination_errors': unit_destination_errors,
        'bubblewrap_errors': bwrap_errors,
        'runner_account': None if account is None else {
            'uid': account.pw_uid, 'username': account.pw_name},
        'account_errors': account_errors,
        'runner_state_directory': str(RUNNER_STATE),
        'runner_state_errors': state_errors,
        'conflicts': conflicts,
        'runner_service_state': _systemd_state(SERVICE),
        'ready': not errors and not conflicts,
        'notes': [
            'This checks installed code and administrator prerequisites only; it does not establish runner UID isolation or validate runner functionality.',
            'The worker performs its own isolation self-check when started. An active systemd state alone is not functional validation.',
            'No runner process is started by --check or --dry-run.',
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result, policy


def _write_new(path, data, mode):
    """Atomically replace a root-owned file without following a temp link."""
    parent = path.parent
    for index in range(100):
        temporary = parent / ('.%s.runner-%d-%d' % (path.name, os.getpid(), index))
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, 'O_NOFOLLOW'):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(temporary), flags, mode)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError('cannot allocate a safe temporary policy file')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(str(temporary), 0, 0)
        os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _backup_policy(data):
    stamp = time.strftime('%Y%m%d%H%M%S', time.gmtime())
    for index in range(100):
        candidate = POLICY.with_name('%s.runner-backup-%s-%d' % (POLICY.name, stamp, index))
        try:
            descriptor = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(str(candidate), 0, 0)
            os.chmod(str(candidate), 0o600)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError('cannot create a unique policy backup')


def _create_account(policy):
    account, errors = _account_status(policy)
    if errors:
        raise RuntimeError(errors[0])
    if account is None:
        try:
            group = grp.getgrnam(SERVICE_USER)
        except KeyError:
            group = None
        if group is None:
            run(['groupadd', '--system', SERVICE_USER])
        elif (group.gr_gid < 1 or group.gr_gid >= 1000 or group.gr_mem):
            raise RuntimeError('existing runner group is not an empty dedicated system group')
        run(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin',
             '--gid', SERVICE_USER, SERVICE_USER])
        try:
            account = pwd.getpwnam(SERVICE_USER)
        except KeyError as exc:  # pragma: no cover - useradd success should create it
            raise RuntimeError('runner account was not created') from exc
        validate_account(account, policy['broker'])
    return account


def _install_unit(created):
    data = UNIT_SOURCE.read_bytes()
    # New unit names are refused during preflight, so write only a fresh file.
    descriptor = os.open(str(UNIT), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    created[0] = True
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(str(UNIT), 0, 0)
    os.chmod(str(UNIT), 0o644)


def _rollback(old_policy, old_mode, created_unit, broker_stopped):
    errors = []
    runner_stopped = True
    try:
        run(['systemctl', 'stop', SERVICE])
    except Exception as exc:
        runner_stopped = False
        errors.append('could not stop runner: %s' % exc)
    if created_unit and not runner_stopped:
        errors.append('runner stop was not confirmed; retained new runner unit and policy for recovery')
    if created_unit and runner_stopped:
        try:
            run(['systemctl', 'disable', SERVICE])
        except Exception as exc:
            errors.append('could not disable runner: %s' % exc)
        try:
            UNIT.unlink()
        except OSError as exc:
            errors.append('could not remove new runner unit: %s' % exc)
        try:
            run(['systemctl', 'daemon-reload'])
        except Exception as exc:
            errors.append('could not reload systemd after rollback: %s' % exc)
    if old_policy is not None and runner_stopped:
        try:
            _write_new(POLICY, old_policy, old_mode)
        except Exception as exc:
            errors.append('could not restore policy: %s' % exc)
    if broker_stopped:
        try:
            run(['systemctl', 'restart', BROKER_SERVICE])
        except Exception as exc:
            errors.append('could not restart broker: %s' % exc)
    return errors, runner_stopped


def install(args):
    if args.check or args.dry_run:
        checked, _policy = report(args)
        return 0 if checked['ready'] else 1
    if os.geteuid() != 0:
        print('root is required; run: sudo /usr/bin/python3 tools/install-web-runner.py', file=sys.stderr)
        return 2
    try:
        descriptor = _acquire_update_lock()
    except Exception as exc:
        print('runner installation did not start: %s' % exc, file=sys.stderr)
        return 1
    try:
        # Recheck only after taking the shared lock; the updater can otherwise
        # replace code or policy between preflight and this transaction.
        checked, policy = report(args)
        if not checked['ready'] or policy is None:
            print('runner prerequisites are missing, unsafe, or already configured; see --check output', file=sys.stderr)
            return 1
        old_policy = POLICY.read_bytes()
        old_mode = stat.S_IMODE(POLICY.stat().st_mode)
        backup = None
        created_unit = [False]
        broker_stopped = False
        try:
            account = _create_account(policy)
            state_errors = _runner_state_errors(account)
            if state_errors:
                raise RuntimeError('; '.join(state_errors))
            backup = _backup_policy(old_policy)
            broker_stopped = True
            run(['systemctl', 'stop', BROKER_SERVICE])
            updated = dict(policy)
            updated['runner'] = {'uid': account.pw_uid, 'username': SERVICE_USER}
            _write_new(POLICY, (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8'), old_mode)
            _install_unit(created_unit)
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', SERVICE])
            run(['systemctl', 'restart', BROKER_SERVICE])
            run(['systemctl', 'is-active', '--quiet', BROKER_SERVICE])
            run(['systemctl', 'start', SERVICE])
            run(['systemctl', 'is-active', '--quiet', SERVICE])
        except Exception as exc:
            rollback_errors, runner_stopped = _rollback(old_policy, old_mode, created_unit[0], broker_stopped)
            if runner_stopped:
                message = 'runner installation failed; runner was stopped and the prior policy/broker service were restored where possible: %s' % exc
            else:
                message = 'runner installation failed and runner stop was not confirmed; retained the new runner state for administrator recovery: %s' % exc
            if rollback_errors:
                message += ' | rollback issues: ' + '; '.join(rollback_errors)
            print(message, file=sys.stderr)
            return 1
        print('installed; systemd currently reports active for %s (verify worker self-check/logs separately; policy backup: %s)' %
              (SERVICE, backup))
        return 0
    finally:
        _release_update_lock(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description='install ResearchKit Web execution runner')
    parser.add_argument('--check', action='store_true', help='read-only prerequisite report')
    parser.add_argument('--dry-run', action='store_true', help='read-only install plan report')
    return install(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
