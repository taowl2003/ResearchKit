"""Server-side fixed-submission browser tests."""

from __future__ import annotations

import sqlite3
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient, application
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


TEACHER = Principal(1000, 'ly', 'teacher')
STUDENT_A = Principal(1001, 'student-a', 'student')
STUDENT_B = Principal(1002, 'student-b', 'student')


@pytest.fixture
def store(tmp_path: Path) -> SubmissionStore:
    return SubmissionStore(tmp_path / 'private-submissions')


def _submission(store: SubmissionStore, owner: Principal, name: str, *, kind: str = 'major') -> dict[str, object]:
    project = store.create_project(owner, name)
    submission = store.submit(project['project_id'], owner, kind, {'case.py': name.encode()}, [])
    return {'project': project, 'submission': submission}


def _ids(page: dict[str, object]) -> list[str]:
    return [item['project']['project_id'] for item in page['items']]  # type: ignore[index]


def test_project_browser_groups_by_project_and_enforces_owner(store: SubmissionStore):
    first = _submission(store, STUDENT_A, 'same name')
    second = _submission(store, STUDENT_A, 'same name')
    teacher = _submission(store, TEACHER, 'teacher study')
    page = store.list_submission_projects(TEACHER, limit=20)
    assert set(_ids(page)) == {first['project']['project_id'], second['project']['project_id'], teacher['project']['project_id']}
    same = [item for item in page['items'] if item['project']['name'] == 'same name']  # type: ignore[index]
    assert len(same) == 2 and {item['project']['owner_username'] for item in same} == {'student-a'}  # type: ignore[index]
    assert all('manifest' not in item['preview'] for item in page['items'])  # type: ignore[index]
    assert first['project']['owner_username'] == 'student-a'

    student_page = store.list_submission_projects(STUDENT_A, limit=20)
    assert set(_ids(student_page)) == {first['project']['project_id'], second['project']['project_id']}
    assert store.list_submission_projects(STUDENT_A, owner='ly', limit=20)['items'] == []
    with pytest.raises(KitError, match='无权'):
        store.list_project_versions(STUDENT_A, teacher['project']['project_id'])
    assert store.list_project_versions(TEACHER, first['project']['project_id'])['items'][0]['submission_id'] == first['submission']['submission_id']


def test_teacher_review_previews_latest_minor_without_changing_personal_major_preference(store):
    from researchkit.web_workflow_rpc import request
    first = _submission(store, STUDENT_A, 'review latest')
    minor = store.submit(first['project']['project_id'], STUDENT_A, 'minor', {'case.py': b'newest minor'}, [])
    _submission(store, STUDENT_B, 'other owner')
    personal = store.list_submission_projects(STUDENT_A, personal=True)
    assert personal['items'][0]['preview']['submission_id'] == first['submission']['submission_id']
    page = request(store, Path('/unused'), TEACHER, 'review_submission_projects', {'limit': 1})
    later = request(store, Path('/unused'), TEACHER, 'review_submission_projects',
                    {'limit': 1, 'cursor': page['next_cursor']})
    item = next(item for item in page['items'] + later['items'] if item['project']['owner_username'] == 'student-a')
    assert item['preview']['submission_id'] == minor['submission_id']
    with pytest.raises(KitError, match='游标'):
        store.list_submission_projects(TEACHER, limit=1, cursor=page['next_cursor'])


def test_filters_use_escaped_search_and_china_date_boundaries(store: SubmissionStore):
    before = _submission(store, STUDENT_A, 'before')
    start = _submission(store, STUDENT_A, 'literal 100%_name')
    end = _submission(store, STUDENT_A, 'end')
    after = _submission(store, STUDENT_A, 'after')
    with store._connection() as connection:
        connection.execute('UPDATE submissions SET created_at = ? WHERE id = ?', ('2026-10-09T15:59:59+00:00', before['submission']['submission_id']))
        connection.execute('UPDATE submissions SET created_at = ? WHERE id = ?', ('2026-10-09T16:00:00+00:00', start['submission']['submission_id']))
        connection.execute('UPDATE submissions SET created_at = ? WHERE id = ?', ('2026-10-10T15:59:59+00:00', end['submission']['submission_id']))
        connection.execute('UPDATE submissions SET created_at = ? WHERE id = ?', ('2026-10-10T16:00:00+00:00', after['submission']['submission_id']))
    page = store.list_submission_projects(TEACHER, from_date='2026-10-10', to_date='2026-10-10', limit=20)
    assert set(_ids(page)) == {start['project']['project_id'], end['project']['project_id']}
    assert _ids(store.list_submission_projects(TEACHER, q='100%_', limit=20)) == [start['project']['project_id']]
    assert _ids(store.list_submission_projects(TEACHER, q=start['project']['project_id'], limit=20)) == [start['project']['project_id']]


def test_pagination_uses_stable_snapshot_for_projects_and_versions(store: SubmissionStore):
    projects = [_submission(store, STUDENT_A, f'project {number}') for number in range(3)]
    first_page = store.list_submission_projects(TEACHER, limit=1)
    assert first_page['next_cursor']
    inserted = _submission(store, STUDENT_A, 'inserted after page one')
    seen = _ids(first_page)
    cursor = first_page['next_cursor']
    while cursor:
        page = store.list_submission_projects(TEACHER, limit=1, cursor=cursor)
        seen.extend(_ids(page))
        cursor = page['next_cursor']
    assert set(seen) == {item['project']['project_id'] for item in projects}
    assert len(seen) == len(set(seen)) and inserted['project']['project_id'] not in seen

    project_id = projects[0]['project']['project_id']
    old_ids = [projects[0]['submission']['submission_id']]
    old_ids.append(store.submit(project_id, STUDENT_A, 'minor', {'case.py': b'one'}, [])['submission_id'])
    old_ids.append(store.submit(project_id, STUDENT_A, 'major', {'case.py': b'two'}, [])['submission_id'])
    version_page = store.list_project_versions(TEACHER, project_id, limit=1)
    store.submit(project_id, STUDENT_A, 'minor', {'case.py': b'new after cursor'}, [])
    version_seen = [item['submission_id'] for item in version_page['items']]
    cursor = version_page['next_cursor']
    while cursor:
        page = store.list_project_versions(TEACHER, project_id, limit=1, cursor=cursor)
        version_seen.extend(item['submission_id'] for item in page['items'])
        cursor = page['next_cursor']
    assert set(version_seen) == set(old_ids)
    assert len(version_seen) == len(set(version_seen))


def test_kind_internal_records_and_bad_browser_parameters(store: SubmissionStore):
    project = _submission(store, STUDENT_A, 'visible')
    minor = store.submit(project['project']['project_id'], STUDENT_A, 'minor', {'case.py': b'minor'}, [])
    page = store.list_submission_projects(TEACHER, kind='minor', limit=20)
    assert page['items'][0]['version_count'] == 1
    assert page['items'][0]['preview']['submission_id'] == minor['submission_id']
    with store._connection() as connection:
        connection.execute('CREATE TABLE web_asset_validation_snapshots(project_id TEXT, submission_id TEXT)')
        connection.execute('INSERT INTO web_asset_validation_snapshots(project_id, submission_id) VALUES (?, ?)',
                           (project['project']['project_id'], minor['submission_id']))
    assert store.list_submission_projects(TEACHER, kind='minor', limit=20)['items'] == []

    for kwargs in ({'kind': 'other'}, {'limit': 0}, {'limit': True}, {'from_date': '2026-1-01'},
                   {'from_date': '2026-10-11', 'to_date': '2026-10-10'}, {'to_date': '9999-12-31'},
                   {'from_date': '0001-01-01'}, {'cursor': 'bad'}):
        with pytest.raises(KitError):
            store.list_submission_projects(TEACHER, **kwargs)
    cursor = store.list_submission_projects(TEACHER, limit=1)['next_cursor']
    if cursor:
        with pytest.raises(KitError, match='游标'):
            store.list_submission_projects(TEACHER, limit=1, cursor=cursor + 'x')
        with pytest.raises(KitError, match='游标'):
            store.list_submission_projects(TEACHER, owner='student-a', limit=1, cursor=cursor)


def test_preview_prefers_major_and_cursor_cannot_cross_identity_or_query(store: SubmissionStore):
    first = _submission(store, STUDENT_A, 'first')
    _submission(store, STUDENT_A, 'second')
    minor = store.submit(first['project']['project_id'], STUDENT_A, 'minor', {'case.py': b'changed'}, [])
    item = next(item for item in store.list_submission_projects(TEACHER)['items']
                if item['project']['project_id'] == first['project']['project_id'])
    assert item['version_count'] == 2
    assert item['preview']['submission_id'] == first['submission']['submission_id']
    assert item['latest_at'] == minor['created_at']
    assert store.list_submission_projects(Principal(STUDENT_A.uid, 'reused-account', 'student'))['items'] == []
    page = store.list_submission_projects(TEACHER, limit=1)
    cursor = page['next_cursor']
    assert cursor
    for options in ({'cursor': cursor + 'x'}, {'cursor': cursor, 'q': 'first'},
                    {'cursor': cursor, 'owner': 'student-a'}):
        with pytest.raises(KitError, match='游标'):
            store.list_submission_projects(TEACHER, limit=1, **options)
    with pytest.raises(KitError, match='游标'):
        store.list_submission_projects(STUDENT_A, limit=1, cursor=cursor)
    with pytest.raises(KitError, match='游标'):
        store.list_project_versions(TEACHER, first['project']['project_id'], limit=1, cursor=cursor)


class TestSubmissionBrowserHttp(AsyncHTTPTestCase):
    token = 'a' * 48

    def get_app(self):
        self.root = Path(tempfile.mkdtemp(prefix='.rk-submission-browser-', dir=Path.cwd()))
        os.chmod(self.root, 0o700)
        account = pwd.getpwuid(os.getuid()).pw_name
        self.principal = Principal(os.getuid(), account, 'teacher' if account == 'ly' else 'student')
        store = SubmissionStore(self.root / 'store')
        self.workspace = Workspace(self.principal, self.root / 'workspace', LocalStoreClient(store, self.principal))
        return application(self.workspace, self.token, demo=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.root)

    def _fetch(self, route: str, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers['Authorization'] = 'Bearer ' + self.token
        return self.fetch('/api/' + route, headers=headers, **kwargs)

    def test_new_browser_routes_and_query_schema(self):
        created = self._fetch('projects', method='POST', body=json.dumps({'name': 'browse HTTP'}),
                              headers={'Content-Type': 'application/json'})
        project = json.loads(created.body)
        submitted = self._fetch('projects/' + project['project_id'] + '/submit', method='POST',
                                body=b'{"kind":"major"}', headers={'Content-Type': 'application/json'})
        assert submitted.code == 200
        page = self._fetch('submission-projects?q=browse&limit=1')
        value = json.loads(page.body)
        assert page.code == 200 and value['items'][0]['project']['owner_username'] == self.principal.username
        history = self._fetch('submission-projects/' + project['project_id'] + '/versions?kind=major')
        assert history.code == 200 and json.loads(history.body)['items'][0]['submission_id'] == json.loads(submitted.body)['submission_id']
        assert self._fetch('submission-projects?limit=01').code == 400
        assert self._fetch('submission-projects?unexpected=x').code == 400
