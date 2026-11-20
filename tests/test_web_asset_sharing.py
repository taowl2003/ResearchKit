"""Publication integrates with the existing exact-version sharing rules."""
from pathlib import Path
import shutil

import pytest

from researchkit.files import KitError
from researchkit.web_assets import AssetService
from researchkit.web_sharing import ComponentService
from test_web_sharing import sharing, safe_root, _teacher, _student, _policy


def source_submission(store):
    student = _student(2021, 'student-source')
    project = store.create_project(student, 'fixed publication source')
    root = Path(__file__).parents[1] / 'library/cases/hello/1.0.0'
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts}
    # Both definitions are valid static Python. Neither top-level statement
    # may execute during extraction, review, publication or component reads.
    files['case.py'] = b'raise RuntimeError("case must not execute")\n' + files['case.py']
    files['.researchkit/plugins/scale/plugin.py'] = b'raise RuntimeError("plugin must not execute")\n' + files['.researchkit/plugins/scale/plugin.py']
    return store.submit(project['project_id'], student, 'major', files, [])


def publish(assets, submission, kind, name):
    draft = assets.create(_teacher(), submission['submission_id'], kind, name, '1.0.0',
                          'scale' if kind == 'algorithm' else None)
    assets.review(_teacher(), draft['candidate_id'], draft['candidate_sha256'], '已人工核对示例来源、提取范围和静态报告。')
    assets.publish(_teacher(), draft['candidate_id'], draft['candidate_sha256'])
    return draft


def test_published_versions_reuse_existing_catalog_exact_acl_and_revocation(sharing):
    store, service, library = sharing
    assets = AssetService(store, library)
    source = source_submission(store)
    candidate = assets.create(_teacher(), source['submission_id'], 'algorithm', 'student.scale', '1.0.0', 'scale')
    assert candidate['ref'] not in {row['ref'] for row in service.catalog(_teacher())['plugins']}
    with pytest.raises(KitError):
        assets.publish(_teacher(), candidate['candidate_id'], candidate['candidate_sha256'])
    draft = publish(assets, source, 'algorithm', 'student.scale')
    alice, bob = _student(2031, 'alice'), _student(2032, 'bob')
    # A new service instance verifies persistence and private initial policy.
    service = ComponentService(store, library)
    assert draft['ref'] in {row['ref'] for row in service.catalog(_teacher())['plugins']}
    assert draft['ref'] not in {row['ref'] for row in service.catalog(alice)['plugins']}
    with pytest.raises(KitError, match='无权'):
        service.component(alice, draft['ref'])
    policy = _policy(service, draft['ref'])
    granted = service.set_access(_teacher(), 'algorithm', draft['ref'], False, [alice], policy['revision'])
    result = service.component(alice, draft['ref'])
    assert result['metadata']['name'] == 'student.scale'
    assert b'plugin must not execute' in result['files']['plugin/plugin.py']
    assert result['files']['contracts/scalar/1.0.0.yaml']
    with pytest.raises(KitError, match='无权'):
        service.component(bob, draft['ref'])
    with pytest.raises(KitError, match='无权'):
        service.component(_student(alice.uid, 'reused'), draft['ref'])
    service.set_access(_teacher(), 'algorithm', draft['ref'], False, [], granted['revision'])
    with pytest.raises(KitError, match='无权'):
        service.component(alice, draft['ref'])


def test_published_case_is_granted_as_fixed_template_and_later_cli_changes_are_ignored(sharing):
    store, service, library = sharing
    assets = AssetService(store, library)
    draft = publish(assets, source_submission(store), 'template', 'reviewed.case')
    student = _student(2033, 'case-reader')
    policy = _policy(service, draft['ref'], 'template')
    service.set_access(_teacher(), 'template', draft['ref'], False, [student], policy['revision'])
    files = service.template(student, draft['ref'])['files']
    assert 'case.py' in files and '.researchkit/plugins/scale/plugin.py' in files
    assert not any(path.startswith('.researchkit/runtime/') or path == 'task' for path in files)
    destination = library / 'cases/reviewed.case/1.0.0'
    shutil.copytree(library / 'cases/hello/1.0.0', destination)
    assert service.template(student, draft['ref'])['files'] == files
    assert any(row['ref'] == draft['ref'] for row in service.catalog(_teacher())['templates'])
    assert service.template(student, draft['ref'])['files'] == files
