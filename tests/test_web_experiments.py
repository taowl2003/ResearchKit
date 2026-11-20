"""Experiment scheme and IDE-result catalogues use a real isolated workspace."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_experiments import delete_result, save, summarize_snapshot
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
from researchkit.projects import seal_lock
from researchkit import model


ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location('build_three_cases', ROOT / 'tools' / 'build-three-cases.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_workspace():
    root = Path(tempfile.mkdtemp(prefix='.rk-experiments-', dir=Path.cwd()))
    os.chmod(root, 0o700)
    principal = Principal(os.getuid(), 'experiment-owner', 'student')
    store = SubmissionStore(root / 'store')
    workspace = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        project_dir = workspace.projects_root / 'rainfall'
        _builder().build_project('rainfall', project_dir)
        os.chmod(project_dir, 0o700)
        project = workspace.register_existing('rainfall', 'projects/rainfall')
        yield root, store, workspace, project
    finally:
        shutil.rmtree(root)


def _run_rainfall(project: dict) -> Path:
    path = Path(project['path'])
    result = subprocess.run(
        [str(ROOT / '.web-venv' / 'bin' / 'python'), 'run_experiment.py', '--scheme', 'factor-2'],
        cwd=path, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return path


def test_real_ide_result_catalogue_save_cas_delete_and_snapshot_filtering(real_workspace):
    _root, store, workspace, project = real_workspace
    path = _run_rainfall(project)
    pid = project['project_id']

    # An unreadable record and a deliberately unsafe sibling must be warnings;
    # the completed IDE batch remains visible.
    bad = path / 'results' / 'factor-2' / 'bad-record'
    bad.mkdir(parents=True)
    (bad / 'experiment.json').write_text('{ not JSON', encoding='utf-8')
    unsafe = path / 'results' / 'factor-2' / 'unsafe-batch'
    unsafe.mkdir()
    os.chmod(unsafe, 0o777)
    try:
        before = workspace.experiment_catalog(pid)
    finally:
        os.chmod(unsafe, 0o700)
    assert before['has_runner'] is True
    assert any(row['id'] == 'factor-2' for row in before['schemes'])
    run = next(row for row in before['runs'] if row['batch_id'] != 'bad-record')
    assert run['record']['status'] == 'succeeded'
    assert any('bad-record' in warning for warning in before['warnings'])
    assert any('unsafe-batch' in warning for warning in before['warnings'])
    _root_path, before_large, _references = workspace._snapshot(pid)
    assert any(name.startswith('results/factor-2/') for name in before_large)

    document = dict(next(row['document'] for row in before['schemes'] if row['id'] == 'factor-2'))
    document['name'] = 'web-copy'
    created = save(workspace, pid, {'id': 'web-copy', 'document': document, 'expected_sha256': None})
    assert created['path'] == 'experiments/web-copy.yaml'
    with pytest.raises(KitError, match='修改'):
        save(workspace, pid, {'id': 'web-copy', 'document': document, 'expected_sha256': '0' * 64})
    recursive = dict(document)
    recursive['case_parameters'] = value = {}
    value['again'] = value
    with pytest.raises(KitError, match='循环引用'):
        save(workspace, pid, {'id': 'recursive', 'document': recursive, 'expected_sha256': None})
    non_finite = dict(document)
    non_finite['case_parameters'] = {'bad': float('nan')}
    with pytest.raises(KitError, match='NaN'):
        save(workspace, pid, {'id': 'non-finite', 'document': non_finite, 'expected_sha256': None})

    # A large IDE artifact is catalogued as metadata.  Selecting a small
    # artifact excludes the large file before snapshot byte limits are applied.
    large = path / 'results' / 'factor-2' / run['batch_id'] / 'large.bin'
    large.write_bytes(b'x' * (16 * 1024 * 1024 + 1))
    refreshed = workspace.experiment_catalog(pid)
    current = next(row for row in refreshed['runs'] if row['batch_id'] == run['batch_id'])
    report = next(row['path'] for row in current['files'] if row['path'].endswith('report.md'))
    _root_path, selected, _references = workspace._snapshot(pid, result_paths=[report])
    assert report in selected and all(not name.endswith('large.bin') for name in selected)
    assert 'case.py' in selected and 'experiments/factor-2.yaml' in selected
    with pytest.raises(KitError, match='单个文件大小'):
        workspace._snapshot(pid)

    # IDE edits after cataloguing invalidate a result deletion; a fresh record
    # can be removed, while deleting a scheme retains all past batches.
    (path / report).write_text('changed by IDE\n', encoding='utf-8')
    with pytest.raises(KitError, match='修改'):
        delete_result(workspace, pid, {'scheme_id': 'factor-2', 'batch_id': run['batch_id'],
                                       'confirmation': f"factor-2/{run['batch_id']}",
                                       'expected_revision': current['revision']})
    current = next(row for row in workspace.experiment_catalog(pid)['runs'] if row['batch_id'] == run['batch_id'])
    deleted = delete_result(workspace, pid, {'scheme_id': 'factor-2', 'batch_id': run['batch_id'],
                                              'confirmation': f"factor-2/{run['batch_id']}",
                                              'expected_revision': current['revision']})
    assert deleted['deleted'] is True
    original = next(row for row in workspace.experiment_catalog(pid)['schemes'] if row['id'] == 'factor-2')
    removed = workspace.delete_experiment(pid, {'id': 'factor-2', 'expected_sha256': original['sha256']})
    assert removed['results_retained'] is True
    assert (path / 'results' / 'factor-2').exists()

    foreign = store.create_project(Principal(os.getuid() + 71, 'other-owner', 'student'), 'foreign')
    with pytest.raises(KitError, match='项目|工作区|无权'):
        workspace.experiment_catalog(foreign['project_id'])


def test_snapshot_summary_keeps_legacy_results_without_the_optional_runner(real_workspace):
    _root, _store, workspace, project = real_workspace
    path = Path(project['path'])
    (path / 'run_experiment.py').unlink()
    legacy = path / 'results' / 'legacy' / 'one'
    legacy.mkdir(parents=True)
    record = {'schema': 'researchkit.experiment-result/v1', 'scheme_id': 'legacy', 'batch_id': 'one',
              'status': 'succeeded'}
    (legacy / 'experiment.json').write_text(json.dumps(record), encoding='utf-8')
    (legacy / 'notes.txt').write_text('old result', encoding='utf-8')
    listed = workspace.experiment_catalog(project['project_id'])
    assert listed['has_runner'] is False
    assert next(row for row in listed['runs'] if row['scheme_id'] == 'legacy')['record']['status'] == 'succeeded'

    summary = summarize_snapshot({
        'project.yaml': (path / 'project.yaml').read_bytes(),
        'results/legacy/one/experiment.json': json.dumps(record).encode(),
        'results/legacy/one/huge.bin': b'x' * (16 * 1024 * 1024 + 1),
    })
    assert summary['has_runner'] is False
    assert summary['runs'][0]['batch_id'] == 'one'
    assert summary['result_files'][-1]['size'] > 16 * 1024 * 1024


def test_catalogue_refuses_to_remove_an_active_ide_batch(real_workspace):
    _root, _store, workspace, project = real_workspace
    path = Path(project['path'])
    batch = path / 'results' / 'active' / 'still-running'
    batch.mkdir(parents=True)
    (batch / 'experiment.json').write_text(json.dumps({
        'schema': 'researchkit.experiment-result/v1', 'scheme_id': 'active',
        'batch_id': 'still-running', 'status': 'running',
    }), encoding='utf-8')
    row = next(item for item in workspace.experiment_catalog(project['project_id'])['runs']
               if item['scheme_id'] == 'active')
    with pytest.raises(KitError, match='运行|活跃'):
        delete_result(workspace, project['project_id'], {
            'scheme_id': 'active', 'batch_id': 'still-running',
            'confirmation': 'active/still-running', 'expected_revision': row['revision'],
        })


def test_enable_preserves_a_legacy_case_and_installs_one_ide_adapter(real_workspace):
    _root, _store, workspace, _rainfall = real_workspace
    legacy = workspace.create_project('legacy main entry')
    pid, path = legacy['project_id'], Path(legacy['path'])
    case = b'def run(context):\n    return {"legacy": True}\n'
    (path / 'case.py').write_bytes(case)
    (path / 'experiments').mkdir()
    (path / 'experiments' / 'baseline.yaml').write_text(
        'schema: researchkit.experiment/v1\nname: baseline\ndescription: old project\n'
        'case_parameters: {}\nalgorithm_parameters: {}\n', encoding='utf-8')
    retained = path / 'experiments' / 'researcher' / 'notes.txt'
    retained.parent.mkdir()
    retained.write_text('do not overwrite', encoding='utf-8')
    seal_lock(path)
    lock_before = (path / '.researchkit' / 'lock.json').read_bytes()

    enabled = workspace.enable_experiments(pid)
    assert set(enabled['created']) == {'experiment_support.py', 'run_experiment.py'}
    assert (path / 'case.py').read_bytes() == case
    assert retained.read_text(encoding='utf-8') == 'do not overwrite'
    assert (path / '.researchkit' / 'lock.json').read_bytes() == lock_before
    model.verify_lock(path)
    helper_before = (path / 'experiment_support.py').read_bytes()
    launcher_before = (path / 'run_experiment.py').read_bytes()
    assert workspace.enable_experiments(pid) == {'enabled': True, 'created': []}
    assert (path / 'experiment_support.py').read_bytes() == helper_before
    assert (path / 'run_experiment.py').read_bytes() == launcher_before

    run = subprocess.run([str(ROOT / '.web-venv' / 'bin' / 'python'), 'run_experiment.py'], cwd=path,
                         text=True, capture_output=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    records = list((path / 'results' / 'baseline').glob('*/experiment.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding='utf-8'))['summary'] == {'legacy': True}

    partial = workspace.create_project('partial experiment entry')
    partial_path = Path(partial['path'])
    (partial_path / 'run_experiment.py').write_text('# researcher launcher\n', encoding='utf-8')
    with pytest.raises(KitError, match='部分实验入口'):
        workspace.enable_experiments(partial['project_id'])
    assert (partial_path / 'run_experiment.py').read_text(encoding='utf-8') == '# researcher launcher\n'
    assert not (partial_path / 'experiment_support.py').exists()


def test_ide_optional_fields_and_result_walk_limits(real_workspace):
    from researchkit.web_experiments import _walk_result_tree
    _root, _store, workspace, project = real_workspace
    path, pid = Path(project['path']), project['project_id']
    (path / 'experiments' / 'minimal.yaml').write_text(
        'schema: researchkit.experiment/v1\nname: Minimal IDE scheme\n')
    schemes = workspace.experiment_catalog(pid)['schemes']
    minimal = next(row for row in schemes if row['id'] == 'minimal')
    assert minimal['document']['description'] == ''
    assert minimal['document']['case_parameters'] == {}
    root = path / 'results' / 'deep' / 'batch'
    root.mkdir(parents=True)
    deep = root
    for _ in range(34):
        deep = deep / 'nested'
        deep.mkdir()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(KitError, match='层级'):
            _walk_result_tree(descriptor, 'results/deep/batch')
        with pytest.raises(KitError, match='条目'):
            _walk_result_tree(descriptor, 'results/deep/batch', budget=[1])
    finally:
        os.close(descriptor)
    listed = workspace.experiment_catalog(pid)
    assert any('层级' in warning for warning in listed['warnings'])
    assert any(row['id'] == 'minimal' for row in listed['schemes'])


def test_explicit_standard_helper_update_preserves_user_files_and_fixed_results(real_workspace):
    _root, store, workspace, project = real_workspace
    path, pid = Path(project['path']), project['project_id']
    old_helper = (ROOT/'tests/fixtures/experiment-support-history.py').read_bytes()
    (path/'experiment_support.py').write_bytes(old_helper)
    _run_rainfall(project); _run_rainfall(project)
    before = workspace.experiment_catalog(pid)
    assert before['helper_update_available'] and before['result_retention'] == 'history'
    assert len(before['runs']) == 2
    retained = {name:(path/name).read_bytes() for name in ['case.py','project.yaml','experiments/factor-2.yaml',
        '.researchkit/lock.json','run_experiment.py']}
    _root_path, files, references = workspace._snapshot(pid)
    fixed = store.submit(pid, workspace.principal, 'minor', files, references)
    assert not any(name.startswith('_runs/') for name in files)
    assert workspace.enable_experiments(pid)['created'] == []
    assert (path/'experiment_support.py').read_bytes() == old_helper
    assert workspace.update_experiment_helper(pid) == {'updated':True}
    assert workspace.update_experiment_helper(pid) == {'updated':False}
    assert all((path/name).read_bytes()==content for name,content in retained.items())
    assert len(workspace.experiment_catalog(pid)['runs']) == 2, '更新入口本身不删除结果'
    _run_rainfall(project)
    assert len(workspace.experiment_catalog(pid)['runs']) == 1
    assert store.get_submission(fixed['id'],workspace.principal)['files'] == files
    assert workspace.experiment_catalog(pid)['result_retention'] == 'latest-success'
    custom = b'# my custom runner\n'+(path/'experiment_support.py').read_bytes()
    (path/'experiment_support.py').write_bytes(custom)
    assert not workspace.experiment_catalog(pid)['helper_update_available']
    with pytest.raises(KitError, match='自定义'):
        workspace.update_experiment_helper(pid)
    assert (path/'experiment_support.py').read_bytes()==custom


def test_retention_does_not_delete_managed_dataset_or_break_missing_import_source(real_workspace):
    import hashlib
    _root, _store, workspace, project = real_workspace
    path = _run_rainfall(project); pid = project['project_id']
    run, = workspace.experiment_catalog(pid)['runs']
    relative = f"results/factor-2/{run['batch_id']}/processed_rainfall.csv"
    raw = (path/relative).read_bytes()
    # Managed data are independent byte copies; mimic the local import receipt
    # and authorized catalogue without depending on a production data service.
    managed = _root/'managed-copy.blob'; managed.write_bytes(raw)
    reference = {'dataset_id':'retained-derived','path':'data.bin','size':len(raw),
                 'sha256':hashlib.sha256(raw).hexdigest()}
    previous = workspace.store.request
    workspace.store.request = lambda method, **params: ([{'dataset_id':'retained-derived','files':[reference]}]
        if method=='get_dataset_catalog' else previous(method,**params))
    workspace.add_project_datasets(pid,['retained-derived'],None)
    fd = workspace._open_project_fd(pid)
    try:
        assert workspace._remember_derived_import(fd,relative,reference)
    finally:
        os.close(fd)
    _run_rainfall(project)
    assert not (path/relative).exists()
    assert managed.read_bytes()==raw
    _root_path, snapshot, references = workspace._snapshot(pid)
    assert references == [reference]
    assert 'DATA-REFERENCES.json' in snapshot
