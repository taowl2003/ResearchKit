"""Root-controlled external data registry for isolated fixed-submission runs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .files import KitError
from .web_identity import Principal
from .web_store import _DATASET_ID, _validate_data_references


_SCHEMA = 'researchkit.web-datasets/v1'
_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_MOUNTS = 128
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_DATASETS = 128
_MAX_REGISTERED_FILES = 1000
_USERNAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z')


class DatasetCancelled(KitError):
    """The broker cancellation probe stopped data preparation."""


class DatasetTimedOut(KitError):
    """Data preparation exceeded its bounded verification deadline."""


@dataclass(frozen=True, slots=True)
class DatasetMount:
    reference: dict[str, object]
    fd: int
    target: str


def _root_owned(info: os.stat_result) -> bool:
    """The only production ownership predicate; tests may patch this narrowly."""
    return info.st_uid == 0


def _safe_info(info: os.stat_result, *, directory: bool | None = None,
               regular: bool | None = None, hardlink: bool = False) -> None:
    if stat.S_ISLNK(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
        raise KitError('受控数据路径不受信任')
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise KitError('受控数据目录无效')
    if regular is True and not stat.S_ISREG(info.st_mode):
        raise KitError('受控数据文件无效')
    if hardlink and info.st_nlink != 1:
        raise KitError('受控数据文件不能有硬链接')


def _safe_parent_info(info):
    """Root-owned sticky receiving collections protect root-owned children.

    Dataset roots/files remain subject to the original read-only checks.
    Only intermediate directories may be writable, and only with the sticky
    bit that prevents their writers from renaming a registered root.
    """
    if (stat.S_ISDIR(info.st_mode) and _root_owned(info)
            and info.st_mode & stat.S_ISVTX):
        return
    _safe_info(info, directory=True)


def _identity(value: object) -> tuple[int, str, str]:
    if not isinstance(value, Principal):
        raise KitError('数据授权需要经 broker 验证的 Principal')
    uid, username, role = value.uid, value.username, value.role
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise KitError('可信身份缺少有效 UID')
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise KitError('可信身份缺少系统用户名')
    if role not in {'teacher', 'student'} or (role == 'teacher') != (username == 'ly'):
        raise KitError('可信身份角色无效')
    return uid, username, role


def _pairs_no_duplicates(pairs: list[tuple[int, str]], label: str) -> None:
    if len(set(pairs)) != len(pairs):
        raise KitError(f'数据注册表含重复{label}')


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise KitError('数据注册表 JSON 含重复键')
        value[key] = item
    return value


def _stable(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class DatasetRegistry:
    """Read a root-managed registry afresh for every authorization operation."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None

    def _read_bytes(self) -> bytes:
        if self.path is None:
            return b''
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise KitError('数据注册表路径不安全')
        current = Path('/')
        for part in self.path.parts[1:-1]:
            current /= part
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise KitError('数据注册表路径不可读取') from exc
            _safe_info(info, directory=True)
        try:
            before = os.lstat(self.path)
        except OSError as exc:
            raise KitError('数据注册表不可读取') from exc
        _safe_info(before, regular=True, hardlink=True)
        if before.st_size > _MAX_REGISTRY_BYTES:
            raise KitError('数据注册表超过大小上限')
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise KitError('数据注册表不可读取') from exc
        try:
            opened = os.fstat(descriptor)
            _safe_info(opened, regular=True, hardlink=True)
            if _stable(before)[:3] != _stable(opened)[:3]:
                raise KitError('数据注册表读取时变化')
            chunks: list[bytes] = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            after = os.fstat(descriptor)
            if len(data) > _MAX_REGISTRY_BYTES or _stable(opened) != _stable(after):
                raise KitError('数据注册表读取时变化或超过上限')
            return data
        finally:
            os.close(descriptor)

    def _load(self) -> dict[str, dict[str, object]]:
        if self.path is None:
            return {}
        try:
            document = json.loads(self._read_bytes().decode('utf-8'), object_pairs_hook=_json_no_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise KitError('数据注册表必须是有限 UTF-8 JSON') from exc
        if type(document) is not dict or set(document) != {'schema', 'datasets'} or document['schema'] != _SCHEMA:
            raise KitError('数据注册表 schema 无效')
        if type(document['datasets']) is not list or len(document['datasets']) > _MAX_DATASETS:
            raise KitError('数据注册表 datasets 无效或过多')
        datasets: dict[str, dict[str, object]] = {}
        total_files = 0
        for raw in document['datasets']:
            if type(raw) is not dict or set(raw) != {'dataset_id', 'root', 'readers', 'files'}:
                raise KitError('数据注册表条目字段无效')
            dataset_id, root, readers, files = raw['dataset_id'], raw['root'], raw['readers'], raw['files']
            if (not isinstance(dataset_id, str) or dataset_id in {'.', '..'} or
                    not _DATASET_ID.fullmatch(dataset_id)):
                raise KitError('数据集 ID 无效')
            # Reuse the submission reference validator for exact path/size/hash
            # rules, then additionally constrain IDs and execution limits.
            if type(files) is not list or any(type(item) is not dict or set(item) != {'path', 'size', 'sha256'}
                                              for item in files):
                raise KitError('数据集 files 必须是标准文件引用列表')
            normalized_files = _validate_data_references(
                [{'dataset_id': dataset_id, **item} for item in files])
            if any(item['size'] > MAX_FILE_BYTES for item in normalized_files):
                raise KitError('数据集单个文件超过执行上限')
            if not isinstance(root, str) or not root:
                raise KitError('数据集根目录无效')
            root_path = Path(root)
            if not root_path.is_absolute() or '..' in root_path.parts:
                raise KitError('数据集根目录必须是无 .. 的绝对路径')
            if type(readers) is not list:
                raise KitError('数据集 readers 必须是列表')
            identities: list[tuple[int, str]] = []
            for reader in readers:
                if type(reader) is not dict or set(reader) != {'uid', 'username'}:
                    raise KitError('数据集 reader 字段无效')
                uid, username = reader['uid'], reader['username']
                if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1 or not isinstance(username, str) or not _USERNAME.fullmatch(username):
                    raise KitError('数据集 reader 身份无效')
                identities.append((uid, username))
            _pairs_no_duplicates(identities, 'reader')
            paths = {str(item['path']) for item in normalized_files}
            for path in paths:
                parts = path.split('/')
                if any('/'.join(parts[:index]) in paths for index in range(1, len(parts))):
                    raise KitError('数据集文件与目录前缀冲突')
            if dataset_id in datasets:
                raise KitError('数据注册表含重复数据集 ID')
            total_files += len(normalized_files)
            if total_files > _MAX_REGISTERED_FILES:
                raise KitError('数据注册表登记文件过多')
            datasets[dataset_id] = {'root': root_path, 'readers': set(identities),
                                    'files': normalized_files}
        return datasets

    @staticmethod
    def _principals(value: object) -> list[tuple[int, str, str]]:
        if type(value) is not list or not value:
            raise KitError('数据授权主体必须是非空 Principal 列表')
        identities: list[tuple[int, str, str]] = []
        seen: set[tuple[int, str]] = set()
        for principal in value:
            identity = _identity(principal)
            key = identity[:2]
            # The broker passes [owner, requester]; self-requested jobs use
            # the same immutable principal twice and need one ACL check.
            if key not in seen:
                seen.add(key)
                identities.append(identity)
        return identities

    def catalog(self, principal: Principal) -> list[dict[str, object]]:
        uid, username, _role = _identity(principal)
        datasets = self._load()
        result = []
        for dataset_id, item in sorted(datasets.items()):
            if (uid, username) in item['readers']:
                result.append({'dataset_id': dataset_id, 'files': [dict(reference) for reference in item['files']]})
        return result

    def _authorize(self, principals: list[tuple[int, str, str]], references: object,
                   datasets: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        normalized = _validate_data_references(references)
        if len(normalized) > MAX_MOUNTS:
            raise KitError('数据挂载数超过执行上限')
        total = 0
        for reference in normalized:
            dataset_id = str(reference['dataset_id'])
            if dataset_id in {'.', '..'}:
                raise KitError('数据集 ID 无效')
            dataset = datasets.get(dataset_id)
            if dataset is None:
                raise KitError('受控数据不存在')
            readers = dataset['readers']
            if any((uid, username) not in readers for uid, username, _role in principals):
                raise KitError('无权使用受控数据')
            if reference not in dataset['files']:
                raise KitError('受控数据引用不匹配')
            total += int(reference['size'])
            if total > MAX_TOTAL_BYTES:
                raise KitError('受控数据总量超过执行上限')
        return normalized

    def authorize(self, principals: list[Principal], references: object) -> list[dict[str, object]]:
        identities = self._principals(principals)
        normalized = _validate_data_references(references)
        # A no-data submission must retain its historical behavior even if an
        # optional registry is absent, removed, or temporarily unreadable.
        if not normalized:
            return []
        return self._authorize(identities, normalized, self._load())

    @staticmethod
    def _cancelled(callback: Callable[[], bool], deadline: float, next_check: list[float]) -> None:
        now = time.monotonic()
        if now > deadline:
            raise DatasetTimedOut('受控数据准备超时')
        if now >= next_check[0]:
            try:
                stopped = callback()
            except Exception as exc:
                raise KitError('受控数据准备取消检查失败') from exc
            if stopped:
                raise DatasetCancelled('受控数据准备已取消')
            next_check[0] = now + 1

    @staticmethod
    def _open_directory(root: Path, *, allow_receiving=False) -> int:
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_CLOEXEC', 0))
        try:
            _safe_info(os.fstat(descriptor), directory=True)
            for index, part in enumerate(root.parts[1:]):
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if index < len(root.parts) - 2 or allow_receiving:
                    _safe_parent_info(info)
                else:
                    _safe_info(info, directory=True)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0) |
                                getattr(os, 'O_CLOEXEC', 0), dir_fd=descriptor)
                try:
                    if _stable(info)[:3] != _stable(os.fstat(child))[:3]:
                        raise KitError('受控数据目录读取时变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_file(root_fd: int, path: str) -> tuple[int, tuple[int, int, int, int, int]]:
        descriptor = os.dup(root_fd)
        try:
            parts = path.split('/')
            for part in parts[:-1]:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                _safe_info(info, directory=True)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0) |
                                getattr(os, 'O_CLOEXEC', 0), dir_fd=descriptor)
                try:
                    if _stable(info)[:3] != _stable(os.fstat(child))[:3]:
                        raise KitError('受控数据目录读取时变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            _safe_info(info, regular=True, hardlink=True)
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0) |
                              getattr(os, 'O_CLOEXEC', 0), dir_fd=descriptor)
            opened = os.fstat(file_fd)
            if _stable(info) != _stable(opened):
                os.close(file_fd)
                raise KitError('受控数据文件读取时变化')
            return file_fd, _stable(opened)
        finally:
            os.close(descriptor)

    @contextmanager
    def open_references(self, principals: list[Principal], references: object, *,
                        cancelled: Callable[[], bool] = lambda: False) -> Iterator[list[DatasetMount]]:
        if not callable(cancelled):
            raise KitError('受控数据取消检查必须可调用')
        identities = self._principals(principals)
        normalized = _validate_data_references(references)
        if not normalized:
            yield []
            return
        datasets = self._load()
        normalized = self._authorize(identities, normalized, datasets)
        deadline, next_check = time.monotonic() + 30, [0.0]
        mounts: list[DatasetMount] = []
        stable_fds: list[tuple[int, tuple[int, int, int, int, int]]] = []
        try:
            for reference in normalized:
                self._cancelled(cancelled, deadline, next_check)
                dataset = datasets[str(reference['dataset_id'])]
                root_fd = self._open_directory(dataset['root'])
                try:
                    file_fd, before = self._open_file(root_fd, str(reference['path']))
                finally:
                    os.close(root_fd)
                try:
                    digest = hashlib.sha256()
                    remaining = int(reference['size'])
                    while remaining:
                        self._cancelled(cancelled, deadline, next_check)
                        chunk = os.read(file_fd, min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if remaining or os.read(file_fd, 1):
                        raise KitError('受控数据文件大小变化')
                    if digest.hexdigest() != reference['sha256'] or _stable(os.fstat(file_fd)) != before:
                        raise KitError('受控数据文件内容变化')
                    os.lseek(file_fd, 0, os.SEEK_SET)
                    target = f"/datasets/{reference['dataset_id']}/{reference['path']}"
                    mounts.append(DatasetMount(dict(reference), file_fd, target))
                    stable_fds.append((file_fd, before))
                except Exception:
                    os.close(file_fd)
                    raise
            self._cancelled(cancelled, deadline, next_check)
            # Hashing large inputs may take seconds. Honor a registry grant
            # revoked during preparation before starting any submitted code.
            self.authorize(principals, normalized)
            yield mounts
        except OSError as exc:
            raise KitError('受控数据文件不可读取') from exc
        finally:
            changed = False
            for descriptor, before in stable_fds:
                try:
                    changed = changed or _stable(os.fstat(descriptor)) != before
                except OSError:
                    changed = True
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if changed:
                raise KitError('受控数据文件在运行期间变化')
