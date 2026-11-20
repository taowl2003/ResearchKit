#!/usr/bin/env python3
"""Universal experiment runner for ResearchKit workspace projects.

Automatically locates the project root from the active file, detects the
selected scheme, and runs the project's run_experiment.py.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import sys


def find_project_root(start_path: Path) -> Path | None:
    try:
        curr = start_path.resolve()
    except Exception:
        return None
    if curr.is_file():
        curr = curr.parent
    while curr != curr.parent:
        if (curr / 'run_experiment.py').exists() and (curr / 'project.yaml').exists():
            return curr
        curr = curr.parent
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal project experiment runner")
    parser.add_argument("--active-file", default="", help="Currently open file in IDE")
    parser.add_argument("--scheme", default="", help="Experiment scheme ID")
    parser.add_argument("--compare", nargs="+", metavar="SCHEME", help="Compare schemes")
    args, unknown = parser.parse_known_args()

    # 1. Determine project root
    active_path = Path(args.active_file) if args.active_file else Path.cwd()
    root = find_project_root(active_path)
    if not root:
        root = find_project_root(Path.cwd())
    if not root:
        print("错误: 未能定位到有效的项目根目录 (未发现 run_experiment.py 和 project.yaml)。", file=sys.stderr)
        print(f"当前参考路径: {active_path}", file=sys.stderr)
        print("请确保已在 projects 下的某个项目中打开文件后再运行调试。", file=sys.stderr)
        return 1

    # 2. Determine scheme
    scheme = args.scheme.strip()
    if (not scheme or scheme == "auto") and not args.compare:
        # If active file is an experiment yaml, auto-detect scheme
        if active_path.is_file() and active_path.suffix in ('.yaml', '.yml') and active_path.parent.name == 'experiments':
            scheme = active_path.stem
        else:
            exp_dir = root / 'experiments'
            schemes = [p.stem for p in sorted(exp_dir.glob('*.yaml'))] if exp_dir.exists() else []
            if len(schemes) == 1:
                scheme = schemes[0]
            elif not schemes:
                print(f"错误: 在 {exp_dir} 下未找到任何 YAML 方案文件。", file=sys.stderr)
                return 1
            else:
                # If baseline exists, use it; otherwise use the first one
                if 'baseline' in schemes:
                    scheme = 'baseline'
                elif 'cnn-baseline' in schemes:
                    scheme = 'cnn-baseline'
                else:
                    scheme = schemes[0]

    # 3. Assemble arguments for run_experiment.py
    run_script = root / 'run_experiment.py'
    cmd_args = [str(run_script)]
    if args.compare:
        cmd_args += ['--compare'] + args.compare
    elif scheme:
        cmd_args += ['--scheme', scheme]
    cmd_args.extend(unknown)

    # 4. Set environment and run
    os.chdir(root)
    sys.argv = cmd_args
    sys.path.insert(0, str(root))
    try:
        runpy.run_path(str(run_script), run_name="__main__")
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)


if __name__ == '__main__':
    sys.exit(main())
