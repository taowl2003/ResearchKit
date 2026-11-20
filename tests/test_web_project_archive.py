"""Personal project archival leaves immutable and IDE contents intact."""
from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient, application
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def make_workspace(root):
    username = pwd.getpwuid(os.getuid()).pw_name
    principal = Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')
    store = SubmissionStore(root / 'store')
    return Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))


@pytest.fixture
def workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-archive-test-', dir=Path.cwd()))
    try:
        yield make_workspace(root)
    finally:
        shutil.rmtree(root)


def test_persisted_hide_restore_preserves_index_files_and_fixed_submission(workspace):
    project = workspace.create_project('archive me')
    project_id = project['project_id']
    fixed = workspace.submit(project_id, 'major')
    state = workspace._state_path.read_bytes()
    original = (Path(project['path']) / 'case.py').read_bytes()
    assert workspace.set_project_archived(project_id, True) == {'project_id': project_id, 'archived': True}
    assert workspace.list_projects() == []
    again = Workspace(workspace.principal, workspace.root, workspace.store)
    assert again.list_projects() == []
    assert again.list_projects(include_archived=True)[0]['archived'] is True
    assert workspace._state_path.read_bytes() == state
    assert (Path(project['path']) / 'case.py').read_bytes() == original
    assert workspace.get_submission(fixed['submission_id'], 'case.py')['text'].encode() == original
    assert workspace.list_submission_projects()['items'][0]['project']['project_id'] == project_id
    workspace.set_project_archived(project_id, False)
    workspace.set_project_archived(project_id, False)
    assert workspace.list_projects()[0]['archived'] is False
    assert workspace._archive_path.stat().st_mode & 0o777 == 0o600


def test_unavailable_project_can_be_restored_without_filesystem_access(workspace):
    project = workspace.create_project('missing project')
    workspace.set_project_archived(project['project_id'], True)
    shutil.rmtree(project['path'])
    workspace.set_project_archived(project['project_id'], False)
    row = workspace.list_projects()[0]
    assert row['unavailable'] is True and row['name'] == 'missing project'


def test_unknown_or_forged_foreign_project_cannot_be_archived(workspace):
    with pytest.raises(KitError, match='工作区'):
        workspace.set_project_archived('missing', True)
    other = Principal(workspace.principal.uid + 1000, 'other-student', 'student')
    foreign = workspace.store.store.create_project(other, 'foreign')
    with workspace._state_lock():
        state = workspace._load_state()
        state[foreign['project_id']] = {'path': 'projects/fake', 'name': 'foreign'}
        workspace._save_state(state)
    with pytest.raises(KitError):
        workspace.set_project_archived(foreign['project_id'], True)
    assert not workspace._archive_path.exists()


@pytest.mark.parametrize('unsafe', ['link', 'dangling', 'hardlink', 'mode', 'json', 'identity', 'fifo'])
def test_unsafe_archive_metadata_rejected_without_overwrite(workspace, unsafe):
    project = workspace.create_project('safety')
    path = workspace._archive_path
    outside = workspace.root / 'outside.json'
    if unsafe in {'link', 'hardlink'}:
        outside.write_text('{}')
        outside.chmod(0o600)
        if unsafe == 'link':
            path.symlink_to(outside)
        else:
            os.link(outside, path)
    elif unsafe == 'dangling':
        path.symlink_to(workspace.root / 'missing.json')
    elif unsafe == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.write_text('not json' if unsafe == 'json' else json.dumps({
            'schema': 'researchkit.web-project-archive/v1', 'uid': workspace.principal.uid,
            'username': 'wrong' if unsafe == 'identity' else workspace.principal.username, 'projects': []}))
        path.chmod(0o644 if unsafe == 'mode' else 0o600)
    with pytest.raises(KitError, match='归档索引'):
        workspace.list_projects()
    with pytest.raises(KitError, match='归档索引'):
        workspace.set_project_archived(project['project_id'], True)
    if outside.exists():
        assert outside.read_text() == '{}'


def test_concurrent_archives_do_not_overwrite_each_other(workspace):
    ids = [workspace.create_project(name)['project_id'] for name in ('first', 'second')]
    instances = [Workspace(workspace.principal, workspace.root, workspace.store) for _ in ids]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: pair[0].set_project_archived(pair[1], True), zip(instances, ids)))
    assert workspace.list_projects() == []
    assert {row['project_id'] for row in workspace.list_projects(include_archived=True)} == set(ids)


class TestArchiveHttp(AsyncHTTPTestCase):
    token = 'b' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-archive-http-', dir=Path.cwd()))
        self.workspace = make_workspace(self.root)
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def request_api(self, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        options = {'headers': headers}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            options.update(method='POST', body=json.dumps(body))
        return self.fetch('/api/' + path, **options)

    def test_archive_routes_and_strict_query(self):
        project = json.loads(self.request_api('projects', {'name': 'HTTP archive'}).body)
        route = 'projects/' + project['project_id']
        assert self.request_api(route + '/archive', {}).code == 200
        assert json.loads(self.request_api('projects').body) == []
        rows = json.loads(self.request_api('projects?include_archived=true').body)
        assert rows[0]['archived'] is True
        for query in ('include_archived=1', 'include_archived=true&include_archived=false', 'owner=other'):
            assert self.request_api('projects?' + query).code == 400
        assert self.request_api(route + '/restore', {'username': 'other'}).code == 400
        assert self.request_api(route + '/restore', {}).code == 200
        assert json.loads(self.request_api('projects').body)[0]['archived'] is False
        assert self.request_api('projects/missing/archive', {}).code == 400
