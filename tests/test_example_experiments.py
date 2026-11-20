"""Real project/runtime tests for the standalone IDE experiment interface."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from researchkit import model
from researchkit.web_packages import pack_package, unpack_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_three_cases_for_test', ROOT / 'tools/build-three-cases.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def rainfall(tmp_path):
    return builder.build_project('rainfall', tmp_path / 'rainfall')


def run(project, *args, cwd=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # The copied runtime must work without the source repository on PYTHONPATH.
    env.pop('PYTHONPATH', None)
    return subprocess.run([sys.executable, str(project / 'run_experiment.py'), *args],
                          cwd=cwd or project, env=env, text=True, capture_output=True, timeout=60)


def records(project, scheme='factor-2'):
    return [json.loads(path.read_text()) for path in sorted((project / 'results' / scheme).glob('*/experiment.json'))]


def test_copied_runtime_runs_from_other_working_directory_without_changing_project(rainfall, tmp_path):
    original = (rainfall / 'project.yaml').read_bytes()
    config = (rainfall / 'experiments/factor-2.yaml').read_bytes()
    result = run(rainfall, '--scheme', 'factor-2', cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    model.verify_lock(rainfall)
    assert (rainfall / 'project.yaml').read_bytes() == original
    assert (rainfall / 'experiments/factor-2.yaml').read_bytes() == config
    record, = records(rainfall)
    assert record['status'] == 'succeeded'
    assert record['execution'] == 'user-environment'
    assert record['summary']['scaled_total'] == 20
    batch = rainfall / 'results/factor-2' / record['batch_id']
    assert json.loads((batch / 'parameters.json').read_text())['algorithms']['scale']['factor'] == 2
    assert (batch / 'summary.json').is_file()
    assert (batch / 'report.md').is_file()
    assert not list(tmp_path.glob('results/*'))


def test_repeated_runs_replace_old_results_only_after_success(rainfall):
    assert run(rainfall).returncode == 0
    first, = records(rainfall)
    scheme_path = rainfall / 'experiments/factor-2.yaml'
    scheme = yaml.safe_load(scheme_path.read_text())
    scheme['algorithm_parameters']['scale']['factor'] = 3
    scheme_path.write_text(yaml.safe_dump(scheme, allow_unicode=True))
    assert run(rainfall).returncode == 0
    result = records(rainfall)
    assert len(result) == 1 and result[0]['batch_id'] != first['batch_id']
    assert result[0]['summary']['scaled_total'] == 30
    assert not (rainfall / 'results/factor-2' / first['batch_id']).exists()
    assert result[0]['parameters']['algorithms']['scale']['factor'] == 3


def test_unknown_alias_parameter_and_unsafe_scheme_are_not_silently_ignored(rainfall):
    path = rainfall / 'experiments/factor-2.yaml'
    doc = yaml.safe_load(path.read_text())
    doc['algorithm_parameters']['not_a_plugin'] = {}
    path.write_text(yaml.safe_dump(doc))
    result = run(rainfall)
    assert result.returncode == 1 and '未加入项目的算法别名' in result.stderr
    doc['algorithm_parameters'].pop('not_a_plugin')
    doc['algorithm_parameters']['scale']['typo_factor'] = 2
    path.write_text(yaml.safe_dump(doc))
    result = run(rainfall)
    assert result.returncode == 1 and '未声明参数' in result.stderr
    assert not records(rainfall)
    result = run(rainfall, '--scheme', '../project')
    assert result.returncode == 1 and '方案名' in result.stderr


def test_failure_keeps_parameters_and_error_without_accumulating_partial_artifacts(rainfall):
    case = rainfall / 'case.py'
    case.write_text('''from experiment_support import run_experiment
def execute(context):
    context.atomic_write_text(context.result_path('partial.txt'), 'started')
    raise ValueError('expected failure')
def run(context):
    return run_experiment(context, execute)
''')
    result = run(rainfall)
    assert result.returncode == 1
    record, = records(rainfall)
    assert record['status'] == 'failed' and 'expected failure' in record['error']
    batch = rainfall / 'results/factor-2' / record['batch_id']
    assert (batch / 'parameters.json').is_file()
    assert not (batch / 'partial.txt').exists()
    assert 'summary' not in record


def test_failure_preserves_previous_success_and_recovers_without_extra_batches(rainfall):
    assert run(rainfall).returncode == 0
    first, = records(rainfall)
    good_dir = rainfall / 'results/factor-2' / first['batch_id']
    good_bytes = (good_dir / 'summary.json').read_bytes()
    original_case = (rainfall / 'case.py').read_text()
    (rainfall / 'case.py').write_text('''def run(context):
    context.atomic_write_text(context.result_path('large.tmp'), 'partial')
    raise ValueError('expected failure')
''')
    assert run(rainfall).returncode == 1
    assert run(rainfall).returncode == 1
    rows = records(rainfall)
    assert len(rows) == 2
    assert (good_dir / 'summary.json').read_bytes() == good_bytes
    failed, = [row for row in rows if row['status'] == 'failed']
    assert set(p.name for p in (rainfall / 'results/factor-2' / failed['batch_id']).iterdir()) == {
        'experiment.json', 'parameters.json', 'report.md'}
    (rainfall / 'case.py').write_text(original_case)
    assert run(rainfall).returncode == 0
    row, = records(rainfall)
    assert row['status'] == 'succeeded' and not good_dir.exists()


def test_compaction_preserves_unknown_running_and_unsafe_trees(rainfall, tmp_path):
    assert run(rainfall).returncode == 0
    first, = records(rainfall)
    first_dir = rainfall / 'results/factor-2' / first['batch_id']
    # Recognized old batches are removed, but the cleanup never follows a link
    # or treats an unrecognized directory as disposable user data.
    link = first_dir / 'external-link'
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'keep').write_text('safe')
    link.symlink_to(outside, target_is_directory=True)
    assert run(rainfall).returncode == 0
    assert first_dir.exists() and (outside / 'keep').read_text() == 'safe'
    link.unlink()
    hard = first_dir / 'hard-link'
    os.link(outside/'keep', hard)
    assert run(rainfall, '--compact').returncode == 0
    assert first_dir.exists() and (outside/'keep').read_text() == 'safe'
    hard.unlink()
    unknown = rainfall / 'results/factor-2/research-notes'; unknown.mkdir()
    (unknown / 'keep.txt').write_text('notes')
    active = rainfall / 'results/factor-2/active'; active.mkdir()
    (active / 'experiment.json').write_text(json.dumps({'schema':'researchkit.experiment-result/v1',
        'scheme_id':'factor-2', 'batch_id':'active', 'status':'running'}))
    assert run(rainfall, '--compact').returncode == 0
    assert not first_dir.exists()
    assert (unknown / 'keep.txt').read_text() == 'notes'
    assert active.exists()


def test_same_scheme_cannot_run_while_its_lock_is_held(rainfall):
    import importlib.util
    spec = importlib.util.spec_from_file_location('retention_helper', ROOT/'core/researchkit/experiment_support.py')
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    assert run(rainfall).returncode == 0
    previous, = records(rainfall)
    with helper._scheme_lock(rainfall, 'factor-2'):
        result = run(rainfall, '--scheme', 'factor-2')
        assert result.returncode == 1 and '正在运行或整理' in result.stderr
        # A different scheme is independent, including its output retention.
        assert run(rainfall, '--scheme', 'factor-3').returncode == 0
    assert records(rainfall) == [previous]


def test_examples_remain_valid_plain_web_import_packages(tmp_path):
    for name in builder.CASES:
        project = builder.build_project(name, tmp_path / name)
        model.project(project)
        model.verify_lock(project)
        files = {path.relative_to(project).as_posix(): path.read_bytes()
                 for path in project.rglob('*') if path.is_file()}
        decoded, bundle = unpack_package(pack_package(files))
        assert bundle is None and decoded == files
        assert not (project / 'DATA-REFERENCES.json').exists()
        assert not list(project.rglob('*.pt')), '未运行的分发包不能携带训练模型'


def test_comparison_uses_latest_successful_batch_and_saves_parameters(rainfall):
    first = rainfall / 'experiments/factor-2.yaml'
    doc = yaml.safe_load(first.read_text())
    doc['name'] = '三倍对照'
    doc['algorithm_parameters']['scale']['factor'] = 3
    (rainfall / 'experiments/triple.yaml').write_text(yaml.safe_dump(doc, allow_unicode=True))
    assert run(rainfall, '--scheme', 'factor-2').returncode == 0
    assert run(rainfall, '--scheme', 'triple').returncode == 0
    result = run(rainfall, '--compare', 'factor-2', 'triple')
    assert result.returncode == 0, result.stderr
    assert 'scaled_total' in result.stdout and 'factor' in result.stdout
    table, = (rainfall / 'results').glob('comparison-*.csv')
    assert 'parameter.algorithms.scale.factor,2' in table.read_text(encoding='utf-8-sig')
    assert run(rainfall, '--compare', 'factor-2', 'triple').returncode == 0
    assert list((rainfall/'results').glob('comparison-*.csv')) == [table]
    assert run(rainfall, '--compare', 'factor-2', 'factor-2').returncode == 1


def test_preparing_user_projects_is_explicit_and_never_overwrites_existing_edits(tmp_path):
    spec = importlib.util.spec_from_file_location('example_prepare', ROOT / 'examples/three-cases/_shared/prepare_projects.py')
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    package = tmp_path / 'package'
    builder.build_project('rainfall', package / 'projects/rainfall')
    source = package / 'projects/rainfall'
    for path in source.rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    workspace = tmp_path / 'my-workspace'
    first = prepare.prepare(package, workspace, ['rainfall'])
    project = first[0][2]
    assert first[0][1] == '已准备，可在网页登记'
    assert (project / 'case.py').stat().st_mode & 0o777 == 0o600
    (project / 'case.py').write_text('# My research edit\n')
    again = prepare.prepare(package, workspace, ['rainfall'])
    assert again[0][1] == '已存在，保留原内容'
    assert (project / 'case.py').read_text() == '# My research edit\n'
