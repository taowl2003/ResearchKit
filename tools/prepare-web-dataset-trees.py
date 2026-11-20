#!/usr/bin/env python3
"""Prepare a reviewed batch of complete dataset versions; do not install/restart."""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from researchkit.web_dataset_trees import prepare_tree, REGISTRY_SCHEMA
from researchkit.web_datasets import DatasetRegistry
from researchkit.files import KitError


def _load_candidates(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise KitError('候选文件不安全或过大')
    doc = json.loads(path.read_text())
    if type(doc) is not dict or doc.get('schema') != 'researchkit.web-dataset-candidates/v1':
        raise KitError('候选文件格式无效')
    return doc


def prepare_batch(candidate_path, output, *, selected=(), readers=(), registry=None, metadata=None,
                  manifest_root=Path('/etc/researchkit-web/dataset-manifests'), seconds=3600, all_platform_readers=False):
    spec = importlib.util.spec_from_file_location('prepare_dataset_single', ROOT / 'tools/prepare-web-dataset.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if all_platform_readers and readers:
        raise KitError('请选择全平台开放或指定账号，不能混用')
    principals = 'platform-users' if all_platform_readers else module._readers(list(readers))
    doc = _load_candidates(candidate_path)
    source_root = Path(doc['source_root'])
    if not source_root.is_absolute() or '..' in source_root.parts:
        raise KitError('候选来源根路径无效')
    candidates = doc.get('candidates')
    if type(candidates) is not list or len(candidates) > 4096:
        raise KitError('候选数量无效')
    requested = set(selected)
    chosen = []
    for candidate in candidates:
        if type(candidate) is not dict or type(candidate.get('selected')) is not bool:
            raise KitError('候选审核标记必须是布尔值')
        if candidate['selected'] or candidate.get('candidate_id') in requested:
            chosen.append(candidate)
    if requested - {item.get('candidate_id') for item in chosen} or not chosen:
        raise KitError('未选择候选或候选 ID 不存在')
    if not manifest_root.is_absolute() or '..' in manifest_root.parts:
        raise KitError('安装清单目录无效')
    old = {'schema': REGISTRY_SCHEMA, 'datasets': []}
    if registry is not None:
        old = json.loads(DatasetRegistry(registry)._read_bytes())
        from researchkit.web_dataset_trees import TreeRegistry
        TreeRegistry(registry)._load()
    details = {'schema': 'researchkit.web-dataset-metadata/v1', 'datasets': {}}
    if metadata is not None:
        details = json.loads(DatasetRegistry(metadata)._read_bytes())
        if details.get('schema') != 'researchkit.web-dataset-metadata/v1' or type(details.get('datasets')) is not dict:
            raise KitError('已有说明格式无效')
    if output.exists() or output.is_symlink():
        raise KitError('输出目录已存在，不会覆盖')
    staging = Path(tempfile.mkdtemp(prefix='.dataset-batch-', dir=output.parent))
    ids = {item['dataset_id'] for item in old['datasets']}
    try:
        manifests = staging / 'manifests'; manifests.mkdir(mode=0o755)
        summary = []
        for candidate in chosen:
            source = Path(candidate['root'])
            if not source.is_absolute() or '..' in source.parts or source == source_root:
                raise KitError('候选来源路径无效')
            source.relative_to(source_root)
            digest = hashlib.sha256(str(source).encode()).hexdigest()
            if candidate['candidate_id'] != digest[:24]:
                raise KitError('候选来源与 ID 不匹配')
            ident = candidate['dataset_id_hint']
            import re
            if not isinstance(ident, str) or len(ident) > 128 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', ident) or ident in ids:
                raise KitError('数据集版本 ID 无效或已存在，请使用新版本 ID')
            snapshot = prepare_tree(source, seconds=seconds)
            raw = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
            checksum = hashlib.sha256(raw).hexdigest()
            target = manifests / (ident + '.json'); target.write_bytes(raw); target.chmod(0o644)
            files = [entry for entry in snapshot['entries'] if entry['kind'] == 'file']
            total = sum(entry['stat'][2] for entry in files)
            old['datasets'].append({'dataset_id': ident, 'manifest': str(manifest_root / target.name),
                                    'sha256': checksum, 'file_count': len(files), 'total_size': total, 'readers': principals})
            ids.add(ident)
            record = candidate.get('metadata', {})
            if type(record) is not dict or any(not isinstance(key, str) or not isinstance(value, str) for key,value in record.items()):
                raise KitError('候选说明字段无效')
            details['datasets'][ident] = {key: value.encode()[:16000].decode('utf-8', errors='ignore') for key,value in record.items()}
            summary.append({'dataset_id': ident, 'file_count': len(files), 'total_size': total,
                            'candidate_id': candidate['candidate_id'], 'status': 'prepared-not-installed'})
        for name, value in [('dataset-trees.json', old), ('datasets.metadata.json', details), ('review.json', summary)]:
            target = staging / name; target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n'); target.chmod(0o644)
        staging.chmod(0o755)
        os.rename(staging, output)
        return summary
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--approve', action='append', default=[])
    access=parser.add_mutually_exclusive_group(required=True)
    access.add_argument('--reader', action='append', default=[])
    access.add_argument('--all-platform-readers', action='store_true')
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--manifest-root', type=Path, default=Path('/etc/researchkit-web/dataset-manifests'))
    parser.add_argument('--seconds-per-dataset', type=int, default=3600)
    args = parser.parse_args(argv)
    if args.seconds_per_dataset < 1 or args.seconds_per_dataset > 86400:
        parser.error('seconds-per-dataset must be 1..86400')
    try:
        result = prepare_batch(args.candidates, args.output, selected=args.approve, readers=args.reader,
                               registry=args.registry, metadata=args.metadata,
                               manifest_root=args.manifest_root, seconds=args.seconds_per_dataset, all_platform_readers=args.all_platform_readers)
    except (KitError, ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
