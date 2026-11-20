import json
import os
from pathlib import Path
import pwd
import shutil
import tempfile
from dataclasses import replace
from types import MappingProxyType

from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import application
from researchkit.web_broker import BrokerServer
from researchkit.web_identity import Policy, Principal
from researchkit.web_workspace import Workspace


class TestDataManagementHTTP(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-data-review-http-', dir=Path.cwd()))
        self.teacher = Principal(os.getuid(), 'ly', 'teacher')
        policy = Policy(MappingProxyType({self.teacher.uid: self.teacher}), MappingProxyType({}),
                        os.getuid(), pwd.getpwuid(os.getuid()).pw_name, self.root / 'store', self.root)
        self.server = BrokerServer(policy)
        self.calls = []
        class Client:
            def request(_, method, **params):
                self.calls.append((method, params))
                return self.server._dispatch(self.workspace.principal, {'method': method, 'params': params})
        self.workspace = Workspace(self.teacher, self.root / 'workspace', Client())
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def api(self, route, body=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        return self.fetch('/api/' + route, method='GET' if body is None else 'POST', headers=headers,
                          body=None if body is None else json.dumps(body))

    def test_disabled_feature_keeps_existing_app_readable_and_new_script_is_served(self):
        result = self.api('data-management')
        assert result.code == 200 and json.loads(result.body)['configured'] is False
        assert self.fetch('/static/data-manager.js').code == 200
        assert self.api('data-management/scan', {}).code == 400
        assert self.api('me').code == 200

    def test_full_dataset_readme_rechecks_current_catalog_permission(self):
        import hashlib
        from researchkit.web_readme_cache import initialize
        body = '# 正文\n\n' + ('说明内容。\n' * 4000) + '\n## 末尾章节\n'
        source_ref = 'README:README.md#sha256=' + hashlib.sha256(body.encode()).hexdigest()
        with self.server.store._connection() as connection:
            initialize(connection)
            connection.execute('INSERT INTO data_management_readme_bodies VALUES(?,?,?)',
                ('b'*24, source_ref, body))
        self.server._dataset_catalog = lambda principal: ([{'dataset_id':'allowed', 'provenance_ref':source_ref,
            'description':'短摘要'}] if principal.username == 'ly' else [])
        response = self.api('datasets/allowed/readme')
        assert response.code == 200 and json.loads(response.body)['markdown'] == body
        assert json.loads(response.body)['complete'] is True
        assert json.loads(response.body)['original'] == body
        assert self.api('datasets/other/readme').code == 400
        assert self.api('datasets/allowed/readme?path=/etc/passwd').code == 400
        self.workspace.principal = Principal(self.teacher.uid+1000, 'test', 'student')
        denied = self.api('datasets/allowed/readme')
        assert denied.code == 400 and '末尾章节'.encode() not in denied.body

    def test_student_cannot_read_or_queue_teacher_dataset_management(self):
        self.workspace.principal = Principal(os.getuid(), 'student', 'student')
        for route, body in [('data-management', None), ('data-management/candidates', None),
                            ('data-management/scan', {}),
                            ('data-management/candidates/' + 'a' * 24 + '/access', {'revision': 1, 'access_token': 'a'*64, 'readers':'platform-users'}),
                            ('data-management/candidates/' + 'a' * 24 + '/review', {'revision': 1, 'decision': 'approve'})]:
            assert self.api(route, body).code == 403
        assert self.calls == []

    def test_http_rejects_unknown_fields_paths_duplicate_filters_and_non_integer_page(self):
        assert self.api('data-management/scan', {'raw_root': '/etc'}).code == 400
        assert self.api('data-management/candidates?limit=5&limit=6').code == 400
        assert self.api('data-management/candidates?offset=-1').code == 400
        assert self.api('data-management/candidates?username=ly').code == 400
        assert self.api('data-management?root=/etc').code == 400
        assert self.api('data-management/candidates/' + 'a' * 24 + '/review',
                        {'revision': 1, 'decision': 'approve', 'role': 'teacher'}).code == 400
        assert self.calls == []

    def test_new_routes_translate_only_review_fields_and_handle_conflicts(self):
        captured = []
        self.workspace.store.request = lambda method, **params: captured.append((method, params)) or {'items': [], 'total': 0}
        result = self.api('data-management/candidates?status=active&query=sonar&offset=50&limit=50')
        assert result.code == 200
        assert captured[-1] == ('data_management_list', {'status': 'active', 'query': 'sonar', 'offset': 50, 'limit': 50})
        result = self.api('data-management/candidates/' + 'b' * 24 + '/review',
                          {'revision': 3, 'decision': 'approve', 'metadata': {'description': '中文说明'}})
        assert result.code == 200
        assert captured[-1] == ('data_management_review', {'candidate_id': 'b' * 24, 'revision': 3,
                                                          'decision': 'approve', 'metadata': {'description': '中文说明'}})
        result = self.api('data-management/candidates/' + 'b' * 24 + '/access',
                          {'revision': 3, 'access_token': 'a'*64, 'readers':['ly','test']})
        assert result.code == 202
        assert captured[-1] == ('data_management_access', {'candidate_id': 'b'*24, 'revision': 3,
                                                          'access_token': 'a'*64, 'readers':['ly','test']})
        count = len(captured)
        for extra in ({'raw_root':'/etc'}, {'dataset_id':'other'}, {'role':'teacher'}, {'metadata':{}}):
            assert self.api('data-management/candidates/' + 'b'*24 + '/access',
                            {'revision':3, 'access_token':'a'*64, 'readers':['ly'], **extra}).code == 400
        assert len(captured) == count

    def test_tag_filters_are_bounded_typed_and_forwarded_without_identity_fields(self):
        from urllib.parse import urlencode
        captured = []
        self.workspace.store.request = lambda method, **params: captured.append((method, params)) or {'items': [], 'total': 0}
        response = self.api('data-management/candidates?' + urlencode({'tags': json.dumps(['GIS', '降雨']), 'tag_mode': 'all'}))
        assert response.code == 200
        assert captured[-1] == ('data_management_list', {'tags': ['GIS', '降雨'], 'tag_mode': 'all'})
        before = len(captured)
        for query in ('tags=GIS', 'tags=%7B%7D', 'tags=%5B%5D&tags=%5B%5D', 'tag_mode=all&tag_mode=any'):
            assert self.api('data-management/candidates?' + query).code == 400
        assert len(captured) == before
