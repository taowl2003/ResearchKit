"""Execute the PowerShell launcher tests when a test runtime is available.

Linux exercises logic and the generated POSIX enrollment command, not Windows
console authentication or the actual production SSH server.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_first_use_and_existing_launcher(tmp_path):
    runtime = os.environ.get('RK_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not runtime:
        pytest.skip('requires PowerShell; set RK_TEST_PWSH to a portable runtime')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([runtime, '-NoLogo', '-NoProfile', '-File',
                             str(root / 'tests/test_windows_first_use.ps1'),
                             '-Repository', str(root), '-Temporary', str(tmp_path), '-Python', sys.executable],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'First-use tests passed' in result.stdout
