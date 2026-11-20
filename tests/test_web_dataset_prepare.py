import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    'researchkit_prepare_web_dataset', Path(__file__).parents[1] / 'tools' / 'prepare-web-dataset.py'
)
assert SPEC and SPEC.loader
prepare_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_tool)


@pytest.fixture(autouse=True)
def _temporary_root_ownership(monkeypatch, tmp_path):
    allowed = {os.lstat(str(path)).st_uid for path in (tmp_path,) + tuple(tmp_path.parents)}
    monkeypatch.setattr(prepare_tool, '_root_owned', lambda info: info.st_uid in allowed)
    # pytest stores temporary roots under world-writable /tmp.  Production
    # deliberately rejects that ancestor; retain checks on each test root
    # itself while isolating the fixture's hosting path from this unit test.
    def safe_test_directory(path, label):
        path = Path(path)
        prepare_tool._safe_info(os.lstat(str(path)), directory=True)
        return path
    monkeypatch.setattr(prepare_tool, '_safe_absolute_directory', safe_test_directory)
    monkeypatch.setattr(prepare_tool.pwd, 'getpwnam',
                        lambda name: SimpleNamespace(pw_name=name, pw_uid=1001, pw_shell='/bin/bash'))


def _registry(path, datasets):
    payload = {'schema': prepare_tool.SCHEMA, 'datasets': datasets}
    path.write_text(json.dumps(payload), encoding='utf-8')
    return payload


def test_prepares_exact_read_only_candidate_without_scanning_unlisted_files(tmp_path):
    root = tmp_path / 'data'
    (root / 'nested').mkdir(parents=True)
    chosen = root / 'nested' / 'input.bin'
    chosen.write_bytes(b'alpha')
    (root / 'unlisted.bin').write_bytes(b'must not appear')

    candidate = prepare_tool.prepare('rainfall-2026-v1', root, ['nested/input.bin'], ['alice'])

    entry = candidate['datasets'][0]
    assert candidate['schema'] == 'researchkit.web-datasets/v1'
    assert entry['dataset_id'] == 'rainfall-2026-v1'
    assert entry['root'] == str(root)
    assert entry['readers'] == [{'uid': 1001, 'username': 'alice'}]
    assert entry['files'] == [{'path': 'nested/input.bin', 'size': 5,
                                'sha256': hashlib.sha256(b'alpha').hexdigest()}]


def test_existing_registry_is_preserved_and_new_entry_is_appended(tmp_path):
    root = tmp_path / 'data'; root.mkdir()
    (root / 'new.csv').write_bytes(b'new')
    registry = tmp_path / 'registry.json'
    original = _registry(registry, [{
        'dataset_id': 'old-v1', 'root': str(root), 'readers': [{'uid': 1001, 'username': 'alice'}],
        'files': [{'path': 'old.csv', 'size': 3, 'sha256': 'a' * 64}],
    }])
    before = registry.read_bytes()

    candidate = prepare_tool.prepare('new-v1', root, ['new.csv'], ['alice'], registry)

    assert candidate['datasets'][0] == original['datasets'][0]
    assert candidate['datasets'][1]['dataset_id'] == 'new-v1'
    assert registry.read_bytes() == before


def test_rejects_system_or_root_reader(monkeypatch, tmp_path):
    root = tmp_path / 'data'; root.mkdir(); (root / 'input').write_bytes(b'x')
    monkeypatch.setattr(prepare_tool.pwd, 'getpwnam',
                        lambda name: SimpleNamespace(pw_name=name, pw_uid=999, pw_shell='/usr/sbin/nologin'))
    with pytest.raises(prepare_tool.PrepareError, match='UID >= 1000'):
        prepare_tool.prepare('demo-v1', root, ['input'], ['service'])
    monkeypatch.setattr(prepare_tool.pwd, 'getpwnam',
                        lambda name: SimpleNamespace(pw_name=name, pw_uid=1001, pw_shell='/usr/sbin/nologin'))
    with pytest.raises(prepare_tool.PrepareError, match='登录用户'):
        prepare_tool.prepare('demo-v1', root, ['input'], ['service'])


def test_rejects_symlink_and_hardlinked_selected_files(tmp_path):
    root = tmp_path / 'data'; root.mkdir()
    target = root / 'target'; target.write_bytes(b'x')
    (root / 'link').symlink_to(target)
    with pytest.raises(prepare_tool.PrepareError):
        prepare_tool.prepare('link-v1', root, ['link'], ['alice'])
    os.link(target, root / 'hard')
    with pytest.raises(prepare_tool.PrepareError, match='硬链接'):
        prepare_tool.prepare('hard-v1', root, ['hard'], ['alice'])
    os.mkfifo(root / 'fifo')
    with pytest.raises(prepare_tool.PrepareError, match='普通文件'):
        prepare_tool.prepare('fifo-v1', root, ['fifo'], ['alice'])


def test_rejects_size_limit_without_allocating_large_data(monkeypatch, tmp_path):
    root = tmp_path / 'data'; root.mkdir(); (root / 'input').write_bytes(b'xx')
    monkeypatch.setattr(prepare_tool, 'MAX_FILE_BYTES', 1)
    with pytest.raises(prepare_tool.PrepareError, match='512 MiB'):
        prepare_tool.prepare('limit-v1', root, ['input'], ['alice'])


def test_duplicate_id_and_duplicate_json_registry_are_rejected_without_changes(tmp_path):
    root = tmp_path / 'data'; root.mkdir(); (root / 'input').write_bytes(b'x')
    registry = tmp_path / 'registry.json'
    _registry(registry, [{
        'dataset_id': 'exists-v1', 'root': str(root), 'readers': [{'uid': 1001, 'username': 'alice'}],
        'files': [],
    }])
    before = registry.read_bytes()
    with pytest.raises(prepare_tool.PrepareError, match='已存在'):
        prepare_tool.prepare('exists-v1', root, ['input'], ['alice'], registry)
    assert registry.read_bytes() == before
    registry.write_text('{"schema":"researchkit.web-datasets/v1","schema":"x","datasets":[]}', encoding='utf-8')
    duplicate_before = registry.read_bytes()
    with pytest.raises(prepare_tool.PrepareError, match='重复键'):
        prepare_tool.prepare('fresh-v1', root, ['input'], ['alice'], registry)
    assert registry.read_bytes() == duplicate_before


def test_cli_writes_only_a_complete_json_candidate(capsys, tmp_path):
    root = tmp_path / 'data'; root.mkdir(); (root / 'input').write_bytes(b'x')
    assert prepare_tool.main(['--dataset-id', 'cli-v1', '--root', str(root), '--file', 'input', '--reader', 'alice']) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted['schema'] == prepare_tool.SCHEMA
    assert emitted['datasets'][0]['files'][0]['path'] == 'input'
