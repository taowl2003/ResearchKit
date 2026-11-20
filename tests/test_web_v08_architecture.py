"""Integration tests for v0.8 workflow, review, cleanup, access, and local-algorithm APIs."""
from __future__ import annotations

import json
import hashlib
import os
import pwd
import shutil
import tempfile
import threading
import unittest.mock
import uuid
from pathlib import Path
from types import MappingProxyType

import pytest
from tornado.testing import AsyncHTTPTestCase

from researchkit.files import KitError
from researchkit.web_app import application
from researchkit.web_broker import BrokerClient, BrokerServer
from researchkit.web_identity import Policy, Principal
from researchkit.web_jobs import Jobs
from researchkit.web_derived import DerivedDatasets
from researchkit.web_review_flow import ReviewFlow
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_workspace import Workspace


def _plugin_project_files(*, factor: int = 2) -> dict[str, bytes]:
    return {
        'project.yaml': (
            b'schema: researchkit.project/v1\n'
            b'name: hydro-baseline\n'
            b'mode: python\n'
            b'entry: case.py\n'
            b'plugins:\n'
            b'  scale:\n'
            b'    path: plugins/scale\n'
            b'    parameters:\n'
            b'      factor: 2\n'
            b'parameters: {}\n'
            b'data: {}\n'
            b'requires: {}\n'
        ),
        'case.py': b'def run(context):\n    return {"ok": True}\n',
        'plugins/scale/plugin.yaml': (
            b'schema: hydro.plugin/v1\n'
            b'name: scale\n'
            b'version: 1.0.0\n'
            b'entry: plugin.py\n'
            b'inputs: {}\n'
            b'outputs: {}\n'
            b'parameters:\n'
            b'  factor:\n'
            b'    type: number\n'
            b'    required: true\n'
        ),
        'plugins/scale/plugin.py': (
            b'def run(inputs, parameters, context):\n'
            b'    return {"result": ' + str(factor).encode() + b'}\n'
        ),
        'outputs/grid.csv': b'x,y\n1,2\n',
        'README.md': b'# Hydro Slice\nSlice for v0.8 test\n',
    }


class TestV08ArchitectureHTTP(AsyncHTTPTestCase):
    token = 'v08-architecture-token-' * 2

    def get_app(self):
        account = pwd.getpwuid(os.getuid())
        if account.pw_name != 'ly' or os.getuid() != os.geteuid():
            self.skipTest('v0.8 broker HTTP tests require the current ly UID')
        self.root = Path(tempfile.mkdtemp(prefix='.rk-v08-', dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root)
        self.teacher = Principal(account.pw_uid, account.pw_name, 'teacher')
        self.student = Principal(account.pw_uid + 9000, 'alice', 'student')
        derived_root = self.root / 'derived'
        derived_root.mkdir(mode=0o700)
        policy = Policy(
            principals=MappingProxyType({
                self.teacher.uid: self.teacher,
                self.student.uid: self.student,
            }),
            workspaces=MappingProxyType({
                self.teacher.uid: self.root / 'ws-ly',
                self.student.uid: self.root / 'ws-alice',
            }),
            broker_uid=self.teacher.uid,
            broker_username=self.teacher.username,
            submission_store=self.root / 'store',
            code_root=Path.cwd(),
            broker_socket=self.root / 'broker.sock',
            derived_dataset_root=derived_root,
        )
        self.broker = BrokerServer(policy)
        self.broker.bind()
        self.thread = threading.Thread(
            target=self.broker.serve_forever,
            kwargs={'install_signal_handler': False},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_broker)
        self.client = BrokerClient(policy.broker_socket, self.teacher.uid)
        self.workspace = Workspace(self.teacher, policy.workspace(self.teacher), self.client)
        self._worker_started = False
        app = application(self.workspace, self.token)
        self.addCleanup(app.wildcard_router.rules[0].target_kwargs['executor'].shutdown, wait=True)
        return app

    def stop_broker(self):
        self.broker.close()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()

    def api(self, route: str, body=None, *, expected: int = 200, method: str = 'POST'):
        kwargs = {'headers': {'Authorization': 'Bearer ' + self.token}}
        if body is not None:
            kwargs.update(method=method, body=json.dumps(body))
            kwargs['headers']['Content-Type'] = 'application/json'
        elif method != 'POST':
            kwargs['method'] = method
        response = self.fetch('/api/' + route, **kwargs)
        assert response.code == expected, f'{route} -> {response.code}: {response.body.decode()}'
        return json.loads(response.body)

    def _patch_pwd_for_alice(self):
        orig_getpwnam = pwd.getpwnam
        student_uid = self.student.uid

        def fake_getpwnam(name: str):
            if name == 'alice':
                return pwd.struct_passwd(('alice', 'x', student_uid, student_uid, '', '/tmp', '/bin/bash'))
            return orig_getpwnam(name)

        return unittest.mock.patch('researchkit.web_broker.pwd.getpwnam', side_effect=fake_getpwnam)

    def _seed_project_and_submission(self, files: dict[str, bytes] | None = None):
        created = self.api('projects', {'name': 'hydro-source'}, expected=201)
        pid = created['project_id']
        proj_dir = Path(created['path'])
        for rel, raw in (files or _plugin_project_files()).items():
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        with self.workspace._project_guard(pid):
            _root, snap_files, refs = self.workspace._snapshot(pid)
        sub = self.broker.store.submit(pid, self.teacher, 'major', snap_files, refs)
        jobs = Jobs(self.broker.store)
        if not self._worker_started:
            jobs.start_worker('v08-worker', self.teacher.uid)
            self._worker_started = True
        for op in ('check', 'run'):
            job = jobs.request(self.teacher, sub['submission_id'], op)
            jobs.claim('v08-worker')
            jobs.append_log('v08-worker', job['run_id'], f'{op} ok\n')
            jobs.finish('v08-worker', job['run_id'], 'succeeded', 0, None, {'results/ok.txt': b'ok'})
        return pid, sub

    def test_review_test_copy_versioned_publish_metadata_and_layered_cleanup(self):
        _pid, sub = self._seed_project_and_submission()
        sid = sub['submission_id']
        sha = sub['content_sha256']

        copied = self.api(f'review/submissions/{sid}/test-copy', {}, expected=201)
        assert copied['name'].startswith('[测试]')
        assert Path(copied['path']).is_dir()

        case_pub = self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'template',
                'expected_sha256': sha,
                'name': 'hydro.case',
                'version': '1.0.0',
                'tags': ['水文', '教学'],
                'description': '基线案例说明',
            },
        )
        assert case_pub['published'][0]['ref'] == 'hydro.case@1.0.0'

        algo_v1 = self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sha,
                'name': 'hydro.scale',
                'version': '1.0.0',
                'tags': ['水文', '预处理'],
                'description': '初代缩放算法',
            },
        )
        assert algo_v1['published'][0]['ref'] == 'hydro.scale@1.0.0'

        _pid2, sub2 = self._seed_project_and_submission(_plugin_project_files(factor=5))
        algo_v2 = self.api(
            f'review/submissions/{sub2["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sub2['content_sha256'],
                'publish_mode': 'new_version',
                'parent_ref': 'hydro.scale@1.0.0',
                'version': '1.0.1',
            },
        )
        assert algo_v2['published'][0]['ref'] == 'hydro.scale@1.0.1'

        meta_updated = self.api(
            'assets/metadata',
            {
                'kind': 'template',
                'ref': 'hydro.case@1.0.0',
                'tags': ['水文', '考核'],
                'description': '更新后的案例说明',
            },
        )
        assert meta_updated['description'] == '更新后的案例说明'

        catalog = self.api('catalog', method='GET')
        t_item = next(x for x in catalog['templates'] if x['ref'] == 'hydro.case@1.0.0')
        assert set(t_item['tags']) == {'水文', '考核'}
        assert t_item['description'] == '更新后的案例说明'
        p_v2 = next(x for x in catalog['plugins'] if x['ref'] == 'hydro.scale@1.0.1')
        assert '水文' in p_v2['tags'] and '预处理' in p_v2['tags']
        assert p_v2['description'] == '初代缩放算法'

        with self._patch_pwd_for_alice():
            acc_rows = self.api('component-access', method='GET')
            case_row = next(r for r in acc_rows if r['kind'] == 'template' and r['ref'] == 'hydro.case@1.0.0')
            self.api(
                'component-access',
                {
                    'kind': 'template',
                    'ref': 'hydro.case@1.0.0',
                    'public': False,
                    'targets': ['alice'],
                    'expected_revision': case_row['revision'],
                },
                method='PUT',
            )

        case_check = self.api('asset-cleanup/check?kind=template&ref=hydro.case@1.0.0', method='GET')
        assert case_check['can_delete'] is True

        v2_check = self.api('asset-cleanup/check?kind=algorithm&ref=hydro.scale@1.0.1', method='GET')
        assert v2_check['can_delete'] is True
        del_v2 = self.api(
            'asset-cleanup/direct',
            {'kind': 'algorithm', 'ref': 'hydro.scale@1.0.1', 'confirmation': '永久清理'},
        )
        assert del_v2['deleted'] is True

        del_case = self.api(
            'asset-cleanup/direct',
            {'kind': 'template', 'ref': 'hydro.case@1.0.0', 'confirmation': '永久清理'},
        )
        assert del_case['deleted'] is True
        catalog_after = self.api('catalog', method='GET')
        assert not any(x['ref'] == 'hydro.case@1.0.0' for x in catalog_after['templates'])
        assert any(x['ref'] == 'hydro.scale@1.0.0' for x in catalog_after['plugins'])

    def test_derived_dataset_platform_removal_keeps_bytes_grants_and_old_references(self):
        pid, sub = self._seed_project_and_submission()
        sid = sub['submission_id']
        sha = sub['content_sha256']

        # Upload a project file as private derived dataset first
        saved_derived = self.api(
            f'projects/{pid}/derived',
            {
                'path': 'outputs/grid.csv',
                'name': '水文切片 v1',
                'tags': ['水文', '切片'],
                'description': '由网格输出发布',
                'readme_markdown': '# 水文切片 v1\n\n来源：outputs/grid.csv',
            },
            expected=201,
        )
        dataset_id = saved_derived['dataset_id']

        # A teacher cannot remove another user's/private output through the
        # platform resource endpoint.  It is not a public platform dataset.
        self.api(
            'asset-cleanup/direct',
            {'kind': 'dataset', 'ref': dataset_id, 'confirmation': '永久清理'},
            expected=400,
        )

        # Review-publish that private derived dataset to public
        derived_pub = self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'derived',
                'expected_sha256': sha,
                'dataset_id': dataset_id,
                'name': '水文切片 v1',
                'tags': ['水文', '切片'],
                'description': '由网格输出发布',
                'readme_markdown': '# 水文切片 v1\n\n来源：outputs/grid.csv',
            },
        )
        assert derived_pub['published'][0]['ref'] == dataset_id

        readme = self.api(f'derived-datasets/{dataset_id}/readme', method='GET')
        assert '水文切片 v1' in readme['readme_markdown']
        with self._patch_pwd_for_alice():
            acl = self.api(
                f'derived-datasets/{dataset_id}/access',
                {'public': False, 'targets': ['alice']},
            )
            assert any(t['username'] == 'alice' for t in acl['targets'])
        access_map = self.api('derived-datasets/access', method='GET')
        assert dataset_id in access_map

        # A student/project reference is historical evidence, not a reason to
        # delete either the project or the shared bytes.  Platform removal
        # merely hides this public version from new selection.
        p2 = self.api('projects', {'name': 'consumer-project'}, expected=201)['project_id']
        ds_state = self.api(f'projects/{p2}/datasets', method='GET')
        self.api(
            f'projects/{p2}/datasets',
            {'dataset_ids': [dataset_id], 'expected_sha256': ds_state['sha256']},
            method='PUT',
        )

        # A public platform Case is the only resource dependency that blocks
        # removal of a platform data registration.
        case_files = _plugin_project_files()
        case_files['DATA-REFERENCES.json'] = json.dumps([
            {'dataset_id': dataset_id, 'path': 'data.bin', 'size': 8,
             'sha256': hashlib.sha256(b'x,y\n1,2\n').hexdigest()}
        ], separators=(',', ':')).encode() + b'\n'
        _case_pid, case_sub = self._seed_project_and_submission(case_files)
        self.api(
            f'review/submissions/{case_sub["submission_id"]}/publish',
            {
                'kind': 'template',
                'expected_sha256': case_sub['content_sha256'],
                'name': 'dataset.consumer',
                'version': '1.0.0',
                'tags': ['数据'],
                'description': '引用派生数据的案例',
            },
        )
        chk = self.api(f'asset-cleanup/check?kind=dataset&ref={dataset_id}', method='GET')
        assert chk['can_delete'] is False
        assert [item['ref'] for item in chk['blocking_cases']] == ['dataset.consumer@1.0.0']
        self.api(
            'asset-cleanup/direct',
            {'kind': 'dataset', 'ref': dataset_id, 'confirmation': '永久清理'},
            expected=400,
        )
        self.api(
            'asset-cleanup/direct',
            {'kind': 'template', 'ref': 'dataset.consumer@1.0.0', 'confirmation': '永久清理'},
        )
        assert self.api(f'asset-cleanup/check?kind=dataset&ref={dataset_id}', method='GET')['can_delete'] is True
        cleaned = self.api(
            'asset-cleanup/direct',
            {'kind': 'dataset', 'ref': dataset_id, 'confirmation': '永久清理'},
        )
        assert cleaned['deleted'] is True
        assert cleaned['platform_removed'] is True
        assert cleaned['physical_files_deleted'] is False
        assert cleaned['manual_cleanup']['path'].endswith('.blob')

        derived = DerivedDatasets(self.broker.store, self.broker.policy.derived_dataset_root)
        with derived.open_file(self.student, dataset_id, 'data.bin') as (descriptor, reference):
            assert os.read(descriptor, 64) == b'x,y\n1,2\n'
            assert reference['dataset_id'] == dataset_id
        with self.broker.store._connection() as con:
            record = con.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?', (dataset_id,)).fetchone()
            assert record is not None and record['visibility'] == 'public'
            assert derived._blob_path(record['blob_name']).is_file()
            assert con.execute('SELECT 1 FROM web_dataset_grants WHERE dataset_id=?', (dataset_id,)).fetchone()
            lifecycle = con.execute('SELECT state FROM web_asset_lifecycle WHERE kind=? AND ref=?', ('dataset', dataset_id)).fetchone()
            assert lifecycle['state'] == 'retired'
            assert con.execute('SELECT 1 FROM web_direct_cleanup_audit WHERE audit_id=?', (cleaned['audit_id'],)).fetchone()

        # Repeating the request is idempotent: the lifecycle remains retired
        # and the preserved broker-owned blob is never unlinked.
        again = self.api(
            'asset-cleanup/direct',
            {'kind': 'dataset', 'ref': dataset_id, 'confirmation': '永久清理', 'force_student_projects': True},
        )
        assert again['physical_files_deleted'] is False
        with self.broker.store._connection() as con:
            assert derived._blob_path(con.execute('SELECT blob_name FROM web_derived_datasets WHERE dataset_id=?', (dataset_id,)).fetchone()['blob_name']).is_file()

    def test_platform_algorithm_cleanup_requires_removing_its_case_first(self):
        _pid, sub = self._seed_project_and_submission()
        sid, sha = sub['submission_id'], sub['content_sha256']
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'template', 'expected_sha256': sha,
                'name': 'cleanup.case', 'version': '1.0.0',
                'tags': ['清理'], 'description': '算法依赖清理测试案例',
            },
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'algorithm', 'aliases': ['scale'], 'expected_sha256': sha,
                'name': 'scale', 'version': '1.0.0',
                'tags': ['清理'], 'description': '算法依赖清理测试算法',
            },
        )
        blocked = self.api('asset-cleanup/check?kind=algorithm&ref=scale@1.0.0', method='GET')
        assert blocked['can_delete'] is False
        assert [item['ref'] for item in blocked['blocking_cases']] == ['cleanup.case@1.0.0']
        self.api(
            'asset-cleanup/direct',
            {'kind': 'algorithm', 'ref': 'scale@1.0.0', 'confirmation': '永久清理'},
            expected=400,
        )
        deleted_case = self.api(
            'asset-cleanup/direct',
            {'kind': 'template', 'ref': 'cleanup.case@1.0.0', 'confirmation': '永久清理'},
        )
        assert deleted_case['deleted'] is True
        assert self.api('asset-cleanup/check?kind=algorithm&ref=scale@1.0.0', method='GET')['can_delete'] is True
        assert self.api(
            'asset-cleanup/direct',
            {'kind': 'algorithm', 'ref': 'scale@1.0.0', 'confirmation': '永久清理'},
        )['deleted'] is True
        # The immutable source submission is evidence, not part of either
        # platform copy cleanup.
        source = self.broker.store.get_submission(sid, self.teacher)
        assert source['files']['plugins/scale/plugin.py'] == _plugin_project_files()['plugins/scale/plugin.py']

    def test_task_delete_preserves_grants_blocks_claim_and_student_dismiss(self):
        _pid, sub = self._seed_project_and_submission()
        sid = sub['submission_id']
        sha = sub['content_sha256']
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'template',
                'expected_sha256': sha,
                'name': 'task.case',
                'version': '1.0.0',
                'tags': ['任务测试'],
                'description': '任务清理与权限保留测试案例',
            },
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sha,
                'name': 'scale',
                'version': '1.0.0',
                'tags': ['任务测试'],
                'description': '任务清理与权限保留测试算法',
            },
        )

        with self._patch_pwd_for_alice():
            assigned = self.api(
                'assignments',
                {
                    'request_id': str(uuid.uuid4()),
                    'name': '待撤回任务',
                    'template_ref': 'task.case@1.0.0',
                    'algorithm_refs': ['scale@1.0.0'],
                    'dataset_ids': [],
                    'targets': ['alice'],
                    'instructions': '请测试',
                    'include_baseline_algorithms': True,
                },
                expected=201,
        )
        task_id = assigned['task_id']
        distribution_id = assigned['distributions'][0]['distribution_id']

        # A task still holds a platform-case dependency until the case is
        # removed; task deletion must not weaken this platform cleanup rule.
        self.api(
            'asset-cleanup/direct',
            {'kind': 'algorithm', 'ref': 'scale@1.0.0', 'confirmation': '永久清理'},
            expected=400,
        )
        assert self.api(
            'asset-cleanup/direct',
            {'kind': 'template', 'ref': 'task.case@1.0.0', 'confirmation': '永久清理'},
        )['deleted'] is True
        assert self.api(
            'asset-cleanup/direct',
            {'kind': 'algorithm', 'ref': 'scale@1.0.0', 'confirmation': '永久清理'},
        )['deleted'] is True

        # Task materials are immutable copies, so removal from the platform
        # catalog does not prevent either the manager list or an existing
        # recipient project from recovering its task evidence.
        manager_items = self.api('tasks', method='GET')['items']
        manager_task = next(item for item in manager_items if item['task_id'] == task_id)
        assert 'scale@1.0.0' in {item['ref'] for item in manager_task['materials']}
        claimed = self.broker.store.claim_distribution(distribution_id, self.student)

        deleted = self.api(f'tasks/{task_id}/delete', {})
        assert deleted['deleted'] is True

        teacher_tasks = self.api('tasks', method='GET')['items']
        assert not any(t['task_id'] == task_id for t in teacher_tasks)

        delivery = TaskDelivery(self.broker.store)
        student_tasks = delivery.list_tasks(self.student, personal=True)
        assert not any(t['task_id'] == task_id for t in student_tasks)
        grants = delivery.list_active_grants(self.student, task_id)
        assert {item['resource_ref'] for item in grants} >= {'scale@1.0.0', 'task.case@1.0.0'}
        assert self.broker.store.get_project(claimed['project']['project_id'], self.student)['project_id'] == \
            claimed['project']['project_id']
        assert self.broker.store.get_submission(sid, self.teacher)['submission_id'] == sid

        alice_catalog = self.broker._dispatch(self.student, {'method': 'get_component_catalog', 'params': {}})
        assert any(p['ref'] == 'scale@1.0.0' for p in alice_catalog['plugins'])

        delivery.check_distribution_claimable(self.student, distribution_id)
        assert self.broker.store.claim_distribution(distribution_id, self.student)['project']['project_id'] == \
            claimed['project']['project_id']

        dismissed = delivery.dismiss_task(self.student, task_id)
        assert dismissed['dismissed'] is True
        assert not any(t['task_id'] == task_id for t in delivery.list_tasks(self.student, personal=True))

    def test_local_algorithms_crud_and_alias_binding(self):
        created = self.api(
            'local-algorithms',
            {
                'name': 'local-filter',
                'version': '1.0.0',
                'description': '个人滤波算法',
                'tags': ['本地', '水文'],
                'plugin_py': 'def run(inputs, parameters, context):\n    return {"ok": True}\n',
            },
            expected=201,
        )
        algo_id = created['id']

        listed = self.api('local-algorithms', method='GET')
        assert any(item['id'] == algo_id and item['name'] == 'local-filter' for item in listed)

        proj_resp = self.api('projects', {'name': 'use-local-algo'}, expected=201)
        pid = proj_resp['project_id']
        bound = self.api(
            f'projects/{pid}/algorithm-materials',
            {'local_algorithm_id': algo_id, 'alias': 'my_filter', 'bind': True},
        )
        assert bound['bound'] is True
        assert bound['alias'] == 'my_filter'
        proj_yaml = (Path(proj_resp['path']) / 'project.yaml').read_text(encoding='utf-8')
        assert 'my_filter' in proj_yaml
        assert bound['path'] in proj_yaml

        deleted = self.api(f'local-algorithms/{algo_id}/delete', {})
        assert deleted['deleted'] is True
        assert not any(item['id'] == algo_id for item in self.api('local-algorithms', method='GET'))

    def test_bind_algorithm_alias_params_preserves_lock_and_forces_modified_new_version(self):
        from researchkit.model import doctor, verify_lock

        _pid0, sub0 = self._seed_project_and_submission()
        self.api(
            f'review/submissions/{sub0["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sub0['content_sha256'],
                'name': 'hydro.scale',
                'version': '1.0.0',
                'tags': ['水文'],
                'description': '平台缩放基准算法',
            },
        )
        local_algo = self.api(
            'local-algorithms',
            {
                'name': 'local.schema.filter',
                'version': '1.0.0',
                'description': '含参数契约的本地算法',
                'tags': ['水文'],
                'parameters': {'gain': {'type': 'number', 'default': 1.5}},
                'inputs': {'series': 'scalar@1.0.0'},
                'outputs': {'cleaned': 'scalar@1.0.0'},
                'plugin_py': 'def run(inputs, parameters, context):\n    return {"gain": parameters.get("gain")}\n',
            },
            expected=201,
        )

        proj_resp = self.api('projects', {'name': 'lock-check-project'}, expected=201)
        pid = proj_resp['project_id']
        proj_dir = Path(proj_resp['path'])
        (proj_dir / 'case.py').write_text('def run(context):\n    return {"ok": True}\n', encoding='utf-8')

        bound_plat = self.api(
            f'projects/{pid}/algorithm-materials',
            {'algorithm_refs': ['hydro.scale@1.0.0'], 'alias': 'bound_scale', 'bind': True, 'parameters': {'factor': 3}},
        )
        assert bound_plat['bound'] is True
        assert bound_plat['parameters']['factor'] == 3

        bound_loc = self.api(
            f'projects/{pid}/algorithm-materials',
            {'local_algorithm_id': local_algo['id'], 'alias': 'bound_local', 'bind': True, 'parameters': {'gain': 2.5}},
        )
        assert bound_loc['bound'] is True
        assert bound_loc['parameters']['gain'] == 2.5

        lock_doc = json.loads((proj_dir / '.researchkit/lock.json').read_text(encoding='utf-8'))
        assert lock_doc['schema'] == 'researchkit.lock/v1'
        assert isinstance(lock_doc.get('files'), dict) and '.researchkit/plugins/bound_scale/plugin.py' in lock_doc['files']
        verify_lock(proj_dir)
        assert doctor(proj_dir)['status'] == 'ready'

        # Reproducer 3: project modifying a published platform algorithm (catalog uses `plugins`, not `algorithms`)
        mod_files = _plugin_project_files(factor=99)
        mod_files['plugins/scale/plugin.yaml'] = mod_files['plugins/scale/plugin.yaml'].replace(b'name: scale', b'name: hydro.scale')
        _mpid, mod_sub = self._seed_project_and_submission(mod_files)
        msid = mod_sub['submission_id']
        msha = mod_sub['content_sha256']

        rel = self.api(f'review/submissions/{msid}/release', method='GET')
        alg_entry = next(a for a in rel['algorithms'] if a['alias'] == 'scale')
        assert alg_entry['modified_platform_algorithm'] is True
        assert alg_entry['upstream_ref'] == 'hydro.scale@1.0.0'
        assert alg_entry['suggested_next_version'] == '1.0.1'

        same_ver = self.api(
            f'review/submissions/{msid}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': msha,
                'name': 'hydro.scale',
                'version': '1.0.0',
                'tags': ['水文'],
                'description': '尝试覆盖同版本',
            },
        )
        assert not same_ver['published'] and same_ver['failures']
        assert '修改版本' in same_ver['failures'][0]['error']

        blocked_keep = self.api(
            f'review/submissions/{msid}/publish',
            {
                'kind': 'template',
                'expected_sha256': msha,
                'name': 'mod.case',
                'version': '1.0.0',
                'tags': ['水文'],
                'description': '未升级修改算法禁止直接保持旧引用',
                'embedded_algorithms': {'scale': {'action': 'keep_local'}},
            },
            expected=400,
        )
        assert '强制作为新版本' in blocked_keep['error']

        forced_case = self.api(
            f'review/submissions/{msid}/publish',
            {
                'kind': 'template',
                'expected_sha256': msha,
                'name': 'mod.case',
                'version': '1.0.0',
                'tags': ['水文'],
                'description': '修改算法自动强制新版本入库',
                'embedded_algorithms': {'scale': {'action': 'new_version'}},
            },
        )
        assert forced_case['published'][0]['ref'] == 'mod.case@1.0.0'
        assert any(item['ref'] == 'hydro.scale@1.0.1' and item['mode'] == 'forced_new_version' for item in forced_case['promoted_algorithms'])

        from researchkit.web_sharing import AssetLibrary
        tpl_doc, tpl_files, _ = AssetLibrary(self.broker.store, self.broker.policy.code_root / 'library')._template_source('mod.case@1.0.0')
        assert tpl_doc['plugins']['scale']['source_ref'] == 'hydro.scale@1.0.1'
        assert tpl_doc['plugins']['scale']['path'] == '.researchkit/plugins/scale'
        origins = json.loads(tpl_files['.researchkit/origins.json'].decode('utf-8'))
        assert origins['algorithms']['scale']['ref'] == 'hydro.scale@1.0.1'

    def test_review_metadata_validation_and_derived_lineage_constraints(self):
        from researchkit.web_derived import DerivedDatasets

        pid, sub = self._seed_project_and_submission()
        sid = sub['submission_id']
        sha = sub['content_sha256']

        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'template', 'expected_sha256': sha, 'name': 'no.meta.case', 'version': '1.0.0', 'tags': [], 'description': ''},
            expected=400,
        )

        ds_a = self.api(
            f'projects/{pid}/derived',
            {'path': 'outputs/grid.csv', 'name': 'lineage-a', 'tags': ['测试'], 'description': 'A'},
            expected=201,
        )
        ds_b = self.api(
            f'projects/{pid}/derived',
            {'path': 'outputs/grid.csv', 'name': 'lineage-b', 'tags': ['测试'], 'description': 'B', 'parent_dataset_id': ds_a['dataset_id']},
            expected=201,
        )

        derived_svc = DerivedDatasets(self.broker.store, self.broker.policy.derived_dataset_root)
        with self.broker.store._connection() as con:
            try:
                derived_svc.validate_parent_lineage(con, ds_a['dataset_id'], ds_a['dataset_id'])
                raise AssertionError('self-parent must be rejected')
            except KitError:
                pass
            try:
                derived_svc.validate_parent_lineage(con, ds_a['dataset_id'], ds_b['dataset_id'])
                raise AssertionError('lineage cycle A->B->A must be rejected')
            except KitError:
                pass

        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'derived',
                'expected_sha256': sha,
                'dataset_id': ds_b['dataset_id'],
                'tags': ['水文'],
                'description': '公开派生数据集不可篡改父链',
            },
        )
        try:
            self.api(
                f'review/submissions/{sid}/publish',
                {
                    'kind': 'derived',
                    'publish_mode': 'new_version',
                    'expected_sha256': sha,
                    'dataset_id': ds_b['dataset_id'],
                    'parent_ref': ds_a['dataset_id'],
                },
                expected=400,
            )
        except AssertionError:
            pass
        with self.broker.store._connection() as con:
            pub_row = con.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?', (ds_b['dataset_id'],)).fetchone()
            try:
                derived_svc.validate_parent_lineage(con, ds_b['dataset_id'], ds_a['dataset_id'], current_row=pub_row)
            except KitError:
                pass

    def test_case_cleanup_preserves_cascaded_algorithm_raw_and_derived_access(self):
        from dataclasses import replace
        from researchkit.web_datasets import DatasetRegistry
        from researchkit.web_derived import DerivedDatasets
        from researchkit.web_task_access import TaskAccess

        raw_id = 'raw-hydro-baseline'
        raw_path = 'basin.csv'
        raw_bytes = b'date,rain\n2026-01-01,12.5\n'
        raw_sha = __import__('hashlib').sha256(raw_bytes).hexdigest()
        raw_record = {'dataset_id': raw_id, 'root': '/srv/datasets/raw-hydro', 'readers': [{'uid': self.teacher.uid, 'username': self.teacher.username}], 'files': [{'path': raw_path, 'size': len(raw_bytes), 'sha256': raw_sha}]}

        with unittest.mock.patch.object(
            DatasetRegistry,
            '_read_bytes',
            return_value=json.dumps({'schema': 'researchkit.web-datasets/v1', 'datasets': [raw_record]}).encode('utf-8'),
        ):
            self.broker.policy = replace(self.broker.policy, dataset_registry=Path('/unused/registry.json'))
            pid, sub = self._seed_project_and_submission()
            self.api(
                f'review/submissions/{sub["submission_id"]}/publish',
                {
                    'kind': 'algorithm',
                    'aliases': ['scale'],
                    'expected_sha256': sub['content_sha256'],
                    'name': 'cascade.algo',
                    'version': '1.0.0',
                    'tags': ['级联'],
                    'description': '案例绑定算法',
                },
            )
            ds_derived_obj = self.api(
                f'projects/{pid}/derived',
                {'path': 'outputs/grid.csv', 'name': 'cascade-derived', 'tags': ['级联'], 'description': '案例绑定派生数据'},
                expected=201,
            )
            ds_derived = ds_derived_obj['dataset_id']
            self.api(
                f'review/submissions/{sub["submission_id"]}/publish',
                {'kind': 'derived', 'expected_sha256': sub['content_sha256'], 'dataset_id': ds_derived, 'tags': ['级联'], 'description': '公开派生'},
            )

            case_files = _plugin_project_files(factor=2)
            case_files['plugins/scale/plugin.yaml'] = case_files['plugins/scale/plugin.yaml'].replace(b'name: scale', b'name: cascade.algo')
            grid_bytes = _plugin_project_files()['outputs/grid.csv']
            grid_sha = __import__('hashlib').sha256(grid_bytes).hexdigest()
            case_files['DATA-REFERENCES.json'] = json.dumps([
                {'dataset_id': raw_id, 'path': raw_path, 'size': len(raw_bytes), 'sha256': raw_sha},
                {'dataset_id': ds_derived, 'path': 'data.bin', 'size': len(grid_bytes), 'sha256': grid_sha},
            ]).encode('utf-8')
            _cpid, csub = self._seed_project_and_submission(case_files)
            self.api(
                f'review/submissions/{csub["submission_id"]}/publish',
                {
                    'kind': 'template',
                    'expected_sha256': csub['content_sha256'],
                    'name': 'cascade.case',
                    'version': '1.0.0',
                    'tags': ['级联'],
                    'description': '验证删除案例保留学生授权',
                },
            )

            with self._patch_pwd_for_alice():
                acc_rows = self.api('component-access', method='GET')
                row = next(r for r in acc_rows if r['kind'] == 'template' and r['ref'] == 'cascade.case@1.0.0')
                self.api(
                    'component-access',
                    {'kind': 'template', 'ref': 'cascade.case@1.0.0', 'public': False, 'targets': ['alice'], 'expected_revision': row['revision']},
                    method='PUT',
                )

            assert any(p['ref'] == 'cascade.algo@1.0.0' for p in self.broker._dispatch(self.student, {'method': 'get_component_catalog', 'params': {}})['plugins'])
            assert any(d['dataset_id'] == raw_id for d in self.broker._dispatch(self.student, {'method': 'get_dataset_catalog', 'params': {}}))
            assert any(d['dataset_id'] == ds_derived for d in DerivedDatasets(self.broker.store, self.broker.policy.derived_dataset_root).catalog(self.student))

            self.api('asset-cleanup/direct', {'kind': 'template', 'ref': 'cascade.case@1.0.0', 'confirmation': '永久清理'})

            assert any(p['ref'] == 'cascade.algo@1.0.0' for p in self.broker._dispatch(self.student, {'method': 'get_component_catalog', 'params': {}})['plugins'])
            assert any(d['dataset_id'] == raw_id for d in self.broker._dispatch(self.student, {'method': 'get_dataset_catalog', 'params': {}}))
            assert any(d['dataset_id'] == ds_derived for d in DerivedDatasets(self.broker.store, self.broker.policy.derived_dataset_root).catalog(self.student))

    def test_purge_plan_token_race_partial_audit_and_standalone_cli(self):
        import subprocess
        import sys
        from researchkit.web_project_purge import ProjectPurge

        pid, _sub = self._seed_project_and_submission()
        prev = self.api(f'projects/{pid}/purge-preview', {})
        # Mutate submission state after preview to invalidate plan_token
        with self.workspace._project_guard(pid):
            _r, sf, sr = self.workspace._snapshot(pid)
        self.broker.store.submit(pid, self.teacher, 'minor', {**sf, 'extra.py': b'# new\n'}, sr)
        self.api(
            f'projects/{pid}/purge',
            {'plan_token': prev['plan_token'], 'confirmation': '确认清理项目', 'selected_keys': []},
            expected=400,
        )

        # Partial audit persistence on derived blob unlink failure
        d_res = self.api(
            f'projects/{pid}/derived',
            {'path': 'outputs/grid.csv', 'name': 'blob-unlink-fail', 'tags': ['测试'], 'description': '测试部分失败审计'},
            expected=201,
        )
        with self.broker.store._connection() as con:
            con.execute('UPDATE web_derived_datasets SET source_submission_id=? WHERE dataset_id=?', (_sub['submission_id'], d_res['dataset_id']))
        prev2 = self.api(f'projects/{pid}/purge-preview', {})
        orig_unlink = os.unlink

        def flaky_unlink(path, *args, **kwargs):
            if '.project-purge-' in str(path):
                raise OSError('simulated unlink error')
            return orig_unlink(path, *args, **kwargs)

        with unittest.mock.patch('os.unlink', side_effect=flaky_unlink):
            receipt = self.broker._project_purge().execute(
                self.teacher,
                pid,
                plan_token=prev2['plan_token'],
                confirmation='确认清理项目',
            )
        assert receipt['status'] == 'partial'
        audits = self.broker._project_purge().list_audit(self.teacher, limit=5)
        assert audits[0]['status'] == 'partial'

        # Standalone CLI copied to /tmp
        src_cli = Path(__file__).resolve().parents[1] / 'tools' / 'rkweb-purge.py'
        tmp_cli = Path('/tmp') / f'rkweb-purge-test-{uuid.uuid4().hex[:8]}.py'
        try:
            shutil.copyfile(src_cli, tmp_cli)
            out = subprocess.check_output([sys.executable, str(tmp_cli), '--help'], text=True)
            assert 'purge' in out.lower() or 'policy' in out.lower()
        finally:
            tmp_cli.unlink(missing_ok=True)


    def test_algorithm_bind_verifies_existing_lock_and_is_idempotent(self):
        from researchkit.model import verify_lock

        _pid, sub = self._seed_project_and_submission()
        self.api(
            f'review/submissions/{sub["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sub['content_sha256'],
                'name': 'lock.scale',
                'version': '1.0.0',
                'tags': ['锁校验'],
                'description': '依赖锁与幂等重复绑定测试算法',
            },
        )
        local_a = self.api(
            'local-algorithms',
            {
                'name': 'lock-local',
                'version': '1.0.0',
                'tags': ['本地'],
                'description': '本地算法锁测试',
                'plugin_py': 'def run(inputs, parameters, context):\n    return {"ok": True}\n',
            },
            expected=201,
        )

        proj = self.api('projects', {'name': 'lock-tamper-proj'}, expected=201)
        pid = proj['project_id']
        proj_dir = Path(proj['path'])
        (proj_dir / 'case.py').write_text('def run(context):\n    return {"ok": True}\n', encoding='utf-8')

        first = self.api(
            f'projects/{pid}/algorithm-materials',
            {'algorithm_refs': ['lock.scale@1.0.0'], 'alias': 'scale_a', 'bind': True, 'parameters': {'factor': 4}},
        )
        assert first['bound'] is True
        locked_plugin = proj_dir / '.researchkit/plugins/scale_a/plugin.py'
        orig_bytes = locked_plugin.read_bytes()
        lock_before = (proj_dir / '.researchkit/lock.json').read_text(encoding='utf-8')

        # Round 2 Issue 1: tamper protected plugin file -> add_algorithms MUST reject before re-signing lock.json
        locked_plugin.write_bytes(b'def run(inputs, parameters, context):\n    return {"tampered": True}\n')
        tampered_bind = self.api(
            f'projects/{pid}/algorithm-materials',
            {'local_algorithm_id': local_a['id'], 'alias': 'loc_after_tamper', 'bind': True},
            expected=400,
        )
        assert '固定依赖发生变化' in tampered_bind['error']
        assert not (proj_dir / 'algorithms/loc_after_tamper').exists()
        assert (proj_dir / '.researchkit/lock.json').read_text(encoding='utf-8') == lock_before

        tampered_legacy = self.api(
            f'projects/{pid}/algorithm-materials',
            {'algorithm_refs': ['lock.scale@1.0.0']},
            expected=400,
        )
        assert '固定依赖发生变化' in tampered_legacy['error']

        # Restore protected file and verify idempotent re-bind preserves unrelated edits
        locked_plugin.write_bytes(orig_bytes)
        verify_lock(proj_dir)
        (proj_dir / 'case.py').write_text('# user edit preserved\ndef run(context):\n    return {"ok": True}\n', encoding='utf-8')

        repeat_same = self.api(
            f'projects/{pid}/algorithm-materials',
            {'algorithm_refs': ['lock.scale@1.0.0'], 'alias': 'scale_a', 'bind': True, 'parameters': {'factor': 4}},
        )
        assert repeat_same['bound'] is True
        assert repeat_same['added'] is False
        assert repeat_same['idempotent'] is True
        assert '# user edit preserved' in (proj_dir / 'case.py').read_text(encoding='utf-8')
        verify_lock(proj_dir)

        loc_bind = self.api(
            f'projects/{pid}/algorithm-materials',
            {'local_algorithm_id': local_a['id'], 'alias': 'loc_b', 'bind': True},
        )
        assert loc_bind['bound'] is True
        loc_repeat = self.api(
            f'projects/{pid}/algorithm-materials',
            {'local_algorithm_id': local_a['id'], 'alias': 'loc_b', 'bind': True},
        )
        assert loc_repeat['idempotent'] is True
        assert loc_repeat['added'] is False

    def test_template_publish_rejects_same_version_algorithm_content_conflict(self):
        # Publish baseline hydro.conflict@1.0.0 with factor=2 and hydro.conflict@1.0.1 with factor=9
        _pid1, sub1 = self._seed_project_and_submission(_plugin_project_files(factor=2))
        self.api(
            f'review/submissions/{sub1["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sub1['content_sha256'],
                'name': 'hydro.conflict',
                'version': '1.0.0',
                'tags': ['冲突测试'],
                'description': '基线算法 1.0.0',
            },
        )
        _pid1b, sub1b = self._seed_project_and_submission(_plugin_project_files(factor=9))
        self.api(
            f'review/submissions/{sub1b["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sub1b['content_sha256'],
                'publish_mode': 'new_version',
                'parent_ref': 'hydro.conflict@1.0.0',
                'version': '1.0.1',
                'tags': ['冲突测试'],
                'description': '基线算法 1.0.1',
            },
        )

        # Case A: local algorithm promoted into existing hydro.conflict@1.0.0 with different code (factor=5)
        loc_files = {
            'project.yaml': (
                b'schema: researchkit.project/v1\nname: loc-conflict\nmode: python\nentry: case.py\n'
                b'plugins:\n  custom_s:\n    path: algorithms/custom_s\n    parameters:\n      factor: 2\n'
                b'parameters: {}\ndata: {}\nrequires: {}\n'
            ),
            'case.py': b'def run(context):\n    return {"ok": True}\n',
            'algorithms/custom_s/plugin.yaml': (
                b'schema: hydro.plugin/v1\nname: custom_s\nversion: 1.0.0\nentry: plugin.py\n'
                b'inputs: {}\noutputs: {}\nparameters:\n  factor:\n    type: number\n    required: true\n'
            ),
            'algorithms/custom_s/plugin.py': b'def run(inputs, parameters, context):\n    return {"result": 5}\n',
        }
        _pid2, sub2 = self._seed_project_and_submission(loc_files)
        err_local = self.api(
            f'review/submissions/{sub2["submission_id"]}/publish',
            {
                'kind': 'template',
                'expected_sha256': sub2['content_sha256'],
                'name': 'conflict.case',
                'version': '1.0.0',
                'tags': ['冲突测试'],
                'description': '同版本不同代码不得覆盖',
                'embedded_algorithms': {
                    'custom_s': {
                        'action': 'promote',
                        'name': 'hydro.conflict',
                        'version': '1.0.0',
                        'tags': ['冲突测试'],
                        'description': '冲突版本',
                    },
                },
            },
            expected=400,
        )
        assert '已存在不同内容' in err_local['error']

        # Case B: modified platform algorithm targeting existing hydro.conflict@1.0.1 with factor=7
        mod_files = _plugin_project_files(factor=7)
        mod_files['plugins/scale/plugin.yaml'] = mod_files['plugins/scale/plugin.yaml'].replace(b'name: scale', b'name: hydro.conflict')
        _pid3, sub3 = self._seed_project_and_submission(mod_files)
        err_mod = self.api(
            f'review/submissions/{sub3["submission_id"]}/publish',
            {
                'kind': 'template',
                'expected_sha256': sub3['content_sha256'],
                'name': 'conflict.modcase',
                'version': '1.0.0',
                'tags': ['冲突测试'],
                'description': '修改平台算法指定冲突版本号也必须拒绝',
                'embedded_algorithms': {'scale': {'action': 'new_version', 'version': '1.0.1'}},
            },
            expected=400,
        )
        assert '已存在不同内容' in err_mod['error']

        cat = self.api('catalog', method='GET')
        assert not any(t['ref'] in {'conflict.case@1.0.0', 'conflict.modcase@1.0.0'} for t in cat['templates'])

    def test_review_publish_strict_metadata_and_parent_ref_validation(self):
        pid, sub = self._seed_project_and_submission()
        sid = sub['submission_id']
        sha = sub['content_sha256']

        # publish_mode='new' requires non-empty tags AND description for algorithm & derived too
        r_alg_no_meta = self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'algorithm', 'aliases': ['scale'], 'expected_sha256': sha, 'name': 'strict.alg', 'version': '1.0.0', 'tags': [], 'description': ''},
            expected=400,
        )
        assert '标签' in r_alg_no_meta['error']

        ds = self.api(
            f'projects/{pid}/derived',
            {'path': 'outputs/grid.csv', 'name': 'strict-ds', 'tags': ['原标签'], 'description': '原说明'},
            expected=201,
        )
        r_ds_no_meta = self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'derived', 'expected_sha256': sha, 'dataset_id': ds['dataset_id'], 'tags': [], 'description': ''},
            expected=400,
        )
        assert '标签' in r_ds_no_meta['error']

        # publish_mode='new_version' rejects missing, nonexistent, and cross-kind parent_ref
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'template',
                'expected_sha256': sha,
                'name': 'strict.case',
                'version': '1.0.0',
                'tags': ['严格测试'],
                'description': '父案例',
            },
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['scale'],
                'expected_sha256': sha,
                'name': 'strict.alg',
                'version': '1.0.0',
                'tags': ['严格测试'],
                'description': '父算法',
            },
        )

        # Missing / nonexistent parent_ref on new_version
        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'template', 'publish_mode': 'new_version', 'parent_ref': '', 'expected_sha256': sha, 'version': '1.0.1'},
            expected=400,
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'template', 'publish_mode': 'new_version', 'parent_ref': 'missing.case@1.0.0', 'expected_sha256': sha, 'version': '1.0.1'},
            expected=400,
        )
        # Cross-kind parent_ref (algorithm parent for template, and template parent for algorithm)
        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'template', 'publish_mode': 'new_version', 'parent_ref': 'strict.alg@1.0.0', 'expected_sha256': sha, 'version': '1.0.1'},
            expected=400,
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'algorithm', 'aliases': ['scale'], 'publish_mode': 'new_version', 'parent_ref': 'strict.case@1.0.0', 'expected_sha256': sha, 'version': '1.0.1'},
            expected=400,
        )
        self.api(
            f'review/submissions/{sid}/publish',
            {'kind': 'derived', 'publish_mode': 'new_version', 'dataset_id': ds['dataset_id'], 'parent_ref': 'strict.alg@1.0.0', 'expected_sha256': sha},
            expected=400,
        )

    def test_purge_crash_recovery_journals_and_system_python_reexec(self):
        from researchkit.web_project_purge import ProjectPurge

        # Construct ProjectPurge once first (its __init__ already runs recover_pending())
        pp = ProjectPurge(self.broker.store, self.broker.policy.code_root / 'library', self.broker.policy)

        # 1. Central ProjectPurge recovery before commit (roll back staged blob) and after commit (unlink leftover)
        p_rollback = self.api('projects', {'name': 'purge-rollback-proj'}, expected=201)['project_id']
        orig_blob = self.broker.policy.derived_dataset_root / 'ds-rollback.csv'
        orig_blob.parent.mkdir(parents=True, exist_ok=True)
        holding_pre = orig_blob.parent / f'.project-purge-{uuid.uuid4().hex[:8]}.blob'
        holding_pre.write_text('precious-data', encoding='utf-8')
        audit_pre = str(uuid.uuid4())
        rfile_pre = self.broker.store.directory / f'.purge-recovery-{audit_pre}.json'
        rfile_pre.write_text(json.dumps({
            'audit_id': audit_pre,
            'project_id': p_rollback,
            'staged_blobs': [[str(orig_blob), str(holding_pre)]],
        }), encoding='utf-8')

        rec1 = pp.recover_pending()
        assert any(r['audit_id'] == audit_pre and r['action'] == 'rolled_back_staged_blobs' for r in rec1)
        assert orig_blob.exists() and orig_blob.read_text(encoding='utf-8') == 'precious-data'
        assert not holding_pre.exists() and not rfile_pre.exists()

        # After-commit simulation
        holding_post = orig_blob.parent / f'.project-purge-{uuid.uuid4().hex[:8]}.blob'
        holding_post.write_text('orphan-after-commit', encoding='utf-8')
        audit_post = str(uuid.uuid4())
        with self.broker.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute(
                """INSERT INTO web_project_purge_audit
                   (audit_id, project_id, project_name, owner_username, actor_uid, actor_username,
                    plan_token, status, deleted_json, retained_json, failed_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (audit_post, str(uuid.uuid4()), 'gone', 'ly', self.teacher.uid, 'ly', 'tok', 'completed', '[]', '[]', '[]', '2026-11-14T00:00:00Z'),
            )
            con.execute(
                """INSERT INTO web_project_purge_recovery
                   (audit_id, project_id, phase, staged_blobs_json, deleted_json, failed_json, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (audit_post, str(uuid.uuid4()), 'after_commit', json.dumps([[str(orig_blob), str(holding_post)]]), '[]', '[]', '2026-11-14T00:00:00Z'),
            )
            con.execute('COMMIT')
        rfile_post = self.broker.store.directory / f'.purge-recovery-{audit_post}.json'
        rfile_post.write_text(json.dumps({'audit_id': audit_post, 'project_id': 'gone', 'staged_blobs': [[str(orig_blob), str(holding_post)]]}), encoding='utf-8')

        rec2 = pp.recover_pending()
        assert any(r['audit_id'] == audit_post for r in rec2)
        assert not holding_post.exists() and not rfile_post.exists()
        with self.broker.store._connection() as con:
            assert con.execute('SELECT 1 FROM web_project_purge_recovery WHERE audit_id=?', (audit_post,)).fetchone() is None

        # 2. Workspace crash recovery: both central-exists restore AND central-gone finalize
        pk = self.api('projects', {'name': 'ws-keep'}, expected=201)
        orig_keep = Path(pk['path'])
        staging_keep = self.workspace.projects_root / f'.purge-staging-{pk["project_id"]}-test'
        os.rename(orig_keep, staging_keep)
        j_keep = self.workspace.projects_root / f'.purge-recovery-{pk["project_id"]}.json'
        j_keep.write_text(json.dumps({'project_id': pk['project_id'], 'orig_path': str(orig_keep), 'staging_path': str(staging_keep)}), encoding='utf-8')
        w_rec1 = self.workspace.recover_pending_purges()
        assert any(r['project_id'] == pk['project_id'] and r['action'] == 'restored_workspace' for r in w_rec1)
        assert orig_keep.is_dir() and not staging_keep.exists() and not j_keep.exists()

        pg = self.api('projects', {'name': 'ws-gone'}, expected=201)
        pg_id = pg['project_id']
        orig_gone = Path(pg['path'])
        staging_gone = self.workspace.projects_root / f'.purge-staging-{pg_id}-test'
        os.rename(orig_gone, staging_gone)
        prev_pg = self.broker._project_purge().preview(self.teacher, pg_id)
        self.broker._project_purge().execute(self.teacher, pg_id, plan_token=prev_pg['plan_token'], confirmation='确认清理项目')
        j_gone = self.workspace.projects_root / f'.purge-recovery-{pg_id}.json'
        j_gone.write_text(json.dumps({'project_id': pg_id, 'orig_path': str(orig_gone), 'staging_path': str(staging_gone)}), encoding='utf-8')
        w_rec2 = self.workspace.recover_pending_purges()
        assert any(r['project_id'] == pg_id and r['action'] == 'completed_purge' for r in w_rec2)
        assert not staging_gone.exists() and not j_gone.exists()
        assert pg_id not in self.workspace._load_state()
        audits = self.broker._project_purge().list_audit(self.teacher, limit=5)
        pg_audit = next(a for a in audits if a['project_id'] == pg_id)
        assert any(d.get('kind') == 'workspace_copy' for d in pg_audit['deleted'])

        # 3. Standalone rkweb-purge CLI invoked via system Python (<3.10 when available) re-execs cleanly
        import subprocess
        import sys
        src_cli = Path(__file__).resolve().parents[1] / 'tools' / 'rkweb-purge.py'
        tmp_cli = Path('/tmp') / f'rkweb-purge-reexec-{uuid.uuid4().hex[:8]}.py'
        sys_py = '/usr/bin/python3' if Path('/usr/bin/python3').is_file() else sys.executable
        try:
            shutil.copyfile(src_cli, tmp_cli)
            env = {
                'PATH': os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
                'PYTHONPATH': '',
                'RKWEB_APP_ROOT': str(Path(__file__).resolve().parents[1]),
            }
            proc = subprocess.run([sys_py, str(tmp_cli), '--help'], env=env, text=True, capture_output=True, check=False)
            assert proc.returncode == 0, proc.stderr
            assert 'purge' in proc.stdout.lower() or 'policy' in proc.stdout.lower()
        finally:
            tmp_cli.unlink(missing_ok=True)

    def test_round3_recovery_journals_reject_escape_preserve_audit_and_serialize(self):
        from researchkit.web_project_purge import ProjectPurge

        # 1) Out-of-bounds journal paths & symlinks MUST be rejected and NEVER delete outside files
        outside_dir = self.root / 'outside-victim-dir'
        outside_dir.mkdir(mode=0o700)
        victim_file = outside_dir / 'precious.txt'
        victim_file.write_bytes(b'must-not-delete')

        evil_pid = str(uuid.uuid4())
        evil_journal = self.workspace.projects_root / f'.purge-recovery-{evil_pid}.json'
        evil_journal.write_text(json.dumps({
            'project_id': evil_pid,
            'orig_path': str(outside_dir),
            'staging_path': str(outside_dir),
        }), encoding='utf-8')

        sym_pid = str(uuid.uuid4())
        sym_staging = self.workspace.projects_root / f'.purge-staging-{uuid.uuid4().hex}'
        os.symlink(outside_dir, sym_staging)
        sym_journal = self.workspace.projects_root / f'.purge-recovery-{sym_pid}.json'
        sym_journal.write_text(json.dumps({
            'project_id': sym_pid,
            'orig_path': str(self.workspace.projects_root / f'proj-{sym_pid}'),
            'staging_path': str(sym_staging),
        }), encoding='utf-8')

        rec = self.workspace.recover_pending_purges()
        assert outside_dir.is_dir() and victim_file.read_bytes() == b'must-not-delete'
        assert sum(1 for r in rec if r.get('action') == 'rejected_unsafe_journal') >= 2

        evil_journal.unlink()
        sym_staging.unlink()
        sym_journal.unlink()

        # Also reject out-of-bounds staged_blobs in central ProjectPurge recovery
        pp = ProjectPurge(self.broker.store, self.broker.policy.code_root / 'library', self.broker.policy)
        bad_audit = str(uuid.uuid4())
        bad_rfile = self.broker.store.directory / f'.purge-recovery-{bad_audit}.json'
        bad_rfile.write_text(json.dumps({
            'audit_id': bad_audit,
            'project_id': 'nonexistent-pid',
            'staged_blobs': [[str(victim_file), str(outside_dir / f'.project-purge-{uuid.uuid4().hex[:8]}.blob')]],
        }), encoding='utf-8')
        try:
            with self.broker.store._connection() as con:
                con.execute('BEGIN IMMEDIATE')
                con.execute(
                    """INSERT INTO web_project_purge_audit
                       (audit_id, project_id, project_name, owner_username, actor_uid, actor_username,
                        plan_token, status, deleted_json, retained_json, failed_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bad_audit, 'nonexistent-pid', 'x', 'ly', self.teacher.uid, 'ly', 'tok', 'completed', '[]', '[]', '[]', '2026-11-14T00:00:00Z'),
                )
                con.execute('COMMIT')
            pp_rec = pp.recover_pending()
            assert any(r.get('audit_id') == bad_audit and r.get('action') == 'rejected_unsafe_journal' for r in pp_rec)
            assert victim_file.read_bytes() == b'must-not-delete'
        finally:
            if bad_rfile.exists():
                bad_rfile.unlink()

        # 2) Audit update failure preserves journal + deleted_extra and retries cleanly
        pg = self.api('projects', {'name': 'ws-audit-retry'}, expected=201)
        pg_id = pg['project_id']
        orig_gone = Path(pg['path'])
        staging_gone = self.workspace.projects_root / f'.purge-staging-{uuid.uuid4().hex}'
        os.rename(orig_gone, staging_gone)
        prev_pg = pp.preview(self.teacher, pg_id)
        pp.execute(self.teacher, pg_id, plan_token=prev_pg['plan_token'], confirmation='确认清理项目')
        j_gone = self.workspace.projects_root / f'.purge-recovery-{pg_id}.json'
        j_gone.write_text(json.dumps({
            'project_id': pg_id,
            'orig_path': str(orig_gone),
            'staging_path': str(staging_gone),
        }), encoding='utf-8')

        orig_request = self.workspace.store.request

        def fail_audit_once(method, **kwargs):
            if method == 'project_purge_update_audit':
                raise KitError('simulated transient audit failure')
            return orig_request(method, **kwargs)

        with unittest.mock.patch.object(self.workspace.store, 'request', side_effect=fail_audit_once):
            w_first = self.workspace.recover_pending_purges()
        assert any(r.get('project_id') == pg_id and r.get('action') == 'pending_audit_update' for r in w_first)
        assert not staging_gone.exists()
        assert j_gone.is_file()
        assert json.loads(j_gone.read_text(encoding='utf-8')).get('deleted_extra')

        # Retry succeeds and records workspace_copy before removing journal
        w_retry = self.workspace.recover_pending_purges()
        assert any(r.get('project_id') == pg_id and r.get('action') == 'completed_purge' for r in w_retry)
        assert not j_gone.exists()
        pg_audit = next(a for a in pp.list_audit(self.teacher, limit=10) if a['project_id'] == pg_id)
        assert any(d.get('kind') == 'workspace_copy' for d in pg_audit['deleted'])

        # 3) Lock re-entrancy (no self-deadlock inside _state_lock or _recovery_lock)
        with self.workspace._state_lock():
            assert isinstance(self.workspace.recover_pending_purges(), list)
        with pp._recovery_lock():
            assert isinstance(pp.recover_pending(), list)

    def test_round3_management_purge_cleans_central_only_and_keeps_teacher_workspace(self):
        created = self.api('projects', {'name': 'ly-central-only'}, expected=201)
        pid = created['project_id']
        proj_dir = Path(created['path'])
        (proj_dir / 'case.py').write_text('def run(context):\n    return {"ok": 1}\n', encoding='utf-8')
        with self.workspace._project_guard(pid):
            _r, snap_files, refs = self.workspace._snapshot(pid)
        sub = self.broker.store.submit(pid, self.teacher, 'major', snap_files, refs)

        prev = self.api(f'projects/{pid}/purge-preview', {})
        assert prev['can_execute'] is True
        del_step = next(s for s in prev['execution_order'] if s['step'] == 'delete_project')
        assert '保留各用户个人工作区本地目录' in del_step['label']

        receipt = self.api(
            f'projects/{pid}/purge',
            {'plan_token': prev['plan_token'], 'confirmation': '确认清理项目', 'reset_task': True},
        )
        assert receipt['status'] == 'completed'

        # Central project & submission removed, while ly's personal workspace directory stays intact!
        with self.assertRaises(KitError):
            self.broker.store.get_project(pid, self.teacher)
        with self.assertRaises(KitError):
            self.broker.store.get_submission(sub['submission_id'], self.teacher)
        assert proj_dir.is_dir()

        listed = self.api('projects', method='GET')
        row = next(p for p in listed if p['project_id'] == pid)
        assert row['central_purged'] is True
        assert row['source'] == 'local'

        deleted = self.api(f'projects/{pid}/delete-local', {'confirmation': '确认删除本地副本'})
        assert deleted['deleted_local'] is True
        assert not proj_dir.exists()
        assert not any(p['project_id'] == pid for p in self.api('projects', method='GET'))

    def test_round3_same_version_algorithm_contract_change_rejected_and_detected(self):
        # 1) Platform algorithm: unchanged vs altered contract IO detected by ReviewFlow inspect (_algorithms)
        lib_dir = Path.cwd() / 'library'
        scale_dir = lib_dir / 'algorithms' / 'example.scale' / '1.0.0'
        scalar_contract = (lib_dir / 'contracts' / 'scalar' / '1.0.0.yaml').read_bytes()
        plat_files = {
            'project.yaml': (
                b'schema: researchkit.project/v1\n'
                b'name: plat-contract-check\n'
                b'mode: python\n'
                b'entry: case.py\n'
                b'plugins:\n'
                b'  scale:\n'
                b'    path: .researchkit/plugins/scale\n'
                b'    source_ref: example.scale@1.0.0\n'
                b'    parameters:\n'
                b'      factor: 2\n'
                b'parameters: {}\n'
                b'data: {}\n'
                b'requires: {}\n'
            ),
            'case.py': b'def run(context):\n    return {"ok": True}\n',
            '.researchkit/contracts/scalar/1.0.0.yaml': scalar_contract,
        }
        for fp in scale_dir.rglob('*'):
            if fp.is_file() and '__pycache__' not in fp.parts:
                plat_files['.researchkit/plugins/scale/' + fp.relative_to(scale_dir).as_posix()] = fp.read_bytes()
        _p_clean, sub_clean = self._seed_project_and_submission(plat_files)
        insp_clean = self.api(f'review/submissions/{sub_clean["submission_id"]}/release', method='GET')
        alg_clean = next(a for a in insp_clean['algorithms'] if a['alias'] == 'scale')
        assert alg_clean['modified_platform_algorithm'] is False

        plat_mod = dict(plat_files)
        plat_mod['.researchkit/contracts/scalar/1.0.0.yaml'] = (
            b'schema: hydro.contract/v1\n'
            b'id: scalar@1.0.0\n'
            b'type: mapping\n'
            b'description: mutated contract type without bumping version\n'
        )
        _p_mod, sub_mod = self._seed_project_and_submission(plat_mod)
        insp_mod = self.api(f'review/submissions/{sub_mod["submission_id"]}/release', method='GET')
        alg_mod = next(a for a in insp_mod['algorithms'] if a['alias'] == 'scale')
        assert alg_mod['modified_platform_algorithm'] is True

        # 2) Same-version algorithm with identical plugin.py & plugin.yaml but modified contract YAML MUST fail reuse
        contract_v1 = (
            b'schema: hydro.contract/v1\n'
            b'id: io.vector@1.0.0\n'
            b'type: sequence\n'
            b'description: vector v1 contract\n'
        )
        io_files = {
            'project.yaml': (
                b'schema: researchkit.project/v1\n'
                b'name: io-contract-test\n'
                b'mode: python\n'
                b'entry: case.py\n'
                b'plugins:\n'
                b'  vec:\n'
                b'    path: plugins/vec\n'
                b'    parameters: {}\n'
                b'parameters: {}\n'
                b'data: {}\n'
                b'requires: {}\n'
            ),
            'case.py': b'def run(context):\n    return {"ok": True}\n',
            'plugins/vec/plugin.yaml': (
                b'schema: hydro.plugin/v1\n'
                b'name: io.calc\n'
                b'version: 1.0.0\n'
                b'entry: plugin.py\n'
                b'inputs:\n'
                b'  arr: io.vector@1.0.0\n'
                b'outputs:\n'
                b'  arr: io.vector@1.0.0\n'
                b'parameters: {}\n'
            ),
            'plugins/vec/plugin.py': b'def run(inputs, parameters, context):\n    return {"arr": inputs.get("arr", [])}\n',
            '.researchkit/contracts/io.vector/1.0.0.yaml': contract_v1,
        }
        _p1, sub1 = self._seed_project_and_submission(io_files)
        pub1 = self.api(
            f'review/submissions/{sub1["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['vec'],
                'expected_sha256': sub1['content_sha256'],
                'name': 'io.calc',
                'version': '1.0.0',
                'tags': ['契约'],
                'description': '初代向量算法',
            },
        )
        assert pub1['published'][0]['ref'] == 'io.calc@1.0.0'

        io_changed_contract = dict(io_files)
        io_changed_contract['.researchkit/contracts/io.vector/1.0.0.yaml'] = (
            b'schema: hydro.contract/v1\n'
            b'id: io.vector@1.0.0\n'
            b'type: mapping\n'
            b'description: breaking change to mapping without new contract version\n'
        )
        _p2, sub2 = self._seed_project_and_submission(io_changed_contract)
        pub_same = self.api(
            f'review/submissions/{sub2["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['vec'],
                'expected_sha256': sub2['content_sha256'],
                'name': 'io.calc',
                'version': '1.0.0',
                'tags': ['契约'],
                'description': '同版本改契约应被拒绝',
            },
        )
        assert pub_same['published'] == []
        assert len(pub_same.get('failures', [])) == 1

        # 3) Different algorithm name referencing existing io.vector@1.0.0 with conflicting definition MUST also fail
        io_other_contract = dict(io_changed_contract)
        io_other_contract['plugins/vec/plugin.yaml'] = io_changed_contract['plugins/vec/plugin.yaml'].replace(
            b'name: io.calc\n', b'name: io.other\n', 1
        )
        _p3, sub3 = self._seed_project_and_submission(io_other_contract)
        pub_new_name = self.api(
            f'review/submissions/{sub3["submission_id"]}/publish',
            {
                'kind': 'algorithm',
                'aliases': ['vec'],
                'expected_sha256': sub3['content_sha256'],
                'name': 'io.other',
                'version': '1.0.0',
                'tags': ['契约'],
                'description': '新算法引用冲突契约也应被拒绝',
            },
        )
        assert pub_new_name['published'] == []
        assert '契约 io.vector@1.0.0' in pub_new_name['failures'][0]['error']

    def test_round3_failed_template_publish_rolls_back_embedded_algorithms_and_datasets(self):
        # Publish baseline fail.case@1.0.0 first so subsequent conflicting template publish fails
        base_files = _plugin_project_files(factor=1)
        _p0, sub0 = self._seed_project_and_submission(base_files)
        pub0 = self.api(
            f'review/submissions/{sub0["submission_id"]}/publish',
            {
                'kind': 'template',
                'expected_sha256': sub0['content_sha256'],
                'name': 'fail.case',
                'version': '1.0.0',
                'tags': ['事务测试'],
                'description': '占位的 1.0.0 案例',
            },
        )
        assert pub0['published'][0]['ref'] == 'fail.case@1.0.0'

        # Create T1 with a different case.py, local algorithm emb_alg, and a private derived dataset attached
        p1 = self.api('projects', {'name': 'rollback-source'}, expected=201)['project_id']
        proj_dir = Path(self.workspace._project_path(p1))
        t1_files = _plugin_project_files(factor=99)
        t1_files['case.py'] = b'def run(context):\n    return {"ok": 99}\n'
        for rel, raw in t1_files.items():
            target = proj_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        priv_ds = self.api(
            f'projects/{p1}/derived',
            {'path': 'outputs/grid.csv', 'name': '私有回滚测试切片'},
            expected=201,
        )
        priv_did = priv_ds['dataset_id']
        ds_state = self.api(f'projects/{p1}/datasets', method='GET')
        self.api(
            f'projects/{p1}/datasets',
            {'dataset_ids': [priv_did], 'expected_sha256': ds_state['sha256']},
            method='PUT',
        )

        with self.workspace._project_guard(p1):
            _r, snap_files, refs = self.workspace._snapshot(p1)
        sub1 = self.broker.store.submit(p1, self.teacher, 'major', snap_files, refs)
        jobs = Jobs(self.broker.store)
        for op in ('check', 'run'):
            job = jobs.request(self.teacher, sub1['submission_id'], op)
            jobs.claim('v08-worker')
            jobs.finish('v08-worker', job['run_id'], 'succeeded', 0, None, {'results/ok.txt': b'ok'})

        res = self.api(
            f'review/submissions/{sub1["submission_id"]}/publish',
            {
                'kind': 'template',
                'expected_sha256': sub1['content_sha256'],
                'name': 'fail.case',
                'version': '1.0.0',
                'tags': ['事务测试'],
                'description': '同版本冲突应全部回滚',
                'embedded_algorithms': {
                    'scale': {
                        'action': 'promote',
                        'name': 'emb.atomic',
                        'version': '1.0.0',
                        'tags': ['回滚'],
                        'description': '失败时必须回滚的内嵌算法',
                    },
                },
                'embedded_datasets': {
                    priv_did: {
                        'action': 'publish_derived',
                        'tags': ['回滚'],
                        'description': '失败时必须回滚的内嵌派生数据集',
                    },
                },
            },
        )
        assert res['published'] == []
        assert res['failures'][0]['ref'] == 'fail.case@1.0.0'

        catalog_after = self.api('catalog', method='GET')
        assert not any(p['ref'] == 'emb.atomic@1.0.0' for p in catalog_after['plugins'])
        with self.broker.store._connection() as con:
            assert con.execute("SELECT 1 FROM web_asset_published WHERE kind='algorithm' AND ref='emb.atomic@1.0.0'").fetchone() is None
            assert con.execute("SELECT 1 FROM web_asset_candidates WHERE kind='algorithm' AND ref='emb.atomic@1.0.0'").fetchone() is None
            drow = con.execute('SELECT visibility, description FROM web_derived_datasets WHERE dataset_id=?', (priv_did,)).fetchone()
            assert drow['visibility'] == 'private'
            assert con.execute('SELECT 1 FROM web_dataset_policies WHERE dataset_id=?', (priv_did,)).fetchone() is None

    def test_failed_publish_rolls_back_only_its_transaction_and_keeps_concurrent_upload(self):
        """A failed review must never compensate by restoring whole shared tables."""
        _first_pid, first = self._seed_project_and_submission(_plugin_project_files(factor=2))
        flow = ReviewFlow(self.broker.store, self.broker.policy.code_root / 'library')
        assert flow.publish(
            self.teacher, first['submission_id'], 'algorithm', ['scale'], first['content_sha256'],
            name='atomic.concurrent', version='1.0.0', tags=['事务'], description='first',
        )['published']

        _second_pid, second = self._seed_project_and_submission(_plugin_project_files(factor=5))
        derived = DerivedDatasets(self.broker.store, self.root / 'derived')
        raw = b'independent student output\n'
        digest = hashlib.sha256(raw).hexdigest()
        candidate_started, upload_attempted, upload_done = threading.Event(), threading.Event(), threading.Event()
        saved: dict[str, str] = {}
        original_candidate = flow._candidate

        def pause_after_tentative_candidate(*args, **kwargs):
            result = original_candidate(*args, **kwargs)
            candidate_started.set()
            assert upload_attempted.wait(5)
            return result

        def save_unrelated_dataset():
            assert candidate_started.wait(5)
            # The outer review owns SQLite's writer lock at this point.  Mark
            # the competing write before it waits for that lock, then prove it
            # commits after the failed review rolls back.
            upload_attempted.set()
            upload = derived.begin_upload(self.student, 'independent-result', len(raw), digest, [])
            derived.append_upload(self.student, upload['upload_id'], 0, raw)
            result = derived.commit_upload(self.student, upload['upload_id'], digest)
            saved['dataset_id'] = str(result['dataset_id'])
            upload_done.set()

        writer = threading.Thread(target=save_unrelated_dataset, daemon=True)
        writer.start()
        with unittest.mock.patch.object(flow, '_candidate', side_effect=pause_after_tentative_candidate):
            failed = flow.publish(
                self.teacher, second['submission_id'], 'algorithm', ['scale'], second['content_sha256'],
                name='atomic.concurrent', version='1.0.0', tags=['事务'], description='conflicts',
            )
        assert failed['published'] == [] and failed['failures']
        assert upload_done.wait(10)
        writer.join(timeout=2)
        assert not writer.is_alive()
        with self.broker.store._connection() as con:
            assert con.execute(
                'SELECT 1 FROM web_derived_datasets WHERE dataset_id=?', (saved['dataset_id'],)
            ).fetchone() is not None
            assert con.execute(
                "SELECT 1 FROM web_asset_candidates WHERE kind='algorithm' AND ref='atomic.concurrent@1.0.0' "
                'AND submission_id=?', (second['submission_id'],)
            ).fetchone() is None

    def test_template_rollback_hides_tentative_assets_and_preserves_competing_upload(self):
        """No promoted asset is visible before its enclosing case publication commits."""
        _base_pid, base = self._seed_project_and_submission(_plugin_project_files(factor=1))
        flow = ReviewFlow(self.broker.store, self.broker.policy.code_root / 'library')
        assert flow.publish(
            self.teacher, base['submission_id'], 'template', None, base['content_sha256'],
            name='atomic.template', version='1.0.0', tags=['事务'], description='baseline',
        )['published']
        _pid, source = self._seed_project_and_submission(_plugin_project_files(factor=9))
        derived = DerivedDatasets(self.broker.store, self.root / 'derived')
        raw = b'writer that must survive rollback\n'
        digest = hashlib.sha256(raw).hexdigest()
        metadata_written, writer_started, writer_done = threading.Event(), threading.Event(), threading.Event()
        saved: dict[str, str] = {}
        original_metadata = flow.assets.set_metadata

        def save_unrelated_dataset():
            assert metadata_written.wait(5)
            writer_started.set()
            upload = derived.begin_upload(self.student, 'rollback-independent', len(raw), digest, [])
            derived.append_upload(self.student, upload['upload_id'], 0, raw)
            saved['dataset_id'] = str(derived.commit_upload(self.student, upload['upload_id'], digest)['dataset_id'])
            writer_done.set()

        def pause_after_algorithm_metadata(principal, kind, ref, **kwargs):
            result = original_metadata(principal, kind, ref, **kwargs)
            if kind == 'algorithm' and ref == 'atomic.embedded@1.0.0':
                metadata_written.set()
                # A separate connection is allowed to read while the review
                # owns its writer transaction, but it must not observe its
                # tentative asset.  Old per-step commits exposed it here.
                with self.broker.store._connection() as con:
                    assert con.execute(
                        "SELECT 1 FROM web_asset_published WHERE kind='algorithm' AND ref='atomic.embedded@1.0.0'"
                    ).fetchone() is None
                assert writer_started.wait(5)
            return result

        writer = threading.Thread(target=save_unrelated_dataset, daemon=True)
        writer.start()
        with unittest.mock.patch.object(flow.assets, 'set_metadata', side_effect=pause_after_algorithm_metadata):
            result = flow.publish(
                self.teacher, source['submission_id'], 'template', None, source['content_sha256'],
                name='atomic.template', version='1.0.0', tags=['事务'], description='conflict',
                embedded_algorithms={
                    'scale': {
                        'action': 'promote', 'name': 'atomic.embedded', 'version': '1.0.0',
                        'tags': ['事务'], 'description': 'must not escape a failed template',
                    },
                },
            )
        assert result['published'] == [] and result['failures']
        assert writer_done.wait(10)
        writer.join(timeout=2)
        assert not writer.is_alive()
        with self.broker.store._connection() as con:
            assert con.execute("SELECT 1 FROM web_asset_published WHERE kind='algorithm' AND ref='atomic.embedded@1.0.0'").fetchone() is None
            assert con.execute("SELECT 1 FROM web_asset_candidates WHERE kind='algorithm' AND ref='atomic.embedded@1.0.0'").fetchone() is None
            assert con.execute("SELECT 1 FROM web_asset_metadata WHERE kind='algorithm' AND ref='atomic.embedded@1.0.0'").fetchone() is None
            assert con.execute('SELECT 1 FROM web_derived_datasets WHERE dataset_id=?', (saved['dataset_id'],)).fetchone() is not None

    def test_unexpected_publication_error_rolls_back_every_tentative_asset_row(self):
        _pid, submission = self._seed_project_and_submission()
        flow = ReviewFlow(self.broker.store, self.broker.policy.code_root / 'library')
        original_metadata = flow.assets.set_metadata

        def fail_after_metadata(*args, **kwargs):
            original_metadata(*args, **kwargs)
            raise RuntimeError('injected post-metadata failure')

        with unittest.mock.patch.object(flow.assets, 'set_metadata', side_effect=fail_after_metadata):
            with pytest.raises(RuntimeError, match='injected post-metadata'):
                flow.publish(
                    self.teacher, submission['submission_id'], 'algorithm', ['scale'], submission['content_sha256'],
                    name='atomic.exception', version='1.0.0', tags=['事务'], description='must rollback',
                )
        with self.broker.store._connection() as con:
            assert con.execute("SELECT 1 FROM web_asset_candidates WHERE kind='algorithm' AND ref='atomic.exception@1.0.0'").fetchone() is None
            assert con.execute("SELECT 1 FROM web_asset_published WHERE kind='algorithm' AND ref='atomic.exception@1.0.0'").fetchone() is None
            assert con.execute("SELECT 1 FROM web_asset_metadata WHERE kind='algorithm' AND ref='atomic.exception@1.0.0'").fetchone() is None

    def test_round3_release_readme_stops_services_before_update_check(self):
        repo_root = Path(__file__).resolve().parents[1]
        candidates = [
            repo_root / 'exports' / 'releases' / 'web-v0.8-feedback-transaction-20261114' / 'README-RELEASE.md',
            repo_root / 'exports' / 'releases' / 'web-v0.8-workflow-redesign-20261114' / 'README-RELEASE.md',
            repo_root / 'README-RELEASE.md',
        ]
        targets = [p for p in candidates if p.is_file()]
        assert targets, 'Missing README-RELEASE.md'
        for readme in targets:
            text = readme.read_text(encoding='utf-8')
            pos_stop = text.index('systemctl stop "$svc"')
            pos_check = text.index('tools/update-web.py" --check')
            apply_marker = 'tools/update-web.py"\n'
            pos_apply = text.index(apply_marker, pos_check + 1)
            pos_start = text.index('systemctl start "$svc"')
            assert pos_stop < pos_check < pos_apply < pos_start



class _DirectBrokerClient:
    def __init__(self, broker: BrokerServer, principal: Principal):
        self._broker = broker
        self._principal = principal

    def request(self, method: str, **params: object) -> object:
        return self._broker._dispatch(self._principal, {'method': method, 'params': dict(params)})


class TestV08StudentHTTP(AsyncHTTPTestCase):
    token = 'v08-student-token-' * 2

    def get_app(self):
        account = pwd.getpwuid(os.getuid())
        if account.pw_name != 'ly' or os.getuid() != os.geteuid():
            self.skipTest('v0.8 student HTTP tests require the current ly UID')
        self.root = Path(tempfile.mkdtemp(prefix='.rk-v08-stu-', dir=Path.cwd()))
        self.addCleanup(shutil.rmtree, self.root)
        self.teacher = Principal(account.pw_uid, account.pw_name, 'teacher')
        self.student = Principal(account.pw_uid + 9000, 'alice', 'student')
        derived_root = self.root / 'derived'
        derived_root.mkdir(mode=0o700)
        policy = Policy(
            principals=MappingProxyType({self.teacher.uid: self.teacher, self.student.uid: self.student}),
            workspaces=MappingProxyType({self.teacher.uid: self.root / 'ws-ly', self.student.uid: self.root / 'ws-alice'}),
            broker_uid=account.pw_uid,
            broker_username=account.pw_name,
            submission_store=self.root / 'store',
            code_root=Path.cwd(),
            broker_socket=self.root / 'broker.sock',
            derived_dataset_root=derived_root,
        )
        self.broker = BrokerServer(policy)
        self.broker.bind()
        self.thread = threading.Thread(target=self.broker.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_broker)
        self.client = _DirectBrokerClient(self.broker, self.student)
        self.workspace = Workspace(self.student, policy.workspace(self.student), self.client)
        app = application(self.workspace, self.token)
        self.addCleanup(app.wildcard_router.rules[0].target_kwargs['executor'].shutdown, wait=True)
        return app

    def stop_broker(self):
        self.broker.close()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()

    def api(self, route: str, body=None, *, expected: int = 200, method: str = 'POST'):
        kwargs = {'headers': {'Authorization': 'Bearer ' + self.token}}
        if body is not None:
            kwargs.update(method=method, body=json.dumps(body))
            kwargs['headers']['Content-Type'] = 'application/json'
        elif method != 'POST':
            kwargs['method'] = method
        response = self.fetch('/api/' + route, **kwargs)
        assert response.code == expected, f'{route} -> {response.code}: {response.body.decode()}'
        return json.loads(response.body)

    def test_student_purge_rejected_and_delete_local_preserves_central_records(self):
        created = self.api('projects', {'name': 'stu-proj'}, expected=201)
        pid = created['project_id']
        proj_dir = Path(created['path'])
        (proj_dir / 'case.py').write_text('def run(context):\n    return {"ok": True}\n', encoding='utf-8')
        with self.workspace._project_guard(pid):
            _r, snap_files, refs = self.workspace._snapshot(pid)
        sub = self.broker.store.submit(pid, self.student, 'major', snap_files, refs)
        jobs = Jobs(self.broker.store)
        jobs.start_worker('stu-w', os.getuid())
        job = jobs.request(self.student, sub['submission_id'], 'check')
        jobs.claim('stu-w')
        jobs.finish('stu-w', job['run_id'], 'succeeded', 0, None, {'results/ok.txt': b'ok'})

        resp_prev = self.fetch(
            f'/api/projects/{pid}/purge-preview',
            method='POST',
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
            body='{}',
        )
        assert resp_prev.code in (400, 403)
        assert '只有教师管理员' in resp_prev.body.decode('utf-8')

        deleted = self.api(f'projects/{pid}/delete-local', {'confirmation': '确认删除本地副本'})
        assert deleted['deleted_local'] is True
        assert not proj_dir.exists()
        assert not any(p['project_id'] == pid for p in self.api('projects', method='GET'))
        assert self.broker.store.get_submission(sub['submission_id'], self.student) is not None
        assert len(jobs.list(self.student, str(sub['submission_id']))) == 1

    def test_student_project_survives_central_purge_and_can_delete_local(self):
        created = self.api('projects', {'name': 'stu-survive-local'}, expected=201)
        pid = created['project_id']
        proj_dir = Path(created['path'])
        (proj_dir / 'case.py').write_text('def run(context):\n    return {"ok": True}\n', encoding='utf-8')
        with self.workspace._project_guard(pid):
            _r, snap_files, refs = self.workspace._snapshot(pid)
        self.broker.store.submit(pid, self.student, 'major', snap_files, refs)

        # Admin purges central project while student local directory stays untouched
        prev = self.broker._project_purge().preview(self.teacher, pid)
        receipt = self.broker._project_purge().execute(
            self.teacher,
            pid,
            plan_token=prev['plan_token'],
            confirmation='确认清理项目',
        )
        assert receipt['status'] == 'completed'
        assert proj_dir.is_dir()

        listed = self.api('projects', method='GET')
        row = next(p for p in listed if p['project_id'] == pid)
        assert row['central_purged'] is True
        assert row['source'] == 'local'

        deleted = self.api(f'projects/{pid}/delete-local', {'confirmation': '确认删除本地副本'})
        assert deleted['deleted_local'] is True
        assert not proj_dir.exists()
        assert not any(p['project_id'] == pid for p in self.api('projects', method='GET'))
