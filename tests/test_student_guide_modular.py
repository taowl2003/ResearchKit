"""Follow the merged guide in a temporary workspace using real Web operations.

No service, shared dataset or real user project is read or changed.
"""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile

import yaml

from researchkit.web_app import LocalStoreClient
from researchkit.web_identity import Principal
from researchkit.web_store import SubmissionStore
from researchkit.web_workspace import Workspace
from researchkit.web_workspace_materials import add_algorithms

ROOT=Path(__file__).resolve().parents[1]


def test_modular_guide_blank_web_algorithms_two_ide_runs_and_web_artifacts():
    guide=(ROOT/'docs/学生使用指南.md').read_text()
    case_section=guide[guide.index('## 3. 第三个案例'):guide.index('## 完成练习后')]
    schemes=[yaml.safe_load(block) for block in re.findall(r'```yaml\n(.*?)```',case_section,re.S)]
    assert len(schemes)==2
    assert schemes[0]['algorithm_parameters']['sim']==schemes[1]['algorithm_parameters']['sim']
    assert schemes[0]['case_parameters']==schemes[1]['case_parameters']=={}
    with tempfile.TemporaryDirectory(prefix='.rk-modular-guide-',dir=ROOT) as temporary:
        root=Path(temporary)
        username=pwd.getpwuid(os.getuid()).pw_name
        actor=Principal(os.getuid(),username,'teacher' if username=='ly' else 'student')
        store=SubmissionStore(root/'store')
        workspace=Workspace(actor,root/'workspace',LocalStoreClient(store,actor))
        project=workspace.create_project('tdoa-study')
        pid=project['project_id'];project_root=Path(project['path'])
        source=ROOT/'examples/three-cases/acoustic-modular'
        for alias in ('sim','doa','locator'):
            algorithm=source/'algorithms'/alias
            metadata=yaml.safe_load((algorithm/'plugin.yaml').read_text())
            local=workspace.create_local_algorithm({
                **{field:metadata[field] for field in ('name','version','parameters','inputs','outputs')},
                'tags':['水声','定位','教学'],'description':'三阶段定位教学算法',
            })
            bound=add_algorithms(workspace,pid,{'local_algorithm_id':local['id'],'alias':alias,'parameters':{}})
            assert bound['path']==f'algorithms/{alias}'
            current=workspace.read_text(pid,f'algorithms/{alias}/plugin.py')
            assert 'return {}' in current['text']
            workspace.save_text(pid,f'algorithms/{alias}/plugin.py',(algorithm/'plugin.py').read_text(),current['sha256'])
        entry=workspace.read_text(pid,'case.py')
        workspace.save_text(pid,'case.py',(source/'case.py').read_text(),entry['sha256'])
        workspace.enable_experiments(pid)
        results=[]
        for scheme in schemes:
            speed=int(scheme['algorithm_parameters']['locator']['sound_speed_m_s'])
            scheme_id=f'sound-speed-{speed}'
            # IDE writes YAML, then Web reads the exact same local file.
            path=project_root/'experiments'/f'{scheme_id}.yaml'
            path.parent.mkdir(exist_ok=True)
            path.write_text(yaml.safe_dump(scheme,allow_unicode=True,sort_keys=False))
            process=subprocess.run([sys.executable,str(project_root/'run_experiment.py'),'--scheme',scheme_id],
                                   cwd=project_root,capture_output=True,text=True,timeout=60)
            assert process.returncode==0,process.stdout+'\n'+process.stderr
            summary_path,=project_root.glob(f'results/{scheme_id}/*/summary.json')
            summary=json.loads(workspace.read_text(pid,summary_path.relative_to(project_root).as_posix())['text'])
            simulation_path,=project_root.glob(f'results/{scheme_id}/*/simulation.json')
            simulation=json.loads(simulation_path.read_text())
            trajectory_path,=project_root.glob(f'results/{scheme_id}/*/localized_trajectory.csv')
            assert len(trajectory_path.read_text().splitlines())==11
            assert summary['count']==10
            results.append((summary,simulation))
        assert results[0][1]==results[1][1]
        assert results[0][0]['rmse_m']<1e-7
        assert results[1][0]['rmse_m']>1
        assert {row['scheme_id'] for row in workspace.experiment_catalog(pid)['runs']}=={'sound-speed-1500','sound-speed-1480'}
