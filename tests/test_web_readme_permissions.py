"""Admission tests on disposable synthetic trees, with no service or real data access."""
import errno
import os
from pathlib import Path
import stat
import struct

import pytest

from researchkit.files import KitError
from researchkit import web_data_publisher as publisher, web_dataset_permissions as permissions
from researchkit.web_dataset_trees import TreeRegistry
from test_web_readme_publication import state, scan


def acl_entries(raw):
    assert struct.unpack('<I', raw[:4])[0] == 2
    return [struct.unpack('<HHI', raw[i:i + 8]) for i in range(4, len(raw), 8)]


def queue(s, relative):
    row = next(r for r in scan(s) if r['relative_root'] == relative)
    assert row['kind'] == 'new'
    s.manager.review(s.teacher, row['candidate_id'], row['revision'], metadata={}, readers='platform-users')
    job = s.manager.worker_claim()
    assert job['kind'] == 'publish'
    return job


def test_receiving_root_grants_teacher_and_snapshot_restores(state):
    s = state
    before = permissions.snapshot_receiving_root(s.raw)
    teacher_uid = s.teacher.uid
    permissions.prepare_receiving_root(s.raw, teacher_uid)
    updated = permissions.snapshot_receiving_root(s.raw)
    assert updated['mode'] & stat.S_ISVTX
    entries = acl_entries(updated['acl'])
    assert (2, 7, teacher_uid) in entries
    assert {entry[2] for entry in entries if entry[0] == 2} <= {teacher_uid, s.raw.stat().st_uid}
    permissions.restore_receiving_root(s.raw, before)
    assert permissions.snapshot_receiving_root(s.raw) == before


def test_nested_candidate_seals_only_selected_unit(state):
    s = state
    gis = s.raw / 'GIS'; group = gis / 'group'; unit = group / 'unit'
    unit.mkdir(parents=True); (unit / 'README.md').write_text('资料说明')
    data = unit / 'data.csv'; data.write_text('a,b\n1,2\n')
    sibling = group / 'other'; sibling.mkdir(); other = sibling / 'outside.csv'; other.write_text('leave alone')
    other_before = (other.stat().st_ino, stat.S_IMODE(other.stat().st_mode), other.read_bytes())
    inode = data.stat().st_ino
    job = queue(s, 'GIS/group/unit')
    result = publisher.publish(s.policy, s.config, job['candidate'])
    assert result['dataset_id'] == job['candidate']['dataset_id']
    assert data.stat().st_ino != inode and data.read_text() == 'a,b\n1,2\n'
    for collection in (gis, group):
        current = permissions.snapshot_receiving_root(collection)
        assert current['mode'] & stat.S_ISVTX
        assert (2, 7, s.teacher.uid) in acl_entries(current['acl'])
    assert (other.stat().st_ino, stat.S_IMODE(other.stat().st_mode), other.read_bytes()) == other_before


@pytest.mark.parametrize('relative', ['../outside', '/absolute', 'x/../outside', 'x\\outside'])
def test_rejects_paths_outside_root(state, relative):
    s = state; outside = s.root / 'outside'; outside.write_text('unchanged'); before = outside.stat()
    with pytest.raises((KitError, OSError)):
        permissions.prepare_dataset(s.config, relative, 'ignored', teacher_uid=s.teacher.uid)
    assert outside.read_text() == 'unchanged'
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(before.st_mode)


def test_rejects_ancestor_symlink_without_touching_target(state):
    s = state; outside = s.root / 'outside'; outside.mkdir()
    (outside / 'README.md').write_text('outside'); file = outside / 'data'; file.write_text('unchanged')
    before = (file.stat().st_ino, stat.S_IMODE(file.stat().st_mode), file.read_bytes())
    (s.raw / 'shortcut').symlink_to(outside, target_is_directory=True)
    with pytest.raises((KitError, OSError)):
        permissions.prepare_dataset(s.config, 'shortcut', 'ignored', teacher_uid=s.teacher.uid)
    assert (file.stat().st_ino, stat.S_IMODE(file.stat().st_mode), file.read_bytes()) == before


def test_copy_failure_keeps_original_inode_without_registering(state, monkeypatch):
    s = state; data = s.unit / 'data.csv'; before = (data.stat().st_ino, data.read_bytes())
    job = queue(s, 'practice')
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, 'no reflink')
    def full(*args):
        raise OSError(errno.ENOSPC, 'disk full')
    monkeypatch.setattr(permissions.fcntl, 'ioctl', unsupported)
    monkeypatch.setattr(permissions.os, 'write', full)
    with pytest.raises(OSError) as error:
        publisher.publish(s.policy, s.config, job['candidate'])
    assert error.value.errno == errno.ENOSPC
    assert (data.stat().st_ino, data.read_bytes()) == before
    assert not list(s.unit.rglob('.researchkit-admit-*'))
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []


def test_hardlink_added_during_copy_does_not_change_external_inode_permissions(state, monkeypatch):
    s = state; data = s.unit / 'data.csv'; outside = s.root / 'race-link'
    original = (data.stat().st_ino, stat.S_IMODE(data.stat().st_mode), data.read_bytes())
    job = queue(s, 'practice')
    real_ioctl = permissions.fcntl.ioctl
    def race(output, command, source):
        if not outside.exists() and os.fstat(source).st_ino == original[0]:
            os.link(data, outside)
        return real_ioctl(output, command, source)
    monkeypatch.setattr(permissions.fcntl, 'ioctl', race)
    with pytest.raises((KitError, OSError)):
        publisher.publish(s.policy, s.config, job['candidate'])
    assert outside.exists()
    assert (outside.stat().st_ino, stat.S_IMODE(outside.stat().st_mode), outside.read_bytes()) == original
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []


def test_receiving_root_rejects_invalid_teacher_uid(state):
    for uid in (0, -1, True, '1000'):
        with pytest.raises(KitError, match='UID'):
            permissions.prepare_receiving_root(state.raw, uid)


def test_interrupted_preparation_file_is_not_admitted_as_data(state):
    s = state
    (s.unit / '.researchkit-admit-leftover').write_bytes(b'partial')
    row = next(r for r in scan(s) if r['relative_root'] == 'practice')
    assert row['kind'] == 'error'
    assert '临时文件' in row['error']
    assert TreeRegistry(s.policy.dataset_tree_registry).catalog(s.student) == []
