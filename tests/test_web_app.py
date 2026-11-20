from __future__ import annotations

import os
import pwd
import shutil
import tempfile
import json
import stat
from unittest import mock

import pytest
from pathlib import Path

from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import MAX_HTTP_BODY, LocalStoreClient, _ensure_workspace, application, bind_loopback_server
from researchkit import web_identity
from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


class TestWebApp(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-web-http-', dir=Path.cwd()))
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

    def test_static_does_not_expose_data_and_api_rejects_token_host_and_origin(self):
        static = self.fetch('/')
        assert static.code == 200
        assert b'workspace' not in static.body.lower()
        assert self.fetch('/api/me').code == 403
        assert self.fetch_api('me', headers={'Host': 'attacker.invalid'}).code == 403
        assert self.fetch_api('me', headers={'Origin': 'https://localhost'}).code == 403

    def test_all_page_scripts_and_styles_are_served_with_correct_types(self):
        from html.parser import HTMLParser

        class PageAssets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.assets = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'script' and attrs.get('src'):
                    self.assets[attrs['src']] = 'application/javascript'
                if tag == 'link' and attrs.get('rel') == 'stylesheet':
                    self.assets[attrs['href']] = 'text/css'

        parser = PageAssets()
        parser.feed(self.fetch('/').body.decode())
        assert '/static/workflow.js' in parser.assets
        assert '/static/lifecycle.js' in parser.assets
        assert '/static/cleanup.js' in parser.assets
        assert '/static/readme-export.js' in parser.assets
        for url, expected_type in parser.assets.items():
            response = self.fetch(url)
            assert response.code == 200, (url, response.code, response.body)
            assert response.headers['Content-Type'].split(';')[0] == expected_type, url
            assert response.headers['X-Content-Type-Options'] == 'nosniff'
            assert response.body
        for url in ['/static/web_app.py', '/static/index.html', '/static/not-a-script.js',
                    '/static/../web_app.py']:
            assert self.fetch(url).code == 404, url

    def test_algorithm_creation_no_longer_serves_prefilled_tutorial(self):
        response = self.fetch('/static/tutorial-algorithms.json')
        assert response.code == 404
        assert self.workspace.list_projects() == []
        assert self.workspace.list_local_algorithms() == []

    def test_personal_resource_preferences_are_local_authenticated_and_strict(self):
        path = 'personal-resource-visibility'
        assert self.fetch('/api/' + path).code == 403
        assert json.loads(self.fetch_api(path).body) == {'hidden_algorithms': [], 'hidden_datasets': []}
        payload = {'kind': 'algorithm', 'ref': 'calibration.scale@1.0.0', 'hidden': True}
        result = self.fetch_api(path, method='PUT', body=json.dumps(payload), headers={'Content-Type': 'application/json'})
        assert result.code == 200
        assert json.loads(result.body)['hidden_algorithms'] == ['calibration.scale@1.0.0']
        assert json.loads(self.fetch_api(path).body)['hidden_algorithms'] == ['calibration.scale@1.0.0']
        assert self.fetch_api(path + '?username=test').code == 400
        invalid = self.fetch_api(path, method='PUT', body=json.dumps({**payload, 'username': 'test'}),
                                 headers={'Content-Type': 'application/json'})
        assert invalid.code == 400
        assert self.workspace.list_projects() == []

    def test_identity_does_not_come_from_request_and_mutation_needs_json(self):
        response = self.fetch_api('me', headers={'X-Username': 'another-user', 'X-Role': 'student'})
        assert response.code == 200
        assert self.principal.username.encode() in response.body
        not_json = self.fetch_api('projects', method='POST', body='name=x')
        assert not_json.code == 415
        created = self.fetch_api('projects', method='POST', body=b'{"name":"http project"}', headers={'Content-Type': 'application/json'})
        assert created.code == 201


    def test_http_project_edit_submit_fixed_version_and_diff(self):
        created = self.fetch_api('projects', method='POST', body=b'{"name":"http lifecycle"}',
                                 headers={'Content-Type': 'application/json'})
        assert created.code == 201
        project = json.loads(created.body)
        project_id = project['project_id']
        initial = self.fetch_api(f'projects/{project_id}/file?path=case.py')
        assert initial.code == 200
        revision = json.loads(initial.body)['sha256']
        disk_case = Path(project['path']) / 'case.py'
        disk_case.write_text('def broken(:\n', encoding='utf-8')
        conflict = self.fetch_api(f'projects/{project_id}/file', method='PUT',
                                  body=json.dumps({'path': 'case.py', 'text': 'web overwrite', 'expected_sha256': revision}).encode(),
                                  headers={'Content-Type': 'application/json'})
        assert conflict.code == 409
        minor = self.fetch_api(f'projects/{project_id}/submit', method='POST', body=b'{"kind":"minor"}',
                               headers={'Content-Type': 'application/json'})
        assert minor.code == 200
        first = json.loads(minor.body)
        disk_case.write_text('def later(:\n', encoding='utf-8')
        major = self.fetch_api(f'projects/{project_id}/submit', method='POST', body=b'{"kind":"major"}',
                               headers={'Content-Type': 'application/json'})
        assert major.code == 200
        second = json.loads(major.body)
        frozen = self.fetch_api(f'submissions/{first["submission_id"]}/file?path=case.py')
        assert json.loads(frozen.body)['text'] == 'def broken(:\n'
        listed = self.fetch_api('submissions')
        assert {item['submission_id'] for item in json.loads(listed.body)} >= {first['submission_id'], second['submission_id']}
        diff = self.fetch_api('diff?before=%s&after=%s&path=case.py' % (first['submission_id'], second['submission_id']))
        assert diff.code == 200
        assert 'def later' in json.loads(diff.body)['diff']

    def test_distribution_claim_and_baseline_comparison_routes(self):
        if self.principal.role != 'teacher':
            pytest.skip('self-distribution fixture requires teacher')
        def post(route, body):
            return self.fetch_api(route, method='POST', body=json.dumps(body),
                                  headers={'Content-Type': 'application/json'})
        project = json.loads(post('projects', {'name': 'HTTP teacher baseline'}).body)
        frozen = json.loads(post('projects/' + project['project_id'] + '/submit', {'kind': 'minor'}).body)
        sent = post('distributions', {'submission_id': frozen['submission_id'], 'target_username': 'ly'})
        assert sent.code == 201
        distribution = json.loads(sent.body)
        listed = self.fetch_api('distributions')
        assert json.loads(listed.body)[0]['distribution_id'] == distribution['distribution_id']
        claimed = post('distributions/' + distribution['distribution_id'] + '/claim', {})
        assert claimed.code == 200
        student_project = json.loads(claimed.body)
        assert student_project['source'] == 'teacher-assigned'
        assert student_project['project_id'] != project['project_id']
        (Path(student_project['path']) / 'case.py').write_text('# independent changes\n')
        submitted = json.loads(post('projects/' + student_project['project_id'] + '/submit', {'kind': 'minor'}).body)
        diff = self.fetch_api('baseline-diff?submission_id=' + submitted['submission_id'] + '&path=case.py')
        assert diff.code == 200
        assert '+# independent changes' in json.loads(diff.body)['diff']
        assert self.fetch('/static/help.html').code == 200

    def test_direct_assignment_preview_dispatch_retry_claim_and_storage(self):
        import uuid
        if self.principal.role != 'teacher':
            pytest.skip('teacher task fixture')
        def post(route, value):
            return self.fetch_api(route,method='POST',body=json.dumps(value),headers={'Content-Type':'application/json'})
        payload={'request_id':str(uuid.uuid4()),'name':'统一分发练习','template_ref':'hello@1.0.0',
                 'dataset_ids':[],'targets':['ly'],'instructions':'领取后修改并提交','composition':None}
        preview=post('assignments/preview',payload)
        assert preview.code==200,preview.body
        content=json.loads(preview.body)
        assert content['algorithm_bindings']['scale']['parameters']['factor']==3
        payload['composition']=content['composition']
        rejected=post('assignments', {**payload,'algorithm_parameters':{'scale':{'factor':4}}})
        assert rejected.code==400  # task preparation cannot rewrite the case
        sent=post('assignments',payload)
        assert sent.code==201,sent.body
        result=json.loads(sent.body)
        assert json.loads(post('assignments',payload).body)==result
        assert json.loads(self.fetch_api('projects').body)==[]
        distribution=result['distributions'][0]
        claimed=post('distributions/'+distribution['distribution_id']+'/claim',{})
        assert claimed.code==200,claimed.body
        project=json.loads(claimed.body)
        import yaml
        document=yaml.safe_load((Path(project['path'])/'project.yaml').read_text())
        assert document['plugins']['scale']['parameters']['factor']==3
        assert len(json.loads(self.fetch_api('projects').body))==1
        stats=self.fetch_api('storage')
        assert stats.code==200,stats.body
        assert json.loads(stats.body)['saved_bytes']>0

    def test_http_body_limit_and_me_does_not_read_workspace(self):
        oversized = self.fetch_api('projects', method='POST', body=b'x' * (MAX_HTTP_BODY + 1),
                                   headers={'Content-Type': 'application/json'})
        assert oversized.code in {400, 413}
        with mock.patch.object(self.workspace, 'list_projects', side_effect=AssertionError('must not read')):
            assert self.fetch_api('me').code == 200

    def test_submission_preview_then_direct_prepare_creates_a_fixed_receipt_without_a_runner(self):
        import uuid
        def post(route,value):
            return self.fetch_api(route,method='POST',body=json.dumps(value),headers={'Content-Type':'application/json'})
        project=json.loads(post('projects',{'name':'提交前验证'}).body)
        plan=post('projects/'+project['project_id']+'/submission-preview',{'kind':'minor'})
        assert plan.code==200,plan.body
        content=json.loads(plan.body)
        assert content['case']['suggested_version']=='0.1'
        assert json.loads(self.fetch_api('submissions').body)==[]
        prepared=post('projects/'+project['project_id']+'/submission-prepare',{
            'kind':'minor','expected_revision':content['snapshot_revision'],'case_version':'0.1',
            'algorithm_versions':{},'request_id':str(uuid.uuid4())})
        assert prepared.code == 200, prepared.body
        receipt = json.loads(prepared.body)
        assert receipt['mode'] == 'direct' and receipt['status'] == 'succeeded'
        assert receipt['submission_id'] == receipt['final_submission_id']
        assert len(json.loads(self.fetch_api('submissions').body)) == 1
        with self.workspace.store.store._connection() as con:
            assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0

    def test_teacher_personal_submissions_hide_other_users_but_review_can_read(self):
        if self.principal.role!='teacher':pytest.skip('teacher account fixture')
        store=self.workspace.store.store
        other=Principal(self.principal.uid+10000,'test','student')
        project=store.create_project(other,'student exact snapshot')
        frozen=store.submit(project['project_id'],other,'minor',{'case.py':b'def run(context): return {}'},[])
        assert json.loads(self.fetch_api('submissions').body)==[]
        assert json.loads(self.fetch_api('submission-projects').body)['items']==[]
        assert self.fetch_api('submissions/'+frozen['submission_id']).code==400
        review=self.fetch_api('review/submissions/'+frozen['submission_id'])
        assert review.code==200,review.body
        assert json.loads(review.body)['project']['owner_username']=='test'
        assert json.loads(self.fetch_api('review/submission-projects').body)['items'][0]['project']['project_id']==project['project_id']

    def test_task_extra_tools_and_supplements_do_not_overwrite_case(self):
        import uuid
        if self.principal.role!='teacher':pytest.skip('teacher account fixture')
        def post(route,value):
            return self.fetch_api(route,method='POST',body=json.dumps(value),headers={'Content-Type':'application/json'})
        payload={'request_id':str(uuid.uuid4()),'name':'工具材料任务','template_ref':'hello@1.0.0',
                 'algorithm_refs':['example.scale@1.0.0'],'dataset_ids':[],'targets':[self.principal.username],'instructions':'保留基础代码'}
        sent=post('assignments',payload)
        assert sent.code==201,sent.body
        task=json.loads(sent.body)
        project=json.loads(post('distributions/'+task['distributions'][0]['distribution_id']+'/claim',{}).body)
        root=Path(project['path'])
        base=(root/'case.py').read_bytes()
        personal=self.fetch_api('tasks?personal=true')
        assert personal.code==200,personal.body
        assert json.loads(personal.body)['items'][0]['task_id']==task['task_id']
        details=json.loads(self.fetch_api('tasks/'+task['task_id']+'/materials?personal=true').body)
        materials=[row for row in details['materials'] if row['kind']=='algorithm']
        assert len(materials)==1
        added=post('projects/'+project['project_id']+'/task-materials',{'task_id':task['task_id'],'material_ids':[materials[0]['material_id']]})
        assert added.code==200,added.body
        assert (root/'case.py').read_bytes()==base

        import yaml
        bindings=yaml.safe_load((root/'project.yaml').read_text())['plugins']
        tool=root/next(iter(bindings.values()))['path']/'plugin.py'
        assert tool.exists()
        assert not list(root.glob('materials/tasks/*/*/plugin/plugin.py')), 'an algorithm already bound in the base case is not copied again'
        tool.write_text('# personal improvement\n')
        conflict=post('projects/'+project['project_id']+'/task-materials',{'task_id':task['task_id'],'material_ids':[materials[0]['material_id']]})
        assert conflict.code==200,conflict.body
        assert tool.read_text()=='# personal improvement\n'
        supplemental=post('tasks/'+task['task_id']+'/supplements',{'request_id':str(uuid.uuid4()),
            'algorithm_refs':[],'dataset_ids':[],'targets':[],'instructions':'先阅读基础案例再优化'})
        assert supplemental.code==201,supplemental.body
        assert (root/'case.py').read_bytes()==base

    def test_resource_package_can_be_claimed_directly_without_an_existing_project(self):
        import uuid
        if self.principal.role != 'teacher':
            pytest.skip('teacher account fixture')
        def post(route, value):
            return self.fetch_api(route, method='POST', body=json.dumps(value), headers={'Content-Type':'application/json'})
        payload = {'request_id':str(uuid.uuid4()), 'name':'独立算法包', 'template_ref':'',
                   'algorithm_refs':['example.scale@1.0.0'], 'dataset_ids':[],
                   'targets':[self.principal.username], 'instructions':'在个人项目中尝试工具。'}
        sent = post('assignments', payload)
        assert sent.code == 201, sent.body
        task = json.loads(sent.body)
        assert task['delivery_mode'] == 'resources' and task['distributions'] == []
        claimed = post('tasks/' + task['task_id'] + '/claim', {})
        assert claimed.code == 200, claimed.body
        first = json.loads(claimed.body)['project']
        assert self.workspace.store.store.get_project(first['project_id'], self.principal)['owner_username'] == self.principal.username
        repeated = post('tasks/' + task['task_id'] + '/claim', {})
        assert repeated.code == 200, repeated.body
        assert json.loads(repeated.body)['project']['project_id'] == first['project_id']
        assert post('tasks/' + task['task_id'] + '/claim', {'username':'other'}).code == 400

    def test_personal_resource_actions_use_existing_project_and_records_are_owner_only(self):
        def post(route, value):
            return self.fetch_api(route, method='POST', body=json.dumps(value), headers={'Content-Type':'application/json'})
        project = json.loads(post('projects', {'name':'个人研究'}).body)
        route = 'projects/' + project['project_id']
        root = Path(project['path'])
        before = (root/'case.py').read_bytes(), (root/'project.yaml').read_bytes()
        for _ in range(2):
            response = post(route+'/algorithm-materials', {'algorithm_refs':['example.scale@1.0.0']})
            assert response.code == 200, response.body
        assert len(list(root.glob('materials/algorithms/*/plugin/plugin.py'))) == 1
        assert ((root/'case.py').read_bytes(), (root/'project.yaml').read_bytes()) == before
        assert len(json.loads(self.fetch_api('projects').body)) == 1
        records = self.fetch_api(route+'/jobs')
        assert records.code == 410
        assert self.fetch_api(route+'/jobs?owner=other').code == 410
        outsider = Principal(self.principal.uid+20000, 'outsider', 'student')
        other = self.workspace.store.store.create_project(outsider, '私有项目')
        assert self.fetch_api('projects/'+other['project_id']+'/jobs').code == 410
        assert post('projects/'+other['project_id']+'/algorithm-materials', {'algorithm_refs':['example.scale@1.0.0']}).code == 400

    @mock.patch.dict(os.environ, {'ASYNC_TEST_TIMEOUT': '30'})
    def test_same_template_new_task_version_starts_independent_projects(self):
        import uuid
        if self.principal.role != 'teacher':
            pytest.skip('teacher account fixture')
        def post(route, value):
            return self.fetch_api(route, method='POST', body=json.dumps(value), headers={'Content-Type':'application/json'})
        payload = {'request_id':str(uuid.uuid4()), 'name':'同一任务', 'template_ref':'hello@1.0.0',
                   'algorithm_refs':[], 'dataset_ids':[], 'targets':[self.principal.username],
                   'instructions':'保留基础案例', 'include_baseline_algorithms':True}
        response = post('assignments', payload)
        assert response.code == 201, response.body
        first = json.loads(response.body)
        next_payload = {**payload, 'request_id':str(uuid.uuid4()), 'parent_task_id':first['task_id']}
        response = post('assignments', next_payload)
        assert response.code == 201, response.body
        second = json.loads(response.body)
        assert second['family_id'] == first['family_id'] and second['task_version'] == 2
        assert second['baseline_submission_id'] != first['baseline_submission_id']
        assert second['distributions'][0]['distribution_id'] != first['distributions'][0]['distribution_id']
        one_response = post('tasks/'+first['task_id']+'/claim', {})
        assert one_response.code == 200, one_response.body
        one = json.loads(one_response.body)['project']
        (Path(one['path'])/'case.py').write_text('# student work on version one\n')
        two_response = post('tasks/'+second['task_id']+'/claim', {})
        assert two_response.code == 200, two_response.body
        two = json.loads(two_response.body)['project']
        assert one['project_id'] != two['project_id']
        assert (Path(one['path'])/'case.py').read_text() == '# student work on version one\n'
        repeated = json.loads(post('tasks/'+second['task_id']+'/claim', {}).body)['project']
        assert repeated['project_id'] == two['project_id']
        assert len(json.loads(self.fetch_api('projects').body)) == 2




def test_first_login_creates_only_safe_policy_workspace():
    if pwd.getpwuid(os.getuid()).pw_name != 'ly':
        pytest.skip('policy fixture uses the designated teacher account')
    root = Path(tempfile.mkdtemp(prefix='.rk-web-first-login-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        target = root / 'created-workspace'
        data = {
            'schema': web_identity.POLICY_SCHEMA,
            'principals': [{'uid': os.getuid(), 'username': 'ly', 'role': 'teacher', 'workspace': str(target)}],
            'broker': {'uid': os.getuid() + 10000, 'username': 'researchkit-web-test'},
            'submission_store': '/var/lib/researchkit-web/submissions', 'code_root': '/opt/researchkit',
        }
        policy = web_identity.policy_from_data(data)
        _ensure_workspace(policy)
        assert target.is_dir() and stat.S_IMODE(target.stat().st_mode) == 0o700
        target.rmdir()
        (root / 'linked').symlink_to(root, target_is_directory=True)
        data['principals'][0]['workspace'] = str(root / 'linked' / 'escape')
        with pytest.raises(Exception, match='链接|不安全'):
            _ensure_workspace(web_identity.policy_from_data(data))
    finally:
        shutil.rmtree(root)


def test_port_zero_binds_only_loopback_with_actual_port(tmp_path: Path):
    username = pwd.getpwuid(os.getuid()).pw_name
    principal = Principal(os.getuid(), username, 'teacher' if username == 'ly' else 'student')
    root = Path(tempfile.mkdtemp(prefix='.rk-web-bind-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        workspace = Workspace(principal, root / 'workspace', LocalStoreClient(SubmissionStore(root / 'store'), principal))
        server, sockets, port = bind_loopback_server(application(workspace, 'b' * 48, demo=True), 0)
        try:
            assert port > 0
            assert all(sock.getsockname()[0] == '127.0.0.1' for sock in sockets)
        finally:
            server.stop()
            for sock in sockets:
                sock.close()
    finally:
        shutil.rmtree(root)
