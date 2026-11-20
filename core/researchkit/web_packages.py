"""Bounded, in-memory ZIP codec for Web project transfer.

The codec treats every archive as untrusted bytes.  It only returns bytes and
metadata; it never creates files, parses a project, imports code, or runs it.
"""
from __future__ import annotations

import io
import json
import re
import stat
import struct
import zipfile
from typing import Any

from .exchange import _archive_names
from .files import KitError
from .web_store import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, _validate_files

BUNDLE_PATH = '.researchkit/web-bundle.json'
MAX_BUNDLE_BYTES = 4 * 1024
# A valid 64 MiB input set plus zip local/central headers fits comfortably in
# this bound while malformed archives cannot make the byte buffer unbounded.
MAX_PACKAGE_BYTES = MAX_TOTAL_BYTES + 4 * 1024 * 1024
_BUNDLE_ID = re.compile(r'[0-9a-f]{32}\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def _fail(message: str) -> KitError:
    return KitError(message)


def _bundle(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != {'schema', 'bundle_id', 'baseline_sha256'}:
        raise _fail('Web 包身份元数据格式无效')
    schema = value['schema']
    bundle_id = value['bundle_id']
    baseline = value['baseline_sha256']
    if (schema != 'researchkit.web-bundle/v1' or not isinstance(bundle_id, str) or
            not _BUNDLE_ID.fullmatch(bundle_id) or not isinstance(baseline, str) or
            not _SHA256.fullmatch(baseline)):
        raise _fail('Web 包身份元数据无效')
    return {'schema': schema, 'bundle_id': bundle_id, 'baseline_sha256': baseline}


def _bundle_bytes(bundle: dict[str, Any]) -> bytes:
    value = json.dumps(_bundle(bundle), ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(value) > MAX_BUNDLE_BYTES:
        raise _fail('Web 包身份元数据超过大小上限')
    return value


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int) -> bytes:
    """Read one member with actual-byte enforcement and CRC validation."""
    try:
        with archive.open(info) as source:
            chunks: list[bytes] = []
            total = 0
            while True:
                block = source.read(min(1024 * 1024, limit + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise _fail(f'Web 包文件超过大小上限：{info.filename!r}')
                chunks.append(block)
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise _fail(f'Web 包损坏或无法读取：{exc}') from exc
    if total != info.file_size:
        raise _fail(f'Web 包文件大小与目录不一致：{info.filename!r}')
    return b''.join(chunks)


def _wrapped(infos: list[zipfile.ZipInfo]) -> str | None:
    """Return the one removable root directory, if regular files share it."""
    regular = [info.filename.rstrip('/') for info in infos if not info.is_dir()]
    if not regular:
        raise _fail('Web 包不含文件')
    if 'project.yaml' in regular:
        return None
    first = {name.split('/', 1)[0] for name in regular if '/' in name}
    if len(first) == 1 and all('/' in name for name in regular):
        root = next(iter(first))
        if f'{root}/project.yaml' in regular:
            return root
    if len(first) > 1 or regular:
        raise _fail('Web 包根目录不明确或缺少 project.yaml')
    return None


def _portable_name(info: zipfile.ZipInfo, root: str | None) -> str:
    name = info.filename.rstrip('/')
    if root is None:
        return name
    prefix = root + '/'
    if not name.startswith(prefix):
        raise _fail('Web 包包含多个顶层目录')
    result = name[len(prefix):]
    if not result:
        raise _fail('Web 包目录项无效')
    return result


def _unicode_names(infos: list[zipfile.ZipInfo]) -> None:
    for info in infos:
        try:
            info.filename.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise _fail('Web 包路径不是有效 UTF-8') from exc
        if any(ord(char) < 32 or ord(char) == 127 for char in info.filename):
            raise _fail('Web 包路径包含控制字符')


def _raw_names(data: bytes, infos: list[zipfile.ZipInfo]) -> None:
    """Inspect local-header names too: ZipInfo truncates embedded NUL names."""
    stream = io.BytesIO(data)
    for info in infos:
        stream.seek(info.header_offset)
        header = stream.read(zipfile.sizeFileHeader)
        if len(header) != zipfile.sizeFileHeader:
            raise _fail('Web 包本地文件头损坏')
        try:
            fields = struct.unpack(zipfile.structFileHeader, header)
        except struct.error as exc:
            raise _fail('Web 包本地文件头损坏') from exc
        name_size = fields[zipfile._FH_FILENAME_LENGTH]
        raw = stream.read(name_size)
        if len(raw) != name_size or b'\x00' in raw:
            raise _fail('Web 包路径包含非法原始名称')
        try:
            name = raw.decode('utf-8' if info.flag_bits & 0x800 else 'cp437')
        except UnicodeDecodeError as exc:
            raise _fail('Web 包路径不是有效 UTF-8') from exc
        if name != info.orig_filename:
            raise _fail('Web 包本地文件名与目录不一致')


def pack_package(files: dict[str, bytes], bundle: dict | None = None) -> bytes:
    """Pack already-approved project bytes into a deterministic Web ZIP."""
    if BUNDLE_PATH in files:
        raise _fail(f'项目文件不得占用 Web 包元数据路径：{BUNDLE_PATH}')
    packed, _manifest = _validate_files(files)
    records: dict[str, bytes] = dict(packed)
    if bundle is not None:
        records[BUNDLE_PATH] = _bundle_bytes(bundle)
    stream = io.BytesIO()
    try:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                             strict_timestamps=True) as archive:
            for path in sorted(records):
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                mode = 0o755 if path == 'task' else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, records[path], compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=6)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail(f'无法创建 Web 包：{exc}') from exc
    result = stream.getvalue()
    if len(result) > MAX_PACKAGE_BYTES:
        raise _fail('Web 包压缩后超过大小上限')
    return result


def unpack_package(data: bytes) -> tuple[dict[str, bytes], dict | None]:
    """Validate and decode an untrusted Web ZIP without touching disk."""
    if not isinstance(data, bytes):
        raise _fail('Web 包必须是 bytes')
    if len(data) > MAX_PACKAGE_BYTES:
        raise _fail('Web 包超过大小上限')
    try:
        raw_data = bytes(data)
        with zipfile.ZipFile(io.BytesIO(raw_data)) as archive:
            infos = _archive_names(archive, max_members=MAX_FILES + 1,
                                   max_member=MAX_FILE_BYTES,
                                   max_expanded=MAX_TOTAL_BYTES + MAX_BUNDLE_BYTES)
            _unicode_names(infos)
            _raw_names(raw_data, infos)
            root = _wrapped(infos)
            files: dict[str, bytes] = {}
            bundle_data: bytes | None = None
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                path = _portable_name(info, root)
                if path == BUNDLE_PATH:
                    if bundle_data is not None or info.file_size > MAX_BUNDLE_BYTES:
                        raise _fail('Web 包身份元数据重复或超过大小上限')
                    bundle_data = _read_member(archive, info, limit=MAX_BUNDLE_BYTES)
                    continue
                if path in files:
                    raise _fail(f'Web 包去除顶层目录后有重复文件：{path}')
                content = _read_member(archive, info, limit=MAX_FILE_BYTES)
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise _fail('Web 包文件总量超过上限')
                files[path] = content
    except KitError:
        raise
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise _fail(f'Web 包损坏或无法读取：{exc}') from exc
    if not files.get('project.yaml'):
        raise _fail('Web 包必须包含非空 project.yaml')
    checked, _manifest = _validate_files(files)
    bundle: dict | None = None
    if bundle_data is not None:
        try:
            bundle = _bundle(json.loads(bundle_data.decode('utf-8')))
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise _fail('Web 包身份元数据不是有效 JSON') from exc
    return checked, bundle
