import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest


def load():
    spec = importlib.util.spec_from_file_location('data_install', Path('tools/install-web-data-manager.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-data-install-test-', dir=Path.cwd()))
    module = load()
    monkeypatch.setattr(module, '_root_owned', lambda info: True)
    unit_root = root / 'units'; unit_root.mkdir()
    monkeypatch.setattr(module, 'UNIT_ROOT', unit_root)
    original_mkdtemp = module.tempfile.mkdtemp
    monkeypatch.setattr(module.tempfile, 'mkdtemp', lambda **kwargs: original_mkdtemp(prefix=kwargs['prefix'], dir=root))
    try:
        config = root / 'etc'; config.mkdir()
        raw = root / 'raw'; raw.mkdir()
        wiki = root / 'wiki'
        for name in ('sources', 'catalog', 'reports'): (wiki / name).mkdir(parents=True)
        for name in ('sources/dataset-boundaries.yaml', 'catalog/datasets.yaml', 'catalog/dataset-subsets.yaml'):
            (wiki / name).write_text('{}\n')
        (wiki / 'reports/ingestion-wave-5-findings.json').write_text('{"units": []}\n')
        code = root / 'app/core/researchkit'; code.mkdir(parents=True)
        for name in ('web_data_management.py', 'web_dataset_discovery.py', 'web_data_publisher.py', 'web_broker.py', 'web_app.py', 'web_identity.py'):
            shutil.copy2(Path('core/researchkit') / name, code / name)
        policy = config / 'policy.json'
        policy.write_text(json.dumps({'schema': 'researchkit.web-policy/v1', 'runner': {'uid': 980, 'username': 'runner'},
                                     'code_root': str(root / 'app'), 'derived_dataset_root': '/keep/derived',
                                     'dataset_registry': '/keep/legacy-datasets.json'}))
        calls = []
        def run(arguments):
            calls.append([str(x) for x in arguments])
            if arguments[:2] == ['systemctl', 'show']:
                return 'active' if module.UNIT in arguments else 'inactive'
            return ''
        monkeypatch.setattr(module, '_run', run)
        yield module, root, policy, raw, wiki, calls
    finally:
        shutil.rmtree(root)


def test_install_check_is_readonly_and_rejects_old_application(state):
    module, root, policy, raw, wiki, calls = state
    before = policy.read_bytes()
    assert module.inspect(policy, raw, wiki)[0]['ready'] is True
    assert policy.read_bytes() == before and list(policy.parent.iterdir()) == [policy]
    code = root / 'app/core/researchkit/web_broker.py'; code.write_text('old version')
    report, _ = module.inspect(policy, raw, wiki)
    assert report['ready'] is False and '更新代码' in report['errors'][0]


def test_install_preserves_legacy_configuration_and_creates_restricted_service(state, monkeypatch):
    module, root, policy, raw, wiki, calls = state
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    result = module.install(policy, raw, wiki)
    after = json.loads(policy.read_text())
    assert result['installed'] and after['dataset_registry'] == '/keep/legacy-datasets.json'
    assert after['derived_dataset_root'] == '/keep/derived'
    assert json.loads(Path(after['dataset_tree_registry']).read_text())['datasets'] == []
    unit = (module.UNIT_ROOT / module.UNIT).read_text()
    assert 'ProtectSystem=strict' in unit and 'RestrictAddressFamilies=AF_UNIX' in unit
    assert 'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0' in unit
    assert 'CapabilityBoundingSet=CAP_DAC_READ_SEARCH' in unit
    assert 'BindReadOnlyPaths="' + str(raw) in unit
    assert '/var/lib/researchkit-web' in unit  # Publisher has no submission-store access.
    assert not list(raw.iterdir())  # Installer never scans or modifies payloads.
    for name in ('researchkit-broker.service', 'researchkit-runner.service'):
        extra = module.UNIT_ROOT / (name + '.d') / 'researchkit-data-management.conf'
        assert str(raw) in extra.read_text() and 'ReadWritePaths' not in extra.read_text()


def test_failed_start_restores_exact_policy_and_existing_registry(state, monkeypatch):
    module, root, policy, raw, wiki, calls = state
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    registry = policy.parent / 'dataset-trees.json'
    registry.write_text(json.dumps({'schema': 'researchkit.web-dataset-trees/v1', 'datasets': [{'id': 'existing'}]}))
    doc = json.loads(policy.read_text()); doc['dataset_tree_registry'] = str(registry); policy.write_text(json.dumps(doc))
    policy_before, registry_before = policy.read_bytes(), registry.read_bytes()
    original = module._run
    def fail(arguments):
        if arguments[:3] == ['systemctl', 'enable', '--now']:
            raise RuntimeError('simulated unit failure')
        return original(arguments)
    monkeypatch.setattr(module, '_run', fail)
    with pytest.raises(RuntimeError, match='unit failure'):
        module.install(policy, raw, wiki)
    assert policy.read_bytes() == policy_before and registry.read_bytes() == registry_before
    assert not (policy.parent / 'data-management.json').exists()
    assert not (module.UNIT_ROOT / module.UNIT).exists()


def test_install_needs_admin_and_rejects_unit_specifier_paths(state, monkeypatch):
    module, _, policy, raw, wiki, _ = state
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1000)
    with pytest.raises(RuntimeError, match='管理员'):
        module.install(policy, raw, wiki)
    with pytest.raises(RuntimeError, match='格式'):
        module.service_unit(policy, Path('/home/share/%h'), wiki)


def test_existing_readonly_routes_are_preserved_without_duplicate_overrides(state):
    module, root, policy, raw, wiki, calls = state
    for name in ('researchkit-broker.service', 'researchkit-runner.service'):
        directory = module.UNIT_ROOT / (name + '.d'); directory.mkdir()
        original = '[Service]\nBindReadOnlyPaths="%s" "/keep/old-data"\n' % raw
        (directory / 'researchkit-data.conf').write_text(original)
    assert module.raw_mount_overrides(raw) == {}
