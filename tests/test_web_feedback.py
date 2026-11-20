"""Unit coverage for private user-to-teacher feedback threads."""
from __future__ import annotations

import uuid
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_feedback import FeedbackService
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
ALICE = Principal(1002, 'alice', 'student')
BOB = Principal(1003, 'bob', 'student')


@pytest.fixture
def private_root():
    root = Path(tempfile.mkdtemp(prefix='.rk-feedback-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def feedback(private_root: Path):
    return FeedbackService(SubmissionStore(private_root / 'store'))


def _new_id() -> str:
    return str(uuid.uuid4())


def _thread(service: FeedbackService, *, subject: str = '运行问题', body: str = '我遇到了一个问题') -> str:
    return str(service.create_thread(ALICE, subject, body, _new_id())['thread_id'])


def test_user_teacher_round_trip_status_and_reopen(feedback: FeedbackService):
    thread_id = _thread(feedback)
    teacher_threads = feedback.list_threads(TEACHER)
    assert teacher_threads['total'] == 1
    assert teacher_threads['threads'][0] == {
        'thread_id': thread_id, 'subject': '运行问题', 'status': 'open',
        'owner_username': 'alice', 'created_at': teacher_threads['threads'][0]['created_at'],
        'updated_at': teacher_threads['threads'][0]['updated_at'], 'unread_count': 1,
        'last_preview': '我遇到了一个问题',
    }
    assert feedback.get_thread(TEACHER, thread_id)['messages'][0]['author_username'] == 'alice'
    assert feedback.set_status(TEACHER, thread_id, 'in_progress') == {
        'thread_id': thread_id, 'status': 'in_progress'}
    reply = feedback.reply(TEACHER, thread_id, '已定位，请重新提交后再试。', _new_id())
    assert reply['thread_id'] == thread_id and reply['message_id'] > 0
    assert feedback.set_status(TEACHER, thread_id, 'resolved')['status'] == 'resolved'
    feedback.reply(ALICE, thread_id, '仍然无法运行。', _new_id())
    detail = feedback.get_thread(ALICE, thread_id)
    assert detail['thread']['status'] == 'open'
    assert [message['author_role'] for message in detail['messages']] == ['student', 'teacher', 'student']
    assert feedback.list_threads(ALICE)['unread_count'] == 1


def test_identity_isolation_and_teacher_only_operations(feedback: FeedbackService):
    thread_id = _thread(feedback)
    for impersonator in (Principal(ALICE.uid, 'other', 'student'), Principal(9999, 'alice', 'student'), BOB):
        assert feedback.list_threads(impersonator)['threads'] == []
        with pytest.raises(KitError, match='无权'):
            feedback.get_thread(impersonator, thread_id)
        with pytest.raises(KitError, match='无权'):
            feedback.reply(impersonator, thread_id, '伪造回复', _new_id())
        with pytest.raises(KitError, match='无权'):
            feedback.mark_read(impersonator, thread_id, 1)
    with pytest.raises(KitError, match='只有教师'):
        feedback.set_status(ALICE, thread_id, 'resolved')
    own = feedback.create_thread(TEACHER, '教师自用测试', 'LY 可以使用同一入口自测。', _new_id())
    assert feedback.get_thread(TEACHER, own['thread_id'])['thread']['owner_username'] == 'ly'
    assert feedback.list_threads(ALICE)['total'] == 1
    with pytest.raises(KitError, match='可信身份'):
        feedback.list_threads(Principal(1001, 'ly', 'student'))


def test_idempotency_rejects_reused_request_for_different_content(feedback: FeedbackService):
    request_id = _new_id()
    first = feedback.create_thread(ALICE, '重复请求', '第一次内容', request_id)
    assert feedback.create_thread(ALICE, '重复请求', '第一次内容', request_id) == first
    assert feedback.list_threads(ALICE)['total'] == 1
    with pytest.raises(KitError, match='已用于其他内容'):
        feedback.create_thread(ALICE, '重复请求', '不同内容', request_id)
    message_request = _new_id()
    one = feedback.reply(ALICE, first['thread_id'], '只应保存一次', message_request)
    assert feedback.reply(ALICE, first['thread_id'], '只应保存一次', message_request) == one
    with pytest.raises(KitError, match='已用于其他内容'):
        feedback.reply(ALICE, first['thread_id'], '内容不同', message_request)
    assert len(feedback.get_thread(ALICE, first['thread_id'])['messages']) == 2


def test_pagination_and_reads_do_not_mark_messages_not_seen(feedback: FeedbackService):
    thread_id = _thread(feedback)
    initial_id = feedback.get_thread(ALICE, thread_id)['messages'][0]['message_id']
    for number in range(55):
        feedback.reply(ALICE, thread_id, f'补充说明 {number}', _new_id())
    latest = feedback.get_thread(TEACHER, thread_id)
    assert len(latest['messages']) == 50
    assert latest['has_more'] is True
    assert latest['next_before_id'] == latest['messages'][0]['message_id']
    older = feedback.get_thread(TEACHER, thread_id, latest['next_before_id'])
    assert len(older['messages']) == 6
    assert older['has_more'] is False and older['next_before_id'] is None

    # Reading never changes a receipt.  A new message that arrives after the
    # old one was shown must remain unread after marking exactly that old ID.
    feedback.mark_read(TEACHER, thread_id, initial_id)
    feedback.reply(ALICE, thread_id, '并发到达的新消息', _new_id())
    feedback.mark_read(TEACHER, thread_id, initial_id)
    listed = feedback.list_threads(TEACHER)
    assert listed['unread_count'] == 56
    assert listed['threads'][0]['unread_count'] == 56


def test_validation_and_failed_write_leave_no_partial_thread(feedback: FeedbackService):
    with pytest.raises(KitError, match='主题不能为空'):
        feedback.create_thread(ALICE, ' \n', '正文', _new_id())
    with pytest.raises(KitError, match='反馈内容不能为空'):
        feedback.create_thread(ALICE, '有效主题', '  ', _new_id())
    with pytest.raises(KitError, match='UUID'):
        feedback.create_thread(ALICE, '有效主题', '正文', 'not-a-uuid')
    with pytest.raises(KitError, match='不得超过 120'):
        feedback.create_thread(ALICE, '甲' * 121, '正文', _new_id())
    with pytest.raises(KitError, match='不得超过 20000'):
        feedback.create_thread(ALICE, '有效主题', '甲' * 20_001, _new_id())
    assert feedback.list_threads(ALICE)['total'] == 0
    with feedback.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) AS count FROM web_feedback_messages').fetchone()['count'] == 0
        assert connection.execute('SELECT COUNT(*) AS count FROM web_feedback_requests').fetchone()['count'] == 0


def test_create_and_reply_rollback_after_writes_begin(feedback: FeedbackService, monkeypatch):
    """A crash after message insertion must leave no durable half-thread."""
    original = feedback._remember_request

    def interrupted(*_args, **_kwargs):
        raise RuntimeError('simulated persistence interruption')

    create_request = _new_id()
    monkeypatch.setattr(feedback, '_remember_request', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        feedback.create_thread(ALICE, '中断创建', '这条不应留下。', create_request)
    with feedback.store._connection() as connection:
        for table in ('web_feedback_threads', 'web_feedback_messages', 'web_feedback_requests'):
            assert connection.execute(f'SELECT COUNT(*) AS count FROM {table}').fetchone()['count'] == 0

    monkeypatch.setattr(feedback, '_remember_request', original)
    created = feedback.create_thread(ALICE, '中断创建', '这次应成功。', create_request)
    assert feedback.get_thread(ALICE, created['thread_id'])['messages'][0]['body'] == '这次应成功。'

    feedback.set_status(TEACHER, created['thread_id'], 'resolved')
    before = feedback.get_thread(ALICE, created['thread_id'])
    before_count = len(before['messages'])
    before_updated = before['thread']['updated_at']
    reply_request = _new_id()
    monkeypatch.setattr(feedback, '_remember_request', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        feedback.reply(ALICE, created['thread_id'], '这条回复也不应留下。', reply_request)
    after = feedback.get_thread(ALICE, created['thread_id'])
    assert len(after['messages']) == before_count
    assert after['thread']['status'] == 'resolved'
    assert after['thread']['updated_at'] == before_updated

    monkeypatch.setattr(feedback, '_remember_request', original)
    reply = feedback.reply(ALICE, created['thread_id'], '这次回复应成功。', reply_request)
    assert reply['thread_id'] == created['thread_id']
    assert feedback.get_thread(ALICE, created['thread_id'])['thread']['status'] == 'open'


def test_schema_is_additive_for_existing_submission_store(private_root: Path):
    store = SubmissionStore(private_root / 'store')
    project = store.create_project(ALICE, 'already here')
    with store._connection() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='web_feedback_threads'").fetchone() is None
    service = FeedbackService(store)
    assert service.create_thread(ALICE, '升级后反馈', '数据库没有被重建。', _new_id())['thread_id']
    assert store.get_project(str(project['project_id']), ALICE)['name'] == 'already here'


def test_invalid_message_thread_pair_never_advances_receipt(feedback: FeedbackService):
    first = _thread(feedback, subject='第一条')
    second = _thread(feedback, subject='第二条')
    foreign_message = feedback.get_thread(ALICE, second)['messages'][0]['message_id']
    with pytest.raises(KitError, match='不属于'):
        feedback.mark_read(TEACHER, first, foreign_message)
    with feedback.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) AS count FROM web_feedback_reads').fetchone()['count'] == 0
