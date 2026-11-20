#!/usr/bin/env python3
"""Read-only preparation of a root-managed ResearchKit Web dataset registry.

The command only emits a complete candidate document to stdout.  An
administrator reviews and installs that document separately; this program does
not copy data, change a registry, or control any service.
"""
import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import sys
import time
from pathlib import Path


SCHEMA = 'researchkit.web-datasets/v1'
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_DATASETS = 128
MAX_FILES = 1000
MAX_REFERENCES = 128
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEADLINE_SECONDS = 30.0
_DATASET_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
_USERNAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


class PrepareError(RuntimeError):
    pass


def _root_owned(info):
    return info.st_uid == 0


def _stable(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _check_deadline(deadline):
    if time.monotonic() > deadline:
        raise PrepareError('数据集准备超过 30 秒上限')


def _safe_info(info, *, directory=False, regular=False, hardlink=False):
    if stat.S_ISLNK(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
        raise PrepareError('数据路径必须由 root 所有、无符号链接且不可组或全员写入')
    if directory and not stat.S_ISDIR(info.st_mode):
        raise PrepareError('数据路径中的目录无效')
    if regular and not stat.S_ISREG(info.st_mode):
        raise PrepareError('指定的数据文件不是普通文件')
    if hardlink and info.st_nlink != 1:
        raise PrepareError('指定的数据文件不能有硬链接')


def _absolute_path(value, label):
    try:
        path = Path(value)
    except TypeError as exc:
        raise PrepareError('%s 必须是绝对路径' % label) from exc
    if not path.is_absolute() or '..' in path.parts:
        raise PrepareError('%s 必须是无 .. 的绝对路径' % label)
    return path


def _safe_absolute_directory(path, label):
    path = _absolute_path(path, label)
    try:
        _safe_info(os.lstat('/'), directory=True)
    except OSError as exc:
        raise PrepareError('%s 根目录不可读取：%s' % (label, exc)) from exc
    current = Path('/')
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(str(current))
        except OSError as exc:
            raise PrepareError('%s 不可读取：%s' % (label, exc)) from exc
        _safe_info(info, directory=True)
    return path


def _relative_path(value):
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        raise PrepareError('--file 必须是非空 POSIX 相对路径')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise PrepareError('--file 必须是 UTF-8 路径') from exc
    parts = value.split('/')
    if (len(encoded) > 4096 or value.startswith('/') or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(not part or part in ('.', '..') for part in parts) or ':' in parts[0]
            or any(len(part.encode('utf-8')) > 255 for part in parts)):
        raise PrepareError('--file 路径不安全')
    return parts


def _open_root(path):
    _safe_absolute_directory(path, '--root')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise PrepareError('--root 不可打开：%s' % exc) from exc
    try:
        _safe_info(os.fstat(descriptor), directory=True)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_relative(root_fd, relative_parts):
    current_fd = os.dup(root_fd)
    try:
        for part in relative_parts[:-1]:
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0)
            if hasattr(os, 'O_NOFOLLOW'):
                flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise PrepareError('指定数据文件的目录不可安全打开：%s' % exc) from exc
            os.close(current_fd)
            current_fd = child_fd
            _safe_info(os.fstat(current_fd), directory=True)
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | os.O_NONBLOCK
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(relative_parts[-1], flags, dir_fd=current_fd)
        except OSError as exc:
            raise PrepareError('指定数据文件不可安全打开：%s' % exc) from exc
        try:
            _safe_info(os.fstat(descriptor), regular=True, hardlink=True)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(current_fd)


def _hash_file(root_fd, relative_parts, deadline):
    _check_deadline(deadline)
    descriptor = _open_relative(root_fd, relative_parts)
    try:
        before = os.fstat(descriptor)
        _safe_info(before, regular=True, hardlink=True)
        if before.st_size > MAX_FILE_BYTES:
            raise PrepareError('指定数据文件超过 512 MiB 上限')
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            _check_deadline(deadline)
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if remaining or _stable(before) != _stable(after):
            raise PrepareError('指定数据文件在哈希期间发生变化')
        return before.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _json_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PrepareError('数据注册表 JSON 含重复键')
        value[key] = item
    return value


def _read_registry(path):
    if path is None:
        return {'schema': SCHEMA, 'datasets': []}
    path = _absolute_path(path, '--registry')
    _safe_absolute_directory(path.parent, '--registry 父目录')
    try:
        before = os.lstat(str(path))
    except OSError as exc:
        raise PrepareError('数据注册表不可读取：%s' % exc) from exc
    _safe_info(before, regular=True, hardlink=True)
    if before.st_size > MAX_REGISTRY_BYTES:
        raise PrepareError('数据注册表超过 2 MiB 上限')
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise PrepareError('数据注册表不可安全打开：%s' % exc) from exc
    try:
        opened = os.fstat(descriptor)
        _safe_info(opened, regular=True, hardlink=True)
        if _stable(before) != _stable(opened):
            raise PrepareError('数据注册表读取时发生变化')
        chunks, remaining = [], MAX_REGISTRY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > MAX_REGISTRY_BYTES or _stable(opened) != _stable(after):
            raise PrepareError('数据注册表读取时发生变化或超过上限')
    finally:
        os.close(descriptor)
    try:
        return json.loads(payload.decode('utf-8'), object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PrepareError('数据注册表必须是有限 UTF-8 JSON') from exc


def _validate_reference(dataset_id, value):
    if type(value) is not dict or set(value) != {'path', 'size', 'sha256'}:
        raise PrepareError('数据注册表 files 必须是标准文件引用列表')
    parts = _relative_path(value['path'])
    size, checksum = value['size'], value['sha256']
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
        raise PrepareError('数据注册表文件大小无效或超过执行上限')
    if not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
        raise PrepareError('数据注册表文件摘要无效')
    return {'path': '/'.join(parts), 'size': size, 'sha256': checksum}


def _validate_registry(document):
    if type(document) is not dict or set(document) != {'schema', 'datasets'} or document.get('schema') != SCHEMA:
        raise PrepareError('数据注册表 schema 无效')
    datasets = document['datasets']
    if type(datasets) is not list or len(datasets) > MAX_DATASETS:
        raise PrepareError('数据注册表 datasets 无效或过多')
    seen_ids, total_files = set(), 0
    for item in datasets:
        if type(item) is not dict or set(item) != {'dataset_id', 'root', 'readers', 'files'}:
            raise PrepareError('数据注册表条目字段无效')
        dataset_id = item['dataset_id']
        if (not isinstance(dataset_id, str) or len(dataset_id) > 128 or dataset_id in {'.', '..'}
                or not _DATASET_ID.fullmatch(dataset_id) or dataset_id in seen_ids):
            raise PrepareError('数据注册表数据集 ID 无效或重复')
        seen_ids.add(dataset_id)
        if not isinstance(item['root'], str):
            raise PrepareError('数据集根目录无效')
        _absolute_path(item['root'], '数据集根目录')
        if type(item['readers']) is not list or type(item['files']) is not list:
            raise PrepareError('数据注册表 readers 或 files 无效')
        identities, paths = set(), set()
        for reader in item['readers']:
            if type(reader) is not dict or set(reader) != {'uid', 'username'}:
                raise PrepareError('数据注册表 reader 字段无效')
            uid, username = reader['uid'], reader['username']
            if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1 or not isinstance(username, str) or not _USERNAME.fullmatch(username):
                raise PrepareError('数据注册表 reader 身份无效')
            if (uid, username) in identities:
                raise PrepareError('数据注册表含重复 reader')
            identities.add((uid, username))
        for file_item in item['files']:
            reference = _validate_reference(dataset_id, file_item)
            if reference['path'] in paths:
                raise PrepareError('数据注册表含重复文件路径')
            paths.add(reference['path'])
        for path in paths:
            parts = path.split('/')
            if any('/'.join(parts[:index]) in paths for index in range(1, len(parts))):
                raise PrepareError('数据注册表文件与目录前缀冲突')
        total_files += len(paths)
        if total_files > MAX_FILES:
            raise PrepareError('数据注册表登记文件过多')
    return datasets


def _readers(names):
    if not names:
        raise PrepareError('至少需要一个 --reader')
    readers, identities = [], set()
    for name in names:
        try:
            account = pwd.getpwnam(name)
        except KeyError as exc:
            raise PrepareError('reader 账号不存在：%s' % name) from exc
        if (account.pw_name != name or not _USERNAME.fullmatch(name) or account.pw_uid < 1000
                or not account.pw_shell or Path(account.pw_shell).name.lower() in {'false', 'nologin'}):
            raise PrepareError('reader 必须是已有的非 root 登录用户（UID >= 1000）')
        identity = (account.pw_uid, account.pw_name)
        if identity in identities:
            raise PrepareError('reader 重复')
        identities.add(identity)
        readers.append({'uid': account.pw_uid, 'username': account.pw_name})
    return readers


def prepare(dataset_id, root, files, readers, registry=None):
    """Return, without writing, the complete registry candidate document."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    if (not isinstance(dataset_id, str) or len(dataset_id) > 128 or dataset_id in {'.', '..'}
            or not _DATASET_ID.fullmatch(dataset_id)):
        raise PrepareError('--dataset-id 无效')
    document = _read_registry(Path(registry) if registry else None)
    _check_deadline(deadline)
    existing = _validate_registry(document)
    if any(item['dataset_id'] == dataset_id for item in existing):
        raise PrepareError('数据集 ID 已存在；不会覆盖已有登记')
    if len(existing) >= MAX_DATASETS:
        raise PrepareError('数据注册表 datasets 已达上限')
    if not files:
        raise PrepareError('至少需要一个 --file')
    if len(files) > MAX_REFERENCES:
        raise PrepareError('本次数据引用超过 128 个上限')
    relative_files = ['/'.join(_relative_path(value)) for value in files]
    if len(set(relative_files)) != len(relative_files):
        raise PrepareError('--file 路径重复')
    total_existing = sum(len(item['files']) for item in existing)
    if total_existing + len(relative_files) > MAX_FILES:
        raise PrepareError('数据注册表登记文件过多')
    root_path = _absolute_path(root, '--root')
    new_readers = _readers(readers)
    prepared, total = [], 0
    root_fd = _open_root(root_path)
    try:
        for relative in relative_files:
            _check_deadline(deadline)
            size, checksum = _hash_file(root_fd, relative.split('/'), deadline)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise PrepareError('本次数据引用合计超过 2 GiB 上限')
            prepared.append({'path': relative, 'size': size, 'sha256': checksum})
    finally:
        os.close(root_fd)
    candidate = {'schema': SCHEMA, 'datasets': list(existing) + [{
        'dataset_id': dataset_id, 'root': str(root_path), 'readers': new_readers, 'files': prepared,
    }]}
    encoded = json.dumps(candidate, ensure_ascii=False, indent=2).encode('utf-8')
    _check_deadline(deadline)
    if len(encoded) > MAX_REGISTRY_BYTES:
        raise PrepareError('输出候选注册表超过 2 MiB 上限')
    return candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description='prepare a read-only ResearchKit Web dataset registry candidate')
    parser.add_argument('--dataset-id', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--file', action='append', default=[])
    parser.add_argument('--reader', action='append', default=[])
    parser.add_argument('--registry')
    args = parser.parse_args(argv)
    try:
        candidate = prepare(args.dataset_id, args.root, args.file, args.reader, args.registry)
    except (PrepareError, OSError) as exc:
        print('dataset preparation failed: %s' % exc, file=sys.stderr)
        return 1
    print(json.dumps(candidate, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
