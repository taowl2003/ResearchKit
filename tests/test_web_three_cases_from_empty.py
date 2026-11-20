"""Web-created empty project -> reviewed framework -> two Web template forks.

Uses an empty central library, blank Web algorithms, IDE edits and broker dispatch.
No project ZIP/import/register path, Web job, real user workspace or share write.
Only the test dataset ownership predicate and local FD transport are replaced;
real broker dataset grants, content hashes and readonly descriptor checks run.
"""
from __future__ import annotations
import base64
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import MappingProxyType
import uuid

import pytest
import yaml

from researchkit import dataset_access, experiment_support, web_datasets
from researchkit.web_broker import BrokerServer, _encode_files, _decode_files
from researchkit.web_identity import Policy, Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
from researchkit.web_workspace_materials import add_algorithms

ROOT = Path(__file__).resolve().parents[1]
SKELETON = 'def run(inputs, parameters, context):\n    # 在此编写本地算法逻辑\n    return {}\n'
LOCAL_METADATA = {
    'rainfall': {'tags': ['降雨', '教学', '统计'], 'description': 'Local rainfall algorithm'},
    'mnist-cnn': {'tags': ['深度学习', 'CNN', 'MNIST'], 'description': 'Local MNIST algorithm'},
    'acoustic-tdoa': {'tags': ['水声', 'TDOA', '定位'], 'description': 'Local TDOA algorithm'},
}

class Client:
    def __init__(self, server, principal):
        self.server, self.principal = server, principal
    def request(self, method, **params):
        if 'files' in params:
            params['files'] = _encode_files(params['files'])
        if method == 'append_derived_upload':
            params['data'] = base64.b64encode(params['data']).decode('ascii')
        value = self.server._dispatch(self.principal, {'method': method, 'params': params})
        if method in {'get_submission','review_submission','get_shared_template','get_shared_component','get_shared_contract'}:
            value['files'] = _decode_files(value['files'])
        return value

def create_blank_then_write_in_ide(workspace, pid, case, alias, parameters=None):
    """Exercise the Web blank form before writing canonical code in the IDE."""
    source = ROOT / 'examples/three-cases' / case / 'algorithms' / alias
    meta = yaml.safe_load((source / 'plugin.yaml').read_text())
    local = workspace.create_local_algorithm({
        'name': meta['name'], 'version': meta['version'],
        'tags': LOCAL_METADATA[case]['tags'], 'description': LOCAL_METADATA[case]['description'],
        'parameters': meta['parameters'], 'inputs': meta['inputs'], 'outputs': meta['outputs'],
        # Deliberately omit plugin_py: the Web form creates only the safe skeleton.
    })
    local_root = workspace.root / '.researchkit-local-algorithms' / str(local['id'])
    assert (local_root / 'plugin.py').read_text() == SKELETON
    created_meta = yaml.safe_load((local_root / 'plugin.yaml').read_text())
    for field in ('name', 'version', 'parameters', 'inputs', 'outputs'):
        assert created_meta[field] == meta[field]

    bound = add_algorithms(workspace, pid, {'local_algorithm_id': local['id'], 'alias': alias,
        **({'parameters': parameters} if parameters else {})})
    assert bound['path'] == f'algorithms/{alias}'
    before = workspace.read_text(pid, f'algorithms/{alias}/plugin.py')
    assert before['text'] == SKELETON
    project_meta = yaml.safe_load(workspace.read_text(pid, f'algorithms/{alias}/plugin.yaml')['text'])
    for field in ('name', 'version', 'parameters', 'inputs', 'outputs'):
        assert project_meta[field] == meta[field]
    workspace.save_text(pid, f'algorithms/{alias}/plugin.py', (source / 'plugin.py').read_text(), before['sha256'])
    assert workspace.read_text(pid, f'algorithms/{alias}/plugin.py')['text'] == (source / 'plugin.py').read_text()
    return bound

def scheme(workspace, pid, name, case, alias, parameters):
    workspace.save_experiment(pid, {'id': name, 'expected_sha256': None, 'document': {
        'schema': 'researchkit.experiment/v1', 'name': name, 'description': 'Web tutorial',
        'case_parameters': {'algorithm_alias': alias, **case},
        'algorithm_parameters': {alias: parameters}}})

def fixed(workspace, pid):
    paths = [r['path'] for r in workspace.experiment_catalog(pid)['result_files']
        if Path(r['path']).name in {'parameters.json','experiment.json','summary.json'}]
    plan = workspace.submission_preview(pid, {'kind':'minor','result_paths':paths})
    receipt = workspace.submission_prepare(pid, {'kind':'minor','result_paths':paths,
        'request_id':str(uuid.uuid4()),'expected_revision':plan['snapshot_revision'],
        'case_version':plan['case']['suggested_version'],
        'algorithm_versions':{r['alias']:r['suggested_version'] for r in plan['algorithms']}})
    assert receipt['status'] == 'succeeded'
    return workspace.store.request('get_submission', submission_id=receipt['submission_id'])

def publish(client, submission, name, alias, dataset_id=None):
    items = [{'kind':'template','name':name,'version':'1.0.0','tags':['教学'],'description':'Web-created framework'},
        {'kind':'algorithm','alias':alias,'tags':['教学'],'description':'Local algorithm reviewed'}]
    if dataset_id:
        items.append({'kind':'derived','dataset_id':dataset_id,'tags':['教学'],
            'description':'Generated teaching data, not real observations'})
    result = client.request('review_publish_batch', submission_id=submission['id'],
        expected_sha256=submission['content_sha256'],items=items)
    assert result['ok'], result
    return result

def save_data(workspace, pid, path, name):
    saved = workspace.upload_project_file_as_derived(pid, path, name,
        description='Generated tutorial input', tags=['教学'],readme_markdown='# Teaching data\nSynthetic input.')
    workspace.add_project_datasets(pid, [saved['dataset_id']], workspace._project_references_unlocked(pid)['sha256'])
    return saved['dataset_id']


def test_empty_library_web_first_framework_and_two_reusable_forks(monkeypatch):
    pytest.importorskip('torch')
    from test_example_mnist import _mnist_files
    teacher = Principal(os.getuid(),'ly','teacher')
    student = Principal(os.getuid()+10000,'test','student')
    actors = {p.username:p for p in (teacher,student)}
    with tempfile.TemporaryDirectory(prefix='.rk-web-first-',dir=ROOT) as temp:
        root=Path(temp);root.chmod(0o700)
        code=root/'code'
        for section in ('algorithms','contracts','cases'):
            (code/'library'/section).mkdir(parents=True)
        data=root/'derived';data.mkdir(mode=0o700)
        server=object.__new__(BrokerServer);server.store=SubmissionStore(root/'store')
        server.policy=Policy(principals=MappingProxyType({p.uid:p for p in actors.values()}),
            workspaces=MappingProxyType({}),broker_uid=teacher.uid,broker_username='ly',
            submission_store=server.store.directory,code_root=code,derived_dataset_root=data)
        # The UDS policy resolver is covered by broker security suites; here
        # two isolated principals share one test process, never real homes.
        server._target_principal=lambda name:actors[name]
        clients={name:Client(server,p) for name,p in actors.items()}
        workspaces={name:Workspace(p,root/(name+'-workspace'),clients[name],library_root=code/'library')
            for name,p in actors.items()}
        mentor=workspaces['ly'];student_ws=workspaces['test']
        assert clients['ly'].request('get_component_catalog')['plugins'] == []
        assert clients['ly'].request('get_component_catalog')['templates'] == []

        # IDE data reads use the actual broker resolver/ACL and duplicate only
        # its readonly FD, rather than copying data into a personal project.
        current=[teacher]
        def read_fd(ident,path,expected):
            with server._open_dataset_file(current[0],{'dataset_id':ident,'path':path,'expected_reference':expected}) as (fd,returned):
                assert returned == expected
                return os.dup(fd)
        monkeypatch.setattr(dataset_access,'_WEB_ROOT',root/'no-sandbox-mount')
        monkeypatch.setattr(dataset_access,'_broker_fd',read_fd)
        # Case wrappers import the helper under this ordinary project name.
        monkeypatch.setitem(sys.modules,'experiment_support',experiment_support)
        def run(ws,project,scheme_name):
            current[0]=ws.principal
            experiment_support.main(Path(project['path']),['--scheme',scheme_name])
            rows=[r for r in ws.experiment_catalog(project['project_id'])['runs'] if r['scheme_id']==scheme_name]
            assert len(rows)==1 and rows[0]['record']['status']=='succeeded'
            return rows[0]['record']['summary']

        base=mentor.create_project('rainfall-framework');pid=base['project_id'];path=Path(base['path'])
        create_blank_then_write_in_ide(mentor,pid,'rainfall','scale',{'factor':2})
        opened=mentor.read_text(pid,'case.py')
        mentor.save_text(pid,'case.py',(ROOT/'examples/three-cases/rainfall/case.py').read_text(),opened['sha256'])
        (path/'data').mkdir();(path/'data/rainfall.csv').write_text('day,rainfall_mm\n2026-01-01,2\n2026-01-02,3.5\n2026-01-03,4.5\n')
        rain=save_data(mentor,pid,'data/rainfall.csv','rainfall input')
        mentor.enable_experiments(pid)
        for factor in (2,3):
            scheme(mentor,pid,f'factor-{factor}',{'data_source':'dataset','dataset_id':rain,
                'data_root':'','data_format':'directory','data_file':'data.bin'},'scale',{'factor':factor})
            assert run(mentor,base,f'factor-{factor}')['scaled_total']==factor*10
        original=fixed(mentor,pid)
        publish(clients['ly'],original,'rainfall.framework','scale',rain)

        access=ComponentService(server.store,code/'library')
        row=next(r for r in access.access(teacher) if r['kind']=='template' and r['ref']=='rainfall.framework@1.0.0')
        clients['ly'].request('set_component_access',kind='template',ref=row['ref'],public=False,
            targets=['ly','test'],expected_revision=row['revision'])
        assert any(r['dataset_id']==rain for r in clients['test'].request('get_dataset_catalog'))
        assert any(r['ref']=='rainfall.scale@1.0.0' for r in clients['test'].request('get_component_catalog')['plugins'])

        # Existing public raw MNIST is represented by tiny, independent IDX
        # fixtures; no real share path is read or mutated by this test.
        raw=root/'mnist';raw.mkdir();records=[]
        for relative,blob in _mnist_files().items():
            target=raw/relative;target.parent.mkdir(exist_ok=True);target.write_bytes(blob)
            records.append({'path':relative,'size':len(blob),'sha256':hashlib.sha256(blob).hexdigest()})
        registry=root/'registry.json';registry.write_text(json.dumps({'schema':'researchkit.web-datasets/v1','datasets':[
            {'dataset_id':'mnist-public','root':str(raw),'readers':[{'uid':p.uid,'username':p.username} for p in actors.values()], 'files':records}]}))
        monkeypatch.setattr(web_datasets,'_root_owned',lambda info:info.st_uid in {0,os.geteuid()})
        server.policy=replace(server.policy,dataset_registry=registry)
        cnn=student_ws.create_project('mnist-study','rainfall.framework@1.0.0');cid=cnn['project_id']
        inherited=Path(cnn['path'])/'case.py';assert inherited.read_bytes()==(path/'case.py').read_bytes()
        create_blank_then_write_in_ide(student_ws,cid,'mnist-cnn','cnn')
        student_ws.add_project_datasets(cid,['mnist-public'],student_ws._project_references_unlocked(cid)['sha256'])
        for sid,lr in [('cnn-baseline',0.001),('cnn-learning-rate',0.0003)]:
            scheme(student_ws,cid,sid,{'data_source':'dataset','dataset_id':'mnist-public','data_root':''},'cnn',
                {'epochs':1,'train_limit':30,'test_limit':20,'batch_size':10,'learning_rate':lr,'device':'cpu','num_workers':0})
            summary=run(student_ws,cnn,sid);assert 0<=summary['accuracy']<=1
        publish(clients['ly'],fixed(student_ws,cid),'mnist.classification','cnn')

        tdoa=student_ws.create_project('tdoa-study','rainfall.framework@1.0.0');tid=tdoa['project_id'];troot=Path(tdoa['path'])
        create_blank_then_write_in_ide(student_ws,tid,'acoustic-tdoa','locator',{'sound_speed_m_s':1500})
        spec=importlib.util.spec_from_file_location('tutorial_data_generator',ROOT/'examples/three-cases/acoustic-tdoa/generate_sample_data.py')
        generator=importlib.util.module_from_spec(spec);spec.loader.exec_module(generator)
        generator.build(troot/'sample-data')
        td=save_data(student_ws,tid,'sample-data.zip','TDOA input')
        rmse=[]
        for speed in (1500,1480):
            sid=f'sound-speed-{speed}'
            scheme(student_ws,tid,sid,{'data_source':'dataset','dataset_id':td,'data_root':'','data_format':'zip'},'locator',{'sound_speed_m_s':speed})
            rmse.append(run(student_ws,tdoa,sid)['rmse_m'])
        assert rmse[0]<0.0001 and 9<rmse[1]<11
        publish(clients['ly'],fixed(student_ws,tid),'acoustic.tdoa','locator',td)
        assert (troot/'case.py').read_bytes()==(path/'case.py').read_bytes()
        assert all(not list((code/'library'/section).iterdir()) for section in ('algorithms','contracts','cases'))
        catalog=clients['ly'].request('get_component_catalog')
        assert len(catalog['templates'])==3
        assert {r['ref'] for r in catalog['plugins']}=={'rainfall.scale@1.0.0','mnist.cnn@1.0.0','acoustic.locator@1.0.0'}
        with server.store._connection() as con:
            assert con.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0]==0
