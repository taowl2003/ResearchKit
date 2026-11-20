from __future__ import annotations
import hashlib,json,os
from pathlib import Path

import pytest

from researchkit import dataset_access
from researchkit.engine import Context
from researchkit.files import KitError


def test_context_reader_uses_mounted_declared_data_and_never_writes(tmp_path,monkeypatch):
    mount=tmp_path/'mount';root=tmp_path/'project';root.mkdir();(mount/'sample-v1').mkdir(parents=True)
    raw=b'value\n60\n';(mount/'sample-v1'/'numbers.csv').write_bytes(raw)
    reference={'dataset_id':'sample-v1','path':'numbers.csv','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    (root/'DATA-REFERENCES.json').write_text(json.dumps([reference]))
    monkeypatch.setattr(dataset_access,'_WEB_ROOT',mount)
    context=Context(root,{})
    with context.open_dataset('sample-v1','numbers.csv',mode='r') as stream:
        assert stream.read()=='value\n60\n'
    for mode in ('w','a','r+'):
        with pytest.raises(KitError,match='读取'):context.open_dataset('sample-v1','numbers.csv',mode=mode)
    with pytest.raises(KitError,match='未引用'):context.open_dataset('other-v1','numbers.csv')
    with pytest.raises(KitError,match='无效'):context.open_dataset('sample-v1','../numbers.csv')
    assert (mount/'sample-v1'/'numbers.csv').read_bytes()==raw


def test_local_reader_passes_exact_project_reference_and_uses_fd(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir();blob=tmp_path/'blob';blob.write_bytes(b'fixed bytes')
    reference={'dataset_id':'derived-abc','path':'data.bin','size':11,'sha256':hashlib.sha256(b'fixed bytes').hexdigest()}
    (root/'DATA-REFERENCES.json').write_text(json.dumps([reference]))
    monkeypatch.setattr(dataset_access,'_WEB_ROOT',tmp_path/'not-mounted')
    calls=[]
    def receive(dataset_id,path,expected):
        calls.append((dataset_id,path,expected));return os.open(blob,os.O_RDONLY)
    monkeypatch.setattr(dataset_access,'_broker_fd',receive)
    with Context(root,{}).open_dataset('derived-abc') as stream:
        fd=stream.fileno();assert stream.read()==b'fixed bytes'
    assert calls==[('derived-abc','data.bin',reference)]
    with pytest.raises(OSError):os.fstat(fd)


def test_tree_reader_declares_whole_dataset_but_asks_specific_file(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir();blob=tmp_path/'blob';blob.write_bytes(b'x')
    marker={'dataset_id':'whole-v1','path':'__researchkit_dataset_tree__','size':1000,'sha256':'a'*64}
    (root/'DATA-REFERENCES.json').write_text(json.dumps([marker]))
    monkeypatch.setattr(dataset_access,'_WEB_ROOT',tmp_path/'no-mount')
    seen=[]
    monkeypatch.setattr(dataset_access,'_broker_fd',lambda ident,path,expected:seen.append(expected) or os.open(blob,os.O_RDONLY))
    with Context(root,{}).open_dataset('whole-v1','nested/input.csv') as stream:assert stream.read()==b'x'
    assert seen==[marker]


@pytest.mark.parametrize('bad', [None, 'writable', 'wrong-version'])
def test_local_client_accepts_only_readonly_bound_broker_descriptor(tmp_path,monkeypatch,bad):
    import array,socket,struct,threading
    target=tmp_path/'blob';target.write_bytes(b'fixed')
    reference={'dataset_id':'derived-one','path':'data.bin','size':5,'sha256':hashlib.sha256(b'fixed').hexdigest()}
    route=tmp_path/'broker.sock'
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(str(route));listener.listen(1)
    monkeypatch.setattr(dataset_access,'_trusted_policy',lambda:(route,os.getuid()))
    observed=[]
    def serve():
        with listener.accept()[0] as connection:
            size=struct.unpack('!I',dataset_access._exact(connection,4))[0]
            observed.append(json.loads(dataset_access._exact(connection,size)))
            metadata={**reference,'sha256':'0'*64} if bad=='wrong-version' else reference
            raw=json.dumps({'ok':True,'result':metadata}).encode()
            descriptor=os.open(target,os.O_RDWR if bad=='writable' else os.O_RDONLY)
            try:
                connection.sendmsg([struct.pack('!I',len(raw))+raw],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[descriptor]))])
            finally:os.close(descriptor)
    thread=threading.Thread(target=serve);thread.start()
    try:
        if bad:
            with pytest.raises(KitError):dataset_access._broker_fd('derived-one','data.bin',reference)
        else:
            descriptor=dataset_access._broker_fd('derived-one','data.bin',reference)
            try:assert os.read(descriptor,5)==b'fixed'
            finally:os.close(descriptor)
    finally:
        thread.join(timeout=3);listener.close()
    assert not thread.is_alive()
    assert observed[0]['params']['expected_reference']==reference
