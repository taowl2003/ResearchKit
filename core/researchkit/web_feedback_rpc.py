"""Feedback operations shared by the authenticated broker and local adapter."""
from __future__ import annotations

from .files import KitError
from .web_feedback import FeedbackService


FIELDS = {
    'feedback_list': ({'page', 'status'}, set()),
    'feedback_get': ({'thread_id', 'before_id'}, {'thread_id'}),
    'feedback_create': ({'subject', 'body', 'request_id'}, {'subject', 'body', 'request_id'}),
    'feedback_reply': ({'thread_id', 'body', 'request_id'}, {'thread_id', 'body', 'request_id'}),
    'feedback_read': ({'thread_id', 'message_id'}, {'thread_id', 'message_id'}),
    'feedback_status': ({'thread_id', 'status'}, {'thread_id', 'status'}),
}
METHODS = frozenset(FIELDS)


def request(store, principal, method, params):
    if method not in METHODS:
        raise KitError('消息操作无效')
    allowed, required = FIELDS[method]
    if type(params) is not dict or set(params) - allowed or required - set(params):
        raise KitError('消息请求字段不合法')
    service = FeedbackService(store)
    action = {
        'feedback_list': service.list_threads,
        'feedback_get': service.get_thread,
        'feedback_create': service.create_thread,
        'feedback_reply': service.reply,
        'feedback_read': service.mark_read,
        'feedback_status': service.set_status,
    }[method]
    return action(principal, **params)
