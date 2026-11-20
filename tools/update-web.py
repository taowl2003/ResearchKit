#!/usr/bin/env python3
"""Safely replace the installed ResearchKit Web application package.

The updater preserves policy, dependencies and shared data. It backs up code
and the full SQLite database, imports trusted installed resources into central
storage, and migrates duplicate file bodies offline as the broker account.
A failed update restores both code and database. The outer administrative
script remains compatible with the RHEL 9 system Python 3.9.
"""
import argparse
import fcntl
import grp
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = Path('/opt/researchkit-web')
APP = INSTALL / 'app'
PACKAGE = APP / 'core' / 'researchkit'
LOCK = ROOT / 'deploy' / 'requirements-web.lock'
INSTALLED_LOCK = APP / 'deploy' / 'requirements-web.lock'
ETC = Path('/etc/researchkit-web')
POLICY = ETC / 'policy.json'
RUNTIME = Path('/run/researchkit-web')
SOCKET = RUNTIME / 'broker.sock'
STORE = Path('/var/lib/researchkit-web/submissions')
UPDATE_LOCK = INSTALL / '.web-update.lock'
SERVICE = 'researchkit-broker.service'
RUNNER_SERVICE = 'researchkit-runner.service'
DATA_MANAGER_SERVICE = 'researchkit-data-manager.service'
SERVICE_USER = 'researchkit-web'
PURGE_CLI = Path('/usr/local/sbin/rkweb-purge')
_MIGRATED_FILE_TABLES = (
    'submission_files', 'web_asset_candidate_files', 'web_asset_published_files',
    'web_asset_contracts', 'web_job_artifacts', 'web_central_builtin_files',
    'web_central_builtin_contracts',
)


def _path_parts(path):
    path = Path(path)
    return list(reversed([path] + list(path.parents)))


def path_errors(path, kind=None, owners=(0,)):
    """Return errors for an existing path and every ancestor without following links."""
    path = Path(path)
    errors = []
    for current in _path_parts(path):
        try:
            info = current.lstat()
        except OSError:
            errors.append('%s is missing' % current)
            break
        if stat.S_ISLNK(info.st_mode):
            errors.append('%s is a symbolic link' % current)
            break
        if info.st_uid not in owners:
            errors.append('%s is not owned by an allowed account' % current)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            errors.append('%s is group or world writable' % current)
    if errors:
        return errors
    try:
        mode = path.lstat().st_mode
    except OSError:
        return ['%s is missing' % path]
    if kind == 'directory' and not stat.S_ISDIR(mode):
        errors.append('%s is not a directory' % path)
    if kind == 'regular-file' and not stat.S_ISREG(mode):
        errors.append('%s is not a regular file' % path)
    return errors


def _tree_errors(source):
    """Reject source links, special files and hard links before a root copy."""
    errors = []
    source = Path(source)
    if not source.exists():
        return ['%s is missing' % source]
    for ancestor in (source,) + tuple(source.parents):
        if ancestor == Path('/'):
            break
        if os.path.lexists(str(ancestor)) and stat.S_ISLNK(ancestor.lstat().st_mode):
            errors.append('%s has a symbolic-link ancestor' % source)
            break
    try:
        source_info = source.lstat()
    except OSError:
        return ['%s cannot be inspected' % source]
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        return ['%s is not a regular source directory' % source]
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            target = current_path / name
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                errors.append('%s is not a regular source path' % target)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                errors.append('%s is a hardlinked source file' % target)
    return errors


def _purge_source_path():
    return ROOT / 'tools' / 'rkweb-purge.py'


def source_tree_errors():
    errors = _tree_errors(ROOT / 'core' / 'researchkit')
    if not LOCK.exists():
        errors.append('%s is missing' % LOCK)
    else:
        info = LOCK.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            errors.append('%s is not a regular unlinked source file' % LOCK)
    purge_src = _purge_source_path()
    if INSTALL == Path('/opt/researchkit-web') or os.path.lexists(str(purge_src)):
        if not purge_src.exists() and not os.path.lexists(str(purge_src)):
            errors.append('%s is missing' % purge_src)
        else:
            for ancestor in (purge_src,) + tuple(purge_src.parents):
                if ancestor == Path('/'):
                    break
                if os.path.lexists(str(ancestor)) and stat.S_ISLNK(ancestor.lstat().st_mode):
                    errors.append('%s has a symbolic-link ancestor' % purge_src)
                    break
            pinfo = purge_src.lstat()
            if stat.S_ISLNK(pinfo.st_mode) or not stat.S_ISREG(pinfo.st_mode) or pinfo.st_nlink != 1:
                errors.append('%s is not a regular unlinked source file' % purge_src)
    return errors


def validate_service_account(account):
    shell = Path(account.pw_shell or '').name.lower()
    if account.pw_uid == 0 or account.pw_uid >= 1000:
        raise RuntimeError('service account must be a non-root system UID')
    if shell not in ('false', 'nologin'):
        raise RuntimeError('service account must have a nologin shell')
    try:
        group = grp.getgrgid(account.pw_gid)
    except KeyError as exc:
        raise RuntimeError('service account primary group does not exist') from exc
    if group.gr_name != SERVICE_USER:
        raise RuntimeError('service account must use its dedicated primary group')


def service_state():
    result = subprocess.run(['systemctl', 'show', '--property=ActiveState', '--value', SERVICE],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('could not query broker service state: %s' % result.stderr.strip())
    return result.stdout.strip()


def runner_service_state():
    result = subprocess.run(['systemctl', 'show', '--property=ActiveState', '--value', RUNNER_SERVICE],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('could not query runner service state: %s' % result.stderr.strip())
    return result.stdout.strip()


def runner_configured():
    """Read the root-protected policy afresh before a code-changing update step."""
    errors = path_errors(POLICY, 'regular-file')
    if errors:
        raise RuntimeError('cannot read policy to determine runner configuration: %s' % '; '.join(errors))
    try:
        policy = json.loads(POLICY.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('cannot read policy to determine runner configuration: %s' % exc) from exc
    if not isinstance(policy, dict):
        raise RuntimeError('cannot read policy to determine runner configuration: policy must be a JSON object')
    return 'runner' in policy


def publisher_preflight():
    errors = path_errors(POLICY, 'regular-file')
    if errors:
        raise RuntimeError('cannot read publisher configuration: ' + '; '.join(errors))
    policy = json.loads(POLICY.read_text(encoding='utf-8'))
    if not isinstance(policy, dict):
        raise RuntimeError('publisher policy must be a JSON object')
    if 'dataset_management_config' not in policy:
        return False
    result = subprocess.run(['systemctl', 'show', DATA_MANAGER_SERVICE, '-p', 'ActiveState', '--value'],
                            text=True, capture_output=True, timeout=10)
    if result.returncode or result.stdout.strip() != 'inactive':
        raise RuntimeError('administrator must run "systemctl stop %s" before updating publisher code' % DATA_MANAGER_SERVICE)
    return True


def runner_preflight():
    """Fail closed while an installed runner could execute mixed-version code."""
    publisher_preflight()
    if not runner_configured():
        return False
    state = runner_service_state()
    if state != 'inactive':
        raise RuntimeError('runner service is %s; administrator must run "systemctl stop %s" before update and start it manually after update' %
                           (state or 'unknown', RUNNER_SERVICE))
    return True


def systemctl(*arguments):
    subprocess.run(['systemctl'] + list(arguments), check=True)


def wait_for_socket(path, uid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = Path(path).lstat()
        except OSError:
            time.sleep(0.1)
            continue
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o666:
            return
        time.sleep(0.1)
    raise RuntimeError('broker socket was not ready within %.1f seconds' % timeout)


def dependency_lock_matches():
    try:
        return LOCK.read_bytes() == INSTALLED_LOCK.read_bytes()
    except OSError:
        return False


def manifest_directory_status(*, repair=False):
    """Inspect/repair only the fixed root-trusted shared manifest directory.

    A missing directory is left for the publisher.  Never follow a link or
    normalize an unsafe owner/writable path into a supposedly trusted one.
    """
    directory = ETC / 'dataset-manifests'
    result = {'path': str(directory), 'exists': False, 'repair_required': False}
    try:
        before = directory.lstat()
    except FileNotFoundError:
        return result
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_mode & 0o022:
        raise RuntimeError('unsafe dataset manifest directory type or writable mode: %s' % directory)
    errors = path_errors(directory, 'directory')
    if errors:
        raise RuntimeError('unsafe dataset manifest directory: %s' % '; '.join(errors))
    mode = stat.S_IMODE(before.st_mode)
    result.update(exists=True, mode='%04o' % mode, repair_required=mode != 0o755)
    if not repair or not result['repair_required']:
        return result
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(str(directory), flags)
    try:
        current = os.fstat(fd)
        if ((current.st_dev, current.st_ino, current.st_uid, current.st_mode) !=
                (before.st_dev, before.st_ino, before.st_uid, before.st_mode)):
            raise RuntimeError('dataset manifest directory changed during update')
        os.fchmod(fd, 0o755)
        os.fsync(fd)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o755:
            raise RuntimeError('dataset manifest directory mode repair failed')
    finally:
        os.close(fd)
    result.update(mode='0755', repair_required=False, repaired=True)
    return result


def prerequisite_errors():
    errors = source_tree_errors()
    for path, kind in ((INSTALL, 'directory'), (APP, 'directory'),
                       (PACKAGE.parent, 'directory'), (PACKAGE, 'directory'),
                       (INSTALL / 'venv', 'directory'), (INSTALLED_LOCK, 'regular-file'),
                       (POLICY, 'regular-file')):
        errors.extend(path_errors(path, kind))
    if not dependency_lock_matches():
        errors.append('installed dependency lock differs from the update source; dependency updates are not supported')
    try:
        account = pwd.getpwnam(SERVICE_USER)
        validate_service_account(account)
    except (KeyError, RuntimeError) as exc:
        errors.append(str(exc))
        account = None
    if account is not None:
        for path, kind in ((RUNTIME, 'directory'), (STORE, 'directory')):
            errors.extend(path_errors(path, kind, owners=(0, account.pw_uid)))
        try:
            socket_info = SOCKET.lstat()
            if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != account.pw_uid \
                    or stat.S_IMODE(socket_info.st_mode) != 0o666:
                errors.append('broker socket does not have the expected owner, type, and mode')
        except OSError:
            errors.append('broker socket is missing')
    try:
        policy = json.loads(POLICY.read_text(encoding='utf-8'))
        if policy.get('broker', {}).get('uid') != (account.pw_uid if account else None):
            errors.append('policy broker UID does not match the service account')
        if policy.get('broker', {}).get('username') != SERVICE_USER:
            errors.append('policy broker username does not match the service account')
        if policy.get('broker_socket') != str(SOCKET) or policy.get('submission_store') != str(STORE):
            errors.append('policy socket or submission-store path does not match this installation')
        if policy.get('code_root') != str(APP):
            errors.append('policy code root does not match the installed application')
    except (OSError, ValueError, TypeError) as exc:
        errors.append('policy cannot be read safely: %s' % exc)
    try:
        manifest_directory_status()
    except (RuntimeError, OSError) as exc:
        errors.append(str(exc))
    try:
        runner_preflight()
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        if service_state() != 'active':
            errors.append('broker service is not active before update')
    except RuntimeError as exc:
        errors.append(str(exc))
    return errors, account


def migration_capacity_report(account):
    """Estimate the free space needed for an offline immutable-body migration.

    The updater makes a root-owned SQLite snapshot, while a first migration
    can also make the broker-owned compatibility backup and temporarily hold
    both old and normalized page sets.  Grouping requirements by device keeps
    the estimate conservative when /opt and /var share a filesystem.
    """
    result = {'database_exists': False, 'content_layer_exists': False,
              'legacy_file_tables': [], 'requirements': [], 'errors': [], 'ready': True}
    if account is None:
        result['errors'].append('cannot estimate migration capacity without a service account')
        result['ready'] = False
        return result
    database = _database_path()
    try:
        info = database.lstat()
    except FileNotFoundError:
        return result
    result['database_exists'] = True
    errors = _database_file_errors(database, account.pw_uid, owner=account.pw_uid)
    if errors:
        result['errors'].extend(errors)
        result['ready'] = False
        return result
    try:
        source = sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True)
        try:
            kinds = dict(source.execute(
                "SELECT name,type FROM sqlite_master WHERE name IN (%s)" % ','.join('?' * len(_MIGRATED_FILE_TABLES)),
                _MIGRATED_FILE_TABLES,
            ).fetchall())
            result['content_layer_exists'] = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_content_blobs'"
            ).fetchone() is not None
        finally:
            source.close()
    except sqlite3.Error as exc:
        result['errors'].append('cannot inspect submission database for migration capacity: %s' % exc)
        result['ready'] = False
        return result
    result['legacy_file_tables'] = sorted(name for name, kind in kinds.items() if kind == 'table')
    size = int(info.st_size)
    # The updater snapshot always requires one full database copy.  Only an
    # old body-table layout needs additional store-volume headroom.  A current
    # content-addressed database does not get penalized for a nonexistent
    # first-migration backup during a read-only --check.
    requests = [(INSTALL, size, 'updater SQLite snapshot')]
    if not result['content_layer_exists']:
        requests.append((STORE, size * 3, 'first body-store migration and broker backup'))
    elif result['legacy_file_tables']:
        requests.append((STORE, size * 2, 'remaining body-table normalization'))
    grouped = {}
    for path, needed, label in requests:
        try:
            device = path.stat().st_dev
            free = shutil.disk_usage(path).free
        except OSError as exc:
            result['errors'].append('cannot inspect free space for %s: %s' % (path, exc))
            result['ready'] = False
            continue
        entry = grouped.setdefault(device, {'path': str(path), 'required_bytes': 0,
                                            'available_bytes': free, 'reasons': []})
        entry['required_bytes'] += needed
        entry['reasons'].append(label)
    result['requirements'] = sorted(grouped.values(), key=lambda item: item['path'])
    for entry in result['requirements']:
        if entry['available_bytes'] < entry['required_bytes']:
            result['errors'].append('insufficient free space on %s: need %d bytes, have %d bytes' %
                                    (entry['path'], entry['required_bytes'], entry['available_bytes']))
    result['ready'] = not result['errors']
    return result


def report(args):
    errors, account = prerequisite_errors()
    capacity = migration_capacity_report(account)
    errors.extend(error for error in capacity['errors'] if error not in errors)
    result = {
        'schema': 'researchkit.web-update-check/v1',
        'root': str(ROOT),
        'package': str(PACKAGE),
        'dependency_lock_matches': dependency_lock_matches(),
        'service_user_uid': account.pw_uid if account else None,
        'migration_capacity': capacity,
        'errors': errors,
        'ready': not errors,
    }
    try:
        result['manifest_directory'] = manifest_directory_status()
    except (RuntimeError, OSError) as exc:
        if str(exc) not in errors:
            errors.append(str(exc))
        result['ready'] = False
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result, account


def _remove_tree_if_ours(path):
    """Cleanup only a path created by this invocation, never a pre-existing path."""
    if path and path.exists() and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


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
    """Take a non-blocking root-only lock for the entire mutable update."""
    parent_errors = path_errors(INSTALL, 'directory')
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
            fd = os.open(str(UPDATE_LOCK), flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(str(UPDATE_LOCK), flags)
    else:
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError('update lock is a symbolic link')
        fd = os.open(str(UPDATE_LOCK), flags)
    try:
        if created:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
        errors = _lock_file_errors(os.fstat(fd))
        if errors:
            raise RuntimeError('; '.join(errors))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another ResearchKit Web update is already running') from exc
        return fd
    except Exception:
        os.close(fd)
        raise


def _stage_source():
    staging = INSTALL / ('.update-staging-%s' % uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    try:
        package = staging / 'researchkit'
        # Preserve a discovered source symlink instead of following it as root;
        # the second source scan below rejects it before anything is installed.
        shutil.copytree(ROOT / 'core' / 'researchkit', package, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        staged_errors = _tree_errors(package)
        if staged_errors:
            raise RuntimeError('staged package is unsafe: %s' % '; '.join(staged_errors))
        for current, directories, files in os.walk(package):
            os.chown(current, 0, 0)
            os.chmod(current, 0o755)
            for name in files:
                target = Path(current) / name
                os.chown(target, 0, 0)
                os.chmod(target, 0o644)
        compiler = INSTALL / 'venv' / 'bin' / 'python'
        compiler_errors = path_errors(compiler, 'regular-file')
        if compiler_errors:
            raise RuntimeError('unsafe installed static compiler: %s' % '; '.join(compiler_errors))
        script = ('import compileall, sys; '
                  'raise SystemExit(0 if compileall.compile_dir(sys.argv[1], quiet=1, force=True) else 1)')
        subprocess.run([str(compiler), '-I', '-c', script, str(package)], check=True)
        for current, directories, _files in os.walk(package, topdown=False):
            for name in directories:
                if name == '__pycache__':
                    shutil.rmtree(Path(current) / name)
        return staging, package
    except Exception:
        _remove_tree_if_ours(staging)
        raise


def _new_backup_path():
    backups = INSTALL / 'backups'
    if not backups.exists():
        backups.mkdir(mode=0o700)
        os.chown(backups, 0, 0)
    errors = path_errors(backups, 'directory')
    if errors:
        raise RuntimeError('unsafe backup directory: %s' % '; '.join(errors))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = backups / ('researchkit-%s-%s' % (stamp, uuid.uuid4().hex))
    if backup.exists() or os.path.lexists(str(backup)):
        raise RuntimeError('unexpected existing backup destination: %s' % backup)
    return backup


def _database_path():
    return STORE / 'submissions.sqlite3'


def _database_file_errors(path, uid, *, owner=0):
    """Validate a single private SQLite file without following a link."""
    try:
        info = Path(path).lstat()
    except OSError as exc:
        return ['%s cannot be inspected: %s' % (path, exc)]
    errors = []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        errors.append('%s is not a regular database file' % path)
    if info.st_uid != owner:
        errors.append('%s has an unexpected owner' % path)
    if info.st_nlink != 1:
        errors.append('%s has unexpected hard links' % path)
    if stat.S_IMODE(info.st_mode) != 0o600:
        errors.append('%s does not have mode 0600' % path)
    return errors


def _database_backup_path(code_backup):
    """Keep the SQLite snapshot next to, never inside, a code backup."""
    target = Path(code_backup).parent / (Path(code_backup).name + '.submissions.sqlite3')
    if target.exists() or os.path.lexists(str(target)):
        raise RuntimeError('unexpected existing database backup destination: %s' % target)
    return target


def _snapshot_database(code_backup, uid):
    """Create a root-owned consistent backup before any new code can migrate it."""
    errors = path_errors(STORE, 'directory', owners=(0, uid))
    if errors:
        raise RuntimeError('unsafe submission store for database backup: %s' % '; '.join(errors))
    database = _database_path()
    try:
        database.lstat()
    except FileNotFoundError:
        # A never-used installation has no pre-update database to preserve.
        return None
    errors = _database_file_errors(database, uid, owner=uid)
    if errors:
        raise RuntimeError('unsafe submission database: %s' % '; '.join(errors))
    backup = _database_backup_path(code_backup)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(backup), flags, 0o600)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    try:
        source = sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        descriptor = os.open(str(backup), os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        # The target was created only by this invocation and is outside all
        # service paths.  Do not retain a partially written backup as evidence.
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    errors = _database_file_errors(backup, uid, owner=0)
    if errors:
        raise RuntimeError('database backup failed validation: %s' % '; '.join(errors))
    return backup


def _copy_private_database(source, target, source_owner, target_owner, target_group):
    """Copy one vetted SQLite file without following either endpoint."""
    source = Path(source)
    target = Path(target)
    errors = _database_file_errors(source, source_owner, owner=source_owner)
    if errors:
        raise RuntimeError('source database is unsafe: %s' % '; '.join(errors))
    before = source.lstat()
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
    source_fd = os.open(str(source), flags)
    destination_fd = None
    created = False
    try:
        source_info = os.fstat(source_fd)
        if (source_info.st_dev, source_info.st_ino, source_info.st_uid, source_info.st_mode,
                source_info.st_nlink, source_info.st_size) != (
                    before.st_dev, before.st_ino, before.st_uid, before.st_mode,
                    before.st_nlink, before.st_size):
            raise RuntimeError('source database changed while preparing rollback')
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
        destination_fd = os.open(str(target), destination_flags, 0o600)
        created = True
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            offset = 0
            while offset < len(block):
                written = os.write(destination_fd, block[offset:])
                if written <= 0:
                    raise RuntimeError('could not write rollback database evidence')
                offset += written
        os.fchown(destination_fd, target_owner, target_group)
        os.fchmod(destination_fd, 0o600)
        os.fsync(destination_fd)
    except Exception:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        try:
            if created and target.exists() and not target.is_symlink():
                target.unlink()
        except OSError:
            pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _restore_database(backup, uid, evidence_directory=None):
    """Restore a snapshot without cross-device moves; retain failed bytes first."""
    if evidence_directory is None:
        evidence_directory = Path(backup).parent if backup is not None else None
    if evidence_directory is None:
        return False, 'no trusted location is available for failed database evidence'
    evidence_directory = Path(evidence_directory)
    errors = path_errors(evidence_directory, 'directory')
    if errors:
        return False, 'database evidence directory is unsafe: %s' % '; '.join(errors)
    if backup is not None:
        backup = Path(backup)
        errors = _database_file_errors(backup, uid, owner=0)
        if errors:
            return False, 'database backup is unsafe: %s' % '; '.join(errors)
    errors = path_errors(STORE, 'directory', owners=(0, uid))
    if errors:
        return False, 'submission store is unsafe during database rollback: %s' % '; '.join(errors)
    database = _database_path()
    try:
        current = database.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        current_errors = _database_file_errors(database, uid, owner=uid)
        if current_errors:
            return False, 'new submission database is unsafe: %s' % '; '.join(current_errors)
    failed = evidence_directory / ('failed-new-database-%s.sqlite3' % uuid.uuid4().hex)
    temporary = STORE / ('.restore-submissions-%s.sqlite3' % uuid.uuid4().hex)
    try:
        if current is not None:
            # /var service data and /opt code backups may live on different
            # mounts.  Copy evidence first; never remove the live file until
            # both the evidence and same-directory replacement are durable.
            _copy_private_database(database, failed, uid, 0, 0)
        if backup is None:
            if current is not None:
                os.unlink(database)
        else:
            _copy_private_database(backup, temporary, 0, uid, os.stat(STORE).st_gid)
            os.replace(str(temporary), str(database))
        directory_fd = os.open(str(STORE), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if backup is not None:
            errors = _database_file_errors(database, uid, owner=uid)
            if errors:
                raise RuntimeError('; '.join(errors))
    except Exception as exc:
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        return False, 'database rollback could not complete; retained backup/evidence: %s' % exc
    if backup is None:
        if current is None:
            return True, 'no pre-update or newly created submission database existed'
        return True, 'new submission database removed; failed new database retained at %s' % failed
    return True, 'submission database restored; failed new database retained at %s' % failed


def _run_maintenance():
    """Run new application migration code only as the broker service account."""
    interpreter = INSTALL / 'venv' / 'bin' / 'python'
    errors = path_errors(interpreter, 'regular-file')
    if errors:
        raise RuntimeError('unsafe installed maintenance interpreter: %s' % '; '.join(errors))
    subprocess.run(['runuser', '--user', SERVICE_USER, '--', str(interpreter), '-I', '-m',
                    'researchkit.web_maintenance', '--policy', str(POLICY), '--migrate', '--retire-web-execution'],
                   check=True)


def _stop_and_confirm():
    systemctl('stop', SERVICE)
    if service_state() != 'inactive':
        raise RuntimeError('broker did not become inactive; code was not changed')


def _start_and_verify(uid):
    systemctl('start', SERVICE)
    if service_state() != 'active':
        raise RuntimeError('broker did not become active')
    wait_for_socket(SOCKET, uid)


def _restore_purge_cli(purge_backup, purge_touched):
    if not purge_touched:
        return None
    try:
        if purge_backup is None:
            if PURGE_CLI.exists() or os.path.lexists(str(PURGE_CLI)):
                os.unlink(str(PURGE_CLI))
            return 'removed newly installed %s' % PURGE_CLI
        tmp_restore = PURGE_CLI.parent / ('.restore-rkweb-purge-' + uuid.uuid4().hex)
        shutil.copy2(str(purge_backup), str(tmp_restore))
        os.chown(tmp_restore, 0, 0)
        os.chmod(tmp_restore, 0o755)
        os.replace(str(tmp_restore), str(PURGE_CLI))
        return 'restored %s' % PURGE_CLI
    except Exception as exc:
        raise RuntimeError('could not restore %s: %s' % (PURGE_CLI, exc)) from exc


def _rollback(staging, backup, switched, broker_stopped, uid, database_backup=None, database_touched=False,
              purge_backup=None, purge_touched=False):
    """Restore a separate copy of the old package while retaining old/new evidence."""
    try:
        if not broker_stopped:
            return False, 'broker stop was not confirmed; code was not changed'
        if service_state() != 'inactive':
            _stop_and_confirm()
        database_detail = None
        if database_touched:
            restored, database_detail = _restore_database(
                database_backup, uid, backup.parent if backup is not None else None)
            if not restored:
                return False, database_detail
        purge_detail = _restore_purge_cli(purge_backup, purge_touched)
        if not switched or not backup or not backup.exists():
            _start_and_verify(uid)
            parts = ['old code was not moved and the broker was restarted', database_detail, purge_detail]
            return True, '; '.join(p for p in parts if p)
        restore = staging / 'restore-old'
        shutil.copytree(backup, restore)
        failed = backup.parent / ('failed-new-%s' % uuid.uuid4().hex)
        if PACKAGE.exists() or os.path.lexists(str(PACKAGE)):
            os.rename(str(PACKAGE), str(failed))
        os.rename(str(restore), str(PACKAGE))
        _start_and_verify(uid)
        parts = ['old code restored; backup retained at %s and failed new code retained at %s' % (backup, failed), database_detail, purge_detail]
        return True, '; '.join(p for p in parts if p)
    except Exception as exc:
        return False, 'automatic rollback could not complete; retained staging and backup evidence: %s' % exc


def update(args):
    checked, account = report(args)
    if args.check or args.dry_run:
        return 0 if checked['ready'] else 1
    if os.geteuid() != 0:
        print('root is required; run via a sudo-capable administrator account', file=sys.stderr)
        return 2
    if not checked['ready'] or account is None:
        print('update prerequisites are unsafe or incomplete; see --check output', file=sys.stderr)
        return 1
    old_umask = os.umask(0o077)
    update_lock_fd = None
    staging = None
    backup = None
    database_backup = None
    database_touched = False
    purge_backup = None
    purge_touched = False
    switched = False
    broker_stopped = False
    try:
        update_lock_fd = _acquire_update_lock()
        staging, staged_package = _stage_source()
        # Recheck the service immediately before stopping it; no code has changed yet.
        if service_state() != 'active':
            raise RuntimeError('broker service stopped before the update could begin')
        # The runner is deliberately not managed here.  It must remain down
        # throughout a code-only broker update, including after staging.
        runner_preflight()
        _stop_and_confirm()
        broker_stopped = True
        manifest_repair = manifest_directory_status(repair=True)
        if manifest_repair.get('repaired'):
            print('repaired dataset manifest directory mode: %s -> 0755' % manifest_repair['path'])
        backup = _new_backup_path()
        database_backup = _snapshot_database(backup, account.pw_uid)
        purge_src = _purge_source_path()
        install_purge = purge_src.is_file() and PURGE_CLI.parent.is_dir() and (
            INSTALL == Path('/opt/researchkit-web') or PURGE_CLI.parent != Path('/usr/local/sbin')
        )
        if install_purge:
            if os.path.lexists(str(PURGE_CLI)):
                cli_errors = path_errors(PURGE_CLI, 'regular-file')
                if cli_errors:
                    raise RuntimeError('unsafe existing rkweb-purge CLI: %s' % '; '.join(cli_errors))
                purge_backup = backup.parent / (backup.name + '.rkweb-purge')
                shutil.copy2(str(PURGE_CLI), str(purge_backup))
                os.chown(purge_backup, 0, 0)
                os.chmod(purge_backup, 0o600)
        os.rename(str(PACKAGE), str(backup))
        switched = True
        os.rename(str(staged_package), str(PACKAGE))
        if install_purge:
            tmp_cli = PURGE_CLI.parent / ('.rkweb-purge-' + uuid.uuid4().hex)
            try:
                shutil.copy2(str(purge_src), str(tmp_cli))
                os.chown(tmp_cli, 0, 0)
                os.chmod(tmp_cli, 0o755)
                os.replace(str(tmp_cli), str(PURGE_CLI))
                purge_touched = True
            except Exception:
                if tmp_cli.exists() or os.path.lexists(str(tmp_cli)):
                    try:
                        tmp_cli.unlink()
                    except OSError:
                        pass
                raise
        database_touched = True
        _run_maintenance()
        _start_and_verify(account.pw_uid)
    except Exception as exc:
        _restored, detail = _rollback(staging, backup, switched, broker_stopped, account.pw_uid,
                                      database_backup, database_touched, purge_backup, purge_touched)
        if staging is not None and not switched:
            _remove_tree_if_ours(staging)
        print('update failed: %s; %s' % (exc, detail), file=sys.stderr)
        return 1
    finally:
        if update_lock_fd is not None:
            os.close(update_lock_fd)
        os.umask(old_umask)
    _remove_tree_if_ours(staging)
    print('code backup: %s' % backup)
    print('database backup: %s' % (database_backup if database_backup is not None else 'no pre-update database'))
    print('updated and active: %s; all users must restart their own rk-web process before using the new Web code' % SERVICE)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='safely update installed ResearchKit Web application code')
    parser.add_argument('--check', action='store_true', help='read-only prerequisite report')
    parser.add_argument('--dry-run', action='store_true', help='read-only update plan report')
    return update(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
