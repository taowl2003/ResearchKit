"""Executable checks for the self-contained rainfall and acoustic TDOA examples."""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location('three_case_builder', ROOT / 'tools' / 'build-three-cases.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(project: Path, scheme: str):
    completed = subprocess.run([sys.executable, 'run_experiment.py', '--scheme', scheme], cwd=project,
                               text=True, capture_output=True, check=True)
    batches = sorted((project / 'results' / scheme).iterdir())
    assert batches
    batch = batches[-1]
    return json.loads((batch / 'summary.json').read_text(encoding='utf-8')), batch, completed


def _run_failing(project: Path, scheme: str):
    completed = subprocess.run([sys.executable, 'run_experiment.py', '--scheme', scheme], cwd=project,
                               text=True, capture_output=True, check=False)
    assert completed.returncode == 1
    return completed.stderr


def _trajectory(path: Path):
    with path.open(encoding='utf-8', newline='') as stream:
        return [(row['ping_id'], float(row['x_m']), float(row['y_m']), float(row['z_m']))
                for row in csv.DictReader(stream)]


def test_rainfall_schemes_are_self_contained_and_keep_per_scheme_artifacts(tmp_path):
    project = _builder().build_project('rainfall', tmp_path / 'rainfall')
    baseline, first_batch, first_process = _run(project, 'factor-2')
    changed, second_batch, second_process = _run(project, 'factor-3')
    assert baseline['count'] == 3
    assert baseline['raw_total'] == 10.0
    assert baseline['scaled_total'] == 20.0
    assert changed['scaled_total'] == 30.0
    assert first_batch != second_batch
    for batch, process in ((first_batch, first_process), (second_batch, second_process)):
        record = json.loads((batch / 'experiment.json').read_text(encoding='utf-8'))
        assert record['status'] == 'succeeded'
        assert record['execution'] == 'user-environment'
        assert (batch / 'parameters.json').is_file()
        assert (batch / 'processed_rainfall.csv').is_file()
        assert (batch / 'rainfall_chart.svg').is_file()
        assert '本次产物：' in process.stdout


def test_tdoa_speed_is_used_and_true_trajectory_is_not_part_of_estimate(tmp_path):
    project = _builder().build_project('acoustic-tdoa', tmp_path / 'tdoa')
    matched, matched_batch, _ = _run(project, 'sound-speed-1500')
    mismatched, mismatched_batch, _ = _run(project, 'sound-speed-1480')
    assert matched['count'] == 10
    assert matched['rmse_m'] < 1e-4
    assert mismatched['rmse_m'] > 1.0
    assert mismatched['sound_speed_m_s'] == 1480.0
    original_coordinates = _trajectory(matched_batch / 'localized_trajectory.csv')

    receivers = project / 'sample-data' / 'hydrophones.csv'
    receiver_rows = list(csv.DictReader(receivers.open(encoding='utf-8', newline='')))
    with receivers.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['receiver_id', 'x_m', 'y_m', 'z_m'])
        writer.writeheader(); writer.writerows(reversed(receiver_rows))
    reordered, reordered_batch, _ = _run(project, 'sound-speed-1500')
    assert reordered['rmse_m'] < 1e-4
    assert _trajectory(reordered_batch / 'localized_trajectory.csv') == original_coordinates

    truth = project / 'sample-data' / 'true_trajectory.csv'
    rows = list(csv.DictReader(truth.open(encoding='utf-8', newline='')))
    for row in rows:
        row.update(x_m='99999', y_m='-99999', z_m='77777')
    with truth.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['ping_id', 'x_m', 'y_m', 'z_m'])
        writer.writeheader(); writer.writerows(rows)
    again, again_batch, _ = _run(project, 'sound-speed-1500')
    assert _trajectory(again_batch / 'localized_trajectory.csv') == original_coordinates
    assert again['rmse_m'] > matched['rmse_m']
    assert (mismatched_batch / 'trajectory.svg').is_file()
    assert (mismatched_batch / 'position_error.svg').is_file()


def test_tdoa_rejects_duplicate_receiver_and_ping_ids(tmp_path):
    project = _builder().build_project('acoustic-tdoa', tmp_path / 'tdoa-receiver')
    receivers = project / 'sample-data' / 'hydrophones.csv'
    receiver_rows = list(csv.DictReader(receivers.open(encoding='utf-8', newline='')))
    receiver_rows[0]['receiver_id'] = receiver_rows[1]['receiver_id']
    with receivers.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['receiver_id', 'x_m', 'y_m', 'z_m'])
        writer.writeheader(); writer.writerows(receiver_rows)
    assert 'receiver_id' in _run_failing(project, 'sound-speed-1500')

    project = _builder().build_project('acoustic-tdoa', tmp_path / 'tdoa-ping')
    measurements = project / 'sample-data' / 'tdoa_measurements.csv'
    measurement_rows = list(csv.DictReader(measurements.open(encoding='utf-8', newline='')))
    measurement_rows.append(dict(measurement_rows[0]))
    with measurements.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=measurement_rows[0])
        writer.writeheader(); writer.writerows(measurement_rows)
    assert 'ping_id' in _run_failing(project, 'sound-speed-1500')

    project = _builder().build_project('acoustic-tdoa', tmp_path / 'tdoa-truth')
    truth = project / 'sample-data' / 'true_trajectory.csv'
    truth_rows = list(csv.DictReader(truth.open(encoding='utf-8', newline='')))
    truth_rows.append(dict(truth_rows[0]))
    with truth.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=truth_rows[0])
        writer.writeheader(); writer.writerows(truth_rows)
    assert 'ping_id' in _run_failing(project, 'sound-speed-1500')


def test_tdoa_single_file_zip_dataset_shape_runs_without_extraction(tmp_path):
    project = _builder().build_project('acoustic-tdoa', tmp_path / 'tdoa')
    scheme = project / 'experiments' / 'sound-speed-1500.yaml'
    content = scheme.read_text(encoding='utf-8').replace('data_format: directory', 'data_format: zip').replace(
        'data_root: sample-data', 'data_root: sample-data.zip')
    scheme.write_text(content, encoding='utf-8')
    summary, batch, _ = _run(project, 'sound-speed-1500')
    assert summary['rmse_m'] < 1e-4
    assert (batch / 'localized_trajectory.csv').is_file()
    assert not list(project.rglob('*.extracting'))
