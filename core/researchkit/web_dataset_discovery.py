"""Trusted, metadata-only discovery of shared dataset directory units.

This module deliberately lives in the installed service package rather than
calling an operator's ``tools/`` script.  It reads only trusted configuration,
Wiki metadata, directory entries and stat information; it never opens a data
file or hashes a payload.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import yaml

from .files import KitError
from .web_dataset_metadata import _FIELDS, enrich_catalog
from .web_dataset_trees import TreeRegistry, tree_metadata, TREE_SCHEMA_README
from .web_dataset_readme import scan as scan_readmes, marker_at, open_relative, read_marker
from .web_datasets import DatasetRegistry

CONFIG_SCHEMA = 'researchkit.web-data-management/v1'
DRAFT_SCHEMA = 'researchkit.web-dataset-chinese-drafts/v1'
MAX_WIKI_BYTES = 16 * 1024 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def light_fingerprint(snapshot: dict[str, object]) -> dict[str, object]:
    """A bounded, content-free identity for a directory snapshot.

    ``entries_digest`` hashes only paths, kinds and stat tuples.  It is shared
    with the publisher so approval cannot be applied to a directory whose
    membership or metadata changed after review.
    """
    raw_entries = snapshot.get('entries')
    if type(raw_entries) is not list:
        raise KitError('数据集快照条目无效')
    entries = []
    for entry in raw_entries:
        if type(entry) is not dict or not {'path', 'kind', 'stat'} <= set(entry):
            raise KitError('数据集快照条目无效')
        # Publication manifests add sha256 for files.  It is deliberately not
        # part of the lightweight approval identity.
        entries.append({'path': entry['path'], 'kind': entry['kind'], 'stat': entry['stat']})
    normalized = {'root_stat': snapshot.get('root_stat'), 'entries': entries}
    return {'root_stat': snapshot.get('root_stat'), 'entries_digest': _digest(normalized)}


# Public publisher-facing name.  It accepts both a full publication manifest
# (with per-file sha256 values) and a scan snapshot; hashes are intentionally
# ignored so the two representations compare exactly.
snapshot_fingerprint = light_fingerprint


def snapshot_difference(previous: dict[str, object], current: dict[str, object]) -> str:
    """Summarize structural/stat differences without opening dataset files."""
    labels = ('设备', 'inode', '大小', '修改时间', '状态变更时间')

    def fields(before: object, after: object) -> list[str]:
        if not isinstance(before, (list, tuple)) or not isinstance(after, (list, tuple)) or len(before) != 5 or len(after) != 5:
            return ['属性格式'] if before != after else []
        return [label for label, old, new in zip(labels, before, after) if old != new]

    old_entries, new_entries = previous['entries'], current['entries']
    added = removed = changed = 0
    samples = []

    def sample(label: str, path: str, differences: list[str] | None = None) -> None:
        if len(samples) >= 10:
            return
        # Paths can contain newlines; keep diagnostics bounded and unambiguous.
        display = json.dumps(path[:120], ensure_ascii=False)
        if len(path) > 120 or len(display) > 120:
            display = display[:117] + '...'
        samples.append(f'{label} {display}' + (f'（{"、".join(differences)}）' if differences else ''))

    old_index = new_index = 0
    # Manifests produced at registration and current snapshots both use path
    # order. Merge them directly instead of constructing dictionaries or a
    # list of every changed entry for potentially very large datasets.
    while old_index < len(old_entries) or new_index < len(new_entries):
        old = old_entries[old_index] if old_index < len(old_entries) else None
        new = new_entries[new_index] if new_index < len(new_entries) else None
        if old is None or (new is not None and new['path'] < old['path']):
            added += 1
            sample('新增', new['path'])
            new_index += 1
        elif new is None or old['path'] < new['path']:
            removed += 1
            sample('移除', old['path'])
            old_index += 1
        else:
            differences = fields(old['stat'], new['stat'])
            if old['kind'] != new['kind']:
                differences.insert(0, '类型')
            if differences:
                changed += 1
                sample('属性变化', old['path'], differences)
            old_index += 1
            new_index += 1
    root = fields(previous.get('root_stat'), current.get('root_stat'))
    parts = ['仅比较目录结构和文件属性，未比较文件内容；属性变化不一定代表内容改变。']
    if root:
        parts.append('根目录属性变化：' + '、'.join(root))
    parts.append(f'新增 {added} 项，移除 {removed} 项，属性变化 {changed} 项。')
    parts.extend(samples)
    if not root and not added and not removed and not changed:
        parts.append('条目顺序或快照格式不同，请进一步检查。')
    return '；'.join(parts)


def _trusted_json(path: Path, *, label: str, maximum: int = 2 * 1024 * 1024) -> object:
    try:
        raw = DatasetRegistry(path)._read_bytes()
    except KitError:
        raise
    if len(raw) > maximum:
        raise KitError(f'{label}超过大小上限')
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError(f'{label}不是有效 JSON') from exc


def _trusted_yaml(path: Path, *, label: str) -> object:
    raw = _wiki_bytes(path, label=label)
    try:
        return yaml.safe_load(raw.decode('utf-8'))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise KitError(f'{label}不是有效 YAML') from exc


def _stable(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _wiki_bytes(path: Path, *, label: str) -> bytes:
    """Read a fixed, non-executable Wiki input without trusting its owner.

    Wiki is maintained by ``ly`` rather than root.  Its content is only a
    review hint, never authorization.  The caller supplies only package-fixed
    config paths, and this reader rejects links/races/oversize files.
    """
    if not path.is_absolute() or '..' in path.parts:
        raise KitError(f'{label}路径无效')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        # Keep each parent descriptor anchored. A Wiki maintainer can replace
        # directories, so lstat followed by opening an absolute path would not
        # suffice to keep a privileged reader inside this metadata route.
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_WIKI_BYTES:
            raise KitError(f'{label}不是受限普通文件')
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stable(before) != _stable(opened):
            raise KitError(f'{label}读取时变化')
        chunks = []; remaining = MAX_WIKI_BYTES + 1
        while remaining:
            block = os.read(fd, min(64 * 1024, remaining))
            if not block: break
            chunks.append(block); remaining -= len(block)
        raw = b''.join(chunks)
        if len(raw) > MAX_WIKI_BYTES or _stable(opened) != _stable(os.fstat(fd)):
            raise KitError(f'{label}读取时变化或超过上限')
        return raw
    except OSError as exc:
        raise KitError(f'{label}路径不可读取或包含链接') from exc
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _path(value: object, field: str, *, required: bool = True) -> Path | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise KitError(f'{field}必须是绝对路径')
    result = Path(value)
    if not result.is_absolute() or '..' in result.parts:
        raise KitError(f'{field}必须是不含 .. 的绝对路径')
    return result


def load_management_config(path: Path | None) -> dict[str, object] | None:
    """Load the root-owned configuration accepted by the publisher service."""
    if path is None:
        return None
    document = _trusted_json(Path(path), label='数据管理配置')
    allowed = {'schema', 'raw_root', 'wiki_root', 'chinese_drafts',
               'scan_interval_seconds', 'seconds_per_dataset', 'discovery_mode', 'prepare_permissions'}
    if type(document) is not dict or set(document) - allowed or document.get('schema') != CONFIG_SCHEMA:
        raise KitError('数据管理配置格式无效')
    raw_root = _path(document.get('raw_root'), 'raw_root')
    wiki_root = _path(document.get('wiki_root'), 'wiki_root', required=False)
    drafts = _path(document.get('chinese_drafts'), 'chinese_drafts', required=False)
    mode = document.get('discovery_mode', 'wiki')
    prepare_permissions = document.get('prepare_permissions', False)
    if not isinstance(mode, str) or mode not in {'wiki', 'readme'} or type(prepare_permissions) is not bool or (prepare_permissions and mode != 'readme'):
        raise KitError('数据集发现模式或权限准备配置无效')
    scan_interval = document.get('scan_interval_seconds', 3600)
    seconds = document.get('seconds_per_dataset', 3600)
    if (type(scan_interval) is not int or isinstance(scan_interval, bool) or
            not 60 <= scan_interval <= 86400):
        raise KitError('scan_interval_seconds必须为60至86400之间的整数')
    if type(seconds) is not int or isinstance(seconds, bool) or not 1 <= seconds <= 86400:
        raise KitError('seconds_per_dataset必须为1至86400之间的整数')
    return {'schema': CONFIG_SCHEMA, 'raw_root': raw_root, 'wiki_root': wiki_root,
            'chinese_drafts': drafts, 'scan_interval_seconds': scan_interval,
            'seconds_per_dataset': seconds, 'discovery_mode': mode, 'prepare_permissions': prepare_permissions}


def _metadata(record: dict[str, object], entity: str) -> dict[str, str]:
    result: dict[str, str] = {'wiki_ref': entity, 'wiki_fact_state': str(record.get('fact_state', 'unconfirmed'))}
    for field in ('name', 'description', 'format', 'usage', 'license', 'student_conditions'):
        value = record.get(field)
        if isinstance(value, str):
            result[field] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[field] = '; '.join(value)
    claims = record.get('claims', [])
    refs = sorted({ref for claim in claims if isinstance(claim, dict)
                   for ref in claim.get('provenance_refs', []) if isinstance(ref, str)}) if isinstance(claims, list) else []
    if refs:
        result['provenance_ref'] = ', '.join(refs)
    return result


def _safe_subset(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise KitError('Wiki子集路径无效')
    result = Path(value)
    if result.is_absolute() or '..' in result.parts:
        raise KitError('Wiki子集路径无效')
    return result


def _findings(path: Path, raw_root: Path, approved: set[Path], boundaries: list[Path]) -> dict[Path, str]:
    try:
        document = json.loads(_wiki_bytes(path, label='Wiki发现记录'))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError('Wiki发现记录不是有效 JSON') from exc
    units = document.get('units') if isinstance(document, dict) else None
    if type(units) is not list or len(units) > 4096:
        raise KitError('Wiki发现记录格式无效')
    result: dict[Path, str] = {}
    for item in units:
        if type(item) is not dict:
            raise KitError('Wiki发现条目无效')
        source, card = item.get('unit_path'), item.get('card')
        if source is None and card is None:
            continue
        if not isinstance(source, str) or not isinstance(card, str) or not card:
            raise KitError('Wiki发现条目无效')
        unit = Path(source)
        if not unit.is_absolute() or '..' in unit.parts:
            raise KitError('Wiki发现路径无效')
        try:
            unit.relative_to(raw_root)
        except ValueError:
            continue
        if any(unit == root or root in unit.parents for root in boundaries) and unit not in approved:
            raise KitError('Wiki发现路径不是人工确定的数据单元')
        before = result.setdefault(unit, card)
        if before != card:
            raise KitError('同一数据单元有冲突的Wiki卡片')
    return result


def _wiki_units(config: dict[str, object]) -> list[tuple[Path, str, dict[str, object], str | None]]:
    """Mirror the deliberately conservative boundary semantics of prepare tool."""
    raw_root = config['raw_root']; wiki_root = config['wiki_root']
    assert isinstance(raw_root, Path)
    if wiki_root is None:
        return []
    assert isinstance(wiki_root, Path)
    sources = wiki_root / 'sources'
    catalog_dir = wiki_root / 'catalog'
    boundaries = _trusted_yaml(sources / 'dataset-boundaries.yaml', label='Wiki数据边界')
    catalog = _trusted_yaml(catalog_dir / 'datasets.yaml', label='Wiki数据集目录')
    subsets = _trusted_yaml(catalog_dir / 'dataset-subsets.yaml', label='Wiki子集目录')
    if type(boundaries) is not dict or boundaries.get('adjudicated_by') != 'user' or boundaries.get('source_root') != str(raw_root):
        raise KitError('Wiki数据边界不是针对当前根目录的人工确认记录')
    records = {item.get('id'): item for item in (catalog.get('records', []) if isinstance(catalog, dict) else [])
               if type(item) is dict and isinstance(item.get('id'), str)}
    subset_records = [item for item in (subsets.get('records', []) if isinstance(subsets, dict) else []) if type(item) is dict]
    units: list[tuple[Path, str, dict[str, object], str | None]] = []
    boundary_paths: list[Path] = []
    for entry in boundaries.get('roots', []):
        if type(entry) is not dict:
            raise KitError('Wiki数据边界条目无效')
        path = Path(entry.get('path', ''))
        try:
            path.relative_to(raw_root)
        except (ValueError, TypeError) as exc:
            raise KitError('Wiki数据边界路径无效') from exc
        model, entity = entry.get('model'), entry.get('card')
        if not isinstance(entity, str) or model not in {'single-dataset', 'aggregating-directory', 'one-dataset-with-subsets'}:
            raise KitError('Wiki数据边界模型无效')
        boundary_paths.append(path)
        if model == 'single-dataset':
            units.append((path, entity, records.get(entity, {}), None))
        else:
            for child in entry.get('subsets', []):
                units.append((path / _safe_subset(child), entity, {}, model))
    if len(units) > 4096:
        raise KitError('Wiki数据单元过多')
    findings_path = wiki_root / 'reports' / 'ingestion-wave-5-findings.json'
    # Older Wiki exports may not yet contain the optional ingestion report.  In
    # that case the same conservative unique-name fallback below remains in
    # force; a present report is always validated exactly.
    findings = (_findings(findings_path, raw_root, {item[0] for item in units}, boundary_paths)
                if findings_path.exists() else {})
    result = []
    for path, parent, record, model in units:
        if model:
            pool = subset_records if model == 'one-dataset-with-subsets' else list(records.values())
            exact = findings.get(path)
            if exact is not None:
                matched = [item for item in pool if item.get('id') == exact]
                if len(matched) != 1 or matched[0].get('name') != path.name:
                    raise KitError('Wiki发现卡片与数据单元不一致')
                linked = matched[0]
                if model == 'one-dataset-with-subsets' and not any(
                    claim.get('predicate') == 'subset-of' and claim.get('object_id') == parent and claim.get('fact_state') == 'verified'
                    for claim in linked.get('claims', []) if isinstance(claim, dict)):
                    raise KitError('Wiki发现卡片不是已验证子集')
            else:
                matched = [item for item in pool if item.get('name') == path.name and item.get('id') != parent and
                           (model != 'one-dataset-with-subsets' or any(
                               claim.get('predicate') == 'subset-of' and claim.get('object_id') == parent and claim.get('fact_state') == 'verified'
                               for claim in item.get('claims', []) if isinstance(claim, dict)))]
                linked = matched[0] if len(matched) == 1 else {
                    'id': parent, 'name': path.name, 'fact_state': 'parent-card-only',
                    'description': '该单元边界已由使用者确认，但没有唯一对应的独立说明；具体用途与使用条件仍需审核。',
                }
            record = linked; parent = str(linked.get('id', parent))
        result.append((path, parent, record, model))
    return result


def _registered(policy: object) -> tuple[dict[str, tuple[str, dict[str, object], object]], dict[str, dict[str, str]]]:
    trees = TreeRegistry(getattr(policy, 'dataset_tree_registry', None))._load()
    roots: dict[str, tuple[str, dict[str, object], object]] = {}
    for dataset_id, item in trees.items():
        document = TreeRegistry._manifest(item)
        roots[str(Path(document['root']))] = (dataset_id, document, item['readers'])
    metadata_path = getattr(policy, 'dataset_metadata', None)
    metadata: dict[str, dict[str, str]] = {}
    if metadata_path is not None:
        # Reuse the canonical validator, then turn its small catalog into a map.
        for item in enrich_catalog([{'dataset_id': dataset_id} for dataset_id in trees], metadata_path):
            metadata[str(item['dataset_id'])] = {key: value for key, value in item.items() if key in _FIELDS}
    return roots, metadata


def _manifest_snapshot(document: dict[str, object]) -> dict[str, object]:
    entries = document.get('entries', [])
    assert isinstance(entries, list)
    return {'root_stat': document.get('root_stat'), 'entries': [
        {key: entry[key] for key in ('path', 'kind', 'stat')} for entry in entries if isinstance(entry, dict)
    ]}


def _drafts(config: dict[str, object]) -> dict[str, dict[str, object]]:
    path = config.get('chinese_drafts')
    if path is None:
        return {}
    assert isinstance(path, Path)
    document = _trusted_json(path, label='中文说明草稿')
    if type(document) is not dict or set(document) != {'schema', 'drafts'} or document.get('schema') != DRAFT_SCHEMA:
        raise KitError('中文说明草稿格式无效')
    drafts = document['drafts']
    if type(drafts) is not dict or len(drafts) > 4096:
        raise KitError('中文说明草稿数量无效')
    result = {}
    for candidate_id, value in drafts.items():
        if not isinstance(candidate_id, str) or type(value) is not dict or set(value) != {'wiki_ref', 'source_sha256', 'metadata'}:
            raise KitError('中文说明草稿字段无效')
        fields = value['metadata']
        if not isinstance(value['wiki_ref'], str) or not isinstance(value['source_sha256'], str) or type(fields) is not dict or set(fields) - _FIELDS:
            raise KitError('中文说明草稿字段无效')
        if any(not isinstance(text, str) or len(text.encode()) > 16384 for text in fields.values()):
            raise KitError('中文说明草稿文本无效')
        result[candidate_id] = value
    return result


def source_units(config: dict[str, object]) -> list[dict[str, object]]:
    """Discover only unit boundaries and Wiki source metadata.

    This is the cheap publisher precondition check: it does not read registry
    manifests, recurse into a dataset, or inspect payload files.
    """
    raw_root = config['raw_root']; assert isinstance(raw_root, Path)
    if config.get('discovery_mode', 'wiki') == 'readme':
        return _readme_sources(config)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise KitError('原始数据根目录不可读取')
    wiki_units = _wiki_units(config)
    known = {path for path, _entity, _record, _model in wiki_units}
    units = list(wiki_units)
    with os.scandir(raw_root) as entries:
        for entry in sorted(entries, key=lambda value: value.name):
            if not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if path in known or any(path in item.parents for item in known):
                continue
            units.append((path, '', {}, None))
    if len(units) > 4096:
        raise KitError('数据集候选过多')
    result = []
    for path, entity, record, _model in units:
        relative = path.relative_to(raw_root).as_posix()
        candidate_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
        metadata = _metadata(record, entity) if entity else {'name': path.name, 'wiki_fact_state': 'unconfirmed'}
        metadata['name'] = path.name if path.name != record.get('name') else str(record.get('name', path.name))
        result.append({'candidate_id': candidate_id, 'relative_root': relative, 'root': str(path),
                       'metadata': metadata, 'metadata_digest': _digest(metadata)})
    return result


def discover(config: dict[str, object], policy: object, *, on_readme=None) -> list[dict[str, object]]:
    """Return all candidate units; callers decide how to persist their state."""
    raw_root = config['raw_root']; assert isinstance(raw_root, Path)
    if config.get('discovery_mode', 'wiki') == 'readme':
        return _discover_readmes(config, policy, on_readme=on_readme)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise KitError('原始数据根目录不可读取')
    wiki_units = _wiki_units(config)
    known = {path for path, _entity, _record, _model in wiki_units}
    # First-level discovery intentionally skips an aggregator which contains an
    # approved child unit: train/class directories never become datasets merely
    # because their parent is present in the raw root.
    units = list(wiki_units)
    with os.scandir(raw_root) as entries:
        for entry in sorted(entries, key=lambda value: value.name):
            if not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if path in known or any(path in item.parents for item in known):
                continue
            units.append((path, '', {}, None))
    if len(units) > 4096:
        raise KitError('数据集候选过多')
    registered, published_metadata = _registered(policy)
    drafts = _drafts(config)
    result: list[dict[str, object]] = []
    for path, entity, record, _model in units:
        relative = path.relative_to(raw_root).as_posix()
        candidate_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
        original = _metadata(record, entity) if entity else {'name': path.name, 'wiki_fact_state': 'unconfirmed'}
        original['name'] = path.name if path.name != record.get('name') else str(record.get('name', path.name))
        metadata_digest = _digest(original)
        item: dict[str, object] = {'candidate_id': candidate_id, 'relative_root': relative, 'root': str(path),
                                   'original_metadata': original, 'metadata_digest': metadata_digest,
                                   'source_metadata_changed': False}
        seed = drafts.get(candidate_id)
        if seed is not None and seed['wiki_ref'] == original.get('wiki_ref') and seed['source_sha256'] == metadata_digest:
            item['draft_metadata'] = seed['metadata']; item['seed_state'] = 'matched'
        else:
            item['draft_metadata'] = {}; item['seed_state'] = 'stale' if seed is not None else 'missing'
        registered_item = registered.get(str(path))
        try:
            fd = DatasetRegistry._open_directory(path)
            try:
                snapshot = tree_metadata(fd, hash_files=False, seconds=int(config['seconds_per_dataset']))
            finally:
                os.close(fd)
            fingerprint = snapshot_fingerprint(snapshot)
            item['source_fingerprint'] = fingerprint
            if registered_item is None:
                item['kind'] = 'new'
            else:
                dataset_id, manifest, readers = registered_item
                item['registered_dataset_id'] = dataset_id
                item['registered_readers'] = readers
                item['current_metadata'] = published_metadata.get(dataset_id, {})
                manifest_snapshot = _manifest_snapshot(manifest)
                if fingerprint != snapshot_fingerprint(manifest_snapshot):
                    item['kind'] = 'data-change'
                    item['error'] = snapshot_difference(manifest_snapshot, snapshot)
                else:
                    item['kind'] = 'unchanged'
        except (OSError, KitError) as exc:
            item['kind'] = 'missing' if not path.exists() else 'error'
            item['source_fingerprint'] = None
            item['error'] = str(exc)[:1024]
            if registered_item is not None:
                item['registered_dataset_id'] = registered_item[0]
                item['registered_readers'] = registered_item[2]
                item['current_metadata'] = published_metadata.get(registered_item[0], {})
        result.append(item)
    return result


def discover_one(config: dict[str, object], policy: object, relative_root: str) -> dict[str, object]:
    """Rediscover one reviewed unit without iterating unrelated raw datasets."""
    raw_root = config['raw_root']; assert isinstance(raw_root, Path)
    if config.get('discovery_mode', 'wiki') == 'readme':
        relative = Path(relative_root)
        marker = marker_at(raw_root, relative)
        registered, published = _registered(policy)
        return _readme_item(config, relative_root, marker, registered, published)
    relative = Path(relative_root)
    if not relative_root or relative.is_absolute() or '..' in relative.parts:
        raise KitError('数据集相对路径无效')
    path = raw_root / relative
    units = {unit[0]: unit for unit in _wiki_units(config)}
    unit = units.get(path)
    if unit is None:
        # A first-level unindexed directory is a valid new candidate.  Deeper
        # paths need an explicit human Wiki boundary and are never guessed.
        if len(relative.parts) != 1:
            raise KitError('数据集路径不在人工确认的边界中')
        unit = (path, '', {}, None)
    registered, published = _registered(policy)
    candidate_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    _path, entity, record, _model = unit
    original = _metadata(record, entity) if entity else {'name': path.name, 'wiki_fact_state': 'unconfirmed'}
    original['name'] = path.name if path.name != record.get('name') else str(record.get('name', path.name))
    item: dict[str, object] = {'candidate_id': candidate_id, 'relative_root': relative.as_posix(), 'root': str(path),
                               'original_metadata': original, 'metadata_digest': _digest(original)}
    registered_item = registered.get(str(path))
    try:
        fd = DatasetRegistry._open_directory(path)
        try:
            snapshot = tree_metadata(fd, hash_files=False, seconds=int(config['seconds_per_dataset']))
        finally:
            os.close(fd)
        item['source_fingerprint'] = snapshot_fingerprint(snapshot)
        if registered_item is None:
            item['kind'] = 'new'
        else:
            dataset_id, manifest, readers = registered_item
            item['registered_dataset_id'] = dataset_id
            item['registered_readers'] = readers
            item['current_metadata'] = published.get(dataset_id, {})
            manifest_snapshot = _manifest_snapshot(manifest)
            item['kind'] = 'data-change' if item['source_fingerprint'] != snapshot_fingerprint(manifest_snapshot) else 'unchanged'
            if item['kind'] == 'data-change':
                item['error'] = snapshot_difference(manifest_snapshot, snapshot)
    except (OSError, KitError) as exc:
        item['kind'] = 'missing' if not path.exists() else 'error'
        item['source_fingerprint'] = None; item['error'] = str(exc)[:1024]
    return item


def _readme_metadata(relative: str, marker: dict[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {'name': Path(relative).name, 'wiki_fact_state': 'unconfirmed'}
    for key in ('description', 'provenance_ref'):
        if isinstance(marker.get(key), str):
            metadata[key] = marker[key]
    if isinstance(marker.get('tags'), list) and all(isinstance(tag, str) for tag in marker['tags']):
        metadata['tags'] = list(marker['tags'])
    return metadata


def _readme_sources(config: dict[str, object]) -> list[dict[str, object]]:
    root = config['raw_root']; assert isinstance(root, Path)
    found = scan_readmes(root, seconds=int(config['seconds_per_dataset']))
    result = []
    for relative, marker in sorted(found.items()):
        path = root / relative
        metadata = _readme_metadata(relative, marker)
        item = {'candidate_id': hashlib.sha256(str(path).encode()).hexdigest()[:24],
                'relative_root': relative, 'root': str(path), 'metadata': metadata,
                'metadata_digest': _digest(metadata), 'readme_managed': True}
        if 'error' in marker:
            item['error'] = marker['error']
        result.append(item)
    return result


def _readme_item(config, relative, marker, registered, published):
    root = config['raw_root']; path = root / relative
    existing = registered.get(str(path))
    managed = bool(marker.get('provenance_ref')) and (existing is None or existing[1]['schema'] == TREE_SCHEMA_README)
    original = _readme_metadata(relative, marker) if managed or existing is None else dict(published.get(existing[0], {}))
    original.setdefault('name', path.name)
    item = {'candidate_id': hashlib.sha256(str(path).encode()).hexdigest()[:24],
            'relative_root': relative, 'root': str(path), 'original_metadata': original,
            'metadata_digest': _digest(original), 'source_metadata_changed': False,
            'draft_metadata': {'description': original.get('description', '')},
            'seed_state': 'readme' if managed else 'missing', 'readme_managed': managed}
    if existing:
        ident, manifest, readers = existing
        item.update(registered_dataset_id=ident, registered_readers=readers,
                    current_metadata=published.get(ident, {}))
    try:
        if 'error' in marker:
            raise KitError(str(marker['error']))
        if existing is None and any(path in Path(other).parents or Path(other) in path.parents for other in registered):
            raise KitError('数据集目录与已登记数据集存在包含关系')
        readme_mode = not existing or existing[1]['schema'] == TREE_SCHEMA_README
        fd = open_relative(root, Path(relative)) if not existing and config.get('prepare_permissions', False) else DatasetRegistry._open_directory(path, allow_receiving=True)
        try:
            snapshot = tree_metadata(fd, hash_files=False, seconds=int(config['seconds_per_dataset']),
                                     readme_mode=readme_mode,
                                     trusted=not (not existing and config.get('prepare_permissions', False)))
        finally:
            os.close(fd)
        fingerprint = snapshot_fingerprint(snapshot)
        item['source_fingerprint'] = fingerprint
        if existing is None:
            item['kind'] = 'new'
        else:
            previous = _manifest_snapshot(existing[1])
            item['kind'] = 'unchanged' if fingerprint == snapshot_fingerprint(previous) else 'data-change'
            if item['kind'] == 'data-change':
                item['error'] = snapshot_difference(previous, snapshot)
    except (OSError, KitError) as exc:
        item['kind'] = 'missing' if not path.exists() else 'error'
        item['source_fingerprint'] = None
        item['error'] = str(exc)[:1024]
    return item


def _discover_readmes(config, policy, *, on_readme=None):
    root = config['raw_root']; registered, published = _registered(policy)
    markers = scan_readmes(root, seconds=int(config['seconds_per_dataset']), on_readme=on_readme)
    # Keep existing registrations visible even when their marker is removed or
    # a newly marked parent changes the discovery boundaries.
    for registered_root in registered:
        try:
            relative = Path(registered_root).relative_to(root).as_posix()
        except ValueError:
            continue
        if relative != '.' and relative not in markers:
            try:
                fd = open_relative(root, Path(relative))
                try:
                    marker = read_marker(fd, include_body=on_readme is not None)
                finally:
                    os.close(fd)
            except (OSError, KitError) as exc:
                markers[relative] = {'error': str(exc)}
                continue
            # Failure to cache a complete README must fail the scan, rather
            # than being mistaken for an unreadable registered directory.
            if marker is not None and on_readme is not None:
                on_readme(relative, marker['provenance_ref'], marker.pop('original'))
            markers[relative] = marker or {}
    return [_readme_item(config, relative, marker, registered, published)
            for relative, marker in sorted(markers.items())]
