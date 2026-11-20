#!/usr/bin/env python3
"""Prepare reviewed Wiki description updates without reading dataset payloads.

Writes a new review file only; never installs metadata or changes a version.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'core'))
from researchkit.files import KitError
from researchkit.web_datasets import DatasetRegistry, _json_no_duplicates
from researchkit.web_dataset_trees import TreeRegistry
from researchkit.web_dataset_metadata import _FIELDS, enrich_catalog, normalize_tags


def refresh(candidates,registry,metadata,output):
    spec=importlib.util.spec_from_file_location('dataset_candidates',ROOT/'tools/review-web-datasets.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    doc=module.validate_candidates(module.read_candidates(candidates))
    source_root=Path(doc['source_root'])
    if not source_root.is_absolute() or '..' in source_root.parts or len(doc['candidates'])>4096:
        raise KitError('审核来源无效')
    selected={};seen=set()
    for item in doc['candidates']:
        if type(item) is not dict or type(item.get('selected')) is not bool:
            raise KitError('审核选择无效')
        path=Path(item['root'])
        if not path.is_absolute() or '..' in path.parts or path==source_root:
            raise KitError('审核路径无效')
        relative=path.relative_to(source_root).as_posix()
        identity=hashlib.sha256(str(path).encode()).hexdigest()[:24]
        if item.get('candidate_id')!=identity or item.get('relative_root')!=relative or identity in seen:
            raise KitError('审核路径或身份不一致')
        seen.add(identity)
        if item['selected']:selected[str(path)]=item['metadata']
    if not selected:raise KitError('未选择说明更新项')
    old=json.loads(DatasetRegistry(metadata)._read_bytes(),object_pairs_hook=_json_no_duplicates)
    enrich_catalog([],metadata)  # Validate allowed text fields, never ACLs.
    trees=TreeRegistry(registry);records=trees._load()
    updated=[];matched=set()
    # Match exact trusted registered roots; candidate ID hints may differ from
    # the immutable IDs that the administrator approved during registration.
    for identity,item in records.items():
        manifest=trees._manifest(item)
        path=manifest['root']
        if path in selected:
            old['datasets'][identity]=selected[path]
            matched.add(path);updated.append(identity)
    if set(selected)-matched:
        raise KitError('所选单元尚未登记：'+', '.join(sorted(set(selected)-matched)))
    raw=(json.dumps(old,ensure_ascii=False,indent=2)+'\n').encode()
    if len(raw)>2*1024*1024:raise KitError('说明清单超过 2 MiB')
    # Validate using the same field rules before writing any result.
    for info in old['datasets'].values():
        if type(info) is not dict or set(info)-_FIELDS:
            raise KitError('说明字段无效，不接受权限字段')
        for key, value in info.items():
            if key == 'tags':
                normalize_tags(value)
            elif not isinstance(value,str) or len(value.encode())>16384:
                raise KitError('说明字段无效，不接受权限字段')
    descriptor=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(raw)
    except BaseException:
        output.unlink()
        raise
    return {'schema':'researchkit.web-metadata-review/v1','updated_dataset_ids':updated,
            'output':str(output),'sha256':hashlib.sha256(raw).hexdigest(),'installed':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('candidates','registry','metadata','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args(argv)
    try:report=refresh(args.candidates,args.registry,args.metadata,args.output)
    except (OSError,ValueError,KeyError,TypeError,KitError) as error:parser.exit(1,str(error)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
