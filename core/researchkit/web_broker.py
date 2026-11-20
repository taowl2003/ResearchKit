"""Local AF_UNIX broker for immutable Web submissions.

The broker has no HTTP listener and never accepts a caller's UID, role, user
name, or workspace path.  Each connection obtains its principal from Linux
``SO_PEERCRED`` and carries exactly one bounded JSON request.
"""
from __future__ import annotations

import argparse
import array
import base64
import contextlib
import fcntl
import json
import os
import pwd
import re
import signal
import socket
import stat
import struct
import threading
import time
from pathlib import Path
from typing import Any

from .files import KitError
from .web_identity import IdentityError, Policy, PolicyError, Principal, load_policy, peer_principal, validate_policy
from .web_store import SubmissionStore


MAX_FRAME = 96 * 1024 * 1024
SOCKET_TIMEOUT = 10.0
LONG_OPERATION_TIMEOUT = 600.0
DEFAULT_SOCKET = Path('/run/researchkit-web/broker.sock')
DEFAULT_WORKERS = 8
_FORBIDDEN_KEYS = {'uid', 'role', 'username', 'workspace', 'workspace_path', 'path'}
_SECRET_PATH = re.compile(r'/(?:[^\s:]+)')


class BrokerError(KitError):
    """A safe, actionable local-protocol error."""


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError('JSON 请求含有重复字段')
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise BrokerError(f'JSON 不允许常量 {value}')


def _json_loads(payload: bytes) -> object:
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise BrokerError('请求必须是 UTF-8 JSON') from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise BrokerError('请求不是有效 JSON') from exc


def _json_dumps(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise BrokerError('响应无法编码') from exc


def _frame_payload(value: object, *, response: bool = False) -> bytes:
    payload = _json_dumps(value)
    if len(payload) <= MAX_FRAME:
        return payload
    if response:
        return _json_dumps({'ok': False, 'error': '响应超过 96 MiB 上限，请缩小请求范围'})
    raise BrokerError('请求帧超过 96 MiB 上限')


def _recv_exact(connection: socket.socket, length: int, *, deadline: float | None = None) -> bytes:
    if deadline is None:
        deadline = time.monotonic() + SOCKET_TIMEOUT
    chunks = bytearray()
    while len(chunks) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerError('连接读取超时')
        connection.settimeout(remaining)
        try:
            block = connection.recv(length - len(chunks))
        except socket.timeout as exc:
            raise BrokerError('连接读取超时') from exc
        except OSError as exc:
            raise BrokerError('连接读取失败') from exc
        if not block:
            raise BrokerError('请求帧不完整')
        chunks.extend(block)
    return bytes(chunks)


def _recv_frame(connection: socket.socket, *, timeout: float | None = None) -> object:
    deadline = time.monotonic() + (SOCKET_TIMEOUT if timeout is None else timeout)
    size = struct.unpack('!I', _recv_exact(connection, 4, deadline=deadline))[0]
    if size > MAX_FRAME:
        raise BrokerError('请求帧超过 96 MiB 上限')
    return _json_loads(_recv_exact(connection, size, deadline=deadline))


def _send_frame(connection: socket.socket, value: object, *, response: bool = False) -> None:
    payload = _frame_payload(value, response=response)
    try:
        connection.sendall(struct.pack('!I', len(payload)) + payload)
    except (socket.timeout, OSError):
        # The request has already been handled; a disconnected peer needs no
        # secondary server-side error or traceback.
        return


def _send_frame_fd(connection: socket.socket, value: object, descriptor: int) -> None:
    """Send a small broker result and exactly one read-only FD together."""
    if not isinstance(descriptor, int) or descriptor < 0:
        raise BrokerError('数据文件描述符无效')
    payload = _frame_payload(value, response=True)
    frame = struct.pack('!I', len(payload)) + payload
    rights = array.array('i', [descriptor])
    try:
        sent = connection.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
        if sent < len(frame):
            connection.sendall(frame[sent:])
    except (socket.timeout, OSError):
        return


def _recv_frame_fd(connection: socket.socket, *, timeout: float | None = None) -> tuple[object, int | None]:
    """Receive one response frame plus its single SCM_RIGHTS descriptor."""
    deadline = time.monotonic() + (SOCKET_TIMEOUT if timeout is None else timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrokerError('连接读取超时')
    connection.settimeout(remaining)
    try:
        header, ancillary, flags, _ = connection.recvmsg(4, socket.CMSG_SPACE(array.array('i').itemsize * 4))
    except (socket.timeout, OSError) as exc:
        raise BrokerError('连接读取失败') from exc
    if len(header) < 4:
        header += _recv_exact(connection, 4 - len(header), deadline=deadline)
    if flags & getattr(socket, 'MSG_CTRUNC', 0):
        raise BrokerError('数据文件描述符响应被截断')
    descriptors: list[int] = []
    for level, kind, content in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array('i')
            values.frombytes(content[:len(content) - len(content) % values.itemsize])
            descriptors.extend(values)
    if len(descriptors) > 1:
        for descriptor in descriptors:
            os.close(descriptor)
        raise BrokerError('数据文件描述符响应无效')
    size = struct.unpack('!I', header)[0]
    try:
        if size > MAX_FRAME:
            raise BrokerError('响应帧超过 96 MiB 上限')
        return _json_loads(_recv_exact(connection, size, deadline=deadline)), (descriptors[0] if descriptors else None)
    except Exception:
        if descriptors:
            os.close(descriptors[0])
        raise


def _safe_message(error: Exception) -> str:
    """Keep KitError guidance while never exposing server filesystem paths."""
    message = str(error).strip() or '请求被拒绝'
    return _SECRET_PATH.sub('[已隐藏路径]', message)


def _socket_path(value: Path | str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor) or '..' in path.parts:
        raise BrokerError('broker socket 必须是非根绝对路径')
    return path


def _trusted_socket_route(path: Path, broker_uid: int, *, require_socket: bool) -> None:
    """Check the public socket route without resolving a possible symlink."""
    if broker_uid < 1:
        raise BrokerError('broker UID 无效')
    chain = list(reversed(path.parents))
    for directory in chain:
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise BrokerError('broker socket 父目录不存在') from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BrokerError('broker socket 父路径必须是非链接目录')
        if info.st_uid not in {0, broker_uid}:
            raise BrokerError('broker socket 父目录所有者不可信')
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise BrokerError('broker socket 父目录不能由第三方写入')
    parent = os.lstat(path.parent)
    if parent.st_uid != broker_uid:
        raise BrokerError('broker socket 目录必须由服务 UID 所有')
    if require_socket:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise BrokerError('broker socket 不存在') from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
            raise BrokerError('broker socket 必须是非链接 Unix socket')
        if info.st_uid != broker_uid or stat.S_IMODE(info.st_mode) != 0o666:
            raise BrokerError('broker socket 所有者或权限不可信')


def _expect_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise BrokerError(f'{label} 必须是对象')
    return value


def _expect_keys(value: object, allowed: set[str], required: set[str], label: str) -> dict[str, object]:
    result = _expect_object(value, label)
    if _FORBIDDEN_KEYS & set(result):
        raise BrokerError('身份和工作区不得出现在 broker 请求中')
    if set(result) - allowed or required - set(result):
        raise BrokerError(f'{label} 字段不合法')
    return result


def _decode_files(value: object) -> dict[str, bytes]:
    files = _expect_object(value, 'files')
    result: dict[str, bytes] = {}
    for name, encoded in files.items():
        if not isinstance(name, str) or not isinstance(encoded, str):
            raise BrokerError('files 必须是 base64 字符串映射')
        try:
            result[name] = base64.b64decode(encoded.encode('ascii'), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise BrokerError(f'files.{name} 不是有效 base64') from exc
    return result


def _encode_files(value: object) -> dict[str, str]:
    if type(value) is not dict:
        raise BrokerError('提交文件快照已损坏')
    result: dict[str, str] = {}
    for name, content in value.items():
        if not isinstance(name, str) or not isinstance(content, bytes):
            raise BrokerError('提交文件快照已损坏')
        result[name] = base64.b64encode(content).decode('ascii')
    return result


ASSET_METHODS = {'create_asset_candidate', 'list_asset_candidates', 'get_asset_candidate',
                 'review_asset_candidate', 'publish_asset_candidate'}
ASSET_VALIDATION_METHODS = {'list_asset_candidate_validations', 'create_asset_candidate_validation'}
DERIVED_METHODS = {'begin_derived_upload', 'append_derived_upload', 'commit_derived_upload',
                   'abort_derived_upload', 'publish_derived', 'get_derived_catalog',
                   'grant_derived_review'}


def asset_request(store, library_root, principal, method, params):
    """Shared schema for the trusted broker and isolated local demo adapter."""
    if principal.role != 'teacher' or principal.username != 'ly':
        raise BrokerError('只有教师可以提取、审阅和发布资产候选')
    from .web_assets import AssetService
    fields = {
        'create_asset_candidate': {'submission_id', 'kind', 'name', 'version', 'alias'},
        'list_asset_candidates': set(),
        'get_asset_candidate': {'candidate_id'},
        'review_asset_candidate': {'candidate_id', 'expected_sha256', 'notes', 'validation_run_id'},
        'publish_asset_candidate': {'candidate_id', 'expected_sha256'},
    }
    body = _expect_keys(params, fields[method], fields[method] - {'alias', 'validation_run_id'}, method)
    service = AssetService(store, library_root)
    if method == 'create_asset_candidate':
        return service.create(principal, body['submission_id'], body['kind'], body['name'],
                              body['version'], body.get('alias'))
    if method == 'list_asset_candidates':
        return service.list_candidates(principal)
    if method == 'get_asset_candidate':
        return service.get_candidate(principal, body['candidate_id'])
    if method == 'review_asset_candidate':
        return service.review(principal, body['candidate_id'], body['expected_sha256'],
                              body['notes'], body.get('validation_run_id'))
    service.publish(principal, body['candidate_id'], body['expected_sha256'])
    return service.get_candidate(principal, body['candidate_id'])


def asset_validation_request(store, library_root, principal, method, params, *, ready: bool,
                             dataset_registry=None, reference_authorizer=None):
    """Broker schema and readiness/ACL gate for internal candidate validation."""
    if principal.role != 'teacher' or principal.username != 'ly':
        raise BrokerError('只有教师可以创建或查看候选验证')
    fields = {
        'list_asset_candidate_validations': {'candidate_id'},
        'create_asset_candidate_validation': {'candidate_id', 'expected_sha256', 'operation', 'cases'},
    }
    body = _expect_keys(params, fields[method], {'candidate_id'} if method.startswith('list_')
                        else {'candidate_id', 'expected_sha256', 'operation'}, method)
    if method != 'list_asset_candidate_validations' and not ready:
        raise BrokerError('执行服务未就绪，未创建候选验证')
    from .web_asset_validation import AssetValidationService
    service = AssetValidationService(store, library_root)
    if method == 'list_asset_candidate_validations':
        return service.list(principal, body['candidate_id'])
    plan = service.prepare(principal, body['candidate_id'], body['expected_sha256'], body['operation'], body.get('cases'))
    if plan.data_references:
        if reference_authorizer is not None:
            reference_authorizer(plan.data_references)
        else:
            from .web_datasets import DatasetRegistry
            DatasetRegistry(dataset_registry).authorize([principal], plan.data_references)
    return service.create(principal, plan)


class BrokerServer:
    """One-request AF_UNIX server bound to a trusted policy and store."""

    def __init__(self, policy: Policy, socket_path: Path | str | None = None, *, workers: int = DEFAULT_WORKERS):
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 64:
            raise BrokerError('worker 数必须在 1 到 64 之间')
        if os.geteuid() == 0 or os.geteuid() != policy.broker_uid:
            raise BrokerError('broker 必须以 policy 中的非 root 服务 UID 运行')
        self.policy = policy
        configured = getattr(policy, 'broker_socket', DEFAULT_SOCKET)
        self.socket_path = _socket_path(socket_path if socket_path is not None else configured)
        self.store = SubmissionStore(policy.submission_store)
        # Migrate task ACL fields before any read-only catalog/runner request.
        from .web_task_delivery import TaskDelivery
        TaskDelivery(self.store)
        self._workers = threading.BoundedSemaphore(workers)
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._bound_identity: tuple[int, int] | None = None
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    def bind(self) -> None:
        if self._listener is not None:
            return
        _trusted_socket_route(self.socket_path, self.policy.broker_uid, require_socket=False)
        try:
            existing = os.lstat(self.socket_path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            raise BrokerError('broker socket 已存在；拒绝删除未知进程的 socket')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_mask = os.umask(0)
        try:
            listener.bind(str(self.socket_path))
        except OSError as exc:
            listener.close()
            raise BrokerError('无法绑定 broker socket') from exc
        finally:
            os.umask(old_mask)
        try:
            os.chmod(self.socket_path, 0o666)
            info = os.lstat(self.socket_path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.policy.broker_uid:
                raise BrokerError('broker socket 创建后的所有者不正确')
            self._bound_identity = (info.st_dev, info.st_ino)
            listener.listen(16)
            listener.settimeout(1.0)
            self._listener = listener
        except Exception:
            listener.close()
            self._unlink_own_socket()
            raise

    def _unlink_own_socket(self) -> None:
        identity = self._bound_identity
        if identity is None:
            return
        try:
            info = os.lstat(self.socket_path)
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                os.unlink(self.socket_path)
        except OSError:
            pass
        finally:
            self._bound_identity = None

    def close(self) -> None:
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self._unlink_own_socket()

    def serve_forever(self, *, install_signal_handler: bool = True) -> None:
        self.bind()
        previous = None
        if install_signal_handler and threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda _number, _frame: self.close())
        try:
            while not self._stopping.is_set():
                listener = self._listener
                if listener is None:
                    break
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    continue
                connection.settimeout(SOCKET_TIMEOUT)
                try:
                    peer_uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                    if peer_uid == 0 and self.policy.dataset_management_config is not None:
                        # Root is never a workspace user. This identity has only
                        # the bounded publisher protocol below, and only after
                        # an administrator opts in with a protected config.
                        principal = Principal(0, 'root', 'data-publisher')
                    elif self.policy.runner_uid is not None and peer_uid == self.policy.runner_uid:
                        account = pwd.getpwuid(peer_uid)
                        if account.pw_name != self.policy.runner_username:
                            raise IdentityError('执行账号身份变化')
                        principal = Principal(peer_uid, account.pw_name, 'runner')
                    else:
                        principal = peer_principal(connection, self.policy)
                except (IdentityError, PolicyError, OSError):
                    _send_frame(connection, {'ok': False, 'error': '认证失败'}, response=True)
                    connection.close()
                    continue
                if not self._workers.acquire(blocking=False):
                    _send_frame(connection, {'ok': False, 'error': 'broker 正忙，请稍后重试'}, response=True)
                    connection.close()
                    continue
                thread = threading.Thread(target=self._serve_connection, args=(connection, principal), daemon=True)
                with self._threads_lock:
                    self._threads.add(thread)
                thread.start()
        finally:
            self.close()
            self._join_workers()
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)

    def _join_workers(self) -> None:
        current = threading.current_thread()
        with self._threads_lock:
            workers = [worker for worker in self._threads if worker is not current]
        for worker in workers:
            worker.join(timeout=SOCKET_TIMEOUT)

    def _serve_connection(self, connection: socket.socket, principal: Principal) -> None:
        try:
            request = _recv_frame(connection)
            if (principal.role != 'runner' and type(request) is dict and
                    request.get('method') == 'open_dataset_file'):
                with self._open_dataset_file(principal, request.get('params')) as (descriptor, reference):
                    _send_frame_fd(connection, {'ok': True, 'result': {'reference': reference}}, descriptor)
                return
            result = self._dispatch(principal, request)
            _send_frame(connection, {'ok': True, 'result': result}, response=True)
        except BrokerError as exc:
            _send_frame(connection, {'ok': False, 'error': _safe_message(exc)}, response=True)
        except KitError as exc:
            _send_frame(connection, {'ok': False, 'error': _safe_message(exc)}, response=True)
        except Exception:
            _send_frame(connection, {'ok': False, 'error': 'broker 无法完成请求'}, response=True)
        finally:
            try:
                connection.close()
            finally:
                self._workers.release()
                current = threading.current_thread()
                with self._threads_lock:
                    self._threads.discard(current)

    def _target_principal(self, username: object) -> Principal:
        """Resolve a recipient through pwd and the same policy as a socket peer."""
        if not isinstance(username, str) or not username or len(username) > 64 or '\x00' in username:
            raise BrokerError('target_username 必须是有效系统用户名')
        try:
            account = pwd.getpwnam(username)
        except (KeyError, ValueError) as exc:
            raise BrokerError('target_username 不是允许的系统用户') from exc
        try:
            target = self.policy.principal(account.pw_uid)
        except IdentityError as exc:
            raise BrokerError('target_username 不是允许的 Web 用户') from exc
        if target.username != account.pw_name:
            raise BrokerError('target_username 的系统解析不一致')
        return target

    def _assignment_targets(self, principal, names, references):
        if principal.role != 'teacher':
            raise BrokerError('只有教师可以布置任务')
        if type(names) is not list or not 1 <= len(names) <= 100 or len(set(str(n) for n in names)) != len(names):
            raise BrokerError('任务需要 1 至 100 个不同目标账号')
        targets = [self._target_principal(name) for name in names]
        from .web_store import _validate_data_references
        refs = _validate_data_references(references)
        from .web_lifecycle import is_active
        for ref in refs:
            if not is_active(self.store, 'dataset', ref['dataset_id']):
                raise BrokerError('任务引用的数据集已下架，请先更换或恢复')
        self._authorized_source_references(principal, refs)
        self._task_access().validate_transfer(principal,targets,refs)
        return targets, refs

    def _task_access(self):
        from .web_task_access import TaskAccess
        from .web_datasets import DatasetRegistry
        from .web_dataset_trees import TreeRegistry
        return TaskAccess(self.store,DatasetRegistry(self.policy.dataset_registry),
                          TreeRegistry(self.policy.dataset_tree_registry),self._derived())

    def _derived(self):
        from .web_derived import DerivedDatasets
        return DerivedDatasets(self.store, self.policy.derived_dataset_root,
                               max_file_bytes=self.policy.derived_max_file_bytes,
                               user_quota_bytes=self.policy.derived_user_quota_bytes,
                               upload_ttl_seconds=self.policy.derived_upload_ttl_seconds)

    def _dataset_catalog(self, principal: Principal, *, include_inactive: bool = False) -> list[dict[str, object]]:
        """Join independent raw/tree/derived namespaces without weakening any ACL."""
        from .web_datasets import DatasetRegistry
        from .web_dataset_trees import TreeRegistry
        from .web_dataset_metadata import enrich_catalog
        catalog = self._task_access().catalog(principal)
        identifiers = [str(item['dataset_id']) for item in catalog]
        if len(set(identifiers)) != len(identifiers):
            raise BrokerError('不同数据来源使用了重复的数据集 ID')
        from .web_lifecycle import states
        inactive = states(self.store, 'dataset')
        if not include_inactive:
            catalog = [row for row in catalog if inactive.get(str(row['dataset_id']), 'active') == 'active']
        from .web_personal_resources import enrich_versions
        return enrich_versions(self.store, enrich_catalog(catalog, self.policy.dataset_metadata))

    def _dataset_files(self, principal: Principal, params: object) -> dict[str, object]:
        """Browse only file records already authorized for this principal."""
        body = _expect_keys(params, {'dataset_id', 'q', 'offset', 'limit'}, {'dataset_id'}, 'get_dataset_files')
        dataset_id, q = body['dataset_id'], body.get('q', '')
        offset, limit = body.get('offset', 0), body.get('limit', 100)
        if (not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 256 or
                not isinstance(q, str) or len(q) > 256 or type(offset) is not int or offset < 0 or
                type(limit) is not int or not 1 <= limit <= 200):
            raise BrokerError('get_dataset_files 参数无效')
        from .web_cleanup import require_available
        with self.store._connection() as connection:
            require_available(connection, 'dataset', dataset_id)
        from .web_dataset_trees import TreeRegistry, is_tree_reference
        item=next((row for row in self._dataset_catalog(principal) if row['dataset_id']==dataset_id),None)
        if item is None:raise BrokerError('数据集不存在、未登记或当前账号未获授权')
        references=item.get('files',[])
        if len(references)==1 and is_tree_reference(references[0]):
            effective=self._task_access().principal_for(principal,references[0])
            return TreeRegistry(self.policy.dataset_tree_registry).list_files(effective,dataset_id,q=q,offset=offset,limit=limit)
        files=[entry for entry in references if q.casefold() in str(entry['path']).casefold()]
        return {'items':files[offset:offset+limit],'total':len(files),
                'next_offset':offset+limit if offset+limit<len(files) else None}

    def _authorized_source_references(self, principal: Principal, references: object) -> list[dict[str, object]]:
        """Accept upload provenance only when it exactly names visible records."""
        from .web_store import _validate_data_references
        refs = _validate_data_references(references if references is not None else [])
        authorized = {
            json.dumps(file, sort_keys=True, separators=(',', ':'))
            for dataset in self._dataset_catalog(principal, include_inactive=True) for file in dataset['files']
        }
        for reference in refs:
            if json.dumps(reference, sort_keys=True, separators=(',', ':')) not in authorized:
                raise BrokerError('无权引用数据：数据集不存在或当前账号未获授权')
        return refs

    def _authorize_job_references(self, owner: Principal, requester: Principal,
                                  submission_id: str, references: object) -> list[dict[str, object]]:
        """Authorize all three data namespaces before queueing a job.

        Returning the derived descriptions makes their broker-approved identity
        available to the runner claim without ever reading a user path.
        """
        from .web_datasets import DatasetRegistry
        from .web_dataset_trees import TreeRegistry, is_tree_reference
        from .web_store import _validate_data_references
        refs = _validate_data_references(references)
        return self._task_access().authorize_job(owner,requester,submission_id,refs)['derived_mounts']

    @contextlib.contextmanager
    def _open_dataset_file(self, principal: Principal, params: object):
        """Resolve one IDE read from trusted catalogue records only."""
        body = _expect_object(params, 'open_dataset_file 参数')
        if set(body) != {'dataset_id', 'path', 'expected_reference'} or ({'uid', 'role', 'username', 'workspace', 'workspace_path'} & set(body)):
            raise BrokerError('open_dataset_file 参数字段不合法')
        if not isinstance(body['dataset_id'], str) or not isinstance(body['path'], str):
            raise BrokerError('open_dataset_file 参数必须是字符串')
        dataset_id, path = body['dataset_id'], body['path']
        from .web_store import _validate_data_references
        expected = _validate_data_references([body['expected_reference']])
        if len(expected) != 1 or expected[0]['dataset_id'] != dataset_id:
            raise BrokerError('数据文件期望引用无效')
        expected = expected[0]
        principal = self._task_access().principal_for(principal,expected)
        from .web_cleanup import require_available
        with self.store._connection() as connection:
            require_available(connection, 'dataset', dataset_id)
        derived = self._derived()
        if any(item['dataset_id'] == dataset_id for item in derived.catalog(principal)):
            with derived.open_file(principal, dataset_id, path) as (descriptor, reference):
                if reference != expected:
                    raise BrokerError('派生数据版本与项目引用不匹配')
                yield descriptor, reference
            return
        from .web_cleanup import require_available
        with self.store._connection() as connection:
            require_available(connection, 'dataset', dataset_id)
        from .web_datasets import DatasetRegistry
        from .web_dataset_trees import TreeRegistry
        raw = DatasetRegistry(self.policy.dataset_registry)
        for entry in raw.catalog(principal):
            if entry['dataset_id'] == dataset_id:
                reference = next((item for item in entry['files'] if item['path'] == path), None)
                if reference is None:
                    raise BrokerError('受控数据文件不存在')
                if reference != expected:
                    raise BrokerError('受控数据版本与项目引用不匹配')
                with raw.open_references([principal], [reference]) as mounts:
                    yield mounts[0].fd, reference
                return
        trees = TreeRegistry(self.policy.dataset_tree_registry)
        if any(item['dataset_id'] == dataset_id for item in trees.catalog(principal)):
            opener = getattr(trees, 'open_file', None)
            if not callable(opener):
                raise BrokerError('数据集树 IDE 文件读取尚未就绪')
            marker = next(item for item in trees.catalog(principal) if item['dataset_id'] == dataset_id)['files'][0]
            if marker != expected:
                raise BrokerError('数据集树版本与项目引用不匹配')
            descriptor, reference = opener(principal, dataset_id, path)
            try:
                yield descriptor, reference
            finally:
                os.close(descriptor)
            return
        raise BrokerError('数据集不存在、未登记或当前账号未获授权')

    def _runner_dispatch(self, principal: Principal, method: object, params: object):
        # This role is minted only from SO_PEERCRED and the root-owned policy.
        if (principal.uid != self.policy.runner_uid or principal.username != self.policy.runner_username
                or principal.uid in {0, self.policy.broker_uid}):
            raise BrokerError('执行账号无权访问任务协议')
        from .web_jobs import Jobs
        jobs = Jobs(self.store)
        required = {
            'runner_start': {'instance_id'}, 'runner_claim': {'instance_id'},
            'runner_poll': {'instance_id', 'run_id'},
            'runner_log': {'instance_id', 'run_id', 'text'},
            'runner_finish': {'instance_id', 'run_id', 'status', 'exit_code', 'reason', 'artifacts', 'staged_artifacts'},
            'runner_claim_retention': {'instance_id'},
            'runner_claim_expired_staging': {'instance_id'},
            'runner_staging_inventory': {'instance_id'},
            'runner_begin_retention': {'instance_id', 'request_id'},
            'runner_append_retention': {'instance_id', 'request_id', 'offset', 'data'},
            'runner_commit_retention': {'instance_id', 'request_id', 'sha256'},
            'runner_fail_retention': {'instance_id', 'request_id', 'reason'},
        }
        if not isinstance(method, str) or method not in required:
            raise BrokerError('执行账号只允许任务领取与状态回传')
        body = _expect_keys(params, required[method], required[method] - {'artifacts', 'staged_artifacts'}, str(method))
        instance = body['instance_id']
        if method == 'runner_start':
            return jobs.start_worker(instance, principal.uid)
        if method == 'runner_claim':
            from .web_submission_flow import SubmissionFlow
            flow=SubmissionFlow(self.store,self.policy.code_root/'library')
            with self.store._connection() as connection:
                pending=connection.execute("SELECT preparation_id FROM web_submission_preparations WHERE status IN ('checking','running')").fetchall()
            for row in pending:
                flow.advance(row['preparation_id'])
                with self.store._connection() as connection:
                    completed=connection.execute('SELECT * FROM web_submission_preparations WHERE preparation_id=?',
                                                 (row['preparation_id'],)).fetchone()
                if completed['final_submission_id']:
                    owner=Principal(completed['owner_uid'],completed['owner_username'],
                                    'teacher' if completed['owner_username']=='ly' else 'student')
                    source=self.store.get_submission(completed['final_submission_id'],owner)
                    self._derived().bind_submission(owner,source['submission_id'],source['manifest']['data_references']['references'],
                                                    self._target_principal('ly'))
            result = jobs.claim(instance)
            if result is not None:
                owner = Principal(result['owner_uid'], result['owner_username'],
                                  'teacher' if result['owner_username'] == 'ly' else 'student')
                requester = Principal(result['requester_uid'], result['requester_username'],
                                      'teacher' if result['requester_username'] == 'ly' else 'student')
                try:
                    result.update(self._task_access().authorize_job(
                        owner,requester,result['submission_id'],result['data_references']))
                except KitError as exc:
                    jobs.reject_claim(instance, result['run_id'], str(exc))
                    return None
                result['files'] = _encode_files(result['files'])
            return result
        if method == 'runner_poll':
            return jobs.poll(instance, body['run_id'])
        if method == 'runner_log':
            return jobs.append_log(instance, body['run_id'], body['text'])
        if method == 'runner_claim_retention':
            row = jobs.claim_artifact_retention(instance)
            if row is not None and row.get('retained_dataset_id'):
                requester = Principal(row['requester_uid'], row['requester_username'],
                                      'teacher' if row['requester_username'] == 'ly' else 'student')
                if requester.role == 'teacher':
                    self._derived().grant_run_output(requester, row['retained_dataset_id'], row['run_id'])
                jobs.finish_artifact_retention(instance, row['request_id'], dataset_id=row['retained_dataset_id'])
                return None
            return row
        if method == 'runner_claim_expired_staging':
            return jobs.claim_expired_staging(instance)
        if method == 'runner_staging_inventory':
            return jobs.staging_inventory(instance)
        if method == 'runner_begin_retention':
            row = jobs.retention_info(instance, body['request_id'])
            owner = Principal(row['owner_uid'], row['owner_username'],
                              'teacher' if row['owner_username'] == 'ly' else 'student')
            # Retention also applies to service-only candidate validation jobs;
            # those snapshots deliberately cannot use the user-facing getter.
            with self.store._connection() as connection:
                source, _files = self.store._source_files(connection, row['submission_id'])
                refs = self.store._stored_manifest(source)['data_references']['references']
            result = self._derived().begin_upload(owner, row['name'], row['size'], row['sha256'], refs)
            jobs.set_retention_upload(instance, body['request_id'], result['upload_id'])
            return result
        if method == 'runner_append_retention':
            if not isinstance(body['data'], str):
                raise BrokerError('派生产物上传块必须是 base64 字符串')
            try:
                data = base64.b64decode(body['data'].encode('ascii'), validate=True)
            except (UnicodeEncodeError, ValueError) as exc:
                raise BrokerError('派生产物上传块不是有效 base64') from exc
            row = jobs.retention_info(instance, body['request_id'])
            if not row['upload_id']:
                raise BrokerError('派生产物上传尚未开始')
            owner = Principal(row['owner_uid'], row['owner_username'],
                              'teacher' if row['owner_username'] == 'ly' else 'student')
            return self._derived().append_upload(owner, row['upload_id'], body['offset'], data)
        if method == 'runner_commit_retention':
            row = jobs.retention_info(instance, body['request_id'])
            if not row['upload_id'] or body['sha256'] != row['sha256']:
                raise BrokerError('派生产物提交摘要不匹配')
            owner = Principal(row['owner_uid'], row['owner_username'],
                              'teacher' if row['owner_username'] == 'ly' else 'student')
            result = self._derived().commit_upload(owner, row['upload_id'], body['sha256'],
                                                    source_kind='run', source_run_id=row['run_id'],
                                                    source_submission_id=row['submission_id'])
            jobs.finish_artifact_retention(instance, body['request_id'], dataset_id=result['dataset_id'])
            requester = Principal(row['requester_uid'], row['requester_username'],
                                  'teacher' if row['requester_username'] == 'ly' else 'student')
            if requester.role == 'teacher':
                self._derived().grant_run_output(requester, result['dataset_id'], row['run_id'])
            return result
        if method == 'runner_fail_retention':
            jobs.finish_artifact_retention(instance, body['request_id'], dataset_id=None, reason=body['reason'])
            return {'failed': True}
        artifacts = _decode_files(body['artifacts']) if 'artifacts' in body else None
        finished = jobs.finish(instance, body['run_id'], body['status'], body['exit_code'], body['reason'], artifacts,
                               body.get('staged_artifacts'))
        from .web_submission_flow import SubmissionFlow
        flow = SubmissionFlow(self.store, self.policy.code_root / 'library')
        flow.advance_snapshot(finished['submission_id'])
        # Bind teacher review access as soon as the verified formal version
        # exists, including when its owner's browser has already closed.
        with self.store._connection() as connection:
            preparation = connection.execute('SELECT * FROM web_submission_preparations WHERE snapshot_id=?',
                                             (finished['submission_id'],)).fetchone()
        if preparation is not None and preparation['final_submission_id']:
            owner = Principal(preparation['owner_uid'], preparation['owner_username'],
                              'teacher' if preparation['owner_username'] == 'ly' else 'student')
            source = self.store.get_submission(preparation['final_submission_id'], owner)
            self._derived().bind_submission(owner, source['submission_id'],
                source['manifest']['data_references']['references'], self._target_principal('ly'))
        return finished

    def _data_management(self):
        from .web_data_management import DataManagement
        return DataManagement(self.store, self.policy)

    def _cleanup(self):
        from .web_cleanup import Cleanup
        return Cleanup(self.store, self.policy.code_root / 'library', self.policy)

    def _project_purge(self):
        from .web_project_purge import ProjectPurge
        return ProjectPurge(self.store, self.policy.code_root / 'library', self.policy)

    def _publisher_dispatch(self, principal, method, params):
        if principal.uid != 0 or self.policy.dataset_management_config is None:
            raise BrokerError('数据发布服务未配置或身份不正确')
        fields = {'publisher_start': {'instance_id'}, 'publisher_heartbeat': set(),
                  'publisher_request_scan': set(), 'publisher_claim': set(),
                  'publisher_finish': {'job_id', 'lease_token', 'result', 'error'},
                  'publisher_readme': {'job_id', 'lease_token', 'candidate_id', 'provenance_ref', 'original'},
                  'publisher_cleanup_claim': set(), 'publisher_cleanup_finish': {'item_id','lease_token','result','error'}}
        if not isinstance(method, str) or method not in fields:
            raise BrokerError('发布账号只允许数据发现、任务领取和状态回传')
        required = ({'item_id','lease_token'} if method == 'publisher_cleanup_finish' else
                    {'job_id', 'lease_token'} if method == 'publisher_finish' else fields[method])
        body = _expect_keys(params, fields[method], required, method)
        if method == 'publisher_cleanup_claim':
            return self._cleanup().claim_raw()
        if method == 'publisher_cleanup_finish':
            return self._cleanup().finish_raw(**body)
        manager = self._data_management()
        if method == 'publisher_readme':
            from .web_readme_cache import put
            return put(manager, **body)
        if method == 'publisher_start':
            started = manager.worker_start(body['instance_id'])
            self._cleanup().publisher_restart()
            return started
        if method == 'publisher_heartbeat':
            return manager.worker_heartbeat()
        if method == 'publisher_request_scan':
            return manager.worker_request_scan()
        if method == 'publisher_claim':
            return manager.worker_claim()
        return manager.worker_finish(body['job_id'], body['lease_token'], body.get('result'), body.get('error', ''))

    def _data_management_request(self, principal, method, params):
        if principal.role != 'teacher' or principal.username != 'ly':
            raise BrokerError('只有教师可以管理原始数据集')
        fields = {'data_management_status': set(), 'data_management_scan': set(),
                  'data_management_list': {'status', 'query', 'offset', 'limit', 'include_inactive', 'tags', 'tag_mode'},
                  'data_management_get': {'candidate_id'},
                  'data_management_access': {'candidate_id', 'revision', 'access_token', 'readers'},
                  'data_management_review': {'candidate_id', 'revision', 'decision', 'metadata', 'readers'}}
        required = {'candidate_id', 'revision', 'decision'} if method == 'data_management_review' else (
            fields[method] if method == 'data_management_access' else {'candidate_id'} if method == 'data_management_get' else set())
        body = _expect_keys(params, fields[method], required, method)
        manager = self._data_management()
        if method == 'data_management_status':
            return manager.status(principal)
        if method == 'data_management_scan':
            return manager.request_scan(principal)
        if method == 'data_management_list':
            return manager.list_candidates(principal, **body)
        if method == 'data_management_get':
            return manager.get_candidate(principal, body['candidate_id'])
        if method == 'data_management_access':
            candidate_id = body.pop('candidate_id')
            return manager.change_access(principal, candidate_id, **body)
        candidate_id = body.pop('candidate_id')
        return manager.review(principal, candidate_id, **body)

    def _dispatch(self, principal: Principal, request: object) -> dict[str, object] | list[dict[str, object]]:
        envelope = _expect_keys(request, {'method', 'params'}, {'method', 'params'}, '请求')
        method = envelope['method']
        params = envelope['params']
        if principal.role == 'data-publisher':
            return self._publisher_dispatch(principal, method, envelope['params'])
        if principal.role == 'runner':
            raise BrokerError('网页执行服务已停用，请在个人 IDE 中运行')
        from .web_execution_policy import retired_method
        retired_method(method)
        from .web_feedback_rpc import METHODS as FEEDBACK_METHODS, request as feedback_request
        if isinstance(method, str) and method in FEEDBACK_METHODS:
            return feedback_request(self.store, principal, method, params)
        if method == 'list_project_jobs':
            from .web_personal_resources import project_jobs
            body = _expect_keys(params, {'project_id'}, {'project_id'}, method)
            return project_jobs(self.store, principal, body['project_id'])
        if method == 'relate_dataset_version':
            from .web_personal_resources import relate_dataset
            return relate_dataset(self.store, principal, params, self._dataset_catalog(principal))
        from .web_task_rpc import METHODS as TASK_METHODS, request as task_request
        if isinstance(method, str) and method in TASK_METHODS:
            result = task_request(self,principal,method,params)
            if method == 'task_materials' and isinstance(result,dict):
                for item in result.get('materials',[]):
                    if 'files' in item:item['files'] = _encode_files(item['files'])
            return result
        from .web_workflow_rpc import METHODS as WORKFLOW_METHODS, request as workflow_request
        if isinstance(method, str) and method in WORKFLOW_METHODS:
            body = dict(_expect_object(params, method))
            if 'files' in body:
                body['files'] = _decode_files(body['files'])
            def bind(submission_id):
                source = self.store.get_submission(submission_id, principal)
                self._derived().bind_submission(principal, submission_id,
                    source['manifest']['data_references']['references'], self._target_principal('ly'))
            result = workflow_request(self.store, self.policy.code_root / 'library', principal, method, body,
                authorize=lambda refs: self._authorized_source_references(principal, refs), bind=bind)
            if method == 'review_submission':
                result['files'] = _encode_files(result['files'])
            return result
        if isinstance(method, str) and method in {
                'data_management_status', 'data_management_scan', 'data_management_list',
                'data_management_get', 'data_management_review', 'data_management_access'}:
            return self._data_management_request(principal, method, envelope['params'])
        if isinstance(method, str) and method in {
                'project_purge_list', 'project_purge_preview', 'project_purge_execute',
                'project_purge_audit', 'project_purge_update_audit'}:
            if principal.role != 'teacher' or principal.username != 'ly':
                raise BrokerError('只有教师管理员可以执行平台项目清理')
            service = self._project_purge()
            if method == 'project_purge_list':
                _expect_keys(envelope['params'], set(), set(), method)
                return service.list_projects(principal)
            if method == 'project_purge_audit':
                body = _expect_keys(envelope['params'], {'limit'}, set(), method)
                return service.list_audit(principal, **body)
            if method == 'project_purge_update_audit':
                body = _expect_keys(envelope['params'], {'audit_id', 'deleted_extra', 'failed_extra'}, {'audit_id'}, method)
                return service.update_audit(principal, **body)
            if method == 'project_purge_preview':
                body = _expect_keys(
                    envelope['params'],
                    {'project_id', 'selected_keys', 'reset_task', 'workspace_info'},
                    {'project_id'},
                    method,
                )
                return service.preview(principal, **body)
            body = _expect_keys(
                envelope['params'],
                {'project_id', 'plan_token', 'confirmation', 'selected_keys', 'reset_task', 'workspace_info'},
                {'project_id', 'plan_token', 'confirmation'},
                method,
            )
            return service.execute(principal, **body)
        if isinstance(method, str) and method in {
                'list_asset_bin', 'preview_asset_cleanup', 'execute_asset_cleanup',
                'get_asset_cleanup', 'retry_asset_cleanup', 'check_asset_cleanup', 'direct_asset_cleanup'}:
            if principal.role != 'teacher' or principal.username != 'ly':
                raise BrokerError('只有教师可以管理回收站和永久清理')
            service = self._cleanup()
            if method == 'check_asset_cleanup':
                body = _expect_keys(envelope['params'], {'kind', 'ref'}, {'kind', 'ref'}, method)
                return service.check_cleanup_references(principal, body['kind'], body['ref'])
            if method == 'direct_asset_cleanup':
                body = _expect_keys(envelope['params'], {'kind', 'ref', 'confirmation', 'force_student_projects'},
                                    {'kind', 'ref', 'confirmation'}, method)
                force = body.get('force_student_projects', False)
                if type(force) is not bool:
                    raise BrokerError('force_student_projects 必须是布尔值')
                return service.direct_cleanup(principal, body['kind'], body['ref'], body['confirmation'],
                                              force_student_projects=force)
            fields = {'list_asset_bin': {'state','kind','q','offset','limit'},
                      'preview_asset_cleanup': {'items'},
                      'execute_asset_cleanup': {'items','plan_token','confirmation'},
                      'get_asset_cleanup': {'request_id'}, 'retry_asset_cleanup': {'request_id'}}
            body = _expect_keys(envelope['params'],fields[method],set() if method=='list_asset_bin' else fields[method],method)
            operation={'list_asset_bin':service.list_bin,'preview_asset_cleanup':service.preview,
                       'execute_asset_cleanup':service.execute,'get_asset_cleanup':service.status,
                       'retry_asset_cleanup':service.retry}[method]
            return operation(principal,**body)
        if isinstance(method, str) and method in {'get_asset_lifecycle', 'set_asset_lifecycle', 'record_project_usage'}:
            from .web_lifecycle import AssetLifecycle
            lifecycle = AssetLifecycle(self.store, self.policy.code_root / 'library', self.policy)
            if method == 'record_project_usage':
                body = _expect_keys(envelope['params'], {'project_id', 'usage'}, {'project_id', 'usage'}, method)
                return lifecycle.record_usage(principal, body['project_id'], body['usage'])
            if method == 'get_asset_lifecycle':
                body = _expect_keys(envelope['params'], {'kind', 'ref', 'offset', 'limit'}, {'kind', 'ref'}, method)
                return lifecycle.report(principal, **body)
            body = _expect_keys(envelope['params'], {'kind', 'ref', 'action', 'expected_revision'},
                                {'kind', 'ref', 'action', 'expected_revision'}, method)
            return lifecycle.change(principal, **body)
        if isinstance(method, str) and method in ASSET_METHODS:
            result = asset_request(self.store, self.policy.code_root / 'library', principal, method, envelope['params'])
            if isinstance(result, dict) and 'files' in result:
                result['files'] = _encode_files(result['files'])
            return result
        if isinstance(method, str) and method in ASSET_VALIDATION_METHODS:
            from .web_jobs import Jobs
            jobs = Jobs(self.store)
            worker_status = jobs.worker_status()
            ready = (self.policy.runner_uid is not None and worker_status['online']
                     and worker_status['execution_uid'] == self.policy.runner_uid)
            return asset_validation_request(self.store, self.policy.code_root / 'library', principal, method,
                                            envelope['params'], ready=ready,
                                            dataset_registry=self.policy.dataset_registry,
                                            reference_authorizer=lambda refs: self._authorized_source_references(principal, refs))
        if method == 'get_dataset_catalog':
            _expect_keys(envelope['params'], set(), set(), method)
            return self._dataset_catalog(principal)
        if method == 'get_dataset_readme':
            body = _expect_keys(envelope['params'], {'dataset_id'}, {'dataset_id'}, method)
            if not isinstance(body['dataset_id'], str):
                raise BrokerError('数据集 ID 无效')
            item = next((row for row in self._dataset_catalog(principal) if row['dataset_id'] == body['dataset_id']), None)
            if item is None or not str(item.get('provenance_ref', '')).startswith('README:'):
                raise BrokerError('数据集未获授权或没有 README 说明')
            from .web_readme_cache import get
            return get(self.store, item['provenance_ref'], item.get('description', ''))
        if method == 'get_dataset_files':
            return self._dataset_files(principal, envelope['params'])
        if isinstance(method, str) and method in DERIVED_METHODS:
            params = envelope['params']
            derived = self._derived()
            if method == 'get_derived_catalog':
                body = _expect_keys(params, {'q', 'visibility', 'limit', 'cursor'}, set(), method)
                return derived.catalog_page(principal, q=body.get('q', ''),
                                            visibility=body.get('visibility', 'available'),
                                            limit=body.get('limit', 50), cursor=body.get('cursor'))
            if method == 'begin_derived_upload':
                body = _expect_keys(params, {'name', 'expected_size', 'expected_sha256', 'source_refs',
                                             'description', 'tags', 'readme_markdown', 'parent_dataset_id'},
                                    {'name', 'expected_size'}, method)
                return derived.begin_upload(principal, body['name'], body['expected_size'],
                                            body.get('expected_sha256'),
                                            self._authorized_source_references(principal, body.get('source_refs')),
                                            description=body.get('description', ''),
                                            tags=body.get('tags'),
                                            readme_markdown=body.get('readme_markdown', ''),
                                            parent_dataset_id=body.get('parent_dataset_id'))
            if method == 'append_derived_upload':
                body = _expect_keys(params, {'upload_id', 'offset', 'data'},
                                    {'upload_id', 'offset', 'data'}, method)
                if not isinstance(body['data'], str):
                    raise BrokerError('派生数据上传块必须是 base64 字符串')
                try:
                    data = base64.b64decode(body['data'].encode('ascii'), validate=True)
                except (UnicodeEncodeError, ValueError) as exc:
                    raise BrokerError('派生数据上传块不是有效 base64') from exc
                return derived.append_upload(principal, body['upload_id'], body['offset'], data)
            if method == 'commit_derived_upload':
                body = _expect_keys(params, {'upload_id', 'sha256'}, {'upload_id', 'sha256'}, method)
                return derived.commit_upload(principal, body['upload_id'], body['sha256'])
            if method == 'abort_derived_upload':
                body = _expect_keys(params, {'upload_id'}, {'upload_id'}, method)
                return derived.abort_upload(principal, body['upload_id'])
            if method == 'grant_derived_review':
                body = _expect_keys(params, {'dataset_id', 'submission_id'}, {'dataset_id', 'submission_id'}, method)
                return derived.grant_review(principal, body['dataset_id'], body['submission_id'])
            body = _expect_keys(params, {'dataset_id', 'description', 'tags', 'readme_markdown', 'parent_dataset_id'},
                                {'dataset_id'}, method)
            return derived.publish(principal, body.pop('dataset_id'), **body)
        if isinstance(method, str) and method in {'execution_status', 'request_job', 'list_jobs', 'get_job', 'cancel_job', 'job_artifacts',
                                                   'save_run_artifacts_as_derived'}:
            from .web_jobs import Jobs
            from .web_runner import LIMITS
            jobs = Jobs(self.store)
            params = envelope['params']
            worker_status = jobs.worker_status()
            ready = (self.policy.runner_uid is not None and worker_status['online']
                     and worker_status['execution_uid'] == self.policy.runner_uid)
            if method == 'execution_status':
                _expect_keys(params, set(), set(), method)
                return {'enabled': ready, 'limits': LIMITS,
                        'reason': '' if ready else '执行服务未安装或隔离自检尚未就绪，请联系管理员'}
            if method == 'request_job':
                body = _expect_keys(params, {'submission_id', 'operation'}, {'submission_id', 'operation'}, method)
                if not ready:
                    raise BrokerError('执行服务未就绪，未创建运行任务')
                submission = self.store.get_submission(body['submission_id'], principal)
                references = submission['manifest']['data_references']['references']
                with self.store._connection() as connection:
                    project = connection.execute('SELECT owner_uid, owner_username FROM projects WHERE id = ?',
                                                 (submission['project_id'],)).fetchone()
                if project is None:
                    raise BrokerError('提交所属项目不存在')
                if (project['owner_uid'], project['owner_username']) != (principal.uid, principal.username):
                    raise BrokerError('只能在个人工作区运行自己的固定提交')
                owner = Principal(project['owner_uid'], project['owner_username'],
                                  'teacher' if project['owner_username'] == 'ly' else 'student')
                self._authorize_job_references(owner, principal, body['submission_id'], references)
                return jobs.request(principal, body['submission_id'], body['operation'])
            if method == 'list_jobs':
                body = _expect_keys(params, {'submission_id'}, set(), method)
                return jobs.list(principal, body.get('submission_id'), personal=True)
            if method == 'save_run_artifacts_as_derived':
                body = _expect_keys(params, {'run_id', 'paths', 'name_prefix'}, {'run_id', 'paths'}, method)
                if not self._derived().enabled:
                    raise BrokerError('管理员尚未启用派生数据存储')
                items = jobs.request_artifact_retention(principal, body['run_id'], body['paths'], body.get('name_prefix'))
                if principal.role == 'teacher':
                    for item in items:
                        dataset_id = item.get('dataset_id')
                        if item.get('status') == 'succeeded' and isinstance(dataset_id, str):
                            self._derived().grant_run_output(principal, dataset_id, body['run_id'])
                return {'items': items}
            body = _expect_keys(params, {'run_id'}, {'run_id'}, method)
            visible = jobs.get(principal, body['run_id'])
            if (visible['owner_uid'],visible['owner_username']) != (principal.uid,principal.username):
                raise BrokerError('个人执行记录只可操作自己的项目，请到教师提交与入库查看证据')
            if method == 'job_artifacts':
                return {'files': _encode_files(jobs.artifacts(principal, body['run_id']))}
            return jobs.get(principal, body['run_id']) if method == 'get_job' else jobs.cancel(principal, body['run_id'])
        if method == 'get_assignment_baseline':
            body = _expect_keys(params, {'request_id','fingerprint'}, {'request_id','fingerprint'}, method)
            return self.store.assignment_baseline(principal,body['request_id'],body['fingerprint'])
        if method == 'get_storage_capacity':
            _expect_keys(params, set(), set(), method)
            if principal.role != 'teacher':
                raise BrokerError('只有教师可以查看平台存储统计')
            from .web_content import capacity
            return capacity(self.store)
        if method in {'validate_assignment_targets', 'create_assignment_distributions'}:
            allowed = {'target_usernames','data_references'} if method == 'validate_assignment_targets' else {'submission_id','target_usernames','request_id'}
            required = {'target_usernames','data_references'} if method == 'validate_assignment_targets' else {'submission_id','target_usernames'}
            body = _expect_keys(params, allowed, required, method)
            if principal.role != 'teacher':
                raise BrokerError('只有教师可以布置任务')
            if method == 'create_assignment_distributions':
                source = self.store.get_submission(body['submission_id'], principal)
                refs = source['manifest']['data_references']['references']
            else:
                refs = body['data_references']
            targets, refs = self._assignment_targets(principal, body['target_usernames'], refs)
            if method == 'validate_assignment_targets':
                return {'targets':[{'uid': target.uid,'username':target.username} for target in targets],
                        'data_references':refs}
            return {'distributions':self.store.create_distributions(principal, body['submission_id'], targets)}
        if method == 'get_derived_readme':
            body = _expect_keys(envelope['params'], {'dataset_id'}, {'dataset_id'}, method)
            return self._derived().get_readme(principal, body['dataset_id'])
        if method == 'set_dataset_access':
            body = _expect_keys(envelope['params'], {'dataset_id', 'public', 'targets'}, {'dataset_id', 'public', 'targets'}, method)
            if principal.role != 'teacher':
                raise BrokerError('只有教师可以修改派生数据集权限')
            if not isinstance(body['targets'], list) or len(body['targets']) > 100:
                raise BrokerError('授权账号必须是有限列表')
            from .web_sharing import ComponentService
            targets = [self._target_principal(name) for name in body['targets']]
            return ComponentService(self.store, self.policy.code_root / 'library').set_dataset_access(
                principal, body['dataset_id'], body['public'], targets)
        if method == 'get_dataset_access_map':
            _expect_keys(envelope['params'], {'include_inactive'}, set(), method)
            if principal.role != 'teacher':
                raise BrokerError('只有教师可以查看派生数据集权限')
            from .web_sharing import ComponentService
            return ComponentService(self.store, self.policy.code_root / 'library').dataset_access_map(principal)
        if method == 'set_asset_metadata':
            body = _expect_keys(envelope['params'], {'kind', 'ref', 'description', 'tags', 'parent_ref', 'display_name'},
                                {'kind', 'ref'}, method)
            if principal.role != 'teacher':
                raise BrokerError('只有教师可以编辑资产标签与说明')
            from .web_assets import AssetService
            return AssetService(self.store, self.policy.code_root / 'library').set_metadata(
                principal, body.pop('kind'), body.pop('ref'), **body)
        if method == 'check_dataset_existence':
            body = _expect_keys(envelope['params'], {'dataset_ids'}, {'dataset_ids'}, method)
            dids = body['dataset_ids']
            if type(dids) is not list or len(dids) > 200 or any(not isinstance(d, str) or not d.strip() for d in dids):
                raise BrokerError('dataset_ids 必须是有限字符串列表')
            from .web_datasets import DatasetRegistry
            from .web_dataset_trees import TreeRegistry
            from .web_lifecycle import states
            raw_ids = {r['dataset_id']: r.get('name', r['dataset_id']) for r in DatasetRegistry(self.policy.dataset_registry).catalog(principal)}
            tree_ids = {r['dataset_id']: r.get('name', r['dataset_id']) for r in TreeRegistry(self.policy.dataset_tree_registry).catalog(principal)}
            purged = states(self.store, 'dataset')
            out = {}
            with self.store._connection() as con:
                has_derived = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_derived_datasets'").fetchone() is not None
                for did in dids:
                    if purged.get(did) == 'purged':
                        out[did] = {'exists': False, 'name': did}
                        continue
                    if did in raw_ids:
                        out[did] = {'exists': True, 'name': raw_ids[did]}
                    elif did in tree_ids:
                        out[did] = {'exists': True, 'name': tree_ids[did]}
                    elif has_derived:
                        drow = con.execute('SELECT name FROM web_derived_datasets WHERE dataset_id=?', (did,)).fetchone()
                        out[did] = {'exists': drow is not None, 'name': drow['name'] if drow is not None else did}
                    else:
                        out[did] = {'exists': False, 'name': did}
            return out
        if not isinstance(method, str) or method not in {
                'create_project', 'get_project', 'submit', 'submit_assignment_baseline', 'get_submission', 'list_submissions',
                'list_submission_projects', 'list_project_versions',
                'create_distribution', 'list_distributions', 'claim_distribution',
                'confirm_distribution', 'get_distribution_baseline', 'export_package', 'import_package',
                'get_package_baseline', 'get_component_catalog', 'get_shared_component', 'get_shared_contract',
                'get_shared_template', 'get_component_access', 'set_component_access', 'validate_draft_contracts'}:
            raise BrokerError('请求方法不支持')
        params = envelope['params']
        if method == 'export_package':
            body = _expect_keys(params, {'submission_id'}, {'submission_id'}, 'export_package 参数')
            result = self.store.export_package(body['submission_id'], principal)
            result['files'] = _encode_files(result['files'])
            return result
        if method == 'import_package':
            body = _expect_keys(params, {'name', 'files', 'bundle'}, {'name', 'files', 'bundle'}, 'import_package 参数')
            return self.store.import_package(principal, body['name'], _decode_files(body['files']), body['bundle'])
        if method == 'get_package_baseline':
            body = _expect_keys(params, {'project_id'}, {'project_id'}, 'get_package_baseline 参数')
            result = self.store.get_package_baseline(body['project_id'], principal)
            result['files'] = _encode_files(result['files'])
            return result
        if method in {'get_component_catalog', 'get_shared_component', 'get_shared_contract',
                      'get_shared_template', 'get_component_access', 'set_component_access', 'validate_draft_contracts'}:
            from .web_sharing import ComponentService
            service = ComponentService(self.store, self.policy.code_root / 'library')
            if method == 'validate_draft_contracts':
                body = _expect_keys(params, {'contracts'}, {'contracts'}, method + ' 参数')
                return service.validate_drafts(principal, body['contracts'])
            if method in {'get_component_catalog', 'get_component_access'}:
                body = _expect_keys(params, {'include_inactive'} if method == 'get_component_access' else set(), set(), method + ' 参数')
                return service.catalog(principal) if method == 'get_component_catalog' else service.access(principal, **body)
            if method == 'set_component_access':
                body = _expect_keys(params, {'kind', 'ref', 'public', 'targets', 'expected_revision'},
                                    {'kind', 'ref', 'public', 'targets', 'expected_revision'}, method + ' 参数')
                if principal.role != 'teacher':
                    raise BrokerError('只有教师可以修改共享权限')
                if not isinstance(body['targets'], list) or len(body['targets']) > 100:
                    raise BrokerError('授权账号必须是有限列表')
                targets = [self._target_principal(name) for name in body['targets']]
                return service.set_access(principal, body['kind'], body['ref'], body['public'], targets,
                                          body['expected_revision'])
            body = _expect_keys(params, {'ref'}, {'ref'}, method + ' 参数')
            operation = {'get_shared_component': service.component, 'get_shared_contract': service.contract,
                         'get_shared_template': service.template}[method]
            result = operation(principal, body['ref'])
            result['files'] = _encode_files(result['files'])
            return result
        if method == 'create_project':
            body = _expect_keys(params, {'name'}, set(), 'create_project 参数')
            if 'name' in body and not isinstance(body['name'], str):
                raise BrokerError('name 必须是字符串')
            return self.store.create_project(principal, body.get('name', ''))
        if method == 'get_project':
            body = _expect_keys(params, {'project_id'}, {'project_id'}, 'get_project 参数')
            if not isinstance(body['project_id'], str):
                raise BrokerError('project_id 必须是字符串')
            return self.store.get_project(body['project_id'], principal)
        if method in {'submit', 'submit_assignment_baseline'}:
            if method == 'submit':
                raise BrokerError('请先预览版本与所选产物，再确认保存固定提交')
            fields = {'project_id', 'kind', 'files', 'data_references'}
            if method == 'submit_assignment_baseline':
                fields |= {'request_id', 'fingerprint'}
                if principal.role != 'teacher':
                    raise BrokerError('只有教师可以生成任务固定基线')
            body = _expect_keys(params, fields, fields, method + ' 参数')
            if not isinstance(body['project_id'], str) or not isinstance(body['kind'], str) or not isinstance(body['data_references'], list):
                raise BrokerError('submit 参数类型无效')
            derived = self._derived()
            derived.validate_submission_references(principal, body['data_references'])
            result = self.store.submit(body['project_id'], principal, body['kind'],
                                       _decode_files(body['files']), body['data_references'],
                                       **({'assignment_request_id':body['request_id'],
                                           'assignment_fingerprint':body['fingerprint']}
                                          if method == 'submit_assignment_baseline' else {}))
            derived.bind_submission(principal, result['submission_id'], body['data_references'],
                                    self._target_principal('ly'))
            return result
        if method == 'get_submission':
            body = _expect_keys(params, {'submission_id'}, {'submission_id'}, 'get_submission 参数')
            if not isinstance(body['submission_id'], str):
                raise BrokerError('submission_id 必须是字符串')
            result = self.store.get_submission(body['submission_id'], principal)
            if (result['project']['owner_uid'], result['project']['owner_username']) != (principal.uid, principal.username):
                raise BrokerError('个人固定提交只可查看自己的项目，请到教师提交与入库查看其他账号')
            result['files'] = _encode_files(result['files'])
            return result
        if method in {'list_submission_projects', 'list_project_versions'}:
            allowed = {'q', 'owner', 'kind', 'from_date', 'to_date', 'limit', 'cursor'}
            required = set()
            if method == 'list_project_versions':
                allowed.add('project_id')
                required.add('project_id')
            body = _expect_keys(params, allowed, required, method + ' 参数')
            for key in {'q', 'owner', 'kind', 'from_date', 'to_date', 'cursor'} & set(body):
                if body[key] is not None and not isinstance(body[key], str):
                    raise BrokerError(f'{key} 必须是字符串')
            if 'limit' in body and (isinstance(body['limit'], bool) or not isinstance(body['limit'], int)):
                raise BrokerError('limit 必须是整数')
            if method == 'list_project_versions':
                if not isinstance(body['project_id'], str):
                    raise BrokerError('project_id 必须是字符串')
                return self.store.list_project_versions(principal, body.pop('project_id'), personal=True, **body)
            return self.store.list_submission_projects(principal, personal=True, **body)
        if method == 'create_distribution':
            body = _expect_keys(params, {'submission_id', 'target_username'},
                                {'submission_id', 'target_username'}, 'create_distribution 参数')
            if not isinstance(body['submission_id'], str) or not isinstance(body['target_username'], str):
                raise BrokerError('create_distribution 参数类型无效')
            return self.store.create_distribution(principal, body['submission_id'],
                                                  self._target_principal(body['target_username']))
        if method == 'list_distributions':
            _expect_keys(params, set(), set(), 'list_distributions 参数')
            return self.store.list_distributions(principal, personal=True)
        if method in {'claim_distribution', 'confirm_distribution', 'get_distribution_baseline'}:
            body = _expect_keys(params, {'distribution_id'}, {'distribution_id'}, f'{method} 参数')
            if not isinstance(body['distribution_id'], str):
                raise BrokerError(f'{method} 的 distribution_id 必须是字符串')
            if method == 'claim_distribution':
                from .web_task_delivery import TaskDelivery
                TaskDelivery(self.store).check_distribution_claimable(principal, body['distribution_id'])
                result = self.store.claim_distribution(body['distribution_id'], principal)
                result['files'] = _encode_files(result['files'])
                return result
            if method == 'confirm_distribution':
                return self.store.confirm_distribution(body['distribution_id'], principal)
            result = self.store.get_distribution_baseline(body['distribution_id'], principal)
            result['files'] = _encode_files(result['files'])
            return result
        body = _expect_keys(params, {'project_id', 'include_minor'}, set(), 'list_submissions 参数')
        if 'project_id' in body and body['project_id'] is not None and not isinstance(body['project_id'], str):
            raise BrokerError('project_id 必须是字符串')
        if 'include_minor' in body and not isinstance(body['include_minor'], bool):
            raise BrokerError('include_minor 必须是布尔值')
        return self.store.list_submissions(principal, body.get('project_id'), include_minor=body.get('include_minor', True), personal=True)


class BrokerClient:
    """Small same-host client that verifies both socket path and peer UID."""

    def __init__(self, socket_path: Path | str, expected_broker_uid: int):
        if isinstance(expected_broker_uid, bool) or not isinstance(expected_broker_uid, int) or expected_broker_uid < 1:
            raise BrokerError('expected_broker_uid 无效')
        self.socket_path = _socket_path(socket_path)
        self.expected_broker_uid = expected_broker_uid

    def request(self, method: str, **params: object) -> object:
        if not isinstance(method, str) or not method:
            raise BrokerError('method 必须是非空字符串')
        if _FORBIDDEN_KEYS & set(params):
            raise BrokerError('客户端不得传递身份或工作区字段')
        payload_params = dict(params)
        if method in {'submit', 'submit_assignment_baseline', 'import_package', 'preview_submission_versions', 'prepare_submission', 'prepare_project_trial'} and 'files' in payload_params:
            payload_params["files"] = _encode_files(payload_params["files"])
        if method in {'append_derived_upload', 'runner_append_retention'} and 'data' in payload_params:
            data = payload_params['data']
            if not isinstance(data, bytes):
                raise BrokerError('派生数据上传块必须是 bytes')
            payload_params['data'] = base64.b64encode(data).decode('ascii')
        if method == 'runner_finish' and 'artifacts' in payload_params:
            payload_params['artifacts'] = _encode_files(payload_params['artifacts'])
        request_payload = _frame_payload({'method': method, 'params': payload_params})
        _trusted_socket_route(self.socket_path, self.expected_broker_uid, require_socket=True)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        timeout = LONG_OPERATION_TIMEOUT if method in {'review_publish_batch', 'commit_derived_upload', 'runner_commit_retention',
            'preview_asset_cleanup','execute_asset_cleanup','get_asset_cleanup','retry_asset_cleanup','publisher_cleanup_claim'} else SOCKET_TIMEOUT
        connection.settimeout(timeout)
        try:
            connection.connect(str(self.socket_path))
            self._verify_peer(connection)
            connection.sendall(struct.pack('!I', len(request_payload)) + request_payload)
            response = _recv_frame(connection, timeout=timeout)
        except (OSError, BrokerError) as exc:
            raise BrokerError(_safe_message(exc)) from exc
        finally:
            connection.close()
        payload = _expect_keys(response, {'ok', 'result', 'error'}, {'ok'}, '响应')
        if not isinstance(payload['ok'], bool):
            raise BrokerError('响应状态无效')
        if payload['ok']:
            if set(payload) != {'ok', 'result'}:
                raise BrokerError('响应字段无效')
            result = payload['result']
            if method in ASSET_METHODS and isinstance(result, dict) and 'files' in result:
                result['files'] = _decode_files(result['files'])
            if method == 'runner_claim' and result is not None:
                result = _expect_object(result, 'runner_claim 响应')
                result['files'] = _decode_files(result.get('files'))
            if method == 'task_materials' and isinstance(result,dict):
                for item in result.get('materials',[]):
                    if 'files' in item:item['files'] = _decode_files(item['files'])
            if method in {'get_submission', 'review_submission', 'claim_distribution', 'get_distribution_baseline', 'export_package',
                          'get_package_baseline', 'get_shared_component', 'get_shared_contract', 'get_shared_template', 'job_artifacts'}:
                result = _expect_object(result, f'{method} 响应')
                result['files'] = _decode_files(result.get('files'))
            return result
        if set(payload) != {'ok', 'error'} or not isinstance(payload['error'], str):
            raise BrokerError('错误响应无效')
        raise BrokerError(payload['error'])

    def open_dataset_file(self, dataset_id: str, path: str, expected_reference: object) -> tuple[int, dict[str, object]]:
        """Obtain one broker-authorized read-only FD for local IDE code."""
        if not isinstance(dataset_id, str) or not isinstance(path, str):
            raise BrokerError('数据集 ID 和路径必须是字符串')
        from .web_store import _validate_data_references
        expected = _validate_data_references([expected_reference])
        if len(expected) != 1 or expected[0]['dataset_id'] != dataset_id:
            raise BrokerError('数据文件期望引用无效')
        request_payload = _frame_payload({'method': 'open_dataset_file',
                                          'params': {'dataset_id': dataset_id, 'path': path,
                                                     'expected_reference': expected[0]}})
        if len(request_payload) > 64 * 1024:
            raise BrokerError('数据文件请求超过 64 KiB 上限')
        _trusted_socket_route(self.socket_path, self.expected_broker_uid, require_socket=True)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(LONG_OPERATION_TIMEOUT)
        descriptor = -1
        try:
            connection.connect(str(self.socket_path))
            self._verify_peer(connection)
            connection.sendall(struct.pack('!I', len(request_payload)) + request_payload)
            response, received_descriptor = _recv_frame_fd(connection, timeout=LONG_OPERATION_TIMEOUT)
            if received_descriptor is not None:
                descriptor = received_descriptor
            payload = _expect_keys(response, {'ok', 'result', 'error'}, {'ok'}, '响应')
            if not payload['ok']:
                if set(payload) != {'ok', 'error'} or not isinstance(payload['error'], str):
                    raise BrokerError('错误响应无效')
                raise BrokerError(payload['error'])
            if received_descriptor is None:
                raise BrokerError('数据文件响应缺少描述符')
            if set(payload) != {'ok', 'result'}:
                raise BrokerError('响应字段无效')
            result = _expect_object(payload['result'], 'open_dataset_file 响应')
            reference = result.get('reference')
            from .web_store import _validate_data_references
            refs = _validate_data_references([reference])
            if len(refs) != 1 or refs[0]['dataset_id'] != dataset_id or refs[0]['path'] != path:
                raise BrokerError('数据文件响应引用不匹配')
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != refs[0]['size']:
                raise BrokerError('数据文件描述符无效')
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            if flags & os.O_ACCMODE != os.O_RDONLY:
                raise BrokerError('数据文件描述符不是只读')
            result['reference'] = refs[0]
            return descriptor, result
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        finally:
            connection.close()

    def _verify_peer(self, connection: socket.socket) -> None:
        if not hasattr(socket, 'SO_PEERCRED'):
            raise BrokerError('SO_PEERCRED 在此平台不可用')
        size = struct.calcsize('3i')
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        except OSError as exc:
            raise BrokerError('无法读取 broker peer credentials') from exc
        if len(raw) != size:
            raise BrokerError('broker peer credentials 长度无效')
        _pid, uid, _gid = struct.unpack('3i', raw)
        if uid != self.expected_broker_uid:
            raise BrokerError('连接的 peer 不是预期 broker UID')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ResearchKit local submission broker')
    parser.add_argument('--policy', required=True, help='root-protected Web policy JSON')
    arguments = parser.parse_args(argv)
    try:
        policy = load_policy(arguments.policy, production=True)
        validate_policy(policy, validate_workspaces=False)
        BrokerServer(policy).serve_forever()
    except (BrokerError, IdentityError, PolicyError, KitError) as exc:
        parser.error(_safe_message(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
