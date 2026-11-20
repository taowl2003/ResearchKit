"""Project-side whole-dataset attachment keeps raw paths out of the Web UI."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def _principal() -> Principal:
    username = pwd.getpwuid(os.getuid()).pw_name
    return Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')


def _reference(dataset_id: str, path: str, data: bytes) -> dict[str, object]:
    return {'dataset_id': dataset_id, 'path': path, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}


class CatalogClient:
    def __init__(self, delegate, catalog):
        self.delegate, self.catalog, self.uploads, self.read_sizes = delegate, catalog, {}, []

    def request(self, method: str, **params):
        if method == 'get_dataset_catalog':
            return self.catalog
        if method == 'begin_derived_upload':
            upload_id = f'upload-{len(self.uploads) + 1}'
            self.uploads[upload_id] = {'name': params['name'], 'expected_size': params['expected_size'],
                                       'source_refs': params['source_refs'], 'data': bytearray()}
            return {'upload_id': upload_id, 'chunk_size': 1024 * 1024, 'expires_at': '2030-01-01T00:00:00Z'}
        if method == 'append_derived_upload':
            upload = self.uploads[params['upload_id']]
            assert params['offset'] == len(upload['data'])
            self.read_sizes.append(len(params['data']))
            upload['data'].extend(params['data'])
            return {'received': len(upload['data'])}
        if method == 'commit_derived_upload':
            upload = self.uploads[params['upload_id']]
            content = bytes(upload['data'])
            assert params['sha256'] == hashlib.sha256(content).hexdigest()
            assert len(content) == upload['expected_size']
            return {'dataset_id': 'derived-immutable-v1', 'name': upload['name'],
                    'files': [_reference('derived-immutable-v1', 'data.bin', content)], 'visibility': 'private'}
        if method == 'abort_derived_upload':
            return {'aborted': True}
        return self.delegate.request(method, **params)


@pytest.fixture
def workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-web-dataset-flow-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    principal = _principal()
    store = SubmissionStore(root / 'store')
    first = _reference('rainfall-2026-v1', 'daily/a.csv', b'a,1\n')
    second = _reference('rainfall-2026-v1', 'daily/b.csv', b'b,2\n')
    tree = _reference('basin-tree-v1', '__researchkit_dataset_tree__', b'manifest-digest')
    catalog = [
        {'dataset_id': 'rainfall-2026-v1', 'name': 'Rainfall', 'description': 'daily values',
         'wiki_ref': 'Dataset/rainfall', 'files': [first, second]},
        {'dataset_id': 'basin-tree-v1', 'kind': 'directory', 'file_count': 2000,
         'total_size': 8 * 1024**3, 'files': [tree]},
    ]
    value = Workspace(principal, root / 'workspace',
                      CatalogClient(LocalStoreClient(store, principal), catalog))
    try:
        yield value, (first, second, tree)
    finally:
        shutil.rmtree(root)


def test_add_whole_catalog_dataset_merges_idempotently_and_preserves_manual_refs(workspace):
    value, (first, second, tree) = workspace
    project = value.create_project('data project')
    project_id = project['project_id']
    before = value.project_datasets(project_id)
    assert before == {'exists': False, 'sha256': None, 'references': [], 'datasets': []}

    attached = value.add_project_datasets(project_id, ['rainfall-2026-v1', 'basin-tree-v1'], None)
    assert attached['references'] == [first, second, tree]
    assert attached['datasets'] == [
        {'dataset_id': 'basin-tree-v1', 'file_count': 1, 'total_size': tree['size']},
        {'dataset_id': 'rainfall-2026-v1', 'file_count': 2, 'total_size': first['size'] + second['size']},
    ]
    project_file = Path(project['path']) / 'DATA-REFERENCES.json'
    assert json.loads(project_file.read_text(encoding='utf-8')) == [first, second, tree]

    again = value.add_project_datasets(project_id, ['rainfall-2026-v1'], attached['sha256'])
    assert again['references'] == [first, second, tree]
    assert again['sha256'] == attached['sha256']


def test_dataset_attachment_requires_catalog_authorization_and_compare_and_swap(workspace):
    value, (first, _second, _tree) = workspace
    project = value.create_project('conflict project')
    project_id = project['project_id']
    with pytest.raises(KitError, match='未获授权'):
        value.add_project_datasets(project_id, ['missing-v1'], None)

    # A file created after the absent-state read must not be overwritten.
    project_file = Path(project['path']) / 'DATA-REFERENCES.json'
    project_file.write_text(json.dumps([first]), encoding='utf-8')
    with pytest.raises(KitError, match='编辑冲突'):
        value.add_project_datasets(project_id, ['rainfall-2026-v1'], None)

    state = value.project_datasets(project_id)
    project_file.write_text(json.dumps([]), encoding='utf-8')
    with pytest.raises(KitError, match='编辑冲突'):
        value.add_project_datasets(project_id, ['rainfall-2026-v1'], state['sha256'])


def test_dataset_attachment_rejects_catalog_that_tries_to_supply_host_metadata(workspace):
    value, _items = workspace
    project = value.create_project('bad catalog')
    value.store.catalog[0]['files'][0]['host_path'] = '/home/share/Datasets/private.csv'
    with pytest.raises(KitError, match='目录文件条目'):
        value.add_project_datasets(project['project_id'], ['rainfall-2026-v1'], None)


def test_project_file_is_streamed_to_derived_store_without_accepting_home_paths(workspace):
    value, _items = workspace
    project = value.create_project('derived project')
    source = Path(project['path']) / 'data.bin'
    content = b'x' * (1024 * 1024 + 31)
    source.write_bytes(content)
    record = value.upload_project_file_as_derived(project['project_id'], 'data.bin', 'cleaned values')
    assert record['dataset_id'] == 'derived-immutable-v1'
    assert value.store.read_sizes == [1024 * 1024, 31]
    assert bytes(value.store.uploads['upload-1']['data']) == content
    with pytest.raises(KitError, match='越界'):
        value.upload_project_file_as_derived(project['project_id'], '/home/ly/private.bin', 'bad')


def test_managed_large_local_copy_is_omitted_only_after_fixed_reference_is_attached(workspace):
    value, _items = workspace
    project = value.create_project('large derived project')
    directory = Path(project['path']) / 'data' / 'processed'
    directory.mkdir(parents=True)
    source = directory / 'features.bin'
    # A sparse source just over the old inline submission limit.
    with source.open('wb') as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    record = value.upload_project_file_as_derived(project['project_id'], 'data/processed/features.bin', 'features')
    assert record['source_reference_only_after_attach'] is True
    assert max(value.store.read_sizes) <= 1024 * 1024
    value.store.catalog.append(record)
    value.add_project_datasets(project['project_id'], [record['dataset_id']], None)
    _root, packed, references = value._snapshot(project['project_id'])
    assert source.exists() and 'data/processed/features.bin' not in packed
    assert '.researchkit/derived-imports.json' not in packed
    assert references == record['files']
    # Changed local output must not silently be omitted as the old saved version.
    with source.open('r+b') as stream:
        stream.write(b'x')
    with pytest.raises(KitError, match='本地产物'):
        value._snapshot(project['project_id'])
