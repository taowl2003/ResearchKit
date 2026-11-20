#!/usr/bin/env python3
"""Administrator-only opt-in data storage/configuration; never imports raw data."""
import argparse
import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

POLICY = Path('/etc/researchkit-web/policy.json')
DEFAULT_ROOT = Path('/home/share/DerivedDatasets')
DEFAULT_RAW_ROOT = Path('/home/share/Datasets')
SYSTEMD_UNITS = Path('/etc/systemd/system')


def service_overrides(raw_root, derived_root, group):
    for path in (raw_root, derived_root):
        if any(char in str(path) for char in ('\n', '\r', '\x00', ':', '"', '\\', '%')):
            raise RuntimeError('data root contains unsupported systemd path characters')
    return {
        'researchkit-broker.service': (
            '[Service]\nProtectHome=false\n'
            'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0\n'
            'InaccessiblePaths=/root /run/user\n'
            f'BindReadOnlyPaths="{raw_root}"\nBindPaths="{derived_root}"\n'
            f'ReadWritePaths="{derived_root}"\nSupplementaryGroups={group}\n'),
        'researchkit-runner.service': (
            '[Service]\nProtectHome=false\n'
            'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0\n'
            'InaccessiblePaths=/root /run/user\n'
            f'BindReadOnlyPaths="{raw_root}" "{derived_root}"\n'),
    }


def _root_owned(info):
    return info.st_uid == 0


def safe_ancestors(path):
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('path must be absolute without ..')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
            raise RuntimeError('ancestor is not a trusted root-owned directory: ' + str(parent))


def atomic_json(path, value, mode):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write('\n'); temporary.flush(); os.fsync(temporary.fileno()); os.fchmod(temporary.fileno(), mode)
    os.replace(temporary.name, path)


def inspect(policy_path, derived_root, bundle=None, raw_root=DEFAULT_RAW_ROOT):
    errors = []
    try:
        safe_ancestors(policy_path)
        info = policy_path.lstat()
        if not stat.S_ISREG(info.st_mode) or not _root_owned(info) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise RuntimeError('policy must be a root-owned non-link regular file')
        policy = json.loads(policy_path.read_text())
        broker = pwd.getpwnam(policy['broker']['username'])
        runner = pwd.getpwnam(policy['runner']['username'])
        if broker.pw_uid != policy['broker']['uid'] or runner.pw_uid != policy['runner']['uid']:
            raise RuntimeError('service account identity changed')
        if broker.pw_uid < 1 or broker.pw_uid >= 1000 or runner.pw_uid < 1 or runner.pw_uid >= 1000:
            raise RuntimeError('non-root service accounts required')
        safe_ancestors(derived_root)
        safe_ancestors(raw_root)
        raw = raw_root.lstat()
        if not stat.S_ISDIR(raw.st_mode) or not _root_owned(raw) or raw.st_mode & 0o022:
            raise RuntimeError('raw root must be a trusted root-owned directory')
        if raw_root == derived_root or raw_root in derived_root.parents or derived_root in raw_root.parents:
            raise RuntimeError('raw and derived roots must be separate directories')
        overrides = service_overrides(raw_root, derived_root, grp.getgrgid(runner.pw_gid).gr_name)
        safe_ancestors(SYSTEMD_UNITS/'placeholder')
        for unit in overrides:
            target = SYSTEMD_UNITS/(unit+'.d')/'researchkit-data.conf'
            if target.parent.exists() or target.parent.is_symlink():
                safe_ancestors(target)
            if target.exists() or target.is_symlink():
                node = target.lstat()
                if not stat.S_ISREG(node.st_mode) or not _root_owned(node) or node.st_nlink != 1 or node.st_mode & 0o022:
                    raise RuntimeError('existing data service override is not trusted')
        if derived_root.exists() or derived_root.is_symlink():
            data = derived_root.lstat()
            if (not stat.S_ISDIR(data.st_mode) or data.st_uid != broker.pw_uid or data.st_gid != runner.pw_gid
                    or stat.S_IMODE(data.st_mode) != 0o2710):
                raise RuntimeError('existing derived root is not a configured private service store; will not take over')
        if policy.get('derived_dataset_root') not in (None, str(derived_root)):
            raise RuntimeError('policy already uses a different derived root; do not migrate automatically')
        if bundle is not None:
            if bundle.is_symlink() or not bundle.is_dir():
                raise RuntimeError('review bundle must be a regular directory')
            for path in bundle.rglob('*'):
                node = path.lstat()
                if stat.S_ISLNK(node.st_mode) or (not stat.S_ISDIR(node.st_mode) and not stat.S_ISREG(node.st_mode)) or (stat.S_ISREG(node.st_mode) and node.st_nlink != 1):
                    raise RuntimeError('unsafe review bundle entry')
            for name in ('dataset-trees.json', 'datasets.metadata.json', 'review.json'):
                path = bundle/name
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise RuntimeError('review bundle registry too large')
                json.loads(path.read_text())
            registry = json.loads((bundle/'dataset-trees.json').read_text())
            if (type(registry) is not dict or set(registry) != {'schema','datasets'}
                    or registry['schema'] != 'researchkit.web-dataset-trees/v1'
                    or type(registry['datasets']) is not list or len(registry['datasets']) > 4096):
                raise RuntimeError('reviewed registry schema invalid')
            metadata = json.loads((bundle/'datasets.metadata.json').read_text())
            allowed_metadata = {'name','description','wiki_ref','wiki_fact_state','provenance_ref','format','usage','license','student_conditions','tags'}
            if (type(metadata) is not dict or set(metadata) != {'schema','datasets'}
                    or metadata['schema'] != 'researchkit.web-dataset-metadata/v1'
                    or type(metadata['datasets']) is not dict or len(metadata['datasets']) > 4096):
                raise RuntimeError('reviewed metadata schema invalid')
            for record in metadata['datasets'].values():
                if type(record) is not dict or set(record) - allowed_metadata:
                    raise RuntimeError('reviewed metadata fields invalid')
                for field, value in record.items():
                    if field == 'tags':
                        if (type(value) is not list or len(value) > 32 or
                                any(not isinstance(tag, str) or not tag.strip() or len(tag) > 64 or len(tag.encode()) > 256
                                    for tag in value)):
                            raise RuntimeError('reviewed metadata fields invalid')
                    elif not isinstance(value,str) or len(value.encode()) > 16384:
                        raise RuntimeError('reviewed metadata fields invalid')
            manifest_root = policy_path.parent / 'dataset-manifests'
            names, identifiers = set(), set()
            for entry in registry['datasets']:
                if (type(entry) is not dict or set(entry) != {'dataset_id','manifest','sha256','total_size','file_count','readers'}
                        or not isinstance(entry['dataset_id'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',entry['dataset_id'])
                        or entry['dataset_id'] in identifiers or type(entry['total_size']) is not int or entry['total_size'] < 0
                        or type(entry['file_count']) is not int or not 0 <= entry['file_count'] <= 500000
                        or not isinstance(entry['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',entry['sha256'])):
                    raise RuntimeError('reviewed dataset identity or limits invalid')
                readers = entry['readers']
                if readers != 'platform-users':
                    if type(readers) is not list or any(type(reader) is not dict or set(reader) != {'uid','username'}
                            or type(reader['uid']) is not int or reader['uid'] < 1 or not isinstance(reader['username'],str)
                            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}',reader['username']) for reader in readers):
                        raise RuntimeError('reviewed dataset readers invalid')
                identifiers.add(entry['dataset_id'])
                target = Path(entry['manifest'])
                if target.parent != manifest_root or target.name != entry['dataset_id']+'.json' or target.name in names:
                    raise RuntimeError('reviewed manifest destination invalid')
                names.add(target.name)
                # A merged bundle need not re-copy old manifests.
                source = bundle/'manifests'/target.name
                checked = source if source.exists() else target
                import hashlib
                if checked.is_symlink() or checked.stat().st_size > 128*1024*1024 or hashlib.sha256(checked.read_bytes()).hexdigest() != entry['sha256']:
                    raise RuntimeError('reviewed manifest missing or changed')
                dataset_root = Path(json.loads(checked.read_text())['root'])
                dataset_root.relative_to(raw_root)
                if target.exists() and target.read_bytes() != checked.read_bytes():
                    raise RuntimeError('refusing to overwrite a published manifest version')
            old_path = policy.get('dataset_tree_registry')
            if old_path:
                old = json.loads(Path(old_path).read_text())
                existing = {entry['dataset_id']: entry for entry in old['datasets']}
                merged = {entry['dataset_id']: entry for entry in registry['datasets']}
                if any(merged.get(key) != value for key,value in existing.items()):
                    raise RuntimeError('bundle would remove/change an existing dataset; regenerate merged bundle')
        return {'ready': True, 'errors': [], 'raw_root': str(raw_root), 'derived_root': str(derived_root)}, policy, broker, runner
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        errors.append(str(exc))
    return {'ready': False, 'errors': errors, 'derived_root': str(derived_root)}, None, None, None


def configure(policy_path, derived_root, bundle=None, raw_root=DEFAULT_RAW_ROOT):
    if os.geteuid() != 0:
        raise RuntimeError('administrator root session required')
    report, policy, broker, runner = inspect(policy_path, derived_root, bundle, raw_root)
    if not report['ready']:
        raise RuntimeError('; '.join(report['errors']))
    state = subprocess.run(['systemctl', 'show', 'researchkit-runner.service', '-p', 'ActiveState', '--value'],
                           text=True, capture_output=True, check=True).stdout.strip()
    if state != 'inactive':
        raise RuntimeError('stop runner and confirm inactive before configuration')
    backup = Path(tempfile.mkdtemp(prefix='researchkit-data-config-backup.', dir='/root'))
    shutil.copy2(policy_path, backup/'policy.json')
    print('backup:', backup, flush=True)
    if not derived_root.exists():
        derived_root.mkdir(mode=0o700)
        os.chown(derived_root, broker.pw_uid, runner.pw_gid); derived_root.chmod(0o2710)
    # Allow the broker to preserve the protected service group on child versions.
    group = grp.getgrgid(runner.pw_gid).gr_name
    if broker.pw_uid not in (runner.pw_uid,):
        subprocess.run(['usermod', '-a', '-G', group, broker.pw_name], check=True)
    policy['derived_dataset_root'] = str(derived_root)
    if bundle is not None:
        directory = policy_path.parent/'dataset-manifests'
        if directory.exists():
            safe_ancestors(directory/'placeholder')
        else:
            directory.mkdir(mode=0o755)
        for source in (bundle/'manifests').glob('*.json'):
            target = directory/source.name
            if target.exists():
                if source.read_bytes() != target.read_bytes():
                    raise RuntimeError('published manifest changed')
                continue
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                stream.write(source.read_bytes()); stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(),0o644)
            os.replace(stream.name,target)
        for name,key in [('dataset-trees.json','dataset_tree_registry'),('datasets.metadata.json','dataset_metadata')]:
            target=policy_path.parent/name
            if target.exists():shutil.copy2(target,backup/name)
            atomic_json(target,json.loads((bundle/name).read_text()),0o644)
            policy[key]=str(target)
    atomic_json(policy_path,policy,stat.S_IMODE(policy_path.stat().st_mode))
    for unit, content in service_overrides(raw_root, derived_root, group).items():
        target = SYSTEMD_UNITS/(unit+'.d')/'researchkit-data.conf'
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, backup/(unit+'.researchkit-data.conf'))
        else:
            (backup/(unit+'.override-was-absent')).touch()
        with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as temporary:
            temporary.write(content); temporary.flush(); os.fsync(temporary.fileno()); os.fchmod(temporary.fileno(),0o644)
        os.replace(temporary.name,target)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    print('configured; restart broker and start runner; existing raw registry unchanged')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--derived-root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--raw-root',type=Path,default=DEFAULT_RAW_ROOT)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--policy',type=Path,default=POLICY)
    args=parser.parse_args(argv)
    report,*_=inspect(args.policy,args.derived_root,args.bundle,args.raw_root)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if args.check:return 0 if report['ready'] else 1
    try:configure(args.policy,args.derived_root,args.bundle,args.raw_root)
    except (RuntimeError, OSError,subprocess.CalledProcessError) as exc:parser.exit(1,str(exc)+'\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
