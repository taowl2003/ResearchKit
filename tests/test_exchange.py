from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from researchkit.exchange import receive_submission, save_project, share_project, submit_project
from researchkit.files import KitError, digest, write_json, write_yaml
from researchkit import model
from researchkit.projects import install_runtime, new_project, seal_lock


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    home = tmp_path / 'kit home'
    home.mkdir()
    monkeypatch.setenv('RK_HOME', str(home))
    project = tmp_path / 'teacher case'
    project.mkdir()
    (project / 'main.py').write_text('print("hello")\n', encoding='utf-8')
    write_yaml(project / 'project.yaml', {
        'schema': 'researchkit.project/v1', 'name': 'hello', 'mode': 'command',
        'command': ['python', 'main.py'], 'plugins': {}, 'parameters': {},
        'data': {}, 'requires': {'python': '3.11', 'modules': []},
    })
    install_runtime(project)
    seal_lock(project)
    return home, project


def _replace_zip(source: Path, target: Path, changes: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as reader:
        records = {name: reader.read(name) for name in reader.namelist()}
    records.update(changes)
    manifest = json.loads(records['_submission.json'])
    manifest['files'] = {name: __import__('hashlib').sha256(data).hexdigest()
                         for name, data in records.items() if name != '_submission.json'}
    records['_submission.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as writer:
        for name, data in records.items():
            writer.writestr(name, data)


def test_portable_share_submit_receive_save(workspace, tmp_path, monkeypatch):
    home, teacher = workspace
    shared = share_project(teacher)
    assert Path(shared['baseline']).is_file()
    assert not (Path(shared['path']) / '.researchkit/last-run.json').exists()
    # The export is self contained and may be moved away from RK_HOME.
    student = tmp_path / 'offsite with spaces'
    Path(shared['path']).rename(student)
    (student / 'main.py').write_text('print("student")\n', encoding='utf-8')
    (student / 'algorithms/local.py').parent.mkdir()
    (student / 'algorithms/local.py').write_text('VALUE = 2\n', encoding='utf-8')
    monkeypatch.delenv('RK_HOME')
    submission = submit_project(student)
    monkeypatch.setenv('RK_HOME', str(home))
    assert Path(submission['path']).parent == student.parent
    assert submission['freshness']['status'] == 'not-run'
    # Changing the source after issue must not change the old baseline.
    (teacher / 'main.py').write_text('print("new teacher")\n', encoding='utf-8')
    received = receive_submission(Path(submission['path']))
    assert received['status'] == 'accepted'
    assert received['student_code_executed'] is False
    assert (Path(received['path']) / 'project/task').stat().st_mode & stat.S_IXUSR
    assert received['changed'] == ['main.py']
    assert received['added'] == ['algorithms/local.py']
    assert '学生代码执行：否' in Path(received['report']).read_text(encoding='utf-8')
    saved = save_project(Path(received['path']) / 'project', 'student-local')
    assert (Path(saved['path']) / 'algorithms/local.py').is_file()
    recreated = new_project(tmp_path / 'recreated', template='saved:student-local')
    assert (Path(recreated['path']) / 'algorithms/local.py').is_file()


def test_share_failure_has_no_issued_baseline(workspace, tmp_path):
    home, project = workspace
    (project / 'unsafe').symlink_to(tmp_path)
    target = home / 'exports' / 'should-not-exist'
    with pytest.raises(KitError):
        share_project(project, target)
    assert not target.exists()
    assert not list((home / 'library/bundles').glob('*/baseline.json'))


@pytest.mark.parametrize('kind', ['runtime', 'lock', 'bundle', 'unknown', 'manifest'])
def test_reject_tampering(workspace, tmp_path, kind):
    home, project = workspace
    shared = share_project(project)
    submission = submit_project(Path(shared['path']))
    source = Path(submission['path'])
    bad = tmp_path / f'{kind}.zip'
    if kind == 'runtime':
        path = '.researchkit/runtime/researchkit/model.py'
        _replace_zip(source, bad, {path: b'# changed\n'})
    elif kind == 'lock':
        _replace_zip(source, bad, {'.researchkit/lock.json': b'{}\n'})
    elif kind in ('bundle', 'unknown'):
        bundle = {'schema': 'researchkit.bundle/v1', 'id': '0' * 32, 'project': 'hello'}
        _replace_zip(source, bad, {'.researchkit/bundle.json': json.dumps(bundle).encode()})
    else:
        with zipfile.ZipFile(source) as reader, zipfile.ZipFile(bad, 'w') as writer:
            for name in reader.namelist():
                writer.writestr(name, b'{}' if name == '_submission.json' else reader.read(name))
    with pytest.raises(KitError):
        receive_submission(bad, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('kind', ['traversal', 'duplicate', 'symlink'])
def test_reject_unsafe_archive(workspace, tmp_path, kind):
    _, project = workspace
    shared = share_project(project)
    source = Path(submit_project(Path(shared['path']))['path'])
    bad = tmp_path / f'{kind}.zip'
    with zipfile.ZipFile(source) as reader, zipfile.ZipFile(bad, 'w') as writer:
        for name in reader.namelist():
            writer.writestr(name, reader.read(name))
        if kind == 'traversal':
            writer.writestr('../escape', b'x')
        elif kind == 'duplicate':
            writer.writestr('main.py', b'x')
        else:
            info = zipfile.ZipInfo('link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            writer.writestr(info, b'main.py')
    with pytest.raises(KitError):
        receive_submission(bad, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_output_must_not_overlap_source(workspace):
    _, project = workspace
    with pytest.raises(KitError, match='重叠'):
        share_project(project, project / 'export')
    shared = share_project(project)
    with pytest.raises(KitError, match='重叠'):
        submit_project(Path(shared['path']), Path(shared['path']) / 'submission.zip')


def test_submit_marks_changed_run_stale_without_executing_code(workspace, tmp_path):
    _, project = workspace
    shared = share_project(project)
    student = Path(shared['path'])
    marker = tmp_path / 'must-not-exist'
    write_json(student / '.researchkit/last-run.json', {
        'status': 'success', 'result': {'value': 1}, 'run_id': 'claimed',
        'input_hash': digest(model.input_files(student)), 'outputs': {},
    })
    (student / 'main.py').write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).write_text(\"executed\")\n',
        encoding='utf-8')
    submission = submit_project(student)
    assert submission['freshness']['status'] == 'stale'
    received = receive_submission(Path(submission['path']))
    assert received['claimed_freshness']['status'] == 'stale'
    assert not marker.exists()
