from __future__ import annotations
import hashlib
import importlib.util
import json
import tempfile
import shutil
from pathlib import Path
import pytest
import yaml
from researchkit.files import KitError
from researchkit import web_datasets


@pytest.fixture
def tmp_path():
    root=Path(tempfile.mkdtemp(prefix=".rk-data-batch-",dir=Path.cwd()))
    try: yield root
    finally: shutil.rmtree(root)


def load_tool(name):
    spec=importlib.util.spec_from_file_location(name,Path('tools')/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_batch_only_selected_units_and_preserves_earlier_versions(tmp_path,monkeypatch):
    monkeypatch.setattr(web_datasets,'_root_owned',lambda info:True)
    source=tmp_path/'raw';source.mkdir()
    for name in ('first','second'):
        (source/name).mkdir();(source/name/'value.csv').write_text(name)
    discovery=load_tool('prepare-web-datasets').discover(source)
    candidates=tmp_path/'candidates.json';candidates.write_text(json.dumps(discovery))
    tool=load_tool('prepare-web-dataset-trees')
    with pytest.raises(KitError,match='未选择'):
        tool.prepare_batch(candidates,tmp_path/'empty',all_platform_readers=True)
    # Only the reviewed first dataset is hashed/registered, second remains absent.
    discovery['candidates'][0]['selected']=True;candidates.write_text(json.dumps(discovery))
    first=tmp_path/'first-bundle'
    summary=tool.prepare_batch(candidates,first,all_platform_readers=True,manifest_root=tmp_path/'manifests')
    registry=json.loads((first/'dataset-trees.json').read_text())
    assert len(summary)==len(registry['datasets'])==1
    assert registry['datasets'][0]['readers']=='platform-users'
    assert (source/'first'/'value.csv').read_text()=='first'
    for item in discovery['candidates']: item['selected']=item['relative_root']=='second'
    candidates.write_text(json.dumps(discovery))
    second=tmp_path/'second-bundle'
    tool.prepare_batch(candidates,second,all_platform_readers=True,registry=first/'dataset-trees.json',
                       metadata=first/'datasets.metadata.json',manifest_root=tmp_path/'manifests')
    combined=json.loads((second/'dataset-trees.json').read_text())
    assert combined['datasets'][0]==registry['datasets'][0] and len(combined['datasets'])==2
    before=(second/'dataset-trees.json').read_bytes()
    with pytest.raises(KitError,match='输出目录已存在'):
        tool.prepare_batch(candidates,second,all_platform_readers=True)
    assert (second/'dataset-trees.json').read_bytes()==before


def test_batch_unknown_review_id_and_escaped_source_are_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(web_datasets,'_root_owned',lambda info:True)
    source=tmp_path/'raw';source.mkdir();(source/'unit').mkdir()
    doc=load_tool('prepare-web-datasets').discover(source)
    candidates=tmp_path/'candidates.json';candidates.write_text(json.dumps(doc))
    tool=load_tool('prepare-web-dataset-trees')
    with pytest.raises(KitError,match='ID 不存在'):
        tool.prepare_batch(candidates,tmp_path/'bad',selected=['unknown'],all_platform_readers=True)
    doc['candidates'][0]['root']=str(tmp_path/'outside');doc['candidates'][0]['selected']=True
    candidates.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        tool.prepare_batch(candidates,tmp_path/'bad',all_platform_readers=True)
    assert not (tmp_path/'bad').exists()


def _write_yaml(path, value):
    path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
    return path


def _wiki_evidence_inputs(tmp_path, *, both=True):
    source = tmp_path / 'raw'; source.mkdir(parents=True)
    parent = source / 'parent'; parent.mkdir()
    children = ['Sidescan/Colocated-Klein-SS-SB']
    if both:
        children.append('Sub-bottom/Colocated-Klein-SS-SB')
    for child in children:
        (parent / child).mkdir(parents=True)
    boundaries = _write_yaml(tmp_path / 'boundaries.yaml', {
        'adjudicated_by': 'user', 'source_root': str(source), 'roots': [{
            'path': str(parent), 'card': 'parent-card', 'model': 'one-dataset-with-subsets', 'subsets': children,
        }],
    })
    catalog = _write_yaml(tmp_path / 'catalog.yaml', {'records': [{'id': 'parent-card', 'name': 'parent'}]})
    first = {'id': 'subset-sidescan', 'name': 'Colocated-Klein-SS-SB', 'fact_state': 'verified', 'claims': [{
        'predicate': 'subset-of', 'object_id': 'parent-card', 'fact_state': 'verified',
    }]}
    records = [first]
    if both:
        records.append({'id': 'subset-sub-bottom', 'name': 'Colocated-Klein-SS-SB', 'fact_state': 'verified', 'claims': [{
            'predicate': 'subset-of', 'object_id': 'parent-card', 'fact_state': 'verified',
        }]})
    subsets = _write_yaml(tmp_path / 'subsets.yaml', {'records': records})
    findings = tmp_path / 'findings.json'
    findings.write_text(json.dumps({'units': [{
        'card': 'subset-sidescan', 'unit_path': str(parent / children[0]), 'fields': {}, 'source_documents': [],
    }] + ([{
        'card': 'subset-sub-bottom', 'unit_path': str(parent / children[1]), 'fields': {}, 'source_documents': [],
    }] if both else [])}), encoding='utf-8')
    return source, boundaries, catalog, subsets, findings


def test_exact_wiki_evidence_distinguishes_same_name_paths_without_payload_reads(tmp_path):
    source, boundaries, catalog, subsets, findings = _wiki_evidence_inputs(tmp_path)
    shutil.rmtree(source)  # Discovery must use metadata, without opening any raw tree.
    tool = load_tool('prepare-web-datasets')
    result = tool.discover(source, boundaries, catalog, subsets, findings)
    by_path = {item['relative_root']: item['metadata'] for item in result['candidates']}
    assert by_path['parent/Sidescan/Colocated-Klein-SS-SB']['wiki_ref'] == 'subset-sidescan'
    assert by_path['parent/Sub-bottom/Colocated-Klein-SS-SB']['wiki_ref'] == 'subset-sub-bottom'
    assert all('usage' not in item and 'license' not in item for item in by_path.values())


def test_rejects_bad_parent_relation_and_conflicting_exact_wiki_evidence(tmp_path):
    source, boundaries, catalog, subsets, findings = _wiki_evidence_inputs(tmp_path)
    tool = load_tool('prepare-web-datasets')
    subset_data = yaml.safe_load(subsets.read_text())
    subset_data['records'][0]['claims'][0]['object_id'] = 'other-parent'
    _write_yaml(subsets, subset_data)
    with pytest.raises(ValueError, match='verified subset'):
        tool.discover(source, boundaries, catalog, subsets, findings)

    source, boundaries, catalog, subsets, findings = _wiki_evidence_inputs(tmp_path / 'conflict')
    evidence = json.loads(findings.read_text())
    evidence['units'].append({**evidence['units'][0], 'card': 'subset-sub-bottom'})
    findings.write_text(json.dumps(evidence), encoding='utf-8')
    with pytest.raises(ValueError, match='conflicting'):
        tool.discover(source, boundaries, catalog, subsets, findings)


def test_discover_old_interface_keeps_unique_name_fallback(tmp_path):
    source, boundaries, catalog, subsets, _findings = _wiki_evidence_inputs(tmp_path, both=False)
    tool = load_tool('prepare-web-datasets')
    result = tool.discover(source, boundaries, catalog, subsets)
    assert result['candidates'][0]['metadata']['wiki_ref'] == 'subset-sidescan'


@pytest.mark.parametrize('problem', ['missing-card', 'wrong-name', 'unapproved-path', 'relative-path'])
def test_exact_evidence_requires_existing_matching_card_and_approved_absolute_path(tmp_path, problem):
    source, boundaries, catalog, subsets, findings = _wiki_evidence_inputs(tmp_path)
    evidence = json.loads(findings.read_text())
    if problem == 'missing-card':
        evidence['units'][0]['card'] = 'nonexistent'
    elif problem == 'wrong-name':
        subset_data = yaml.safe_load(subsets.read_text())
        subset_data['records'][0]['name'] = 'different-name'
        _write_yaml(subsets, subset_data)
    elif problem == 'unapproved-path':
        evidence['units'][0]['unit_path'] += '/unapproved-child'
    else:
        evidence['units'][0]['unit_path'] = 'relative/path'
    findings.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        load_tool('prepare-web-datasets').discover(source, boundaries, catalog, subsets, findings)
