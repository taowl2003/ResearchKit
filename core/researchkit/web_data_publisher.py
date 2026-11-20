"""Restricted root data publisher; never executes dataset or project code.

The broker owns reviews and queue state. This service only discovers configured
roots and publishes exact reviewed snapshots into root-controlled JSON files.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
import uuid

from .files import KitError
from .web_broker import BrokerClient, _safe_message
from .web_identity import load_policy
from .web_datasets import DatasetRegistry, _json_no_duplicates, _safe_info, _stable
from .web_dataset_metadata import _FIELDS, SCHEMA as METADATA_SCHEMA, enrich_catalog, normalize_tags
from .web_dataset_trees import TreeRegistry, prepare_tree, tree_metadata


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _read(path):
    return DatasetRegistry(path)._read_bytes()


def _metadata(value):
    if type(value) is not dict or set(value) - _FIELDS:
        raise KitError('只允许受限的文字说明，不接受权限或目录字段')
    result = dict(value)
    for key, text in result.items():
        if key == 'tags':
            result[key] = normalize_tags(text)
        elif not isinstance(text, str) or len(text.encode()) > 16384:
            raise KitError('只允许受限的文字说明，不接受权限或目录字段')
    return result


def _atomic(path, raw):
    if len(raw) > 2 * 1024**2:
        raise KitError('登记或说明清单超过 2 MiB')
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_path(config, relative):
    if (not isinstance(relative, str) or not relative or '\x00' in relative or '\\' in relative
            or Path(relative).is_absolute() or '..' in Path(relative).parts or
            Path(relative).as_posix() != relative or len(relative.encode()) > 4096):
        raise KitError('审核的数据集边界无效')
    path = Path(config['raw_root']) / relative
    if path == Path(config['raw_root']):
        raise KitError('不能把整个共享根目录作为一个候选')
    return path


def _readers(policy, value):
    if value == 'platform-users':
        return value
    if type(value) is not list or not value or len(value) > 128:
        raise KitError('请选择平台用户或指定的真实账号')
    import pwd
    result = []
    names = set()
    for record in value:
        name = record.get('username') if type(record) is dict else record
        if not isinstance(name, str) or name in names:
            raise KitError('数据读取账号无效或重复')
        account = pwd.getpwnam(name)
        principal = policy.principal(account.pw_uid)
        if principal.username != name or (type(record) is dict and
                (set(record) != {'uid', 'username'} or record.get('uid') != principal.uid)):
            raise KitError('审核账号身份已变化')
        names.add(name)
        result.append({'uid': principal.uid, 'username': principal.username})
    return result


def publish(policy, config, candidate):
    """Publish one immutable reviewed task; retry safely after a lost receipt."""
    if type(candidate) is not dict:
        raise KitError('发布任务格式无效')
    if candidate.get('kind') == 'access-change':
        from .web_dataset_access import apply_access
        return apply_access(policy, config, candidate)
    identity = candidate.get('dataset_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', identity):
        raise KitError('发布版本身份无效')
    path = _source_path(config, candidate.get('relative_root'))
    if candidate.get('candidate_id') != hashlib.sha256(str(path).encode()).hexdigest()[:24]:
        raise KitError('发布边界与已审核身份不一致')
    kind = candidate.get('kind')
    if kind not in {'new', 'metadata-change'}:
        raise KitError('原始目录变化需要保留旧版本并在新目录登记，不能覆盖旧引用')
    info = _metadata(candidate.get('metadata'))
    original = _metadata(candidate.get('original_metadata', {}))
    readme_mode = config.get('discovery_mode') == 'readme'
    deadline = time.monotonic() + int(config['seconds_per_dataset'])
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise KitError('数据准备和登记超时，请刷新扫描后重试')
        return max(1, int(seconds))
    if readme_mode and original.get('provenance_ref', '').startswith('README:'):
        info['description'] = original.get('description', '')
        info['name'] = info.get('name') or original.get('name') or path.name
    for field in ('wiki_ref', 'wiki_fact_state', 'provenance_ref'):
        if field in original:
            info[field] = original[field]
        else:
            info.pop(field, None)
    registry_path, metadata_path = policy.dataset_tree_registry, policy.dataset_metadata
    if registry_path is None or metadata_path is None or registry_path.parent != metadata_path.parent:
        raise KitError('管理员尚未配置数据发布目录')
    parent = registry_path.parent
    # Open the complete trusted parent chain before creating any lock or file.
    descriptor = DatasetRegistry._open_directory(parent)
    os.close(descriptor)
    lock = os.open(parent / '.data-publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _safe_info(os.fstat(lock), regular=True, hardlink=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry_raw, metadata_raw = _read(registry_path), _read(metadata_path)
        registry = json.loads(registry_raw, object_pairs_hook=_json_no_duplicates)
        details = json.loads(metadata_raw, object_pairs_hook=_json_no_duplicates)
        records = TreeRegistry(registry_path)._load()
        enrich_catalog([], metadata_path)
        if identity in DatasetRegistry(policy.dataset_registry)._load():
            raise KitError('版本 ID 已被逐文件数据登记使用，请重新扫描')
        if kind == 'metadata-change':
            if identity != candidate.get('registered_dataset_id') or identity not in records:
                raise KitError('说明更新必须对应已有的准确版本')
            existing = TreeRegistry._manifest(records[identity])
            if existing['root'] != str(path):
                raise KitError('已登记的路径和本次审核不同')
            # This path only changes descriptions. ACLs and manifest stay exact.
            if details['datasets'].get(identity) == info:
                return {'dataset_id': identity, 'published_metadata': info, 'reused': True}
            _verify_source(config, candidate)
            details['datasets'][identity] = info
            _backup_and_update(metadata_path, metadata_raw, canonical(details) + b'\n')
            return {'dataset_id': identity, 'published_metadata': info}
        if identity in records:
            # Lost response after installation: accept only our own exact
            # approved version, never another task or altered manifest.
            existing = TreeRegistry._manifest(records[identity])
            receipt_path = parent / 'data-publish-receipts' / (candidate['candidate_id'] + '-' + identity + '.json')
            receipt = json.loads(_read(receipt_path), object_pairs_hook=_json_no_duplicates)
            if (receipt.get('revision') != candidate.get('revision') or receipt.get('metadata') != info
                    or receipt.get('manifest_sha256') != records[identity]['sha256']
                    or existing['root'] != str(path) or details['datasets'].get(identity) != info):
                raise KitError('已有版本不属于本次审核，请重新扫描')
            return {'dataset_id': identity, 'published_metadata': info, 'reused': True}
        for record in records.values():
            existing_root = Path(TreeRegistry._manifest(record)['root'])
            if existing_root == path:
                raise KitError('该目录已有版本，请更新说明或使用新的版本目录')
            if readme_mode and (existing_root in path.parents or path in existing_root.parents):
                raise KitError('选中目录与已登记版本存在包含关系，不能改变旧版本权限')
        _verify_source(config, candidate)
        expected = candidate.get('source_fingerprint')
        if readme_mode and config.get('prepare_permissions'):
            from .web_dataset_permissions import prepare_dataset
            import pwd
            teacher = policy.principal(pwd.getpwnam('ly').pw_uid)
            if teacher.username != 'ly' or teacher.role != 'teacher':
                raise KitError('缺少可信教师身份')
            preparations = parent / 'data-prepare-receipts'
            preparations.mkdir(mode=0o700, exist_ok=True)
            checked = DatasetRegistry._open_directory(preparations); os.close(checked)
            progress_path = preparations / (candidate['candidate_id'] + '-' + identity + '.json')
            if progress_path.exists():
                progress = json.loads(_read(progress_path), object_pairs_hook=_json_no_duplicates)
                if (progress.get('revision') != candidate.get('revision') or
                        progress.get('source_fingerprint') != expected):
                    raise KitError('权限准备记录不属于本次审核，请重新扫描')
                expected = progress['prepared_fingerprint']
            else:
                prepared = prepare_dataset({**config, 'seconds_per_dataset': remaining()}, candidate['relative_root'], expected, teacher_uid=teacher.uid)
                # Preparation deliberately replaces writable uploaded inodes.
                # A root-only receipt allows retries after a later commit fails.
                from .web_dataset_discovery import snapshot_fingerprint
                sealed = snapshot_fingerprint(prepared['snapshot'])
                _atomic(progress_path, canonical({'revision': candidate['revision'],
                    'source_fingerprint': expected, 'prepared_fingerprint': sealed}) + b'\n')
                expected = sealed
        snapshot = prepare_tree(path, seconds=remaining() if readme_mode else config['seconds_per_dataset'], readme_mode=readme_mode)
        _verify_fingerprint(snapshot, expected)
        raw_fd = DatasetRegistry._open_directory(path)
        try:
            observed = tree_metadata(raw_fd, hash_files=False, seconds=min(remaining() if readme_mode else config['seconds_per_dataset'], 300), readme_mode=readme_mode)
        finally:
            os.close(raw_fd)
        _verify_fingerprint(observed, expected)
        _verify_source(config, candidate)
        raw = canonical(snapshot) + b'\n'
        from .web_dataset_trees import MAX_MANIFEST_BYTES
        if len(raw) > MAX_MANIFEST_BYTES:
            raise KitError('数据集清单超过容量上限')
        manifest_root = parent / 'dataset-manifests'
        receipts = parent / 'data-publish-receipts'
        for directory in (manifest_root, receipts):
            directory.mkdir(mode=0o755, exist_ok=True)
            fd = DatasetRegistry._open_directory(directory)
            try:
                if directory == manifest_root:
                    # The publisher keeps UMask=0077.  Only this trusted,
                    # shared manifest directory needs traversal by the worker;
                    # mkdir(mode=0755) alone is filtered down to 0700.
                    os.fchmod(fd, 0o755)
                    os.fsync(fd)
            finally:
                os.close(fd)
        manifest_path = manifest_root / (identity + '.json')
        if manifest_path.exists():
            from .web_dataset_trees import _trusted_manifest
            if _trusted_manifest(manifest_path) != raw:
                raise KitError('已有清单内容不同，停止登记')
        else:
            # Finish and sync before making the final name visible. link()
            # refuses an existing version; after unlinking the temporary name
            # the trusted manifest has exactly one link again.
            fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=manifest_root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.chmod(temporary, 0o644)
                os.link(temporary, manifest_path, follow_symlinks=False)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            _sync_directory(manifest_root)
        files = [entry for entry in snapshot['entries'] if entry['kind'] == 'file']
        checksum = hashlib.sha256(raw).hexdigest()
        entry = {'dataset_id': identity, 'manifest': str(manifest_path), 'sha256': checksum,
                 'total_size': sum(item['stat'][2] for item in files), 'file_count': len(files),
                 'readers': _readers(policy, candidate.get('readers', 'platform-users'))}
        registry['datasets'].append(entry)
        details['datasets'][identity] = info
        registry_new, metadata_new = canonical(registry) + b'\n', canonical(details) + b'\n'
        if max(len(registry_new), len(metadata_new)) > 2 * 1024**2 or len(registry['datasets']) > 4096:
            raise KitError('登记清单容量达到上限')
        receipt = {'schema': 'researchkit.web-data-publish-receipt/v1', 'revision': candidate['revision'],
                   'dataset_id': identity, 'metadata': info, 'manifest_sha256': checksum}
        # Receipt and description precede the registry commit. A crash before
        # the final replace cannot expose an incomplete dataset to projects.
        _atomic(receipts / (candidate['candidate_id'] + '-' + identity + '.json'), canonical(receipt) + b'\n')
        _backup_and_update(metadata_path, metadata_raw, metadata_new)
        if _read(registry_path) != registry_raw:
            raise KitError('登记清单在发布期间变化，请重新扫描')
        _backup_and_update(registry_path, registry_raw, registry_new)
        return {'dataset_id': identity, 'published_metadata': info,
                'file_count': len(files), 'total_size': entry['total_size']}
    finally:
        os.close(lock)


def _backup_and_update(path, expected, raw):
    if _read(path) != expected:
        raise KitError('正式说明或登记在发布期间变化，请重新审核')
    backup = path.parent / 'data-management-backups'
    backup.mkdir(mode=0o700, exist_ok=True)
    fd = DatasetRegistry._open_directory(backup); os.close(fd)
    backup_fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=backup)
    with os.fdopen(backup_fd, 'wb') as stream:
        stream.write(expected); stream.flush(); os.fsync(stream.fileno())
    _atomic(path, raw)


def _verify_source(config, candidate):
    # Imported lazily so --help remains available before any configured roots
    # are read. The discovery function reads Wiki/directory metadata only.
    if config.get('discovery_mode') == 'readme':
        from .web_dataset_discovery import _readme_metadata
        from .web_dataset_readme import marker_at
        marker = marker_at(Path(config['raw_root']), Path(candidate['relative_root']))
        if _readme_metadata(candidate['relative_root'], marker) != candidate.get('original_metadata'):
            raise KitError('README 说明在审核后变化，请重新扫描审核')
        return
    from .web_dataset_discovery import source_units
    units = source_units(config)
    current = next((item for item in units if item['candidate_id'] == candidate['candidate_id']), None)
    if current is None or current['metadata'] != candidate.get('original_metadata'):
        raise KitError('数据边界或 Wiki 说明在审核后变化，请重新扫描审核')


def _verify_fingerprint(snapshot, expected):
    from .web_dataset_discovery import snapshot_fingerprint
    if snapshot_fingerprint(snapshot) != expected:
        raise KitError('数据目录在审核后变化，请重新扫描审核')


def sync_readme_metadata(policy, config, items):
    """Synchronize README description, tags and evidence; never change refs or readers."""
    if config.get('discovery_mode') != 'readme':
        return items
    path = policy.dataset_metadata
    if path is None:
        return items
    lock = os.open(path.parent / '.data-publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _safe_info(os.fstat(lock), regular=True, hardlink=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = _read(path)
        enrich_catalog([], path)
        details = json.loads(before, object_pairs_hook=_json_no_duplicates)
        changed = False
        for item in items:
            identity = item.get('registered_dataset_id')
            if not identity or not item.get('readme_managed') or item.get('kind') in {'missing', 'error'}:
                continue
            source = _metadata(item['original_metadata'])
            current = dict(details['datasets'].get(identity, {}))
            current.update(description=source.get('description', ''), provenance_ref=source.get('provenance_ref', ''),
                           tags=source.get('tags', []))
            if current != details['datasets'].get(identity):
                details['datasets'][identity] = current
                changed = True
            item['current_metadata'] = current
        if changed:
            _backup_and_update(path, before, canonical(details) + b'\n')
        return items
    finally:
        os.close(lock)


def serve(policy_path, *, check=False, lock_path=Path('/run/researchkit-data-manager/publisher.lock')):
    if os.geteuid() != 0:
        raise KitError('数据发布服务必须由管理员安装的独立服务启动')
    policy = load_policy(policy_path, production=True)
    from .web_data_management import load_management_config
    config = load_management_config(policy.dataset_management_config)
    if policy.dataset_tree_registry is None or policy.dataset_metadata is None:
        raise KitError('数据管理登记文件尚未配置')
    TreeRegistry(policy.dataset_tree_registry)._load()
    enrich_catalog([], policy.dataset_metadata)
    fd = DatasetRegistry._open_directory(Path(config['raw_root']), allow_receiving=config.get('prepare_permissions', False)); os.close(fd)
    if check:
        return {'ready': True, 'schema': 'researchkit.web-data-publisher-check/v1'}
    descriptor = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    _safe_info(os.fstat(descriptor), regular=True, hardlink=True)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    client = BrokerClient(policy.broker_socket, policy.broker_uid)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    client.request('publisher_start', instance_id=str(uuid.uuid4()))
    def heartbeat():
        while not stopping.wait(5):
            try:
                client.request('publisher_heartbeat')
            except KitError:
                pass
    monitor = threading.Thread(target=heartbeat, daemon=True); monitor.start()
    next_scan = 0.0
    try:
        while not stopping.is_set():
            try:
                if time.monotonic() >= next_scan:
                    client.request('publisher_request_scan')
                    next_scan = time.monotonic() + config['scan_interval_seconds']
                cleanup = client.request('publisher_cleanup_claim')
                if cleanup is not None:
                    result, error = None, ''
                    try:
                        from .web_cleanup_raw import purge_raw
                        result = purge_raw(policy, config, cleanup['request'])
                    except Exception as exc:
                        error = '原始数据永久清理未完成：' + _safe_message(exc)
                    client.request('publisher_cleanup_finish', item_id=cleanup['item_id'],
                                   lease_token=cleanup['lease_token'], result=result, error=error)
                    continue
                job = client.request('publisher_claim')
                if job is None:
                    stopping.wait(3); continue
                result, error = None, ''
                try:
                    if job['kind'] == 'scan':
                        from .web_dataset_discovery import discover
                        def cache_readme(relative, provenance_ref, original):
                            candidate_id = hashlib.sha256(str(config['raw_root'] / relative).encode()).hexdigest()[:24]
                            client.request('publisher_readme', job_id=job['job_id'], lease_token=job['lease_token'],
                                           candidate_id=candidate_id, provenance_ref=provenance_ref, original=original)
                        result = {'items': sync_readme_metadata(policy, config, discover(config, policy, on_readme=cache_readme))}
                    elif job['kind'] == 'publish':
                        result = publish(policy, config, job['candidate'])
                    else:
                        raise KitError('发布服务任务类型无效')
                except Exception as exc:
                    # Invalid Wiki input is a failed task, not a restart loop.
                    # No user-controlled code is evaluated in this process.
                    error = '数据发现或登记未完成：' + _safe_message(exc)
                client.request('publisher_finish', job_id=job['job_id'], lease_token=job['lease_token'],
                               result=result, error=error)
            except KitError as exc:
                print(_safe_message(exc), file=sys.stderr, flush=True)
                stopping.wait(3)
    finally:
        stopping.set(); monitor.join(timeout=6); os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=Path('/etc/researchkit-web/policy.json'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = serve(args.policy, check=args.check)
    except (OSError, ValueError, KeyError, KitError) as exc:
        parser.exit(1, _safe_message(exc) + '\n')
    if report is not None:
        print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
