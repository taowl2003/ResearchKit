from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import sys
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchkit import web_identity as identity


def _data(uid: int, workspace: Path, *, username: str = "ly") -> dict:
    return {
        "schema": identity.POLICY_SCHEMA,
        "principals": [{"uid": uid, "username": username, "role": "teacher", "workspace": str(workspace)}],
        "broker": {"uid": uid + 10000, "username": "researchkit-web"},
        "submission_store": "/var/lib/researchkit-web/submissions",
        "code_root": "/opt/researchkit",
    }


def _record(uid: int, mode: int, kind: int = stat.S_IFDIR) -> SimpleNamespace:
    return SimpleNamespace(st_uid=uid, st_mode=kind | mode)


def test_optional_runner_requires_distinct_nonroot_system_identity(tmp_path):
    data = _data(1011, tmp_path)
    data['broker'] = {'uid': 981, 'username': 'researchkit-web'}
    assert identity.policy_from_data(data).runner_uid is None
    for uid, username in [(0, 'researchkit-runner'), (981, 'researchkit-runner'),
                          (1011, 'researchkit-runner'), (980, 'ly'), (980, 'researchkit-web')]:
        data['runner'] = {'uid': uid, 'username': username}
        with pytest.raises(identity.PolicyError):
            identity.policy_from_data(data)
    data['runner'] = {'uid': 980, 'username': 'researchkit-runner'}
    assert identity.policy_from_data(data).runner_uid == 980


def test_optional_dataset_registry_requires_absolute_safe_path(tmp_path):
    data = _data(1011, tmp_path)
    assert identity.policy_from_data(data).dataset_registry is None
    for path in ('relative.json', '/etc/../datasets.json', None):
        data['dataset_registry'] = path
        with pytest.raises(identity.PolicyError):
            identity.policy_from_data(data)
    data['dataset_registry'] = '/etc/researchkit-web/datasets.json'
    assert identity.policy_from_data(data).dataset_registry == Path(data['dataset_registry'])


def _safe_workspace_components(path: Path, uid: int) -> list[tuple[Path, SimpleNamespace]]:
    return [
        (Path("/home"), _record(0, 0o755)),
        (Path("/home/ly"), _record(uid, 0o700)),
        (path, _record(uid, 0o700)),
    ]


def _policy_for_current_user(workspace: Path) -> tuple[identity.Policy, identity.Principal | None]:
    """Keep the real-UID tests portable while policy retains ly as teacher."""
    uid = os.getuid()
    if uid == 0:
        return identity.policy_from_data(_data(1011, Path("/home/ly/workspace"))), None
    username = pwd.getpwuid(uid).pw_name
    if username == "ly":
        return identity.policy_from_data(_data(uid, workspace)), identity.Principal(uid, "ly", "teacher")
    teacher_uid = uid + 10000
    data = _data(teacher_uid, Path("/home/ly/workspace"))
    data["principals"].append({
        "uid": uid, "username": username, "role": "student", "workspace": str(workspace),
    })
    return identity.policy_from_data(data), identity.Principal(uid, username, "student")


def test_policy_requires_ly_teacher_dedicated_broker_and_permits_existing_case_names(tmp_path: Path):
    data = _data(1011, tmp_path)
    data["principals"][0]["role"] = "student"
    with pytest.raises(identity.PolicyError, match="teacher"):
        identity.policy_from_data(data)
    data = _data(1011, tmp_path)
    data["broker"] = {"uid": 1011, "username": "researchkit-web"}
    with pytest.raises(identity.PolicyError, match="dedicated"):
        identity.policy_from_data(data)
    data = _data(1011, tmp_path)
    data["principals"].append({
        "uid": 1012, "username": "Dengyao", "role": "student", "workspace": "/home/Dengyao/workspace",
    })
    policy = identity.policy_from_data(data)
    assert policy.principal(1012).username == "Dengyao"
    with pytest.raises(TypeError):
        policy.principals[1012] = identity.Principal(1012, "Dengyao", "student")  # type: ignore[index]


def test_current_principal_rejects_root_mismatch_and_unlisted_uid(tmp_path: Path, monkeypatch):
    policy = identity.policy_from_data(_data(1011, tmp_path))
    uid = 1011
    monkeypatch.setattr(identity.os, "getuid", lambda: uid)
    monkeypatch.setattr(identity.os, "geteuid", lambda: uid + 1)
    with pytest.raises(identity.IdentityError, match="must match"):
        identity.current_principal(policy)
    monkeypatch.setattr(identity.os, "getuid", lambda: 0)
    monkeypatch.setattr(identity.os, "geteuid", lambda: 0)
    with pytest.raises(identity.IdentityError, match="root"):
        identity.current_principal(policy)
    monkeypatch.setattr(identity.os, "getuid", lambda: uid + 9999)
    monkeypatch.setattr(identity.os, "geteuid", lambda: uid + 9999)
    with pytest.raises(identity.IdentityError, match="not permitted"):
        identity.current_principal(policy)


def test_current_principal_uses_real_uid_and_matching_pwd(monkeypatch):
    uid = os.getuid()
    workspace = Path("/home/ly/researchkit-web-workspace")
    policy, expected = _policy_for_current_user(workspace)
    if uid == 0:
        with pytest.raises(identity.IdentityError, match="root"):
            identity.current_principal(policy)
        return
    monkeypatch.setattr(identity, "_path_components", lambda path: _safe_workspace_components(path, uid))
    assert identity.current_principal(policy) == expected
    monkeypatch.setattr(
        identity.pwd, "getpwuid",
        lambda value: SimpleNamespace(pw_name=f"not-{expected.username}", pw_dir="/home/ly"),
    )
    with pytest.raises(identity.IdentityError, match="resolves"):
        identity.current_principal(policy)


def test_peer_principal_uses_real_socket_credentials_without_pwd_mocking():
    uid = os.getuid()
    policy, expected = _policy_for_current_user(Path("/home/ly/researchkit-web-workspace"))
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if uid == 0:
            with pytest.raises(identity.IdentityError, match="root"):
                identity.peer_principal(left, policy)
            return
        principal = identity.peer_principal(left, policy)
    finally:
        left.close()
        right.close()
    assert principal == expected


def test_peer_principal_rejects_non_unix_socket(tmp_path: Path):
    policy = identity.policy_from_data(_data(1011, tmp_path))
    fake_socket = SimpleNamespace(family=socket.AF_INET)
    with pytest.raises(identity.IdentityError, match="AF_UNIX"):
        identity.peer_principal(fake_socket, policy)


def test_workspace_rejects_home_escape_and_bad_ancestors(monkeypatch):
    uid = os.getuid()
    principal = identity.Principal(uid, "ly", "teacher")
    workspace = Path("/home/ly/workspace")
    monkeypatch.setattr(identity.pwd, "getpwuid", lambda value: SimpleNamespace(pw_name="ly", pw_dir="/home/ly"))
    monkeypatch.setattr(identity, "_path_components", lambda path: _safe_workspace_components(path, uid))
    assert identity.validate_workspace(principal, workspace) == workspace
    with pytest.raises(identity.WorkspaceError, match="home directory"):
        identity.validate_workspace(principal, Path("/home/ly"))
    with pytest.raises(identity.WorkspaceError, match="escapes"):
        identity.validate_workspace(principal, Path("/srv/workspace"))
    monkeypatch.setattr(identity, "_path_components", lambda path: [
        (Path("/tmp"), _record(0, 0o1777)), (path, _record(uid, 0o700)),
    ])
    with pytest.raises(identity.WorkspaceError, match="world writable"):
        identity.validate_workspace(principal, workspace)


def test_production_policy_rejects_nonroot_owner_or_writable_ancestor(monkeypatch):
    policy_file = Path("/etc/researchkit-web/policy.json")
    monkeypatch.setattr(identity, "_path_components", lambda path: [
        (Path("/etc"), _record(0, 0o755)),
        (Path("/etc/researchkit-web"), _record(1011, 0o700)),
        (policy_file, _record(0, 0o600, stat.S_IFREG)),
    ])
    with pytest.raises(identity.PolicyError, match="owned by root"):
        identity.validate_production_policy_file(policy_file)
    monkeypatch.setattr(identity, "_path_components", lambda path: [
        (Path("/etc"), _record(0, 0o775)),
        (policy_file, _record(0, 0o600, stat.S_IFREG)),
    ])
    with pytest.raises(identity.PolicyError, match="writable"):
        identity.validate_production_policy_file(policy_file)


def test_store_rejects_root_and_unsafe_ancestors(monkeypatch):
    policy = identity.policy_from_data(_data(1011, Path("/home/ly/workspace")))
    root_policy = identity.Policy(
        principals=policy.principals, workspaces=policy.workspaces,
        broker_uid=policy.broker_uid, broker_username=policy.broker_username,
        submission_store=Path("/"), code_root=policy.code_root,
    )
    with pytest.raises(identity.PolicyError, match="filesystem root"):
        identity._validate_submission_store(root_policy)
    monkeypatch.setattr(identity, "_path_components", lambda path: [
        (Path("/var"), _record(0, 0o755)),
        (Path("/var/lib"), _record(9999, 0o700)),
        (path, _record(policy.broker_uid, 0o700)),
    ])
    with pytest.raises(identity.PolicyError, match="unexpected owner"):
        identity._validate_submission_store(policy)

    monkeypatch.undo()
    root_code_policy = identity.Policy(
        principals=policy.principals, workspaces=policy.workspaces,
        broker_uid=policy.broker_uid, broker_username=policy.broker_username,
        submission_store=policy.submission_store, code_root=Path("/"),
    )
    with pytest.raises(identity.PolicyError, match="filesystem root"):
        identity._validate_trusted_code_root(root_code_policy)


def test_load_policy_development_mode_rejects_symlink(tmp_path: Path):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(_data(1011, tmp_path)), encoding="utf-8")
    assert identity.load_policy(policy_file, production=False).source == policy_file
    link = tmp_path / "policy-link.json"
    link.symlink_to(policy_file)
    with pytest.raises(identity.PolicyError, match="symbolic"):
        identity.load_policy(link, production=False)


def test_preflight_without_policy_is_not_ready_and_declares_scope():
    report = identity.preflight_report(None)
    assert report["ready"] is False
    assert report["checks"][0]["name"] == "policy"
    assert report["scope"] == "filesystem-and-identity-only"
    assert "real-multiuser-isolation" in report["unverified"]


def test_preflight_reports_invalid_json_without_traceback(tmp_path: Path):
    policy_file = tmp_path / "broken-policy.json"
    policy_file.write_bytes(b"\xffnot-json")
    report = identity.preflight_report(policy_file)
    assert report["ready"] is False
    assert any(check["name"] == "policy_contents" and check["status"] == "not-ready"
               for check in report["checks"])


def test_root_policy_path_is_not_ready_and_never_indexes_empty_components():
    with pytest.raises(identity.PolicyError, match="regular file"):
        identity.load_policy("/", production=False)
    report = identity.preflight_report("/")
    assert report["ready"] is False
    assert any(check["status"] == "not-ready" for check in report["checks"])


def test_ssh_users_policy_derives_principal_and_workspace_from_pwd(monkeypatch):
    data = _data(1011, Path("/home/ly/workspace"))
    data.update({"access_mode": "ssh-users", "min_uid": 1000, "teacher": "ly", "principals": []})
    data["broker"] = {"uid": 2001, "username": "researchkit-web"}
    policy = identity.policy_from_data(data)
    records = {
        1100: SimpleNamespace(pw_name="studentA", pw_shell="/bin/bash", pw_dir="/home/studentA"),
        1011: SimpleNamespace(pw_name="ly", pw_shell="/bin/bash", pw_dir="/home/ly"),
        1200: SimpleNamespace(pw_name="nologin-user", pw_shell="/usr/sbin/nologin", pw_dir="/home/no"),
    }
    monkeypatch.setattr(identity.pwd, "getpwuid", lambda uid: records[uid])
    student = policy.principal(1100)
    assert student == identity.Principal(1100, "studentA", "student")
    assert policy.workspace(student) == Path("/home/studentA/researchkit-workspace")
    assert policy.principal(1011).role == "teacher"
    with pytest.raises(identity.IdentityError, match="not permitted"):
        policy.principal(999)
    with pytest.raises(identity.IdentityError, match="interactive"):
        policy.principal(1200)


def test_ssh_users_policy_rejects_broker_and_explicit_mixing(tmp_path: Path, monkeypatch):
    data = _data(1011, tmp_path)
    data.update({"access_mode": "ssh-users", "principals": []})
    data["broker"] = {"uid": 2001, "username": "researchkit-web"}
    policy = identity.policy_from_data(data)
    with pytest.raises(identity.IdentityError, match="not permitted"):
        policy.principal(2001)
    data["principals"] = [{"uid": 1011, "username": "ly", "role": "teacher", "workspace": "/home/ly/workspace"}]
    with pytest.raises(identity.PolicyError, match="must not mix"):
        identity.policy_from_data(data)


def test_installer_dry_run_is_read_only_json():
    result = subprocess.run(
        [sys.executable, "tools/install-web.py", "--dry-run"],
        cwd=Path(__file__).parents[1], text=True, capture_output=True,
    )
    assert result.returncode in (0, 1)
    report = json.loads(result.stdout)
    assert report["schema"] == "researchkit.web-install-check/v1"
    assert "would_install_system_python" in report
