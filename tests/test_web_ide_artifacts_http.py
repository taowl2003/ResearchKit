"""HTTP contracts for IDE-produced experiment artifacts, never Web execution."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import LocalStoreClient, application
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location('build_three_cases_http', ROOT / 'tools' / 'build-three-cases.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIDEArtifactHTTP(AsyncHTTPTestCase):
    token = 'e' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-ide-artifacts-http-', dir=Path.cwd()))
        os.chmod(self.root, 0o700)
        self.principal = Principal(os.getuid(), 'ly', 'teacher')
        self.store = SubmissionStore(self.root / 'store')
        self.workspace = Workspace(self.principal, self.root / 'workspace',
                                   LocalStoreClient(self.store, self.principal))
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def api(self, route, body=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        kwargs = {'headers': headers}
        if body is not None:
            kwargs.update(method='POST', body=json.dumps(body).encode('utf-8'))
            headers['Content-Type'] = 'application/json'
        return self.fetch('/api/' + route, **kwargs)

    def _project_with_real_run(self):
        destination = self.workspace.projects_root / 'rainfall'
        _builder().build_project('rainfall', destination)
        os.chmod(destination, 0o700)
        project = self.workspace.register_existing('rainfall HTTP', 'projects/rainfall')
        process = subprocess.run(
            [str(ROOT / '.web-venv' / 'bin' / 'python'), 'run_experiment.py', '--scheme', 'factor-2'],
            cwd=destination, text=True, capture_output=True, timeout=30,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        return project, destination

    def test_http_catalogue_cas_results_and_retired_execution_routes(self):
        project, directory = self._project_with_real_run()
        pid = project['project_id']
        route = f'projects/{pid}/experiments'
        catalogue = self.api(route)
        assert catalogue.code == 200
        initial = json.loads(catalogue.body)
        source = next(row for row in initial['schemes'] if row['id'] == 'factor-2')
        document = dict(source['document'])
        document['name'] = 'HTTP copy'
        created = self.api(route, {'id': 'http-copy', 'document': document, 'expected_sha256': None})
        assert created.code == 200
        saved = json.loads(created.body)
        conflict = self.api(route, {'id': 'http-copy', 'document': document, 'expected_sha256': '0' * 64})
        assert conflict.code == 400
        deleted = self.api(route + '/delete', {'id': 'http-copy', 'expected_sha256': saved['sha256']})
        assert deleted.code == 200 and json.loads(deleted.body)['results_retained'] is True

        run = initial['runs'][0]
        bad_confirmation = self.api(f'projects/{pid}/experiment-results/delete', {
            'scheme_id': run['scheme_id'], 'batch_id': run['batch_id'], 'confirmation': 'wrong/value',
            'expected_revision': run['revision'],
        })
        assert bad_confirmation.code == 400
        changed = directory / 'results' / run['scheme_id'] / run['batch_id'] / 'http-change.txt'
        changed.write_text('IDE changed this batch', encoding='utf-8')
        stale = self.api(f'projects/{pid}/experiment-results/delete', {
            'scheme_id': run['scheme_id'], 'batch_id': run['batch_id'],
            'confirmation': f"{run['scheme_id']}/{run['batch_id']}", 'expected_revision': run['revision'],
        })
        assert stale.code == 400

        for route, body in ((f'projects/{pid}/trial', {}), ('execution', {}), ('jobs', {}),
                            ('asset-candidates/nope/validations', {})):
            assert self.api(route, body).code == 410
        assert self.api(f'projects/{pid}/jobs').code == 410

    def test_result_file_http_is_inert_scoped_and_available_from_fixed_submissions(self):
        project, directory = self._project_with_real_run()
        pid = project['project_id']
        catalogue = json.loads(self.api(f'projects/{pid}/experiments').body)
        run = catalogue['runs'][0]
        batch = directory / 'results' / run['scheme_id'] / run['batch_id']
        (batch / 'notes.txt').write_text('<script>alert(1)</script>', encoding='utf-8')
        (batch / 'chart.svg').write_text('<svg onload="alert(1)"><script>x()</script><a href="https://bad"><text>safe</text></a></svg>', encoding='utf-8')
        (batch / 'declared.svg').write_text('<!DOCTYPE svg><svg/>', encoding='utf-8')
        (batch / 'outside.txt').symlink_to('/etc/passwd')
        base = f'results/{run["scheme_id"]}/{run["batch_id"]}'

        text = self.fetch('/api/projects/' + pid + '/result-file?path=' + base + '/notes.txt',
                          headers={'Authorization': 'Bearer ' + self.token})
        assert text.code == 200 and text.headers['Content-Type'].startswith('text/plain')
        assert text.body == b'<script>alert(1)</script>'
        assert text.headers['Content-Security-Policy'] == "default-src 'none'; sandbox"
        svg = self.fetch('/api/projects/' + pid + '/result-file?path=' + base + '/chart.svg',
                         headers={'Authorization': 'Bearer ' + self.token})
        assert svg.code == 200 and b'<script' not in svg.body and b'onload' not in svg.body and b'href=' not in svg.body
        assert self.fetch('/api/projects/' + pid + '/result-file?path=' + base + '/declared.svg',
                          headers={'Authorization': 'Bearer ' + self.token}).code == 400
        assert self.fetch('/api/projects/' + pid + '/result-file?path=results/../project.yaml',
                          headers={'Authorization': 'Bearer ' + self.token}).code == 400
        assert self.fetch('/api/projects/' + pid + '/result-file?path=' + base + '/outside.txt',
                          headers={'Authorization': 'Bearer ' + self.token}).code == 400
        (batch / 'outside.txt').unlink()

        _root, files, refs = self.workspace._snapshot(pid)
        submission = self.store.submit(pid, self.principal, 'minor', files, refs)
        sid = submission['id']
        fixed = self.fetch('/api/submissions/' + sid + '/result-file?path=' + base + '/notes.txt&download=true',
                           headers={'Authorization': 'Bearer ' + self.token})
        assert fixed.code == 200 and fixed.body == text.body
        assert fixed.headers['Content-Disposition'].startswith('attachment;')
        listed = self.api(f'submissions/{sid}/experiments')
        assert listed.code == 200 and json.loads(listed.body)['submission_id'] == sid
        reviewed = self.api(f'review/submissions/{sid}/experiments')
        assert reviewed.code == 200

        self.workspace.principal = Principal(os.getuid(), 'artifact-owner', 'student')
        assert self.api(f'review/submissions/{sid}/experiments').code == 403
        self.workspace.principal = self.principal

    def test_enable_endpoint_creates_the_optional_ide_files_once(self):
        project = self.workspace.create_project('HTTP old Python project')
        path = Path(project['path'])
        original_case = (path / 'case.py').read_bytes()
        route = f"projects/{project['project_id']}/experiments/enable"
        first = self.api(route, {})
        assert first.code == 200
        assert set(json.loads(first.body)['created']) == {'experiment_support.py', 'run_experiment.py'}
        assert (path / 'case.py').read_bytes() == original_case
        second = self.api(route, {})
        assert second.code == 200 and json.loads(second.body)['created'] == []

    def test_explicit_helper_update_http_keeps_custom_files_and_rejects_extra_parameters(self):
        project, directory = self._project_with_real_run()
        helper = directory/'experiment_support.py'
        helper.write_bytes((ROOT/'tests/fixtures/experiment-support-history.py').read_bytes())
        pid = project['project_id']
        assert json.loads(self.api(f'projects/{pid}/experiments').body)['helper_update_available'] is True
        route = f'projects/{pid}/experiments/update-helper'
        assert self.api(route, {'path':'case.py'}).code == 400
        assert self.api(route+'?force=true', {}).code == 400
        assert self.api(route, {}).code == 200
        assert json.loads(self.api(f'projects/{pid}/experiments').body)['result_retention']=='latest-success'
        custom=b'# custom\n'+helper.read_bytes();helper.write_bytes(custom)
        assert self.api(route, {}).code == 400
        assert helper.read_bytes()==custom
