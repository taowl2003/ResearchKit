"""Harmless real bubblewrap probes, run under the current UID, not production.

These tests establish namespace/termination behavior, not multi-account
deployment. The installed system-service cgroup gate is checked separately.
"""
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.projects import install_runtime, seal_lock
from researchkit.web_runner import ENV, execute, materialize, sandbox_command


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = Path('/opt/researchkit-web/venv')


@pytest.fixture
def snapshot():
    with tempfile.TemporaryDirectory(prefix='.rk-run-test-', dir=ROOT) as temporary:
        root = Path(temporary)
        project = root / 'project'
        shutil.copytree(ROOT / 'library/cases/hello/1.0.0', project)
        install_runtime(project)
        seal_lock(project)
        yield project


def run(project, operation='run', *, cancel=lambda: False, timeout=10):
    log = []
    result = execute(project, operation, code_root=ROOT, environment=ENVIRONMENT,
                     log=log.append, cancelled=cancel, timeout=timeout)
    return result, ''.join(log)


def test_static_check_then_fixed_runtime_and_namespace_visibility(snapshot):
    snapshot.joinpath('case.py').write_text('''def run(context):
    import os, socket
    assert not os.path.exists('/home')
    assert not os.path.exists('/var/lib/researchkit-web')
    assert not os.path.exists('/run/researchkit-web/broker.sock')
    assert socket.if_nameindex() == [(1, 'lo')]
    assert os.getenv('RESEARCHKIT_TEST_SECRET') is None
    print('inside-isolated-fixed-code')
    return {'answer': context.call('scale', inputs={'value': 2})['value']}
''')
    os.environ['RESEARCHKIT_TEST_SECRET'] = 'must-not-cross'
    try:
        checked, log = run(snapshot, 'check')
        assert checked[0] == 'succeeded', log
        assert 'inside-isolated-fixed-code' not in log
        result, log = run(snapshot)
        assert result[0] == 'succeeded', log
        assert 'inside-isolated-fixed-code' in log and '6' in log
        assert not (snapshot / '_runs').exists()
    finally:
        os.environ.pop('RESEARCHKIT_TEST_SECRET', None)


def test_cancel_timeout_and_log_overflow_stop_execution(snapshot):
    snapshot.joinpath('case.py').write_text('def run(context):\n    while True: pass\n')
    result, _ = run(snapshot, timeout=0.3)
    assert result[0] == 'timed_out'
    result, _ = run(snapshot, cancel=lambda: True)
    assert result[0] == 'cancelled'
    snapshot.joinpath('case.py').write_text("def run(context):\n    print('x' * 1100000, flush=True)\n    return {}\n")
    result, log = run(snapshot)
    assert result[0] == 'failed' and '日志' in result[2]
    assert len(log.encode()) <= 1024**2


def test_local_external_mapping_refused_before_code(snapshot):
    snapshot.joinpath('.researchkit/local.json').write_text('{"data_root":"/home/ly"}')
    snapshot.joinpath('case.py').write_text("def run(context):\n    print('must-not-run')\n    return {}\n")
    result, log = run(snapshot)
    assert result[0] == 'failed'
    assert 'local.json' in log and 'must-not-run' not in log


def test_codec_rejects_paths_before_materializing(tmp_path):
    with pytest.raises(KitError):
        materialize(tmp_path, {'../escape': b'bad'})
    assert not list(tmp_path.iterdir())


def test_external_mount_is_exact_readonly_and_does_not_leak_fds(snapshot):
    source = snapshot.parent / 'dataset'
    source.mkdir()
    data = source / 'numbers.txt'
    data.write_text('2 3 5')
    (source / 'private.txt').write_text('must-not-be-mounted')
    snapshot.joinpath('case.py').write_text('''def run(context):
    from pathlib import Path
    import os
    data = Path('/datasets/numbers-v1/nested/numbers.txt')
    assert data.read_text() == '2 3 5'
    assert not (data.parent / 'private.txt').exists()
    try:
        data.write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('external data writable')
    for fd in Path('/proc/self/fd').iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        assert '/dataset/' not in target, target
    context.atomic_write_json(context.result_path('sum.json'), {'sum': 10})
    return {'answer': 10}
''')
    fd = os.open(data, os.O_RDONLY)
    try:
        # Pin the file, then replace its host pathname. The mount must use the
        # verified descriptor, never reopen a path after the hash check.
        data.rename(source / 'old.txt')
        data.write_text('wrong replacement')
        mount = SimpleNamespace(fd=fd, target='/datasets/numbers-v1/nested/numbers.txt')
        log = []
        result = execute(snapshot, 'run', code_root=ROOT, environment=ENVIRONMENT,
                         log=log.append, cancelled=lambda: False, timeout=10, mounts=[mount])
        assert result[0] == 'succeeded', ''.join(log)
        assert json.loads(result[3]['results/sum.json']) == {'sum': 10}
        assert (source / 'old.txt').read_text() == '2 3 5'
        assert data.read_text() == 'wrong replacement'
    finally:
        os.close(fd)


def test_readiness_notification_requires_systemd_channel(tmp_path, monkeypatch):
    from researchkit.web_runner import notify_ready
    monkeypatch.delenv('NOTIFY_SOCKET', raising=False)
    with pytest.raises(KitError, match='readiness'):
        notify_ready()
    path = str(tmp_path / 'notify.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        server.settimeout(2)
        monkeypatch.setenv('NOTIFY_SOCKET', path)
        notify_ready()
        assert server.recv(1024).startswith(b'READY=1\n')
    assert 'NOTIFY_SOCKET' not in ENV


def test_bwrap_probe_has_private_pid_and_network_namespace(snapshot):
    result = subprocess.run(sandbox_command(snapshot, operation='check', code_root=ROOT,
                                            environment=ENVIRONMENT, probe=True),
                            env=ENV, capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout)['isolated'] is True


def test_execution_uses_submission_runtime_and_readonly_host_snapshot(snapshot):
    engine = snapshot / '.researchkit/runtime/researchkit/engine.py'
    engine.write_text(engine.read_text() + "\nprint('PINNED_RUNTIME_TEST_060')\n")
    seal_lock(snapshot)
    original = snapshot.joinpath('case.py').read_bytes()
    result, log = run(snapshot)
    assert result[0] == 'succeeded', log
    assert 'PINNED_RUNTIME_TEST_060' in log
    assert json.loads(result[3]['results/summary.json'])['result'] == 6
    assert snapshot.joinpath('case.py').read_bytes() == original
    assert not snapshot.joinpath('results/summary.json').exists()


def test_hard_output_limit_cannot_be_raised_and_links_are_refused(snapshot):
    snapshot.joinpath('case.py').write_text('''def run(context):
    import resource, pathlib
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (2**30, 2**30))
    except ValueError:
        print('hard-file-limit-enforced')
    else:
        raise AssertionError('file hard limit was raised')
    pathlib.Path('/work/project/results/leak').symlink_to('/usr/bin/python3.12')
    return {}
''')
    result, log = run(snapshot)
    assert 'hard-file-limit-enforced' in log
    assert result[0] == 'failed' and result[3] == {}


def test_timeout_cleans_descendants_with_separate_sessions(snapshot, monkeypatch):
    import researchkit.web_runner as runner
    snapshot.joinpath('case.py').write_text('''def run(context):
    import subprocess, sys, time
    subprocess.Popen([sys.executable, '-c', 'import os,time; os.setsid(); print("descendant-ready",flush=True); time.sleep(30)'])
    while True: time.sleep(.1)
''')
    original = runner.subprocess.Popen
    monitor = []
    def capture(*args, **kwargs):
        proc = original(*args, **kwargs)
        monitor.append(proc.pid)
        return proc
    monkeypatch.setattr(runner.subprocess, 'Popen', capture)
    children = set()
    def collect(pid):
        try:
            ids = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            return
        for item in ids:
            child = int(item)
            children.add(child)
            collect(child)
    def log(value):
        if 'descendant-ready' in value:
            collect(monitor[0])
    result = execute(snapshot, 'run', code_root=ROOT, environment=ENVIRONMENT,
                     log=log, cancelled=lambda: False, timeout=1.5)
    assert result[0] == 'timed_out' and len(children) >= 2
    for pid in children:
        deadline = time.monotonic() + 1
        while True:
            try:
                status = Path(f'/proc/{pid}/status').read_text()
            except (FileNotFoundError, ProcessLookupError):
                break
            if '\nState:\tZ' in status or '\nState:\tX' in status:
                break
            assert time.monotonic() < deadline, status
            time.sleep(.02)


def test_large_processed_output_is_staged_without_small_zip_copy(snapshot,tmp_path):
    from researchkit.web_runner import staged_artifacts
    snapshot.joinpath('case.py').write_text("""def run(context):
    from pathlib import Path
    folder=Path('data/processed');folder.mkdir(parents=True,exist_ok=True)
    with (folder/'features.bin').open('wb') as stream:
        for _ in range(3):stream.write(b'x'*1024*1024)
    return {'produced_bytes':3*1024*1024}
""")
    stage=tmp_path/'stage';stage.mkdir()
    result=execute(snapshot,'run',code_root=ROOT,environment=ENVIRONMENT,log=lambda text:None,
                   cancelled=lambda:False,artifact_stage=stage,artifact_file_limit=32*1024**3)
    assert result[0]=='succeeded',result
    assert result[3]=={},'processed outputs are retained separately, not put into the small ZIP'
    records=staged_artifacts(stage,file_limit=32*1024**3)
    assert len(records)==1 and records[0]['path']=='data/processed/features.bin' and records[0]['size']==3*1024*1024


def test_large_total_small_outputs_use_partial_zip_and_full_staging(snapshot,tmp_path):
    from researchkit.web_runner import staged_artifacts
    snapshot.joinpath('case.py').write_text("""def run(context):
    from pathlib import Path
    for index in range(5):
        Path(f'results/part-{index}.bin').write_bytes(b'x'*(2*1024*1024))
    return {'parts':5}
""")
    stage=tmp_path/'stage';stage.mkdir()
    result=execute(snapshot,'run',code_root=ROOT,environment=ENVIRONMENT,log=lambda text:None,
                   cancelled=lambda:False,artifact_stage=stage,artifact_file_limit=32*1024**3)
    assert result[0]=='succeeded',result
    assert sum(len(value) for value in result[3].values())<=8*1024**2
    assert len(staged_artifacts(stage,file_limit=32*1024**3))==5
