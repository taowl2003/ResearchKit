"""Direct platform cleanup keeps registries and data bytes intact."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from researchkit import web_datasets
from researchkit.web_cleanup import Cleanup
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')


def test_retired_builtin_case_is_manageable_and_releases_algorithm_only_after_deletion():
    from researchkit.web_sharing import ComponentService
    source = Path(__file__).resolve().parents[1] / 'library'
    with tempfile.TemporaryDirectory(prefix='.rk-inactive-builtin-', dir=Path.cwd()) as temp:
        root = Path(temp)
        library = root / 'library'
        for section in ('algorithms', 'contracts', 'cases'):
            shutil.copytree(source / section, library / section)
        store = SubmissionStore(root / 'store')
        sharing = ComponentService(store, library)
        cleanup = Cleanup(store, library)
        case_ref = 'hello@1.0.0'
        before = cleanup.lifecycle.report(TEACHER, 'template', case_ref)
        cleanup.lifecycle.change(TEACHER, 'template', case_ref, 'retire', before['revision'])
        assert sharing.catalog(TEACHER)['templates'] == []
        assert any(row['ref'] == case_ref and row['lifecycle']['state'] == 'retired'
                   for row in sharing.access(TEACHER, include_inactive=True))
        blocked = cleanup.check_cleanup_references(TEACHER, 'algorithm', 'example.scale@1.0.0')
        assert not blocked['can_delete'] and blocked['blocking_cases'][0]['ref'] == case_ref
        cleanup.direct_cleanup(TEACHER, 'template', case_ref, '永久清理')
        assert cleanup.check_cleanup_references(TEACHER, 'algorithm', 'example.scale@1.0.0')['can_delete']
        cleanup.direct_cleanup(TEACHER, 'algorithm', 'example.scale@1.0.0', '永久清理')
        # Central deletion must not revive the imported case on a fresh read,
        # and must not unlink the legacy CLI source tree or any user project.
        assert ComponentService(store, library).access(TEACHER, include_inactive=True) == []
        assert (library / 'cases/hello/1.0.0/project.yaml').is_file()
        assert (library / 'algorithms/example.scale/1.0.0/plugin.py').is_file()


def test_raw_dataset_removal_only_retires_platform_selection(tmp_path: Path, monkeypatch) -> None:
    # pytest's temporary parent is intentionally writable.  Registry path
    # hardening is covered by web_datasets tests; this test isolates direct
    # cleanup's no-unlink contract.
    monkeypatch.setattr(web_datasets, '_safe_info', lambda *_args, **_kwargs: None)
    raw_root = tmp_path / 'raw'
    raw_root.mkdir()
    payload = b'value\n1\n'
    source = raw_root / 'measurements.csv'
    source.write_bytes(payload)
    registry = tmp_path / 'datasets.json'
    registry.write_text(json.dumps({
        'schema': 'researchkit.web-datasets/v1',
        'datasets': [{
            'dataset_id': 'raw-practice-v1',
            'root': str(raw_root),
            'readers': [{'uid': TEACHER.uid, 'username': TEACHER.username}],
            'files': [{
                'path': 'measurements.csv', 'size': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest(),
            }],
        }],
    }))
    registry_before = registry.read_bytes()
    store = SubmissionStore(tmp_path / 'store')
    policy = SimpleNamespace(
        derived_dataset_root=None,
        dataset_tree_registry=None,
        dataset_registry=registry,
        dataset_metadata=None,
    )
    cleanup = Cleanup(store, policy=policy)

    result = cleanup.direct_cleanup(TEACHER, 'dataset', 'raw-practice-v1', '永久清理')

    assert result['deleted'] is True
    assert result['platform_removed'] is True
    assert result['physical_files_deleted'] is False
    assert result['manual_cleanup']['path'] == str(source)
    assert 'root' in result['manual_cleanup']['message']
    assert source.read_bytes() == payload
    assert registry.read_bytes() == registry_before
    assert cleanup.lifecycle.summary('dataset', 'raw-practice-v1')['state'] == 'retired'
    # The already-retired record remains safe to retry and remains physically
    # untouched; no browser request can substitute a different path.
    again = cleanup.direct_cleanup(TEACHER, 'dataset', 'raw-practice-v1', '永久清理')
    assert again['physical_files_deleted'] is False
    assert source.read_bytes() == payload
