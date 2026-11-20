"""Tests for 3-step full-chain per-project purge, audit log, and CLI tool."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
from types import MappingProxyType
import uuid

import pytest

from researchkit import web_broker, web_datasets
from researchkit.files import KitError
from researchkit.web_derived import DerivedDatasets
from researchkit.web_identity import Policy, Principal
from researchkit.web_project_purge import _CONFIRMATION
from researchkit.web_store import SubmissionStore
from researchkit.web_versions import apply_versions
from researchkit.web_workspace import Workspace


def _teacher_principal() -> Principal:
    uid = os.geteuid()
    username = pwd.getpwuid(uid).pw_name
    if username != 'ly':
        pytest.skip('project purge tests require the ly account')
    return Principal(uid=uid, username=username, role='teacher')


def _project_files(*, algo_name: str = 'mnist.cnn', algo_version: str = '1.0.0') -> dict[str, bytes]:
    return {
        'project.yaml': (
            b'schema: researchkit.project/v1\nmode: python\nentry: case.py\nname: mnist-demo\n'
            b'plugins:\n  mnist_cnn:\n    path: algorithms/mnist_cnn\n    parameters:\n      epochs: 2\n'
            b'parameters: {}\ndata: {}\nrequires: {}\n'
        ),
        'case.py': b'def run(context):\n    return {"accuracy": 0.85}\n',
        'algorithms/mnist_cnn/plugin.yaml': (
            f'schema: hydro.plugin/v1\nname: {algo_name}\nversion: {algo_version}\nentry: plugin.py\n'
            f'inputs:\n  value: scalar@1.0.0\noutputs:\n  result: scalar@1.0.0\n'
            f'parameters:\n  epochs:\n    type: number\n    required: true\n'
        ).encode('utf-8'),
        'algorithms/mnist_cnn/plugin.py': b'def run(inputs, parameters, context):\n    return {"result": inputs["value"]}\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'schema: hydro.contract/v1\nid: scalar@1.0.0\ntype: number\ndescription: scalar\n',
    }


def _policy(tmp_path: Path, teacher: Principal) -> tuple[Policy, Path]:
    store_dir = tmp_path / 'broker-store'
    store_dir.mkdir(mode=0o700)
    os.chmod(store_dir, 0o700)
    derived_dir = tmp_path / 'derived-root'
    derived_dir.mkdir(mode=0o700)
    os.chmod(derived_dir, 0o700)
    ws_dir = tmp_path / 'ws-root'
    ws_dir.mkdir(mode=0o700)
    os.chmod(ws_dir, 0o700)
    code_root = tmp_path / 'code-root'
    lib_dir = code_root / 'library'
    for sub in ('algorithms', 'cases', 'contracts'):
        (lib_dir / sub).mkdir(parents=True, exist_ok=True)
    for d in (code_root, lib_dir, lib_dir / 'algorithms', lib_dir / 'cases', lib_dir / 'contracts'):
        os.chmod(d, 0o755)

    # Create a read-only shared raw file and registry for shared-mnist-raw-v1.
    raw_dir = tmp_path / 'shared-raw'
    raw_dir.mkdir()
    raw_file = raw_dir / 'mnist-train.idx'
    raw_bytes = b'MNIST_IDX_DEMO'
    raw_file.write_bytes(raw_bytes)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    reg_file = tmp_path / 'dataset-registry.json'
    os.chmod(raw_dir, 0o755)
    os.chmod(raw_file, 0o644)
    reg_file.write_text(json.dumps({
        'schema': 'researchkit.web-datasets/v1',
        'datasets': [{
            'dataset_id': 'shared-mnist-raw-v1',
            'root': str(raw_dir),
            'readers': [{'uid': teacher.uid, 'username': teacher.username}],
            'files': [{'path': 'mnist-train.idx', 'size': len(raw_bytes), 'sha256': raw_sha}],
        }],
    }), encoding='utf-8')
    os.chmod(reg_file, 0o644)

    policy = Policy(
        principals=MappingProxyType({teacher.uid: teacher}),
        workspaces=MappingProxyType({teacher.uid: ws_dir}),
        broker_uid=teacher.uid,
        broker_username=teacher.username,
        submission_store=store_dir,
        code_root=tmp_path / 'code-root',
        broker_socket=tmp_path / 'broker.sock',
        derived_dataset_root=derived_dir,
        dataset_registry=reg_file,
    )
    return policy, raw_file


@pytest.fixture
def purge_env(monkeypatch):
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    monkeypatch.setattr(web_broker, '_trusted_socket_route', lambda _path, _uid, require_socket: None)
    monkeypatch.setattr(web_datasets, '_root_owned', lambda info: info.st_uid in {0, os.geteuid()})
    cache_root = Path(__file__).resolve().parents[1] / '.pytest_cache'
    cache_root.mkdir(exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix='purge-test-', dir=cache_root))
    os.chmod(tmp_path, 0o700)
    teacher = _teacher_principal()
    policy, raw_file = _policy(tmp_path, teacher)
    server = web_broker.BrokerServer(policy, workers=2)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, kwargs={'install_signal_handler': False}, daemon=True)
    thread.start()
    client = web_broker.BrokerClient(server.socket_path, policy.broker_uid)
    # Browser execution is retired.  Project-purge fixtures create immutable
    # direct submissions; no runner identity or Web job is needed.
    runner = None
    ws = Workspace(teacher, policy.workspaces[teacher.uid], client, library_root=policy.code_root / 'library')
    try:
        yield server, client, ws, teacher, runner, policy, raw_file, tmp_path
    finally:
        server.close()
        thread.join(timeout=5)
        shutil.rmtree(tmp_path, ignore_errors=True)


def _prepare_and_submit(server, client, runner, project_id: str, files: dict[str, bytes], refs: list[dict[str, object]]):
    plan = client.request('preview_submission_versions', project_id=project_id, files=files, kind='major')
    selected = {item['alias']: item['suggested_version'] for item in plan['algorithms']}
    desired = apply_versions(files, plan, {'case_version': plan['case']['suggested_version'], 'algorithm_versions': selected})
    receipt = client.request(
        'prepare_submission', project_id=project_id, files=desired, data_references=refs, kind='major',
        case_version=plan['case']['suggested_version'],
        request_id=str(uuid.uuid4()), request_fingerprint='b' * 64,
    )
    assert receipt['status'] == 'succeeded'
    assert receipt['mode'] == 'direct'
    final = client.request('get_submission_preparation', preparation_id=receipt['preparation_id'])
    assert final['submission_id'] == receipt['submission_id']
    return client.request('get_submission', submission_id=receipt['submission_id'])


def _create_derived(store: SubmissionStore, root: Path, owner: Principal, name: str, sub_id: str) -> str:
    derived = DerivedDatasets(store, root)
    payload = b'sample_index,true_label,pred_label\n0,7,7\n'
    sha = hashlib.sha256(payload).hexdigest()
    up = derived.begin_upload(owner, name, len(payload), sha, [])
    derived.append_upload(owner, up['upload_id'], 0, payload)
    rec = derived.commit_upload(owner, up['upload_id'], sha)
    derived.publish(owner, rec['dataset_id'])
    with store._connection() as con:
        con.execute('UPDATE web_derived_datasets SET source_submission_id=? WHERE dataset_id=?', (sub_id, rec['dataset_id']))
    return str(rec['dataset_id'])


def test_full_chain_project_purge_and_republish(purge_env):
    server, client, ws, teacher, runner, policy, raw_file, _tmp = purge_env

    # 1. Create a baseline project & distribute it as a teacher task so we also test task claim reset.
    baseline_proj = client.request('create_project', name='rainfall-baseline')
    baseline_sub = _prepare_and_submit(server, client, runner, baseline_proj['project_id'], _project_files(algo_name='example.scale'), [])
    dist = client.request('create_distribution', submission_id=baseline_sub['id'], target_username=teacher.username)

    # 2. Student claims distribution into workspace and develops MNIST CNN.
    claimed = ws.claim_distribution(dist['distribution_id'])
    pid = str(claimed['project_id'])
    proj_dir = Path(claimed['path'])
    assert proj_dir.is_dir()

    # Record shared raw dataset + shared algorithm + produced algorithm usage.
    client.request('record_project_usage', project_id=pid, usage={
        'dataset_ids': ['shared-mnist-raw-v1'],
        'algorithm_refs': ['example.scale@1.0.0', 'mnist.cnn@1.0.0'],
        'template_refs': [],
    })

    raw_bytes = raw_file.read_bytes()
    sub1 = _prepare_and_submit(server, client, runner, pid, _project_files(algo_name='mnist.cnn', algo_version='1.0.0'), [
        {'dataset_id': 'shared-mnist-raw-v1', 'path': 'mnist-train.idx', 'size': len(raw_bytes), 'sha256': hashlib.sha256(raw_bytes).hexdigest()},
    ])
    sub1_id = str(sub1['id'])

    # 3. Create derived dataset from sub1, then teacher publishes mnist.cnn@1.0.0 and template.
    derived_id = _create_derived(server.store, policy.derived_dataset_root, teacher, 'mnist-pred-v1', sub1_id)
    assert any((policy.derived_dataset_root / 'blobs').glob('*'))

    pub_algo = client.request('review_publish', submission_id=sub1_id, kind='algorithm', aliases=['mnist_cnn'], expected_sha256=sub1['content_sha256'], tags=['测试'], description='测试算法')
    assert len(pub_algo['published']) == 1, pub_algo
    pub_tpl = client.request('review_publish', submission_id=sub1_id, kind='template', aliases=None, expected_sha256=sub1['content_sha256'], tags=['测试'], description='测试案例')
    assert len(pub_tpl['published']) == 1, pub_tpl
    tpl_ref = pub_tpl['published'][0]['ref']

    # 4. Preview project purge: must separate project scope, produced assets, and retained shared items.
    preview = ws.preview_project_purge(pid)
    assert preview['can_execute'] is True
    assert preview['project']['has_task_link'] is True
    assert preview['project']['workspace']['in_workspace'] is True
    produced_keys = {item['key'] for item in preview['produced_items']}
    assert f'template:{tpl_ref}' in produced_keys
    assert 'algorithm:mnist.cnn@1.0.0' in produced_keys
    assert f'derived_dataset:{derived_id}' in produced_keys

    # Shared items are strictly in retained_shared_items and NEVER in produced_items.
    shared_refs = {(item['kind'], item['ref']) for item in preview['retained_shared_items']}
    assert ('dataset', 'shared-mnist-raw-v1') in shared_refs
    assert ('algorithm', 'example.scale@1.0.0') in shared_refs

    # Execution order places template BEFORE algorithm BEFORE submissions/project.
    steps = [s['step'] for s in preview['execution_order']]
    assert steps.index('delete_template') < steps.index('delete_algorithm') < steps.index('delete_project')

    # Sibling dependency safety: selecting algorithm WITHOUT its produced template must be blocked!
    bad_preview = ws.preview_project_purge(pid, {'selected_keys': ['algorithm:mnist.cnn@1.0.0'], 'reset_task': True})
    assert bad_preview['can_execute'] is False
    with pytest.raises(KitError, match='阻挡'):
        ws.purge_project(pid, {
            'plan_token': bad_preview['plan_token'],
            'confirmation': _CONFIRMATION,
            'selected_keys': ['algorithm:mnist.cnn@1.0.0'],
            'reset_task': True,
        })

    # 5. Execute full-chain purge cleanly!
    receipt = ws.purge_project(pid, {
        'plan_token': preview['plan_token'],
        'confirmation': _CONFIRMATION,
        'selected_keys': [item['key'] for item in preview['produced_items'] if item['selected']],
        'reset_task': True,
    })
    assert receipt['status'] == 'completed'
    # Management purge clears central records only; personal workspace dir remains until deleted locally.
    assert proj_dir.exists()
    assert raw_file.exists()  # Borrowed shared dataset file untouched!
    listed = ws.list_projects(include_archived=True)
    assert any(p['project_id'] == pid and p.get('central_purged') for p in listed)
    assert not list((policy.derived_dataset_root / 'blobs').glob('*'))

    del_res = ws.delete_personal_project(pid, '确认删除本地副本')
    assert del_res['deleted_local'] is True
    assert not proj_dir.exists()
    assert not any(p['project_id'] == pid for p in ws.list_projects(include_archived=True))

    # Distribution reset back to pending!
    dists = client.request('list_distributions')
    drow = next(d for d in dists if (d.get('distribution_id') or d.get('id')) == dist['distribution_id'])
    assert drow['status'] == 'pending'

    # Audit log recorded!
    audits = ws.list_project_purge_audit()
    assert len(audits) == 1
    assert audits[0]['project_id'] == pid
    assert any(item['kind'] == 'project_record' for item in audits[0]['deleted'])

    # Version freed: another project can publish mnist.cnn@1.0.0 again!
    p2 = ws.create_project('mnist-republish')
    sub2 = _prepare_and_submit(server, client, runner, p2['project_id'], _project_files(algo_name='mnist.cnn', algo_version='1.0.0'), [])
    repub = client.request('review_publish', submission_id=sub2['id'], kind='algorithm', aliases=['mnist_cnn'], expected_sha256=sub2['content_sha256'], tags=['测试'], description='测试算法')
    assert repub['published'][0]['ref'] == 'mnist.cnn@1.0.0'


def test_external_reference_blocks_deleting_derived_dataset(purge_env):
    server, client, ws, teacher, runner, policy, _raw_file, _tmp = purge_env

    p1 = ws.create_project('producer-proj')
    sub1 = _prepare_and_submit(server, client, runner, p1['project_id'], _project_files(algo_name='custom.p1'), [])
    derived_id = _create_derived(server.store, policy.derived_dataset_root, teacher, 'shared-derived-v1', sub1['id'])

    # Second project attaches that derived dataset!
    p2 = ws.create_project('consumer-proj')
    client.request('record_project_usage', project_id=p2['project_id'], usage={
        'dataset_ids': [derived_id],
        'algorithm_refs': [],
        'template_refs': [],
    })

    preview = ws.preview_project_purge(p1['project_id'])
    ditem = next(i for i in preview['produced_items'] if i['ref'] == derived_id)
    assert ditem['can_delete'] is False
    assert ditem['selected'] is False
    assert preview['can_execute'] is True

    receipt = ws.purge_project(p1['project_id'], {
        'plan_token': preview['plan_token'],
        'confirmation': _CONFIRMATION,
        'selected_keys': [],
        'reset_task': True,
    })
    assert receipt['status'] == 'completed'
    assert any(r['ref'] == derived_id for r in receipt['retained'])
    assert len(list((policy.derived_dataset_root / 'blobs').glob('*'))) == 1


def test_cli_rkweb_purge(purge_env):
    server, client, ws, teacher, runner, policy, _raw_file, tmp_path = purge_env

    p = ws.create_project('cli-purge-proj')
    pid = p['project_id']
    _prepare_and_submit(server, client, runner, pid, _project_files(algo_name='cli.algo'), [])

    policy_file = tmp_path / 'dev-policy.json'
    policy_file.write_text(json.dumps({
        'schema': 'researchkit.web-policy/v1',
        'principals': [{'uid': teacher.uid, 'username': teacher.username, 'role': 'teacher', 'workspace': str(policy.workspaces[teacher.uid])}],
        'broker': {'uid': 981, 'username': 'rk-broker-test'},
        'submission_store': str(policy.submission_store),
        'code_root': str(policy.code_root),
        'broker_socket': str(policy.broker_socket),
        'derived_dataset_root': str(policy.derived_dataset_root),
    }), encoding='utf-8')

    tool = Path(__file__).resolve().parents[1] / 'tools' / 'rkweb-purge.py'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'core')}
    prev_out = subprocess.check_output(
        [sys.executable, str(tool), '--policy', str(policy_file), '--dev-policy', '--preview', '--project', pid, '--json'],
        env=env, text=True,
    )
    prev = json.loads(prev_out)
    assert prev['can_execute'] is True

    apply_out = subprocess.check_output(
        [sys.executable, str(tool), '--policy', str(policy_file), '--dev-policy', '--apply',
         '--project', pid, '--plan-token', prev['plan_token'], '--delete-produced', '--remove-workspace', '--backup', '--confirm'],
        env=env, text=True,
    )
    receipt = json.loads(apply_out)
    assert receipt['status'] == 'completed'
    assert list((policy.submission_store / 'backups').glob('submissions-before-purge-*.sqlite3'))
    assert not ws.list_projects(include_archived=True)
