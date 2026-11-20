#!/usr/bin/env python3
"""Check or atomically install a reviewed description-only update (administrator).

Requires hashes of BOTH files. Does not edit data, versions, ACLs or policy.
Run with the installed ResearchKit Python environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from researchkit.files import KitError
from researchkit.web_datasets import DatasetRegistry, _json_no_duplicates
from researchkit.web_dataset_metadata import enrich_catalog


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect(policy_path, reviewed, expected_current, expected_reviewed):
    for value in (expected_current, expected_reviewed):
        if not re.fullmatch(r'[0-9a-f]{64}', value):
            raise KitError('必须提供当前说明和已审核说明的 SHA-256')
    policy_raw = DatasetRegistry(policy_path)._read_bytes()
    policy = json.loads(policy_raw, object_pairs_hook=_json_no_duplicates)
    target = policy.get('dataset_metadata')
    if not isinstance(target, str) or not Path(target).is_absolute() or '..' in Path(target).parts:
        raise KitError('policy 尚未启用数据集说明')
    target = Path(target)
    if target == reviewed:
        raise KitError('审核文件必须与正式文件分开')
    old_raw = DatasetRegistry(target)._read_bytes()
    new_raw = DatasetRegistry(reviewed)._read_bytes()
    if digest(old_raw) != expected_current or digest(new_raw) != expected_reviewed:
        raise KitError('说明文件已变化，请重新核对摘要和审核')
    enrich_catalog([], target)
    enrich_catalog([], reviewed)
    old = json.loads(old_raw, object_pairs_hook=_json_no_duplicates)
    new = json.loads(new_raw, object_pairs_hook=_json_no_duplicates)
    if set(old['datasets']) != set(new['datasets']):
        raise KitError('只能更新既有说明，不能新增或删除登记 ID')
    changes = {identity: {'before': old['datasets'][identity], 'after': new['datasets'][identity]}
               for identity in sorted(old['datasets']) if old['datasets'][identity] != new['datasets'][identity]}
    report = {'ready': True, 'installed': False, 'target': str(target),
              'current_sha256': expected_current, 'reviewed_sha256': expected_reviewed, 'changes': changes}
    return report, target, old_raw, new_raw, policy_raw


def apply(policy_path, reviewed, expected_current, expected_reviewed):
    if os.geteuid() != 0:
        raise KitError('请由已有权限的管理员执行，不要给 ly 提权')
    report, target, old_raw, new_raw, policy_raw = inspect(
        policy_path, reviewed, expected_current, expected_reviewed)
    if not report['changes']:
        return report
    # The trusted target parent is root-owned and not writable by users. Make a
    # private, exact backup before replacing the single public metadata file.
    backup_fd, backup_name = tempfile.mkstemp(prefix=target.name + '.backup.', dir=target.parent)
    with os.fdopen(backup_fd, 'wb') as stream:
        stream.write(old_raw); stream.flush(); os.fsync(stream.fileno())
    descriptor, pending_name = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(new_raw); stream.flush(); os.fsync(stream.fileno())
        # Refuse a concurrent administrator's change. No services are stopped
        # for descriptions: readers see either the complete old or new file.
        if (DatasetRegistry(target)._read_bytes() != old_raw or
                DatasetRegistry(policy_path)._read_bytes() != policy_raw):
            raise KitError('正式配置在安装期间变化，保留备份并停止')
        os.chmod(pending_name, 0o644)
        os.replace(pending_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(pending_name):
            os.unlink(pending_name)
    return {**report, 'installed': True, 'backup': backup_name}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=Path('/etc/researchkit-web/policy.json'))
    parser.add_argument('--reviewed', type=Path, required=True)
    parser.add_argument('--expected-current-sha256', required=True)
    parser.add_argument('--expected-reviewed-sha256', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    arguments = (args.policy, args.reviewed, args.expected_current_sha256, args.expected_reviewed_sha256)
    try:
        result = inspect(*arguments)[0] if args.check else apply(*arguments)
    except (OSError, ValueError, KeyError, TypeError, KitError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
