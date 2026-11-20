from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit import web_datasets
from researchkit.web_assets import AssetService
from researchkit.web_identity import Principal
from researchkit.web_lifecycle import AssetLifecycle
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'test', 'student')


def _project_files() -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: test
plugins: {}
parameters: {}
data: {}
requires: {}
''',
        'case.py': b'def run(context):\n    return {}\n',
    }


def _asset_project_files(*, origin: str = '') -> dict[str, bytes]:
    return {
        'project.yaml': f'''schema: researchkit.project/v1
mode: python
entry: case.py
name: source
plugins:
  scale:
    path: plugins/scale
parameters: {{}}
data: {{}}
requires: {{}}
{origin}'''.encode(),
        'case.py': b'def run(context):\n    return {}\n',
        'plugins/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: scale
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs: {}
parameters: {}
''',
        'plugins/scale/plugin.py': b'def run(inputs, parameters, context):\n    return {}\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
    }


def _reference(dataset_id: str = 'rainfall') -> dict[str, object]:
    return {'dataset_id': dataset_id, 'path': 'rainfall.csv', 'size': 3,
            'sha256': hashlib.sha256(b'csv').hexdigest()}


@pytest.fixture
def state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_datasets, '_root_owned', lambda _info: True)
    # pytest's /tmp parent is deliberately world-writable; registry parser
    # security is covered by its own tests, while this fixture needs a small
    # synthetic registry under pytest's managed temporary directory.
    monkeypatch.setattr(web_datasets, '_safe_info', lambda *_args, **_kwargs: None)
    registry = tmp_path / 'datasets.json'
    registry.write_text(json.dumps({'schema': 'researchkit.web-datasets/v1', 'datasets': [{
        'dataset_id': 'rainfall', 'root': str(tmp_path / 'raw'),
        'readers': [{'uid': TEACHER.uid, 'username': TEACHER.username}],
        'files': [{'path': 'rainfall.csv', 'size': 3, 'sha256': hashlib.sha256(b'csv').hexdigest()}],
    }]}))
    store = SubmissionStore(tmp_path / 'store')
    policy = SimpleNamespace(dataset_registry=registry, dataset_tree_registry=None)
    return store, AssetLifecycle(store, policy=policy)


def test_dataset_report_lists_fixed_submission_and_blocks_trash(state):
    store, lifecycle = state
    project = store.create_project(TEACHER, 'rain')
    fixed = store.submit(project['project_id'], TEACHER, 'minor', _project_files(), [_reference()])
    report = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    assert report['state'] == 'active'
    assert report['reference_count'] == 1
    assert report['references'][0]['submission_id'] == fixed['id']
    assert 'rain' in report['references'][0]['label']
    assert report['can_trash'] is True
    retired = lifecycle.change(TEACHER, 'dataset', 'rainfall', 'retire', report['revision'])
    assert retired['state'] == 'retired'
    restored = lifecycle.change(TEACHER, 'dataset', 'rainfall', 'restore', retired['revision'])
    assert restored['state'] == 'active'
    trashed = lifecycle.change(TEACHER, 'dataset', 'rainfall', 'trash', restored['revision'])
    assert trashed['state'] == 'trashed' and trashed['reference_count'] == 1


def test_unreferenced_dataset_can_be_trashed_and_restored_without_touching_registry(state):
    _store, lifecycle = state
    before = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    assert before['can_trash'] is True
    trashed = lifecycle.change(TEACHER, 'dataset', 'rainfall', 'trash', before['revision'])
    assert trashed['state'] == 'trashed'
    assert lifecycle.summary('dataset', 'rainfall')['state'] == 'trashed'
    restored = lifecycle.change(TEACHER, 'dataset', 'rainfall', 'restore', trashed['revision'])
    assert restored['state'] == 'active'
    assert [item['action'] for item in restored['history']] == ['restore', 'trash']


def test_report_and_mutation_require_teacher_and_fresh_revision(state):
    _store, lifecycle = state
    with pytest.raises(KitError, match='只有教师'):
        lifecycle.report(STUDENT, 'dataset', 'rainfall')
    report = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    lifecycle.change(TEACHER, 'dataset', 'rainfall', 'retire', report['revision'])
    with pytest.raises(KitError, match='状态冲突'):
        lifecycle.change(TEACHER, 'dataset', 'rainfall', 'restore', report['revision'])


def test_usage_summary_is_owner_bound_and_extracts_exact_metadata(state):
    store, lifecycle = state
    project = store.create_project(TEACHER, 'usage')
    usage = lifecycle.extract_usage(_project_files(), [_reference()])
    assert usage == {'dataset_ids': ['rainfall'], 'algorithm_refs': [], 'template_refs': []}
    lifecycle.record_usage(TEACHER, project['project_id'], usage)
    report = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    assert report['reference_count'] == 1
    assert report['references'][0]['type'] == 'web-project-summary'
    with pytest.raises(KitError, match='只能登记自己的'):
        lifecycle.record_usage(STUDENT, project['project_id'], usage)


def test_only_public_derived_datasets_are_lifecycle_managed(state):
    store, _lifecycle = state
    with store._connection() as con:
        con.execute('''CREATE TABLE web_derived_datasets (
            dataset_id TEXT PRIMARY KEY, owner_username TEXT NOT NULL,
            source_submission_id TEXT, source_refs_json TEXT NOT NULL, visibility TEXT NOT NULL)''')
        con.execute("INSERT INTO web_derived_datasets VALUES ('derived-public','test',NULL,'[]','public')")
        con.execute("INSERT INTO web_derived_datasets VALUES ('derived-private','test',NULL,'[]','private')")
    lifecycle = AssetLifecycle(store, policy=_lifecycle.policy)
    report = lifecycle.report(TEACHER, 'dataset', 'derived-public')
    retired = lifecycle.change(TEACHER, 'dataset', 'derived-public', 'retire', report['revision'])
    assert retired['state'] == 'retired'
    with pytest.raises(KitError, match='不存在'):
        lifecycle.report(TEACHER, 'dataset', 'derived-private')


def test_malformed_fixed_metadata_blocks_trash_and_reference_pagination_is_complete(state):
    store, lifecycle = state
    project = store.create_project(TEACHER, 'bad-metadata')
    fixed = store.submit(project['project_id'], TEACHER, 'minor', _project_files(), [])
    with store._connection() as con:
        con.execute("UPDATE submissions SET manifest_json='{' WHERE id=?", (fixed['id'],))
    malformed = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    assert malformed['reference_count'] == 0 and malformed['can_trash'] is False
    assert any('无法完整核对' in note for note in malformed['notes'])

    # Use a fresh store to avoid deliberately malformed metadata masking the
    # pagination assertion; fixed submissions remain the authoritative count.
    other_root = Path(tempfile.mkdtemp(prefix='.rk-lifecycle-pages-', dir=Path.cwd()))
    other = SubmissionStore(other_root / 'store')
    try:
        policy = lifecycle.policy
        page_lifecycle = AssetLifecycle(other, policy=policy)
        for number in range(55):
            item = other.create_project(TEACHER, f'page-{number}')
            other.submit(item['project_id'], TEACHER, 'minor', _project_files(), [_reference()])
        first = page_lifecycle.report(TEACHER, 'dataset', 'rainfall', 0, 50)
        second = page_lifecycle.report(TEACHER, 'dataset', 'rainfall', 50, 50)
        assert first['reference_count'] == 55
        assert len(first['references']) == 50 and len(second['references']) == 5
        assert {row['submission_id'] for row in first['references']} | {row['submission_id'] for row in second['references']} == {
            row['id'] for row in other.list_submissions(TEACHER)
        }
    finally:
        shutil.rmtree(other_root)


def test_published_template_dependencies_and_template_provenance_are_reported(state, request):
    store, _old = state
    library = Path(tempfile.mkdtemp(prefix='.rk-lifecycle-library-', dir=Path.cwd()))
    request.addfinalizer(lambda: shutil.rmtree(library))
    for directory in ('algorithms', 'cases', 'contracts'):
        (library / directory).mkdir(mode=0o755)
    source_project = store.create_project(TEACHER, 'asset-source')
    source = store.submit(source_project['project_id'], TEACHER, 'minor', _asset_project_files(), [_reference()])
    assets = AssetService(store, library)
    algorithm_one = assets.create(TEACHER, source['id'], 'algorithm', 'scale', '1.0.0', 'scale')
    assets.review(TEACHER, algorithm_one['candidate_id'], algorithm_one['candidate_sha256'], 'ok')
    assets.publish(TEACHER, algorithm_one['candidate_id'], algorithm_one['candidate_sha256'])
    # A second exact version exists, but the template embeds only 1.0.0.
    algorithm_two = assets.create(TEACHER, source['id'], 'algorithm', 'scale', '2.0.0', 'scale')
    assets.review(TEACHER, algorithm_two['candidate_id'], algorithm_two['candidate_sha256'], 'ok')
    assets.publish(TEACHER, algorithm_two['candidate_id'], algorithm_two['candidate_sha256'])
    template = assets.create(TEACHER, source['id'], 'template', 'practice', '1.0.0')
    assets.review(TEACHER, template['candidate_id'], template['candidate_sha256'], 'ok')
    assets.publish(TEACHER, template['candidate_id'], template['candidate_sha256'])

    # A later immutable case retains the exact source provenance.  It is an
    # incoming template use; the source fixed submission itself is excluded.
    child = store.create_project(TEACHER, 'from-template')
    child_files = _asset_project_files(origin=(
        'origin:\n  kind: web-asset-source/v1\n  submission_id: ' + str(source['id']) + '\n'))
    store.submit(child['project_id'], TEACHER, 'minor', child_files, [_reference()])
    assigned = store.create_distribution(TEACHER, source['id'], STUDENT)
    store.claim_distribution(assigned['distribution_id'], STUDENT)
    package = store.export_package(source['id'], TEACHER)
    outsider = Principal(1003, 'other', 'student')
    store.import_package(outsider, 'imported', package['files'], package['bundle'])

    policy = SimpleNamespace(dataset_registry=_old.policy.dataset_registry,
                             dataset_tree_registry=None)
    lifecycle = AssetLifecycle(store, library, policy)
    dataset = lifecycle.report(TEACHER, 'dataset', 'rainfall')
    assert any(item['type'] == 'platform-template' and item['detail'] == 'practice@1.0.0'
               for item in dataset['references']), dataset
    used_algorithm = lifecycle.report(TEACHER, 'algorithm', 'scale@1.0.0')
    assert any(item['type'] == 'platform-template' for item in used_algorithm['references'])
    unused_version = lifecycle.report(TEACHER, 'algorithm', 'scale@2.0.0')
    assert not any(item['type'] == 'platform-template' for item in unused_version['references'])
    template_report = lifecycle.report(TEACHER, 'template', 'practice@1.0.0')
    types = {item['type'] for item in template_report['references']}
    assert {'template-instance', 'distribution', 'trusted-baseline', 'package_bundle', 'package-baseline'} <= types
