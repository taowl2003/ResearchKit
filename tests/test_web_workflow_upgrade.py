"""A frozen-release store survives the cumulative workflow schema additions."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

from researchkit.web_identity import Principal
from researchkit.web_jobs import Jobs
from researchkit.web_review_flow import ReviewFlow
from researchkit.web_sharing import ComponentService
from researchkit.web_store import SubmissionStore
from researchkit.web_submission_flow import SubmissionFlow
from researchkit.web_task_delivery import TaskDelivery


@pytest.fixture
def upgrade_directory():
    directory=Path(tempfile.mkdtemp(prefix='.rk-workflow-upgrade-',dir=Path.cwd()))
    directory.chmod(0o700)
    try:
        yield directory
    finally:
        # Only these generated copies have their directory modes adjusted.
        for path in directory.rglob('*'):
            if path.is_dir():path.chmod(0o700)
        shutil.rmtree(directory)


def test_previous_store_preserves_history_grants_and_content(upgrade_directory):
    tmp_path=upgrade_directory
    root=Path(__file__).resolve().parents[1]
    previous=Path(os.environ.get('RK_TEST_PREVIOUS_RELEASE',root/'exports/releases/web-v0.7-navigation-groups-20261110'))
    if not (previous/'core/researchkit/web_store.py').is_file():
        previous=root.parent/'web-v0.7-navigation-groups-20261110'
    if not (previous/'core/researchkit/web_store.py').is_file():
        pytest.skip('需要上一冻结包副本；可用 RK_TEST_PREVIOUS_RELEASE 指定，只复制到临时目录进行演练')
    # Both old code and its library run from disposable copies. The frozen
    # release is only read while copying and can never be a publication target.
    shutil.copytree(previous/'core',tmp_path/'old-core')
    library=tmp_path/'library'
    shutil.copytree(previous/'library',library)
    source='''import json,sys
from pathlib import Path
from researchkit.web_store import SubmissionStore
from researchkit.web_identity import Principal
from researchkit.web_sharing import ComponentService
from researchkit.web_jobs import Jobs
from researchkit.web_assets import AssetService
from researchkit.web_content import capacity
store=SubmissionStore(Path(sys.argv[1]))
ly=Principal(3401,'ly','teacher');student=Principal(3402,'test','student')
library=Path(sys.argv[2]);service=ComponentService(store,library)
files={'project.yaml':b'schema: researchkit.project/v1\\nname: previous lesson\\nmode: python\\nentry: case.py\\nplugins: {}\\nparameters: {}\\ndata: {}\\nrequires: {python: "3.11", modules: [], minimums: {}}\\n','case.py':b'def run(context):\\n    return {"value": 7}\\n'}
project=store.create_project(student,'previous lesson')
sub=store.submit(project['project_id'],student,'major',files,[])
jobs=Jobs(store);jobs.start_worker('previous-worker',3403)
for op in ('check','run'):
    job=jobs.request(student,sub['submission_id'],op)
    jobs.claim('previous-worker');jobs.append_log('previous-worker',job['run_id'],'old success\\n')
    jobs.finish('previous-worker',job['run_id'],'succeeded',0,None,{'results/value.txt':b'7'})
assets=AssetService(store,library)
case=assets.create(ly,sub['submission_id'],'template','previous.lesson','1.0.0')
case=assets.review(ly,case['candidate_id'],case['candidate_sha256'],'verified previous release')
assets.publish(ly,case['candidate_id'],case['candidate_sha256'])
row=next(item for item in service.access(ly) if item['kind']=='template' and item['ref']=='previous.lesson@1.0.0')
service.set_access(ly,'template','previous.lesson@1.0.0',False,[student],row['revision'])
print(json.dumps({'submission_id':sub['submission_id'],'sha256':sub['content_sha256'],'capacity':capacity(store)}))
'''
    env={**os.environ,'PYTHONPATH':str(tmp_path/'old-core'),'PYTHONDONTWRITEBYTECODE':'1'}
    process=subprocess.run([sys.executable,'-c',source,str(tmp_path/'store'),str(library)],
                           cwd=tmp_path,env=env,capture_output=True,text=True,check=False)
    assert process.returncode==0,process.stderr
    old=json.loads(process.stdout)
    store=SubmissionStore(tmp_path/'store')
    ly=Principal(3401,'ly','teacher');student=Principal(3402,'test','student')
    SubmissionFlow(store,library);TaskDelivery(store)
    review=ReviewFlow(store,library);service=ComponentService(store,library)
    frozen=store.get_submission(old['submission_id'],ly)
    assert frozen['content_sha256']==old['sha256']
    assert frozen['files']['case.py']==b'def run(context):\n    return {"value": 7}\n'
    # Existing job rows survive the schema migration for retention/audit until
    # the explicit retirement installer is run.  New review is manual and no
    # longer treats these legacy rows as a publication gate.
    released=review.release(ly,old['submission_id'])
    assert released['review_mode']=='manual'
    assert service.template(student,'previous.lesson@1.0.0')['files']['case.py']==frozen['files']['case.py']
    historic=Jobs(store).list(student,personal=True)
    assert {item['operation'] for item in historic}=={'check','run'}
    assert len(store.list_submissions(student,personal=True))==1
    assert store.list_submissions(ly,personal=True)==[]
    from researchkit.web_content import capacity
    now=capacity(store)
    assert now['unique_bytes']==old['capacity']['unique_bytes']
    assert now['file_references']==old['capacity']['file_references']


@pytest.mark.parametrize('startup', ['broker', 'local'])
def test_last_workflow_task_survives_startup_migration_and_same_request_retry(upgrade_directory, startup):
    from types import MappingProxyType
    from researchkit.web_app import LocalStoreClient
    from researchkit.web_broker import BrokerServer
    from researchkit.web_identity import Policy
    root = Path(__file__).resolve().parents[1]
    previous = root / 'exports/releases/web-v0.7-workflow-20261112'
    if not (previous / 'core/researchkit/web_task_delivery.py').is_file():
        previous = root.parent / 'web-v0.7-workflow-20261112'
    if not (previous / 'core/researchkit/web_task_delivery.py').is_file():
        pytest.skip('需要上一冻结workflow包的只读副本')
    directory = upgrade_directory
    shutil.copytree(previous / 'core', directory / 'old-core')
    source = '''import json,sys,os
from pathlib import Path
from researchkit.web_store import SubmissionStore
from researchkit.web_identity import Principal
from researchkit.web_task_delivery import TaskDelivery
from researchkit.web_content import capacity
store=SubmissionStore(Path(sys.argv[1]));ly=Principal(os.getuid(),'ly','teacher');test=Principal(os.getuid()+1000,'test','student')
project=store.create_project(ly,'old task')
baseline=store.submit(project['project_id'],ly,'major',{'case.py':b'def run(context): return {}'},[])
payload={'request_id':'64059c87-5d00-49a7-9994-d69e058f4ad9','name':'old formal task','instructions':'keep old bytes'}
task=TaskDelivery(store).create_task(ly,payload,baseline_submission_id=baseline['submission_id'],targets=[test])
print(json.dumps({'task':task,'baseline':baseline['submission_id'],'payload':payload,'capacity':capacity(store)}))
'''
    process = subprocess.run([sys.executable, '-c', source, str(directory / 'store')], cwd=directory,
        env={**os.environ, 'PYTHONPATH':str(directory / 'old-core'), 'PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    old = json.loads(process.stdout)
    teacher = Principal(os.getuid(), 'ly', 'teacher')
    student = Principal(os.getuid()+1000, 'test', 'student')
    if startup == 'broker':
        policy = Policy(MappingProxyType({teacher.uid:teacher, student.uid:student}), MappingProxyType({}),
            teacher.uid, 'ly', directory / 'store', directory, broker_socket=directory / 'broker.sock')
        server = BrokerServer(policy)
        store = server.store
    else:
        store = SubmissionStore(directory / 'store')
        LocalStoreClient(store, teacher)
    with store._connection() as connection:
        assert connection.execute('SELECT delivery_mode FROM web_task_deliveries').fetchone()[0] == 'project'
    delivery = TaskDelivery(store)
    current = delivery.create_task(teacher, old['payload'], baseline_submission_id=old['baseline'], targets=[student])
    assert current['task_id'] == old['task']['task_id']
    assert current['distributions'][0]['distribution_id'] == old['task']['distributions'][0]['distribution_id']
    assert len(delivery.list_tasks(student, personal=True)) == 1
    from researchkit.web_content import capacity
    now = capacity(store)
    assert now['file_references'] == old['capacity']['file_references']
    assert now['unique_bytes'] == old['capacity']['unique_bytes']
