"""HTTP boundaries for project trials and a single atomic review batch."""
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import LocalStoreClient, application
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


class TestTrialsAndBatchHTTP(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        username = pwd.getpwuid(os.getuid()).pw_name
        if username != 'ly':
            pytest.skip('personal workspace fixture uses the configured ly account')
        self.root = Path(tempfile.mkdtemp(prefix='.rk-trials-batch-http-', dir=Path.cwd()))
        self.root.chmod(0o700)
        self.principal = Principal(os.getuid(), username, 'teacher')
        self.store = SubmissionStore(self.root / 'store')
        self.client = LocalStoreClient(self.store, self.principal, library_root=self.root / 'library')
        self.workspace = Workspace(self.principal, self.root / 'workspace', self.client)
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        if hasattr(self, 'root'):
            shutil.rmtree(self.root)

    def api(self, path, body=None):
        options = {'headers': {'Authorization': 'Bearer ' + self.token}}
        if body is not None:
            options.update(method='POST', body=json.dumps(body))
            options['headers']['Content-Type'] = 'application/json'
        return self.fetch('/api/' + path, **options)

    def test_trial_and_job_http_routes_are_retired_without_creating_submissions(self):
        project = self.workspace.create_project('HTTP trial')
        pid = project['project_id']
        response = self.api(f'projects/{pid}/trial', {})
        assert response.code == 410
        assert self.api(f'projects/{pid}/jobs').code == 410
        assert self.api('jobs').code == 410
        assert self.store.list_submissions(self.principal, personal=True) == []

    def test_batch_http_rejects_students_and_publishes_two_algorithms_in_one_request(self):
        from test_web_review_batch import _files
        project = self.store.create_project(self.principal, 'HTTP batch')
        submission = self.store.submit(project['project_id'], self.principal, 'minor', _files(), [])
        # Human review publishes immutable fixed code directly.  It does not
        # manufacture Web check/run evidence.
        payload = {'expected_sha256': submission['content_sha256'], 'items': [
            {'kind': 'algorithm', 'alias': alias, 'tags': ['HTTP测试'], 'description': alias}
            for alias in ('scale', 'offset')]}
        path = 'review/submissions/' + submission['id'] + '/publish-batch'
        self.workspace.principal = Principal(self.principal.uid, self.principal.username, 'student')
        assert self.api(path, payload).code == 403
        self.workspace.principal = self.principal
        assert self.api(path, {**payload, 'owner': 'test'}).code == 400
        assert self.api(path, {**payload, 'items': []}).code == 400
        response = self.api(path, payload)
        assert response.code == 200, response.body
        result = json.loads(response.body)
        assert result['ok'] is True
        assert {row['ref'] for row in result['published']} == {'batch.scale@1.0.0', 'batch.offset@1.0.0'}
        again = self.api(path, payload)
        assert json.loads(again.body)['reused'] is True


def test_batch_broker_client_uses_existing_long_operation_deadline(broker, monkeypatch):
    from researchkit import web_broker
    from researchkit.files import KitError
    _server, client, _principal = broker
    observed = []
    receive = web_broker._recv_frame
    def track(connection, *args, **kwargs):
        observed.append(kwargs.get('timeout'))
        return receive(connection, *args, **kwargs)
    monkeypatch.setattr(web_broker, '_recv_frame', track)
    # A small invalid body still exercises the real reply path and deadline
    # without creating resources; a large valid batch uses the same method.
    with pytest.raises(KitError):
        client.request('review_publish_batch')
    assert web_broker.LONG_OPERATION_TIMEOUT in observed


# Reuse the existing isolated Unix-socket fixture, not production services.
from test_web_broker import broker
