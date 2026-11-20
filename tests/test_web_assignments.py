from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_assignments import AssignmentService
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


class AssignmentStore:
    """Small broker double: it records the two server-only assignment hooks."""

    def __init__(self):
        self.projects: dict[str, dict[str, object]] = {}
        self.baselines: dict[tuple[str, str], dict[str, object]] = {}
        self.project_creates = 0
        self.submits = 0
        self.dispatches = 0
        self.resource_tasks: dict[str, dict[str, object]] = {}
        self.fail_dispatch_once = False
        self.catalog = {
            'rainfall-v1': [{'dataset_id': 'rainfall-v1', 'path': 'rainfall.csv', 'size': 12,
                             'sha256': hashlib.sha256(b'day,rainfall\n').hexdigest()}],
        }
        self.template = {
            'project.yaml': b'''schema: researchkit.project/v1
name: hello
mode: python
entry: case.py
plugins:
  scale:
    path: .researchkit/plugins/scale
    parameters:
      factor: 3
parameters:
  value: 2
data: {}
requires:
  python: '3.11'
  modules: []
  minimums: {}
origin:
  kind: test
''',
            'case.py': b'def run(context):\n    return context.call("scale", inputs={"value": 2})\n',
            '.researchkit/plugins/scale/plugin.yaml': b'''schema: hydro.plugin/v1
name: example.scale
version: 1.0.0
entry: plugin.py
inputs:
  value: scalar@1.0.0
outputs:
  value: scalar@1.0.0
parameters:
  factor:
    type: number
    required: true
''',
            '.researchkit/plugins/scale/plugin.py': b'def run(inputs, parameters, context):\n    return {"value": inputs["value"] * parameters["factor"]}\n',
            '.researchkit/contracts/scalar/1.0.0.yaml': b'''schema: hydro.contract/v1
id: scalar@1.0.0
type: number
description: scalar
''',
        }

    def request(self, method: str, **params: object):
        if method == 'get_shared_template':
            assert params['ref'] == 'practice@1.0.0'
            return {'ref': params['ref'], 'files': dict(self.template)}
        if method == 'create_project':
            self.project_creates += 1
            project_id = str(uuid.uuid4())
            record = {'project_id': project_id, 'id': project_id, 'name': params['name'],
                      'owner_uid': os.getuid(), 'owner_username': 'ly', 'source': 'self-created'}
            self.projects[project_id] = record
            return dict(record)
        if method == 'get_project':
            return dict(self.projects[str(params['project_id'])])
        if method == 'record_project_usage':
            return {}
        if method == 'get_dataset_catalog':
            return [{'dataset_id': dataset_id, 'files': files} for dataset_id, files in self.catalog.items()]
        if method == 'get_component_catalog':
            return {'components': [], 'contracts': []}
        if method == 'validate_assignment_targets':
            targets = params['target_usernames']
            references = params['data_references']
            task_payload = params['task_payload']
            assert isinstance(targets, list) and isinstance(references, list)
            assert task_payload['composition'] if 'composition' in task_payload else True
            if 'missing' in targets:
                raise KitError('目标账号不存在')
            known = {item['dataset_id'] for item in references}
            if not known <= set(self.catalog):
                raise KitError('目标账号无权使用选择的数据集')
            return {'targets': [{'username': value, 'uid': index + 1000} for index, value in enumerate(targets)],
                    'data_references': references}
        if method == 'get_assignment_baseline':
            current = self.baselines.get(('ly', str(params['request_id'])))
            if current is None:
                return None
            if current['fingerprint'] != params['fingerprint']:
                raise KitError('任务请求内容不同')
            result = current['result']
            assert isinstance(result, dict)
            return {'submission_id': result['submission_id'], 'content_sha256': result['content_sha256'],
                    'manifest': {'data_references': {'references': current['data_references']}}}
        if method == 'submit_assignment_baseline':
            key = ('ly', str(params['request_id']))
            current = self.baselines.get(key)
            if current is not None:
                if current['fingerprint'] != params['fingerprint']:
                    raise KitError('任务请求内容不同')
                return dict(current['result'])
            self.submits += 1
            files = params['files']
            assert isinstance(files, dict)
            submission_id = str(uuid.uuid4())
            result = {'submission_id': submission_id,
                      'content_sha256': hashlib.sha256(b''.join(files[path] for path in sorted(files))).hexdigest(),
                      'files': dict(files)}
            self.baselines[key] = {'fingerprint': params['fingerprint'], 'result': result,
                                   'data_references': params['data_references']}
            return dict(result)
        if method == 'create_assignment_distributions':
            task_payload = params['task_payload']
            assert task_payload['name'] == '降雨练习' or task_payload['name'] == 'hello direct assignment'
            self.dispatches += 1
            if self.fail_dispatch_once:
                self.fail_dispatch_once = False
                raise KitError('模拟分发短暂失败')
            return {'task_id': 'task-' + str(params['request_id']), 'kind': 'formal', 'materials': [], 'distributions': [
                {'distribution_id': str(uuid.uuid4()), 'target_username': target,
                 'source_submission_id': params['submission_id'], 'status': 'pending'}
                for target in params['target_usernames']
            ]}
        if method == 'create_resource_task':
            payload = params['task_payload']
            assert isinstance(payload, dict) and payload['template_ref'] is None
            request_id = str(payload['request_id'])
            existing = self.resource_tasks.get(request_id)
            if existing is not None:
                return dict(existing)
            self.dispatches += 1
            result = {'task_id': 'resource-' + request_id, 'kind': 'formal', 'delivery_mode': 'resources',
                      'baseline_submission_id': None, 'materials': [], 'recipients': [], 'distributions': []}
            self.resource_tasks[request_id] = result
            return dict(result)
        raise AssertionError(method)


@pytest.fixture
def assignment():
    root = Path(tempfile.mkdtemp(prefix='.rk-assignment-test-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    store = AssignmentStore()
    workspace = Workspace(Principal(os.getuid(), 'ly', 'teacher'), root / 'workspace', store)
    try:
        yield AssignmentService(workspace), store, workspace
    finally:
        shutil.rmtree(root)


def _request(*, request_id: str | None = None, targets: list[str] | None = None,
             datasets: list[str] | None = None, algorithm_refs: list[str] | None = None) -> dict[str, object]:
    return {'request_id': request_id or str(uuid.uuid4()), 'name': '降雨练习', 'template_ref': 'practice@1.0.0',
            'dataset_ids': datasets if datasets is not None else ['rainfall-v1'],
            'targets': targets if targets is not None else ['ly', 'test'], 'instructions': '读取降雨数据并提交结果。',
            'algorithm_refs': algorithm_refs if algorithm_refs is not None else []}


def test_direct_assignment_is_private_idempotent_and_self_claimable(assignment):
    service, store, workspace = assignment
    request = _request(algorithm_refs=['example.extra@1.0.0'])
    preview = service.preview(request)
    assert preview['datasets'] == []  # extra datasets are task materials, not baseline references
    assert preview['targets'] == ['ly', 'test']
    assert [item['username'] for item in preview['target_details']] == ['ly', 'test']
    assert preview['composition'] is None
    assert preview['algorithm_bindings']['scale']['parameters']['factor'] == 3
    assert preview['algorithm_bindings']['scale']['parameter_schema']['factor']['type'] == 'number'
    result = service.create(request)
    assert store.submits == 1
    assert store.project_creates == 1
    assert [item['target_username'] for item in result['distributions']] == ['ly', 'test']
    assert result['task_id'].startswith('task-')
    assert result['kind'] == 'formal'
    snapshot = next(iter(store.baselines.values()))['result']['files']
    assert 'TASK.md' not in snapshot
    assert 'DATA-REFERENCES.json' not in snapshot
    assert b'plugins:\n  scale:' in snapshot['project.yaml']
    assert b'factor: 3' in snapshot['project.yaml']
    assert not list((workspace.root / '.researchkit-assignment-staging').glob('assignment-*'))
    assert service.create(request) == result
    assert store.submits == 1


def test_reused_request_id_with_different_content_is_rejected(assignment):
    service, _store, _workspace = assignment
    request = _request()
    service.create(request)
    changed = dict(request)
    changed['instructions'] = 'different'
    with pytest.raises(KitError, match='内容不同'):
        service.create(changed)


def test_invalid_target_or_dataset_fails_before_staging_submission(assignment):
    service, store, workspace = assignment
    with pytest.raises(KitError, match='目标账号不存在'):
        service.create(_request(targets=['missing']))
    with pytest.raises(KitError, match='数据集不存在'):
        service.create(_request(datasets=['not-registered']))
    assert store.submits == 0
    assert not list((workspace.root / '.researchkit-assignment-staging').glob('assignment-*'))


def test_retry_after_dispatch_failure_reuses_broker_baseline_and_cleans_staging(assignment):
    service, store, workspace = assignment
    store.fail_dispatch_once = True
    request = _request()
    with pytest.raises(KitError, match='短暂失败'):
        service.create(request)
    assert store.submits == 1
    result = service.create(request)
    assert result['baseline_submission_id']
    assert store.submits == 1
    assert store.dispatches == 2
    assert store.project_creates == 1
    assert not list((workspace.root / '.researchkit-assignment-staging').glob('assignment-*'))


def test_student_cannot_prepare_assignment():
    parent = Path(tempfile.mkdtemp(prefix='.rk-assignment-student-', dir=Path.cwd()))
    os.chmod(parent, 0o700)
    try:
        service = AssignmentService(Workspace(Principal(os.getuid(), 'test', 'student'), parent / 'workspace', AssignmentStore()))
        with pytest.raises(KitError, match='只有教师'):
            service.create(_request())
    finally:
        shutil.rmtree(parent)


def test_assignment_rejects_any_case_rewriting_composition(assignment):
    service, store, _workspace = assignment
    request = _request()
    request['composition'] = {'steps': []}
    with pytest.raises(KitError, match='不支持编排'):
        service.create(request)
    assert store.submits == 0


def test_bundled_hello_assignment_self_claim_and_local_run():
    """Exercise the real bundled template through the production-shaped client."""
    root = Path(tempfile.mkdtemp(prefix='.rk-assignment-real-', dir=Path.cwd())).resolve()
    os.chmod(root, 0o700)
    username = pwd.getpwuid(os.getuid()).pw_name
    principal = Principal(os.getuid(), username, 'teacher')
    library = Path(__file__).resolve().parents[1] / 'library'
    try:
        store = SubmissionStore(root / 'store')
        client = LocalStoreClient(store, principal, library_root=library)
        workspace = Workspace(principal, root / 'workspace', client, library_root=library)
        request = {'request_id': str(uuid.uuid4()), 'name': 'hello direct assignment',
                   'template_ref': 'hello@1.0.0', 'dataset_ids': [], 'targets': [username],
                   'instructions': '运行后确认结果。', 'algorithm_refs': []}
        preview = workspace.assignment_preview(request)
        assert preview['composition'] is None
        assert preview['algorithm_bindings']['scale']['ref'] == 'example.scale@1.0.0'
        result = workspace.create_assignment(request)
        distribution = result['distributions'][0]
        claimed = workspace.claim_distribution(distribution['distribution_id'])
        run = subprocess.run([str(Path(claimed['path']) / 'task'), 'run'], cwd=claimed['path'],
                             capture_output=True, text=True, timeout=20, check=False)
        assert run.returncode == 0, run.stderr
        assert '"result": 6' in run.stdout
    finally:
        shutil.rmtree(root)


def test_assignment_accepts_published_case_with_editable_project_plugin(assignment):
    service,store,_workspace=assignment
    import yaml
    document=yaml.safe_load(store.template['project.yaml'])
    old=document['plugins']['scale']['path']
    new='algorithms/custom_scale'
    document['plugins']['scale']['path']=new
    store.template['project.yaml']=yaml.safe_dump(document,sort_keys=False).encode()
    for path in list(store.template):
        if path.startswith(old+'/'):
            store.template[new+path[len(old):]]=store.template.pop(path)
    payload=_request(algorithm_refs=['example.extra@1.0.0'])
    assert service.preview(payload)['algorithm_bindings']['scale']['parameters']['factor']==3
    service.create(payload)
    files=next(iter(store.baselines.values()))['result']['files']
    assert b'factor: 3' in files['project.yaml']
    assert new+'/plugin.py' in files


def test_resource_only_assignment_skips_staging_and_uses_resource_delivery(assignment):
    service, store, _workspace = assignment
    request = _request(datasets=[], algorithm_refs=['example.extra@1.0.0'])
    request['template_ref'] = None
    preview = service.preview(request)
    assert preview['delivery_mode'] == 'resources'
    assert preview['template_ref'] is None
    assert preview['source_algorithms'] == [] and preview['source_data'] == []
    assert preview['algorithm_refs'] == ['example.extra@1.0.0']
    created = service.create(request)
    assert created['delivery_mode'] == 'resources'
    assert created['baseline_submission_id'] is None
    assert created['distributions'] == []
    assert store.project_creates == 0 and store.submits == 0
    assert service.create(request) == created
    with pytest.raises(KitError, match='至少需要'):
        service.preview({**request, 'request_id': str(uuid.uuid4()), 'algorithm_refs': [], 'dataset_ids': []})


def test_local_retry_cache_stays_bounded_without_changing_central_baselines(assignment,monkeypatch):
    service,store,_workspace=assignment
    import researchkit.web_assignments as module
    records={str(uuid.uuid4()):{'fingerprint':'a'*64,'result':{'request':'x'*120}} for _ in range(20)}
    newest=next(reversed(records))
    monkeypatch.setattr(module,'MAX_EDIT_BYTES',1024)
    service._save_state(records)
    recovered=service._load_state()
    assert newest in recovered and len(recovered)<20
    assert service._state_path.stat().st_size<=1024
    assert store.baselines=={}  # trimming this cache never purges central history
