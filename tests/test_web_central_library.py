from __future__ import annotations

import os
import shutil
import tempfile
import hashlib
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_asset_library import AssetLibrary
from researchkit.web_assets import AssetService
from researchkit.web_central_library import CentralLibrary
from researchkit.web_identity import Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(2001, 'student', 'student')


@pytest.fixture
def safe_root():
    root = Path(tempfile.mkdtemp(prefix='.rk-central-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _library(tmp_path: Path) -> Path:
    target = tmp_path / 'library'
    source = Path(__file__).resolve().parents[1] / 'library'
    for name in ('algorithms', 'contracts', 'cases'):
        shutil.copytree(source / name, target / name)
    for path in (target, *target.rglob('*')):
        os.chown(path, os.geteuid(), -1)
    return target


def test_static_library_is_imported_once_then_served_without_source_tree(safe_root: Path) -> None:
    root = _library(safe_root)
    store = SubmissionStore(safe_root / 'store')
    library = AssetLibrary(store, root)
    report = library.central.report()
    assert report['imported'] is True and report['origin'] == 'builtin'
    assert report['assets'] == 2 and report['contracts'] == 1
    assert {row['ref'] for row in library.catalog()['plugins']} == {'example.scale@1.0.0'}
    assert {row['ref'] for row in library.catalog()['templates']} == {'hello@1.0.0'}

    # The retained on-disk CLI library can disappear or be replaced without
    # changing already imported Web bytes.
    for name in ('algorithms', 'contracts', 'cases'):
        shutil.rmtree(root / name)
    assert library._plugin_source('example.scale@1.0.0')[1]['plugin.py']
    assert library._template_source('hello@1.0.0')[1]['case.py']
    assert library._contract_source('scalar@1.0.0')[1]
    recreated = AssetLibrary(store, root)
    assert recreated.catalog() == library.catalog()
    assert recreated.central.report()['source_revision'] == report['source_revision']


def test_import_failure_is_not_marked_or_partially_committed(safe_root: Path) -> None:
    root = _library(safe_root)
    plugin = root / 'algorithms' / 'example.scale' / '1.0.0' / 'plugin.yaml'
    plugin.write_text(plugin.read_text(encoding='utf-8').replace(
        'scalar@1.0.0', 'missing@1.0.0'), encoding='utf-8')
    store = SubmissionStore(safe_root / 'store')
    with pytest.raises(KitError, match='依赖的契约不存在'):
        CentralLibrary(store, root)
    with store._connection() as con:
        assert con.execute('SELECT count(*) FROM web_central_builtin_assets').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM web_central_builtin_contracts').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM web_central_library_state').fetchone()[0] == 0


def test_component_policy_survives_central_import_and_static_source_removal(safe_root: Path) -> None:
    root = _library(safe_root)
    store = SubmissionStore(safe_root / 'store')
    service = ComponentService(store, root)
    policy = next(row for row in service.access(TEACHER)
                  if row['kind'] == 'algorithm' and row['ref'] == 'example.scale@1.0.0')
    service.set_access(TEACHER, 'algorithm', 'example.scale@1.0.0', False, [], policy['revision'])
    for name in ('algorithms', 'contracts', 'cases'):
        shutil.rmtree(root / name)
    recreated = ComponentService(store, root)
    with pytest.raises(KitError, match='无权'):
        recreated.component(STUDENT, 'example.scale@1.0.0')
    rows = recreated.access(TEACHER)
    assert next(row for row in rows if row['ref'] == 'example.scale@1.0.0')['public'] is False


def test_existing_published_ref_with_different_bytes_blocks_atomic_import(safe_root: Path) -> None:
    root = _library(safe_root)
    store = SubmissionStore(safe_root / 'store')
    # Provision legacy publication tables without creating the central marker.
    AssetService(store, root)
    with store._connection() as con:
        con.execute(
            "INSERT INTO web_asset_published(kind,ref,candidate_id,asset_id,published_at) "
            "VALUES ('algorithm','example.scale@1.0.0','legacy','legacy','now')"
        )
        con.execute(
            "INSERT INTO web_asset_published_files(kind,ref,path,sha256,size,content) "
            "VALUES ('algorithm','example.scale@1.0.0','plugin/plugin.py',?,?,?)",
            (hashlib.sha256(b'bad').hexdigest(), 3, b'bad'),
        )
    with pytest.raises(KitError, match='内容不同'):
        CentralLibrary(store, root)
    with store._connection() as con:
        assert con.execute('SELECT count(*) FROM web_central_builtin_assets').fetchone()[0] == 0


def test_initial_import_preserves_semantically_equal_legacy_contract_format(safe_root: Path):
    import hashlib
    import json
    import yaml
    library_root=_library(safe_root)
    store=SubmissionStore(safe_root/'store')
    from researchkit.web_assets import AssetService
    AssetService(store,library_root)
    document=yaml.safe_load((library_root/'contracts/scalar/1.0.0.yaml').read_bytes())
    reformatted=yaml.safe_dump(document,sort_keys=True).encode()
    with store._connection() as con:
        con.execute('INSERT INTO web_asset_contracts(contract_ref,sha256,content,contract_json,candidate_id) VALUES(?,?,?,?,?)',
                    ('scalar@1.0.0',hashlib.sha256(reformatted).hexdigest(),reformatted,json.dumps(document),'legacy-source'))
    central=CentralLibrary(store,library_root)
    assert central.report()['imported']
    assert central.contract('scalar@1.0.0')[0]==document
    with store._connection() as con:
        assert bytes(con.execute('SELECT content FROM web_asset_contracts').fetchone()[0])==reformatted
