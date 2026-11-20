"""Dataset registry tests use temporary files and fabricated Principals only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_datasets import DatasetRegistry
from researchkit.web_identity import Principal


TEACHER = Principal(1000, 'ly', 'teacher')
ALICE = Principal(1001, 'alice', 'student')
BOB = Principal(1002, 'bob', 'student')


@pytest.fixture
def root():
    value = Path(tempfile.mkdtemp(prefix='.rk-datasets-', dir=Path.cwd()))
    os.chmod(value, 0o700)
    try:
        yield value
    finally:
        shutil.rmtree(value)


@pytest.fixture
def trusted(monkeypatch):
    import researchkit.web_datasets as web_datasets

    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid in {0, os.geteuid()})


def _reference(dataset_id: str, path: str, content: bytes) -> dict[str, object]:
    return {'dataset_id': dataset_id, 'path': path, 'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest()}


def _registry(root: Path, *, readers=None, files=None, dataset_id='demo') -> tuple[Path, list[dict[str, object]]]:
    data = root / 'data'
    data.mkdir(parents=True)
    contents = files or {'input/a.txt': b'alpha'}
    references = []
    for path, content in contents.items():
        target = data / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        references.append(_reference(dataset_id, path, content))
    document = {'schema': 'researchkit.web-datasets/v1', 'datasets': [{
        'dataset_id': dataset_id, 'root': str(data),
        'readers': readers if readers is not None else [{'uid': ALICE.uid, 'username': ALICE.username}],
        'files': [{key: item[key] for key in ('path', 'size', 'sha256')} for item in references],
    }]}
    path = root / 'datasets.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path, references


def test_catalog_and_authorize_require_exact_uid_username_and_recheck_revocation(root, trusted):
    path, references = _registry(root)
    registry = DatasetRegistry(path)
    assert registry.catalog(ALICE) == [{'dataset_id': 'demo', 'files': references}]
    assert registry.catalog(TEACHER) == []  # teacher has no implicit data access
    assert registry.catalog(Principal(ALICE.uid, 'reused-name', 'student')) == []
    assert registry.authorize([ALICE], references) == references
    assert registry.authorize([ALICE, ALICE], references) == references
    with pytest.raises(KitError, match='无权'):
        registry.authorize([TEACHER], references)
    with pytest.raises(KitError, match='无权'):
        registry.authorize([ALICE, BOB], references)

    document = json.loads(path.read_text(encoding='utf-8'))
    document['datasets'][0]['readers'] = []
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(KitError, match='无权'):
        registry.authorize([ALICE], references)


def test_open_references_verifies_bytes_closes_fds_and_detects_drift(root, trusted):
    path, references = _registry(root)
    registry = DatasetRegistry(path)
    with registry.open_references([ALICE], references) as mounts:
        assert mounts[0].target == '/datasets/demo/input/a.txt'
        assert os.read(mounts[0].fd, 16) == b'alpha'
        descriptor = mounts[0].fd
    with pytest.raises(OSError):
        os.fstat(descriptor)

    with pytest.raises(KitError, match='变化'):
        with registry.open_references([ALICE], references) as mounts:
            changed = root / 'data/input/a.txt'
            changed.write_bytes(b'other')
            information = changed.stat()
            os.utime(changed, ns=(information.st_atime_ns, information.st_mtime_ns + 1_000_000_000))
            assert mounts


def test_open_references_rejects_cancel_missing_and_limits(root, trusted, monkeypatch):
    import researchkit.web_datasets as web_datasets

    path, references = _registry(root, files={'a.txt': b'a', 'b.txt': b'b'})
    registry = DatasetRegistry(path)
    with pytest.raises(KitError, match='取消'):
        with registry.open_references([ALICE], references, cancelled=lambda: True):
            pass
    missing = dict(references[0])
    missing['path'] = 'missing.txt'
    with pytest.raises(KitError, match='不匹配'):
        registry.authorize([ALICE], [missing])
    monkeypatch.setattr(web_datasets, 'MAX_MOUNTS', 1)
    with pytest.raises(KitError, match='挂载数'):
        registry.authorize([ALICE], references)


def test_registry_and_open_trust_checks_reject_nonroot_writable_links_and_hardlinks(root, trusted, monkeypatch):
    path, references = _registry(root)
    # Production trust is root-only; no test override is active for this call.
    import researchkit.web_datasets as web_datasets
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid == 0)
    with pytest.raises(KitError, match='不受信任'):
        DatasetRegistry(path).catalog(ALICE)
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid in {0, os.geteuid()})

    os.chmod(root / 'data', 0o777)
    with pytest.raises(KitError, match='不受信任'):
        with DatasetRegistry(path).open_references([ALICE], references):
            pass
    os.chmod(root / 'data', 0o755)

    target = root / 'data/input/a.txt'
    target.unlink()
    target.symlink_to(root / 'outside')
    with pytest.raises(KitError):
        with DatasetRegistry(path).open_references([ALICE], references):
            pass

    target.unlink()
    target.write_bytes(b'alpha')
    os.link(target, root / 'data/input/linked.txt')
    document = json.loads(path.read_text(encoding='utf-8'))
    document['datasets'][0]['files'] = [{'path': 'input/linked.txt', 'size': 5,
                                         'sha256': hashlib.sha256(b'alpha').hexdigest()}]
    path.write_text(json.dumps(document), encoding='utf-8')
    reference = _reference('demo', 'input/linked.txt', b'alpha')
    with pytest.raises(KitError, match='硬链接'):
        with DatasetRegistry(path).open_references([ALICE], [reference]):
            pass


def test_registry_rejects_duplicate_json_prefix_conflict_and_no_data(root, trusted):
    path, _references = _registry(root)
    path.write_text('{"schema":"researchkit.web-datasets/v1","schema":"x","datasets":[]}', encoding='utf-8')
    with pytest.raises(KitError, match='重复键'):
        DatasetRegistry(path).catalog(ALICE)

    path, _references = _registry(root / 'second', files={'a': b'a'})
    document = json.loads(path.read_text(encoding='utf-8'))
    document['datasets'][0]['files'].append({'path': 'a/b', 'size': 1,
                                              'sha256': hashlib.sha256(b'b').hexdigest()})
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(KitError, match='前缀冲突'):
        DatasetRegistry(path).catalog(ALICE)

    path, _references = _registry(root / 'third')
    document = json.loads(path.read_text(encoding='utf-8'))
    document['datasets'][0]['dataset_id'] = 'bad/id'
    document['datasets'][0]['files'] = []
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(KitError, match='ID'):
        DatasetRegistry(path).catalog(ALICE)
    assert DatasetRegistry(None).catalog(ALICE) == []
    assert DatasetRegistry(None).authorize([ALICE], []) == []


def test_preparation_timeout_missing_file_total_limit_and_mid_hash_revocation(root, trusted, monkeypatch):
    import researchkit.web_datasets as module
    path, references = _registry(root, files={'a.txt': b'a', 'b.txt': b'b'})
    registry = DatasetRegistry(path)
    with monkeypatch.context() as patch:
        moments = iter([0.0, 31.0])
        patch.setattr(module.time, 'monotonic', lambda: next(moments))
        with pytest.raises(module.DatasetTimedOut):
            with registry.open_references([ALICE], references):
                pytest.fail('timeout allowed execution')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MAX_TOTAL_BYTES', 1)
        with pytest.raises(KitError, match='总量'):
            registry.authorize([ALICE], references)
    (root / 'data/b.txt').unlink()
    with pytest.raises(KitError, match='不可读取'):
        with registry.open_references([ALICE], references):
            pytest.fail('missing file allowed execution')
    (root / 'data/b.txt').write_bytes(b'b')
    def revoke():
        document = json.loads(path.read_text())
        document['datasets'][0]['readers'] = []
        path.write_text(json.dumps(document))
        return False
    with pytest.raises(KitError, match='无权'):
        with registry.open_references([ALICE], references, cancelled=revoke):
            pytest.fail('revoked grant allowed execution')
