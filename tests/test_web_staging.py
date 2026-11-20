from __future__ import annotations
import hashlib
from pathlib import Path
import pytest
from researchkit.files import KitError
from researchkit.web_runner import staged_artifacts,compact_staging,check_output_budget
from researchkit.web_jobs import Jobs
from test_web_jobs import ALICE,BOB,TEACHER,_submission
from researchkit.web_store import SubmissionStore


def test_four_output_roots_are_collected_and_disposable_code_is_removed(tmp_path):
    for folder in ('results','report','data/processed','artifacts'):
        target=tmp_path/folder;target.mkdir(parents=True);(target/'value.bin').write_bytes(b'abc')
    (tmp_path/'case.py').write_text('def run(context): pass')
    records=staged_artifacts(tmp_path,file_limit=32*1024**3)
    assert {item['path'] for item in records}=={folder+'/value.bin' for folder in ('results','report','data/processed','artifacts')}
    compact_staging(tmp_path,records)
    assert not (tmp_path/'case.py').exists()
    assert all((tmp_path/item['path']).read_bytes()==b'abc' for item in records)
    compact_staging(tmp_path,[]);assert not tmp_path.exists()


def test_budget_covers_unadvertised_paths_and_rejects_nested_links(tmp_path,monkeypatch):
    import researchkit.web_runner as runner
    monkeypatch.setattr(runner,'MAX_STAGED_TOTAL_BYTES',1024)
    (tmp_path/'unadvertised.bin').write_bytes(b'x'*1025)
    with pytest.raises(KitError,match='总量'):check_output_budget(tmp_path,file_limit=4096)
    (tmp_path/'unadvertised.bin').unlink()
    (tmp_path/'data').symlink_to('/etc',target_is_directory=True)
    with pytest.raises(KitError,match='链接'):staged_artifacts(tmp_path,file_limit=4096)


def test_retention_reuses_version_even_after_expiry_and_queued_second_request(tmp_path):
    store=SubmissionStore(tmp_path/'store');jobs=Jobs(store);source=_submission(store)
    run=jobs.request(ALICE,source['submission_id'],'run');jobs.start_worker('worker-a',4444);jobs.claim('worker-a')
    artifact={'path':'artifacts/features.bin','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}
    jobs.finish('worker-a',run['run_id'],'succeeded',0,None,staged_artifacts=[artifact])
    student=jobs.request_artifact_retention(ALICE,run['run_id'],[artifact['path']])[0]
    teacher=jobs.request_artifact_retention(TEACHER,run['run_id'],[artifact['path']])[0]
    first=jobs.claim_artifact_retention('worker-a')
    jobs.finish_artifact_retention('worker-a',first['request_id'],dataset_id='derived-same-version')
    second=jobs.claim_artifact_retention('worker-a')
    assert second['retained_dataset_id']=='derived-same-version'
    jobs.finish_artifact_retention('worker-a',second['request_id'],dataset_id='derived-same-version')
    with store._connection() as connection:
        connection.execute('UPDATE web_job_staged_artifacts SET expires_at=0')
    assert jobs.request_artifact_retention(TEACHER,run['run_id'],[artifact['path']])[0]['dataset_id']=='derived-same-version'
    assert jobs.claim_expired_staging('worker-a')==[]
    with pytest.raises(KitError,match='无权'):
        jobs.request_artifact_retention(BOB,run['run_id'],[artifact['path']])


def test_new_worker_can_retain_previous_successful_job_and_retry_interruption(tmp_path):
    store=SubmissionStore(tmp_path/'store');jobs=Jobs(store);source=_submission(store)
    run=jobs.request(ALICE,source['submission_id'],'run');jobs.start_worker('worker-a',4444);jobs.claim('worker-a')
    artifact={'path':'data/processed/values.bin','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}
    jobs.finish('worker-a',run['run_id'],'succeeded',0,None,staged_artifacts=[artifact])
    request=jobs.request_artifact_retention(ALICE,run['run_id'],[artifact['path']])[0]
    jobs.start_worker('worker-b',4444)
    claimed=jobs.claim_artifact_retention('worker-b')
    assert jobs.retention_info('worker-b',request['request_id'])['run_id']==run['run_id']
    jobs.start_worker('worker-c',4444)
    retry=jobs.request_artifact_retention(ALICE,run['run_id'],[artifact['path']])[0]
    assert retry['status']=='queued'
    jobs.claim_artifact_retention('worker-c')
    with pytest.raises(KitError):jobs.retention_info('worker-b',request['request_id'])
    assert jobs.retention_info('worker-c',request['request_id'])['run_id']==run['run_id']
    assert jobs.staging_inventory('worker-c')==[run['run_id']]


def test_orphan_cleanup_preserves_registered_staging_and_other_files(tmp_path):
    from researchkit.web_runner import cleanup_orphan_staging
    for name in ('staged-keep','staged-orphan','worker-other'):
        (tmp_path/name).mkdir();(tmp_path/name/'payload').write_bytes(b'abc')
    cleanup_orphan_staging(tmp_path,['keep'])
    assert (tmp_path/'staged-keep'/'payload').read_bytes()==b'abc'
    assert not (tmp_path/'staged-orphan').exists()
    assert (tmp_path/'worker-other').exists()
