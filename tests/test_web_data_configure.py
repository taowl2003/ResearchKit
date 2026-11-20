from __future__ import annotations
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def configured(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='.rk-data-config-test-', dir=Path.cwd()))
    spec = importlib.util.spec_from_file_location('configure_data', Path('tools/configure-web-data.py'))
    tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, '_root_owned', lambda info: info.st_uid in {0, os.getuid()})
    accounts = {'broker': SimpleNamespace(pw_name='broker', pw_uid=981,pw_gid=os.getgid()),
                'runner': SimpleNamespace(pw_name='runner', pw_uid=980,pw_gid=os.getgid())}
    monkeypatch.setattr(tool.pwd,'getpwnam',lambda name: accounts[name])
    (root/'raw').mkdir()
    (root/'units').mkdir()
    monkeypatch.setattr(tool, 'DEFAULT_RAW_ROOT', root/'raw')
    monkeypatch.setattr(tool, 'SYSTEMD_UNITS', root/'units')
    policy = root/'policy.json'
    policy.write_text(json.dumps({'broker':{'uid':981,'username':'broker'},'runner':{'uid':980,'username':'runner'},'retained':'value'}))
    policy.chmod(0o600)
    try: yield tool,root,policy
    finally: shutil.rmtree(root)


def test_check_is_read_only_and_will_not_take_over_existing_directory(configured):
    tool,root,policy=configured; original=policy.read_bytes();target=root/'derived'
    report,*_=tool.inspect(policy,target,raw_root=root/'raw')
    assert report['ready'] and not target.exists() and policy.read_bytes()==original
    target.mkdir(); (target/'existing.dat').write_text('keep')
    report,*_=tool.inspect(policy,target,raw_root=root/'raw')
    assert not report['ready'] and 'take over' in report['errors'][0]
    assert (target/'existing.dat').read_text()=='keep'


def test_apply_requires_inactive_runner_and_preserves_policy_backup(configured,monkeypatch):
    tool,root,policy=configured;target=root/'derived';original=policy.read_bytes();calls=[]
    monkeypatch.setattr(tool.os,'geteuid',lambda:0)
    monkeypatch.setattr(tool.subprocess,'run',lambda args,**kwargs: SimpleNamespace(stdout='active\n'))
    with pytest.raises(RuntimeError,match='stop runner'):
        tool.configure(policy,target,raw_root=root/'raw')
    assert not target.exists() and policy.read_bytes()==original
    monkeypatch.setattr(tool.subprocess,'run',lambda args,**kwargs: calls.append(args) or SimpleNamespace(stdout='inactive\n'))
    monkeypatch.setattr(tool.os,'chown',lambda *args: calls.append(('chown',args)))
    backup=root/'backup';backup.mkdir()
    monkeypatch.setattr(tool.tempfile,'mkdtemp',lambda **kwargs:str(backup))
    tool.configure(policy,target,raw_root=root/'raw')
    assert (backup/'policy.json').read_bytes()==original
    result=json.loads(policy.read_text())
    assert result['retained']=='value' and result['derived_dataset_root']==str(target)
    assert target.stat().st_mode & 0o7777==0o2710
    assert any(args[0]=='usermod' for args in calls)


def test_policy_links_are_rejected(configured):
    tool,root,policy=configured;linked=root/'linked.json';linked.symlink_to(policy)
    report,*_=tool.inspect(linked,root/'derived',raw_root=root/'raw')
    assert not report['ready']


def test_service_overrides_use_root_owned_read_only_home_stub_and_only_allow_managed_store_writes(configured,monkeypatch):
    tool,root,policy=configured
    content=tool.service_overrides(root/'raw',root/'derived','runner')
    broker=content['researchkit-broker.service'];runner=content['researchkit-runner.service']
    home_stub = 'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0'
    for override in (broker, runner):
        assert 'ProtectHome=false' in override
        assert home_stub in override
        assert 'InaccessiblePaths=/root /run/user' in override
    assert f'BindPaths="{root}/derived"' in broker
    assert 'BindPaths=' not in runner and 'ReadWritePaths=' not in runner
    assert f'BindReadOnlyPaths="{root}/raw" "{root}/derived"' in runner
    with pytest.raises(RuntimeError,match='unsupported'):
        tool.service_overrides(root/'raw%escape',root/'derived','runner')


def test_reviewed_bundle_is_activated_with_narrow_mounts_and_rejects_acl_metadata(configured,monkeypatch):
    from researchkit import web_datasets
    from test_web_dataset_batch import load_tool
    tool,root,policy=configured
    monkeypatch.setattr(web_datasets,'_root_owned',lambda info:info.st_uid in {0,os.getuid()})
    source=root/'raw'/'sample';source.mkdir();(source/'input.csv').write_text('value\n10\n')
    doc=load_tool('prepare-web-datasets').discover(root/'raw');doc['candidates'][0]['selected']=True
    candidates=root/'candidates.json';candidates.write_text(json.dumps(doc))
    bundle=root/'bundle'
    load_tool('prepare-web-dataset-trees').prepare_batch(candidates,bundle,all_platform_readers=True,
                                                       manifest_root=root/'dataset-manifests')
    metadata=json.loads((bundle/'datasets.metadata.json').read_text())
    ident=next(iter(metadata['datasets']));metadata['datasets'][ident]['tags']=['课堂','示例']
    (bundle/'datasets.metadata.json').write_text(json.dumps(metadata))
    report,*_=tool.inspect(policy,root/'derived',bundle,raw_root=root/'raw')
    assert report['ready'],report
    metadata['datasets'][ident]['readers']='*'
    (bundle/'datasets.metadata.json').write_text(json.dumps(metadata))
    report,*_=tool.inspect(policy,root/'derived',bundle,raw_root=root/'raw')
    assert not report['ready'] and not (root/'derived').exists()
    del metadata['datasets'][ident]['readers'];(bundle/'datasets.metadata.json').write_text(json.dumps(metadata))
    monkeypatch.setattr(tool.os,'geteuid',lambda:0)
    monkeypatch.setattr(tool.os,'chown',lambda *args:None)
    monkeypatch.setattr(tool.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout='inactive\n'))
    backup=root/'backup';backup.mkdir();monkeypatch.setattr(tool.tempfile,'mkdtemp',lambda **kwargs:str(backup))
    tool.configure(policy,root/'derived',bundle,raw_root=root/'raw')
    installed=json.loads(policy.read_text())
    assert installed['dataset_tree_registry']==str(root/'dataset-trees.json')
    assert (root/'dataset-manifests'/(ident+'.json')).read_bytes()==(bundle/'manifests'/(ident+'.json')).read_bytes()
    override=root/'units'/'researchkit-runner.service.d'/'researchkit-data.conf'
    override_text=override.read_text()
    assert 'ProtectHome=false' in override_text
    assert 'TemporaryFileSystem=/home:ro,mode=0755,uid=0,gid=0' in override_text
    assert 'InaccessiblePaths=/root /run/user' in override_text
    assert 'ReadWritePaths=' not in override_text
    assert (source/'input.csv').read_text()=='value\n10\n'
