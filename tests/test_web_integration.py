"""Real current-UID HTTP -> UNIX broker integration, without security mocks.

The broker and workspace use isolated development directories and the current
OS account. This does not substitute for two real student accounts or the
installed system service, neither of which this test touches.
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import tempfile
import threading
import urllib.parse
from pathlib import Path
from types import MappingProxyType

from tornado.testing import AsyncHTTPTestCase, gen_test

from researchkit.web_app import application
from researchkit.web_broker import BrokerClient, BrokerServer
from researchkit.web_identity import Policy, Principal
from researchkit.web_workspace import Workspace


class TestDistributionBrokerHTTP(AsyncHTTPTestCase):
    token = 'integration-only-' * 3

    def get_app(self):
        account = pwd.getpwuid(os.getuid())
        if account.pw_name != 'ly' or os.getuid() != os.geteuid():
            self.skipTest('real teacher self-distribution requires the current ly UID')
        self.root = Path(tempfile.mkdtemp(prefix='.rk-integration-', dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root)
        principal = Principal(account.pw_uid, account.pw_name, 'teacher')
        policy = Policy(
            principals=MappingProxyType({principal.uid: principal}),
            workspaces=MappingProxyType({principal.uid: self.root / 'workspace'}),
            broker_uid=principal.uid, broker_username=principal.username,
            submission_store=self.root / 'store', code_root=Path.cwd(),
            broker_socket=self.root / 'broker.sock',
        )
        self.broker = BrokerServer(policy)
        self.broker.bind()
        self.thread = threading.Thread(target=self.broker.serve_forever,
                                       kwargs={'install_signal_handler': False}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_broker)
        self.client = BrokerClient(policy.broker_socket, principal.uid)
        self.workspace = Workspace(principal, policy.workspace(principal), self.client)
        app = application(self.workspace, self.token)
        self.addCleanup(app.wildcard_router.rules[0].target_kwargs['executor'].shutdown, wait=True)
        return app

    def stop_broker(self):
        self.broker.close()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()

    def api(self, route, body=None, *, expected=200, method='POST'):
        kwargs = {'headers': {'Authorization': 'Bearer ' + self.token}}
        if body is not None:
            kwargs.update(method=method, body=json.dumps(body))
            kwargs['headers']['Content-Type'] = 'application/json'
        response = self.fetch('/api/' + route, **kwargs)
        assert response.code == expected, response.body.decode()
        return json.loads(response.body)

    def seed_submission(self, project_id, kind):
        """Seed a legacy/formally verified snapshot for unrelated feature tests.

        Direct browser submission is intentionally closed by the production
        broker.  The dedicated workflow integration test covers the full
        preview/check/run/promotion path; these tests need stable historical
        versions only to exercise claim, package, lifecycle and data behavior.
        """
        with self.workspace._project_guard(project_id):
            _root, files, references = self.workspace._snapshot(project_id)
        return self.broker.store.submit(project_id, self.workspace.principal, kind, files, references)

    def test_fixed_distribution_round_trip_and_retry(self):
        assert self.api('me')['demo'] is False
        original = self.api('projects', {'name': 'HTTP broker 基线'}, expected=201)
        source = Path(original['path'])
        binary = b'\x00\xff\x80snapshot'
        (source / '数据.bin').write_bytes(binary)
        (source / 'case.py').write_text('def broken(:\n', encoding='utf-8')
        frozen = self.seed_submission(original['project_id'], 'major')
        request = {'submission_id': frozen['submission_id'], 'target_username': 'ly'}
        sent = self.api('distributions', request, expected=201)
        assert self.api('distributions', request, expected=201) == sent
        (source / 'case.py').write_text('later teacher workspace\n', encoding='utf-8')
        claimed = self.api('distributions/' + sent['distribution_id'] + '/claim', {})
        destination = Path(claimed['path'])
        assert claimed['source'] == 'teacher-assigned'
        assert claimed['baseline_submission_id'] == frozen['submission_id']
        assert (destination / 'case.py').read_text() == 'def broken(:\n'
        assert (destination / '数据.bin').read_bytes() == binary
        assert os.access(destination / 'task', os.X_OK)
        (destination / 'case.py').write_text('# independent edit\n', encoding='utf-8')
        retry = self.api('distributions/' + sent['distribution_id'] + '/claim', {})
        assert retry['project_id'] == claimed['project_id']
        assert (destination / 'case.py').read_text() == '# independent edit\n'
        submitted = self.seed_submission(claimed['project_id'], 'minor')
        assert submitted['version'] == '0.1'
        difference = self.api('baseline-diff?submission_id=' + submitted['submission_id'] + '&path=case.py')
        assert '-def broken(' in difference['diff'] and '+# independent edit' in difference['diff']
        baseline = self.client.request('get_distribution_baseline', distribution_id=sent['distribution_id'])
        assert baseline['files']['数据.bin'] == binary
        assert baseline['files']['case.py'] == b'def broken(:\n'
        assert self.api('distributions')[0]['status'] == 'claimed'
        assert len(self.api('projects')) == 2
        assert self.fetch('/static/help.html').code == 200

    def test_composition_preview_conflict_and_fixed_submission_over_broker(self):
        catalog = self.api('catalog')
        assert any(item['ref'] == 'example.scale@1.0.0' for item in catalog['plugins'])
        project = self.api('projects', {'name': 'HTTP broker composition'}, expected=201)
        project_id = project['project_id']
        path = Path(project['path'])
        before = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        record = self.api('projects/' + project_id + '/composition')
        request = {
            'expected_revision': record['revision'], 'parameters': {'value': 2},
            'steps': [{'alias': 'scale', 'ref': 'example.scale@1.0.0', 'parameters': {'factor': 3},
                       'inputs': {'value': {'kind': 'parameter', 'name': 'value'}}}],
            'draft_contracts': [], 'generate_entry': True,
        }
        preview = self.api('projects/' + project_id + '/composition/preview', request)
        assert 'project.yaml' in preview['changed_paths']
        assert 'case.py' in preview['changed_paths']
        assert before == {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        (path / 'case.py').write_text('# IDE changed before apply\n')
        self.api('projects/' + project_id + '/composition', request, method='PUT', expected=409)
        assert (path / 'case.py').read_text() == '# IDE changed before apply\n'
        record = self.api('projects/' + project_id + '/composition')
        request['expected_revision'] = record['revision']
        result = self.api('projects/' + project_id + '/composition', request, method='PUT')
        assert result['managed'] is True
        assert result['steps'][0]['ref'] == 'example.scale@1.0.0'
        from researchkit.model import project as validate_project, verify_lock
        assert validate_project(path)['plugins']['scale']['parameters'] == {'factor': 3}
        verify_lock(path)
        frozen = self.seed_submission(project_id, 'minor')
        generated = (path / 'case.py').read_bytes()
        (path / 'case.py').write_text('# later IDE edit\n')
        snapshot = self.client.request('get_submission', submission_id=frozen['submission_id'])
        assert snapshot['files']['case.py'] == generated
        assert b'web_composition' in snapshot['files']['project.yaml']

    def test_composition_endpoints_require_identity_and_reject_invalid_drafts(self):
        project = self.api('projects', {'name': 'composition boundary'}, expected=201)
        route = 'projects/' + project['project_id'] + '/composition'
        path = Path(project['path'])
        before = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        record = self.api(route)
        body = {'expected_revision': record['revision'], 'parameters': {}, 'steps': [],
                'draft_contracts': [], 'generate_entry': False}
        for endpoint, method in [('catalog', 'GET'), (route, 'GET'),
                                 (route + '/preview', 'POST'), (route, 'PUT')]:
            kwargs = {} if method == 'GET' else {'body': json.dumps(body)}
            response = self.fetch('/api/' + endpoint, method=method,
                                  headers={'Content-Type': 'application/json'}, **kwargs)
            assert response.code == 403
            response = self.fetch('/api/' + endpoint, method=method,
                                  headers={'Content-Type': 'application/json',
                                           'Authorization': 'Bearer ' + self.token,
                                           'Origin': 'https://attacker.invalid'}, **kwargs)
            assert response.code == 403
        # A syntactically valid but unregistered ID must not access a workspace.
        self.api('projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/composition', expected=400)
        invalid = dict(body, steps=[{'alias': 'missing', 'ref': 'unknown@1.0.0',
                                    'parameters': {}, 'inputs': {}}])
        self.api(route + '/preview', invalid, expected=400)
        self.api(route, invalid, method='PUT', expected=400)
        deeply_nested = json.dumps(body).replace('"parameters": {}',
            '"parameters": {"nested":' + '[' * 128 + '0' + ']' * 128 + '}')
        rejected = self.fetch('/api/' + route, method='PUT', body=deeply_nested,
                              headers={'Content-Type': 'application/json',
                                       'Authorization': 'Bearer ' + self.token})
        assert rejected.code == 400
        assert before == {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
        static = self.fetch('/static/composer.js')
        assert static.code == 200
        assert 'javascript' in static.headers['Content-Type']

    def test_interrupted_composition_is_recovered_before_read_and_submit(self):
        from researchkit.web_composer import Composer
        project = self.api('projects', {'name': 'composition recovery guard'}, expected=201)
        project_id = project['project_id']
        case = Path(project['path']) / 'case.py'
        original = case.read_bytes()
        composer = Composer(self.workspace)
        partial = b'# incomplete composition transaction\n'
        composer._write_journal(project_id, [('case.py', original, partial)])
        case.write_bytes(partial)
        read = self.api('projects/' + project_id + '/file?path=case.py')
        assert read['text'].encode() == original
        assert case.read_bytes() == original
        composer._write_journal(project_id, [('case.py', original, partial)])
        case.write_bytes(partial)
        frozen = self.seed_submission(project_id, 'minor')
        snapshot = self.client.request('get_submission', submission_id=frozen['submission_id'])
        assert snapshot['files']['case.py'] == original
        assert case.read_bytes() == original
        # A later IDE change blocks submission instead of sealing a mixed state.
        composer._write_journal(project_id, [('case.py', original, partial)])
        case.write_bytes(b'# IDE work must survive\n')
        import pytest
        from researchkit.files import KitError
        with pytest.raises(KitError, match='冲突|事务'):
            self.seed_submission(project_id, 'minor')
        assert case.read_bytes() == b'# IDE work must survive\n'
        assert len(self.api('submissions?project_id=' + project_id)) == 1

    @gen_test
    async def test_concurrent_composition_and_submit_never_seal_partial_files(self):
        project = self.workspace.create_project('concurrent composition')
        project_id = project['project_id']
        before = self.workspace._snapshot(project_id)[1]
        record = self.workspace.read_composition(project_id)
        payload = {'expected_revision': record['revision'], 'parameters': {'value': 2},
                   'steps': [{'alias': 'scale', 'ref': 'example.scale@1.0.0',
                              'parameters': {'factor': 3},
                              'inputs': {'value': {'kind': 'parameter', 'name': 'value'}}}],
                   'draft_contracts': [], 'generate_entry': True}
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}
        base = self.get_url('/api/projects/' + project_id)
        responses = await asyncio.gather(
            self.http_client.fetch(base + '/composition', method='PUT',
                                   headers=headers, body=json.dumps(payload), raise_error=False),
            self.http_client.fetch(base + '/submit', method='POST',
                                   headers=headers, body='{"kind":"minor"}', raise_error=False),
            self.http_client.fetch(base + '/composition', method='PUT',
                                   headers=headers, body=json.dumps(payload), raise_error=False),
        )
        assert sorted([responses[0].code, responses[2].code]) == [200, 409]
        assert responses[1].code == 400
        after = self.workspace._snapshot(project_id)[1]
        submitted = self.seed_submission(project_id, 'minor')
        snapshot = self.client.request('get_submission', submission_id=submitted['submission_id'])
        assert snapshot['files'] in (before, after)

    def test_teacher_package_download_import_retry_and_unassigned_baseline(self):
        from researchkit.web_packages import pack_package, unpack_package
        project = self.api('projects', {'name': 'HTTP package source'}, expected=201)
        path = Path(project['path'])
        # An incompressible ZIP exceeds the ordinary 1.2 MB JSON limit and
        # exercises the dedicated binary HTTP path through the real broker.
        binary = b'\x00\xff\x80' + os.urandom(1_300_000)
        (path / 'data.bin').write_bytes(binary)
        (path / 'case.py').write_text('def broken(:\n')
        frozen = self.seed_submission(project['project_id'], 'major')
        response = self.fetch('/api/submissions/' + frozen['submission_id'] + '/package',
                              method='POST', body='{}', headers={
                                  'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        assert response.code == 200, response.body.decode(errors='replace')
        assert response.headers['Content-Type'] == 'application/zip'
        files, marker = unpack_package(response.body)
        assert len(response.body) > 1_200_000
        assert files['data.bin'] == binary
        assert marker['baseline_sha256'] == frozen['content_sha256']
        route = '/api/imports?name=' + urllib.parse.quote('教师包导入')
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/zip'}
        imported_response = self.fetch(route, method='POST', headers=headers, body=response.body)
        assert imported_response.code == 201, imported_response.body.decode()
        imported = json.loads(imported_response.body)
        assert imported['source'] == 'teacher-unassigned'
        assert imported['project_id'] != project['project_id']
        case = Path(imported['path']) / 'case.py'
        case.write_text('# student independent edit\n')
        retried = self.fetch(route, method='POST', headers=headers, body=response.body)
        assert json.loads(retried.body)['project_id'] == imported['project_id']
        assert case.read_text() == '# student independent edit\n'
        submitted = self.seed_submission(imported['project_id'], 'minor')
        difference = self.api('baseline-diff?submission_id=' + submitted['submission_id'] + '&path=case.py')
        assert '+# student independent edit' in difference['diff']
        files['case.py'] = b'# modified before import\n'
        refused = self.fetch(route, method='POST', headers=headers, body=pack_package(files, marker))
        assert refused.code == 400
        assert len(self.api('projects')) == 2
        assert self.fetch(route, method='POST', headers={'Content-Type': 'application/zip'}, body=response.body).code == 403

    def test_shared_template_and_permissions_pass_through_real_broker(self):
        catalog = self.api('catalog')
        assert any(item['ref'] == 'hello@1.0.0' for item in catalog['templates'])
        created = self.api('projects', {'name': 'HTTP hello template', 'template_ref': 'hello@1.0.0'}, expected=201)
        from researchkit.model import project, verify_lock
        doc = project(Path(created['path']))
        assert doc['parameters']['value'] == 2
        assert doc['plugins']['scale']['parameters']['factor'] == 3
        verify_lock(Path(created['path']))
        rules = self.api('component-access')
        rule = next(row for row in rules if row['kind'] == 'algorithm' and row['ref'] == 'example.scale@1.0.0')
        body = {'kind': rule['kind'], 'ref': rule['ref'], 'public': False,
                'targets': ['ly'], 'expected_revision': rule['revision']}
        updated = self.api('component-access', body, method='PUT')
        assert updated['public'] is False
        assert updated['targets'][0]['uid'] == os.getuid()
        assert updated['targets'][0]['username'] == 'ly'
        self.api('component-access', body, method='PUT', expected=409)
        body['expected_revision'] = updated['revision']
        body['targets'] = ['root']
        self.api('component-access', body, method='PUT', expected=400)
        current = next(row for row in self.api('component-access')
                       if row['kind'] == rule['kind'] and row['ref'] == rule['ref'])
        assert current == updated

    def test_public_job_http_protocol_is_retired_without_touching_fixed_submission(self):
        project = self.api('projects', {'name': 'IDE artifact protocol test'}, expected=201)
        frozen = self.seed_submission(project['project_id'], 'minor')
        route = 'submissions/' + frozen['submission_id'] + '/jobs'
        # No browser route may queue, list, inspect, cancel, or download a
        # Web execution job.  The immutable submission itself remains usable.
        assert self.fetch('/api/execution', headers={'Authorization': 'Bearer ' + self.token}).code == 410
        assert self.fetch('/api/' + route, method='POST', body='{"operation":"run"}',
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json'}).code == 410
        assert self.fetch('/api/jobs', headers={'Authorization': 'Bearer ' + self.token}).code == 410
        snapshot = self.client.request('get_submission', submission_id=frozen['submission_id'])
        assert snapshot['content_sha256'] == frozen['content_sha256']

    def test_controlled_data_http_fixed_snapshot_and_ide_authorization(self):
        # HTTP/socket identity is real.  Data remains a fixed, read-only IDE
        # input; the browser no longer creates a bwrap execution job.
        from dataclasses import replace
        import hashlib
        from unittest.mock import patch
        import pytest
        from researchkit.files import KitError
        from researchkit.web_datasets import DatasetRegistry
        root = self.root / 'controlled'
        root.mkdir()
        (root / 'input.txt').write_bytes(b'2 3 5')
        reference = {'dataset_id': 'numbers-v1', 'path': 'input.txt', 'size': 5,
                     'sha256': hashlib.sha256(b'2 3 5').hexdigest()}
        registry_path = self.root / 'datasets.json'
        dataset = {'dataset_id': 'numbers-v1', 'root': str(root), 'readers': [],
                   'files': [{key: reference[key] for key in ('path', 'size', 'sha256')}]}
        def save_registry():
            registry_path.write_text(json.dumps({'schema': 'researchkit.web-datasets/v1', 'datasets': [dataset]}))
        save_registry()
        self.broker.policy = replace(self.broker.policy, dataset_registry=registry_path)
        project = self.api('projects', {'name': 'controlled data HTTP'}, expected=201)
        source = Path(project['path'])
        (source / 'case.py').write_text('# IDE reads the fixed controlled-data reference.\n')
        with patch('researchkit.web_datasets._root_owned', lambda info: info.st_uid in {0, os.geteuid()}):
            assert self.api('datasets') == []
            dataset['readers'] = [{'uid': os.getuid(), 'username': 'ly'}]
            save_registry()
            assert self.api('datasets') == [{'dataset_id': 'numbers-v1', 'files': [reference]}]
            attached = self.api('projects/' + project['project_id'] + '/datasets',
                                {'dataset_ids': ['numbers-v1'], 'expected_sha256': None},
                                method='PUT')
            assert attached['references'] == [reference]
            frozen = self.seed_submission(project['project_id'], 'major')
            assert frozen['manifest']['data_references']['references'] == [reference]
            # IDE mounting uses the same exact reference checks, without a
            # browser execution request.
            with DatasetRegistry(registry_path).open_references([self.workspace.principal], [reference]) as mounts:
                assert os.read(mounts[0].fd, 5) == b'2 3 5'
            assert (root / 'input.txt').read_bytes() == b'2 3 5'
            dataset['readers'] = []
            save_registry()
            with pytest.raises(KitError, match='无权'):
                with DatasetRegistry(registry_path).open_references([self.workspace.principal], [reference]):
                    pass
            # Historical Web job routes are closed regardless of the current
            # raw-data ACL.
            assert self.fetch('/api/submissions/' + frozen['submission_id'] + '/jobs', method='POST',
                              body='{"operation":"run"}', headers={
                                  'Authorization': 'Bearer ' + self.token,
                                  'Content-Type': 'application/json'}).code == 410

    def test_asset_http_review_publish_then_compose_and_create_from_template(self):
        import pytest
        from researchkit.files import KitError
        from researchkit.model import project as inspect_project
        project = self.api('projects', {'name': 'publication HTTP source', 'template_ref': 'hello@1.0.0'}, expected=201)
        source = Path(project['path'])
        original = (source / 'case.py').read_text()
        (source / 'case.py').write_text('raise RuntimeError("static-publication-must-not-execute")\n' + original)
        fixed = self.seed_submission(project['project_id'], 'major')
        request = {'submission_id': fixed['submission_id'], 'kind': 'algorithm',
                   'name': 'http.published.scale', 'version': '1.0.0', 'alias': 'scale'}
        self.api('asset-candidates', {**request, 'method': 'get_submission'}, expected=400)
        draft = self.api('asset-candidates', request, expected=201)
        candidate_route = 'asset-candidates/' + draft['candidate_id']
        assert draft['status'] == 'draft' and draft['source_sha256'] == fixed['content_sha256']
        assert isinstance(draft['files'], list)
        assert draft['ref'] not in {row['ref'] for row in self.api('catalog')['plugins']}
        self.api(candidate_route + '/publish', {'expected_sha256': draft['candidate_sha256']}, expected=400)
        file = self.api(candidate_route + '/file?path=plugin%2Fplugin.py')
        assert file['path'] == 'plugin/plugin.py' and 'def run' in file['text']
        self.api(candidate_route + '/file?path=..%2Fsecret', expected=400)
        (source / '.researchkit/plugins/scale/plugin.py').write_text('later IDE modification')
        assert self.api(candidate_route + '/file?path=plugin%2Fplugin.py') == file
        reviewed = self.api(candidate_route + '/review', {'expected_sha256': draft['candidate_sha256'],
                            'notes': '人工核对固定来源、接口、参数及静态结果；本次不声称执行算法单测。', 'validation_run_id': None})
        assert reviewed['status'] == 'reviewed'
        published = self.api(candidate_route + '/publish', {'expected_sha256': draft['candidate_sha256']})
        assert published['status'] == 'published'
        assert self.api(candidate_route + '/publish', {'expected_sha256': draft['candidate_sha256']})['candidate_id'] == draft['candidate_id']
        assert draft['ref'] in {row['ref'] for row in self.api('catalog')['plugins']}
        access = next(row for row in self.api('component-access') if row['ref'] == draft['ref'])
        assert access['public'] is False and access['targets'] == []
        target = self.api('projects', {'name': 'reuse published algorithm'}, expected=201)
        composition_route = 'projects/' + target['project_id'] + '/composition'
        opened = self.api(composition_route)
        self.api(composition_route, {'expected_revision': opened['revision'], 'parameters': {'value': 2},
                 'draft_contracts': [], 'generate_entry': True,
                 'steps': [{'alias': 'published', 'ref': draft['ref'], 'parameters': {'factor': 4},
                            'inputs': {'value': {'kind': 'parameter', 'name': 'value'}}}]}, method='PUT')
        assert 'published' in inspect_project(Path(target['path']))['plugins']
        case = self.api('asset-candidates', {**request, 'kind': 'template', 'name': 'http.published.case', 'alias': None}, expected=201)
        case_route = 'asset-candidates/' + case['candidate_id']
        self.api(case_route + '/review', {'expected_sha256': case['candidate_sha256'], 'notes': '核对固定案例和依赖副本，不执行案例顶层代码。'})
        self.api(case_route + '/publish', {'expected_sha256': case['candidate_sha256']})
        reused = self.api('projects', {'name': 'reuse published case', 'template_ref': case['ref']}, expected=201)
        assert 'static-publication-must-not-execute' in (Path(reused['path']) / 'case.py').read_text()
        assert (Path(reused['path']) / '.researchkit/lock.json').is_file()
        attacker = Principal(os.getuid() + 100000, 'unrelated-student', 'student')
        for method, params in [('list_asset_candidates', {}), ('get_asset_candidate', {'candidate_id': draft['candidate_id']}),
                               ('publish_asset_candidate', {'candidate_id': draft['candidate_id'], 'expected_sha256': draft['candidate_sha256']})]:
            with pytest.raises(KitError, match='教师'):
                self.broker._dispatch(attacker, {'method': method, 'params': params})
