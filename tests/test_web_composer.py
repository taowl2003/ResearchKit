from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_composer import Composer
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def _principal() -> Principal:
    account = pwd.getpwuid(os.getuid())
    return Principal(account.pw_uid, account.pw_name, 'teacher')


@pytest.fixture
def composer():
    # Workspace intentionally rejects pytest's group-writable /tmp parents.
    root = Path(tempfile.mkdtemp(prefix='.rk-composer-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    library = root / 'library'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'library/algorithms', library / 'algorithms')
    shutil.copytree(Path(__file__).resolve().parents[1] / 'library/contracts', library / 'contracts')
    store = SubmissionStore(root / 'store')
    ws = Workspace(_principal(), root / 'workspace', LocalStoreClient(store, _principal(), library_root=library), library_root=library)
    yield Composer(ws), ws
    shutil.rmtree(root)


def _payload(revision: str, *, value=2, generated=True):
    return {'expected_revision': revision, 'parameters': {'value': value}, 'draft_contracts': [],
            'generate_entry': generated, 'steps': [{'alias': 'scale', 'ref': 'example.scale@1.0.0',
            'parameters': {'factor': 3}, 'inputs': {'value': {'kind': 'parameter', 'name': 'value'}}}]}


def test_preview_and_apply_are_static_and_generate_a_sealed_composition(composer):
    value, ws = composer
    project = ws.create_project('compose')
    opened = value.read(project['project_id'])
    preview = value.preview(project['project_id'], _payload(opened['revision']))
    assert preview['entry_text'] is not None and 'context.call' in preview['entry_text']
    assert '.researchkit/plugins/web-' in '\n'.join(preview['changed_paths'])
    assert not (Path(project['path']) / '.researchkit/plugins').exists()  # preview has no side effect
    applied = value.apply(project['project_id'], _payload(opened['revision']))
    root = Path(project['path'])
    assert applied['managed'] is True
    assert applied['steps'][0]['ref'] == 'example.scale@1.0.0'
    assert 'context.call' in (root / 'case.py').read_text(encoding='utf-8')
    assert (root / '.researchkit/lock.json').is_file()
    assert all(path.name.startswith('web-') for path in (root / '.researchkit/plugins').iterdir())


def test_rejects_conflict_forward_outputs_and_bad_values(composer):
    value, ws = composer
    project = ws.create_project('errors')
    opened = value.read(project['project_id'])
    path = Path(project['path']) / 'case.py'
    path.write_text('# IDE changed this\n', encoding='utf-8')
    with pytest.raises(KitError, match='修改'):
        value.apply(project['project_id'], _payload(opened['revision']))
    current = value.read(project['project_id'])
    bad = _payload(current['revision'], value=float('nan'))
    with pytest.raises(KitError, match='NaN'):
        value.preview(project['project_id'], bad)
    bad = _payload(current['revision'])
    bad['steps'][0]['inputs']['value'] = {'kind': 'output', 'step': 'scale', 'port': 'value'}
    with pytest.raises(KitError, match='更早'):
        value.preview(project['project_id'], bad)


def test_drafts_stay_local_and_regular_projects_are_not_converted(composer):
    value, ws = composer
    project = ws.create_project('draft')
    opened = value.read(project['project_id'])
    request = _payload(opened['revision'])
    request['draft_contracts'] = [{'id': 'local.value@1.0.0', 'type': 'number', 'description': 'local'}]
    value.apply(project['project_id'], request)
    assert (Path(project['path']) / '.researchkit/contracts/local.value/1.0.0.yaml').is_file()
    assert not (ws.library_root / 'contracts/local.value').exists()
    ordinary = ws.create_project('ordinary')
    root = Path(ordinary['path'])
    # Simulate an IDE-created fixed plugin config and refresh its lock through
    # the project helper, so rejection is about ownership rather than a lock.
    root_yaml = (root / 'project.yaml').read_text(encoding='utf-8')
    root_yaml = root_yaml.replace('plugins: {}', 'plugins:\n  ide:\n    path: algorithms/ide\n    parameters: {}')
    (root / 'project.yaml').write_text(root_yaml, encoding='utf-8')
    current = value.read(ordinary['project_id'])
    with pytest.raises(KitError, match='IDE 配置'):
        value.preview(ordinary['project_id'], _payload(current['revision']))


def test_recover_only_restores_our_recorded_bytes(composer, monkeypatch):
    value, ws = composer
    project = ws.create_project('recover')
    before = value.read(project['project_id'])
    original = value._write_file
    calls = 0

    def interrupted(project_id, root_fd, path, data, expected):
        nonlocal calls
        calls += 1
        original(project_id, root_fd, path, data, expected)
        if calls == 1:
            raise OSError('simulated interruption')

    monkeypatch.setattr(value, '_write_file', interrupted)
    with pytest.raises(OSError):
        value.apply(project['project_id'], _payload(before['revision']))
    monkeypatch.setattr(value, '_write_file', original)
    value.recover(project['project_id'])
    assert value.read(project['project_id'])['revision'] == before['revision']
    # A later IDE replacement must be retained and makes recovery diagnostic.
    value._write_journal(project['project_id'], [('case.py', b'old', b'new')])
    (Path(project['path']) / 'case.py').write_text('IDE now owns this\n', encoding='utf-8')
    with pytest.raises(KitError, match='冲突'):
        value.recover(project['project_id'])


def test_catalog_multiple_versions_and_generated_entry_semantics(composer):
    value, ws = composer
    # The production layout stores multiple contract versions as sibling YAML
    # files, not version directories.
    contracts = ws.library_root / 'contracts/scalar'
    second = contracts / '2.0.0.yaml'
    second.write_text((contracts / '1.0.0.yaml').read_text(encoding='utf-8').replace('scalar@1.0.0', 'scalar@2.0.0'), encoding='utf-8')
    journal_root = ws.root / '.researchkit-web-composer-journals'
    assert not journal_root.exists()
    catalog = value.catalog()
    assert not journal_root.exists()  # catalog/read-only paths do not create state
    assert {row['id'] for row in catalog['contracts']} == {'scalar@1.0.0', 'scalar@2.0.0'}
    steps = [
        {'alias': 'first', 'ref': 'example.scale@1.0.0', 'parameters': {'factor': 2},
         'inputs': {'value': {'kind': 'parameter', 'name': 'base'}}},
        {'alias': 'second', 'ref': 'example.scale@1.0.0', 'parameters': {'factor': 3},
         'inputs': {'value': {'kind': 'output', 'step': 'first', 'port': 'value'}}},
        {'alias': 'third', 'ref': 'example.scale@1.0.0', 'parameters': {'factor': 4},
         'inputs': {'value': {'kind': 'literal', 'value': 9}}},
    ]
    namespace = {}
    exec(value._entry(steps), namespace)

    class Context:
        parameters = {'base': 5}
        def __init__(self): self.calls = []
        def call(self, alias, *, inputs):
            self.calls.append((alias, inputs))
            return {'value': inputs['value']}

    context = Context()
    assert namespace['run'](context) == {'value': 9}
    assert context.calls == [('first', {'value': 5}), ('second', {'value': 5}), ('third', {'value': 9})]


def test_mid_transaction_ide_edit_is_preserved_and_journal_remains(composer, monkeypatch):
    value, ws = composer
    project = ws.create_project('mid-edit')
    before = value.read(project['project_id'])
    root = Path(project['path'])
    original = value._write_file
    calls = 0

    def edit_after_first(project_id, root_fd, path, data, expected):
        nonlocal calls
        original(project_id, root_fd, path, data, expected)
        calls += 1
        if calls == 1:
            (root / 'project.yaml').write_text('# IDE edit\n', encoding='utf-8')

    monkeypatch.setattr(value, '_write_file', edit_after_first)
    with pytest.raises(KitError, match='冲突'):
        value.apply(project['project_id'], _payload(before['revision']))
    assert (root / 'project.yaml').read_text(encoding='utf-8') == '# IDE edit\n'
    monkeypatch.setattr(value, '_write_file', original)
    with pytest.raises(KitError, match='恢复冲突'):
        value.recover(project['project_id'])


def test_output_only_contract_is_copied_and_library_links_are_rejected(composer):
    value, ws = composer
    source = ws.library_root / 'algorithms/test.emit/1.0.0'
    source.mkdir(parents=True)
    (source / 'plugin.yaml').write_text("""schema: hydro.plugin/v1
name: test.emit
version: 1.0.0
entry: plugin.py
inputs: {}
outputs:
  value: scalar@1.0.0
parameters: {}
""", encoding='utf-8')
    (source / 'plugin.py').write_text('def run(inputs, parameters, context):\n    return {"value": 1}\n', encoding='utf-8')
    project = ws.create_project('output-only')
    opened = value.read(project['project_id'])
    request = {'expected_revision': opened['revision'], 'parameters': {}, 'draft_contracts': [],
               'generate_entry': False, 'steps': [{'alias': 'emit', 'ref': 'test.emit@1.0.0',
               'parameters': {}, 'inputs': {}}]}
    value.apply(project['project_id'], request)
    assert (Path(project['path']) / '.researchkit/contracts/scalar/1.0.0.yaml').is_file()
    (ws.library_root / 'algorithms/unsafe').symlink_to(source, target_is_directory=True)
    # Once imported, Web serves the central snapshot and does not reopen CLI
    # files. Unsafe links still prevent a first import into a new store.
    assert any(row['ref'] == 'test.emit@1.0.0' for row in value.catalog()['plugins'])
    from researchkit.web_asset_library import AssetLibrary
    from researchkit.web_store import SubmissionStore
    with pytest.raises(KitError, match='链接'):
        AssetLibrary(SubmissionStore(ws.root / 'unsafe-import-store'), ws.library_root)


def test_deep_values_invalid_ide_state_and_required_default_are_handled(composer):
    value, ws = composer
    project = ws.create_project('bounds')
    opened = value.read(project['project_id'])
    nested = 0
    for _ in range(34):
        nested = [nested]
    request = _payload(opened['revision'], value=nested)
    with pytest.raises(KitError, match='复杂'):
        value.preview(project['project_id'], request)
    root = Path(project['path'])
    root_yaml = (root / 'project.yaml').read_text(encoding='utf-8').replace('parameters: {}', 'parameters:\n  unsafe: !!set {x: null}')
    (root / 'project.yaml').write_text(root_yaml, encoding='utf-8')
    with pytest.raises(KitError, match='JSON'):
        value.read(project['project_id'])
    # Composer validates explicit values after model.project-style defaults.
    source = ws.library_root / 'algorithms/test.default/1.0.0'
    source.mkdir(parents=True)
    (source / 'plugin.yaml').write_text("""schema: hydro.plugin/v1
name: test.default
version: 1.0.0
entry: plugin.py
inputs: {}
outputs: {}
parameters:
  required_with_default:
    type: number
    required: true
    default: 4
""", encoding='utf-8')
    (source / 'plugin.py').write_text('def run(inputs, parameters, context):\n    return {}\n', encoding='utf-8')
    other = ws.create_project('defaults')
    current = value.read(other['project_id'])
    request = {'expected_revision': current['revision'], 'parameters': {}, 'draft_contracts': [],
               'generate_entry': False, 'steps': [{'alias': 'defaulted', 'ref': 'test.default@1.0.0',
               'parameters': {}, 'inputs': {}}]}
    assert value.preview(other['project_id'], request)['changed_paths']


def test_missing_optional_parameters_malformed_records_and_shared_permissions(composer):
    import yaml
    value, ws = composer
    project = ws.create_project('record boundaries')
    config = Path(project['path']) / 'project.yaml'
    original = yaml.safe_load(config.read_text())
    original.pop('parameters')
    config.write_text(yaml.safe_dump(original))
    assert value.read(project['project_id'])['parameters'] == {}
    original['web_composition'] = {'schema': 'researchkit.web-composition/v1',
                                   'steps': [None], 'draft_contracts': []}
    config.write_text(yaml.safe_dump(original))
    with pytest.raises(KitError, match='步骤格式'):
        value.read(project['project_id'])
    del original['web_composition']
    config.write_text(yaml.safe_dump(original))
    request = _payload(value.read(project['project_id'])['revision'], value='\ud800')
    with pytest.raises(KitError, match='UTF-8'):
        value.preview(project['project_id'], request)
    source = ws.library_root / 'algorithms/example.scale/1.0.0/plugin.py'
    source.chmod(0o666)
    with pytest.raises(KitError, match='普通文件'):
        value.catalog()
