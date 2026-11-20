import json
import os
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType

from tornado.testing import AsyncHTTPTestCase

from researchkit.web_app import application
from researchkit.web_assets import AssetService
from researchkit.web_broker import BrokerServer, _encode_files, _decode_files
from researchkit.web_identity import Policy, Principal
from researchkit.web_workspace import Workspace


class TestCleanupHTTP(AsyncHTTPTestCase):
    token='b'*48
    def get_app(self):
        self.root=Path(tempfile.mkdtemp(prefix='.rk-cleanup-api-',dir=Path.cwd()))
        self.teacher=Principal(os.getuid(),'ly','teacher')
        self.student=Principal(os.getuid()+100,'test','student')
        for name in ('algorithms','contracts','cases'):(self.root/'library'/name).mkdir(parents=True)
        policy=Policy(MappingProxyType({self.teacher.uid:self.teacher,self.student.uid:self.student}),MappingProxyType({}),os.getuid(),'ly',self.root/'store',self.root)
        self.server=BrokerServer(policy)
        class Client:
            def request(_,method,**params):
                if 'files' in params:params['files']=_encode_files(params['files'])
                result=self.server._dispatch(self.workspace.principal,{'method':method,'params':params})
                if isinstance(result,dict) and 'files' in result:result['files']=_decode_files(result['files'])
                return result
        self.workspace=Workspace(self.teacher,self.root/'workspace',Client())
        return application(self.workspace,self.token,demo=True)
    def tearDown(self):
        super().tearDown();shutil.rmtree(self.root)
    def api(self,route,body=None,method=None):
        headers={'Authorization':'Bearer '+self.token}
        if body is not None:headers['Content-Type']='application/json'
        return self.fetch('/api/'+route,method=method or ('GET' if body is None else 'POST'),headers=headers,body=None if body is None else json.dumps(body))
    def seed_submission(self,project_id,kind):
        with self.workspace._project_guard(project_id):
            _root,files,references=self.workspace._snapshot(project_id)
        return self.server.store.submit(project_id,self.workspace.principal,kind,files,references)
    def new_case(self,name='cleanup.case'):
        project=self.workspace.create_project('source')
        fixed=self.seed_submission(project['project_id'],'minor')
        assets=AssetService(self.server.store,self.root/'library')
        candidate=assets.create(self.teacher,fixed['submission_id'],'template',name,'1.0.0')
        assets.review(self.teacher,candidate['candidate_id'],candidate['candidate_sha256'],'test')
        assets.publish(self.teacher,candidate['candidate_id'],candidate['candidate_sha256'])
        return {'kind':'template','ref':name+'@1.0.0'},project,fixed
    def change(self,item,action):
        report=json.loads(self.api('asset-lifecycle?kind='+item['kind']+'&ref='+item['ref']).body)
        return self.api('asset-lifecycle',dict(item,action=action,expected_revision=report['revision']),method='PUT')
    def test_retired_restore_recycle_preview_and_permanent_cleanup(self):
        item,project,fixed=self.new_case()
        assert self.change(item,'retire').code==200
        listed=self.api('asset-bin?state=retired&kind=template&q=cleanup&limit=1&offset=0')
        assert json.loads(listed.body)['items'][0]['ref']==item['ref']
        assert self.change(item,'restore').code==200
        assert json.loads(self.api('asset-bin?state=retired').body)['total']==0
        assert self.change(item,'trash').code==200
        preview=self.api('asset-cleanup/preview',{'items':[item]})
        assert preview.code==200,preview.body
        plan=json.loads(preview.body);assert plan['can_execute'],plan
        assert b'/home/' not in preview.body
        bad=self.api('asset-cleanup/execute',{'items':[item],'plan_token':plan['plan_token'],'confirmation':'yes'})
        assert bad.code==400
        finished=self.api('asset-cleanup/execute',{'items':[item],'plan_token':plan['plan_token'],'confirmation':'永久清理'})
        assert finished.code==200,finished.body
        result=json.loads(finished.body);assert result['status']=='completed'
        assert json.loads(self.api('asset-cleanup/'+result['request_id']).body)['status']=='completed'
        assert json.loads(self.api('asset-bin?state=trashed').body)['total']==0
        assert self.api('asset-lifecycle',dict(item,action='restore',expected_revision=plan['items'][0]['revision']),method='PUT').code==400
        assert Path(project['path'],'case.py').exists()
        assert self.workspace.store.request('get_submission',submission_id=fixed['submission_id'])['files']['case.py']
        from researchkit.engine import run_project
        assert run_project(Path(project['path']))['status']=='success'
    def test_students_cannot_list_preview_execute_or_retry(self):
        self.workspace.principal=self.student
        routes=[('asset-bin',None),('asset-cleanup/preview',{'items':[]}),
                ('asset-cleanup/execute',{'items':[],'plan_token':'a'*64,'confirmation':'永久清理'}),
                ('asset-cleanup/11111111-1111-1111-1111-111111111111',None),
                ('asset-cleanup/11111111-1111-1111-1111-111111111111/retry',{})]
        for route,body in routes:assert self.api(route,body).code==403
    def test_requests_cannot_add_paths_identity_or_unselected_assets(self):
        for route in ('asset-bin?limit=-1','asset-bin?limit=1&limit=2','asset-bin?root=/etc','asset-bin?state=active'):
            assert self.api(route).code==400
        for body in ({'items':[],'path':'/etc'},{'items':[],'role':'teacher'},{'items':[{'kind':'dataset','ref':'x','root':'/etc'}]}):
            assert self.api('asset-cleanup/preview',body).code==400
        assert self.api('asset-cleanup/11111111-1111-1111-1111-111111111111/retry',{'items':[]}).code==400
