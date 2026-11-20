"""Public student handoff must be readable offline and never contain a profile."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


def builder():
    spec=importlib.util.spec_from_file_location('student_kit_builder',ROOT/'tools/build-student-kit.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_offline_student_zip_roundtrip_and_scheme_templates(tmp_path,monkeypatch):
    kit=builder(); package=tmp_path/'student.zip'
    monkeypatch.setattr(sys,'argv',['build-student-kit.py','--output',str(package)])
    kit.main()
    monkeypatch.setattr(sys,'argv',['build-student-kit.py','--check','--output',str(package)])
    kit.main()
    with ZipFile(package) as archive:
        files={n.removeprefix(kit.TOP+'/'):archive.read(n) for n in archive.namelist()}
    assert kit.validate(files)==1
    assert kit.ENTRY in files
    assert [name for name in files if name.endswith('.html')]==[kit.ENTRY]
    assert not any(name.endswith('.md') for name in files)
    assert {Path(name).parts[0] for name in files}=={kit.ENTRY,'启动器','辅助文件'}
    guide=files[kit.ENTRY].decode()
    assert '<h2 id="第一部分首次连接与准备">' in guide
    assert '三大全流程实战案例（三阶段模块化版）' in guide
    assert '第三部分：功能与操作备查' in guide
    assert 'case.py' in guide and 'localized_trajectory.csv' in guide
    assert 'href="#参考三ide-断点调试与多方案启动"' in guide
    for name in kit.LAUNCHERS:
        assert files['启动器/'+name]==(ROOT/'tools/windows'/name).read_bytes()
    for config in json.loads(files['辅助文件/launch.json'])['configurations']:
        scheme=config['args'][-1]
        # The Web-first tutorial deliberately creates cnn-* names, while the
        # optional offline reference project uses baseline/learning-rate.
        reference_name={'cnn-baseline':'baseline','cnn-learning-rate':'learning-rate'}.get(scheme,scheme)
        sources=list((ROOT/'examples/three-cases').glob('*/experiments/'+reference_name+'.yaml'))
        assert len(sources)==1,scheme
        assert '--scheme '+scheme in guide
        assert yaml.safe_load(sources[0].read_text())['schema']=='researchkit.experiment/v1'
        assert config['program']=='${workspaceFolder}/run_experiment.py'
    assert '注入自动解包补丁' not in guide


def test_personal_connection_and_unexpected_files_are_never_packaged(tmp_path,monkeypatch):
    kit=builder(); source=tmp_path/'source'
    for folder in ['tools/windows','tools/student-kit','docs']:
        (source/folder).mkdir(parents=True)
    for name in (*kit.LAUNCHERS,'README.md'):
        shutil.copyfile(ROOT/'tools/windows'/name,source/'tools/windows'/name)
    for name in kit.SUPPORT:
        shutil.copyfile(ROOT/'tools/student-kit'/name,source/'tools/student-kit'/name)
    for name in kit.DOCS:
        shutil.copyfile(ROOT/'docs'/name,source/'docs'/name)
    secret='PRIVATE_PROFILE_SHOULD_NEVER_LEAVE_THIS_COMPUTER'
    (source/'tools/windows/connection.json').write_text(secret)
    (source/'tools/windows/id_ed25519').write_text(secret)
    (source/'tools/student-kit/extra-secrets.txt').write_text(secret)
    monkeypatch.setattr(kit,'ROOT',source)
    files=kit.outputs();kit.validate(files)
    assert not any(secret.encode() in value for value in files.values())
    assert not any('connection.json'==Path(name).name for name in files)


def test_builder_refuses_to_replace_a_different_existing_kit(tmp_path,monkeypatch):
    kit=builder();package=tmp_path/'already-configured.zip'
    with ZipFile(package,'w') as archive:
        archive.writestr('connection.json','keep this personal file')
    previous=package.read_bytes()
    monkeypatch.setattr(sys,'argv',['build-student-kit.py','--output',str(package)])
    with pytest.raises(AssertionError,match='Existing kit differs'):
        kit.main()
    assert package.read_bytes()==previous
