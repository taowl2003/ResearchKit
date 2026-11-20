from __future__ import annotations
import json
import os
import pwd
import shutil
import tempfile
from pathlib import Path
from tornado.testing import AsyncHTTPTestCase
from researchkit.web_app import application,LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
from test_web_dataset_workflow import CatalogClient,_reference


class TestDatasetWorkflowHTTP(AsyncHTTPTestCase):
    token='a'*48
    def get_app(self):
        self.root=Path(tempfile.mkdtemp(prefix='.rk-data-http-',dir=Path.cwd()))
        account=pwd.getpwuid(os.getuid()).pw_name
        principal=Principal(os.getuid(),account,'teacher' if account=='ly' else 'student')
        self.reference=_reference('raw-v1','value.csv',b'value\n10\n')
        self.client=CatalogClient(LocalStoreClient(SubmissionStore(self.root/'store'),principal),
                                  [{'dataset_id':'raw-v1','files':[self.reference]}])
        self.workspace=Workspace(principal,self.root/'workspace',self.client)
        return application(self.workspace,self.token,demo=True)
    def tearDown(self):
        super().tearDown();shutil.rmtree(self.root)
    def api(self,path,method='GET',body=None):
        headers={'Authorization':'Bearer '+self.token}
        if body is not None:headers['Content-Type']='application/json'
        return self.fetch('/api/'+path,method=method,headers=headers,
                          body=json.dumps(body) if body is not None else None)
    def test_whole_dataset_cas_and_server_side_file_stream(self):
        project=json.loads(self.api('projects','POST',{'name':'HTTP data flow'}).body)
        path='projects/'+project['project_id']
        state=json.loads(self.api(path+'/datasets').body)
        assert state['sha256'] is None
        attached=self.api(path+'/datasets','PUT',{'dataset_ids':['raw-v1'],'expected_sha256':None})
        assert attached.code==200 and json.loads(attached.body)['references']==[self.reference]
        conflict=self.api(path+'/datasets','PUT',{'dataset_ids':['raw-v1'],'expected_sha256':None})
        assert conflict.code==409
        illegal=self.api(path+'/datasets','PUT',{'dataset_ids':['raw-v1'],'expected_sha256':None,'owner':'ly'})
        assert illegal.code==400
        output=Path(project['path'])/'data'/'processed';output.mkdir(parents=True)
        (output/'values.bin').write_bytes(b'z'*(1024*1024+1))
        saved=self.api(path+'/derived','POST',{'path':'data/processed/values.bin','name':'cleaned'})
        assert saved.code==201
        assert json.loads(saved.body)['source_reference_only_after_attach'] is True
        assert self.client.read_sizes==[1024*1024,1]
        escaped=self.api(path+'/derived','POST',{'path':'/home/ly/private','name':'bad'})
        assert escaped.code==400
    def test_retired_job_retention_is_rejected_and_catalog_query_validation(self):
        captured=[]
        self.workspace.store.request=lambda method,**params:captured.append((method,params)) or {'items':[],'next_cursor':None}
        queued=self.api('jobs/run-example/derived','POST',{'paths':['artifacts/features.bin'],'name':'features'})
        # Browser execution and copying its retained job output were removed.
        # This must fail before it can issue a broker request or create state.
        assert queued.code==410
        assert captured==[]
        catalog=self.api('derived-datasets?visibility=mine&limit=25&q=features')
        assert catalog.code==200
        assert captured[-1][1]=={'visibility':'mine','limit':25,'q':'features'}
        assert self.api('derived-datasets?visibility=unknown').code==400
        assert self.api('derived-datasets?limit=1&limit=2').code==400

    def test_tutorial_rainfall_csv_is_listed_and_saved_without_moving_it(self):
        project=json.loads(self.api('projects','POST',{'name':'rainfall-framework'}).body)
        source=Path(project['path'])/'data'/'rainfall.csv'
        source.parent.mkdir(parents=True)
        content=b'day,rainfall_mm\n2026-01-01,2\n2026-01-02,3.5\n2026-01-03,4.5\n'
        source.write_bytes(content)
        listing=self.api('projects?include_archived=true')
        assert listing.code==200
        row=next(row for row in json.loads(listing.body) if row['project_id']==project['project_id'])
        csv=next(item for item in row['files'] if item['path']=='data/rainfall.csv')
        assert csv['size']==len(content) and not csv.get('reference_only')
        saved=self.api('projects/'+project['project_id']+'/derived','POST',{
            'path':'data/rainfall.csv','name':'降雨练习原料-v1',
        })
        assert saved.code==201, saved.body
        assert json.loads(saved.body)['dataset_id']=='derived-immutable-v1'
        assert bytes(self.client.uploads['upload-1']['data'])==content
        assert source.read_bytes()==content
