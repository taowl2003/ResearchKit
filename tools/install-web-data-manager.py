#!/usr/bin/env python3
"""Install the opt-in shared dataset publisher once; preserve all existing data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
POLICY = Path('/etc/researchkit-web/policy.json')
UNIT_ROOT = Path('/etc/systemd/system')
UNIT = 'researchkit-data-manager.service'
PYTHON = Path('/opt/researchkit-web/venv/bin/python')


def _root_owned(info):
    return info.st_uid == 0


def _trusted(path, directory=False):
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('路径必须是无 .. 的绝对路径')
    for component in reversed(path.parents):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
            raise RuntimeError('配置父目录不受 root 保护: ' + str(component))
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid or not _root_owned(info) or info.st_mode & 0o022:
        raise RuntimeError('配置路径必须为 root 所有且不可由普通用户写入: ' + str(path))
    return info


def _json(path):
    _trusted(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError('配置超过大小上限')
    return json.loads(path.read_text())


def _run(arguments):
    result = subprocess.run([str(value) for value in arguments], text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or '命令失败')
    return result.stdout.strip()


def _path(path):
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '\n\r\x00:"\\%'):
        raise RuntimeError('服务挂载路径格式不受支持')
    return path


def service_unit(policy_path, raw_root, wiki_root, python=PYTHON):
    for path in (policy_path, raw_root, wiki_root, python):
        _path(path)
    return ('[Unit]\nDescription=ResearchKit reviewed dataset discovery and publisher\n'
            'After=researchkit-broker.service\nWants=researchkit-broker.service\n\n'
            '[Service]\nType=simple\nUser=root\nGroup=root\nUMask=0077\n'
            'RuntimeDirectory=researchkit-data-manager\nRuntimeDirectoryMode=0700\n'
            'ExecStart="' + str(python) + '" -I -m researchkit.web_data_publisher --policy "' + str(policy_path) + '"\n'
            'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=15\nKillMode=control-group\n'
            'MemoryMax=1536M\nMemorySwapMax=0\nTasksMax=16\nCPUQuota=50%\n'
            'NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=false\n'
            'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0\n'
            'InaccessiblePaths=/root /run/user /var/lib/researchkit-web /var/lib/researchkit-runner\n'
            'BindReadOnlyPaths="' + str(raw_root) + '" "' + str(wiki_root) + '"\n'
            'ReadWritePaths="' + str(policy_path.parent) + '" /run/researchkit-data-manager\n'
            'CapabilityBoundingSet=CAP_DAC_READ_SEARCH\nRestrictSUIDSGID=yes\n'
            'RestrictAddressFamilies=AF_UNIX\nProtectKernelTunables=yes\nProtectKernelModules=yes\n'
            'ProtectControlGroups=yes\n\n[Install]\nWantedBy=multi-user.target\n')


def raw_mount_overrides(raw_root):
    """Add only a missing read-only route; preserve all existing data mounts."""
    _path(raw_root)
    result = {}
    for unit in ('researchkit-broker.service', 'researchkit-runner.service'):
        directory = UNIT_ROOT / (unit + '.d')
        if directory.exists():
            _trusted(directory, directory=True)
        mounted = False
        for name in ('researchkit-data.conf', 'researchkit-data-management.conf'):
            path = directory / name
            if path.exists():
                _trusted(path)
                for line in path.read_text().splitlines():
                    if line.startswith('BindReadOnlyPaths=') and str(raw_root) in shlex.split(line.split('=', 1)[1]):
                        mounted = True
        if not mounted:
            result[directory / 'researchkit-data-management.conf'] = '[Service]\nBindReadOnlyPaths="' + str(raw_root) + '"\n'
    return result


def inspect(policy_path, raw_root, wiki_root):
    errors = []
    try:
        policy = _json(policy_path)
        if policy.get('schema') != 'researchkit.web-policy/v1' or not policy.get('runner'):
            raise RuntimeError('需要已部署的统一数据工作流和独立 runner')
        _trusted(raw_root, directory=True)
        _path(wiki_root)
        # Wiki is maintained by ly. It is metadata input, not executable code;
        # source files need not be root-owned, but must not traverse links.
        for path in [wiki_root] + list(wiki_root.parents):
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise RuntimeError('Wiki 路径不能包含链接')
        for relative in ('sources/dataset-boundaries.yaml', 'catalog/datasets.yaml', 'catalog/dataset-subsets.yaml',
                         'reports/ingestion-wave-5-findings.json'):
            path = wiki_root / relative
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise RuntimeError('缺少有界 Wiki 说明文件: ' + str(path))
        if _run(['systemctl', 'show', 'researchkit-runner.service', '-p', 'ActiveState', '--value']) != 'inactive':
            raise RuntimeError('请先等待运行完成、停止 runner 并确认 inactive')
        _trusted(policy_path.parent, directory=True)
        _trusted(UNIT_ROOT, directory=True)
        code_root = Path(policy['code_root'])
        for name in ('web_data_management.py', 'web_dataset_discovery.py', 'web_data_publisher.py', 'web_broker.py', 'web_app.py', 'web_identity.py'):
            installed = code_root / 'core/researchkit' / name
            _trusted(installed)
            if installed.read_bytes() != (ROOT / 'core/researchkit' / name).read_bytes():
                raise RuntimeError('请先用本包 update-web.py 更新代码: ' + name)
        config_path = Path(policy.get('dataset_management_config', str(policy_path.parent / 'data-management.json')))
        if config_path.parent != policy_path.parent:
            raise RuntimeError('数据管理配置必须位于既有配置目录')
        if config_path.exists():
            config = _json(config_path)
            if config.get('raw_root') != str(raw_root) or config.get('wiki_root') != str(wiki_root):
                raise RuntimeError('已有数据管理根目录不同，不自动迁移')
        for field, name in (('dataset_tree_registry', 'dataset-trees.json'), ('dataset_metadata', 'datasets.metadata.json')):
            path = Path(policy.get(field, str(policy_path.parent / name)))
            if path.parent != policy_path.parent:
                raise RuntimeError('登记清单必须位于既有配置目录')
            if path.exists():
                _json(path)
        drafts = ROOT / 'deploy/dataset-chinese-drafts.json'
        if drafts.is_symlink() or not drafts.is_file() or drafts.stat().st_size > 2 * 1024**2:
            raise RuntimeError('缺少中文草稿文件')
        value = json.loads(drafts.read_text())
        if value.get('schema') != 'researchkit.web-dataset-chinese-drafts/v1':
            raise RuntimeError('中文草稿格式无效')
        service_unit(policy_path, raw_root, wiki_root)
        raw_mount_overrides(raw_root)
        return {'ready': True, 'errors': [], 'raw_root': str(raw_root), 'wiki_root': str(wiki_root)}, policy
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        errors.append(str(error))
        return {'ready': False, 'errors': errors}, None


def _atomic(path, raw, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def install(policy_path, raw_root, wiki_root):
    if os.geteuid() != 0:
        raise RuntimeError('请由已有权限的管理员安装，ly 不需要 sudo')
    report, policy = inspect(policy_path, raw_root, wiki_root)
    if not report['ready']:
        raise RuntimeError('; '.join(report['errors']))
    backup = Path(tempfile.mkdtemp(prefix='researchkit-data-manager-backup.', dir='/root'))
    targets = {}
    created = []
    config_path = Path(policy.get('dataset_management_config', str(policy_path.parent / 'data-management.json')))
    registry_path = Path(policy.get('dataset_tree_registry', str(policy_path.parent / 'dataset-trees.json')))
    metadata_path = Path(policy.get('dataset_metadata', str(policy_path.parent / 'datasets.metadata.json')))
    draft_path = policy_path.parent / 'data-description-drafts.json'
    unit_path = UNIT_ROOT / UNIT
    bindings = raw_mount_overrides(raw_root)
    for path in bindings:
        path.parent.mkdir(mode=0o755, exist_ok=True)
        _trusted(path.parent, directory=True)
    paths = (policy_path, config_path, registry_path, metadata_path, draft_path, unit_path) + tuple(bindings)
    for index, path in enumerate(paths):
        if path.exists():
            _trusted(path)
            saved = backup / ('%d-' % index + path.name)
            shutil.copy2(path, saved)
            targets[path] = saved
        else:
            created.append(path)
    previous_active = unit_path.exists() and _run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value']) == 'active'
    print('backup:', backup, flush=True)
    _run(['systemctl', 'stop', UNIT]) if unit_path.exists() else None
    _run(['systemctl', 'stop', 'researchkit-broker.service'])
    try:
        if not registry_path.exists():
            _atomic(registry_path, _encode({'schema': 'researchkit.web-dataset-trees/v1', 'datasets': []}))
        if not metadata_path.exists():
            _atomic(metadata_path, _encode({'schema': 'researchkit.web-dataset-metadata/v1', 'datasets': {}}))
        _atomic(draft_path, (ROOT / 'deploy/dataset-chinese-drafts.json').read_bytes())
        if not config_path.exists():
            _atomic(config_path, _encode({'schema': 'researchkit.web-data-management/v1', 'raw_root': str(raw_root),
                                         'wiki_root': str(wiki_root), 'chinese_drafts': str(draft_path),
                                         'scan_interval_seconds': 3600, 'seconds_per_dataset': 3600}))
        policy.update({'dataset_management_config': str(config_path), 'dataset_tree_registry': str(registry_path),
                       'dataset_metadata': str(metadata_path)})
        _atomic(policy_path, _encode(policy))
        _atomic(unit_path, service_unit(policy_path, raw_root, wiki_root).encode())
        for path, text in bindings.items():
            _atomic(path, text.encode())
        _run(['systemctl', 'daemon-reload'])
        # Validates the configured root files and raw tree route, without
        # hashing raw files or changing permissions/content.
        _run([PYTHON, '-I', '-m', 'researchkit.web_data_publisher', '--policy', policy_path, '--check'])
        _run(['systemctl', 'start', 'researchkit-broker.service'])
        _run(['systemctl', 'enable', '--now', UNIT])
        if _run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value']) != 'active':
            raise RuntimeError('数据发布服务未启动')
    except BaseException:
        # Stop before undoing config; failed state/data are retained if stopping
        # cannot be established. No database or original dataset is deleted.
        _run(['systemctl', 'stop', UNIT]) if unit_path.exists() else None
        _run(['systemctl', 'stop', 'researchkit-broker.service'])
        if unit_path in created and unit_path.exists():
            _run(['systemctl', 'disable', UNIT])
        for path, saved in targets.items():
            _atomic(path, saved.read_bytes(), stat.S_IMODE(saved.stat().st_mode))
        for path in created:
            if path.exists():
                path.unlink()
        _run(['systemctl', 'daemon-reload'])
        _run(['systemctl', 'start', 'researchkit-broker.service'])
        if previous_active:
            _run(['systemctl', 'start', UNIT])
        raise
    return {'installed': True, 'backup': str(backup), 'service': UNIT,
            'next': 'start runner, reconnect personal Web, open 数据集管理'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--raw-root', type=Path, default=Path('/home/share/Datasets'))
    parser.add_argument('--wiki-root', type=Path, default=Path('/home/ly/hydro-wiki'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if args.check:
        report, _ = inspect(args.policy, args.raw_root, args.wiki_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['ready'] else 1
    try:
        report = install(args.policy, args.raw_root, args.wiki_root)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
