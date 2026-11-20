from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_review_flow import ReviewFlow
from researchkit.web_store import SubmissionStore


TEACHER = Principal(1001, 'ly', 'teacher')
STUDENT = Principal(1002, 'test', 'student')


def _files(*, plugin_body: bytes = b'return {"value": inputs["value"]}', plugin_version: str = '1.0.0') -> dict[str, bytes]:
    return {
        'project.yaml': b'''schema: researchkit.project/v1
mode: python
entry: case.py
name: source-in-file
plugins:
  scale:
    path: algorithms/scale
    parameters: {}
parameters: {}
data: {}
requires: {}
''',
        'case.py': b'def run(context):\n    return context.call("scale", inputs={"value": 2})\n',
        'algorithms/scale/plugin.yaml': f'''schema: hydro.plugin/v1
name: example.scale
version: {plugin_version}
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs:
  value: scalar@1.0.0
parameters: {{}}
'''.encode(),
        'algorithms/scale/plugin.py': b'def run(inputs, parameters, context):\n    ' + plugin_body + b'\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
    }


def _subject(tmp_path: Path, *, name: str = 'rainfall class') -> tuple[SubmissionStore, ReviewFlow, dict[str, object]]:
    store = SubmissionStore(tmp_path / 'store')
    project = store.create_project(STUDENT, name)
    submission = store.submit(project['project_id'], STUDENT, 'minor', _files(), [])
    return store, ReviewFlow(store, tmp_path / 'library'), submission


def test_release_is_teacher_only_and_reports_manual_review_without_web_execution(tmp_path: Path) -> None:
    _store, flow, submission = _subject(tmp_path)
    with pytest.raises(KitError, match='教师'):
        flow.release(STUDENT, submission['id'])
    released = flow.release(TEACHER, submission['id'])
    assert released['publications'] == []
    assert released['review_mode'] == 'manual'
    published = flow.publish(
        TEACHER, submission['id'], 'algorithm', ['scale'], submission['content_sha256'],
        tags=['测试'], description='测试说明',
    )
    assert published['published'][0]['ref'] == 'example.scale@1.0.0'
    with pytest.raises(KitError, match='内容已变化'):
        flow.publish(TEACHER, submission['id'], 'algorithm', ['scale'], '0' * 64, tags=['测试'], description='测试说明')


def test_direct_case_and_algorithm_publication_is_idempotent(tmp_path: Path) -> None:
    store, flow, submission = _subject(tmp_path)
    case = flow.publish(TEACHER, submission['id'], 'case', None, submission['content_sha256'], tags=['测试'], description='测试说明')
    assert len(case['published']) == 1
    case_row = case['published'][0]
    assert case_row['kind'] == 'template'
    assert case_row['ref'].startswith('rainfall-class-') and case_row['ref'].endswith('@0.1.0')
    algorithms = flow.publish(TEACHER, submission['id'], 'algorithm', ['scale'], submission['content_sha256'], tags=['测试'], description='测试说明')
    assert algorithms['published'][0]['ref'] == 'example.scale@1.0.0'
    assert algorithms['published'][0]['status'] == 'published'
    again = flow.publish(TEACHER, submission['id'], 'algorithm', ['scale'], submission['content_sha256'], tags=['测试'], description='测试说明')
    assert again['reused'] is True
    assert again['published'][0]['status'] == 'reused'
    overview = flow.release(TEACHER, submission['id'])
    assert {(row['kind'], row['ref']) for row in overview['publications']} == {
        ('template', case_row['ref']), ('algorithm', 'example.scale@1.0.0')}


def test_same_algorithm_ref_with_different_content_fails_without_overwrite(tmp_path: Path) -> None:
    store, flow, first = _subject(tmp_path)
    assert flow.publish(TEACHER, first['id'], 'algorithm', ['scale'], first['content_sha256'], tags=['测试'], description='测试说明')['published']
    project_id = first['project']['project_id']
    changed = _files(plugin_body=b'return {"value": inputs["value"] * 2}')
    second = store.submit(project_id, STUDENT, 'minor', changed, [])
    result = flow.publish(TEACHER, second['id'], 'algorithm', ['scale'], second['content_sha256'], tags=['测试'], description='测试说明')
    assert result['published'] == []
    assert result['failures'] and '修改版本' in result['failures'][0]['error']


def test_only_selected_aliases_are_published_and_bad_alias_is_rejected(tmp_path: Path) -> None:
    store, flow, submission = _subject(tmp_path)
    files = _files()
    files['project.yaml'] = files['project.yaml'].replace(
        b'parameters: {}\nparameters: {}',
        b'parameters: {}\n  offset:\n    path: algorithms/offset\n    parameters: {}\nparameters: {}',
    )
    files['algorithms/offset/plugin.yaml'] = files['algorithms/scale/plugin.yaml'].replace(b'example.scale', b'example.offset')
    files['algorithms/offset/plugin.py'] = b'def run(inputs, parameters, context):\n    return {"value": inputs["value"] + 1}\n'
    project_id = submission['project']['project_id']
    submission = store.submit(project_id, STUDENT, 'minor', files, [])
    with pytest.raises(KitError, match='不存在'):
        flow.publish(TEACHER, submission['id'], 'algorithm', ['none'], submission['content_sha256'], tags=['测试'], description='测试说明')
    result = flow.publish(TEACHER, submission['id'], 'algorithm', ['offset'], submission['content_sha256'], tags=['测试'], description='测试说明')
    assert [row['alias'] for row in result['published']] == ['offset']


def test_manual_review_ignores_retired_execution_history_and_internal_submissions_are_not_reviewable(tmp_path: Path) -> None:
    store, flow, submission = _subject(tmp_path)
    # Historical Jobs rows no longer gate human review or publication.
    release = flow.release(TEACHER, submission['id'])
    assert release['review_mode'] == 'manual'
    assert flow.publish(
        TEACHER, submission['id'], 'case', None, submission['content_sha256'],
        tags=['测试'], description='测试说明',
    )['published']
    # Teacher task baselines are internal distribution records, never review
    # sources, even though they are represented by immutable submissions.
    baseline_project = store.create_project(TEACHER, 'internal task baseline')
    baseline = store.submit(
        baseline_project['project_id'], TEACHER, 'major', _files(), [],
        assignment_request_id=str(uuid.uuid4()),
        assignment_fingerprint=hashlib.sha256(b'internal baseline').hexdigest(),
    )
    with pytest.raises(KitError, match='任务内部基线'):
        flow.release(TEACHER, baseline['id'])
