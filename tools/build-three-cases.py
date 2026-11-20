#!/usr/bin/env python3
"""Build IDE-ready, current-Web-importable examples without touching users.

Run with the research Python environment. Output is a new directory, never an
in-place update of a workspace, release, platform resource or shared dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))

from researchkit import model, projects
from researchkit.web_packages import pack_package, unpack_package

CASES = ('rainfall', 'mnist-cnn', 'acoustic-tdoa')
RUNTIME = ('__init__.py', 'files.py', 'model.py', 'engine.py', 'dataset_access.py')


def build_project(name: str, destination: Path) -> Path:
    if name not in CASES:
        raise ValueError('未知案例')
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f'目标已存在，不会覆盖：{destination}')
    source = ROOT / 'examples' / 'three-cases' / name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
    shared = ROOT / 'examples' / 'three-cases' / '_shared'
    for name in ('experiment_support.py', 'run_experiment.py'):
        shutil.copyfile(shared / name, destination / name)
    runtime = destination / '.researchkit' / 'runtime' / 'researchkit'
    runtime.mkdir(parents=True)
    for name in RUNTIME:
        shutil.copyfile(ROOT / 'core' / 'researchkit' / name, runtime / name)
    contract = destination / '.researchkit' / 'contracts' / 'mapping' / '1.0.0.yaml'
    contract.parent.mkdir(parents=True)
    contract.write_text('schema: hydro.contract/v1\nid: mapping@1.0.0\ntype: mapping\n', encoding='utf-8')
    projects.seal_lock(destination)
    model.project(destination)
    model.verify_lock(destination)
    return destination


def build_all(destination: Path):
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    report = []
    for name in CASES:
        project = build_project(name, destination / 'projects' / name)
        files = {p.relative_to(project).as_posix(): p.read_bytes()
                 for p in sorted(project.rglob('*')) if p.is_file()}
        archive = pack_package(files)
        decoded, bundle = unpack_package(archive)
        assert decoded == files and bundle is None
        (destination / f'{name}.zip').write_bytes(archive)
        report.append({'case': name, 'files': len(files), 'zip_bytes': len(archive),
                       'zip_sha256': hashlib.sha256(archive).hexdigest()})
    shutil.copyfile(ROOT / 'examples/three-cases/_shared/prepare_projects.py', destination / 'prepare_projects.py')
    guide = ROOT / 'docs' / '科研编程平台三大全流程实战案例.md'
    if guide.is_file():
        shutil.copyfile(guide, destination / '先读这里-三个案例操作指南.md')
    (destination / 'PACKAGE.json').write_text(json.dumps({
        'schema': 'researchkit.three-cases-package/v1', 'cases': report,
        'requires_platform_upgrade': False,
        'execution': 'IDE / user Python environment',
        'note': '现有网页提交仍有后台执行门槛；GPU实验先在IDE运行、查看项目产物。'
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sums = []
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            sums.append(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(destination).as_posix()}')
    (destination / 'SHA256SUMS').write_text('\n'.join(sums) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='新的案例包目录；已存在则停止')
    args = parser.parse_args()
    print(json.dumps(build_all(args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
