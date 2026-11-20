"""A user can start from an empty Web library without hidden shared resources."""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
from researchkit.web_workspace_materials import add_algorithms


SKELETON = 'def run(inputs, parameters, context):\n    # 在此编写本地算法逻辑\n    return {}\n'


def _principal() -> Principal:
    username = pwd.getpwuid(os.geteuid()).pw_name
    return Principal(os.geteuid(), username, 'teacher' if username == 'ly' else 'student')


@pytest.fixture
def cold_workspace(monkeypatch: pytest.MonkeyPatch):
    """Keep both the broker store and the central resource library empty."""
    # pytest creates a per-test parent directory that is intentionally not a
    # service-owned store ancestor.  The Web store's production ancestor check
    # is covered elsewhere; it is unrelated to this isolated cold-start flow.
    monkeypatch.setattr(SubmissionStore, '_verify_ancestors', lambda self: None)
    root = Path(tempfile.mkdtemp(prefix='.rk-cold-start-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    library = root / 'empty-library'
    library.mkdir(mode=0o700)
    store = SubmissionStore(root / 'store')
    principal = _principal()
    client = LocalStoreClient(store, principal, library_root=library)
    workspace = Workspace(principal, root / 'workspace', client, library_root=library)
    try:
        yield workspace, store
    finally:
        shutil.rmtree(root)


def test_cold_start_local_mapping_algorithm_runs_without_shared_library(cold_workspace) -> None:
    workspace, store = cold_workspace
    project = workspace.create_project('从零开始')
    project_id = str(project['project_id'])
    root = Path(project['path'])

    # Leaving plugin_py blank represents the empty Web creation form.  Its
    # runnable implementation is written only after adding it to this project.
    local = workspace.create_local_algorithm({
        'name': 'starter.summary',
        'version': '1.0.0',
        'description': '空平台库中的本地示例算法',
        'outputs': {'summary': 'mapping@1.0.0'},
        'parameters': {'answer': {'type': 'integer', 'default': 42}},
    })
    local_root = workspace.root / '.researchkit-local-algorithms' / str(local['id'])
    assert (local_root / 'plugin.py').read_text(encoding='utf-8') == SKELETON
    assert (local_root / 'contracts/mapping/1.0.0.yaml').read_text(encoding='utf-8').splitlines()[:3] == [
        'schema: hydro.contract/v1', 'id: mapping@1.0.0', 'type: mapping',
    ]

    bound = add_algorithms(workspace, project_id, {
        'local_algorithm_id': local['id'], 'alias': 'starter', 'bind': True,
    })
    assert bound['kind'] == 'local'
    assert (root / '.researchkit/contracts/mapping/1.0.0.yaml').is_file()
    plugin = workspace.read_text(project_id, 'algorithms/starter/plugin.py')
    assert plugin['text'] == SKELETON
    workspace.save_text(
        project_id,
        'algorithms/starter/plugin.py',
        'def run(inputs, parameters, context):\n'
        '    return {"summary": {"answer": parameters["answer"]}}\n',
        str(plugin['sha256']),
    )

    opened = workspace.read_text(project_id, 'case.py')
    workspace.save_text(
        project_id,
        'case.py',
        'def run(context):\n'
        '    summary = context.call("starter", inputs={})["summary"]\n'
        '    context.atomic_write_json(context.result_path("summary.json"), summary)\n'
        '    return summary\n',
        str(opened['sha256']),
    )
    workspace.enable_experiments(project_id)
    workspace.save_experiment(project_id, {
        'id': 'baseline',
        'expected_sha256': None,
        'document': {
            'schema': 'researchkit.experiment/v1',
            'name': '基线方案',
            'description': '由 IDE 在用户自己的 Python 中运行',
            'case_parameters': {},
            'algorithm_parameters': {'starter': {'answer': 7}},
        },
    })

    completed = subprocess.run(
        [sys.executable, 'run_experiment.py', '--scheme', 'baseline'],
        cwd=root, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = sorted((root / 'results/baseline').glob('*/experiment.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['status'] == 'succeeded'
    assert record['summary'] == {'answer': 7}
    assert json.loads((records[0].parent / 'summary.json').read_text(encoding='utf-8')) == {'answer': 7}
    assert workspace.experiment_catalog(project_id)['runs'][0]['record']['summary'] == {'answer': 7}
    with store._connection() as con:
        jobs_table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='web_jobs'"
        ).fetchone()
        assert jobs_table is None or con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0


def test_cold_start_never_forges_unknown_contracts(cold_workspace) -> None:
    workspace, _store = cold_workspace
    project = workspace.create_project('未知契约')
    local = workspace.create_local_algorithm({
        'name': 'needs.review', 'version': '1.0.0',
        'outputs': {'summary': 'review.private@1.0.0'},
    })
    before = workspace._snapshot(str(project['project_id']))[1]
    with pytest.raises(KitError, match='缺少契约|契约'):
        add_algorithms(workspace, str(project['project_id']), {
            'local_algorithm_id': local['id'], 'alias': 'review', 'bind': True,
        })
    assert not (Path(project['path']) / '.researchkit/contracts/review.private/1.0.0.yaml').exists()
    assert workspace._snapshot(str(project['project_id']))[1] == before
