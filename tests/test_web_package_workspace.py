"""Current-UID Workspace package materialization and recovery tests."""
from __future__ import annotations

import io
import os
import pwd
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_packages import pack_package
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


@pytest.fixture
def workspace():
    account = pwd.getpwuid(os.getuid())
    if account.pw_name != 'ly':
        pytest.skip('requires current configured teacher UID ly')
    root = Path(tempfile.mkdtemp(prefix='.rk-package-workspace-', dir=Path.cwd()))
    root.chmod(0o700)
    principal = Principal(account.pw_uid, account.pw_name, 'teacher')
    store = SubmissionStore(root / 'store')
    value = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        yield value
    finally:
        shutil.rmtree(root)


def _ordinary_zip():
    return pack_package({
        'project.yaml': b'schema: researchkit.project/v1\nname: zip\nmode: python\nentry: case.py\nplugins: {}\nparameters: {}\ndata: {}\nrequires: {python: "3.11", modules: [], minimums: {}}\n',
        'case.py': b'def run(context):\n    return {}\n',
    })


def _teacher_package(workspace: Workspace, name='bundle'):
    project = workspace.create_project(name)
    frozen = workspace.submit(project['project_id'], 'major')
    return project, frozen, workspace.export_package(frozen['submission_id'])


def test_ordinary_and_unassigned_teacher_packages_materialize(workspace):
    ordinary = workspace.import_package('ordinary imported', _ordinary_zip())
    assert ordinary['source'] == 'self-created'
    assert Path(ordinary['path']).name.startswith('imported-')
    _source, _frozen, package = _teacher_package(workspace, 'unassigned source')
    unassigned = workspace.import_package('teacher package', package)
    assert unassigned['source'] == 'teacher-unassigned'
    assert Path(unassigned['path']).name.startswith('imported-')


def test_assigned_import_reuses_claim_and_confirmation_retry_preserves_ide_edit(workspace, monkeypatch):
    _source, frozen, package = _teacher_package(workspace, 'assigned source')
    distribution = workspace.create_distribution(frozen['submission_id'], 'ly')
    request = workspace.store.request
    with monkeypatch.context() as patch:
        patch.setattr(workspace.store, 'request', lambda method, **params: (_ for _ in ()).throw(KitError('confirm interrupted')) if method == 'confirm_distribution' else request(method, **params))
        with pytest.raises(KitError, match='interrupted'):
            workspace.import_package('assigned import', package)
    project_id = workspace.store.request('claim_distribution', distribution_id=distribution['distribution_id'])['project']['project_id']
    path = workspace._project_path(project_id)
    (path / 'case.py').write_text('# IDE edit survives\n', encoding='utf-8')
    recovered = workspace.import_package('retry', package)
    assert recovered['project_id'] == project_id
    assert recovered['source'] == 'teacher-assigned'
    assert (path / 'case.py').read_text(encoding='utf-8') == '# IDE edit survives\n'


def test_state_save_failure_recovers_exact_directory_but_never_recreates_or_overwrites(workspace, monkeypatch):
    _source, _frozen, package = _teacher_package(workspace, 'recover source')
    original = workspace._save_state
    with monkeypatch.context() as patch:
        patch.setattr(workspace, '_save_state', lambda _projects: (_ for _ in ()).throw(OSError('state write failed')))
        with pytest.raises(OSError, match='state write failed'):
            workspace.import_package('recover import', package)
    destination = next(workspace.projects_root.glob('imported-*'))
    retried = workspace.import_package('recover import', package)
    assert Path(retried['path']) == destination
    moved = destination.with_name(destination.name + '-moved')
    destination.rename(moved)
    with pytest.raises(KitError):
        workspace.import_package('moved retry', package)
    assert not destination.exists() and (moved / 'case.py').is_file()

    # A crash before state save followed by an IDE edit must not overwrite it.
    _source, _frozen, other = _teacher_package(workspace, 'modified recover source')
    prior = set(workspace.projects_root.glob('imported-*'))
    with monkeypatch.context() as patch:
        patch.setattr(workspace, '_save_state', lambda _projects: (_ for _ in ()).throw(OSError('state write failed')))
        with pytest.raises(OSError):
            workspace.import_package('modified recover import', other)
    created = set(workspace.projects_root.glob('imported-*')) - prior
    assert len(created) == 1
    changed = created.pop() / 'case.py'
    changed.write_text('# do not replace\n', encoding='utf-8')
    with pytest.raises(KitError, match='已有修改'):
        workspace.import_package('modified retry', other)
    assert changed.read_text(encoding='utf-8') == '# do not replace\n'


def test_malicious_zip_is_rejected_before_store_project_creation(workspace):
    store = workspace.store.store
    connection = store._connect()
    try:
        before = connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    finally:
        connection.close()
    # Traversal is rejected by the codec before Workspace contacts the store.
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, 'w') as archive:
        archive.writestr('../project.yaml', b'x')
    with pytest.raises(KitError):
        workspace.import_package('traversal', traversal.getvalue())
    link = io.BytesIO()
    with zipfile.ZipFile(link, 'w') as archive:
        info = zipfile.ZipInfo('project.yaml')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b'outside')
    with pytest.raises(KitError):
        workspace.import_package('link', link.getvalue())
    connection = store._connect()
    try:
        after = connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    finally:
        connection.close()
    assert after == before
