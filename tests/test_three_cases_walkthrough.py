"""Current-Web compatibility of the IDE examples, without launching Web jobs."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

import yaml

from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_packages import pack_package
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('three_cases_walkthrough_builder', ROOT / 'tools/build-three-cases.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_three_case_zips_import_into_current_workspace_and_outputs_are_readable():
    with tempfile.TemporaryDirectory(prefix='.rk-example-import-', dir=ROOT) as temp:
        root = Path(temp)
        who = Principal(os.getuid(), pwd.getpwuid(os.getuid()).pw_name, 'teacher')
        store = SubmissionStore(root / 'store')
        workspace = Workspace(who, root / 'workspace', LocalStoreClient(store, who))
        for name in builder.CASES:
            project = builder.build_project(name, root / name)
            files = {path.relative_to(project).as_posix(): path.read_bytes()
                     for path in project.rglob('*') if path.is_file()}
            imported = workspace.import_package(name, pack_package(files))
            assert imported['name'] == name
            assert workspace.read_text(imported['project_id'], 'project.yaml')['text']
            assert workspace.read_text(imported['project_id'], 'run_experiment.py')['text']
            # Rainfall exercises a real IDE subprocess in the actual imported
            # project and the current Web file-reading interface afterwards.
            if name != 'rainfall':
                continue
            imported_root = Path(imported['path'])
            completed = subprocess.run([sys.executable, str(imported_root / 'run_experiment.py'),
                                        '--scheme', 'factor-2'], cwd=root, text=True,
                                       capture_output=True, timeout=60)
            assert completed.returncode == 0, completed.stderr
            summary_path, = imported_root.glob('results/factor-2/*/summary.json')
            content = workspace.read_text(imported['project_id'], summary_path.relative_to(imported_root).as_posix())
            assert json.loads(content['text'])['scaled_total'] == 20
            _path, snapshot, references = workspace._snapshot(imported['project_id'])
            assert summary_path.relative_to(imported_root).as_posix() in snapshot
            assert references == []
        with store._connection() as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'web_jobs' in tables:
                assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0] == 0


def test_water_acoustics_has_exactly_two_schemes_using_identical_observations():
    project = ROOT / 'examples/three-cases/acoustic-tdoa'
    paths = sorted((project / 'experiments').glob('*.yaml'))
    assert [p.stem for p in paths] == ['sound-speed-1480', 'sound-speed-1500']
    a, b = [yaml.safe_load(path.read_text()) for path in paths]
    assert a['case_parameters'] == b['case_parameters']
    assert a['algorithm_parameters']['locator'] == {'sound_speed_m_s': 1480.0}
    assert b['algorithm_parameters']['locator'] == {'sound_speed_m_s': 1500.0}


def test_guide_uses_web_creation_and_reviewed_framework_instead_of_imports():
    guide = (ROOT / 'docs/学生使用指南.md').read_text()
    pointer = (ROOT / 'docs/three-cases-walkthrough.md').read_text()
    assert '学生使用指南.md' in pointer
    assert '```python' not in pointer
    assert '### 1.1 网页新建空项目' in guide
    assert '个人本地算法' in guide
    assert '填入示例' not in guide
    assert 'plugin.py` 文本框**留空**' in guide
    assert '接口骨架' in guide
    assert 'python prepare_projects.py' not in guide
    assert '登记现有项目' not in guide and '导入 ZIP 包' not in guide
    assert 'rainfall.framework@1.0.0' in guide
    assert 'MNIST CNN' in guide and 'sound-speed-1480' in guide
    assert '不自动清空任何现有资源' in guide
    assert 'example.scale@1.0.0' not in guide
    assert 'case-836de5f7' not in guide
    spec = importlib.util.spec_from_file_location('render_tutorial', ROOT / 'tools/render-three-case-tutorial.py')
    renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
    assert set(renderer.outputs()) == {ROOT / 'docs/学生使用指南.md'}
    for path, expected in renderer.outputs().items():
        assert path.read_text() == expected, str(path)
