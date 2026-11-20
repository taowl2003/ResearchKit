"""Bounded admission of teacher-approved raw dataset directories.

No shell commands or user-supplied scripts. Parent directories are locked while
being adopted; untrusted files receive fresh root-owned inodes (reflink where
available, streaming otherwise). This avoids following links or changing the
permissions of an inode which an uploader could hard-link elsewhere, and stops
retained upload FDs from writing into the registered copy afterwards.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import struct
import time
import uuid

from .files import KitError
from . import web_datasets

ACL = 'system.posix_acl_access'
DEFAULT_ACL = 'system.posix_acl_default'
FICLONE = 0x40049409


def _require_root():
    if os.geteuid() != 0:
        raise KitError('权限准备必须由已安装的数据后台执行')


def _acl(fd):
    try:
        return os.getxattr(fd, ACL)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP):
            return None
        raise


def _remove_acl(fd, name):
    try:
        os.removexattr(fd, name)
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise


def _state(fd):
    return {'mode': stat.S_IMODE(os.fstat(fd).st_mode), 'acl': _acl(fd)}


def _restore(fd, saved):
    os.fchmod(fd, saved['mode'])
    if saved['acl'] is None:
        _remove_acl(fd, ACL)
    else:
        os.setxattr(fd, ACL, saved['acl'])


def snapshot_receiving_root(path):
    fd = web_datasets.DatasetRegistry._open_directory(Path(path), allow_receiving=True)
    try:
        return _state(fd)
    finally:
        os.close(fd)


def restore_receiving_root(path, saved):
    _require_root()
    fd = web_datasets.DatasetRegistry._open_directory(Path(path), allow_receiving=True)
    try:
        _restore(fd, saved)
    finally:
        os.close(fd)


def _grant_collection(fd, teacher_uid):
    """Preserve existing access, add the teacher, protect adopted child names."""
    info = os.fstat(fd)
    raw = _acl(fd)
    if raw is None:
        entries = [(1, (info.st_mode >> 6) & 7, 0xffffffff),
                   (4, (info.st_mode >> 3) & 7, 0xffffffff),
                   (32, info.st_mode & 7, 0xffffffff)]
    else:
        if len(raw) < 4 or (len(raw) - 4) % 8 or struct.unpack('<I', raw[:4])[0] != 2:
            raise KitError('接收目录 ACL 格式无效')
        entries = [struct.unpack('<HHI', raw[i:i+8]) for i in range(4, len(raw), 8)]
    users = {ident: perms for tag, perms, ident in entries if tag == 2}
    if info.st_uid:
        users[info.st_uid] = users.get(info.st_uid, 0) | ((info.st_mode >> 6) & 7)
    users[teacher_uid] = users.get(teacher_uid, 0) | 7
    # Respect an existing ACL mask for all previous non-owner permissions.
    mask = next((perms for tag, perms, _ in entries if tag == 16), 7)
    base = [(tag, (perms & mask) if tag in (4, 8) else perms, ident)
            for tag, perms, ident in entries if tag not in (2, 16)]
    named = [(2, (perms if ident in (teacher_uid, info.st_uid) else perms & mask), ident)
             for ident, perms in sorted(users.items())]
    acl_entries = sorted(base + named + [(16, 7, 0xffffffff)], key=lambda item: (item[0], item[2]))
    new_acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in acl_entries)
    if info.st_uid != 0:
        os.fchown(fd, 0, -1)
    os.fchmod(fd, (stat.S_IMODE(info.st_mode) & 0o777) | stat.S_ISVTX)
    os.setxattr(fd, ACL, new_acl)


def prepare_receiving_root(path, teacher_uid):
    _require_root()
    if type(teacher_uid) is not int or teacher_uid <= 0:
        raise KitError('教师 UID 无效')
    fd = web_datasets.DatasetRegistry._open_directory(Path(path), allow_receiving=True)
    try:
        if not web_datasets._root_owned(os.fstat(fd)):
            raise KitError('接收根必须由管理员所有')
        _grant_collection(fd, teacher_uid)
    finally:
        os.close(fd)


@contextmanager
def _locked_parent(fd):
    web_datasets._safe_parent_info(os.fstat(fd))
    saved = _state(fd)
    changed = bool(saved['mode'] & 0o022)
    if changed:
        os.fchmod(fd, saved['mode'] & ~0o022)
    try:
        yield
    finally:
        if changed:
            _restore(fd, saved)


def _open_child(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def prepare_dataset(config, relative_root, expected, *, teacher_uid):
    """Seal one reviewed, unregistered unit; never called for old versions."""
    _require_root()
    if type(teacher_uid) is not int or teacher_uid <= 0:
        raise KitError('教师 UID 无效')
    if config.get('discovery_mode') != 'readme' or not config.get('prepare_permissions'):
        raise KitError('自动权限准备尚未启用')
    relative = Path(relative_root)
    if (not relative.parts or relative.is_absolute() or '..' in relative.parts
            or relative.as_posix() != relative_root or '\\' in relative_root):
        raise KitError('权限准备目录无效')
    from .web_dataset_readme import open_relative
    from .web_dataset_trees import tree_metadata
    from .web_dataset_discovery import snapshot_fingerprint
    deadline = time.monotonic() + int(config['seconds_per_dataset'])
    def check():
        if time.monotonic() > deadline:
            raise KitError('权限准备超时，请刷新扫描后重试')
    probe = open_relative(Path(config['raw_root']), relative)
    try:
        before = tree_metadata(probe, readme_mode=True, trusted=False, seconds=config['seconds_per_dataset'])
    finally:
        os.close(probe)
    if snapshot_fingerprint(before) != expected:
        raise KitError('数据目录在审核后变化，请重新扫描审核')
    planned = {entry['path']: entry for entry in before['entries']}
    parent = web_datasets.DatasetRegistry._open_directory(Path(config['raw_root']), allow_receiving=True)
    count = 0
    try:
        for index, name in enumerate(relative.parts):
            check()
            with _locked_parent(parent):
                child = _open_child(parent, name)
                try:
                    info = os.fstat(child)
                    if index == len(relative.parts) - 1:
                        if [info.st_dev, info.st_ino, 0, 0, 0] != before['root_stat']:
                            raise KitError('数据集目录在权限准备时被替换')
                        os.fchown(child, 0, -1)
                        _remove_acl(child, ACL); _remove_acl(child, DEFAULT_ACL)
                        os.fchmod(child, 0o755)
                    else:
                        _grant_collection(child, teacher_uid)
                except Exception:
                    os.close(child)
                    raise
            os.close(parent); parent = child

        def seal(directory, prefix='', depth=0):
            nonlocal count
            check()
            if depth > 64:
                raise KitError('数据集目录层级过深')
            with os.scandir(directory) as stream:
                children = sorted(stream, key=lambda entry: entry.name)
            for item in children:
                check(); count += 1
                if count > 500000:
                    raise KitError('数据集条目超过上限')
                path = prefix + item.name
                info = os.stat(item.name, dir_fd=directory, follow_symlinks=False)
                marker = not prefix and item.name.casefold() == 'readme.md'
                entry = planned.get(path)
                if not marker and (entry is None or list(web_datasets._stable(info))[:2] != entry['stat'][:2]):
                    raise KitError('数据集条目在审核后被替换')
                if stat.S_ISDIR(info.st_mode):
                    if entry['kind'] != 'directory':
                        raise KitError('数据集条目类型变化')
                    child = _open_child(directory, item.name)
                    try:
                        if web_datasets._stable(os.fstat(child)) != web_datasets._stable(info):
                            raise KitError('数据集目录被替换')
                        os.fchown(child, 0, -1)
                        _remove_acl(child, ACL); _remove_acl(child, DEFAULT_ACL)
                        os.fchmod(child, 0o755)
                        seal(child, path + '/', depth + 1)
                    finally:
                        os.close(child)
                else:
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise KitError('权限准备只接受没有链接的普通文件')
                    if not marker and list(web_datasets._stable(info)) != entry['stat']:
                        raise KitError('数据文件在审核后变化')
                    _seal_file(directory, item.name, info, teacher_uid if marker else 0, check)
        seal(parent)
        final = tree_metadata(parent, readme_mode=True, seconds=config['seconds_per_dataset'])
        if {(e['path'],e['kind']) for e in final['entries']} != {(e['path'],e['kind']) for e in before['entries']}:
            raise KitError('权限准备期间数据集结构变化，请重新扫描')
        return {'prepared_files': count, 'snapshot': final}
    finally:
        os.close(parent)


def _seal_file(directory, name, info, owner, check):
    source = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    temporary = '.researchkit-admit-' + uuid.uuid4().hex
    output = None
    try:
        if web_datasets._stable(os.fstat(source)) != web_datasets._stable(info):
            raise KitError('待登记文件被替换')
        # Fresh inode: chown/chmod cannot affect a hard link outside the root;
        # a writer retaining the uploaded FD cannot mutate the sealed file.
        output = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            fcntl.ioctl(output, FICLONE, source)
        except OSError as exc:
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL):
                raise
            os.ftruncate(output, 0)
            os.lseek(output, 0, os.SEEK_SET)
            while True:
                check(); block = os.read(source, 1024 * 1024)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    view = view[os.write(output, view):]
        if web_datasets._stable(os.fstat(source)) != web_datasets._stable(info):
            raise KitError('文件复制期间发生变化，请重新扫描')
        os.fchown(output, owner, -1)
        _remove_acl(output, ACL)
        os.fchmod(output, 0o644)
        os.fsync(output)
        if web_datasets._stable(os.stat(name, dir_fd=directory, follow_symlinks=False)) != web_datasets._stable(info):
            raise KitError('文件在权限准备时被替换')
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if output is not None:
            os.close(output)
        os.close(source)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
