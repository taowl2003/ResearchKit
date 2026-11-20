from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore


def _teacher() -> Principal:
    return Principal(1001, 'ly', 'teacher')


def _student(uid: int, username: str) -> Principal:
    return Principal(uid, username, 'student')


def _service_owned(root: Path) -> None:
    """Provision the test system library for the UID running the broker."""
    for path in (root, *root.rglob('*')):
        os.chown(path, os.geteuid(), -1)


@pytest.fixture
def safe_root():
    root = Path(tempfile.mkdtemp(prefix='.rk-sharing-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def sharing(safe_root: Path):
    library = safe_root / 'library'
    source = Path(__file__).resolve().parents[1] / 'library'
    for name in ('algorithms', 'contracts', 'cases'):
        shutil.copytree(source / name, library / name)
    _service_owned(library)
    store = SubmissionStore(safe_root / 'store')
    return store, ComponentService(store, library), library


def _policy(service: ComponentService, ref: str, kind: str = 'algorithm') -> dict[str, object]:
    return next(row for row in service.access(_teacher())
                if row['kind'] == kind and row['ref'] == ref)


def _private_plugin(library: Path, name: str = 'private.scale', version: str = '1.0.0') -> str:
    ref = f'{name}@{version}'
    root = library / 'algorithms' / name / version
    root.mkdir(parents=True)
    (root / 'plugin.yaml').write_text(
        'schema: hydro.plugin/v1\nname: ' + name + '\nversion: ' + version +
        '\nentry: plugin.py\ninputs:\n  value: scalar@1.0.0\noutputs:\n  value: scalar@1.0.0\nparameters: {}\n',
        encoding='utf-8')
    (root / 'plugin.py').write_text('raise RuntimeError("must never execute")\n', encoding='utf-8')
    return ref


def test_students_get_only_exactly_granted_components_and_contracts(sharing):
    _store, service, library = sharing
    alice, bob = _student(2001, 'alice'), _student(2002, 'bob')
    private = _private_plugin(library)
    service = ComponentService(SubmissionStore(library.parent / 'private-store'), library)
    assert private not in {row['ref'] for row in service.catalog(alice)['plugins']}
    with pytest.raises(KitError, match='无权'):
        service.component(alice, private)  # probing a source ref cannot bypass policy
    before = _policy(service, private)
    service.set_access(_teacher(), 'algorithm', private, False, [alice], before['revision'])
    assert {row['ref'] for row in service.catalog(alice)['plugins']} == {
        'example.scale@1.0.0', private}
    assert {row['id'] for row in service.catalog(alice)['contracts']} == {'scalar@1.0.0'}
    assert private not in {row['ref'] for row in service.catalog(bob)['plugins']}
    result = service.component(alice, private)
    assert result['metadata']['name'] == 'private.scale'
    assert result['files']['contracts/scalar/1.0.0.yaml']
    # scalar remains available through the public example plugin.  An exact
    # contract ref outside every allowed algorithm remains hidden.
    with pytest.raises(KitError, match='无权'):
        service.contract(bob, 'private.contract@1.0.0')


def test_versions_are_private_by_default_and_uid_and_username_must_both_match(sharing):
    _store, service, library = sharing
    ref = _private_plugin(library, 'versioned', '2.0.0')
    service = ComponentService(SubmissionStore(library.parent / 'versioned-store'), library)
    student = _student(2101, 'alice')
    same_uid, same_name = _student(2101, 'other'), _student(2102, 'alice')
    policy = _policy(service, ref)
    service.set_access(_teacher(), 'algorithm', ref, False, [student], policy['revision'])
    assert service.component(student, ref)['ref'] == ref
    for impersonator in (same_uid, same_name):
        with pytest.raises(KitError, match='无权'):
            service.component(impersonator, ref)
    with pytest.raises(KitError, match='无权'):
        service.component(student, 'versioned@1.0.0')


def test_default_public_revocation_survives_recreate_and_teacher_only(sharing):
    store, service, library = sharing
    student = _student(2201, 'student')
    assert service.component(student, 'example.scale@1.0.0')['ref'] == 'example.scale@1.0.0'
    policy = _policy(service, 'example.scale@1.0.0')
    changed = service.set_access(_teacher(), 'algorithm', 'example.scale@1.0.0', False, [],
                                 policy['revision'])
    with pytest.raises(KitError, match='无权'):
        service.component(student, 'example.scale@1.0.0')
    recreated = ComponentService(store, library)
    assert _policy(recreated, 'example.scale@1.0.0')['public'] is False
    with pytest.raises(KitError, match='只有教师'):
        recreated.access(student)
    with pytest.raises(KitError, match='只有教师'):
        recreated.set_access(student, 'algorithm', 'example.scale@1.0.0', False, [], changed['revision'])


def test_access_cas(sharing):
    _store, service, library = sharing
    policy = _policy(service, 'hello@1.0.0', 'template')
    changed = service.set_access(_teacher(), 'template', 'hello@1.0.0', False, [], policy['revision'])
    with pytest.raises(KitError, match='冲突'):
        service.set_access(_teacher(), 'template', 'hello@1.0.0', True, [], policy['revision'])
    assert changed['revision'] != policy['revision']
    recreated = ComponentService(service.store, library)
    assert _policy(recreated, 'hello@1.0.0', 'template')['public'] is False


def test_component_tables_are_an_additive_migration(safe_root: Path):
    library = safe_root / 'library'
    source = Path(__file__).resolve().parents[1] / 'library'
    for name in ('algorithms', 'contracts', 'cases'):
        shutil.copytree(source / name, library / name)
    _service_owned(library)
    store = SubmissionStore(safe_root / 'store')  # Existing pre-sharing DB.
    project = store.create_project(_teacher(), 'pre-sharing')
    with store._connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'component_policies'").fetchone() is None
    service = ComponentService(store, library)
    assert _policy(service, 'example.scale@1.0.0')['public'] is True
    assert store.get_project(project['project_id'], _teacher())['name'] == 'pre-sharing'


def test_template_grants_are_static_and_do_not_execute_code(sharing):
    _store, service, library = sharing
    ref = 'private.template@1.0.0'
    root = library / 'cases' / 'private.template' / '1.0.0'
    root.mkdir(parents=True)
    (root / 'project.yaml').write_text(
        'schema: researchkit.project/v1\nname: private\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {}\n',
        encoding='utf-8')
    marker = library.parent / 'executed'
    (root / 'case.py').write_text(f"Path({str(marker)!r}).write_text('bad')\n", encoding='utf-8')
    service = ComponentService(SubmissionStore(library.parent / 'template-store'), library)
    student = _student(2301, 'template-student')
    with pytest.raises(KitError, match='无权'):
        service.template(student, ref)
    policy = _policy(service, ref, 'template')
    service.set_access(_teacher(), 'template', ref, False, [student], policy['revision'])
    result = service.template(student, ref)
    assert result['files']['case.py'].startswith(b'Path(')
    assert not marker.exists()
    assert {row['ref'] for row in service.catalog(student)['templates']} == {'hello@1.0.0', ref}


def test_template_catalog_absent_cases_and_unsafe_source_are_rejected(safe_root: Path):
    library = safe_root / 'library'
    source = Path(__file__).resolve().parents[1] / 'library'
    shutil.copytree(source / 'algorithms', library / 'algorithms')
    shutil.copytree(source / 'contracts', library / 'contracts')
    _service_owned(library)
    store = SubmissionStore(safe_root / 'store')
    service = ComponentService(store, library)
    assert service.catalog(_teacher())['templates'] == []
    link = library / 'algorithms' / 'bad'
    link.symlink_to(library / 'algorithms' / 'example.scale', target_is_directory=True)
    assert service.catalog(_teacher())['templates'] == []
    with pytest.raises(KitError, match='链接'):
        ComponentService(SubmissionStore(safe_root / 'unsafe-import-store'), library)


def test_student_local_draft_does_not_require_private_contract_read_permission(sharing):
    from researchkit.web_app import LocalStoreClient
    from researchkit.web_workspace import Workspace
    store, service, library = sharing
    # Provision the private contract before the one-time central import.
    private = library / 'contracts/private.value'
    private.mkdir()
    (private / '1.0.0.yaml').write_text('schema: hydro.contract/v1\nid: private.value@1.0.0\ntype: string\ndescription: secret\n')
    store = SubmissionStore(library.parent / 'private-contract-store')
    service = ComponentService(store,library)
    student = _student(2601, 'draft-student')
    ws = Workspace(student, library.parent / 'student-workspace',
                   LocalStoreClient(store, student, library_root=library))
    created = ws.create_project('local draft')
    record = ws.read_composition(created['project_id'])
    request = {'expected_revision': record['revision'], 'parameters': {}, 'steps': [],
               'draft_contracts': [{'id': 'student.value@1.0.0', 'type': 'number', 'description': 'local'}],
               'generate_entry': False}
    with pytest.raises(KitError, match='无权'):
        service.contract(student, 'student.value@1.0.0')
    ws.apply_composition(created['project_id'], request)
    assert (Path(created['path']) / '.researchkit/contracts/student.value/1.0.0.yaml').is_file()
    assert not (library / 'contracts/student.value').exists()
    # A private shared name cannot be redefined through a draft either.
    with pytest.raises(KitError, match='无权'):
        service.contract(student, 'private.value@1.0.0')
    with pytest.raises(KitError, match='冲突'):
        service.validate_drafts(student, [{'id': 'private.value@1.0.0', 'type': 'number', 'description': 'different'}])
