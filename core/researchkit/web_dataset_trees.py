"""Whole, root-published dataset trees pinned by a reviewed snapshot manifest.

Files are hashed during publication and for the datasets selected by a run.
Content and metadata are verified before and after execution; one read-only
directory FD is bound. User-provided paths and writable trees are rejected.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .files import KitError
from .web_datasets import DatasetMount, DatasetRegistry, _safe_info, _stable, _identity
from .web_store import _validate_data_references, _DATASET_ID, _relative_path

TREE_PATH = '__researchkit_dataset_tree__'
TREE_SCHEMA = 'researchkit.web-dataset-tree/v1'
TREE_SCHEMA_README = 'researchkit.web-dataset-tree/v2'
REGISTRY_SCHEMA = 'researchkit.web-dataset-trees/v1'
MAX_ENTRIES = 500000
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def is_tree_reference(reference):
    return type(reference) is dict and reference.get('path') == TREE_PATH


def tree_metadata(root_fd, *, hash_files=False, cancelled=lambda: False, seconds=300, readme_mode=False, trusted=True):
    """Observe only the explicitly selected root through no-follow descriptors."""
    entries = []
    deadline, next_check = time.monotonic() + seconds, [0.0]
    def check():
        DatasetRegistry._cancelled(cancelled, deadline, next_check)
    def safe(info, *, directory=False, regular=False, hardlink=False):
        if trusted:
            _safe_info(info, directory=directory, regular=regular, hardlink=hardlink)
        elif ((directory and not stat.S_ISDIR(info.st_mode)) or
              (regular and not stat.S_ISREG(info.st_mode)) or
              (hardlink and info.st_nlink != 1)):
            raise KitError('数据集路径类型或硬链接无效')
    def walk(fd, prefix, depth):
        if depth > 64:
            raise KitError('数据集目录层级过深')
        before = os.fstat(fd); safe(before, directory=True)
        with os.scandir(fd) as stream:
            children = sorted(stream, key=lambda entry: entry.name)
        for child in children:
            check()
            if readme_mode and not prefix and child.name.casefold() == 'readme.md':
                # README is mutable explanatory metadata, outside the pinned
                # data; its ownership may be the teacher's. Still reject links
                # and non-regular files before exposing the dataset tree.
                info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise KitError('README.md 必须是没有硬链接的普通文件')
                continue
            path = '/'.join(filter(None, (prefix, child.name)))
            if readme_mode and child.name.startswith('.researchkit-admit-'):
                raise KitError('发现上次权限准备中断的临时文件，请保留目录并联系管理员恢复')
            _relative_path(path, label='数据集树路径')
            if path == TREE_PATH:
                raise KitError('数据集含保留的引用标记名称')
            info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                safe(info, directory=True)
                item = {'path': path, 'kind': 'directory', 'stat': ([info.st_dev, info.st_ino, 0, 0, 0] if readme_mode else list(_stable(info)))}
                entries.append(item)
                sub = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                try:
                    if _stable(os.fstat(sub)) != _stable(info):
                        raise KitError('数据集目录发生变化')
                    walk(sub, path, depth + 1)
                finally:
                    os.close(sub)
            else:
                safe(info, regular=True, hardlink=True)
                item = {'path': path, 'kind': 'file', 'stat': list(_stable(info))}
                if hash_files:
                    source = os.open(child.name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                    try:
                        if _stable(os.fstat(source)) != _stable(info):
                            raise KitError('数据集文件发生变化')
                        digest = hashlib.sha256()
                        while True:
                            check(); block = os.read(source, 1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                        if _stable(os.fstat(source)) != _stable(info):
                            raise KitError('数据集文件在登记时变化')
                        item['sha256'] = digest.hexdigest()
                    finally:
                        os.close(source)
                entries.append(item)
            if len(entries) > MAX_ENTRIES:
                raise KitError('数据集树条目超过上限')
        if _stable(os.fstat(fd)) != _stable(before):
            raise KitError('数据集目录在读取时变化')
    root_info = os.fstat(root_fd)
    walk(root_fd, '', 0)
    return {'root_stat': ([root_info.st_dev, root_info.st_ino, 0, 0, 0] if readme_mode else list(_stable(root_info))), 'entries': sorted(entries, key=lambda item: item['path'])}


def prepare_tree(root: Path, *, seconds=3600, readme_mode=False):
    if not root.is_absolute() or '..' in root.parts:
        raise KitError('数据集根路径无效')
    fd = DatasetRegistry._open_directory(root, allow_receiving=readme_mode)
    try:
        snapshot = tree_metadata(fd, hash_files=True, seconds=seconds, readme_mode=readme_mode)
    finally:
        os.close(fd)
    return {'schema': TREE_SCHEMA_README if readme_mode else TREE_SCHEMA, 'root': str(root), **snapshot}


def _trusted_manifest(path):
    # Same trusted reader, but explicit larger bound for reviewed tree manifests.
    # Do not mutate module limits globally: read via a checked descriptor instead.
    current = Path('/')
    for part in path.parts[1:-1]:
        current /= part; _safe_info(os.lstat(current), directory=True)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd); _safe_info(before, regular=True, hardlink=True)
        if before.st_size > MAX_MANIFEST_BYTES:
            raise KitError('数据集树清单过大')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES or _stable(os.fstat(fd)) != _stable(before):
            raise KitError('数据集树清单在读取时变化')
        return raw
    finally:
        os.close(fd)


class TreeRegistry:
    def __init__(self, path: Path | None):
        self.path = path

    def _load(self):
        if self.path is None:
            return {}
        try:
            doc = json.loads(DatasetRegistry(self.path)._read_bytes())
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise KitError('数据集树注册表无效') from exc
        if (type(doc) is not dict or set(doc) != {'schema', 'datasets'} or
                doc['schema'] != REGISTRY_SCHEMA or type(doc['datasets']) is not list or len(doc['datasets']) > 4096):
            raise KitError('数据集树注册表格式无效')
        result = {}
        for item in doc['datasets']:
            required = {'dataset_id', 'manifest', 'sha256', 'total_size', 'file_count', 'readers'}
            if type(item) is not dict or set(item) != required:
                raise KitError('数据集树登记字段无效')
            ident = item['dataset_id']
            if (not isinstance(ident, str) or len(ident) > 128 or ident in {'.', '..'} or
                    not _DATASET_ID.fullmatch(ident) or ident in result or
                    not isinstance(item['sha256'], str) or not _HASH.fullmatch(item['sha256']) or
                    type(item['total_size']) is not int or item['total_size'] < 0 or
                    type(item['file_count']) is not int or not 0 <= item['file_count'] <= MAX_ENTRIES):
                raise KitError('数据集树登记身份或规模无效')
            path = item['manifest']
            if not isinstance(path, str) or not Path(path).is_absolute() or '..' in Path(path).parts:
                raise KitError('数据集树清单路径无效')
            if item['readers'] != 'platform-users' and type(item['readers']) is not list:
                raise KitError('数据集树授权无效')
            seen = set()
            for reader in (item['readers'] if isinstance(item['readers'], list) else []):
                if (type(reader) is not dict or set(reader) != {'uid', 'username'} or
                        type(reader['uid']) is not int or reader['uid'] < 1 or
                        not isinstance(reader['username'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', reader['username'])):
                    raise KitError('数据集树授权身份无效')
                identity = (reader['uid'], reader['username'])
                if identity in seen:
                    raise KitError('数据集树授权重复')
                seen.add(identity)
            result[ident] = {**item, 'readers_set': None if item['readers'] == 'platform-users' else seen}
        return result

    @staticmethod
    def reference(item):
        return {'dataset_id': item['dataset_id'], 'path': TREE_PATH,
                'size': item['total_size'], 'sha256': item['sha256']}

    def catalog(self, principal):
        identity = _identity(principal)[:2]
        return [{'dataset_id': item['dataset_id'], 'kind': 'directory', 'file_count': item['file_count'],
                 'total_size': item['total_size'], 'files': [self.reference(item)]}
                for item in self._load().values() if item['readers_set'] is None or identity in item['readers_set']]

    def authorize(self, principals, references):
        normalized = _validate_data_references(references)
        if not normalized:
            return []
        identities = DatasetRegistry._principals(principals)
        items = self._load()
        for ref in normalized:
            item = items.get(ref['dataset_id'])
            if not is_tree_reference(ref) or item is None or ref != self.reference(item):
                raise KitError('数据集树引用不存在或版本不匹配')
            if item['readers_set'] is not None and any(identity[:2] not in item['readers_set'] for identity in identities):
                raise KitError('无权使用数据集树')
        return normalized

    @staticmethod
    def _manifest(item):
        try:
            raw = _trusted_manifest(Path(item['manifest']))
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise KitError('数据集树清单摘要变化')
            doc = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError, OSError) as exc:
            raise KitError('数据集树清单不可读取') from exc
        if (type(doc) is not dict or set(doc) != {'schema', 'root', 'root_stat', 'entries'} or
                doc['schema'] not in {TREE_SCHEMA, TREE_SCHEMA_README} or not isinstance(doc['root'], str) or
                not Path(doc['root']).is_absolute() or '..' in Path(doc['root']).parts or
                type(doc['entries']) is not list or len(doc['entries']) > MAX_ENTRIES):
            raise KitError('数据集树清单格式无效')
        if (doc['schema'] == TREE_SCHEMA_README and
                (type(doc['root_stat']) is not list or len(doc['root_stat']) != 5 or doc['root_stat'][2:] != [0, 0, 0])):
            raise KitError('数据集树根指纹无效')
        fingerprints = [doc['root_stat']]
        names = set(); total = 0; count = 0
        for entry in doc['entries']:
            if type(entry) is not dict or entry.get('kind') not in {'file', 'directory'}:
                raise KitError('数据集树条目无效')
            expected = {'path', 'kind', 'stat'} | ({'sha256'} if entry['kind'] == 'file' else set())
            if set(entry) != expected:
                raise KitError('数据集树条目字段无效')
            path = _relative_path(entry['path'], label='数据集树路径')
            if (path in names or path == TREE_PATH or
                    (doc['schema'] == TREE_SCHEMA_README and '/' not in path and path.casefold() == 'readme.md')):
                raise KitError('数据集树路径重复或保留')
            names.add(path); fingerprints.append(entry['stat'])
            if entry['kind'] == 'file':
                if not isinstance(entry['sha256'], str) or not _HASH.fullmatch(entry['sha256']):
                    raise KitError('数据集树文件摘要无效')
                count += 1
                if type(entry['stat']) is list and len(entry['stat']) == 5 and type(entry['stat'][2]) is int:
                    total += entry['stat'][2]
        if any(type(value) is not list or len(value) != 5 or any(type(part) is not int or part < 0 for part in value) for value in fingerprints):
            raise KitError('数据集树文件指纹无效')
        if count != item['file_count'] or total != item['total_size']:
            raise KitError('数据集树清单规模不匹配')
        return doc

    def open_file(self, principal, dataset_id, path):
        """Return one authorized readonly FD for IDE access, never a host path."""
        path = _relative_path(path, label='数据文件路径')
        item = self._load().get(dataset_id)
        if item is None:
            raise KitError('数据集树不存在')
        self.authorize([principal], [self.reference(item)])
        document = self._manifest(item)
        entry = next((entry for entry in document['entries'] if entry['path'] == path and entry['kind'] == 'file'), None)
        if entry is None:
            raise KitError('文件不在已登记数据集版本中')
        root_fd = DatasetRegistry._open_directory(Path(document['root']), allow_receiving=document['schema'] == TREE_SCHEMA_README)
        try:
            fd, before = DatasetRegistry._open_file(root_fd, path)
        finally:
            os.close(root_fd)
        try:
            if list(before) != entry['stat']:
                raise KitError('数据集文件版本已变化')
            digest = hashlib.sha256()
            deadline, next_check = time.monotonic() + 600, [0.0]
            while True:
                DatasetRegistry._cancelled(lambda: False, deadline, next_check)
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            if digest.hexdigest() != entry['sha256'] or _stable(os.fstat(fd)) != before:
                raise KitError('数据集文件内容已变化')
            self.authorize([principal], [self.reference(item)])
            os.lseek(fd, 0, os.SEEK_SET)
            return fd, {'dataset_id': dataset_id, 'path': path, 'size': entry['stat'][2], 'sha256': entry['sha256']}
        except Exception:
            os.close(fd)
            raise

    def list_files(self, principal, dataset_id, *, q='', offset=0, limit=100):
        if (not isinstance(q, str) or len(q) > 256 or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 200):
            raise KitError('数据文件浏览参数无效')
        item = self._load().get(dataset_id)
        if item is None:
            raise KitError('数据集树不存在')
        self.authorize([principal], [self.reference(item)])
        document = self._manifest(item)
        files = [entry for entry in document['entries'] if entry['kind'] == 'file' and q.casefold() in entry['path'].casefold()]
        rows = [{'path': entry['path'], 'size': entry['stat'][2], 'sha256': entry['sha256']} for entry in files[offset:offset+limit]]
        return {'items': rows, 'total': len(files), 'next_offset': offset+limit if offset+limit < len(files) else None}

    @staticmethod
    def _verify(fd, document, cancelled):
        observed = tree_metadata(fd, hash_files=True, cancelled=cancelled, seconds=3600,
                                 readme_mode=document['schema'] == TREE_SCHEMA_README)
        expected = {'root_stat': document['root_stat'], 'entries': document['entries']}
        if observed != expected:
            raise KitError('已登记数据集树发生变化，请登记新版本')

    @contextmanager
    def open_references(self, principals, references, *, cancelled=lambda: False):
        refs = self.authorize(principals, references)
        items = self._load(); held = []; mounts = []
        try:
            for ref in refs:
                document = self._manifest(items[ref['dataset_id']])
                fd = DatasetRegistry._open_directory(Path(document['root']), allow_receiving=document['schema'] == TREE_SCHEMA_README)
                held.append((fd, document))
                self._verify(fd, document, cancelled)
                mounts.append(DatasetMount(dict(ref), fd, '/datasets/' + ref['dataset_id']))
            self.authorize(principals, refs)
            yield mounts
            for fd, doc in held:
                self._verify(fd, doc, cancelled)
        except OSError as exc:
            raise KitError('数据集树不可读取') from exc
        finally:
            for fd, _ in held:
                os.close(fd)
