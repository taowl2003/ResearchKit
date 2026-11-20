#!/usr/bin/env python3
"""Batch discovery produces review candidates, never activates datasets/services."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

SCHEMA = 'researchkit.web-dataset-candidates/v1'
METADATA_SCHEMA = 'researchkit.web-dataset-metadata/v1'
MAX_FINDINGS_BYTES = 16 * 1024 * 1024


def read_json(path, maximum=4 * 1024 * 1024):
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > maximum:
        raise ValueError('candidate file must be a bounded regular file')
    return json.loads(source.read_text())


def read_yaml(path):
    import yaml
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Wiki input must be a bounded regular metadata file')
    return yaml.safe_load(source.read_text())


def _safe_finding_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Wiki findings unit_path is invalid')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Wiki findings unit_path must be an absolute path without ..')
    return path


def read_findings(path, root, approved_paths, boundary_paths):
    """Return exact user-boundary paths mapped to their frozen Wiki card."""
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FINDINGS_BYTES:
        raise ValueError('Wiki findings must be a bounded regular JSON file')
    try:
        document = json.loads(source.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Wiki findings must be finite UTF-8 JSON') from exc
    units = document.get('units') if isinstance(document, dict) else None
    if not isinstance(units, list) or len(units) > 4096:
        raise ValueError('Wiki findings units are invalid or excessive')
    found = {}
    for item in units:
        if not isinstance(item, dict):
            raise ValueError('Wiki findings unit is invalid')
        path_value, card = item.get('unit_path'), item.get('card')
        if path_value is None and card is None:
            continue
        path = _safe_finding_path(path_value)
        if not isinstance(card, str) or not card:
            raise ValueError('Wiki findings card is invalid')
        try:
            path.relative_to(root)
        except ValueError:
            continue
        key = str(path)
        previous = found.get(key)
        if previous is not None and previous != card:
            raise ValueError('conflicting Wiki findings cards for one unit path')
        found[key] = card
    for path in found:
        item_path = Path(path)
        if any(item_path == boundary or boundary in item_path.parents for boundary in boundary_paths) and path not in approved_paths:
            raise ValueError('Wiki findings path is not a user-adjudicated boundary unit')
    return found


def metadata(record, entity):
    result = {'wiki_ref': entity, 'wiki_fact_state': str(record.get('fact_state', 'unconfirmed'))}
    for field in ('name', 'description', 'format', 'usage', 'license', 'student_conditions'):
        value = record.get(field)
        if isinstance(value, str):
            result[field] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[field] = '; '.join(value)
    refs = sorted({ref for claim in record.get('claims', []) if isinstance(claim, dict)
                   for ref in claim.get('provenance_refs', []) if isinstance(ref, str)})
    if refs:
        result['provenance_ref'] = ', '.join(refs)
    return result


def discover(root, boundaries=None, catalog=None, subsets=None, wiki_findings=None):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('source root must be an absolute path without ..')
    records = {}
    if catalog:
        doc = read_yaml(catalog)
        records = {item['id']: item for item in doc.get('records', []) if isinstance(item, dict) and isinstance(item.get('id'), str)}
    subset_records = []
    if subsets:
        subset_records = read_yaml(subsets).get('records', [])
    units = []
    if boundaries:
        doc = read_yaml(boundaries)
        if doc.get('adjudicated_by') != 'user' or doc.get('source_root') != str(root):
            raise ValueError('Wiki boundaries must be user-adjudicated for this exact source root')
        for entry in doc.get('roots', []):
            path = Path(entry['path']); path.relative_to(root)
            model = entry['model']; entity = entry['card']
            if model == 'single-dataset':
                units.append((path, entity, records.get(entity, {}), None))
            elif model in {'aggregating-directory', 'one-dataset-with-subsets'}:
                # Keep the human-approved unit boundaries. Never guess from names.
                for child in entry.get('subsets', []):
                    relative = Path(child)
                    if relative.is_absolute() or '..' in relative.parts or not child:
                        raise ValueError('unsafe Wiki subset path')
                    units.append((path / relative, entity, None, model))
            else:
                raise ValueError('unknown dataset boundary model')
    else:
        # Only one directory level; do not parse or hash any payload files.
        if root.is_symlink() or not root.is_dir():
            raise ValueError('source root must be a non-link directory')
        with os.scandir(root) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_dir(follow_symlinks=False):
                    units.append((Path(entry.path), '', {}, None))
    if len(units) > 4096:
        raise ValueError('too many discovery units')
    findings = {}
    if wiki_findings:
        approved_paths = {str(path) for path, _entity, _record, _model in units}
        boundary_paths = [Path(entry['path']) for entry in doc.get('roots', [])] if boundaries else []
        findings = read_findings(wiki_findings, root, approved_paths, boundary_paths)
    candidates = []
    seen = set()
    for path, entity, record, model in units:
        relative = path.relative_to(root).as_posix()
        if '..' in path.parts or str(path) in seen:
            raise ValueError('duplicate or unsafe candidate path')
        seen.add(str(path))
        if model:
            pool = subset_records if model == 'one-dataset-with-subsets' else list(records.values())
            exact_card = findings.get(str(path))
            if exact_card is not None:
                matches = [item for item in pool if item.get('id') == exact_card]
                if len(matches) != 1:
                    raise ValueError('Wiki findings card is absent from the applicable catalog')
                linked = matches[0]
                if linked.get('name') != path.name:
                    raise ValueError('Wiki findings card name does not match its unit path')
                if model == 'one-dataset-with-subsets' and not any(
                    claim.get('predicate') == 'subset-of' and claim.get('object_id') == entity
                    and claim.get('fact_state') == 'verified' for claim in linked.get('claims', []) if isinstance(claim, dict)):
                    raise ValueError('Wiki findings card is not a verified subset of this parent')
            else:
                matches = [item for item in pool if item.get('name') == path.name and item.get('id') != entity
                           and (model != 'one-dataset-with-subsets' or any(
                               claim.get('predicate') == 'subset-of' and claim.get('object_id') == entity
                               and claim.get('fact_state') == 'verified' for claim in item.get('claims', []) if isinstance(claim, dict)))]
                linked = matches[0] if len(matches) == 1 else {
                    'id': entity, 'name': path.name, 'fact_state': 'parent-card-only',
                    'description': '该单元边界已由使用者确认，但没有唯一对应的独立说明；知识库索引指向父数据集，具体用途与使用条件仍需审核。',
                }
            entity, record = linked.get('id', entity), linked
        digest = hashlib.sha256(str(path).encode()).hexdigest()
        description = metadata(record, entity) if entity else {'name': path.name, 'wiki_fact_state': 'unconfirmed'}
        # An approved subset keeps its parent Wiki identity and exact relative unit.
        description['name'] = path.name if path.name != record.get('name') else record.get('name', path.name)
        candidates.append({'candidate_id': digest[:24], 'dataset_id_hint': 'shared-' + digest[:16] + '-v1',
                           'relative_root': relative, 'root': str(path), 'status': 'pending-review',
                           'selected': False, 'metadata': description})
    return {'schema': SCHEMA, 'source_root': str(root), 'candidates': candidates}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--wiki-boundaries')
    parser.add_argument('--wiki-catalog')
    parser.add_argument('--wiki-subsets')
    parser.add_argument('--wiki-findings')
    args = parser.parse_args(argv)
    try:
        doc = discover(args.root, args.wiki_boundaries, args.wiki_catalog, args.wiki_subsets, args.wiki_findings)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
