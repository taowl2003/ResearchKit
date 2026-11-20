"""Trusted local identity and deployment checks for the future Web service.

This module deliberately has no HTTP concepts.  A request body, cookie, or
project file is never an identity source: a workspace service obtains its
identity from its real effective UID, and the submission broker obtains it
from ``SO_PEERCRED`` on an AF_UNIX socket.
"""
from __future__ import annotations

import json
import os
import platform
import pwd
import re
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


POLICY_SCHEMA = "researchkit.web-policy/v1"
TEACHER_USERNAME = "ly"
DEFAULT_BROKER_SOCKET = Path("/run/researchkit-web/broker.sock")
# Linux account names are ultimately verified through pwd.  This only excludes
# unsafe separators and control characters; existing sites may use names such
# as Dengyao.
_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


class IdentityError(RuntimeError):
    """The operating-system identity is not allowed by the service policy."""


class PolicyError(ValueError):
    """A Web service policy is malformed or unsuitable for production."""


class WorkspaceError(PolicyError):
    """A configured workspace does not have a safe owner or path boundary."""


@dataclass(frozen=True, slots=True)
class Principal:
    """An immutable, server-derived Web identity."""

    uid: int
    username: str
    role: str


@dataclass(frozen=True, slots=True)
class Policy:
    """The small, explicit allow-list used by local Web processes."""

    principals: Mapping[int, Principal]
    workspaces: Mapping[int, Path]
    broker_uid: int
    broker_username: str
    submission_store: Path
    code_root: Path
    source: Path | None = None
    access_mode: str = "explicit"
    min_uid: int = 1000
    broker_socket: Path = DEFAULT_BROKER_SOCKET
    runner_uid: int | None = None
    runner_username: str | None = None
    dataset_registry: Path | None = None
    dataset_metadata: Path | None = None
    dataset_tree_registry: Path | None = None
    derived_dataset_root: Path | None = None
    derived_max_file_bytes: int = 32 * 1024**3
    derived_user_quota_bytes: int = 64 * 1024**3
    derived_upload_ttl_seconds: int = 3600
    dataset_management_config: Path | None = None

    def principal(self, uid: int) -> Principal:
        if self.access_mode == "ssh-users":
            return _ssh_user_principal(self, uid)
        try:
            return self.principals[uid]
        except KeyError as exc:
            raise IdentityError(f"UID {uid} is not permitted by the Web policy") from exc

    def workspace(self, principal: Principal) -> Path:
        if self.access_mode == "ssh-users":
            return Path(pwd.getpwuid(principal.uid).pw_dir) / "researchkit-workspace"
        try:
            return self.workspaces[principal.uid]
        except KeyError as exc:
            raise WorkspaceError(f"no workspace is configured for {principal.username}") from exc


def _require_uid(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(f"{field} must be a non-root positive integer UID")
    return value


def _require_min_uid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError("min_uid must be a positive integer")
    return value


def _require_bounded_int(value: object, field: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PolicyError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _require_username(value: object, field: str) -> str:
    if not isinstance(value, str) or not _USERNAME.fullmatch(value):
        raise PolicyError(f"{field} must be a Linux username")
    return value


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise PolicyError(f"{field} must be an absolute path without '..'")
    return path


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{field} must be an object")
    return value


def policy_from_data(data: object, *, source: Path | None = None) -> Policy:
    """Parse a policy without consulting the host account database.

    Call :func:`validate_policy` during deployment and the identity entry
    points at process start.  Keeping parsing separate makes diagnostics useful
    on a machine where the service accounts have not been provisioned yet.
    """
    root = _mapping(data, "policy")
    if root.get("schema") != POLICY_SCHEMA:
        raise PolicyError(f"schema must be {POLICY_SCHEMA!r}")
    access_mode = root.get("access_mode", "explicit")
    if access_mode not in ("explicit", "ssh-users"):
        raise PolicyError("access_mode must be 'explicit' or 'ssh-users'")
    min_uid = _require_min_uid(root.get("min_uid", 1000))
    records = root.get("principals", [])
    if not isinstance(records, list):
        raise PolicyError("principals must be a list")
    if access_mode == "explicit" and not records:
        raise PolicyError("explicit access_mode requires a non-empty principals list")
    if access_mode == "ssh-users" and records:
        raise PolicyError("ssh-users access_mode must not mix in explicit principals")
    if access_mode == "ssh-users" and root.get("teacher", TEACHER_USERNAME) != TEACHER_USERNAME:
        raise PolicyError(f"the teacher account must be {TEACHER_USERNAME!r}")

    principals: dict[int, Principal] = {}
    workspaces: dict[int, Path] = {}
    names: set[str] = set()
    teacher_count = 0
    for index, record in enumerate(records):
        item = _mapping(record, f"principals[{index}]")
        uid = _require_uid(item.get("uid"), f"principals[{index}].uid")
        username = _require_username(item.get("username"), f"principals[{index}].username")
        role = item.get("role")
        if role not in ("teacher", "student"):
            raise PolicyError(f"principals[{index}].role must be 'teacher' or 'student'")
        if uid in principals or username in names:
            raise PolicyError("principal UID and username entries must be unique")
        if role == "teacher":
            teacher_count += 1
            if username != TEACHER_USERNAME:
                raise PolicyError(f"the teacher account must be {TEACHER_USERNAME!r}")
        elif username == TEACHER_USERNAME:
            raise PolicyError(f"{TEACHER_USERNAME!r} must have the teacher role")
        principals[uid] = Principal(uid=uid, username=username, role=role)
        workspaces[uid] = _absolute_path(item.get("workspace"), f"principals[{index}].workspace")
        names.add(username)
    if access_mode == "explicit" and teacher_count != 1:
        raise PolicyError("policy must contain exactly one teacher")

    broker = _mapping(root.get("broker"), "broker")
    broker_uid = _require_uid(broker.get("uid"), "broker.uid")
    broker_username = _require_username(broker.get("username"), "broker.username")
    if broker_uid in principals or broker_username in names:
        raise PolicyError("broker must use a dedicated service UID and username")
    store = _absolute_path(root.get("submission_store"), "submission_store")
    code_root = _absolute_path(root.get("code_root"), "code_root")
    broker_socket = _absolute_path(root.get("broker_socket", str(DEFAULT_BROKER_SOCKET)), "broker_socket")
    runner_uid, runner_username = None, None
    if 'runner' in root:
        runner = _mapping(root['runner'], 'runner')
        if set(runner) != {'uid', 'username'}:
            raise PolicyError('runner requires only uid and username')
        runner_uid = _require_uid(runner['uid'], 'runner.uid')
        runner_username = _require_username(runner['username'], 'runner.username')
        if (runner_uid >= 1000 or runner_uid == broker_uid or runner_uid in principals
                or runner_username in names or runner_username in {broker_username, TEACHER_USERNAME}):
            raise PolicyError('runner must use a separate non-login system account')
    return Policy(
        principals=MappingProxyType(principals),
        workspaces=MappingProxyType(workspaces),
        broker_uid=broker_uid,
        broker_username=broker_username,
        submission_store=store,
        code_root=code_root,
        source=source,
        access_mode=access_mode,
        min_uid=min_uid,
        broker_socket=broker_socket,
        runner_uid=runner_uid,
        runner_username=runner_username,
        dataset_registry=(_absolute_path(root['dataset_registry'], 'dataset_registry')
                          if 'dataset_registry' in root else None),
        dataset_metadata=(_absolute_path(root['dataset_metadata'], 'dataset_metadata')
                          if 'dataset_metadata' in root else None),
        dataset_tree_registry=(_absolute_path(root['dataset_tree_registry'], 'dataset_tree_registry')
                               if 'dataset_tree_registry' in root else None),
        derived_dataset_root=(_absolute_path(root['derived_dataset_root'], 'derived_dataset_root')
                              if 'derived_dataset_root' in root else None),
        derived_max_file_bytes=_require_bounded_int(
            root.get('derived_max_file_bytes'), 'derived_max_file_bytes',
            default=32 * 1024**3, minimum=1, maximum=32 * 1024**3),
        derived_user_quota_bytes=_require_bounded_int(
            root.get('derived_user_quota_bytes'), 'derived_user_quota_bytes',
            default=64 * 1024**3, minimum=1, maximum=1024**5),
        derived_upload_ttl_seconds=_require_bounded_int(
            root.get('derived_upload_ttl_seconds'), 'derived_upload_ttl_seconds',
            default=3600, minimum=60, maximum=24 * 3600),
        dataset_management_config=(_absolute_path(root['dataset_management_config'], 'dataset_management_config')
                                   if 'dataset_management_config' in root else None),
    )


def _path_components(path: Path) -> list[tuple[Path, os.stat_result]]:
    """Return lstat records while refusing absent and symlinked components."""
    if not path.is_absolute() or ".." in path.parts:
        raise PolicyError(f"unsafe path: {path}")
    current = Path(path.anchor)
    result: list[tuple[Path, os.stat_result]] = []
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise PolicyError(f"path component does not exist: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PolicyError(f"symbolic links are not permitted: {current}")
        result.append((current, info))
    return result


def validate_production_policy_file(path: str | Path) -> Path:
    """Require a root-owned regular policy below non-root-writable ancestors."""
    policy_path = Path(path).absolute()
    policy_path = _absolute_path(str(policy_path), "policy")
    components = _path_components(policy_path)
    if not components or not stat.S_ISREG(components[-1][1].st_mode):
        raise PolicyError("production policy must be a regular file")
    for component, info in components:
        if info.st_uid != 0:
            raise PolicyError(f"production policy path must be owned by root: {component}")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f"production policy has a non-root-writable path: {component}")
    return policy_path


def load_policy(path: str | Path, *, production: bool = True) -> Policy:
    """Load JSON policy, optionally enforcing production file protection."""
    # Command-line callers commonly provide a relative filename.  Make it
    # absolute without resolving it, so a symlink is still detected below.
    policy_path = _absolute_path(str(Path(path).absolute()), "policy")
    if production:
        validate_production_policy_file(policy_path)
    else:
        components = _path_components(policy_path)
        if not components or not stat.S_ISREG(components[-1][1].st_mode):
            raise PolicyError("policy must be a regular file")
    try:
        with policy_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy is not valid JSON: {exc.msg}") from exc
    except UnicodeDecodeError as exc:
        raise PolicyError("policy is not valid UTF-8 JSON") from exc
    except OSError as exc:
        raise PolicyError(f"policy cannot be read: {exc}") from exc
    return policy_from_data(data, source=policy_path)


def _ssh_user_principal(policy: Policy, uid: int) -> Principal:
    """Derive a principal from a real SSH-capable local account."""
    if uid == 0 or uid < policy.min_uid or uid == policy.broker_uid:
        raise IdentityError(f"UID {uid} is not permitted by the Web policy")
    try:
        record = pwd.getpwuid(uid)
    except KeyError as exc:
        raise IdentityError(f"UID {uid} is not permitted by the Web policy") from exc
    shell_name = Path(record.pw_shell or "").name.lower()
    if shell_name in {"false", "nologin"} or not record.pw_shell:
        raise IdentityError(f"UID {uid} does not have an interactive login shell")
    if record.pw_name == policy.broker_username:
        raise IdentityError("broker account is never a Web workspace principal")
    try:
        username = _require_username(record.pw_name, "pwd username")
    except PolicyError as exc:
        raise IdentityError(str(exc)) from exc
    return Principal(uid, username, "teacher" if username == TEACHER_USERNAME else "student")


def _system_principal(principal: Principal) -> None:
    try:
        record = pwd.getpwuid(principal.uid)
    except KeyError as exc:
        raise IdentityError(f"configured UID {principal.uid} does not exist") from exc
    if record.pw_name != principal.username:
        raise IdentityError(
            f"configured UID {principal.uid} resolves to {record.pw_name!r}, "
            f"not {principal.username!r}"
        )


def validate_workspace(principal: Principal, workspace: str | Path) -> Path:
    """Validate the sole workspace the service may expose for ``principal``."""
    _system_principal(principal)
    path = _absolute_path(str(workspace), f"workspace for {principal.username}")
    home = _absolute_path(pwd.getpwuid(principal.uid).pw_dir, f"home for {principal.username}")
    if path == home:
        raise WorkspaceError(f"workspace for {principal.username} cannot be its home directory")
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise WorkspaceError(f"workspace for {principal.username} escapes its home directory") from exc
    components = _path_components(path)
    if not components:
        raise WorkspaceError("workspace cannot be filesystem root")
    for component, item in components:
        if item.st_uid not in (0, principal.uid):
            raise WorkspaceError(f"workspace path has an unexpected owner: {component}")
        if item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise WorkspaceError(f"workspace path is group or world writable: {component}")
    info = components[-1][1]
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(f"workspace is not a directory: {path}")
    if info.st_uid != principal.uid:
        raise WorkspaceError(f"workspace is not owned by {principal.username}: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise WorkspaceError(f"workspace must have mode 0700: {path}")
    return path


def validate_policy(policy: Policy, *, validate_workspaces: bool = True) -> None:
    """Verify configured identities against ``pwd`` and their workspace bounds."""
    broker = Principal(policy.broker_uid, policy.broker_username, "broker")
    _system_principal(broker)
    if policy.runner_uid is not None:
        account = pwd.getpwuid(policy.runner_uid)
        if (account.pw_name != policy.runner_username or account.pw_uid >= 1000
                or account.pw_uid in {0, policy.broker_uid}
                or Path(account.pw_shell).name not in {'nologin', 'false'}):
            raise IdentityError('runner must resolve to its separate non-login system account')
    if policy.access_mode == "ssh-users":
        try:
            teacher_record = pwd.getpwnam(TEACHER_USERNAME)
        except KeyError as exc:
            raise IdentityError(f"teacher account {TEACHER_USERNAME!r} does not exist") from exc
        principals = (_ssh_user_principal(policy, teacher_record.pw_uid),)
    else:
        principals = policy.principals.values()
    for principal in principals:
        _system_principal(principal)
        if validate_workspaces:
            validate_workspace(principal, policy.workspace(principal))
    _validate_derived_dataset_root(policy)


def _principal_for_uid(policy: Policy, uid: int) -> Principal:
    if uid == 0:
        raise IdentityError("root is never a Web workspace principal")
    principal = policy.principal(uid)
    _system_principal(principal)
    return principal


def current_principal(policy: Policy) -> Principal:
    """Authenticate a per-user workspace process from real and effective UID."""
    real_uid = os.getuid()
    effective_uid = os.geteuid()
    if real_uid == 0 or effective_uid == 0:
        raise IdentityError("root must not run a Web workspace service")
    if real_uid != effective_uid:
        raise IdentityError("workspace service UID and effective UID must match")
    principal = _principal_for_uid(policy, real_uid)
    validate_workspace(principal, policy.workspace(principal))
    return principal


def peer_principal(sock: socket.socket, policy: Policy) -> Principal:
    """Authenticate a broker client through Linux ``SO_PEERCRED`` only."""
    if sock.family != socket.AF_UNIX:
        raise IdentityError("broker identity requires an AF_UNIX socket")
    if not hasattr(socket, "SO_PEERCRED"):
        raise IdentityError("SO_PEERCRED is unavailable on this platform")
    size = struct.calcsize("3i")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    except OSError as exc:
        raise IdentityError("could not obtain UNIX peer credentials") from exc
    if len(raw) != size:
        raise IdentityError("UNIX peer credentials have an unexpected size")
    _pid, uid, _gid = struct.unpack("3i", raw)
    return _principal_for_uid(policy, uid)


def _check(checks: list[dict[str, str]], name: str, action) -> None:
    try:
        detail = action()
    except (IdentityError, PolicyError, OSError) as exc:
        checks.append({"name": name, "status": "not-ready", "detail": str(exc)})
    else:
        checks.append({"name": name, "status": "ready", "detail": str(detail)})


def _validate_submission_store(policy: Policy) -> Path:
    components = _path_components(policy.submission_store)
    if not components:
        raise PolicyError("submission_store cannot be filesystem root")
    for component, item in components:
        if item.st_uid not in (0, policy.broker_uid):
            raise PolicyError(f"submission_store path has an unexpected owner: {component}")
        if item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f"submission_store path is group or world writable: {component}")
    info = components[-1][1]
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyError("submission_store must be an existing directory")
    if info.st_uid != policy.broker_uid:
        raise PolicyError("submission_store must be owned by the dedicated broker UID")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise PolicyError("submission_store must have mode 0700")
    return policy.submission_store


def _validate_derived_dataset_root(policy: Policy) -> Path | None:
    """Check the optional broker:runner setgid storage root."""
    path = policy.derived_dataset_root
    if path is None:
        return None
    components = _path_components(path)
    if not components:
        raise PolicyError('derived_dataset_root cannot be filesystem root')
    for component, item in components[:-1]:
        if item.st_uid != 0 or item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f'derived_dataset_root ancestor is not root-trusted: {component}')
    if policy.runner_uid is None:
        raise PolicyError('derived_dataset_root requires a configured runner service account')
    info = components[-1][1]
    runner_gid = pwd.getpwuid(policy.runner_uid).pw_gid
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != policy.broker_uid or info.st_gid != runner_gid or
            stat.S_IMODE(info.st_mode) != 0o2710):
        raise PolicyError('derived_dataset_root must be broker:runner mode 02710')
    return path


def _validate_broker_socket_parent(policy: Policy) -> Path:
    """Validate the service-owned runtime directory for the public UDS."""
    parent = policy.broker_socket.parent
    components = _path_components(parent)
    if not components:
        raise PolicyError("broker socket parent cannot be filesystem root")
    for component, item in components:
        if item.st_uid not in (0, policy.broker_uid):
            raise PolicyError(f"broker socket parent has an unexpected owner: {component}")
        if item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f"broker socket parent is group or world writable: {component}")
    info = components[-1][1]
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != policy.broker_uid:
        raise PolicyError("broker socket parent must be a broker-owned directory")
    if stat.S_IMODE(info.st_mode) != 0o755:
        raise PolicyError("broker socket parent must have mode 0755")
    return parent


def _validate_trusted_code_root(policy: Policy) -> Path:
    components = _path_components(policy.code_root)
    if not components:
        raise PolicyError("code_root cannot be filesystem root")
    info = components[-1][1]
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyError("code_root must be an existing directory")
    for component, item in components:
        if item.st_uid != 0 or item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f"code_root is not a root-owned trusted path: {component}")
    return policy.code_root


def _validate_broker_interpreter() -> Path:
    """Check the interpreter that would run a broker, not a user workspace."""
    executable = Path(sys.executable).absolute()
    components = _path_components(executable)
    if not components or not stat.S_ISREG(components[-1][1].st_mode):
        raise PolicyError("broker Python executable must be a regular file")
    for component, item in components:
        if item.st_uid != 0 or item.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError(f"broker Python executable is not on a root-owned trusted path: {component}")
    return executable


def _path_permissions(path: Path) -> dict[str, object]:
    """Describe an existing path with lstat, without following a symlink."""
    result: dict[str, object] = {"path": str(path)}
    try:
        info = path.lstat()
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result.update({
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": format(stat.S_IMODE(info.st_mode), "04o"),
        "kind": "symlink" if stat.S_ISLNK(info.st_mode) else
                "directory" if stat.S_ISDIR(info.st_mode) else
                "regular-file" if stat.S_ISREG(info.st_mode) else "other",
    })
    return result


def preflight_report(policy_path: str | Path | None) -> dict[str, object]:
    """Return a JSON-serializable, read-only deployment readiness report."""
    report: dict[str, object] = {
        "schema": "researchkit.web-preflight/v1",
        "scope": "filesystem-and-identity-only",
        "unverified": [
            "trusted-code-content",
            "runtime-dependencies",
            "ssh-client-paths",
            "real-multiuser-isolation",
            "runner",
        ],
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "system": {"platform": platform.platform(), "os_name": os.name},
        "process": {"uid": os.getuid(), "euid": os.geteuid(), "username": pwd.getpwuid(os.getuid()).pw_name},
        "checks": [],
        "paths": {},
    }
    checks: list[dict[str, str]] = report["checks"]  # type: ignore[assignment]
    if policy_path is None:
        checks.append({"name": "policy", "status": "not-ready", "detail": "--policy is required for production preflight"})
        report["ready"] = False
        return report

    policy: Policy | None = None
    report["paths"] = {"policy": _path_permissions(Path(policy_path).absolute())}
    _check(checks, "policy_protection", lambda: validate_production_policy_file(policy_path))
    try:
        policy = load_policy(policy_path, production=False)
    except (PolicyError, OSError) as exc:
        checks.append({"name": "policy_contents", "status": "not-ready", "detail": str(exc)})
    else:
        checks.append({"name": "policy_contents", "status": "ready", "detail": str(policy.source)})
        report["paths"] = {
            "policy": _path_permissions(policy.source or Path(policy_path).absolute()),
            "submission_store": _path_permissions(policy.submission_store),
            "code_root": _path_permissions(policy.code_root),
            "python_executable": _path_permissions(Path(sys.executable).absolute()),
            "broker_socket": _path_permissions(policy.broker_socket),
            "broker_socket_parent": _path_permissions(policy.broker_socket.parent),
            "derived_dataset_root": (_path_permissions(policy.derived_dataset_root)
                                     if policy.derived_dataset_root is not None else None),
            "workspaces": {
                principal.username: _path_permissions(policy.workspace(principal))
                for principal in policy.principals.values()
            },
        }
        broker = Principal(policy.broker_uid, policy.broker_username, "broker")
        _check(checks, "broker_identity", lambda: (_system_principal(broker), "dedicated broker identity validates")[1])
        principals = policy.principals.values()
        if policy.access_mode == "ssh-users":
            def dynamic_teacher() -> Principal:
                try:
                    return policy.principal(pwd.getpwnam(TEACHER_USERNAME).pw_uid)
                except KeyError as exc:
                    raise IdentityError(f"teacher account {TEACHER_USERNAME!r} does not exist") from exc
            teacher: Principal | None = None
            _check(checks, "teacher_identity", lambda: (dynamic_teacher(), "teacher UID and pwd username match")[1])
            try:
                teacher = dynamic_teacher()
            except IdentityError:
                principals = ()
            else:
                principals = (teacher,)
        for principal in principals:
            _check(
                checks,
                f"principal_identity:{principal.username}",
                lambda principal=principal: (_system_principal(principal), "UID and pwd username match")[1],
            )
            _check(
                checks,
                f"workspace:{principal.username}",
                lambda principal=principal: validate_workspace(principal, policy.workspace(principal)),
            )
        _check(checks, "submission_store", lambda: _validate_submission_store(policy))
        if policy.derived_dataset_root is not None:
            _check(checks, "derived_dataset_root", lambda: _validate_derived_dataset_root(policy))
        _check(checks, "broker_socket_parent", lambda: _validate_broker_socket_parent(policy))
        _check(
            checks,
            "trusted_code_root",
            lambda: f"{_validate_trusted_code_root(policy)} (filesystem path only; code contents unverified)",
        )
        _check(checks, "broker_python_interpreter", _validate_broker_interpreter)
    report["ready"] = all(check["status"] == "ready" for check in checks)
    return report
