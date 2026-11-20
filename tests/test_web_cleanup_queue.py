"""Broker -> existing root publisher -> durable cleanup completion."""
import json

import pytest

from researchkit.files import KitError
from researchkit.web_broker import BrokerServer
from researchkit.web_cleanup import Cleanup
from researchkit.web_data_management import DataManagement
from researchkit.web_dataset_trees import TreeRegistry
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit import web_cleanup_raw
from test_web_readme_publication import state, approve
from researchkit import web_data_publisher as publisher


def test_raw_cleanup_claim_finish_reservation_and_lost_reply_retry(state,monkeypatch):
    s=state
    store=SubmissionStore(s.root/'submissions')
    s.manager=DataManagement(store,s.policy);s.manager.worker_start('queue-test')
    job=approve(s);published=publisher.publish(s.policy,s.config,job['candidate'])
    s.manager.worker_finish(job['job_id'],job['lease_token'],published)
    ident=published['dataset_id'];raw_ref=TreeRegistry.reference(TreeRegistry(s.policy.dataset_tree_registry)._load()[ident])
    s.policy.code_root=s.root
    for name in ('algorithms','contracts','cases'):(s.root/'library'/name).mkdir(parents=True)
    server=object.__new__(BrokerServer);server.store=store;server.policy=s.policy
    def call(principal,method,**params):return server._dispatch(principal,{'method':method,'params':params})
    life=Cleanup(store,s.root/'library',s.policy).lifecycle
    current=life.report(s.teacher,'dataset',ident)
    life.change(s.teacher,'dataset',ident,'trash',current['revision'])
    item={'kind':'dataset','ref':ident}
    # Shared physical directories must be blocked before confirmation rather
    # than leaving an avoidably locked task for the root worker to reject.
    previous_registry=s.policy.dataset_registry
    legacy=s.root/'legacy-overlap.json'
    legacy.write_text(json.dumps({'schema':'researchkit.web-datasets/v1','datasets':[{
        'dataset_id':'legacy','root':str(s.unit),'readers':[],'files':[]}]}))
    s.policy.dataset_registry=legacy
    overlap=call(s.teacher,'preview_asset_cleanup',items=[item])
    assert not overlap['can_execute']
    assert any('包含关系' in b.get('detail','') for b in overlap['items'][0]['blockers'])
    s.policy.dataset_registry=previous_registry
    plan=call(s.teacher,'preview_asset_cleanup',items=[item]);assert plan['can_execute'],plan
    assert str(s.raw) not in json.dumps(plan)
    execution=call(s.teacher,'execute_asset_cleanup',items=[item],plan_token=plan['plan_token'],confirmation='永久清理')
    assert execution['status']=='queued' and s.unit.exists()
    # A new fixed snapshot cannot race in after the confirmed purge.
    project=store.create_project(s.teacher,'late data user')
    with pytest.raises(KitError,match='清理'):
        store.submit(project['project_id'],s.teacher,'minor',{'case.py':b'pass'},[raw_ref])
    current=life.report(s.teacher,'dataset',ident)
    with pytest.raises(KitError,match='清理'):
        life.change(s.teacher,'dataset',ident,'restore',current['revision'])
    root=Principal(0,'root','data-publisher')
    with pytest.raises(KitError):call(s.student,'publisher_cleanup_claim')
    claimed=call(root,'publisher_cleanup_claim')
    assert claimed['request']['dataset_id']==ident
    monkeypatch.setattr(web_cleanup_raw,'_root_only',lambda:None)
    result=web_cleanup_raw.purge_raw(s.policy,s.config,claimed['request'])
    assert not s.unit.exists()
    # Simulate losing the root completion response. Its receipt lets retry
    # return success without touching a replacement directory at the old name.
    s.unit.mkdir();(s.unit/'README.md').write_text('new independent upload')
    assert web_cleanup_raw.purge_raw(s.policy,s.config,claimed['request'])['reused']
    assert (s.unit/'README.md').read_text()=='new independent upload'
    with pytest.raises(KitError):call(root,'publisher_cleanup_finish',item_id=claimed['item_id'],lease_token='wrong',result=result)
    # Restarted publisher can immediately reclaim the receipt, while the
    # old worker's lease cannot acknowledge or alter the resumed task.
    call(root,'publisher_start',instance_id='queue-test-restarted')
    previous=claimed;claimed=call(root,'publisher_cleanup_claim')
    assert claimed['request']==previous['request']
    assert claimed['lease_token']!=previous['lease_token']
    with pytest.raises(KitError):call(root,'publisher_cleanup_finish',item_id=previous['item_id'],lease_token=previous['lease_token'],result=result)
    result=web_cleanup_raw.purge_raw(s.policy,s.config,claimed['request'])
    assert result['reused'] and (s.unit/'README.md').exists()
    call(root,'publisher_cleanup_finish',item_id=claimed['item_id'],lease_token=claimed['lease_token'],result=result)
    assert call(s.teacher,'get_asset_cleanup',request_id=execution['request_id'])['status']=='completed'
    assert call(s.teacher,'list_asset_bin',state='trashed')['total']==0
    assert s.manager.get_candidate(s.teacher,job['candidate']['candidate_id'])['registered_dataset_id'] is None
