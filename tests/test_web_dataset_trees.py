from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit import web_datasets
from researchkit.web_dataset_trees import TreeRegistry, prepare_tree, TREE_PATH
from researchkit.web_dataset_metadata import enrich_catalog
from researchkit.web_identity import Principal


@pytest.fixture
def fixture(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-tree-test-', dir=Path.cwd()))
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid in {0, os.getuid()})
    source = root / 'source'; source.mkdir(mode=0o755)
    (source / 'nested').mkdir()
    (source / 'nested' / 'input.csv').write_text('value\n10\n')
    account = pwd.getpwuid(os.getuid()).pw_name
    principal = Principal(os.getuid(), account, 'teacher' if account == 'ly' else 'student')
    try:
        yield root, source, principal
    finally:
        shutil.rmtree(root)


def registry_for(fixture):
    root, source, principal = fixture
    snapshot = prepare_tree(source)
    raw = json.dumps(snapshot).encode()
    manifest = root / 'manifest.json'; manifest.write_bytes(raw)
    registry = root / 'registry.json'
    registry.write_text(json.dumps({'schema': 'researchkit.web-dataset-trees/v1', 'datasets': [{
        'dataset_id': 'whole-v1', 'manifest': str(manifest), 'sha256': hashlib.sha256(raw).hexdigest(),
        'total_size': 9, 'file_count': 1, 'readers': [{'uid': principal.uid, 'username': principal.username}]}]}))
    return TreeRegistry(registry)


def test_whole_tree_mount_single_reference_and_correct_relative_layout(fixture):
    registry = registry_for(fixture); root, source, principal = fixture
    item = registry.catalog(principal)[0]
    assert item['file_count'] == 1 and len(item['files']) == 1
    assert item['files'][0]['path'] == TREE_PATH
    with registry.open_references([principal], item['files']) as mounts:
        assert len(mounts) == 1 and mounts[0].target == '/datasets/whole-v1'
        assert os.path.isdir('/proc/self/fd/' + str(mounts[0].fd))
        nested = os.open('nested', os.O_RDONLY | os.O_DIRECTORY, dir_fd=mounts[0].fd)
        try:
            fd = os.open('input.csv', os.O_RDONLY, dir_fd=nested)
            assert os.read(fd, 100) == b'value\n10\n'; os.close(fd)
        finally:
            os.close(nested)


def test_tree_rejects_content_and_membership_changes(fixture):
    registry = registry_for(fixture); root, source, principal = fixture
    references = registry.catalog(principal)[0]['files']
    (source / 'nested' / 'input.csv').write_text('value\n20\n')
    with pytest.raises(KitError, match='变化'):
        with registry.open_references([principal], references):
            pass


def test_tree_detects_mutation_during_run(fixture):
    registry = registry_for(fixture); root, source, principal = fixture
    references = registry.catalog(principal)[0]['files']
    with pytest.raises(KitError, match='变化'):
        with registry.open_references([principal], references):
            (source / 'unexpected.txt').write_text('later')


def test_tree_requires_all_principals_and_exact_snapshot(fixture):
    registry = registry_for(fixture); _, _, principal = fixture
    refs = registry.catalog(principal)[0]['files']
    attacker = Principal(principal.uid + 999, 'different-user', 'student')
    assert registry.catalog(attacker) == []
    with pytest.raises(KitError, match='无权'):
        registry.authorize([principal, attacker], refs)
    with pytest.raises(KitError, match='匹配'):
        registry.authorize([principal], [{**refs[0], 'sha256': '0' * 64}])


@pytest.mark.parametrize('unsafe', ['soft', 'hard', 'writable'])
def test_tree_publication_rejects_unsafe_payloads(fixture, unsafe):
    _, source, _ = fixture
    target = source / 'nested' / 'input.csv'
    if unsafe == 'soft':
        (source / 'linked.csv').symlink_to(target)
    elif unsafe == 'hard':
        os.link(target, source / 'linked.csv')
    else:
        target.chmod(0o666)
    with pytest.raises(KitError):
        prepare_tree(source)


def test_metadata_enrichment_never_overrides_authorization_fields(fixture):
    root, _, principal = fixture
    file = root / 'metadata.json'
    file.write_text(json.dumps({'schema': 'researchkit.web-dataset-metadata/v1', 'datasets': {
        'whole-v1': {'name': 'A dataset', 'wiki_ref': 'dataset-real-id', 'description': 'Wiki description'}}}))
    catalog = [{'dataset_id': 'whole-v1', 'files': [], 'visibility': 'private'}]
    enriched = enrich_catalog(catalog, file)
    assert enriched[0]['name'] == 'A dataset' and enriched[0]['visibility'] == 'private'
    file.write_text(json.dumps({'schema': 'researchkit.web-dataset-metadata/v1', 'datasets': {
        'whole-v1': {'readers': '*'}}}))
    with pytest.raises(KitError):
        enrich_catalog(catalog, file)


def test_batch_discovery_requires_review_and_never_reads_payload(fixture):
    root, source, _ = fixture
    spec = importlib.util.spec_from_file_location('batch', Path('tools/prepare-web-datasets.py'))
    tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
    result = tool.discover(source)
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['selected'] is False
    assert result['candidates'][0]['status'] == 'pending-review'
    assert result['candidates'][0]['root'] == str(source / 'nested')
    assert (source / 'nested' / 'input.csv').read_text() == 'value\n10\n'


def test_tree_browse_and_ide_fd_keep_relative_paths_and_authorization(fixture):
    registry=registry_for(fixture);_,_,principal=fixture
    listing=registry.list_files(principal,'whole-v1',q='input',limit=1)
    assert listing['total']==1 and listing['next_offset'] is None
    assert listing['items'][0]['path']=='nested/input.csv'
    assert 'root' not in listing and 'manifest' not in listing
    fd,reference=registry.open_file(principal,'whole-v1','nested/input.csv')
    try:
        assert os.read(fd,100)==b'value\n10\n' and reference['size']==9
    finally:os.close(fd)
    with pytest.raises(KitError,match='无权'):
        registry.list_files(Principal(7777,'unauthorized','student'),'whole-v1')
    with pytest.raises(KitError):registry.open_file(principal,'whole-v1','../outside')
