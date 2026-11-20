from __future__ import annotations

import os
import pwd
import shutil
import stat
import tempfile
from unittest import mock
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def _principal() -> Principal:
    username = pwd.getpwuid(os.getuid()).pw_name
    return Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')


@pytest.fixture
def workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-web-test-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    principal = _principal()
    store = SubmissionStore(root / 'store')
    value = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        yield value
    finally:
        shutil.rmtree(root)


def test_same_ide_workspace_has_cas_edit_and_immutable_broken_submission(workspace: Workspace):
    project = workspace.create_project('bad project')
    project_id = project['project_id']
    opened = workspace.read_text(project_id, 'case.py')
    path = Path(project['path']) / 'case.py'
    path.write_text('def incomplete(:\n', encoding='utf-8')
    with pytest.raises(KitError, match='编辑冲突'):
        workspace.save_text(project_id, 'case.py', 'replacement', opened['sha256'])
    reloaded = workspace.read_text(project_id, 'case.py')
    workspace.save_text(project_id, 'case.py', 'def incomplete(:\n# still broken\n', reloaded['sha256'])
    first = workspace.submit(project_id, 'minor')
    path.write_text('changed later', encoding='utf-8')
    second = workspace.submit(project_id, 'major')
    assert first['version'] == '0.1'
    assert second['version'] == '1.0'
    assert workspace.get_submission(first['submission_id'], 'case.py')['text'] == 'def incomplete(:\n# still broken\n'


def test_workspace_rejects_links_and_protected_writes(workspace: Workspace):
    project = workspace.create_project('links')
    root = Path(project['path'])
    (root / 'outside').symlink_to('/etc')
    with pytest.raises(KitError, match='软链接'):
        workspace.describe_project(project['project_id'])
    (root / 'outside').unlink()
    protected = workspace.read_text(project['project_id'], '.researchkit/lock.json')
    assert protected['editable'] is False  # read API permits dependency inspection; project listing marks it readonly.
    with pytest.raises(KitError, match='只读'):
        workspace.save_text(project['project_id'], '.researchkit/lock.json', '{}', protected['sha256'])


def test_existing_project_registration_stays_inside_projects_root(workspace: Workspace):
    with pytest.raises(KitError, match='projects'):
        workspace.register_existing('bad', 'outside')
    existing = workspace.projects_root / 'existing'
    existing.mkdir(mode=0o700)
    (existing / 'project.yaml').write_text('schema: researchkit.project/v1\n', encoding='utf-8')
    registered = workspace.register_existing('registered', 'projects/existing')
    assert registered['relative_path'] == 'projects/existing'


def test_project_description_does_not_follow_yaml_replaced_after_listing(workspace: Workspace, monkeypatch):
    project = workspace.create_project('yaml race')
    root = Path(project['path'])
    outside = workspace.root / 'outside.yaml'
    outside.write_text('private: must-not-read\n', encoding='utf-8')
    original = workspace._check_tree

    def replace_after_listing(root_fd, *, collect):
        result = original(root_fd, collect=collect)
        (root / 'project.yaml').unlink()
        (root / 'project.yaml').symlink_to(outside)
        return result

    monkeypatch.setattr(workspace, '_check_tree', replace_after_listing)
    assert workspace.describe_project(project['project_id'])['project_yaml'] is None


def test_read_rejects_parent_symlink_and_tree_rejects_hardlink(workspace: Workspace):
    project = workspace.create_project('descriptor paths')
    root = Path(project['path'])
    (root / 'alias').symlink_to(root, target_is_directory=True)
    with pytest.raises(KitError, match='父目录'):
        workspace.read_text(project['project_id'], 'alias/case.py')
    (root / 'alias').unlink()
    os.link(root / 'case.py', root / 'case-copy.py')
    with pytest.raises(KitError, match='无链接'):
        workspace.describe_project(project['project_id'])


def test_save_rejects_directory_replacement_after_cas_read(workspace: Workspace, monkeypatch):
    project = workspace.create_project('directory race')
    root = Path(project['path'])
    editable = root / 'editable'
    editable.mkdir()
    source = editable / 'note.py'
    source.write_text('first', encoding='utf-8')
    opened = workspace.read_text(project['project_id'], 'editable/note.py')
    original = workspace._read_regular_from_parent
    calls = 0

    def swap_after_second_read(parent_fd, name, *, limit):
        nonlocal calls
        answer = original(parent_fd, name, limit=limit)
        calls += 1
        if calls == 2:
            old = root / 'editable-old'
            os.rename(editable, old)
            editable.mkdir()
            (editable / 'note.py').write_text('replacement', encoding='utf-8')
        return answer

    monkeypatch.setattr(workspace, '_read_regular_from_parent', swap_after_second_read)
    with pytest.raises(KitError, match='父目录'):
        workspace.save_text(project['project_id'], 'editable/note.py', 'web update', opened['sha256'])
    assert (editable / 'note.py').read_text(encoding='utf-8') == 'replacement'
