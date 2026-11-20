"""Bounded, descriptor-anchored discovery of README-marked dataset roots."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path

import yaml

from .files import KitError
from .web_dataset_metadata import normalize_tags
from .web_datasets import DatasetRegistry, _stable

MAX_DEPTH = 64
MAX_ENTRIES = 500000
MAX_CANDIDATES = 4096
MAX_README = 1024 * 1024
MAX_DESCRIPTION = 16384
MAX_FRONTMATTER = 64 * 1024
_TAG_KEYS = ('tags', '标签', 'labels', 'label')


def _frontmatter(text: str) -> tuple[str, list[str]]:
    """Return Markdown body and bounded, non-executable YAML frontmatter tags."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip('\r\n') != '---':
        return text, []
    end = next((index for index, line in enumerate(lines[1:], 1)
                if line.rstrip('\r\n') in {'---', '...'}), None)
    if end is None:
        raise KitError('README YAML frontmatter缺少结束标记 ---')
    raw = ''.join(lines[1:end])
    if len(raw.encode('utf-8')) > MAX_FRONTMATTER:
        raise KitError('README YAML frontmatter超过大小上限')
    try:
        document = yaml.safe_load(raw)
    except (yaml.YAMLError, RecursionError) as exc:
        raise KitError('README YAML frontmatter格式无效，请修正 tags') from exc
    if document is None:
        return ''.join(lines[end + 1:]), []
    if type(document) is not dict:
        raise KitError('README YAML frontmatter必须是对象')
    aliases = [key for key in _TAG_KEYS if key in document]
    if len(aliases) > 1:
        raise KitError('README frontmatter只能使用一个标签字段（tags 或 标签）')
    if not aliases:
        return ''.join(lines[end + 1:]), []
    value = document[aliases[0]]
    if isinstance(value, str):
        value = value.replace('，', ',').replace('、', ',').split(',')
    try:
        tags = normalize_tags(value, label='README tags')
    except KitError as exc:
        raise KitError(f'{exc}，请将 tags 写为字符串列表或逗号分隔字符串') from exc
    return ''.join(lines[end + 1:]), tags


def open_relative(root: Path, relative: Path) -> int:
    fd = DatasetRegistry._open_directory(root, allow_receiving=True)
    try:
        for part in relative.parts:
            if part in ('', '.', '..'):
                raise KitError('数据集相对路径无效')
            info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise KitError('数据集路径不是目录或包含链接')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if _stable(os.fstat(child)) != _stable(info):
                os.close(child)
                raise KitError('数据集路径在读取时变化')
            os.close(fd); fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def read_marker(fd: int, *, include_body: bool = False) -> dict[str, object] | None:
    names = [name for name in os.listdir(fd) if name.casefold() == 'readme.md']
    if not names:
        return None
    if len(names) != 1:
        raise KitError('同一目录有多个大小写不同的 README.md')
    name = names[0]
    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise KitError('README.md 必须是没有硬链接的普通文件')
    if before.st_size > MAX_README:
        raise KitError('README.md 超过大小上限')
    source = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    try:
        opened = os.fstat(source)
        if _stable(before) != _stable(opened):
            raise KitError('README.md 读取时变化')
        data = bytearray()
        while len(data) <= MAX_README:
            block = os.read(source, min(65536, MAX_README + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) > MAX_README or _stable(os.fstat(source)) != _stable(opened):
            raise KitError('README.md 读取时变化或超过大小上限')
    finally:
        os.close(source)
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise KitError('README.md 必须使用 UTF-8 编码') from exc
    body, tags = _frontmatter(text)
    raw_text = body.encode('utf-8')
    description = raw_text[:MAX_DESCRIPTION].decode('utf-8', errors='ignore')
    if len(raw_text) > MAX_DESCRIPTION:
        description = raw_text[:MAX_DESCRIPTION-128].decode('utf-8', errors='ignore') + '\n[README 较长，网页仅显示前 16 KiB；完整说明请查看原文件。]'
    result = {'description': description, 'tags': tags,
              'provenance_ref': f'README:{name}#sha256={hashlib.sha256(data).hexdigest()}'}
    if include_body:
        result['original'] = data.decode('utf-8')
    return result


def scan(root: Path, *, seconds: int, on_readme=None) -> dict[str, dict[str, object]]:
    """Stop at a marker; report invalid markers as one candidate error."""
    result: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + seconds
    count = 0
    root_fd = DatasetRegistry._open_directory(root, allow_receiving=True)
    def walk(fd: int, parts: tuple[str, ...]) -> None:
        nonlocal count
        if len(parts) > MAX_DEPTH or time.monotonic() > deadline:
            raise KitError('README 扫描超过层级或时间限制')
        if parts:
            relative = '/'.join(parts)
            try:
                marker = read_marker(fd, include_body=on_readme is not None)
            except (KitError, OSError) as exc:
                result[relative] = {'error': str(exc)[:1024]}
                return
            if marker is not None:
                if on_readme is not None:
                    on_readme(relative, marker['provenance_ref'], marker.pop('original'))
                result[relative] = marker
                if len(result) > MAX_CANDIDATES:
                    raise KitError('数据集候选过多')
                return
        before = os.fstat(fd)
        with os.scandir(fd) as stream:
            children = sorted(stream, key=lambda item: item.name)
        for child in children:
            if child.name.startswith('.researchkit-cleanup-'):
                continue  # Interrupted permanent cleanup is not a new dataset.
            count += 1
            if count > MAX_ENTRIES or time.monotonic() > deadline:
                raise KitError('README 扫描超过数量或时间限制')
            info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            sub = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if _stable(os.fstat(sub)) != _stable(info):
                    raise KitError('数据集路径在扫描时变化')
                walk(sub, parts + (child.name,))
            finally:
                os.close(sub)
        if _stable(os.fstat(fd)) != _stable(before):
            raise KitError('数据集目录在扫描时变化')
    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    return result


def marker_at(root: Path, relative: Path) -> dict[str, object]:
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise KitError('数据集相对路径无效')
    for depth in range(1, len(relative.parts) + 1):
        fd = open_relative(root, Path(*relative.parts[:depth]))
        try:
            marker = read_marker(fd)
        finally:
            os.close(fd)
        if marker is not None:
            if depth != len(relative.parts):
                raise KitError('上级目录已标记为数据集')
            return marker
    raise KitError('数据集目录缺少 README.md')
