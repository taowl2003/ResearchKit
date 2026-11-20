"""Canonical IDE examples -> fixed small artifacts -> manual reusable assets.

No live platform, shared dataset changes, Web worker, or substitute CNN source.
The genuine MNIST training implementation is tested in test_example_mnist.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from types import MappingProxyType
import uuid

import pytest
import yaml

from researchkit.web_broker import BrokerServer, _encode_files, _decode_files
from researchkit.web_identity import Policy, Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('canonical_e2e_builder', ROOT / 'tools/build-three-cases.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DirectBroker:
    def __init__(self, server, principal):
        self.server, self.principal = server, principal

    def request(self, method, **params):
        if 'files' in params:
            params['files'] = _encode_files(params['files'])
        value = self.server._dispatch(self.principal, {'method': method, 'params': params})
        if method in {'get_submission', 'review_submission'}:
            value['files'] = _decode_files(value['files'])
        return value


@pytest.mark.parametrize('case,scheme', [('rainfall', 'factor-2'),
                                        ('acoustic-tdoa', 'sound-speed-1480'),
                                        ('mnist-cnn', 'baseline')])
def test_canonical_ide_project_direct_submission_and_manual_publication(case, scheme):
    username = pwd.getpwuid(os.getuid()).pw_name
    if username != 'ly':
        pytest.skip('broker policy recipient resolution uses the ly test account')
    principal = Principal(os.getuid(), username, 'teacher')
    with tempfile.TemporaryDirectory(prefix='.rk-canonical-e2e-', dir=ROOT) as temp:
        root = Path(temp)
        store = SubmissionStore(root / 'store')
        server = object.__new__(BrokerServer)
        server.store = store
        server.policy = Policy(principals=MappingProxyType({principal.uid: principal}),
            workspaces=MappingProxyType({}), broker_uid=principal.uid, broker_username=username,
            submission_store=store.directory, code_root=ROOT)
        client = DirectBroker(server, principal)
        workspace = Workspace(principal, root / 'workspace', client)
        source = builder.build_project(case, workspace.projects_root / case)
        source.chmod(0o700)
        project = workspace.register_existing(case, 'projects/' + case)
        path, pid = Path(project['path']), project['project_id']
        if case == 'mnist-cnn':
            pytest.importorskip('torch')
            from test_example_mnist import _mnist_files
            for relative, raw in _mnist_files().items():
                target = path / 'tiny-mnist' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            config = path / 'experiments' / 'baseline.yaml'
            document = yaml.safe_load(config.read_text())
            document['case_parameters'].update(data_source='local', data_root=str(path / 'tiny-mnist'), dataset_id='')
            document['algorithm_parameters']['cnn'].update(train_limit=30, test_limit=20, epochs=1,
                batch_size=10, num_workers=0, device='cpu')
            config.write_text(yaml.safe_dump(document))
        run = subprocess.run([sys.executable, str(path / 'run_experiment.py'), '--scheme', scheme],
            cwd=root, capture_output=True, text=True, timeout=90,
            env={**os.environ, 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'})
        assert run.returncode == 0, run.stderr
        results = workspace.experiment_catalog(pid)
        assert len(results['runs']) == 1
        assert results['runs'][0]['record']['status'] == 'succeeded'
        selected = [row['path'] for row in results['result_files']
                    if Path(row['path']).name in {'parameters.json', 'experiment.json', 'summary.json'}]
        assert len(selected) >= 2
        plan = workspace.submission_preview(pid, {'kind': 'minor', 'result_paths': selected})
        receipt = workspace.submission_prepare(pid, {'kind': 'minor', 'result_paths': selected,
            'request_id': str(uuid.uuid4()), 'expected_revision': plan['snapshot_revision'],
            'case_version': plan['case']['suggested_version'],
            'algorithm_versions': {row['alias']: row['suggested_version'] for row in plan['algorithms']}})
        assert receipt['status'] == 'succeeded'
        fixed = client.request('get_submission', submission_id=receipt['submission_id'])
        assert {p for p in fixed['files'] if p.startswith('results/')} == set(selected)
        summary = workspace.submission_experiments(receipt['submission_id'])
        assert summary['runs'][0]['record']['status'] == 'succeeded'
        review = client.request('review_release', submission_id=receipt['submission_id'])
        assert review['review_mode'] == 'manual' and 'evidence' not in review
        published = client.request('review_publish_batch', submission_id=receipt['submission_id'],
            expected_sha256=fixed['content_sha256'], items=[{
                'kind': 'template', 'name': f'canonical.{case}', 'version': '1.0.0',
                'description': 'IDE example manually reviewed with fixed parameters and artifacts',
                'tags': ['教学', case]}])
        assert published['ok'] is True, published
        with store._connection() as connection:
            assert connection.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0
