"""Description maintenance must not rehash data or alter immutable references."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

import pytest

from researchkit.files import KitError
from researchkit import web_datasets
from researchkit.web_dataset_metadata import SCHEMA
from researchkit.web_dataset_trees import TreeRegistry


def tool(name):
    spec = importlib.util.spec_from_file_location(name, Path('tools') / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-description-test-', dir=Path.cwd()))
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: True)
    try:
        source = root / 'raw'; source.mkdir()
        unit = source / 'unit'; unit.mkdir()
        payload = unit / 'input.csv'; payload.write_text('input')
        candidates = tool('prepare-web-datasets').discover(source)
        candidates['candidates'][0]['selected'] = True
        # Published IDs may have been approved independently from the hints.
        candidates['candidates'][0]['dataset_id_hint'] = 'human-reviewed-v1'
        candidate_file = root / 'candidates.json'
        candidate_file.write_text(json.dumps(candidates))
        bundle = root / 'bundle'
        tool('prepare-web-dataset-trees').prepare_batch(candidate_file, bundle, all_platform_readers=True,
                                                       manifest_root=root / 'manifests')
        # Simulate the administrator's manifest installation, leaving payloads
        # untouched; the batch tool intentionally only prepares this directory.
        shutil.copytree(bundle / 'manifests', root / 'manifests')
        metadata = bundle / 'datasets.metadata.json'
        old = json.loads(metadata.read_text())
        old['datasets']['other-existing-id'] = {'name': 'untouched'}
        metadata.write_text(json.dumps(old))
        policy = root / 'policy.json'; policy.write_text(json.dumps({'dataset_metadata': str(metadata)}))
        yield root, candidates, candidate_file, bundle / 'dataset-trees.json', metadata, policy, payload
    finally:
        shutil.rmtree(root)


def reviewed(state, info=None):
    root, candidates, candidate_file, registry, metadata, policy, payload = state
    candidates['candidates'][0]['metadata'] = info or {'name': 'reviewed name', 'usage': 'only verified instructions'}
    candidate_file.write_text(json.dumps(candidates))
    output = root / 'reviewed.json'
    report = tool('refresh-web-dataset-metadata').refresh(candidate_file, registry, metadata, output)
    return output, report


def test_refresh_uses_manifest_path_preserves_other_ids_and_never_reads_payload(state, monkeypatch):
    root, _, _, registry, metadata, _, payload = state
    registry_before, metadata_before = registry.read_bytes(), metadata.read_bytes()
    pinned = TreeRegistry(registry).reference(TreeRegistry(registry)._load()['human-reviewed-v1'])
    payload.unlink()  # Raw input is unavailable, yet a description refresh must succeed.
    monkeypatch.setattr(web_datasets.DatasetRegistry, '_open_directory',
                        lambda *a, **k: pytest.fail('must not open raw dataset directories'))
    output, report = reviewed(state)
    assert report['installed'] is False
    assert report['updated_dataset_ids'] == ['human-reviewed-v1']
    assert json.loads(output.read_text())['datasets'] == {
        'human-reviewed-v1': {'name': 'reviewed name', 'usage': 'only verified instructions'},
        'other-existing-id': {'name': 'untouched'}}
    assert registry.read_bytes() == registry_before and metadata.read_bytes() == metadata_before
    assert TreeRegistry(registry).reference(TreeRegistry(registry)._load()['human-reviewed-v1']) == pinned
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        tool('refresh-web-dataset-metadata').refresh(state[2], registry, metadata, output)


def test_refresh_preserves_registered_tags_as_the_only_structured_metadata_field(state):
    metadata = json.loads(state[4].read_text())
    metadata['datasets']['other-existing-id']['tags'] = ['课堂', '示例']
    state[4].write_text(json.dumps(metadata))
    output, _ = reviewed(state)
    assert json.loads(output.read_text())['datasets']['other-existing-id']['tags'] == ['课堂', '示例']


@pytest.mark.parametrize('bad', [{'readers': 'platform-users'}, {'usage': 12}])
def test_refresh_rejects_non_description_or_non_text_fields(state, bad):
    with pytest.raises((KitError, ValueError)):
        reviewed(state, bad)
    assert not (state[0] / 'reviewed.json').exists()


def test_refresh_rejects_unregistered_units_and_false_identity(state):
    root, candidates, candidate_file, registry, metadata, _, _ = state
    item = candidates['candidates'][0]
    item['root'] = str(root / 'raw' / 'not-registered')
    item['relative_root'] = 'not-registered'
    item['candidate_id'] = hashlib.sha256(item['root'].encode()).hexdigest()[:24]
    candidate_file.write_text(json.dumps(candidates))
    with pytest.raises(KitError, match='尚未登记'):
        tool('refresh-web-dataset-metadata').refresh(candidate_file, registry, metadata, root / 'reviewed.json')
    item['candidate_id'] = '0' * 24
    candidate_file.write_text(json.dumps(candidates))
    with pytest.raises(ValueError, match='不匹配'):
        tool('refresh-web-dataset-metadata').refresh(candidate_file, registry, metadata, root / 'reviewed.json')
    assert not (root / 'reviewed.json').exists()


def arguments(state, output):
    module = tool('apply-web-dataset-metadata')
    return module, (state[5], output, module.digest(state[4].read_bytes()), module.digest(output.read_bytes()))


def test_apply_check_is_readonly_then_backup_and_replace_only_metadata(state, monkeypatch):
    output, _ = reviewed(state)
    module, args = arguments(state, output)
    metadata_before, registry_before, policy_before = (state[i].read_bytes() for i in (4, 3, 5))
    assert module.inspect(*args)[0]['installed'] is False
    assert state[4].read_bytes() == metadata_before
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    result = module.apply(*args)
    assert result['installed'] is True
    assert Path(result['backup']).read_bytes() == metadata_before
    assert Path(result['backup']).stat().st_mode & 0o777 == 0o600
    assert state[4].read_bytes() == output.read_bytes()
    assert state[4].stat().st_mode & 0o777 == 0o644
    assert state[3].read_bytes() == registry_before and state[5].read_bytes() == policy_before


def test_apply_rejects_normal_user_stale_hash_or_acl_and_id_changes(state, monkeypatch):
    output, _ = reviewed(state)
    module, args = arguments(state, output)
    before = state[4].read_bytes()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1000)
    with pytest.raises(KitError, match='管理员'):
        module.apply(*args)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    with pytest.raises(KitError, match='已变化'):
        module.apply(args[0], args[1], '0' * 64, args[3])
    assert state[4].read_bytes() == before
    doc = json.loads(output.read_text()); doc['datasets']['human-reviewed-v1']['readers'] = 'platform-users'
    output.write_text(json.dumps(doc)); module, args = arguments(state, output)
    with pytest.raises(KitError, match='字段无效'):
        module.apply(*args)
    doc['datasets']['human-reviewed-v1'].pop('readers'); doc['datasets']['new-id'] = {'name': 'unreviewed'}
    output.write_text(json.dumps(doc)); module, args = arguments(state, output)
    with pytest.raises(KitError, match='不能新增或删除'):
        module.apply(*args)
    assert state[4].read_bytes() == before
    assert not list(state[4].parent.glob('*.backup.*'))


def test_apply_catches_concurrent_policy_change_before_replacement(state, monkeypatch):
    output, _ = reviewed(state)
    module, args = arguments(state, output)
    before = state[4].read_bytes()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    original_mkstemp = module.tempfile.mkstemp
    def racing_mkstemp(*a, **k):
        result = original_mkstemp(*a, **k)
        if k['prefix'].startswith('.'):
            state[5].write_text(json.dumps({'dataset_metadata': str(state[4]), 'changed': True}))
        return result
    monkeypatch.setattr(module.tempfile, 'mkstemp', racing_mkstemp)
    with pytest.raises(KitError, match='安装期间变化'):
        module.apply(*args)
    assert state[4].read_bytes() == before
    assert len(list(state[4].parent.glob('*.backup.*'))) == 1
    assert not list(state[4].parent.glob('.datasets.metadata.json.*'))
