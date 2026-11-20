"""Private, broker-side feedback threads between Web users and ``ly``.

The service deliberately receives a server-derived :class:`Principal`; neither
the sender nor the recipient is supplied by a browser.  A student can create
and continue only their own thread, while the one configured teacher account
(``ly``) can read, reply to, and classify every thread.  LY may also create a
private self-feedback thread from the same personal entry point.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from .files import KitError
from .web_store import _principal


_PAGE_SIZE = 20
_MESSAGE_PAGE_SIZE = 50
_STATUSES = {'open', 'in_progress', 'resolved'}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise KitError(f'{label} 必须是 UUID')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise KitError(f'{label} 必须是 UUID') from exc
    if str(parsed) != value:
        raise KitError(f'{label} 必须使用小写标准 UUID')
    return value


def _text(value: object, *, label: str, maximum: int, single_line: bool = False) -> str:
    if not isinstance(value, str):
        raise KitError(f'{label}必须是文本')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise KitError(f'{label}必须是 UTF-8 文本') from exc
    result = value.strip()
    if not result:
        raise KitError(f'{label}不能为空')
    if len(result) > maximum:
        raise KitError(f'{label}不得超过 {maximum} 字')
    if '\x00' in result or any(ord(char) < 32 and char not in '\n\r\t' for char in result):
        raise KitError(f'{label}包含非法控制字符')
    if single_line and any(char in result for char in '\n\r\t'):
        raise KitError(f'{label}不能包含换行或制表符')
    return result


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KitError(f'{label}必须是正整数')
    return value


def _request_fingerprint(action: str, **fields: object) -> str:
    payload = json.dumps({'action': action, **fields}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class FeedbackService:
    """Persisted internal feedback threads.

    Instances are cheap and may be created for each broker request.  Schema
    creation is therefore additive and idempotent on an existing submissions
    database.
    """

    def __init__(self, store) -> None:
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS web_feedback_threads (
                    thread_id TEXT PRIMARY KEY,
                    owner_uid INTEGER NOT NULL,
                    owner_username TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_feedback_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES web_feedback_threads(thread_id),
                    author_uid INTEGER NOT NULL,
                    author_username TEXT NOT NULL,
                    author_role TEXT NOT NULL CHECK(author_role IN ('teacher','student')),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_feedback_threads_owner_updated
                    ON web_feedback_threads(owner_uid, owner_username, updated_at DESC, thread_id DESC)''')
                connection.execute('''CREATE INDEX IF NOT EXISTS web_feedback_messages_thread_id
                    ON web_feedback_messages(thread_id, message_id DESC)''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_feedback_reads (
                    thread_id TEXT NOT NULL REFERENCES web_feedback_threads(thread_id),
                    reader_uid INTEGER NOT NULL,
                    reader_username TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, reader_uid, reader_username)
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS web_feedback_requests (
                    request_id TEXT PRIMARY KEY,
                    actor_uid INTEGER NOT NULL,
                    actor_username TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('create_thread','reply')),
                    fingerprint TEXT NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES web_feedback_threads(thread_id),
                    message_id INTEGER NOT NULL REFERENCES web_feedback_messages(message_id),
                    created_at TEXT NOT NULL
                )''')
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _identity(principal) -> tuple[int, str, str]:
        return _principal(principal)

    @staticmethod
    def _is_teacher(identity: tuple[int, str, str]) -> bool:
        _uid, username, role = identity
        return role == 'teacher' and username == 'ly'

    @classmethod
    def _require_teacher(cls, identity: tuple[int, str, str]) -> None:
        if not cls._is_teacher(identity):
            raise KitError('只有教师 ly 可以处理用户反馈')

    @staticmethod
    def _page(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
            raise KitError('页码必须是 1 至 10000 的整数')
        return value

    @staticmethod
    def _status(value: object, *, allow_all: bool = False) -> str:
        values = _STATUSES | ({'all'} if allow_all else set())
        if not isinstance(value, str) or value not in values:
            choices = '、'.join(sorted(values))
            raise KitError(f'状态必须是：{choices}')
        return value

    @staticmethod
    def _thread_row(connection: sqlite3.Connection, identity: tuple[int, str, str],
                    thread_id: object) -> sqlite3.Row:
        thread_id = _uuid(thread_id, label='反馈会话 ID')
        row = connection.execute('SELECT * FROM web_feedback_threads WHERE thread_id=?', (thread_id,)).fetchone()
        if row is None:
            raise KitError('反馈会话不存在')
        uid, username, _role = identity
        if not FeedbackService._is_teacher(identity) and (int(row['owner_uid']), str(row['owner_username'])) != (uid, username):
            raise KitError('无权查看该反馈会话')
        return row

    @staticmethod
    def _unread_sql(identity: tuple[int, str, str]) -> tuple[str, tuple[object, ...]]:
        uid, username, _role = identity
        visible = '' if FeedbackService._is_teacher(identity) else 'AND threads.owner_uid=? AND threads.owner_username=?'
        # A user never has unread messages authored by themself.  This exact
        # UID+name comparison also keeps an identity collision from bypassing
        # the ownership rule.
        sql = f'''SELECT COUNT(*) AS count
                  FROM web_feedback_messages messages
                  JOIN web_feedback_threads threads ON threads.thread_id=messages.thread_id
                  LEFT JOIN web_feedback_reads reads
                    ON reads.thread_id=threads.thread_id AND reads.reader_uid=? AND reads.reader_username=?
                  WHERE messages.message_id>COALESCE(reads.last_read_message_id, 0)
                    AND (messages.author_uid<>? OR messages.author_username<>?) {visible}'''
        values: tuple[object, ...] = (uid, username, uid, username)
        if visible:
            values += (uid, username)
        return sql, values

    @classmethod
    def _unread_count(cls, connection: sqlite3.Connection, identity: tuple[int, str, str],
                      thread_id: str | None = None) -> int:
        sql, values = cls._unread_sql(identity)
        if thread_id is not None:
            sql += ' AND threads.thread_id=?'
            values += (thread_id,)
        return int(connection.execute(sql, values).fetchone()['count'])

    @staticmethod
    def _preview(value: str) -> str:
        return ' '.join(value.split())[:240]

    def list_threads(self, principal, page: int = 1, status: str = 'all') -> dict[str, object]:
        identity = self._identity(principal)
        page = self._page(page)
        status = self._status(status, allow_all=True)
        uid, username, _role = identity
        where: list[str] = []
        values: list[object] = []
        if not self._is_teacher(identity):
            where += ['owner_uid=?', 'owner_username=?']
            values += [uid, username]
        if status != 'all':
            where.append('status=?')
            values.append(status)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.store._connection() as connection:
            total = int(connection.execute('SELECT COUNT(*) AS count FROM web_feedback_threads' + clause, values).fetchone()['count'])
            rows = connection.execute(
                '''SELECT threads.*, (
                       SELECT body FROM web_feedback_messages messages
                        WHERE messages.thread_id=threads.thread_id
                        ORDER BY messages.message_id DESC LIMIT 1
                   ) AS last_body
                   FROM web_feedback_threads threads''' + clause +
                ' ORDER BY threads.updated_at DESC, threads.thread_id DESC LIMIT ? OFFSET ?',
                [*values, _PAGE_SIZE, (page - 1) * _PAGE_SIZE],
            ).fetchall()
            threads = [{
                'thread_id': str(row['thread_id']), 'subject': str(row['subject']),
                'status': str(row['status']), 'owner_username': str(row['owner_username']),
                'created_at': str(row['created_at']), 'updated_at': str(row['updated_at']),
                'unread_count': self._unread_count(connection, identity, str(row['thread_id'])),
                'last_preview': self._preview(str(row['last_body'] or '')),
            } for row in rows]
            unread_count = self._unread_count(connection, identity)
        return {'threads': threads, 'total': total, 'page': page, 'page_size': _PAGE_SIZE,
                'unread_count': unread_count}

    def get_thread(self, principal, thread_id: str, before_id: int | None = None) -> dict[str, object]:
        identity = self._identity(principal)
        if before_id is not None:
            before_id = _positive_int(before_id, label='消息分页 ID')
        with self.store._connection() as connection:
            thread = self._thread_row(connection, identity, thread_id)
            query = 'SELECT * FROM web_feedback_messages WHERE thread_id=?'
            values: list[object] = [thread['thread_id']]
            if before_id is not None:
                query += ' AND message_id<?'
                values.append(before_id)
            rows = connection.execute(query + ' ORDER BY message_id DESC LIMIT ?', [*values, _MESSAGE_PAGE_SIZE + 1]).fetchall()
        has_more = len(rows) > _MESSAGE_PAGE_SIZE
        visible = list(reversed(rows[:_MESSAGE_PAGE_SIZE]))
        next_before_id = int(visible[0]['message_id']) if has_more and visible else None
        return {
            'thread': {
                'thread_id': str(thread['thread_id']), 'subject': str(thread['subject']),
                'status': str(thread['status']), 'owner_username': str(thread['owner_username']),
                'created_at': str(thread['created_at']), 'updated_at': str(thread['updated_at']),
            },
            'messages': [{
                'message_id': int(row['message_id']), 'author_username': str(row['author_username']),
                'author_role': str(row['author_role']), 'body': str(row['body']),
                'created_at': str(row['created_at']),
            } for row in visible],
            'has_more': has_more, 'next_before_id': next_before_id,
        }

    def mark_read(self, principal, thread_id: str, message_id: int) -> dict[str, object]:
        identity = self._identity(principal)
        message_id = _positive_int(message_id, label='消息 ID')
        uid, username, _role = identity
        now = _timestamp()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                thread = self._thread_row(connection, identity, thread_id)
                message = connection.execute(
                    'SELECT message_id FROM web_feedback_messages WHERE thread_id=? AND message_id=?',
                    (thread['thread_id'], message_id),
                ).fetchone()
                if message is None:
                    raise KitError('消息不属于该反馈会话')
                connection.execute('''INSERT INTO web_feedback_reads
                    (thread_id,reader_uid,reader_username,last_read_message_id,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(thread_id,reader_uid,reader_username) DO UPDATE SET
                        last_read_message_id=MAX(web_feedback_reads.last_read_message_id, excluded.last_read_message_id),
                        updated_at=excluded.updated_at''',
                    (thread['thread_id'], uid, username, message_id, now),
                )
                row = connection.execute('''SELECT last_read_message_id FROM web_feedback_reads
                    WHERE thread_id=? AND reader_uid=? AND reader_username=?''',
                    (thread['thread_id'], uid, username)).fetchone()
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'thread_id': str(thread['thread_id']), 'message_id': message_id,
                'last_read_message_id': int(row['last_read_message_id'])}

    @staticmethod
    def _request_result(connection: sqlite3.Connection, identity: tuple[int, str, str], request_id: str,
                        action: str, fingerprint: str) -> tuple[str, int] | None:
        row = connection.execute('SELECT * FROM web_feedback_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        uid, username, _role = identity
        if (int(row['actor_uid']), str(row['actor_username']), str(row['action']), str(row['fingerprint'])) != (uid, username, action, fingerprint):
            raise KitError('同一消息请求 ID 已用于其他内容，请新建请求')
        return str(row['thread_id']), int(row['message_id'])

    @staticmethod
    def _remember_request(connection: sqlite3.Connection, identity: tuple[int, str, str], request_id: str,
                          action: str, fingerprint: str, thread_id: str, message_id: int, now: str) -> None:
        uid, username, _role = identity
        connection.execute('''INSERT INTO web_feedback_requests
            (request_id,actor_uid,actor_username,action,fingerprint,thread_id,message_id,created_at)
            VALUES(?,?,?,?,?,?,?,?)''', (request_id, uid, username, action, fingerprint, thread_id, message_id, now))

    def create_thread(self, principal, subject: str, body: str, request_id: str) -> dict[str, object]:
        identity = self._identity(principal)
        subject = _text(subject, label='主题', maximum=120, single_line=True)
        body = _text(body, label='反馈内容', maximum=20_000)
        request_id = _uuid(request_id, label='消息请求 ID')
        fingerprint = _request_fingerprint('create_thread', subject=subject, body=body)
        uid, username, role = identity
        now = _timestamp()
        thread_id = str(uuid.uuid4())
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                existing = self._request_result(connection, identity, request_id, 'create_thread', fingerprint)
                if existing is not None:
                    connection.execute('COMMIT')
                    return {'thread_id': existing[0]}
                connection.execute('''INSERT INTO web_feedback_threads
                    (thread_id,owner_uid,owner_username,subject,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)''', (thread_id, uid, username, subject, 'open', now, now))
                cursor = connection.execute('''INSERT INTO web_feedback_messages
                    (thread_id,author_uid,author_username,author_role,body,created_at)
                    VALUES(?,?,?,?,?,?)''', (thread_id, uid, username, role, body, now))
                message_id = int(cursor.lastrowid)
                self._remember_request(connection, identity, request_id, 'create_thread', fingerprint,
                                       thread_id, message_id, now)
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'thread_id': thread_id}

    def reply(self, principal, thread_id: str, body: str, request_id: str) -> dict[str, object]:
        identity = self._identity(principal)
        thread_id = _uuid(thread_id, label='反馈会话 ID')
        body = _text(body, label='回复内容', maximum=20_000)
        request_id = _uuid(request_id, label='消息请求 ID')
        fingerprint = _request_fingerprint('reply', thread_id=thread_id, body=body)
        uid, username, role = identity
        now = _timestamp()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                existing = self._request_result(connection, identity, request_id, 'reply', fingerprint)
                if existing is not None:
                    connection.execute('COMMIT')
                    return {'thread_id': existing[0], 'message_id': existing[1]}
                thread = self._thread_row(connection, identity, thread_id)
                cursor = connection.execute('''INSERT INTO web_feedback_messages
                    (thread_id,author_uid,author_username,author_role,body,created_at)
                    VALUES(?,?,?,?,?,?)''', (thread['thread_id'], uid, username, role, body, now))
                message_id = int(cursor.lastrowid)
                status = 'open' if not self._is_teacher(identity) and thread['status'] == 'resolved' else str(thread['status'])
                connection.execute('UPDATE web_feedback_threads SET status=?,updated_at=? WHERE thread_id=?',
                                   (status, now, thread['thread_id']))
                self._remember_request(connection, identity, request_id, 'reply', fingerprint,
                                       str(thread['thread_id']), message_id, now)
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'thread_id': thread_id, 'message_id': message_id}

    def set_status(self, principal, thread_id: str, status: str) -> dict[str, object]:
        identity = self._identity(principal)
        self._require_teacher(identity)
        status = self._status(status)
        now = _timestamp()
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                thread = self._thread_row(connection, identity, thread_id)
                connection.execute('UPDATE web_feedback_threads SET status=?,updated_at=? WHERE thread_id=?',
                                   (status, now, thread['thread_id']))
                connection.execute('COMMIT')
            except Exception:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise
        return {'thread_id': str(thread['thread_id']), 'status': status}
