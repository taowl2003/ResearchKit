"""Exercise cleanup through real HTTP, Workspace and the broker dispatch path."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import MappingProxyType

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import application
from researchkit.web_broker import BrokerServer, _decode_files, _encode_files
from researchkit.web_identity import Policy, Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_workspace import Workspace


class TestLifecycleIntegration(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-cleanup-http-', dir=Path.cwd()))
        self.teacher = Principal(os.getuid(), 'ly', 'teacher')
        self.student = Principal(os.getuid() + 100, 'test', 'student')
        shutil.copytree(Path(__file__).resolve().parents[1] / 'library', self.root / 'library')
        policy = Policy(MappingProxyType({self.teacher.uid: self.teacher, self.student.uid: self.student}),
                        MappingProxyType({}), os.getuid(), 'ly', self.root / 'store', self.root)
        self.server = BrokerServer(policy)
        class Client:
            def request(_, method, **params):
                if 'files' in params:
                    params['files'] = _encode_files(params['files'])
                result = self.server._dispatch(self.workspace.principal, {'method': method, 'params': params})
                if isinstance(result, dict) and 'files' in result:
                    result['files'] = _decode_files(result['files'])
                return result
        self.workspace = Workspace(self.teacher, self.root / 'workspace', Client())
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def api(self, route, body=None, method=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        return self.fetch('/api/' + route, method=method or ('GET' if body is None else 'PUT'),
                          headers=headers, body=None if body is None else json.dumps(body))

    def report(self, ref='hello@1.0.0', kind='template'):
        response = self.api('asset-lifecycle?kind=' + kind + '&ref=' + ref)
        assert response.code == 200, response.body
        return json.loads(response.body)

    def change(self, report, action):
        return self.api('asset-lifecycle', {key: report[key] for key in ('kind', 'ref')} |
                        {'action': action, 'expected_revision': report['revision']})

    def seed_submission(self, project_id, kind):
        """Create a historical verified version for lifecycle-only scenarios."""
        with self.workspace._project_guard(project_id):
            _root, files, references = self.workspace._snapshot(project_id)
        return self.server.store.submit(project_id, self.workspace.principal, kind, files, references)

    def test_template_trash_restore_preserves_files_and_grants(self):
        before = {p.relative_to(self.root / 'library'): p.read_bytes() for p in (self.root / 'library').rglob('*') if p.is_file()}
        source_project = self.workspace.create_project('cleanup source')
        fixed = self.seed_submission(source_project['project_id'], 'minor')
        from researchkit.web_assets import AssetService
        assets = AssetService(self.server.store, self.root / 'library')
        candidate = assets.create(self.teacher, fixed['submission_id'], 'template', 'cleanup.practice', '1.0.0')
        assets.review(self.teacher, candidate['candidate_id'], candidate['candidate_sha256'], '人工审核练习')
        assets.publish(self.teacher, candidate['candidate_id'], candidate['candidate_sha256'])
        access = json.loads(self.api('component-access').body)
        report = self.report('cleanup.practice@1.0.0')
        assert report['can_trash'], report
        removed = self.change(report, 'trash')
        assert removed.code == 200, removed.body
        assert 'cleanup.practice@1.0.0' not in {r['ref'] for r in json.loads(self.api('catalog').body)['templates']}
        assert 'cleanup.practice@1.0.0' not in {r['ref'] for r in json.loads(self.api('component-access').body)}
        hidden = json.loads(self.api('component-access?include_inactive=true').body)
        assert next(r for r in hidden if r['ref'] == 'cleanup.practice@1.0.0')['lifecycle']['state'] == 'trashed'
        with pytest.raises(KitError, match='下架|回收站'):
            self.workspace.create_project('blocked', 'cleanup.practice@1.0.0')
        restored = self.change(json.loads(removed.body), 'restore')
        assert restored.code == 200
        after_access = json.loads(self.api('component-access').body)
        assert [(r['kind'], r['ref'], r['public'], r['targets'], r['revision']) for r in access] == [(r['kind'], r['ref'], r['public'], r['targets'], r['revision']) for r in after_access]
        assert before == {p.relative_to(self.root / 'library'): p.read_bytes() for p in (self.root / 'library').rglob('*') if p.is_file()}

    def test_workspace_summary_fixed_submission_and_retirement_preserve_old_case(self):
        project = self.workspace.create_project('working case', 'hello@1.0.0')
        report = self.report()
        assert any(r.get('project_id') == project['project_id'] for r in report['references'])
        assert report['can_trash']  # imported central template has a known exact origin
        frozen = self.seed_submission(project['project_id'], 'minor')
        retired = self.change(report, 'retire')
        assert retired.code == 200, retired.body
        retained = self.workspace.store.request('get_submission', submission_id=frozen['submission_id'])
        assert retained['files']['case.py']
        assert Path(project['path'], 'case.py').is_file()
        from researchkit.engine import run_project
        outcome = run_project(Path(project['path']))
        assert outcome['status'] == 'success', outcome
        # Existing pinned dependencies can still be submitted, read and checked;
        # the new-project catalog and template copy reject the retired template.
        assert self.seed_submission(project['project_id'], 'minor')['submission_id']
        assert self.change(report, 'restore').code == 409
        report = self.report()
        assert any(r.get('submission_id') == frozen['submission_id'] for r in report['references'])
        assert self.change(report, 'restore').code == 200

    def test_algorithm_lists_installed_template_and_exact_reference(self):
        report = self.report('example.scale@1.0.0', 'algorithm')
        assert report['reference_count'] > 0, report
        assert any('hello@1.0.0' in str(r) for r in report['references']), report
        assert report['can_trash']
        retired = self.change(report, 'retire')
        assert retired.code == 200
        assert 'example.scale@1.0.0' not in {r['ref'] for r in json.loads(self.api('catalog').body)['plugins']}
        assert self.change(json.loads(retired.body), 'restore').code == 200

    def test_students_cannot_inspect_mutate_or_impersonate_project_usage(self):
        report = self.report()
        project = self.workspace.create_project('teacher only')
        self.workspace.principal = self.student
        assert self.api('asset-lifecycle?kind=template&ref=hello@1.0.0').code == 403
        assert self.change(report, 'retire').code == 403
        with pytest.raises(KitError, match='自己'):
            self.workspace.store.request('record_project_usage', project_id=project['project_id'], usage={
                'dataset_ids': [], 'algorithm_refs': [], 'template_refs': []})
        with pytest.raises(KitError, match='教师'):
            self.workspace.store.request('get_asset_lifecycle', kind='template', ref='hello@1.0.0')

    def test_strict_http_query_fields_and_mutation_body(self):
        for query in ('kind=template&ref=hello@1.0.0&role=teacher', 'kind=template&kind=algorithm&ref=hello@1.0.0',
                      'kind=template&ref=hello@1.0.0&offset=-1', 'kind=template', 'kind=dataset&ref=../../etc'):
            assert self.api('asset-lifecycle?' + query).code == 400
        for value in ('yes', 'true&include_inactive=false'):
            assert self.api('component-access?include_inactive=' + value).code == 400
        assert self.api('data-management/candidates?include_inactive=1').code == 400
        report = self.report()
        for extra in ({'root': '/etc'}, {'uid': self.teacher.uid}, {'role': 'teacher'}):
            assert self.api('asset-lifecycle', {'kind':report['kind'], 'ref':report['ref'], 'action':'retire',
                                               'expected_revision':report['revision'], **extra}).code == 400
