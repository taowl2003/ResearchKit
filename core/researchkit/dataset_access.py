"""Read declared datasets in both isolated Web runs and server-side IDE runs.

The local client receives a read-only descriptor from the authenticated broker;
no private blob paths, temporary downloads or user-configurable broker endpoints.
"""
from __future__ import annotations

import array
import fcntl
import json
import os
import re
import socket
import stat
import struct
from pathlib import Path

from .files import KitError

_POLICY_PATH = Path('/etc/researchkit-web/policy.json')
_WEB_ROOT = Path('/datasets')
_TREE_PATH = '__researchkit_dataset_tree__'
_MAX_METADATA = 64 * 1024


def _root_owned(info):
    return info.st_uid == 0


def _trusted_policy():
    path = _POLICY_PATH
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _root_owned(info) or info.st_mode & 0o022:
            raise KitError('本机数据服务配置路径不受信任')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not _root_owned(info) or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > _MAX_METADATA:
            raise KitError('本机数据服务配置不受信任')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(_MAX_METADATA+1)
        policy=json.loads(raw)
        uid=policy['broker']['uid']; route=Path(policy.get('broker_socket','/run/researchkit-web/broker.sock'))
        if type(uid) is not int or uid < 1 or not route.is_absolute() or '..' in route.parts:
            raise KitError('本机数据服务身份无效')
        for parent in reversed(route.parents):
            info=parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0,uid} or info.st_mode & 0o022:
                raise KitError('本机数据服务通信路径不受信任')
        endpoint=route.lstat()
        if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != uid:
            raise KitError('本机数据服务通信端点不受信任')
        return route,uid
    except (OSError,ValueError,KeyError,TypeError,RecursionError) as exc:
        raise KitError('无法读取本机数据服务配置') from exc
    finally:os.close(fd)


def _exact(channel,count):
    parts=[]
    while count:
        block=channel.recv(count)
        if not block:raise KitError('数据服务响应不完整')
        parts.append(block);count-=len(block)
    return b''.join(parts)


def _broker_fd(dataset_id,path,expected):
    route,uid=_trusted_policy();descriptors=[]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
            channel.settimeout(600)
            channel.connect(str(route))
            peer=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
            if peer!=uid:raise KitError('数据服务身份不匹配')
            raw=json.dumps({'method':'open_dataset_file','params':{'dataset_id':dataset_id,'path':path,'expected_reference':expected}}).encode()
            channel.sendall(struct.pack('!I',len(raw))+raw)
            header,ancillary,flags,_=channel.recvmsg(4,socket.CMSG_SPACE(16),getattr(socket,'MSG_CMSG_CLOEXEC',0))
            for level,kind,data in ancillary:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    values=array.array('i');values.frombytes(data[:len(data)-len(data)%values.itemsize]);descriptors.extend(values)
            if flags & (getattr(socket,'MSG_CTRUNC',0)|getattr(socket,'MSG_TRUNC',0)):
                raise KitError('数据服务文件描述符响应被截断')
            header+=_exact(channel,4-len(header));size=struct.unpack('!I',header)[0]
            if size<1 or size>_MAX_METADATA:raise KitError('数据服务元数据响应过大')
            response=json.loads(_exact(channel,size))
            if type(response) is not dict or response.get('ok') is not True:
                raise KitError(str(response.get('error','数据服务拒绝访问'))[:500] if isinstance(response,dict) else '数据服务响应无效')
            meta=response.get('result')
            if (len(descriptors)!=1 or type(meta) is not dict or set(meta)!={'dataset_id','path','size','sha256'} or
                meta['dataset_id']!=dataset_id or meta['path']!=path or type(meta['size']) is not int or meta['size']<0 or
                not isinstance(meta['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',meta['sha256'])):
                raise KitError('数据服务文件响应不匹配')
            fd=descriptors[0];info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size!=meta['size'] or
                    fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_RDONLY):
                raise KitError('数据服务未提供只读普通文件')
            if expected['path']!=_TREE_PATH and meta!=expected:
                raise KitError('数据服务文件与项目固定引用不一致')
            os.set_inheritable(fd,False);descriptors.clear();return fd
    except (OSError,ValueError,UnicodeError,RecursionError) as exc:
        raise KitError('数据服务文件访问失败：'+str(exc)[:300]) from exc
    finally:
        for fd in descriptors:
            os.close(fd)


def open_dataset(case_dir, dataset_id, path='data.bin', *, mode='rb', encoding='utf-8'):
    if mode not in {'r','rb'}:
        raise KitError('数据集只允许读取')
    if (not isinstance(dataset_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',dataset_id) or
            not isinstance(path,str) or path.startswith('/') or '\\' in path or
            any(part in {'','.','..'} for part in path.split('/'))):
        raise KitError('数据集 ID 或相对文件路径无效')
    try:
        references=json.loads((Path(case_dir)/'DATA-REFERENCES.json').read_text())
        if type(references) is not list:raise ValueError
    except (OSError,ValueError,RecursionError) as exc:
        raise KitError('项目缺少有效的 DATA-REFERENCES.json') from exc
    expected=next((ref for ref in references if type(ref) is dict and ref.get('dataset_id')==dataset_id and ref.get('path')==path),None)
    if expected is None:
        expected=next((ref for ref in references if type(ref) is dict and ref.get('dataset_id')==dataset_id and ref.get('path')==_TREE_PATH),None)
    if expected is None or set(expected)!={'dataset_id','path','size','sha256'}:
        raise KitError('项目未引用该数据文件或数据集版本')
    if _WEB_ROOT.is_dir():
        target=_WEB_ROOT/dataset_id/path
        fd=os.open(target,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    else:
        fd=_broker_fd(dataset_id,path,expected)
    try:
        return os.fdopen(fd,mode,**({'encoding':encoding} if mode=='r' else {}))
    except Exception:
        os.close(fd);raise
