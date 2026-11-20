#!/usr/bin/env python3
"""Enable README-based dataset discovery on an existing installation once."""
import argparse
import base64
import errno
import importlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP_VENV = Path('/opt/researchkit-web/venv')
POLICY = Path('/etc/researchkit-web/policy.json')
UNIT_ROOT = Path('/etc/systemd/system')
UNIT = 'researchkit-data-manager.service'
FILES = ('web_dataset_permissions.py', 'web_dataset_readme.py', 'web_dataset_discovery.py',
         'web_dataset_trees.py', 'web_data_publisher.py', 'web_datasets.py', 'web_data_management.py', 'web_dataset_access.py', 'web_lifecycle.py', 'web_cleanup.py', 'web_cleanup_raw.py')

# The administrator may launch this stdlib-only script with system Python 3.9.
# Import application code only in its installed, isolated Python environment.
_RUNTIME_BOOTSTRAP = '''import runpy, sys
from pathlib import Path
if sys.version_info < (3, 11) or Path(sys.prefix).resolve() != Path(sys.argv[1]):
    sys.exit('已安装的平台 Python 环境无效：需要平台虚拟环境和 Python 3.11 或更新版本')
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


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
    info = _trusted(path)
    if info.st_size > 2 * 1024 * 1024:
        raise RuntimeError('配置超过大小上限')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError('配置必须是 JSON 对象')
    return value


def _run(arguments):
    result = subprocess.run([str(x) for x in arguments], text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or '命令失败')
    return result.stdout.strip()


def _installed_runtime(argv):
    if os.geteuid() != 0:
        raise RuntimeError('请由已有权限的管理员启用，ly 不需要 sudo')
    interpreter = APP_VENV / 'bin/python'
    _trusted(interpreter)
    _trusted(APP_VENV / 'pyvenv.cfg')
    if (sys.version_info >= (3, 11) and sys.flags.isolated
            and Path(sys.prefix).resolve() == APP_VENV):
        return None
    # The bootstrap validates the runtime before loading this script again, so
    # a damaged/wrong interpreter cannot cause an endless restart loop.
    return subprocess.run([str(interpreter), '-I', '-B', '-c', _RUNTIME_BOOTSTRAP,
                           str(APP_VENV), str(Path(__file__).resolve()), *argv]).returncode


def _path(path):
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '\n\r\x00:"\\%'):
        raise RuntimeError('服务挂载路径格式不受支持')
    return path


def _raw_root(path):
    _path(path)
    if path == Path('/'):
        raise RuntimeError('数据根目录不能是文件系统根目录')
    # A writable ancestor could replace the selected tree during privileged work.
    for component in reversed(tuple(path.parents)):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
            raise RuntimeError('数据根目录父路径不受 root 保护: ' + str(component))
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise RuntimeError('数据根目录不能是链接')
    if not _root_owned(path.lstat()):
        raise RuntimeError('数据根目录必须由 root 所有')
    if path.lstat().st_mode & 0o022 and not path.lstat().st_mode & stat.S_ISVTX:
        raise RuntimeError('已有可写数据根缺少 sticky 保护，请管理员先核实权限')
    try:
        os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise RuntimeError('接收目录需要支持 Linux POSIX ACL') from error


def _permissions_module(path):
    package = path.parent
    _trusted(package, directory=True)
    # Relative imports must resolve only from the trusted installed package.
    for member in package.glob('*.py'):
        _trusted(member)
    _trusted(package / '__init__.py')
    if any(name == 'researchkit' or name.startswith('researchkit.') for name in sys.modules):
        raise RuntimeError('进程已加载 researchkit，拒绝混用模块')
    sys.path.insert(0, str(package.parent))
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module('researchkit.web_dataset_permissions')
    finally:
        sys.dont_write_bytecode = prior


def dropin(raw_root):
    _path(raw_root)
    return ('[Service]\nBindReadOnlyPaths=\nBindPaths="' + str(raw_root) + '"\n'
            'ReadWritePaths="' + str(raw_root) + '"\nCapabilityBoundingSet=\n'
            'CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER\n')


def inspect(policy_path):
    try:
        if os.geteuid() != 0:
            raise RuntimeError('请由已有权限的管理员启用，ly 不需要 sudo')
        policy = _json(policy_path)
        if policy.get('schema') != 'researchkit.web-policy/v1' or not policy.get('runner'):
            raise RuntimeError('需要已安装的 Web 和独立 runner')
        config_path = Path(policy['dataset_management_config'])
        if config_path.parent != policy_path.parent:
            raise RuntimeError('数据管理配置必须位于既有配置目录')
        config = _json(config_path)
        if config.get('schema') != 'researchkit.web-data-management/v1':
            raise RuntimeError('数据管理配置格式无效')
        allowed = {'schema','raw_root','wiki_root','chinese_drafts','scan_interval_seconds',
                   'seconds_per_dataset','discovery_mode','prepare_permissions'}
        if set(config) - allowed:
            raise RuntimeError('数据管理配置含当前代码不支持的字段')
        budget = config.get('seconds_per_dataset', 3600)
        if type(budget) is not int or not 1 <= budget <= 86400:
            raise RuntimeError('数据处理时间限制无效')
        raw_root = Path(config['raw_root'])
        _raw_root(raw_root)
        if policy.get('dataset_tree_registry'):
            registry = _json(Path(policy['dataset_tree_registry']))
            for record in registry.get('datasets', []):
                manifest = Path(record['manifest'])
                info = _trusted(manifest)
                if info.st_size > 128 * 1024 * 1024:
                    raise RuntimeError('已有数据清单过大')
                prior_root = Path(json.loads(manifest.read_text()).get('root', '/'))
                if prior_root == raw_root or prior_root in raw_root.parents:
                    raise RuntimeError('共享接收根处于已有固定数据版本内，不能改变其权限')
        _trusted(UNIT_ROOT, directory=True)
        _trusted(UNIT_ROOT / UNIT)
        directory = UNIT_ROOT / (UNIT + '.d')
        if directory.exists() or directory.is_symlink():
            _trusted(directory, directory=True)
        target = directory / 'researchkit-readme.conf'
        if target.exists() or target.is_symlink():
            _trusted(target)
        for unit in ('researchkit-runner.service', UNIT):
            if _run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value']) != 'inactive':
                raise RuntimeError('请先停止 ' + unit + ' 并确认 inactive')
        code_root = Path(policy['code_root']) / 'core/researchkit'
        for name in FILES:
            installed = code_root / name
            _trusted(installed)
            if installed.read_bytes() != (ROOT / 'core/researchkit' / name).read_bytes():
                raise RuntimeError('请先用本包 update-web.py 更新代码: ' + name)
        teacher = pwd.getpwnam('ly')
        if teacher.pw_uid == 0:
            raise RuntimeError('教师账号不能是 root')
        dropin(raw_root)
        try:
            permissions = _permissions_module(code_root / 'web_dataset_permissions.py')
        except Exception as error:
            raise RuntimeError('平台权限模块无法加载（尚未修改配置或权限）: ' + str(error)) from error
        return {'ready': True, 'errors': [], 'raw_root': str(raw_root), 'python': sys.executable}, (config_path, config, raw_root, target, teacher.pw_uid, permissions)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        return {'ready': False, 'errors': [str(error)]}, None


def _atomic(path, raw, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def enable(policy_path):
    report, state = inspect(policy_path)
    if not report['ready']:
        raise RuntimeError('; '.join(report['errors']))
    config_path, config, raw_root, target, teacher_uid, permissions = state
    original_root = permissions.snapshot_receiving_root(raw_root)
    backup = Path(tempfile.mkdtemp(prefix='researchkit-readme-backup.', dir='/root'))
    shutil.copy2(config_path, backup / 'data-management.json')
    had_dropin = target.exists()
    if had_dropin:
        shutil.copy2(target, backup / 'researchkit-readme.conf')
    (backup / 'receiving-root.json').write_bytes(_encode({
        'path': str(raw_root), 'mode': original_root['mode'],
        'acl_base64': (base64.b64encode(original_root['acl']).decode() if original_root['acl'] is not None else None)}))
    print('backup:', backup, flush=True)
    made_dir = False
    try:
        if not target.parent.exists():
            target.parent.mkdir(mode=0o755)
            made_dir = True
        _trusted(target.parent, directory=True)
        config.update({'discovery_mode': 'readme', 'prepare_permissions': True})
        interval = config.get('scan_interval_seconds', 3600)
        if type(interval) is not int or not 60 <= interval <= 86400:
            raise RuntimeError('扫描间隔无效')
        config['scan_interval_seconds'] = min(interval, 60)
        _atomic(config_path, _encode(config), stat.S_IMODE((backup / 'data-management.json').stat().st_mode))
        _atomic(target, dropin(raw_root).encode())
        permissions.prepare_receiving_root(raw_root, teacher_uid)
        _run(['systemctl', 'daemon-reload'])
    except BaseException as error:
        failures = []
        for action in (lambda: _atomic(config_path, (backup / 'data-management.json').read_bytes(),
                                        stat.S_IMODE((backup / 'data-management.json').stat().st_mode)),
                       lambda: (_atomic(target, (backup / 'researchkit-readme.conf').read_bytes(),
                                         stat.S_IMODE((backup / 'researchkit-readme.conf').stat().st_mode))
                                if had_dropin else target.unlink(missing_ok=True)),
                       lambda: permissions.restore_receiving_root(raw_root, original_root),
                       lambda: _run(['systemctl', 'daemon-reload'])):
            try:
                action()
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        if made_dir:
            try:
                target.parent.rmdir()
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        suffix = ('；回滚失败: ' + '; '.join(failures)) if failures else '；已回滚配置和权限'
        raise RuntimeError(str(error) + suffix + '；备份: ' + str(backup)) from error
    return {'enabled': True, 'backup': str(backup), 'raw_root': str(raw_root),
            'next': '由管理员重启 broker 读取新配置，再启动 data-manager 和 runner；个人 Web 重新连接'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        status = _installed_runtime(sys.argv[1:] if argv is None else argv)
    except (OSError, ValueError, RuntimeError) as error:
        if args.check:
            print(json.dumps({'ready': False, 'errors': [str(error)]}, ensure_ascii=False, indent=2))
            return 1
        parser.exit(1, str(error) + '\n')
    if status is not None:
        return status
    if args.check:
        report, _ = inspect(args.policy)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['ready'] else 1
    try:
        report = enable(args.policy)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
