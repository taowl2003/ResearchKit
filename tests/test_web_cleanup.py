"""Permanent cleanup is confined to selected recycle-bin platform copies."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit.files import KitError
from researchkit.web_assets import AssetService
from researchkit.web_cleanup import Cleanup
from researchkit.web_derived import DerivedDatasets
from researchkit.web_identity import Principal
from researchkit.web_lifecycle import AssetLifecycle
from researchkit.web_store import SubmissionStore
from test_web_lifecycle import _asset_project_files

TEACHER=Principal(1001,'ly','teacher')
STUDENT=Principal(1002,'test','student')


@pytest.fixture
def setup():
    root=Path(tempfile.mkdtemp(prefix='.rk-purge-',dir=Path.cwd()))
    try:
        library=root/'library'
        for name in ('algorithms','contracts','cases'):(library/name).mkdir(parents=True)
        store=SubmissionStore(root/'store')
        derived=root/'derived';derived.mkdir(mode=0o700)
        policy=SimpleNamespace(derived_dataset_root=derived,dataset_registry=None,dataset_tree_registry=None,dataset_management_config=None)
        assets=AssetService(store,library)
        project=store.create_project(TEACHER,'test source')
        fixed=store.submit(project['project_id'],TEACHER,'minor',_asset_project_files(),[])
        cleanup=Cleanup(store,library,policy)
        yield SimpleNamespace(root=root,store=store,assets=assets,fixed=fixed,cleanup=cleanup,policy=policy,library=library)
    finally:shutil.rmtree(root)


def publish(s,kind,name,version='1.0.0'):
    candidate=s.assets.create(TEACHER,s.fixed['id'],kind,name,version,'scale' if kind=='algorithm' else None)
    s.assets.review(TEACHER,candidate['candidate_id'],candidate['candidate_sha256'],'测试审核')
    s.assets.publish(TEACHER,candidate['candidate_id'],candidate['candidate_sha256'])
    return {'kind':kind,'ref':name+'@'+version}


def trash(s,item):
    life=s.cleanup.lifecycle
    report=life.report(TEACHER,**item)
    return life.change(TEACHER,**item,action='trash',expected_revision=report['revision'])


def execute(s,items,plan=None):
    plan=plan or s.cleanup.preview(TEACHER,items)
    return s.cleanup.execute(TEACHER,items,plan['plan_token'],'永久清理')


@pytest.mark.parametrize('old_action,old_state', [('retire', 'retired'), ('trash', 'trashed')])
def test_hidden_legacy_case_must_be_manageable_and_deleted_before_algorithm(setup, old_action, old_state):
    from researchkit.web_sharing import ComponentService
    s = setup
    algorithm = publish(s, 'algorithm', 'scale')
    case = publish(s, 'template', 'calibration-template')
    before = s.cleanup.lifecycle.report(TEACHER, **case)
    s.cleanup.lifecycle.change(TEACHER, **case, action=old_action, expected_revision=before['revision'])
    sharing = ComponentService(s.store, s.library)
    # Student/template selection hides inactive resources, but the teacher's
    # management source must still expose their exact identity and state.
    assert sharing.catalog(TEACHER)['templates'] == []
    row = next(item for item in sharing.access(TEACHER, include_inactive=True)
               if item['kind'] == 'template' and item['ref'] == case['ref'])
    assert row['lifecycle']['state'] == old_state
    check = s.cleanup.check_cleanup_references(TEACHER, **algorithm)
    assert not check['can_delete']
    assert [item['ref'] for item in check['blocking_cases']] == [case['ref']]
    with pytest.raises(KitError, match='平台案例'):
        s.cleanup.direct_cleanup(TEACHER, **algorithm, confirmation='永久清理')
    # Only an administrator's explicit case deletion releases its dependency.
    s.cleanup.direct_cleanup(TEACHER, **case, confirmation='永久清理')
    assert not any(item['kind'] == 'template' and item['ref'] == case['ref']
                   for item in sharing.access(TEACHER, include_inactive=True))
    assert s.cleanup.check_cleanup_references(TEACHER, **algorithm)['can_delete']
    assert s.cleanup.direct_cleanup(TEACHER, **algorithm, confirmation='永久清理')['deleted']
    assert s.store.get_submission(s.fixed['id'], TEACHER)['files']['case.py']


def test_only_recycle_bin_can_be_permanently_cleaned_and_source_history_survives(setup):
    s=setup;item=publish(s,'algorithm','scale')
    preview=s.cleanup.preview(TEACHER,[item])
    assert not preview['can_execute']
    with pytest.raises(KitError):execute(s,[item],preview)
    trash(s,item)
    preview=s.cleanup.preview(TEACHER,[item])
    assert preview['can_execute'],preview
    assert preview['items'][0]['advisories']  # source snapshot has an independent copy
    outcome=execute(s,[item],preview)
    assert outcome['status']=='completed'
    assert not s.cleanup.list_bin(TEACHER)['items']
    with s.store._connection() as con:
        assert not con.execute('SELECT 1 FROM web_asset_published WHERE ref=?',(item['ref'],)).fetchone()
        assert not con.execute('SELECT 1 FROM web_asset_published_files WHERE ref=?',(item['ref'],)).fetchone()
        assert not con.execute('SELECT 1 FROM web_asset_candidates WHERE ref=?',(item['ref'],)).fetchone()
    source=s.store.get_submission(s.fixed['id'],TEACHER)
    assert source['files']['plugins/scale/plugin.py']==_asset_project_files()['plugins/scale/plugin.py']
    with pytest.raises(KitError,match='清理'):
        publish(s,'algorithm','scale')  # exact removed version cannot silently resurrect
    assert execute(s,[item],preview)['request_id']==outcome['request_id']


def test_reference_suggestions_require_explicit_selection_and_order_case_before_algorithm(setup):
    s=setup;algorithm=publish(s,'algorithm','scale');case=publish(s,'template','practice')
    trash(s,algorithm)
    preview=s.cleanup.preview(TEACHER,[algorithm])
    assert not preview['can_execute']
    blockers=preview['items'][0]['blockers']
    assert any(b.get('ref')==case['ref'] and '一起勾选' in b['suggestion'] for b in blockers),blockers
    # Referenced assets may enter a recoverable bin; purge still verifies the
    # complete selected closure and refuses to add live assets automatically.
    assert not s.cleanup.preview(TEACHER,[algorithm,case])['can_execute']
    trash(s,case)
    plan=s.cleanup.preview(TEACHER,[algorithm,case])
    assert plan['can_execute'],plan
    assert plan['order']==[case,algorithm]
    assert execute(s,[algorithm,case],plan)['status']=='completed'
    assert s.store.get_submission(s.fixed['id'],TEACHER)['files']['case.py']


def test_new_dependency_invalidates_a_confirmed_plan(setup):
    s=setup;algorithm=publish(s,'algorithm','scale');trash(s,algorithm)
    before=s.cleanup.preview(TEACHER,[algorithm]);assert before['can_execute']
    publish(s,'template','later.case')
    with pytest.raises(KitError,match='冲突'):
        execute(s,[algorithm],before)
    assert s.cleanup.lifecycle.report(TEACHER,**algorithm)['state']=='trashed'


def test_restored_item_invalidates_plan_and_confirmation_is_required(setup):
    s=setup;case=publish(s,'template','unused.case');report=trash(s,case)
    plan=s.cleanup.preview(TEACHER,[case])
    with pytest.raises(KitError,match='确认'):
        s.cleanup.execute(TEACHER,[case],plan['plan_token'],'yes')
    s.cleanup.lifecycle.change(TEACHER,**case,action='restore',expected_revision=report['revision'])
    with pytest.raises(KitError,match='冲突'):execute(s,[case],plan)


def test_role_fields_and_selection_cannot_expand_the_purge_scope(setup):
    s=setup;case=publish(s,'template','unused');trash(s,case)
    for operation in (lambda:s.cleanup.list_bin(STUDENT),lambda:s.cleanup.preview(STUDENT,[case]),
                      lambda:s.cleanup.execute(STUDENT,[case],'a'*64,'永久清理')):
        with pytest.raises(KitError,match='教师'):operation()
    for selection in ([case,case],[dict(case,path='/etc')],[],[{'kind':'dataset','ref':'../etc'}]):
        with pytest.raises(KitError):s.cleanup.preview(TEACHER,selection)


def upload(s):
    import hashlib
    service=DerivedDatasets(s.store,s.policy.derived_dataset_root)
    payload=b'1,2,3\n';digest=hashlib.sha256(payload).hexdigest()
    begun=service.begin_upload(TEACHER,'test derived',len(payload),digest)
    service.append_upload(TEACHER,begun['upload_id'],0,payload)
    record=service.commit_upload(TEACHER,begun['upload_id'],digest)
    service.publish(TEACHER,record['dataset_id'])
    with s.store._connection() as con:
        row=con.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?',(record['dataset_id'],)).fetchone()
    return {'kind':'dataset','ref':record['dataset_id']},record,service._blob_path(row['blob_name'])


def test_public_derived_blob_is_removed_but_referenced_data_is_blocked(setup):
    s=setup;item,record,blob=upload(s);trash(s,item)
    plan=s.cleanup.preview(TEACHER,[item]);assert plan['can_execute'],plan
    result=execute(s,[item],plan)
    assert result['status']=='completed' and not blob.exists()
    assert not list(blob.parent.glob('.cleanup-*'))
    with s.store._connection() as con:
        assert not con.execute('SELECT 1 FROM web_derived_datasets WHERE dataset_id=?',(item['ref'],)).fetchone()
    # A distinct referenced version must be kept for old fixed submissions.
    other,record,blob=upload(s)
    project=s.store.create_project(TEACHER,'uses external data')
    s.store.submit(project['project_id'],TEACHER,'minor',_asset_project_files(),record['files'])
    trash(s,other)
    blocked=s.cleanup.preview(TEACHER,[other])
    assert not blocked['can_execute']
    assert any('固定提交' in b['suggestion'] for b in blocked['items'][0]['blockers'])
    assert blob.exists()


def test_derived_retry_resumes_after_rename_and_rejects_new_references(setup,monkeypatch):
    s=setup;item,record,blob=upload(s);trash(s,item)
    plan=s.cleanup.preview(TEACHER,[item])
    actual_rename=os.rename;fail=[True]
    def interrupted(source,target,*args,**kwargs):
        actual_rename(source,target,*args,**kwargs)
        if fail[0]:fail[0]=False;raise OSError('after rename')
    monkeypatch.setattr(os,'rename',interrupted)
    result=execute(s,[item],plan)
    assert result['status']=='failed' and not blob.exists()
    project=s.store.create_project(TEACHER,'late submission')
    with pytest.raises(KitError,match='清理'):
        s.store.submit(project['project_id'],TEACHER,'minor',_asset_project_files(),record['files'])
    result=s.cleanup.retry(TEACHER,result['request_id'])
    assert result['status']=='completed'
    assert not list(blob.parent.glob('.cleanup-*'))


def test_central_imported_resources_follow_same_bin_cleanup_without_touching_cli(tmp_path):
    from researchkit.web_asset_library import AssetLibrary
    from researchkit.web_content import capacity
    library = Path(__file__).resolve().parents[1] / 'library'
    original = (library / 'cases/hello/1.0.0/case.py').read_bytes()
    store = SubmissionStore(tmp_path / 'store')
    shared = AssetLibrary(store, library)
    cleanup = Cleanup(store,library)
    case = {'kind':'template','ref':'hello@1.0.0'}
    algorithm = {'kind':'algorithm','ref':'example.scale@1.0.0'}
    for item in (case,algorithm):
        report = cleanup.lifecycle.report(TEACHER,**item)
        cleanup.lifecycle.change(TEACHER,**item,action='trash',expected_revision=report['revision'])
    plan=cleanup.preview(TEACHER,[algorithm,case])
    assert plan['can_execute'],plan
    assert plan['order']==[case,algorithm]
    result=cleanup.execute(TEACHER,[algorithm,case],plan['plan_token'],'永久清理')
    assert result['status']=='completed'
    assert (library / 'cases/hello/1.0.0/case.py').read_bytes()==original
    # Completion marker survives deletion; CLI copies cannot reappear in Web.
    again=AssetLibrary(store,library).catalog()
    assert again['templates']==[] and again['plugins']==[]
    assert again['contracts']  # shared schema history is deliberately retained
    assert capacity(store)['unique_files'] >= 1
