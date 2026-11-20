"""Trusted bootstrap, invoked only inside the disposable execution namespace."""
from __future__ import annotations

import json
import base64
import os
import resource
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _artifact_file_limit() -> int:
    raw = os.environ.get('RESEARCHKIT_ARTIFACT_FILE_LIMIT', str(2 * 1024**2))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError('结果文件大小上限无效') from exc
    if not 1 <= value <= 32 * 1024**3:
        raise ValueError('结果文件大小上限无效')
    return value


def export_artifacts(root: Path) -> None:
    """Treat project outputs as untrusted files, never follow output links."""
    records, total = {}, 0
    # Only the legacy small-result channel uses base64.  Larger files stay in
    # runner staging and are offered for explicit streaming retention later.
    transfer_file_limit = 2 * 1024**2
    staged_results = os.environ.get('RESEARCHKIT_ALLOW_STAGED_RESULTS') == '1'
    omitted = False
    for name in ('results', 'report'):
        folder = root / name
        if not folder.exists() and not folder.is_symlink():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('结果目录必须是普通目录')
        for parent, directories, files in os.walk(folder, followlinks=False):
            if any((Path(parent) / child).is_symlink() for child in directories):
                raise ValueError('结果不能包含目录链接')
            for name in files:
                path = Path(parent) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('结果只能是普通文件')
                if len(records) >= 64:
                    raise ValueError('结果文件超过 64 个')
                # Large outputs remain in the runner-owned staging bind.  They
                # are listed and retained later through a separate streaming
                # protocol, never base64-packed into this small-artifact frame.
                if info.st_size > transfer_file_limit:
                    omitted = True
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError('结果文件在读取时变化')
                    data = stream.read(transfer_file_limit + 1)
                if len(data) > transfer_file_limit:
                    raise ValueError('结果文件过大')
                if total + len(data) > 8 * 1024**2:
                    if staged_results:
                        omitted = True
                        continue
                    raise ValueError('结果文件总计超过 8 MiB')
                total += len(data)
                records[path.relative_to(root).as_posix()] = base64.b64encode(data).decode('ascii')
    if omitted:
        print('结果 ZIP 仅包含容量内小文件；全部可保留产物见服务器暂存清单。', flush=True)
    value = json.dumps(records, ensure_ascii=True).encode('utf-8')
    if len(value) > 16 * 1024**2 - 8:
        raise ValueError('结果传输体积超过上限')
    with open('/transfer', 'r+b') as stream:
        stream.write(len(value).to_bytes(8, 'big') + value)
        stream.flush()


def main() -> int:
    # Limits are inherited by all subprocesses; the service cgroup provides
    # aggregate memory/process limits, including tmpfs and forked processes.
    artifact_file_limit = _artifact_file_limit()
    for kind, value in ((resource.RLIMIT_CPU, 30), (resource.RLIMIT_AS, 512 * 1024**2),
                        (resource.RLIMIT_FSIZE, max(16 * 1024**2, artifact_file_limit)), (resource.RLIMIT_NOFILE, 64),
                        (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(kind, (value, value))
    operation = sys.argv[1]
    if operation not in {'check', 'run'}:
        raise ValueError('invalid execution operation')
    root = Path('/work/project')
    shutil.copytree('/snapshot', root, dirs_exist_ok=True)
    if (root / '.researchkit/local.json').exists():
        raise ValueError('受限运行不接受 local.json 外部路径映射；请移除后重新提交')
    # Static inspection always uses the installed, trusted implementation.
    from researchkit import model
    model.project(root)
    model.verify_lock(root)
    model.environment(model.project(root))
    if operation == 'check':
        print(json.dumps(model.doctor(root), ensure_ascii=False, default=str), flush=True)
        return 0
    runtime = root / '.researchkit/runtime'
    if not (runtime / 'researchkit/engine.py').is_file():
        raise ValueError('固定提交缺少可执行的固定 runtime')
    # The submitted runtime is untrusted code too. A fresh interpreter loads
    # that exact version, entirely inside the same resource-limited sandbox.
    program = ('import sys; from pathlib import Path; '
               'sys.path.insert(0, "/work/project/.researchkit/runtime"); '
               'from researchkit.engine import _worker; '
               'raise SystemExit(_worker(Path("/work/project"), Path("/work/outcome.json")))')
    result = subprocess.run([sys.executable, '-I', '-u', '-c', program], cwd=root, check=False)
    export_artifacts(root)
    path = Path('/work/outcome.json')
    if path.is_file():
        with path.open('rb') as stream:
            value = stream.read(64 * 1024 + 1)
        if len(value) > 64 * 1024:
            raise ValueError('运行返回结果超过 64 KiB')
        print('项目返回结果（由项目代码提供）：', flush=True)
        print(value.decode('utf-8', errors='replace'), flush=True)
    return result.returncode if result.returncode >= 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
