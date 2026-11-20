from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml


class KitError(RuntimeError):
    """An actionable user-facing error."""


SKIP = {'.git', '__pycache__', '.pytest_cache', '_work', '_runs', '.venv', 'venv'}


def inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name:
        raise KitError(f'路径必须是项目内的相对路径：{name!r}')
    rel = Path(name)
    if rel.is_absolute() or '..' in rel.parts:
        raise KitError(f'路径越界：{name}')
    path = root / rel
    if not inside(path, root):
        raise KitError(f'路径越界：{name}')
    return path


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', value):
        raise KitError(f'名称只能包含字母、数字、点、下划线和连字符：{value!r}')
    return value


def reference(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or '@' not in value:
        raise KitError(f'请使用 名称@版本：{value}')
    name, version = value.rsplit('@', 1)
    for part in name.split('/'):
        identifier(part)
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise KitError(f'请指定精确版本 x.y.z：{value}')
    return name, version


def read_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        raise KitError(f'无法读取 {path.name}: {exc}') from exc
    if not isinstance(value, dict):
        raise KitError(f'{path.name} 顶层必须是映射')
    return value


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise KitError(f'无法读取 {path.name}: {exc}') from exc
    if not isinstance(value, dict):
        raise KitError(f'{path.name} 顶层必须是映射')
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.rk-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_yaml(path: Path, value) -> None:
    atomic_text(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True))


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n')


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def snapshot(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    result = {}
    excluded = exclude or set()
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for name in dirs + names:
            p = Path(current) / name
            if p.is_symlink():
                raise KitError(f'请将软链接改成文件副本或本地数据映射：{p.relative_to(root)}')
        for name in sorted(names):
            p = Path(current) / name
            rel = p.relative_to(root).as_posix()
            if name.endswith(('.pyc', '.pyo')) or any(rel == x or rel.startswith(x + '/') for x in excluded):
                continue
            if not p.is_file():
                raise KitError(f'不支持特殊文件：{rel}')
            result[rel] = sha256(p)
    return result



def reject_links(root: Path) -> None:
    """Check the tree without reading large datasets during static checks."""
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [x for x in dirs if x not in SKIP]
        for name in dirs + names:
            path = Path(current) / name
            if path.is_symlink():
                raise KitError(f'请将软链接改成文件副本或本地数据映射：{path.relative_to(root)}')


def file_stats(root: Path, *, exclude: set[str] | None = None) -> dict[str, list[int]]:
    """Cheap change detector; hashes are computed only if these stats differ."""
    excluded = exclude or set()
    result = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for name in sorted(names):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if name.endswith(('.pyc', '.pyo')) or any(rel == x or rel.startswith(x + '/') for x in excluded):
                continue
            if path.is_symlink() or not path.is_file():
                raise KitError(f'不支持软链接或特殊文件：{rel}')
            stat = path.stat()
            result[rel] = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    return result


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def copy_tree(source: Path, target: Path, *, exclude: set[str] | None = None) -> None:
    records = snapshot(source, exclude=exclude)
    target.mkdir(parents=True, exist_ok=True)
    for rel in records:
        dest = safe_path(target, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dest)


@contextlib.contextmanager
def staging(target: Path):
    target = target.resolve()
    if target.exists():
        raise KitError(f'目标已存在，请换一个新名称：{target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.rk-', dir=target.parent))
    try:
        yield temporary
        # A competing writer must not cause replacement of an existing directory.
        if target.exists():
            raise KitError(f'目标已存在：{target}')
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
