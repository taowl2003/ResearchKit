"""Personal list removal persists without touching grants, data, or projects."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile

import pytest

from researchkit.files import KitError
from researchkit.web_identity import Principal
from researchkit.web_resource_visibility import FILENAME
from researchkit.web_workspace import Workspace


class NoBrokerMutation:
    def request(self, *_args, **_kwargs):
        raise AssertionError('List preferences must never call the central broker')


@pytest.fixture
def workspaces():
    with tempfile.TemporaryDirectory(prefix='.rk-resource-visibility-', dir=Path.cwd()) as temp:
        root = Path(temp)
        teacher = Principal(os.getuid(), 'ly', 'teacher')
        student = Principal(os.getuid() + 1, 'test', 'student')
        yield [Workspace(p, root / p.username, NoBrokerMutation()) for p in (teacher, student)]


def test_personal_removal_restore_and_other_account_isolation(workspaces):
    owner, other = workspaces
    project = owner.projects_root / 'preserved'
    project.mkdir(mode=0o700)
    code = project / 'plugin.py'
    code.write_bytes(b'# existing project code\n')
    data = owner.root / 'retained-data.bin'
    data.write_bytes(b'original bytes')
    empty = {'hidden_algorithms': [], 'hidden_datasets': []}
    assert owner.personal_resource_visibility() == empty
    for ref in ('calibration.scale@1.0.0', 'calibration.scale@1.0.1'):
        owner.set_personal_resource_visibility({'kind': 'algorithm', 'ref': ref, 'hidden': True})
    expected = {'hidden_algorithms': ['calibration.scale@1.0.0', 'calibration.scale@1.0.1'],
                'hidden_datasets': ['old-private-data']}
    assert owner.set_personal_resource_visibility({'kind': 'dataset', 'ref': 'old-private-data', 'hidden': True}) == expected
    reloaded = Workspace(owner.principal, owner.root, NoBrokerMutation())
    assert reloaded.personal_resource_visibility() == expected
    assert other.personal_resource_visibility() == empty
    # Student accounts have the same personal organization ability as LY.
    other.set_personal_resource_visibility({'kind': 'dataset', 'ref': 'student-data', 'hidden': True})
    assert owner.personal_resource_visibility() == expected
    result = owner.set_personal_resource_visibility({'kind': 'algorithm', 'ref': 'calibration.scale@1.0.0', 'hidden': False})
    assert result['hidden_algorithms'] == ['calibration.scale@1.0.1']
    assert result['hidden_datasets'] == ['old-private-data']
    assert code.read_bytes() == b'# existing project code\n'
    assert data.read_bytes() == b'original bytes'
    assert (owner.root / FILENAME).stat().st_mode & 0o777 == 0o600


def test_concurrent_instances_do_not_lose_removals(workspaces):
    owner = workspaces[0]
    instances = [Workspace(owner.principal, owner.root, NoBrokerMutation()) for _ in range(12)]
    def remove(index):
        instances[index].set_personal_resource_visibility({'kind': 'dataset', 'ref': 'data-' + str(index), 'hidden': True})
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(remove, range(12)))
    assert set(owner.personal_resource_visibility()['hidden_datasets']) == {'data-' + str(i) for i in range(12)}


@pytest.mark.parametrize('payload', [
    {'kind': 'dataset', 'ref': '../other', 'hidden': True},
    {'kind': 'algorithm', 'ref': 'scale', 'hidden': True},
    {'kind': 'algorithm', 'ref': 'scale@latest', 'hidden': True},
    {'kind': 'dataset', 'ref': 'id', 'hidden': 'true'},
    {'kind': 'template', 'ref': 'case@1.0.0', 'hidden': True},
    {'kind': 'dataset', 'ref': 'id', 'hidden': True, 'username': 'other'},
])
def test_invalid_or_identity_supplied_payload_rejected(workspaces, payload):
    owner = workspaces[0]
    with pytest.raises(KitError):
        owner.set_personal_resource_visibility(payload)
    assert not (owner.root / FILENAME).exists()


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'mode', 'identity', 'corrupt'])
def test_unsafe_preferences_rejected_without_overwrite(workspaces, unsafe):
    owner, other = workspaces
    owner.set_personal_resource_visibility({'kind': 'dataset', 'ref': 'keep', 'hidden': True})
    path = owner.root / FILENAME
    if unsafe == 'symlink':
        outside = other.root / 'untouched'
        outside.write_bytes(b'outside')
        path.unlink()
        path.symlink_to(outside)
    elif unsafe == 'hardlink':
        os.link(path, other.root / 'linked')
    elif unsafe == 'mode':
        path.chmod(0o644)
    elif unsafe == 'identity':
        payload = json.loads(path.read_text())
        payload['username'] = 'somebody-else'
        path.write_text(json.dumps(payload))
    else:
        path.write_text('not json')
    before = path.read_bytes()
    with pytest.raises(KitError):
        owner.personal_resource_visibility()
    with pytest.raises(KitError):
        owner.set_personal_resource_visibility({'kind': 'dataset', 'ref': 'new', 'hidden': True})
    assert path.read_bytes() == before
