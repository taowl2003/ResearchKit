"""Change only the readers of one registered raw tree, under publisher locking."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from pathlib import Path

from .files import KitError
from .web_dataset_trees import TreeRegistry
from .web_datasets import DatasetRegistry, _json_no_duplicates, _safe_info
from .web_identity import IdentityError


def registered_entry(policy, dataset_id):
    item = TreeRegistry(policy.dataset_tree_registry)._load().get(dataset_id)
    if item is None:
        raise KitError('已登记的数据集版本不存在，请刷新查看')
    return {key: value for key, value in item.items() if key != 'readers_set'}


def access_token(entry):
    raw = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def apply_access(policy, config, task):
    from .web_data_publisher import _read, _atomic, _backup_and_update, _readers, canonical, _source_path
    fields = {'kind', 'request_id', 'candidate_id', 'revision', 'relative_root', 'dataset_id',
              'expected_entry', 'readers', 'reviewer_uid', 'reviewer_username'}
    if type(task) is not dict or set(task) != fields or task.get('kind') != 'access-change':
        raise KitError('数据授权任务字段无效')
    try:
        if str(uuid.UUID(task['request_id'])) != task['request_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise KitError('数据授权任务身份无效') from exc
    path = _source_path(config, task['relative_root'])
    if task['candidate_id'] != hashlib.sha256(str(path).encode()).hexdigest()[:24]:
        raise KitError('数据授权边界不一致')
    if (type(task['expected_entry']) is not dict or
            task['expected_entry'].get('dataset_id') != task['dataset_id']):
        raise KitError('数据授权必须指向已登记的准确版本')
    try:
        readers = _readers(policy, task['readers'])  # Recheck UID/name at execution.
    except (KeyError, ValueError, OSError, IdentityError) as error:
        raise KitError('授权账号身份已变化，请刷新后重新修改') from error
    registry_path = policy.dataset_tree_registry
    if registry_path is None:
        raise KitError('尚未配置数据登记文件')
    parent = registry_path.parent
    fd = DatasetRegistry._open_directory(parent); os.close(fd)
    lock = os.open(parent / '.data-publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _safe_info(os.fstat(lock), regular=True, hardlink=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = _read(registry_path)
        registry = json.loads(before, object_pairs_hook=_json_no_duplicates)
        current = registered_entry(policy, task['dataset_id'])
        if TreeRegistry._manifest(current)['root'] != str(path):
            raise KitError('授权版本与已登记目录不一致')
        expected = task['expected_entry']
        updated = {**expected, 'readers': readers}
        receipts = parent / 'data-access-receipts'
        receipts.mkdir(mode=0o700, exist_ok=True)
        fd = DatasetRegistry._open_directory(receipts); os.close(fd)
        receipt_path = receipts / (task['request_id'] + '.json')
        digest = hashlib.sha256(canonical(task)).hexdigest()
        receipt = None
        if receipt_path.exists():
            receipt = json.loads(_read(receipt_path), object_pairs_hook=_json_no_duplicates)
            if receipt.get('task_sha256') != digest or receipt.get('phase') not in {'prepared', 'applied'}:
                raise KitError('授权恢复记录与任务不一致')
            if current == updated:
                _atomic(receipt_path, canonical({'task_sha256': digest, 'phase': 'applied'}) + b'\n')
                return {'dataset_id': task['dataset_id'], 'readers': readers, 'reused': True}
            if receipt['phase'] == 'applied':
                raise KitError('此授权任务已完成，后续权限已有变化，请刷新')
        if current != expected:
            raise KitError('数据授权已变化，请刷新后重新修改')
        if receipt is None:
            _atomic(receipt_path, canonical({'task_sha256': digest, 'phase': 'prepared'}) + b'\n')
        for item in registry['datasets']:
            if item['dataset_id'] == task['dataset_id']:
                item['readers'] = readers
        _backup_and_update(registry_path, before, canonical(registry) + b'\n')
        _atomic(receipt_path, canonical({'task_sha256': digest, 'phase': 'applied'}) + b'\n')
        return {'dataset_id': task['dataset_id'], 'readers': readers}
    finally:
        os.close(lock)
