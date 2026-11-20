"""Feedback through the HTTP adapter and authenticated broker protocol."""
from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from types import MappingProxyType

from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import LocalStoreClient, application
from researchkit.web_broker import BrokerClient, BrokerServer
from researchkit.web_feedback import FeedbackService
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


class TestFeedbackHTTP(AsyncHTTPTestCase):
    token = 'feedback-http-token-' * 3

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-feedback-http-', dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root)
        self.store = SubmissionStore(self.root / 'store')
        self.owner = Principal(os.getuid(), 'alice', 'student')
        self.other = Principal(os.getuid() + 10000, 'other', 'student')
        self.teacher = Principal(os.getuid() + 20000, 'ly', 'teacher')
        self.adapter = LocalStoreClient(self.store, self.owner)
        self.workspace = Workspace(self.owner, self.root / 'workspace', self.adapter)
        app = application(self.workspace, self.token, demo=True)
        self.addCleanup(app.wildcard_router.rules[0].target_kwargs['executor'].shutdown, wait=True)
        return app

    def api(self, route, body=None, expected=200):
        headers = {'Authorization': 'Bearer ' + self.token}
        kwargs = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            kwargs.update(method='POST', body=json.dumps(body))
        response = self.fetch('/api/' + route, headers=headers, **kwargs)
        assert response.code == expected, (response.code, response.body)
        return json.loads(response.body)

    def switch(self, principal):
        # Test-only trusted identity boundary, never supplied by HTTP headers/body.
        self.adapter.principal = principal
        self.workspace.principal = principal

    def test_user_admin_reply_status_and_read_receipts(self):
        first = self.api('feedback', {'subject': '运行反馈', 'body': '出现错误 <script>',
                                     'request_id': str(uuid.uuid4())}, 201)
        thread_id = first['thread_id']
        assert self.api('feedback')['total'] == 1
        self.api(f'feedback/{thread_id}/status', {'status': 'resolved'}, 403)
        self.switch(self.teacher)
        assert self.api('feedback')['unread_count'] == 1
        detail = self.api('feedback/' + thread_id)
        assert detail['messages'][0]['body'] == '出现错误 <script>'
        assert self.api('feedback')['unread_count'] == 1  # GET alone is not a read receipt.
        self.api(f'feedback/{thread_id}/read', {'message_id': detail['messages'][-1]['message_id']})
        assert self.api('feedback')['unread_count'] == 0
        self.api(f'feedback/{thread_id}/status', {'status': 'in_progress'})
        reply = {'body': '已修复，请重试。', 'request_id': str(uuid.uuid4())}
        sent = self.api(f'feedback/{thread_id}/reply', reply)
        assert self.api(f'feedback/{thread_id}/reply', reply) == sent
        self.api(f'feedback/{thread_id}/status', {'status': 'resolved'})
        self.switch(self.owner)
        assert self.api('feedback')['unread_count'] == 1
        result = self.api('feedback/' + thread_id)
        assert result['thread']['status'] == 'resolved'
        assert result['messages'][-1]['author_username'] == 'ly'
        assert len(result['messages']) == 2
        self.api(f'feedback/{thread_id}/reply', {'body': '还有一个问题', 'request_id': str(uuid.uuid4())})
        assert self.api('feedback/' + thread_id)['thread']['status'] == 'open'

    def test_no_cross_user_reads_spoofing_or_unvalidated_arguments(self):
        private = FeedbackService(self.store).create_thread(self.other, '其他用户', '私有问题', str(uuid.uuid4()))['thread_id']
        assert self.api('feedback')['total'] == 0
        self.api('feedback/' + private, expected=400)
        self.api(f'feedback/{private}/reply', {'body': '冒用', 'request_id': str(uuid.uuid4())}, 400)
        self.api(f'feedback/{private}/read', {'message_id': 1}, 400)
        self.api('feedback', {'subject': '伪造身份', 'body': '正文', 'request_id': str(uuid.uuid4()),
                              'owner_username': 'other'}, 400)
        for query in ('page=0', 'page=1&page=2', 'status=all&sender=other', 'page=true'):
            self.api('feedback?' + query, expected=400)
        assert self.fetch('/api/feedback').code == 403
        assert self.fetch('/static/feedback.js').code == 200


def test_feedback_through_real_broker_socket():
    root = Path(tempfile.mkdtemp(prefix='.rk-feedback-broker-', dir=Path.cwd()))
    account = pwd.getpwuid(os.getuid())
    teacher = Principal(account.pw_uid, account.pw_name, 'teacher' if account.pw_name == 'ly' else 'student')
    policy = Policy(principals=MappingProxyType({teacher.uid: teacher}), workspaces=MappingProxyType({}),
                    broker_uid=teacher.uid, broker_username=teacher.username, submission_store=root / 'store',
                    code_root=Path.cwd(), broker_socket=root / 'broker.sock')
    server = BrokerServer(policy)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
    thread.start()
    try:
        client = BrokerClient(server.socket_path, teacher.uid)
        request_id = str(uuid.uuid4())
        created = client.request('feedback_create', subject='真实通道', body='站内测试', request_id=request_id)
        repeated = client.request('feedback_create', subject='真实通道', body='站内测试', request_id=request_id)
        assert created == repeated
        detail = client.request('feedback_get', thread_id=created['thread_id'])
        assert len(detail['messages']) == 1
        assert detail['thread']['owner_username'] == teacher.username
        assert client.request('feedback_list')['total'] == 1
    finally:
        server.close()
        thread.join(timeout=10)
        shutil.rmtree(root)
    assert not thread.is_alive()
