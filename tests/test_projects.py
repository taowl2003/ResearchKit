"""Project lifecycle checks using only temporary sources and destinations."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))

from researchkit import model, projects
from researchkit.files import KitError, snapshot, write_yaml


@pytest.fixture
def kit_home(tmp_path, monkeypatch):
    home = tmp_path / 'kit'
    for relative in ('library/cases/hello', 'library/algorithms/example.scale',
                     'library/contracts/scalar'):
        shutil.copytree(ROOT / relative, home / relative)
    monkeypatch.setenv('RK_HOME', str(home))
    return home


def test_hello_template_and_portable_task(kit_home, tmp_path):
    source = kit_home / 'library/cases/hello/1.0.0'
    before = snapshot(source)
    output = tmp_path / 'a path with spaces' / 'hello'
    projects.new_project(output)
    guide = (output / 'README-ResearchKit.md').read_text()
    assert 'case.py' in guide and 'project.yaml' in guide
    assert './task run' in guide and './task submit' in guide
    assert './task check' in guide and './task edit ALIAS' in guide
    assert 'algorithms/ALIAS/' in guide
    assert snapshot(source) == before
    assert model.doctor(output)['status'] == 'ready'
    env = os.environ.copy()
    env.pop('RK_HOME', None)
    env.pop('PYTHONPATH', None)
    menu = subprocess.run([str(output / 'task')], cwd=tmp_path, env=env,
                          capture_output=True, text=True, check=True).stdout
    help_text = subprocess.run([str(output / 'task'), '--help'], cwd=tmp_path, env=env,
                               capture_output=True, text=True, check=True).stdout
    assert './task run' in menu and './task submit' in menu
    assert 'rk new' not in menu and 'rk share' not in menu
    assert 'submit' in help_text and 'share' not in help_text and 'new' not in help_text
    result = subprocess.run([str(output / 'task'), 'run', '--json'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['result']['result'] == 6
    assert model.freshness(output)['status'] == 'success'


def test_script_directory_with_explicit_nonmain_command(kit_home, tmp_path):
    source = tmp_path / 'my-script'
    source.mkdir()
    (source / 'run.py').write_text('from pathlib import Path\nPath("result.txt").write_text("ok")\n')
    output = kit_home / 'projects/script'
    projects.import_project(source, output, ['python', 'run.py'])
    assert model.project(output)['command'] == ['python', 'run.py']
    from researchkit.engine import run_project
    assert run_project(output)['status'] == 'success'
    assert (output / 'result.txt').read_text() == 'ok'
    with pytest.raises(KitError, match='相互包含'):
        projects.import_project(source, source / 'nested', ['python', 'run.py'])


def test_legacy_import_uses_only_selected_release_assets(kit_home, tmp_path):
    source = tmp_path / 'legacy-case'
    source.mkdir()
    write_yaml(source / 'case.yaml', {
        'schema': 'hydro.case/v1', 'case': {'name': 'demo/one', 'version': '1.0.0', 'entry': 'case.py'},
        'platform': {'version': '1.0.0'}, 'environment': {'name': 'cpu'},
        'plugins': {'scale': {'name': 'example.scale', 'version': '1.0.0',
                              'parameters': {'factor': 4}}},
        'parameters': {}, 'data': {'sources': []}, 'dependencies': {'python': ['yaml']}})
    (source / 'case.py').write_text('def run(context):\n    return context.call("scale", inputs={"value": 2})\n')
    platform = tmp_path / 'legacy-platform'
    release = platform / 'releases/1.0.0/runtime'
    shutil.copytree(kit_home / 'library/algorithms/example.scale/1.0.0',
                    release / 'plugins/example.scale/1.0.0')
    shutil.copytree(kit_home / 'library/contracts/scalar', release / 'contracts/scalar')
    write_yaml(release / 'environments/cpu.yaml', {
        'schema': 'hydro.environment/v1', 'name': 'cpu',
        'python': {'minimum': '3.11'}, 'required_modules': ['yaml'],
        'module_minimums': {'yaml': '6.0'}})
    before_source, before_platform = snapshot(source), snapshot(platform)
    output = kit_home / 'projects/imported'
    result = projects.import_project(source, output, platform=platform)
    assert result['plugins'] == ['scale']
    assert model.project(output)['requires']['minimums'] == {'yaml': '6.0'}
    assert sorted(x.name for x in (output / '.researchkit/plugins').iterdir()) == ['scale']
    assert not (output / 'case.yaml').exists()
    assert snapshot(source) == before_source and snapshot(platform) == before_platform


def test_publish_uses_read_only_source_and_refuses_overwrite(kit_home, tmp_path):
    source = tmp_path / 'plugin-source'
    shutil.copytree(kit_home / 'library/algorithms/example.scale/1.0.0', source)
    meta = source / 'plugin.yaml'
    meta.write_text(meta.read_text().replace('version: 1.0.0', 'version: 2.0.0'))
    before = snapshot(source)
    result = projects.publish_plugin(source, kit_home / 'library/contracts')
    assert result['ref'] == 'example.scale@2.0.0'
    assert snapshot(source) == before
    with pytest.raises(KitError, match='版本已存在'):
        projects.publish_plugin(source, kit_home / 'library/contracts')


def test_add_missing_contract_preserves_valid_project(kit_home, tmp_path):
    output = kit_home / 'projects/base'
    projects.new_project(output)
    source = kit_home / 'library/algorithms/missing/1.0.0'
    shutil.copytree(kit_home / 'library/algorithms/example.scale/1.0.0', source)
    meta = source / 'plugin.yaml'
    meta.write_text(meta.read_text().replace('example.scale', 'missing').replace(
        'scalar@1.0.0', 'absent@1.0.0'))
    before = snapshot(output)
    with pytest.raises(KitError, match='缺少契约'):
        projects.add_plugin(output, 'missing@1.0.0', 'missing', {'factor': 2})
    assert snapshot(output) == before
    assert model.doctor(output)['status'] == 'ready'
