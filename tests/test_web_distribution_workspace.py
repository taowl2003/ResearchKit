"""Distribution materialization tests use current OS UID, not two real student accounts."""
import os
import pwd
import shutil
import tempfile
from pathlib import Path

import pytest

from researchkit.files import KitError
from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace


@pytest.fixture
def workspace():
    if pwd.getpwuid(os.getuid()).pw_name != 'ly':
        pytest.skip('self-distribution fixture needs the configured teacher ly')
    root = Path(tempfile.mkdtemp(prefix='.rk-distribution-', dir=Path.cwd()))
    root.chmod(0o700)
    principal = Principal(os.getuid(), 'ly', 'teacher')
    store = SubmissionStore(root / 'store')
    value = Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))
    try:
        yield value
    finally:
        shutil.rmtree(root)


def assigned(workspace):
    project = workspace.create_project('teacher baseline')
    frozen = workspace.submit(project['project_id'], 'minor')
    distribution = workspace.create_distribution(frozen['submission_id'], 'ly')
    return project, frozen, distribution


def test_claim_fixed_baseline_and_retry_preserves_ide_edits(workspace):
    original, frozen, distribution = assigned(workspace)
    expected = (Path(original['path']) / 'case.py').read_bytes()
    (Path(original['path']) / 'case.py').write_text('teacher changed after sending\n')
    claimed = workspace.claim_distribution(distribution['distribution_id'])
    assert claimed['source'] == 'teacher-assigned'
    assert claimed['baseline_submission_id'] == frozen['submission_id']
    assert (Path(claimed['path']) / 'case.py').read_bytes() == expected
    case = Path(claimed['path']) / 'case.py'
    case.write_text('def broken(:\n# own edit\n')
    repeated = workspace.claim_distribution(distribution['distribution_id'])
    assert repeated['project_id'] == claimed['project_id']
    assert case.read_text() == 'def broken(:\n# own edit\n'
    submitted = workspace.submit(claimed['project_id'], 'minor')
    assert submitted['version'] == '0.1'
    difference = workspace.diff_baseline(submitted['submission_id'], 'case.py')
    assert '+def broken(' in difference['diff']
    assert difference['baseline_submission_id'] == frozen['submission_id']
    assert workspace.list_distributions()[0]['status'] == 'claimed'


def test_retry_after_registry_write_failure_recovers_exact_files(workspace, monkeypatch):
    _project, _frozen, distribution = assigned(workspace)
    original = workspace._save_state
    with monkeypatch.context() as patch:
        patch.setattr(workspace, '_save_state', lambda _state: (_ for _ in ()).throw(OSError('simulated disk failure')))
        with pytest.raises(OSError):
            workspace.claim_distribution(distribution['distribution_id'])
    assert workspace.list_distributions()[0]['status'] == 'claiming'
    recovered = workspace.claim_distribution(distribution['distribution_id'])
    assert recovered['project_id']
    assert workspace.list_distributions()[0]['status'] == 'claimed'
    assert len(list(workspace.projects_root.glob('assigned-*'))) == 1


def test_retry_after_confirmation_failure_does_not_replace_edits(workspace, monkeypatch):
    _project, _frozen, distribution = assigned(workspace)
    request = workspace.store.request
    def fail_confirm(method, **params):
        if method == 'confirm_distribution':
            raise KitError('simulated connection loss')
        return request(method, **params)
    with monkeypatch.context() as patch:
        patch.setattr(workspace.store, 'request', fail_confirm)
        with pytest.raises(KitError, match='connection loss'):
            workspace.claim_distribution(distribution['distribution_id'])
    record = workspace.list_distributions()[0]
    project_path = workspace._project_path(record['project_id'])
    (project_path / 'case.py').write_text('preserve this edit\n')
    recovered = workspace.claim_distribution(distribution['distribution_id'])
    assert (Path(recovered['path']) / 'case.py').read_text() == 'preserve this edit\n'


def test_unregistered_modified_destination_is_not_overwritten(workspace, monkeypatch):
    _project, _frozen, distribution = assigned(workspace)
    with monkeypatch.context() as patch:
        patch.setattr(workspace, '_save_state', lambda _state: (_ for _ in ()).throw(OSError('registry failed')))
        with pytest.raises(OSError):
            workspace.claim_distribution(distribution['distribution_id'])
    path = next(workspace.projects_root.glob('assigned-*')) / 'case.py'
    path.write_text('must survive\n')
    with pytest.raises(KitError, match='已有修改'):
        workspace.claim_distribution(distribution['distribution_id'])
    assert path.read_text() == 'must survive\n'


def test_claim_rejects_existing_destination_link_and_bad_snapshot_paths(workspace):
    import uuid
    project_id = str(uuid.uuid4())
    link = workspace.projects_root / ('assigned-' + project_id)
    link.symlink_to(workspace.root, target_is_directory=True)
    with pytest.raises(KitError, match='不安全'):
        workspace._materialize_distribution(project_id, {'case.py': b'no'})
    with pytest.raises(KitError, match='路径'):
        workspace._materialize_distribution(str(uuid.uuid4()), {'../escape': b'no'})
    assert not (workspace.root / 'escape').exists()


def test_self_created_has_no_fabricated_baseline(workspace):
    project = workspace.create_project('ordinary')
    (Path(project['path']) / 'project.yaml').write_text('origin: {kind: teacher-assigned, distribution_id: fake}\n')
    submission = workspace.submit(project['project_id'], 'minor')
    assert submission['project']['source'] == 'self-created'
    with pytest.raises(KitError, match='没有可信'):
        workspace.diff_baseline(submission['submission_id'], 'project.yaml')


def test_registered_missing_directory_is_not_recreated_on_retry(workspace):
    _project, _frozen, distribution = assigned(workspace)
    claimed = workspace.claim_distribution(distribution['distribution_id'])
    path = Path(claimed['path'])
    moved = path.with_name(path.name + '-moved-in-ide')
    path.rename(moved)
    with pytest.raises(KitError):
        workspace.claim_distribution(distribution['distribution_id'])
    assert not path.exists()
    assert (moved / 'case.py').is_file()


def test_fixed_baseline_diff_handles_added_deleted_and_binary_files(workspace):
    _original, frozen, distribution = assigned(workspace)
    claimed = workspace.claim_distribution(distribution['distribution_id'])
    path = Path(claimed['path'])
    (path / 'README.md').unlink()
    (path / 'new.py').write_text('# added by recipient\n')
    (path / 'binary.dat').write_bytes(b'\x00\xff')
    submitted = workspace.submit(claimed['project_id'], 'minor')
    (path / 'new.py').write_text('# unsubmitted IDE change\n')
    added = workspace.diff_baseline(submitted['submission_id'], 'new.py')
    assert '+# added by recipient' in added['diff']
    assert 'unsubmitted' not in added['diff']
    deleted = workspace.diff_baseline(submitted['submission_id'], 'README.md')
    assert '-# teacher baseline' in deleted['diff']
    assert deleted['baseline_submission_id'] == frozen['submission_id']
    with pytest.raises(KitError, match='二进制'):
        workspace.diff_baseline(submitted['submission_id'], 'binary.dat')
    with pytest.raises(KitError, match='均没有'):
        workspace.diff_baseline(submitted['submission_id'], 'missing.py')


def test_parallel_workspace_claims_share_one_directory(workspace):
    from concurrent.futures import ThreadPoolExecutor
    _project, _frozen, distribution = assigned(workspace)
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: workspace.claim_distribution(distribution['distribution_id']), range(4)))
    assert len({claim['project_id'] for claim in claims}) == 1
    assert len(list(workspace.projects_root.glob('assigned-*'))) == 1
    assert len(workspace.list_projects()) == 2
