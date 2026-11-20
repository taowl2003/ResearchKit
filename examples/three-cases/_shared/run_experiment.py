#!/usr/bin/env python3
"""IDE Run/Debug this file; arguments: --scheme baseline (or --list)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / '.researchkit' / 'runtime'))
sys.path.insert(0, str(ROOT))

try:
    from experiment_support import main
except ModuleNotFoundError as exc:
    if exc.name == 'yaml':
        raise SystemExit('当前 Python 缺少 PyYAML；请按完整手册选择已有科研环境或安装案例依赖。') from exc
    raise

if __name__ == '__main__':
    raise SystemExit(main(ROOT))
