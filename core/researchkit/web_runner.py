"""Serial execution service in a dedicated UID, outside the submission broker.

No arbitrary command/path is accepted over the protocol. All code execution
happens in bubblewrap, with a minimal filesystem and aggregate cgroup limits.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import fcntl
import hashlib
import json
import os
import selectors
import select
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .files import KitError
from .web_broker import BrokerClient, _decode_files
from .web_identity import IdentityError, Principal, load_policy, validate_policy, _validate_trusted_code_root, _validate_broker_interpreter
from .web_store import _validate_data_references, _validate_files
from .web_datasets import DatasetRegistry, DatasetCancelled, DatasetTimedOut
from .web_dataset_trees import TreeRegistry, is_tree_reference
from .web_derived import DerivedDatasets

LIMITS = {'timeout_seconds': 60, 'memory_bytes': 768 * 1024**2,
          'max_processes': 64, 'log_bytes': 1024**2}
MAX_STAGED_ARTIFACTS = 64
MAX_STAGED_TOTAL_BYTES = 64 * 1024**3
ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8',
       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}


def notify_ready() -> None:
    address = os.environ.get('NOTIFY_SOCKET', '')
    if not address.startswith(('/', '@')):
        raise KitError('执行服务需要 systemd readiness 通知通道')
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.sendto(b'READY=1\nSTATUS=Isolation self-check passed; worker registered', address)


def sandbox_command(snapshot: Path, *, operation: str, code_root: Path,
                    environment: Path, probe: bool = False, transfer: Path | None = None,
                    mounts=(), artifact_stage: Path | None = None,
                    artifact_file_limit: int = 2 * 1024**2) -> list[str]:
    if operation not in {'check', 'run'}:
        raise KitError('不支持的运行操作')
    command = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
               '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr',
               '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
               '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc', '--dev', '/dev',
               '--tmpfs', '/tmp', '--tmpfs', '/work', '--chdir', '/work',
               '--ro-bind', str(snapshot), '/snapshot',
               '--ro-bind', str(environment), str(environment),
               '--ro-bind', str(code_root / 'core'), '/opt/researchkit-code']
    python = str(environment / 'bin/python')
    if transfer is not None:
        command += ['--bind', str(transfer), '/transfer']
    if artifact_stage is not None:
        command += ['--dir', '/work/project', '--bind', str(artifact_stage), '/work/project',
                    '--setenv', 'RESEARCHKIT_ARTIFACT_FILE_LIMIT', str(artifact_file_limit),
                    '--setenv', 'RESEARCHKIT_ALLOW_STAGED_RESULTS', '1' if artifact_file_limit > 2 * 1024**2 else '0']
    if mounts:
        command += ['--tmpfs', '/datasets']
        for mount in mounts:
            command += ['--ro-bind-fd', str(mount.fd), mount.target]
        command += ['--remount-ro', '/datasets']
    if probe:
        program = ('import os,socket,json; '
                   'assert not os.path.exists("/home") and not os.path.exists("/var/lib"); '
                   'assert not os.path.exists("/run/researchkit-web/broker.sock"); '
                   'assert [x[1] for x in socket.if_nameindex()]==["lo"]; '
                   'assert os.getpid()<10; '
                   'print(json.dumps({"isolated":True,"uid":os.getuid()}))')
        return command + [python, '-I', '-c', program]
    program = ('import sys,runpy; sys.path.insert(0,"/opt/researchkit-code"); '
               'runpy.run_module("researchkit.web_run_entry",run_name="__main__")')
    return command + [python, '-I', '-u', '-c', program, operation]


def verify_cgroup() -> dict[str, int]:
    """Fail closed unless the system service has hard aggregate constraints."""
    lines = Path('/proc/self/cgroup').read_text().splitlines()
    rel = next((line[3:] for line in lines if line.startswith('0::')), None)
    if rel is None or '..' in Path(rel).parts:
        raise KitError('需要 cgroup v2 资源限制')
    root = Path('/sys/fs/cgroup') / rel.lstrip('/')
    try:
        memory = int((root / 'memory.max').read_text())
        processes = int((root / 'pids.max').read_text())
        swap = int((root / 'memory.swap.max').read_text())
        quota, period = map(int, (root / 'cpu.max').read_text().split())
    except (OSError, ValueError) as exc:
        raise KitError('执行服务缺少内存、进程数或 CPU 硬限制') from exc
    if not (0 < memory <= LIMITS['memory_bytes'] and 0 < processes <= LIMITS['max_processes']
            and swap == 0 and 0 < quota <= period):
        raise KitError('执行服务资源限制超出允许值')
    status = Path('/proc/self/status').read_text().splitlines()
    if 'NoNewPrivs:\t1' not in status:
        raise KitError('执行服务必须启用 NoNewPrivileges')
    return {'memory_bytes': memory, 'max_processes': processes}


def materialize(folder: Path, files: dict[str, bytes]) -> None:
    files, _ = _validate_files(files)
    for rel, data in files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o500 if rel == 'task' else 0o400)


def staged_artifacts(root: Path, *, file_limit: int) -> list[dict[str, object]]:
    """Read runner-owned output after the sandbox has died, without links."""
    if file_limit <= 0:
        raise KitError('派生产物单文件上限无效')
    check_output_budget(root, file_limit=file_limit)
    entries: list[dict[str, object]] = []
    total = 0
    for name in ('results', 'report', 'data/processed', 'artifacts'):
        folder = root / name
        if not folder.exists() and not folder.is_symlink():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise KitError('结果目录必须是普通目录')
        for parent, directories, files in os.walk(folder, followlinks=False):
            if any((Path(parent) / child).is_symlink() for child in directories):
                raise KitError('结果不能包含目录链接')
            for filename in sorted(files):
                path = Path(parent) / filename
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_size > file_limit):
                    raise KitError('结果文件类型或大小无效')
                if len(entries) >= MAX_STAGED_ARTIFACTS:
                    raise KitError('结果文件超过 64 个')
                total += info.st_size
                if total > MAX_STAGED_TOTAL_BYTES:
                    raise KitError('结果文件总计超过 64 GiB')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
                try:
                    opened = os.fstat(fd)
                    if _stable_file(info) != _stable_file(opened):
                        raise KitError('结果文件在读取时变化')
                    digest = hashlib.sha256()
                    while True:
                        block = os.read(fd, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                    if _stable_file(opened) != _stable_file(os.fstat(fd)):
                        raise KitError('结果文件在读取时变化')
                finally:
                    os.close(fd)
                entries.append({'path': path.relative_to(root).as_posix(), 'size': info.st_size,
                                'sha256': digest.hexdigest()})
    return sorted(entries, key=lambda item: str(item['path']))


def check_output_budget(root: Path, *, file_limit: int) -> None:
    """Bound the complete writable project while submitted code is running."""
    from .web_store import MAX_FILES
    total, entries = 0, 0
    for parent, directories, files in os.walk(root, followlinks=False):
        if len(Path(parent).relative_to(root).parts) > 64:
            raise KitError('运行输出目录层级超过上限')
        for name in directories + files:
            entries += 1
            if entries > MAX_FILES * 2 + MAX_STAGED_ARTIFACTS:
                raise KitError('运行输出文件和目录数量超过上限')
            try:
                info = (Path(parent)/name).lstat()
            except FileNotFoundError:
                continue  # Submitted code may be atomically replacing a file.
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise KitError('运行输出含链接或特殊文件')
            if info.st_size > max(16 * 1024**2, file_limit):
                raise KitError('运行输出单文件超过上限')
            total += info.st_size
            if total > MAX_STAGED_TOTAL_BYTES:
                raise KitError('运行输出总量超过 64 GiB')


def _stable_file(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def retain_staged_artifact(client: BrokerClient, instance: str, root: Path,
                           request: dict[str, object]) -> None:
    """Stream one runner-private staged file to the broker, one MiB at a time."""
    request_id, run_id, path = request.get('request_id'), request.get('run_id'), request.get('path')
    size, expected = request.get('size'), request.get('sha256')
    if (not all(isinstance(value, str) and value for value in (request_id, run_id, path, expected)) or
            isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise KitError('派生产物保留请求记录无效')
    relative = Path(path)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise KitError('派生产物保留路径无效')
    target = root / f'staged-{run_id}' / relative
    begin = client.request('runner_begin_retention', instance_id=instance, request_id=request_id)
    if type(begin) is not dict or not isinstance(begin.get('upload_id'), str):
        raise KitError('派生产物上传初始化响应无效')
    upload_id = begin['upload_id']
    descriptor = -1
    try:
        before = os.lstat(target)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
            raise KitError('暂存派生产物状态异常')
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
        opened = os.fstat(descriptor)
        if _stable_file(before) != _stable_file(opened):
            raise KitError('暂存派生产物读取时变化')
        offset, digest = 0, hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            client.request('runner_append_retention', instance_id=instance, request_id=request_id,
                           offset=offset, data=block)
            offset += len(block)
        if offset != size or digest.hexdigest() != expected or _stable_file(opened) != _stable_file(os.fstat(descriptor)):
            raise KitError('暂存派生产物摘要变化')
        client.request('runner_commit_retention', instance_id=instance, request_id=request_id, sha256=expected)
    except Exception as exc:
        try:
            client.request('runner_fail_retention', instance_id=instance, request_id=request_id, reason=str(exc)[:1000])
        except Exception:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    # Once committed, this staging byte sequence has a broker-owned immutable
    # copy.  Removing the runner-only temporary frees space without touching
    # the catalogue blob.
    try:
        target.unlink()
    except OSError:
        pass
    _prune_empty(target.parent, root / f'staged-{run_id}')


def remove_expired_staging(root: Path, records: object) -> None:
    if type(records) is not list:
        raise KitError('过期产物清理记录无效')
    for record in records:
        if type(record) is not dict or set(record) != {'run_id', 'path'}:
            raise KitError('过期产物清理记录无效')
        run_id, path = record['run_id'], record['path']
        if not isinstance(run_id, str) or not isinstance(path, str):
            raise KitError('过期产物清理记录无效')
        relative = Path(path)
        if relative.is_absolute() or '..' in relative.parts:
            raise KitError('过期产物清理路径无效')
        target = root / f'staged-{run_id}' / relative
        try:
            info = target.lstat()
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                target.unlink()
        except FileNotFoundError:
            pass
        _prune_empty(target.parent, root / f'staged-{run_id}')


def _prune_empty(directory: Path, stage: Path) -> None:
    while directory == stage or stage in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            break
        if directory == stage:
            break
        directory = directory.parent


def compact_staging(stage: Path, artifacts: list[dict[str, object]]) -> None:
    """Discard disposable code/runtime copies; keep only verified output bytes."""
    keep = {item['path'] for item in artifacts}
    for parent, directories, files in os.walk(stage, topdown=False, followlinks=False):
        for name in files:
            path = Path(parent)/name
            if path.relative_to(stage).as_posix() not in keep:
                path.unlink()
        for name in directories:
            try:
                (Path(parent)/name).rmdir()
            except OSError:
                pass
    _prune_empty(stage, stage)


def cleanup_orphan_staging(root: Path, inventory: object) -> None:
    if type(inventory) is not list or any(not isinstance(value,str) for value in inventory):
        raise KitError('暂存产物索引无效')
    keep = {f'staged-{value}' for value in inventory}
    for path in root.iterdir():
        if path.name.startswith('staged-') and path.name not in keep:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)


def execute(snapshot: Path, operation: str, *, code_root: Path, environment: Path,
            log, cancelled, timeout: float = 60, mounts=(), artifact_stage: Path | None = None,
            artifact_file_limit: int = 2 * 1024**2):
    # Only this one file is writable on the host. The trusted bootstrap sets a
    # hard 16 MiB RLIMIT_FSIZE before any submitted runtime/code is loaded.
    with tempfile.NamedTemporaryFile(prefix='output-', dir=snapshot.parent) as transfer:
        state, code, reason = _execute(snapshot, operation, code_root=code_root, environment=environment,
                                      log=log, cancelled=cancelled, timeout=timeout, transfer=Path(transfer.name), mounts=mounts,
                                      artifact_stage=artifact_stage, artifact_file_limit=artifact_file_limit)
        artifacts = {}
        try:
            transfer.seek(0)
            header = transfer.read(8)
            if header:
                size = int.from_bytes(header, 'big')
                if len(header) != 8 or size > 16 * 1024**2 - 8:
                    raise KitError('结果传输大小无效')
                payload = transfer.read(size)
                if len(payload) != size:
                    raise KitError('结果传输不完整')
                artifacts = _decode_files(json.loads(payload))
                from .web_jobs import _artifact_files
                artifacts, _ = _artifact_files(artifacts)
        except (KitError, ValueError, RecursionError) as exc:
            state, reason, artifacts = 'failed', '结果文件校验失败：' + str(exc)[:500], {}
        return state, code, reason, artifacts


def _execute(snapshot: Path, operation: str, *, code_root: Path, environment: Path,
             log, cancelled, timeout: float, transfer: Path, mounts=(), artifact_stage: Path | None = None,
             artifact_file_limit: int = 2 * 1024**2) -> tuple[str, int | None, str]:
    command = sandbox_command(snapshot, operation=operation, code_root=code_root, environment=environment,
                              transfer=transfer, mounts=mounts, artifact_stage=artifact_stage,
                              artifact_file_limit=artifact_file_limit)
    # bwrap's outside monitor can exit before its namespace init has reaped
    # descendants. --sync-fd is held by that init, not the submitted command.
    sync_read, sync_write = os.pipe()
    command[1:1] = ['--sync-fd', str(sync_write)]
    try:
        process = subprocess.Popen(command, env=ENV, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True,
                                   pass_fds=(sync_write, *(mount.fd for mount in mounts)))
    except BaseException:
        os.close(sync_read)
        raise
    finally:
        os.close(sync_write)
    reason, state = '', 'failed'
    total = 0
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    deadline = time.monotonic() + timeout
    def terminate():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    watchdog = threading.Timer(timeout, terminate)
    watchdog.daemon = True
    watchdog.start()
    next_poll = 0.0
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                state, reason = 'timed_out', '运行达到时长上限'
                break
            if now >= next_poll:
                if cancelled():
                    state, reason = 'cancelled', '用户取消'
                    break
                if transfer.stat().st_size > 16 * 1024**2:
                    state, reason = 'failed', '结果传输超过 16 MiB，已停止任务'
                    break
                if artifact_stage is not None:
                    try:
                        check_output_budget(artifact_stage, file_limit=artifact_file_limit)
                    except (KitError, OSError) as exc:
                        state, reason = 'failed', str(exc)[:500]
                        break
                next_poll = now + 1
            events = selector.select(min(0.2, max(0, deadline - now)))
            if events:
                chunk = os.read(process.stdout.fileno(), 16 * 1024)
                if not chunk:
                    code = process.wait(timeout=2)
                    if time.monotonic() >= deadline:
                        return 'timed_out', code, '运行达到时长上限'
                    tail = decoder.decode(b'', final=True)
                    if tail:
                        log(tail)
                    return ('succeeded' if code == 0 else 'failed', code,
                            '' if code == 0 else '隔离进程退出异常；请查看日志')
                available = max(0, LIMITS['log_bytes'] - total)
                text = decoder.decode(chunk[:available])
                if text:
                    log(text)
                total += len(chunk)
                if total > LIMITS['log_bytes']:
                    state, reason = 'failed', '运行日志超过 1 MiB，已停止任务'
                    break
        return state, None, reason
    finally:
        watchdog.cancel()
        watchdog.join()
        selector.close()
        # Killing the bwrap monitor terminates the PID namespace, including
        # descendants which made their own process groups or sessions.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            # Do not accept another job when termination cannot be confirmed.
            # Exiting the service lets systemd KillMode=control-group finish
            # cleanup before a replacement worker registers.
            raise SystemExit('无法确认隔离进程终止；执行服务退出等待 systemd 清理') from exc
        finally:
            process.stdout.close()
        try:
            ready, _, _ = select.select([sync_read], [], [], 5)
            if not ready or os.read(sync_read, 1) != b'':
                raise SystemExit('无法确认隔离命名空间退出；执行服务退出等待 systemd 清理')
        finally:
            os.close(sync_read)


def validate_job_identities(policy, job):
    """Live requester required; teacher may review an offline student's snapshot.

    No workspace is read and dataset grants are still checked for BOTH recorded
    owner and reviewer. An existing UID with another username is never accepted.
    """
    requester = policy.principal(job['requester_uid'])
    if requester.username != job['requester_username']:
        raise KitError('任务账号身份已变化，拒绝执行')
    try:
        owner = policy.principal(job['owner_uid'])
    except IdentityError:
        if requester.role != 'teacher':
            raise
        import pwd
        try:
            account = pwd.getpwuid(job['owner_uid'])
        except KeyError:
            return
        if account.pw_name != job['owner_username']:
            raise KitError('任务所属 UID 已被其他账号复用，拒绝执行')
    else:
        if owner.username != job['owner_username']:
            raise KitError('任务账号身份已变化，拒绝执行')


def execute_claimed(job, snapshot: Path, *, registry, code_root: Path,
                    environment: Path, log, cancelled, tree_registry=None,
                    derived_root: Path | None = None, artifact_stage: Path | None = None,
                    artifact_file_limit: int = 2 * 1024**2):
    """Resolve immutable metadata outside the sandbox, keeping open file pins."""
    principals = [Principal(job[f'{prefix}_uid'], job[f'{prefix}_username'],
                            'teacher' if job[f'{prefix}_username'] == 'ly' else 'student')
                  for prefix in ('owner', 'requester')]
    references = job['data_references']
    derived_descriptions = job.get('derived_mounts', [])
    if type(derived_descriptions) is not list:
        raise KitError('broker 派生数据挂载描述无效')
    derived_refs = {json.dumps(item.get('reference'), sort_keys=True, separators=(',', ':'))
                    for item in derived_descriptions if type(item) is dict}
    if len(derived_refs) != len(derived_descriptions):
        raise KitError('broker 派生数据挂载描述无效')
    tree_refs = [reference for reference in references if is_tree_reference(reference)]
    raw_refs = [reference for reference in references if not is_tree_reference(reference) and
                json.dumps(reference, sort_keys=True, separators=(',', ':')) not in derived_refs]
    trees = tree_registry if tree_registry is not None else TreeRegistry(None)

    def authorized_mounts():
        """Open raw/tree references against broker-issued per-ref principals."""
        mapping = job.get('dataset_authorizations')
        if mapping is None:
            # Historical queued jobs predate exact delegated-task grants.
            return [(reference, principals) for reference in raw_refs + tree_refs]
        if type(mapping) is not list:
            raise KitError('broker 数据集授权映射无效')
        requested = raw_refs + tree_refs
        expected = {json.dumps(reference, sort_keys=True, separators=(',', ':')) for reference in requested}
        issued: dict[str, list[Principal]] = {}
        for item in mapping:
            if type(item) is not dict or set(item) != {'reference', 'principals'}:
                raise KitError('broker 数据集授权映射无效')
            try:
                checked = _validate_data_references([item['reference']])
            except KitError as exc:
                raise KitError('broker 数据集授权引用无效') from exc
            if len(checked) != 1:
                raise KitError('broker 数据集授权引用无效')
            reference = checked[0]
            key = json.dumps(reference, sort_keys=True, separators=(',', ':'))
            if key not in expected or key in issued or not isinstance(item['principals'], list) or not item['principals']:
                raise KitError('broker 数据集授权映射与任务引用不一致')
            values: list[Principal] = []
            identities: set[tuple[int, str]] = set()
            for value in item['principals']:
                if type(value) is not dict or set(value) != {'uid', 'username', 'role'}:
                    raise KitError('broker 数据集授权主体无效')
                principal = Principal(value['uid'], value['username'], value['role'])
                if (isinstance(principal.uid, bool) or not isinstance(principal.uid, int) or principal.uid < 1 or
                        not isinstance(principal.username, str) or not principal.username or
                        principal.role not in {'teacher', 'student'} or
                        (principal.role == 'teacher') != (principal.username == 'ly') or
                        (principal.uid, principal.username) in identities):
                    raise KitError('broker 数据集授权主体无效')
                identities.add((principal.uid, principal.username)); values.append(principal)
            issued[key] = values
        if set(issued) != expected:
            raise KitError('broker 数据集授权映射与任务引用不一致')
        return [(reference, issued[json.dumps(reference, sort_keys=True, separators=(',', ':'))])
                for reference in requested]

    with contextlib.ExitStack() as stack:
        raw_mounts, tree_mounts = [], []
        for reference, authorized_principals in authorized_mounts():
            if is_tree_reference(reference):
                tree_mounts.extend(stack.enter_context(trees.open_references(authorized_principals, [reference], cancelled=cancelled)))
            else:
                raw_mounts.extend(stack.enter_context(registry.open_references(authorized_principals, [reference], cancelled=cancelled)))
        derived_mounts = stack.enter_context(DerivedDatasets.open_claimed_mounts(derived_root, derived_descriptions))
        mounts = [*raw_mounts, *tree_mounts, *derived_mounts]
        if mounts:
            log('受控外部数据：已核验当前授权；仅挂载固定版本（只读）。\n')
        result = execute(snapshot, job['operation'], code_root=code_root, environment=environment,
                         log=log, cancelled=cancelled, mounts=mounts, artifact_stage=artifact_stage,
                         artifact_file_limit=artifact_file_limit)
        if artifact_stage is None:
            return result
        return (*result, staged_artifacts(artifact_stage, file_limit=artifact_file_limit))


def serve(policy_path: Path, *, check: bool = False) -> None:
    policy = load_policy(policy_path)
    validate_policy(policy, validate_workspaces=False)
    _validate_trusted_code_root(policy)
    _validate_broker_interpreter()
    uid = os.getuid()
    if uid != os.geteuid() or uid != policy.runner_uid or uid in {0, policy.broker_uid}:
        raise KitError('执行服务必须由策略中独立的非登录执行账号启动')
    verify_cgroup()
    environment = policy.code_root.parent / 'venv'
    # StateDirectory is private and owned by this system service; no workspace
    # or submission-store directory is accepted from a caller.
    root = Path('/var/lib/researchkit-runner')
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise KitError('执行服务临时目录所有权或权限无效')
    with (root / 'worker.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Previous process crashes can leave only broker-supplied input copies.
        # The sandbox never sees this host directory or the worker lock.
        for stale in root.iterdir():
            if stale.name.startswith(('job-', 'probe-', 'output-')):
                if stale.is_symlink():
                    stale.unlink()
                elif stale.is_dir():
                    shutil.rmtree(stale)
                elif stale.is_file():
                    stale.unlink()
        with tempfile.TemporaryDirectory(prefix='probe-', dir=root) as temporary:
            probe = subprocess.run(sandbox_command(Path(temporary), operation='check', code_root=policy.code_root,
                                                   environment=environment, probe=True), env=ENV,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)
            if json.loads(probe.stdout).get('isolated') is not True:
                raise KitError('执行隔离自检未通过')
        if check:
            print(json.dumps({'ready': True, 'execution_uid': uid, 'limits': LIMITS}))
            return
        client = BrokerClient(policy.broker_socket, policy.broker_uid)
        instance = uuid.uuid4().hex
        client.request('runner_start', instance_id=instance)
        cleanup_orphan_staging(root, client.request('runner_staging_inventory', instance_id=instance))
        notify_ready()
        while True:
            job = client.request('runner_claim', instance_id=instance)
            if job is None:
                expired = client.request('runner_claim_expired_staging', instance_id=instance)
                remove_expired_staging(root, expired)
                retention = client.request('runner_claim_retention', instance_id=instance)
                if retention is not None:
                    try:
                        retain_staged_artifact(client, instance, root, retention)
                    except Exception:
                        # The broker has already recorded the specific request
                        # as failed; keep the worker alive for later jobs.
                        pass
                    continue
                time.sleep(2)
                continue
            run_id = job['run_id']
            stage = root / f'staged-{run_id}'
            try:
                validate_job_identities(policy, job)
                with tempfile.TemporaryDirectory(prefix='job-', dir=root) as temporary:
                    snapshot = Path(temporary)
                    materialize(snapshot, job['files'])
                    stage.mkdir(mode=0o700)
                    def append(value):
                        client.request('runner_log', instance_id=instance, run_id=run_id, text=value)
                    def cancelled():
                        return client.request('runner_poll', instance_id=instance, run_id=run_id)['cancel_requested']
                    artifact_limit = (policy.derived_max_file_bytes if policy.derived_dataset_root is not None
                                      else 2 * 1024**2)
                    state, code, reason, artifacts, staged = execute_claimed(
                        job, snapshot, registry=DatasetRegistry(policy.dataset_registry),
                        tree_registry=TreeRegistry(policy.dataset_tree_registry),
                        derived_root=policy.derived_dataset_root,
                        code_root=policy.code_root, environment=environment, log=append, cancelled=cancelled,
                        artifact_stage=stage, artifact_file_limit=artifact_limit)
            except DatasetCancelled as exc:
                state, code, reason, artifacts, staged = 'cancelled', None, str(exc), {}, []
            except DatasetTimedOut as exc:
                state, code, reason, artifacts, staged = 'timed_out', None, str(exc), {}, []
            except Exception as exc:
                state, code, reason = 'failed', None, str(exc)[:1000]
                artifacts, staged = {}, []
            if state != 'succeeded':
                staged = []
                if stage.exists():
                    shutil.rmtree(stage)
            else:
                try:
                    compact_staging(stage, staged)
                except OSError as exc:
                    state, reason, artifacts, staged = 'failed', '产物暂存清理失败：' + str(exc)[:500], {}, []
                    shutil.rmtree(stage)
            client.request('runner_finish', instance_id=instance, run_id=run_id,
                           status=state, exit_code=code, reason=reason, artifacts=artifacts,
                           staged_artifacts=staged)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--policy', type=Path, default=Path('/etc/researchkit-web/policy.json'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    serve(args.policy, check=args.check)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
