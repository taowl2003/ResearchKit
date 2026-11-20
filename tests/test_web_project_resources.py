"""Acceptance coverage for the owner-scoped project dependency inventory."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient, application
from researchkit.web_assets import AssetService
from researchkit.web_identity import Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


def _principal() -> Principal:
    username = pwd.getpwuid(os.getuid()).pw_name
    return Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')


@pytest.fixture
def project_workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-project-resources-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    principal = _principal()
    store = SubmissionStore(root / 'store')
    workspace = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        yield root, store, workspace
    finally:
        shutil.rmtree(root)


def _reference(dataset_id: str, path: str, content: bytes) -> dict[str, object]:
    return {
        'dataset_id': dataset_id,
        'path': path,
        'size': len(content),
        'sha256': hashlib.sha256(content).hexdigest(),
    }


def test_workspace_inventory_keeps_exact_algorithm_and_dataset_declarations(project_workspace, monkeypatch) -> None:
    _root, _store, workspace = project_workspace
    project = workspace.create_project('inventory', 'hello@1.0.0')
    path = Path(project['path'])
    document = yaml.safe_load((path / 'project.yaml').read_text(encoding='utf-8'))
    document['plugins']['local_tool'] = {'path': 'algorithms/local-tool', 'parameters': {'threshold': 7}}
    (path / 'algorithms/local-tool').mkdir(parents=True)
    (path / 'algorithms/local-tool/plugin.yaml').write_text(
        'schema: hydro.plugin/v1\nname: local.tool\nversion: 2.3.4\nentry: plugin.py\ninputs: {}\noutputs: {}\nparameters: {}\n',
        encoding='utf-8',
    )
    (path / 'algorithms/local-tool/plugin.py').write_text('def run(inputs, parameters, context):\n    return {}\n', encoding='utf-8')
    (path / 'project.yaml').write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding='utf-8')
    raw = _reference('raw-rain-v1', 'daily/2026-01.csv', b'1,2\n')
    derived = _reference('derived-output-v1', 'outputs/result.bin', b'abc')
    (path / 'DATA-REFERENCES.json').write_text(json.dumps([raw, derived]), encoding='utf-8')

    original_request = workspace.store.request

    def unavailable_registration(method: str, **params: object):
        if method == 'check_dataset_existence':
            raise KitError('registry unavailable')
        return original_request(method, **params)

    monkeypatch.setattr(workspace.store, 'request', unavailable_registration)
    result = workspace.project_resources(project['project_id'])

    assert result['project_id'] == project['project_id']
    assert result['project_name'] == 'inventory'
    assert result['source_case_ref'] == 'hello@1.0.0'
    assert result['baseline_submission_id'] is None
    assert '暂未读取到数据登记状态' in result['warning']
    algorithms = {row['alias']: row for row in result['algorithms']}
    assert algorithms['scale']['kind'] == 'platform_copy'
    assert algorithms['scale']['ref'] == 'example.scale@1.0.0'
    assert algorithms['scale']['source_ref'] is None
    assert algorithms['local_tool'] == {
        'alias': 'local_tool', 'name': 'local.tool', 'version': '2.3.4', 'ref': 'local.tool@2.3.4',
        'source_ref': None, 'path': 'algorithms/local-tool', 'kind': 'local', 'modified': None,
        'parameters': {'threshold': 7},
    }
    datasets = {row['dataset_id']: row for row in result['datasets']}
    assert datasets['raw-rain-v1']['files'] == [raw]
    assert datasets['derived-output-v1']['files'] == [derived]


def test_inventory_rejects_foreign_projects_unsafe_plugin_paths_and_links(project_workspace) -> None:
    root, store, workspace = project_workspace
    other = Principal(workspace.principal.uid + 100, 'other-user', 'student')
    foreign = store.create_project(other, 'foreign')
    with pytest.raises(KitError, match='无权|工作区'):
        workspace.project_resources(foreign['project_id'])

    project = workspace.create_project('unsafe inventory')
    path = Path(project['path'])
    document = yaml.safe_load((path / 'project.yaml').read_text(encoding='utf-8'))
    document['plugins'] = {'unsafe': {'path': '../outside', 'parameters': {}}}
    (path / 'project.yaml').write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    with pytest.raises(KitError, match='算法路径'):
        workspace.project_resources(project['project_id'])

    document['plugins'] = {'unsafe': {'path': 'algorithms/unsafe', 'parameters': {}}}
    (path / 'project.yaml').write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    outside = root / 'outside-algorithm'
    outside.mkdir()
    (outside / 'plugin.yaml').write_text('secret: must-not-read\n', encoding='utf-8')
    (path / 'algorithms').mkdir(exist_ok=True)
    (path / 'algorithms/unsafe').symlink_to(outside, target_is_directory=True)
    with pytest.raises(KitError, match='软链接'):
        workspace.project_resources(project['project_id'])


def test_inventory_refuses_invalid_configuration_and_directory_replacement(project_workspace, monkeypatch):
    _root, _store, workspace = project_workspace
    project = workspace.create_project('racing inventory')
    path = Path(project['path'])
    (path / 'project.yaml').write_text('not a project document\n', encoding='utf-8')
    with pytest.raises(KitError, match='project.yaml'):
        workspace.project_resources(project['project_id'])
    (path / 'project.yaml').write_text('schema: researchkit.project/v1\nplugins: {}\n', encoding='utf-8')
    original_read = workspace._read_regular
    moved = path.with_name(path.name + '-moved')
    def replace_directory(descriptor, rel, **kwargs):
        value = original_read(descriptor, rel, **kwargs)
        if rel == 'project.yaml' and not moved.exists():
            path.rename(moved)
            path.mkdir(mode=0o700)
            (path / 'project.yaml').write_text('schema: researchkit.project/v1\nplugins: {}\n', encoding='utf-8')
        return value
    monkeypatch.setattr(workspace, '_read_regular', replace_directory)
    with pytest.raises(KitError, match='项目目录.*变化'):
        workspace.project_resources(project['project_id'])


def test_component_access_shows_template_provenance_only_to_teachers(tmp_path: Path) -> None:
    library = Path(__file__).resolve().parents[1] / 'library'
    teacher = Principal(1001, 'ly', 'teacher')
    student = Principal(1002, 'source-student', 'student')
    store = SubmissionStore(tmp_path / 'store')
    source_project = store.create_project(student, 'published source')
    case_root = library / 'cases/hello/1.0.0'
    files = {entry.relative_to(case_root).as_posix(): entry.read_bytes()
             for entry in case_root.rglob('*') if entry.is_file()}
    submission = store.submit(source_project['project_id'], student, 'minor', files, [])
    assets = AssetService(store, library)
    candidate = assets.create(teacher, str(submission['id']), 'template', 'provenance.case', '1.0.0')
    assets.review(teacher, str(candidate['candidate_id']), str(candidate['candidate_sha256']), 'reviewed')
    assets.publish(teacher, str(candidate['candidate_id']), str(candidate['candidate_sha256']))
    service = ComponentService(store, library)

    with pytest.raises(KitError, match='教师'):
        service.access(student)
    rows = service.access(teacher)
    published = next(row for row in rows if row['kind'] == 'template' and row['ref'] == 'provenance.case@1.0.0')
    assert published['source']['submission_id'] == submission['id']
    assert published['source']['project_id'] == source_project['project_id']
    assert published['source']['project_name'] == 'published source'
    assert published['source']['owner_username'] == 'source-student'
    assert next(row for row in rows if row['kind'] == 'template' and row['ref'] == 'hello@1.0.0')['source'] is None
    assert all('source' not in row for row in service.catalog(student)['templates'])


class TestProjectResourcesHttp(AsyncHTTPTestCase):
    token = 'r' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-project-resources-http-', dir=Path.cwd()))
        os.chmod(self.root, 0o700)
        self.principal = _principal()
        self.store = SubmissionStore(self.root / 'store')
        self.workspace = Workspace(self.principal, self.root / 'workspace', LocalStoreClient(self.store, self.principal))
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def api(self, route: str, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('Authorization', 'Bearer ' + self.token)
        return self.fetch('/api/' + route, headers=headers, **kwargs)

    def test_resources_route_is_owner_scoped_and_has_no_query_surface(self):
        # Use the real workspace creator outside Tornado's five-second test
        # client deadline; the route under test is still the HTTP GET below.
        project = self.workspace.create_project('http resources')
        response = self.api(f"projects/{project['project_id']}/resources")
        assert response.code == 200
        assert json.loads(response.body)['project_id'] == project['project_id']
        assert self.api(f"projects/{project['project_id']}/resources?path=/etc/passwd").code == 400

        other = Principal(self.principal.uid + 101, 'private-other', 'student')
        foreign = self.store.create_project(other, 'not this workspace')
        denied = self.api(f"projects/{foreign['project_id']}/resources")
        assert denied.code == 400
        assert b'not this workspace' not in denied.body
