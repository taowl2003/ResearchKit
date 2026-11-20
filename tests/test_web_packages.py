from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest

from researchkit.files import KitError
from researchkit import web_packages, web_store
from researchkit.web_packages import BUNDLE_PATH, pack_package, unpack_package


def _zip(records: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in records:
            archive.writestr(name, content)
    return stream.getvalue()


def _bundle() -> dict[str, str]:
    return {'schema': 'researchkit.web-bundle/v1', 'bundle_id': 'a' * 32,
            'baseline_sha256': 'b' * 64}


def test_pack_round_trip_preserves_binary_unicode_and_task_mode():
    files = {'project.yaml': b'schema: researchkit.project/v1\n', 'task': b'#!/bin/sh\n',
             '数据.bin': b'\x00\xff\x01'}
    data = pack_package(files, _bundle())
    unpacked, bundle = unpack_package(data)
    assert unpacked == files
    assert bundle == _bundle()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.getinfo('task').external_attr >> 16 & stat.S_IXUSR
        assert BUNDLE_PATH in archive.namelist()


def test_unpack_accepts_one_common_top_directory():
    data = _zip([('project/project.yaml', b'name: demo\n'), ('project/数据.bin', b'\x00\xff')])
    files, bundle = unpack_package(data)
    assert files == {'project.yaml': b'name: demo\n', '数据.bin': b'\x00\xff'}
    assert bundle is None


@pytest.mark.parametrize('bundle', [
    b'{}',
    json.dumps({'schema': 'wrong', 'bundle_id': 'a' * 32, 'baseline_sha256': 'b' * 64}).encode(),
    json.dumps({'schema': 'researchkit.web-bundle/v1', 'bundle_id': 'x', 'baseline_sha256': 'b' * 64}).encode(),
])
def test_rejects_malformed_web_bundle_metadata(bundle: bytes):
    data = _zip([('project.yaml', b'name: demo\n'), (BUNDLE_PATH, bundle)])
    with pytest.raises(KitError, match='元数据'):
        unpack_package(data)


@pytest.mark.parametrize('records', [
    [('project.yaml', b'x'), ('../escape', b'x')],
    [('project.yaml', b'x'), ('project.yaml', b'y')],
])
def test_rejects_unsafe_or_duplicate_zip_paths(records):
    with pytest.raises(KitError):
        unpack_package(_zip(records))


def test_rejects_link_special_names_and_reserved_metadata_path():
    link = zipfile.ZipInfo('link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(KitError, match='链接'):
        unpack_package(_zip([('project.yaml', b'x'), (link, b'target')]))
    with pytest.raises(KitError, match='元数据路径'):
        pack_package({'project.yaml': b'x', BUNDLE_PATH: b'{}'})


def test_rejects_embedded_nul_in_raw_zip_name():
    data = _zip([('project.yaml', b'x'), ('evil', b'x')]).replace(b'evil', b'a\x00il')
    with pytest.raises(KitError, match='原始名称'):
        unpack_package(data)


def test_small_limits_reject_member_and_total_without_large_allocations(monkeypatch):
    monkeypatch.setattr(web_packages, 'MAX_FILE_BYTES', 4)
    monkeypatch.setattr(web_packages, 'MAX_TOTAL_BYTES', 6)
    monkeypatch.setattr(web_store, 'MAX_FILE_BYTES', 4)
    monkeypatch.setattr(web_store, 'MAX_TOTAL_BYTES', 6)
    with pytest.raises(KitError, match='单个文件'):
        unpack_package(_zip([('project.yaml', b'hello')]))
    with pytest.raises(KitError, match='总量'):
        unpack_package(_zip([('project.yaml', b'abcd'), ('data.bin', b'cde')]))
