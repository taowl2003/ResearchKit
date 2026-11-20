"""Real Linux tmpfs masks with fabricated IDs inside a private user namespace.

This reproduces filesystem trust failure without changing host /home or units.
It is not acceptance of installed systemd service identities or cgroups.
"""
from pathlib import Path
import json
import subprocess


def test_root_trust_rejects_generic_home_tmpfs_and_accepts_explicit_readonly_mask():
    root=Path.cwd()
    program=r'''
import ctypes,json,os,pwd,stat,sys
from pathlib import Path
from types import MappingProxyType
sys.path.insert(0,'/code')
from researchkit.web_identity import Policy,PolicyError,_validate_derived_dataset_root
os.makedirs('/home/share/DerivedDatasets',mode=0o700)
os.chmod('/home/share/DerivedDatasets',0o2710)
policy=Policy(principals=MappingProxyType({}),workspaces=MappingProxyType({}),broker_uid=0,
              broker_username='root',submission_store=Path('/unused'),code_root=Path('/code'),
              runner_uid=0,runner_username='root',derived_dataset_root=Path('/home/share/DerivedDatasets'))
os.chmod('/home',0o777)
try:_validate_derived_dataset_root(policy)
except PolicyError as error:assert 'ancestor is not root-trusted: /home' in str(error)
else:raise AssertionError('generic writable tmpfs was incorrectly trusted')
os.chmod('/home',0o755)
assert _validate_derived_dataset_root(policy)==Path('/home/share/DerivedDatasets')
# Remount only this anonymous tmpfs. It has no host data mounted beneath it.
libc=ctypes.CDLL(None,use_errno=True)
assert libc.mount(None,b'/home',None,ctypes.c_ulong(32|1),None)==0,ctypes.get_errno()
try:Path('/home/forbidden').write_text('must fail')
except OSError as error:assert error.errno==30
else:raise AssertionError('home mask is writable')
assert _validate_derived_dataset_root(policy)==Path('/home/share/DerivedDatasets')
assert not Path('/home/ly').exists() and not Path('/root').exists()
print(json.dumps({'home_uid':os.stat('/home').st_uid,'home_mode':oct(stat.S_IMODE(os.stat('/home').st_mode)),
                  'read_only':True,'host_homes_hidden':True}))
'''
    command=['/usr/bin/bwrap','--unshare-all','--uid','0','--gid','0','--cap-add','CAP_SYS_ADMIN',
             '--tmpfs','/','--ro-bind','/usr','/usr','--symlink','usr/lib','/lib',
             '--symlink','usr/lib64','/lib64','--symlink','usr/bin','/bin',
             '--ro-bind','/etc/passwd','/etc/passwd','--ro-bind',str(root/'core'),'/code',
             '--tmpfs','/home','/usr/bin/python3.12','-I','-c',program]
    completed=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert completed.returncode==0,completed.stderr
    result=json.loads(completed.stdout)
    assert result=={'home_uid':0,'home_mode':'0o755','read_only':True,'host_homes_hidden':True}
