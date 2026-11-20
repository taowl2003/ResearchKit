from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
from unittest import mock
from pathlib import Path

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient, application
from researchkit.web_assets import AssetService
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace

from test_web_asset_validation import TEACHER, _files


class TestAssetValidationHttp(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-asset-validation-http-', dir=Path.cwd()))
        os.chmod(self.root, 0o700)
        username = pwd.getpwuid(os.getuid()).pw_name
        self.principal = Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')
        store = SubmissionStore(self.root / 'store')
        self.workspace = Workspace(self.principal, self.root / 'workspace', LocalStoreClient(store, self.principal))
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def fetch_api(self, path: str, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('Authorization', 'Bearer ' + self.token)
        return self.fetch('/api/' + path, headers=headers, **kwargs)

    def _candidate(self):
        if self.principal.role != 'teacher' or self.principal.username != 'ly':
            pytest.skip('candidate validation HTTP route requires the teacher account')
        store = self.workspace.store.store
        project = store.create_project(self.principal, 'http validation candidate')
        source = store.submit(project['project_id'], self.principal, 'major', _files(), [])
        return AssetService(store, self.root / 'library').create(
            self.principal, source['submission_id'], 'algorithm', 'http.validation', '1.0.0', 'scale')

    def test_validation_routes_are_retired_before_creating_any_record(self):
        candidate = self._candidate()
        route = 'asset-candidates/' + candidate['candidate_id'] + '/validations'
        listed = self.fetch_api(route)
        assert listed.code == 410
        malformed = self.fetch_api(route, method='POST', body=b'{}',
                                   headers={'Content-Type': 'application/json'})
        assert malformed.code == 410
        request = {'expected_sha256': candidate['candidate_sha256'], 'operation': 'run',
                   'cases': [{'inputs': {'value': 3}, 'parameters': {'factor': 2},
                              'expected_outputs': {'result': 6}}]}
        rejected = self.fetch_api(route, method='POST', body=json.dumps(request).encode(),
                                  headers={'Content-Type': 'application/json'})
        assert rejected.code == 410
        with self.workspace.store.store._connection() as con:
            assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_asset_validation_snapshots'").fetchone() is None

    def test_validation_post_stays_retired_even_if_a_local_client_claims_readiness(self):
        candidate = self._candidate()
        route = 'asset-candidates/' + candidate['candidate_id'] + '/validations'
        request = {'expected_sha256': candidate['candidate_sha256'], 'operation': 'run',
                   'cases': [{'inputs': {'value': 3}, 'parameters': {'factor': 2},
                              'expected_outputs': {'result': 6}}]}
        client = self.workspace.store
        original = client.request

        def ready_request(method, **params):
            if method == 'create_asset_candidate_validation':
                from researchkit.web_broker import asset_validation_request
                return asset_validation_request(client.store, client.library_root, client.principal, method, params,
                                                ready=True)
            return original(method, **params)

        with mock.patch.object(client, 'request', side_effect=ready_request):
            response = self.fetch_api(route, method='POST', body=json.dumps(request).encode(),
                                      headers={'Content-Type': 'application/json'})
            assert response.code == 410
            assert client.request.call_count == 0
        with self.workspace.store.store._connection() as con:
            assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_asset_validation_snapshots'").fetchone() is None


def test_local_validation_methods_are_retired_before_the_old_role_gate(tmp_path):
    from researchkit.web_store import SubmissionStore
    store = SubmissionStore(tmp_path / 'store')
    student = Principal(1002, 'student', 'student')
    with pytest.raises(KitError, match='网页.*移除'):
        LocalStoreClient(store, student, library_root=tmp_path / 'library').request(
            'list_asset_candidate_validations', candidate_id='retired-validation')
