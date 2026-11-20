#!/usr/bin/env python3
"""Copy example projects into the current user's workspace, never overwrite."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil

CASES = ('rainfall', 'mnist-cnn', 'acoustic-tdoa')


def prepare(package: Path, workspace: Path, cases):
    root = workspace.expanduser().resolve() / 'projects'
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for name in cases:
        source = package / 'projects' / name
        if not (source / 'project.yaml').is_file():
            raise ValueError(f'案例包不完整：{name}；请先核对 SHA256SUMS')
        destination = root / ('rk-demo-' + name)
        if destination.exists() or destination.is_symlink():
            results.append((name, '已存在，保留原内容', destination))
            continue
        # copytree's exclusive directory creation prevents accidental overwrite.
        shutil.copytree(source, destination)
        # Release originals may be read-only; the user's own copy must be
        # editable.  Never change the package or an already existing project.
        for current, directories, files in os.walk(destination):
            Path(current).chmod(0o700)
            for filename in files:
                (Path(current) / filename).chmod(0o600)
        results.append((name, '已准备，可在网页登记', destination))
    return results


def main():
    parser = argparse.ArgumentParser(description='将案例复制到本人工作区；不登记中央资源、不覆盖已有目录')
    parser.add_argument('--case', choices=CASES, action='append', help='仅准备指定案例；默认全部')
    parser.add_argument('--workspace', type=Path, default=Path.home() / 'researchkit-workspace')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('请使用自己的研究账号运行，不要使用 root')
    package = Path(__file__).resolve().parent
    for name, state, path in prepare(package, args.workspace, args.case or CASES):
        print(f'{name}：{state}')
        print(f'  IDE 打开：{path}')
        print(f'  网页“登记现有项目”填写：projects/rk-demo-{name}')


if __name__ == '__main__':
    main()
