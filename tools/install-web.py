#!/usr/bin/env python3
"""Install the root-owned ResearchKit Web broker without touching user projects.

This program is intentionally compatible with the RHEL 9 system Python 3.9.
Use --check or --dry-run first; neither writes files nor invokes dnf/pip.
"""
import argparse
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
ETC = Path('/etc/researchkit-web')
STORE = Path('/var/lib/researchkit-web/submissions')
RUNTIME = Path('/run/researchkit-web')
UNIT = Path('/etc/systemd/system/researchkit-broker.service')
SERVICE_USER = 'researchkit-web'
LOCK = ROOT / 'deploy' / 'requirements-web.lock'
WHEELS = ROOT / 'deploy' / 'wheels'
REQUIRED_SOURCE = (
    ROOT / 'pyproject.toml', ROOT / 'core' / 'researchkit',
    ROOT / 'library' / 'cases' / 'hello',
    ROOT / 'library' / 'algorithms' / 'example.scale',
    ROOT / 'library' / 'contracts' / 'scalar', LOCK, WHEELS,
    ROOT / 'deploy' / 'researchkit-broker.service',
    ROOT / 'core' / 'researchkit' / 'web_app.py',
    ROOT / 'core' / 'researchkit' / 'web_broker.py',
)



def source_tree_errors():
    """Reject source links, special files, and hardlinks before root copies it."""
    errors = []
    roots = [ROOT / 'core' / 'researchkit', ROOT / 'library' / 'cases' / 'hello',
             ROOT / 'library' / 'algorithms' / 'example.scale',
             ROOT / 'library' / 'contracts' / 'scalar', WHEELS]
    for root in roots:
        for ancestor in (root,) + tuple(root.parents):
            if ancestor == Path('/'):
                break
            if os.path.lexists(str(ancestor)) and stat.S_ISLNK(ancestor.lstat().st_mode):
                errors.append('%s has a symbolic-link ancestor' % root)
                break
        if not root.exists():
            continue
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            errors.append('%s is not a regular source directory' % root)
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories if name != '__pycache__']
            for name in directories + files:
                if name == '__pycache__' or name.endswith('.pyc'):
                    continue
                target = current_path / name
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    errors.append('%s is not a regular source path' % target)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    errors.append('%s is a hardlinked source file' % target)
    for target in (ROOT / 'pyproject.toml', LOCK, ROOT / 'deploy' / 'researchkit-broker.service'):
        if target.exists():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                errors.append('%s is not a regular unlinked source file' % target)
    return errors


def safe_existing(path, kinds, owners=(0,)):
    """Do not follow or modify a pre-existing foreign or unsafe target."""
    path = Path(path)
    if not os.path.lexists(str(path)):
        return False
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid not in owners:
        raise RuntimeError('unsafe existing target: %s' % path)
    if kinds == 'directory' and not stat.S_ISDIR(info.st_mode):
        raise RuntimeError('existing target is not a directory: %s' % path)
    if kinds == 'regular-file' and not stat.S_ISREG(info.st_mode):
        raise RuntimeError('existing target is not a regular file: %s' % path)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError('existing target is group or world writable: %s' % path)
    return True


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


def cleanup_failed_install(paths, started):
    unit = UNIT
    if started:
        try:
            run(['systemctl', 'disable', '--now', 'researchkit-broker.service'])
        except Exception:
            # Keep all code/configuration/data if stopping could not be confirmed.
            print('could not stop the new broker; retained all installation paths for recovery', file=sys.stderr)
            return False
    if unit in paths:
        try: unit.unlink()
        except OSError: pass
        try: run(['systemctl', 'daemon-reload'])
        except Exception: pass
    seen = set()
    for path in reversed(paths):
        if path in seen or path == unit:
            continue
        seen.add(path)
        try:
            if path == INSTALL and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.is_dir() and not path.is_symlink():
                # Never recursively remove service data or a configuration directory.
                path.rmdir()
            elif path.exists() or os.path.lexists(str(path)):
                path.unlink()
        except OSError:
            pass
    return True

def trusted_root_path(path):
    """A broker interpreter must resolve through root-owned, non-writable paths."""
    try:
        path = Path(path).resolve(strict=True)
    except OSError:
        return False
    if not path.is_file():
        return False
    current = Path('/')
    for part in path.parts[1:]:
        current /= part
        info = current.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    return True


def python_version(path):
    try:
        result = subprocess.run([str(path), '-c',
            'import json,sys; print(json.dumps(list(sys.version_info[:3])))'],
            check=True, text=True, capture_output=True)
        return tuple(json.loads(result.stdout))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def choose_python(explicit=None):
    candidates = [Path(explicit)] if explicit else [
        Path('/usr/bin/python3.12'), Path('/usr/bin/python3.11'),
        Path('/usr/local/bin/python3.12'), Path('/usr/local/bin/python3.11'),
        Path('/usr/bin/python3'),
    ]
    for candidate in candidates:
        if not trusted_root_path(candidate):
            continue
        version = python_version(candidate)
        if version and version[:2] in ((3, 11), (3, 12)):
            return candidate, version
    return None, None


def report(args):
    interpreter, version = choose_python(args.python)
    missing = [str(path) for path in REQUIRED_SOURCE if not path.exists()]
    source_errors = source_tree_errors()
    result = {
        'schema': 'researchkit.web-install-check/v1',
        'root': str(ROOT),
        'python': str(interpreter) if interpreter else None,
        'python_version': list(version) if version else None,
        'missing_source': missing,
        'source_security_errors': source_errors,
        'would_install_system_python': bool(args.install_system_python and not interpreter),
        'ready': bool(interpreter and not missing and not source_errors),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result, interpreter


def run(command):
    subprocess.run(command, check=True)


def run_maintenance(environment, policy_file):
    """Initialize central storage through the installed broker interpreter.

    This script deliberately remains importable by the RHEL system Python 3.9.
    The Web package itself is only imported by the installed 3.11+ virtual
    environment after privileges have been dropped to the broker account.
    """
    run(['runuser', '--user', SERVICE_USER, '--', str(Path(environment) / 'bin' / 'python'),
         '-I', '-m', 'researchkit.web_maintenance', '--policy', str(policy_file), '--migrate'])


def secure_source_tree(path):
    """Root owns copied source; do not apply this to the generated venv."""
    for current, directories, files in os.walk(path):
        os.chown(current, 0, 0)
        os.chmod(current, 0o755)
        for name in files:
            target = Path(current) / name
            os.chown(target, 0, 0)
            os.chmod(target, 0o644)


def install(args):
    checked, interpreter = report(args)
    if args.check or args.dry_run:
        return 0 if checked['ready'] else 1
    if os.geteuid() != 0:
        print('root is required; run: sudo /usr/bin/python3 tools/install-web.py [options]', file=sys.stderr)
        return 2
    if checked['missing_source'] or checked['source_security_errors']:
        print('trusted install source is missing or unsafe; see --check output', file=sys.stderr)
        return 1
    if not interpreter and args.install_system_python:
        run(['dnf', 'install', '-y', 'python3.12', 'python3.12-pip'])
        interpreter, _version = choose_python(args.python or '/usr/bin/python3.12')
    if not interpreter:
        print('no trusted Python 3.11 or 3.12 found; use --install-system-python explicitly', file=sys.stderr)
        return 1
    # Refuse unsafe pre-existing targets before creating any system state.
    if safe_existing(INSTALL, 'directory'):
        print('%s already exists; refuse to replace a trusted installation automatically' % INSTALL, file=sys.stderr)
        return 1
    for directory in (ETC, STORE.parent):
        safe_existing(directory, 'directory', owners=(0,))
    policy_existed = safe_existing(ETC / 'policy.json', 'regular-file', owners=(0,))
    launcher_existed = safe_existing(Path('/usr/local/bin/rk-web'), 'regular-file', owners=(0,))
    unit_existed = safe_existing(Path('/etc/systemd/system/researchkit-broker.service'), 'regular-file', owners=(0,))
    if policy_existed or launcher_existed or unit_existed:
        print('refuse to overwrite existing managed policy, launcher, or unit; remove it explicitly first', file=sys.stderr)
        return 1
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--no-create-home',
             '--shell', '/usr/sbin/nologin', SERVICE_USER])
        account = pwd.getpwnam(SERVICE_USER)
    validate_service_account(account)
    # Existing mutable service paths may belong only to root or this service UID.
    store_existed = safe_existing(STORE, 'directory', owners=(0, account.pw_uid))
    runtime_existed = safe_existing(RUNTIME, 'directory', owners=(0, account.pw_uid))
    if store_existed:
        info = STORE.lstat()
        if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
            print('existing submission store must already be broker-owned mode 0700', file=sys.stderr)
            return 1
    if runtime_existed:
        info = RUNTIME.lstat()
        if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o755:
            print('existing runtime directory must already be broker-owned mode 0755', file=sys.stderr)
            return 1
    created = []
    started = False
    previous_umask = os.umask(0o022)
    try:
        INSTALL.mkdir(mode=0o755, parents=True); created.append(INSTALL)
        app = INSTALL / 'app'; app.mkdir(mode=0o755)
        shutil.copy2(ROOT / 'pyproject.toml', app / 'pyproject.toml')
        (app / 'core').mkdir()
        shutil.copytree(ROOT / 'core' / 'researchkit', app / 'core' / 'researchkit', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for relative in ('cases/hello', 'algorithms/example.scale', 'contracts/scalar'):
            source = ROOT / 'library' / relative
            destination = app / 'library' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (app / 'deploy').mkdir()
        shutil.copy2(LOCK, app / 'deploy' / 'requirements-web.lock')
        shutil.copytree(WHEELS, app / 'deploy' / 'wheels', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        secure_source_tree(app)
        environment = INSTALL / 'venv'
        run([str(interpreter), '-I', '-m', 'venv', '--copies', str(environment)])
        pip = environment / 'bin' / 'pip'
        run([str(pip), 'install', '--no-index', '--find-links', str(app / 'deploy' / 'wheels'),
             '--require-hashes', '-r', str(app / 'deploy' / 'requirements-web.lock')])
        site = subprocess.check_output([str(environment / 'bin' / 'python'), '-I', '-c',
            'import site; print(site.getsitepackages()[0])'], text=True).strip()
        pth = Path(site) / 'researchkit-web.pth'
        pth.write_text(str(app / 'core') + '\n', encoding='utf-8')
        os.chown(pth, 0, 0); os.chmod(pth, 0o644)
        if not ETC.exists(): ETC.mkdir(mode=0o755, parents=True); created.append(ETC)
        policy_file = ETC / 'policy.json'
        policy = {
            'schema': 'researchkit.web-policy/v1', 'access_mode': 'ssh-users',
            'min_uid': 1000, 'teacher': 'ly',
            'broker': {'uid': account.pw_uid, 'username': SERVICE_USER},
            'broker_socket': '/run/researchkit-web/broker.sock',
            'submission_store': str(STORE), 'code_root': str(app),
        }
        policy_file.write_text(json.dumps(policy, indent=2) + '\n', encoding='utf-8')
        created.append(policy_file)
        os.chown(ETC, 0, 0); os.chmod(ETC, 0o755)
        os.chown(policy_file, 0, 0); os.chmod(policy_file, 0o644)
        if not store_existed:
            STORE.mkdir(mode=0o700, parents=True); created.append(STORE)
            os.chown(STORE, account.pw_uid, account.pw_gid); os.chmod(STORE, 0o700)
        if not runtime_existed:
            RUNTIME.mkdir(mode=0o755, parents=True); created.append(RUNTIME)
            os.chown(RUNTIME, account.pw_uid, account.pw_gid); os.chmod(RUNTIME, 0o755)
        # Initialize the schema and make the one-time central library copy
        # before exposing a broker socket.  The maintenance module validates
        # its own broker UID, so root never imports application code here.
        run_maintenance(environment, policy_file)
        launcher = Path('/usr/local/bin/rk-web')
        launcher.write_text('#!/bin/sh\nexec /opt/researchkit-web/venv/bin/python -I -m researchkit.web_app "$@"\n', encoding='utf-8')
        os.chown(launcher, 0, 0); os.chmod(launcher, 0o755)
        created.append(launcher)
        unit = Path('/etc/systemd/system/researchkit-broker.service')
        shutil.copy2(ROOT / 'deploy' / 'researchkit-broker.service', unit)
        os.chown(unit, 0, 0); os.chmod(unit, 0o644)
        created.append(unit)
        run(['systemctl', 'daemon-reload'])
        started = True
        run(['systemctl', 'enable', '--now', 'researchkit-broker.service'])
        run(['systemctl', 'is-active', '--quiet', 'researchkit-broker.service'])
        wait_for_socket('/run/researchkit-web/broker.sock', account.pw_uid)
    except Exception as exc:
        cleanup_failed_install(created, started)
        print('installation failed; newly created empty paths were cleaned while account, Python, pip, and any service data were retained: %s' % exc, file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
    print('installed and active: researchkit-broker.service')
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description='install ResearchKit Web broker')
    parser.add_argument('--check', action='store_true', help='read-only prerequisite report')
    parser.add_argument('--dry-run', action='store_true', help='read-only install plan report')
    parser.add_argument('--python', help='trusted root-owned Python >=3.11')
    parser.add_argument('--install-system-python', action='store_true',
                        help='when root, install only RHEL python3.12 and python3.12-pip via dnf')
    return install(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
