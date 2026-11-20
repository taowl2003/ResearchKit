"""Complete broker authorization path using fabricated OS principals and real blobs."""
from __future__ import annotations
import hashlib
import os
from types import MappingProxyType
import pytest
from researchkit.files import KitError
from researchkit.web_broker import BrokerServer
from researchkit.web_identity import Policy,Principal
from researchkit.web_store import SubmissionStore

LY=Principal(1011,'ly','teacher');STUDENT=Principal(1012,'student','student');OTHER=Principal(1013,'other','student')
RUNNER=Principal(980,'test-runner','runner')


def test_ide_uploaded_output_teacher_review_private_reuse_and_publication(tmp_path):
    store=SubmissionStore(tmp_path/'store');root=tmp_path/'derived';root.mkdir(mode=0o700)
    server=object.__new__(BrokerServer);server.store=store
    server.policy=Policy(principals=MappingProxyType({p.uid:p for p in (LY,STUDENT,OTHER)}),
                         workspaces=MappingProxyType({}),broker_uid=981,broker_username='broker',
                         submission_store=store.directory,code_root=tmp_path,runner_uid=RUNNER.uid,
                         runner_username=RUNNER.username,derived_dataset_root=root)
    def call(principal,method,**params):return server._dispatch(principal,{'method':method,'params':params})
    # The old public job/retention protocol must remain closed even when an
    # old runner identity is configured.  IDE-produced project files are
    # uploaded as derived data instead.
    with pytest.raises(KitError, match='网页检查、运行'):
        call(STUDENT, 'request_job', submission_id='x', operation='run')
    project=call(STUDENT,'create_project',name='student source')
    digest=hashlib.sha256(b'abc').hexdigest()
    upload=call(STUDENT, 'begin_derived_upload', name='IDE features', expected_size=3,
                expected_sha256=digest, source_refs=[])
    call(STUDENT, 'append_derived_upload', upload_id=upload['upload_id'], offset=0,
         data='YWJj')
    record=call(STUDENT, 'commit_derived_upload', upload_id=upload['upload_id'], sha256=digest)
    ident=record['dataset_id'];refs=record['files']
    frozen=store.submit(project['project_id'],STUDENT,'minor',{'case.py':b'pass'},refs)
    # Binding the immutable submission grants ly a narrow review read right;
    # it does not reveal the student's private data to unrelated users.
    server._derived().bind_submission(STUDENT, frozen['submission_id'], refs, LY)
    assert call(LY,'get_derived_catalog')['items'][0]['dataset_id']==ident
    assert call(OTHER,'get_derived_catalog')['items']==[]
    with server._open_dataset_file(LY,{'dataset_id':ident,'path':'data.bin','expected_reference':refs[0]}) as (fd,metadata):
        assert os.read(fd,3)==b'abc' and metadata==refs[0]
    # The same immutable file reference is reusable by the owner without a
    # second blob; it is a fixed submission reference rather than a job output.
    repeated=store.submit(project['project_id'],STUDENT,'minor',{'case.py':b'pass'},refs)
    assert repeated['manifest']['data_references']['references']==refs
    assert len(list((root/'blobs').glob('*.blob')))==1
    call(LY,'publish_derived',dataset_id=ident)
    assert call(OTHER,'get_derived_catalog')['items'][0]['dataset_id']==ident
    public_project=call(OTHER,'create_project',name='public reuse')
    public_version=store.submit(public_project['project_id'],OTHER,'minor',{'case.py':b'pass'},refs)
    assert public_version['manifest']['data_references']['references']==refs


def test_manual_template_review_preserves_private_derived_reference(tmp_path):
    from researchkit.web_derived import DerivedDatasets
    from researchkit.web_assets import AssetService
    from test_web_asset_validation import _files
    store=SubmissionStore(tmp_path/'store');root=tmp_path/'derived';root.mkdir(mode=0o700)
    derived=DerivedDatasets(store,root)
    digest=hashlib.sha256(b'abc').hexdigest();upload=derived.begin_upload(STUDENT,'input',3,digest)
    derived.append_upload(STUDENT,upload['upload_id'],0,b'abc');data=derived.commit_upload(STUDENT,upload['upload_id'],digest)
    source=_files(template=True)
    source['case.py']=('def run(context):\n    with context.open_dataset("'+data['dataset_id']+'", "data.bin") as stream:\n        return {"bytes": len(stream.read())}\n').encode()
    project=store.create_project(STUDENT,'template with reviewed input')
    frozen=store.submit(project['project_id'],STUDENT,'major',source,data['files'])
    derived.bind_submission(STUDENT,frozen['submission_id'],data['files'],LY)
    candidate=AssetService(store,tmp_path/'library').create(LY,frozen['submission_id'],'template','data.template','1.0.0')
    server=object.__new__(BrokerServer);server.store=store
    server.policy=Policy(principals=MappingProxyType({p.uid:p for p in (LY,STUDENT,OTHER)}),
                         workspaces=MappingProxyType({}),broker_uid=981,broker_username='broker',
                         submission_store=store.directory,code_root=tmp_path,runner_uid=980,
                         runner_username=RUNNER.username,derived_dataset_root=root)
    def call(principal,method,**params):return server._dispatch(principal,{'method':method,'params':params})
    # Candidate validation jobs are retired.  Review is a deliberate manual
    # decision over the fixed snapshot and its listed IDE artifacts.
    with pytest.raises(KitError, match='网页检查、运行'):
        call(LY, 'create_asset_candidate_validation', candidate_id=candidate['candidate_id'],
             expected_sha256=candidate['candidate_sha256'], operation='run')
    reviewed=AssetService(store,tmp_path/'library').review(
        LY,candidate['candidate_id'],candidate['candidate_sha256'],'人工审核固定代码与 IDE 产物')
    assert reviewed['status']=='reviewed' and reviewed['validation_run_id'] is None
    published=AssetService(store,tmp_path/'library').publish(LY,candidate['candidate_id'],candidate['candidate_sha256'])
    assert published['status']=='published'
    with server._open_dataset_file(LY,{'dataset_id':data['dataset_id'],'path':'data.bin',
                                       'expected_reference':data['files'][0]}) as (fd,_metadata):
        assert os.read(fd,3)==b'abc'
    with pytest.raises(KitError,match='无权'):
        with server._open_dataset_file(OTHER,{'dataset_id':data['dataset_id'],'path':'data.bin',
                                              'expected_reference':data['files'][0]}):
            pass
