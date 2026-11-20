"""Server-side lifecycle used by the one-window SSH launcher.

This uses a real controlling PTY and the real Web main entry point, with a
private test workspace. It does not pretend to run Windows PowerShell or SSH.
"""
from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path


def test_launch_address_authenticates_and_terminal_close_stops_personal_web():
    root = Path(tempfile.mkdtemp(prefix='.rk-launch-test-', dir=Path.cwd()))
    master, slave = pty.openpty()
    child = None
    try:
        code = '''
import fcntl, os, pwd, sys, termios
from pathlib import Path
from researchkit import web_app
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
account = pwd.getpwuid(os.getuid())
principal = Principal(account.pw_uid, account.pw_name, 'teacher' if account.pw_name == 'ly' else 'student')
root = Path(sys.argv[1])
store = SubmissionStore(root / 'store')
workspace = Workspace(principal, root / 'workspace', web_app.LocalStoreClient(store, principal))
web_app._production_workspace = lambda *_args: workspace
raise SystemExit(web_app.main(['--port', '0']))
'''
        environment = dict(os.environ, PYTHONPATH=str(Path('core').resolve()), PYTHONDONTWRITEBYTECODE='1')
        child = subprocess.Popen([sys.executable, '-c', code, str(root)],
                                 stdin=slave, stdout=slave, stderr=slave, env=environment)
        os.close(slave)
        slave = -1
        received = b''
        match = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                received += os.read(master, 4096)
                match = re.search(rb'http://127\.0\.0\.1:(\d+)/#token=([A-Za-z0-9_-]{43})\r?\n', received)
                if match:
                    break
            assert child.poll() is None, 'personal Web exited before startup'
        assert match is not None, 'personal Web did not emit a startup URL'
        port, token = match.group(1).decode(), match.group(2).decode()
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/me',
                                         headers={'Authorization': 'Bearer ' + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            assert json.load(response)['uid'] == os.getuid()
        # Browsers request this automatically. The v0.7 default handler returns
        # 405, but a missing icon must not terminate the personal Web session.
        try:
            opener.open(f'http://127.0.0.1:{port}/favicon.ico', timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 405
            error.close()
        else:
            raise AssertionError('v0.7 favicon request unexpectedly succeeded')
        assert child.poll() is None, 'favicon request terminated personal Web'
        with opener.open(request, timeout=5) as response:
            assert json.load(response)['uid'] == os.getuid()
        os.close(master)
        master = -1
        assert child.wait(timeout=5) == -signal.SIGHUP
    finally:
        if master != -1:
            os.close(master)
        if slave != -1:
            os.close(slave)
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        shutil.rmtree(root)
