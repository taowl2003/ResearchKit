"""Root-only, recoverable permanent removal of one retired raw dataset tree.

The broker decides *whether* a retired item has no retained references.  This
module owns the much narrower root-side operation: remove the reviewed tree,
its registry row, description and manifest.  It deliberately accepts no paths
from the broker.  A receipt is written before the first rename, so a retry can
only ever continue with the quarantined inode and cannot remove a later
directory recreated at the old name.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .files import KitError
from .web_data_publisher import _atomic, _backup_and_update, _read, canonical
from .web_dataset_metadata import SCHEMA as METADATA_SCHEMA, enrich_catalog
from .web_dataset_trees import TREE_SCHEMA_README, TreeRegistry, tree_metadata
from .web_datasets import DatasetRegistry, _json_no_duplicates, _safe_info, _stable


_SCHEMA = 'researchkit.web-data-cleanup-receipt/v2'
_PHASES = {'prepared', 'quarantined', 'registry-removed', 'metadata-removed',
           'manifest-removed', 'completed'}
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')


def _root_only():
    if os.geteuid() != 0:
        raise KitError('永久清理只能由已安装的数据后台执行')


def _entry(item):
    return {key: value for key, value in item.items() if key != 'readers_set'}


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _metadata_entry_digest(datasets, dataset_id):
    """A compare-and-swap token for just one description row."""
    return _digest({'present': dataset_id in datasets,
                    'value': datasets.get(dataset_id)})


def _expect_request(request):
    if type(request) is not dict or set(request) != {'request_id', 'dataset_id', 'expected_entry'}:
        raise KitError('永久清理任务字段无效')
    request_id, dataset_id, expected = request['request_id'], request['dataset_id'], request['expected_entry']
    if (not isinstance(request_id, str) or not _UUID.fullmatch(request_id) or
            str(uuid.UUID(request_id)) != request_id):
        raise KitError('永久清理任务身份无效')
    if not isinstance(dataset_id, str) or not dataset_id or not isinstance(expected, dict):
        raise KitError('永久清理必须指向已登记的准确版本')
    if expected.get('dataset_id') != dataset_id:
        raise KitError('永久清理任务的数据集身份不一致')
    # TreeRegistry is the only parser permitted to validate the remaining
    # entry fields.  Do not duplicate a looser local schema here.
    return request_id, dataset_id, expected


def _private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    fd = DatasetRegistry._open_directory(path)
    try:
        os.fchmod(fd, 0o700)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_receipt(path, receipt):
    """Use publisher atomicity, then tighten its generic 0644 file mode."""
    _atomic(path, canonical(receipt) + b'\n')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _safe_info(os.fstat(fd), regular=True, hardlink=True)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_receipt(path):
    try:
        value = json.loads(_read(path), object_pairs_hook=_json_no_duplicates)
    except (ValueError, UnicodeError, RecursionError, OSError) as exc:
        raise KitError('永久清理恢复记录不可读取') from exc
    required = {'schema', 'request_sha256', 'dataset_id', 'expected_entry', 'root',
                'root_dev', 'root_ino', 'holding', 'bytes_removed', 'manifest',
                'manifest_sha256', 'metadata_entry_sha256', 'phase'}
    if (type(value) is not dict or set(value) != required or value.get('schema') != _SCHEMA or
            not isinstance(value['request_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['request_sha256']) or
            not isinstance(value['dataset_id'], str) or not isinstance(value['expected_entry'], dict) or
            not isinstance(value['root'], str) or not isinstance(value['holding'], str) or
            type(value['root_dev']) is not int or type(value['root_ino']) is not int or
            type(value['bytes_removed']) is not int or value['bytes_removed'] < 0 or
            not isinstance(value['manifest'], str) or not isinstance(value['manifest_sha256'], str) or
            any(not isinstance(value[key], str) or not re.fullmatch(r'[0-9a-f]{64}', value[key])
                for key in ('manifest_sha256', 'metadata_entry_sha256')) or
            value.get('phase') not in _PHASES):
        raise KitError('永久清理恢复记录格式无效')
    return value


def _under(child, parent):
    return child != parent and parent in child.parents


def _path(path, label):
    if not isinstance(path, str) or not path:
        raise KitError(label + '无效')
    result = Path(path)
    if not result.is_absolute() or '..' in result.parts:
        raise KitError(label + '无效')
    return result


def _registered_roots(policy, *, dataset_id, root, holding, allow_target_missing):
    """Fail closed if a newly registered unit would share either delete route."""
    records = TreeRegistry(policy.dataset_tree_registry)._load()
    target = records.get(dataset_id)
    if target is None and not allow_target_missing:
        raise KitError('数据集登记已变化，请刷新后重新清理')
    roots = []
    for ident, item in records.items():
        if ident != dataset_id:
            roots.append(_path(TreeRegistry._manifest(item)['root'], '其他数据集根目录'))
    legacy = getattr(policy, 'dataset_registry', None)
    if legacy is not None:
        for item in DatasetRegistry(legacy)._load().values():
            roots.append(Path(item['root']))
    for other in roots:
        if (root == other or root in other.parents or other in root.parents or
                holding == other or holding in other.parents or other in holding.parents):
            raise KitError('数据集目录或清理隔离目录与其他已登记版本存在包含关系，不能永久清理')
    return records


def _validate_resume_config(policy, config, receipt):
    if not isinstance(config, dict) or config.get('prepare_permissions') is not True:
        raise KitError('永久清理恢复要求当前仍启用权限准备')
    raw_root = _path(str(config.get('raw_root', '')), '共享数据根目录')
    root = _path(receipt['root'], '清理根目录')
    holding = _path(receipt['holding'], '清理隔离目录')
    if not _under(root, raw_root) or holding.parent != root.parent:
        raise KitError('当前共享数据根目录与永久清理恢复记录不一致')
    descriptor = DatasetRegistry._open_directory(raw_root, allow_receiving=True)
    os.close(descriptor)
    records = _registered_roots(policy, dataset_id=receipt['dataset_id'], root=root,
                                holding=holding, allow_target_missing=True)
    target = records.get(receipt['dataset_id'])
    if target is not None and _entry(target) != receipt['expected_entry']:
        raise KitError('数据集登记已变化，请刷新后重新清理')
    if target is not None and receipt['phase'] not in {'prepared', 'quarantined'}:
        raise KitError('已删除的目标数据集登记又出现，停止永久清理')
    return raw_root, root, holding


def _validate_root(policy, config, dataset_id, expected):
    if not isinstance(config, dict) or config.get('prepare_permissions') is not True:
        raise KitError('永久清理仅支持已启用权限准备的 README 数据集')
    raw_root = _path(str(config.get('raw_root', '')), '共享数据根目录')
    if getattr(policy, 'dataset_tree_registry', None) is None or getattr(policy, 'dataset_metadata', None) is None:
        raise KitError('尚未配置数据集树登记')
    records = TreeRegistry(policy.dataset_tree_registry)._load()
    current = records.get(dataset_id)
    if current is None or _entry(current) != expected:
        raise KitError('数据集登记已变化，请刷新后重新清理')
    document = TreeRegistry._manifest(current)
    if document['schema'] != TREE_SCHEMA_README:
        raise KitError('旧式逐文件或非 README 数据集不能使用永久清理')
    root = _path(document['root'], '已登记数据集根目录')
    _reject_mounts(root)
    if not _under(root, raw_root):
        raise KitError('已登记数据集不在受控共享数据根目录内')
    manifest = _path(current['manifest'], '数据集树清单')
    parent = Path(policy.dataset_tree_registry).parent
    manifest_root = parent / 'dataset-manifests'
    if manifest.parent != manifest_root or manifest.name != dataset_id + '.json':
        raise KitError('数据集树清单不属于受控发布目录')
    return raw_root, root, parent, current, document, manifest


def _reject_mounts(path):
    # Device equality alone misses same-filesystem bind mounts.  A registered
    # root may live under the service's raw-root bind, but must not itself be
    # a mountpoint or contain any nested mount, even on the same device.
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
        points = []
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            if len(parts) < 5:
                raise ValueError('short mountinfo line')
            escaped = parts[4]
            decoded = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), escaped)
            points.append(Path(decoded))
    except (OSError, IndexError, ValueError) as exc:
        raise KitError('无法核对数据目录挂载信息，停止永久清理') from exc
    if any(point == path or path in point.parents for point in points):
        raise KitError('永久清理拒绝数据集内的挂载点')


def _snapshot(fd, document):
    for name in os.listdir(fd):
        if name.casefold() == 'readme.md':
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise KitError('永久清理拒绝非普通单链接 README')
    observed = tree_metadata(fd, hash_files=True, seconds=3600, readme_mode=True)
    device = observed['root_stat'][0]
    if any(entry['stat'][0] != device for entry in observed['entries']):
        raise KitError('数据集含嵌套挂载点，不能永久清理')
    if {'root_stat': observed['root_stat'], 'entries': observed['entries']} != {
            'root_stat': document['root_stat'], 'entries': document['entries']}:
        raise KitError('数据集内容或目录指纹已变化，不能永久清理')


def _open_exact_directory(path, dev, ino):
    fd = DatasetRegistry._open_directory(path)
    try:
        info = os.fstat(fd)
        if info.st_dev != dev or info.st_ino != ino:
            raise KitError('永久清理隔离目录身份已变化')
        return fd
    except Exception:
        os.close(fd)
        raise


def _rename_to_holding(root, holding, dev, ino):
    parent = DatasetRegistry._open_directory(root.parent, allow_receiving=True)
    try:
        root_info = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        _safe_info(root_info, directory=True)
        if root_info.st_dev != dev or root_info.st_ino != ino:
            raise KitError('数据集根目录在永久清理前已变化')
        try:
            os.stat(holding.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise KitError('永久清理隔离目录已存在')
        os.rename(root.name, holding.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)
    fd = _open_exact_directory(holding, dev, ino)
    try:
        os.fchmod(fd, 0o700)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_tree_at(parent_fd, name, *, device, expected_dev, expected_ino, root=False):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _safe_info(info, directory=True)
    if info.st_dev != device or (root and (info.st_dev != expected_dev or info.st_ino != expected_ino)):
        raise KitError('永久清理发现目录替换或挂载点')
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if _stable(os.fstat(fd))[:2] != (info.st_dev, info.st_ino):
            raise KitError('永久清理目录读取时变化')
        with os.scandir(fd) as stream:
            names = [entry.name for entry in stream]
        for child in names:
            entry = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if entry.st_dev != device:
                raise KitError('永久清理拒绝跨挂载点目录')
            if stat.S_ISDIR(entry.st_mode):
                _remove_tree_at(fd, child, device=device, expected_dev=0, expected_ino=0)
            elif stat.S_ISREG(entry.st_mode):
                # README.md is deliberately outside the v2 snapshot and may
                # be maintained by ly.  It is still a one-link regular file;
                # every data file remains root-owned/read-only as before.
                if child.casefold() == 'readme.md' and root:
                    if entry.st_nlink != 1:
                        raise KitError('永久清理拒绝带硬链接的 README')
                else:
                    _safe_info(entry, regular=True, hardlink=True)
                os.unlink(child, dir_fd=fd)
            else:
                raise KitError('永久清理只接受普通文件和目录')
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _remove_holding(holding, receipt):
    _reject_mounts(holding)
    parent = DatasetRegistry._open_directory(holding.parent, allow_receiving=True)
    try:
        if os.fstat(parent).st_dev != receipt['root_dev']:
            raise KitError('永久清理拒绝跨挂载点数据集根目录')
        _remove_tree_at(parent, holding.name, device=receipt['root_dev'],
                        expected_dev=receipt['root_dev'], expected_ino=receipt['root_ino'], root=True)
    finally:
        os.close(parent)


def _new_receipt(policy, config, request_id, dataset_id, expected, request_hash):
    raw_root, root, parent, current, document, manifest = _validate_root(policy, config, dataset_id, expected)
    # Opening the configured root proves its complete parent chain.  Opening
    # the selected root applies the original root-owned/no-write trust policy.
    raw_fd = DatasetRegistry._open_directory(raw_root, allow_receiving=True)
    os.close(raw_fd)
    root_fd = DatasetRegistry._open_directory(root)
    try:
        info = os.fstat(root_fd)
        if [info.st_dev, info.st_ino, 0, 0, 0] != document['root_stat']:
            raise KitError('已登记数据集根目录已变化，不能永久清理')
        _snapshot(root_fd, document)
    finally:
        os.close(root_fd)
    root_parent_fd = DatasetRegistry._open_directory(root.parent, allow_receiving=True)
    try:
        if os.fstat(root_parent_fd).st_dev != info.st_dev:
            raise KitError('数据集根目录是独立挂载点，不能永久清理')
    finally:
        os.close(root_parent_fd)
    registry_raw, metadata_raw = _read(policy.dataset_tree_registry), _read(policy.dataset_metadata)
    enrich_catalog([], policy.dataset_metadata)
    registry = json.loads(registry_raw, object_pairs_hook=_json_no_duplicates)
    metadata = json.loads(metadata_raw, object_pairs_hook=_json_no_duplicates)
    if (type(registry) is not dict or type(registry.get('datasets')) is not list or
            type(metadata) is not dict or metadata.get('schema') != METADATA_SCHEMA or type(metadata.get('datasets')) is not dict):
        raise KitError('数据集登记或说明格式无效')
    rows = registry['datasets']
    if not any(row == expected for row in rows):
        raise KitError('数据集登记已变化，请刷新后重新清理')
    holding = root.parent / ('.researchkit-cleanup-' + request_id)
    _registered_roots(policy, dataset_id=dataset_id, root=root, holding=holding,
                      allow_target_missing=False)
    return parent, {'schema': _SCHEMA, 'request_sha256': request_hash, 'dataset_id': dataset_id,
                    'expected_entry': expected, 'root': str(root), 'root_dev': info.st_dev,
                    'root_ino': info.st_ino, 'holding': str(holding),
                    'bytes_removed': current['total_size'], 'manifest': str(manifest),
                    'manifest_sha256': current['sha256'],
                    'metadata_entry_sha256': _metadata_entry_digest(metadata['datasets'], dataset_id),
                    'phase': 'prepared'}


def purge_raw(policy, config, request):
    """Permanently remove one v2 tree after the broker has authorized it.

    It never accepts a filesystem path and it never deletes an item merely
    because its registration disappeared: a matching root-only receipt is
    required to resume every interrupted operation.
    """
    _root_only()
    request_id, dataset_id, expected = _expect_request(request)
    request_hash = _digest(request)
    registry_path = getattr(policy, 'dataset_tree_registry', None)
    metadata_path = getattr(policy, 'dataset_metadata', None)
    if registry_path is None or metadata_path is None or Path(registry_path).parent != Path(metadata_path).parent:
        raise KitError('管理员尚未配置数据发布目录')
    parent = Path(registry_path).parent
    checked = DatasetRegistry._open_directory(parent); os.close(checked)
    lock = os.open(parent / '.data-publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _safe_info(os.fstat(lock), regular=True, hardlink=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipts = parent / 'data-cleanup-receipts'
        _private_directory(receipts)
        receipt_path = receipts / (request_id + '.json')
        if receipt_path.exists():
            receipt = _read_receipt(receipt_path)
            if receipt['request_sha256'] != request_hash or receipt['dataset_id'] != dataset_id or receipt['expected_entry'] != expected:
                raise KitError('永久清理恢复记录不属于本次任务')
        else:
            _parent, receipt = _new_receipt(policy, config, request_id, dataset_id, expected, request_hash)
            _write_receipt(receipt_path, receipt)
        if receipt['phase'] == 'completed':
            return {'dataset_id': dataset_id, 'deleted': True, 'bytes_removed': receipt['bytes_removed'], 'reused': True}
        _raw_root, root, holding = _validate_resume_config(policy, config, receipt)
        if holding.parent != root.parent or holding.name != '.researchkit-cleanup-' + request_id:
            raise KitError('永久清理恢复记录隔离目录无效')
        if receipt['phase'] == 'prepared':
            if holding.exists():
                if root.exists():
                    raise KitError('永久清理恢复发现原目录和隔离目录同时存在')
                descriptor = _open_exact_directory(holding, receipt['root_dev'], receipt['root_ino'])
                os.close(descriptor)
            else:
                _rename_to_holding(root, holding, receipt['root_dev'], receipt['root_ino'])
            receipt = {**receipt, 'phase': 'quarantined'}; _write_receipt(receipt_path, receipt)
        elif root.exists():
            # A new directory at the old name is never part of a resumed
            # deletion, even if it happens to have the same contents.
            raise KitError('永久清理恢复发现原目录已被重新创建，已停止')
        if receipt['phase'] == 'quarantined':
            # Verify the immutable data snapshot after isolation and before
            # any registry row becomes invisible.
            fd = _open_exact_directory(holding, receipt['root_dev'], receipt['root_ino'])
            try:
                item = TreeRegistry(registry_path)._load().get(dataset_id)
                if item is not None and _entry(item) != expected:
                    raise KitError('数据集登记已变化，请刷新后重新清理')
                document = TreeRegistry._manifest(item) if item is not None else None
                # If the registry write already completed before a crash, use
                # the manifest path recorded in the receipt; it remains trusted
                # and its digest pins the same reviewed bytes.
                if document is None:
                    raw = _read(_path(receipt['manifest'], '数据集树清单'))
                    if hashlib.sha256(raw).hexdigest() != receipt['manifest_sha256']:
                        raise KitError('永久清理清单摘要已变化')
                    document = TreeRegistry._manifest(receipt['expected_entry'])
                _snapshot(fd, document)
            finally:
                os.close(fd)
            before = _read(registry_path)
            document = json.loads(before, object_pairs_hook=_json_no_duplicates)
            rows = document.get('datasets') if type(document) is dict else None
            if type(rows) is not list:
                raise KitError('数据集树登记格式无效')
            targets = [row for row in rows if type(row) is dict and row.get('dataset_id') == dataset_id]
            if targets:
                if len(targets) != 1 or targets[0] != expected:
                    raise KitError('数据集登记已变化，请刷新后重新清理')
                document['datasets'] = [row for row in rows if row is not targets[0]]
                _backup_and_update(registry_path, before, canonical(document) + b'\n')
            receipt = {**receipt, 'phase': 'registry-removed'}; _write_receipt(receipt_path, receipt)
        if receipt['phase'] == 'registry-removed':
            before = _read(metadata_path)
            document = json.loads(before, object_pairs_hook=_json_no_duplicates)
            datasets = document.get('datasets') if type(document) is dict else None
            if type(document) is not dict or document.get('schema') != METADATA_SCHEMA or type(datasets) is not dict:
                raise KitError('数据集说明格式无效')
            if _metadata_entry_digest(datasets, dataset_id) != receipt['metadata_entry_sha256']:
                if dataset_id in datasets:
                    raise KitError('目标数据集说明在永久清理期间变化，已停止')
            elif dataset_id in datasets:
                document['datasets'] = {key: value for key, value in datasets.items() if key != dataset_id}
                _backup_and_update(metadata_path, before, canonical(document) + b'\n')
            receipt = {**receipt, 'phase': 'metadata-removed'}; _write_receipt(receipt_path, receipt)
        if receipt['phase'] == 'metadata-removed':
            manifest = _path(receipt['manifest'], '数据集树清单')
            if manifest.parent != parent / 'dataset-manifests' or manifest.name != dataset_id + '.json':
                raise KitError('永久清理清单路径无效')
            try:
                info = os.lstat(manifest)
            except FileNotFoundError:
                pass
            else:
                _safe_info(info, regular=True, hardlink=True)
                if hashlib.sha256(_read(manifest)).hexdigest() != receipt['manifest_sha256']:
                    raise KitError('永久清理清单摘要已变化')
                os.unlink(manifest)
                # Its directory is trusted by the checks above.
                descriptor = os.open(manifest.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
            receipt = {**receipt, 'phase': 'manifest-removed'}; _write_receipt(receipt_path, receipt)
        if receipt['phase'] == 'manifest-removed':
            if holding.exists():
                _remove_holding(holding, receipt)
            receipt = {**receipt, 'phase': 'completed'}; _write_receipt(receipt_path, receipt)
        return {'dataset_id': dataset_id, 'deleted': True, 'bytes_removed': receipt['bytes_removed']}
    finally:
        os.close(lock)
