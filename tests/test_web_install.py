from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "researchkit_install_web", Path(__file__).parents[1] / "tools" / "install-web.py"
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_choose_python_accepts_only_trusted_311_or_312(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "python"
    candidate.write_text("", encoding="utf-8")
    monkeypatch.setattr(installer, "python_version", lambda path: (3, 12, 4))
    monkeypatch.setattr(installer, "trusted_root_path", lambda path: True)
    assert installer.choose_python(candidate) == (candidate, (3, 12, 4))
    monkeypatch.setattr(installer, "python_version", lambda path: (3, 13, 0))
    assert installer.choose_python(candidate) == (None, None)


def test_dry_run_never_reaches_root_mutation(monkeypatch):
    args = argparse.Namespace(check=False, dry_run=True, python=None, install_system_python=True)
    monkeypatch.setattr(installer, "report", lambda args: ({"ready": True}, Path("/usr/bin/python3.12")))
    monkeypatch.setattr(installer.os, "geteuid", lambda: pytest.fail("dry-run must not check or use root"))
    assert installer.install(args) == 0


def test_source_validation_rejects_symlink_and_hardlink(monkeypatch, tmp_path: Path):
    core = tmp_path / "core" / "researchkit"
    core.mkdir(parents=True)
    regular = core / "regular.py"
    regular.write_text("x", encoding="utf-8")
    link = core / "link.py"
    link.symlink_to(regular)
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(installer, "WHEELS", tmp_path / "wheels")
    monkeypatch.setattr(installer, "LOCK", tmp_path / "deploy" / "requirements-web.lock")
    assert any("link.py" in error for error in installer.source_tree_errors())


def test_service_account_must_be_dedicated_nologin_system_account():
    valid = SimpleNamespace(pw_uid=999, pw_gid=77, pw_shell="/usr/sbin/nologin")
    original = installer.grp.getgrgid
    installer.grp.getgrgid = lambda gid: SimpleNamespace(gr_name="researchkit-web")
    try:
        installer.validate_service_account(valid)
        with pytest.raises(RuntimeError, match="system UID"):
            installer.validate_service_account(SimpleNamespace(pw_uid=1000, pw_gid=77, pw_shell="/usr/sbin/nologin"))
        with pytest.raises(RuntimeError, match="nologin"):
            installer.validate_service_account(SimpleNamespace(pw_uid=999, pw_gid=77, pw_shell="/bin/bash"))
    finally:
        installer.grp.getgrgid = original


def test_choose_python_never_probes_untrusted_candidate(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "python"
    candidate.write_text("", encoding="utf-8")
    monkeypatch.setattr(installer, "trusted_root_path", lambda path: False)
    monkeypatch.setattr(installer, "python_version", lambda path: pytest.fail("must not execute untrusted candidate"))
    assert installer.choose_python(candidate) == (None, None)


def test_wait_for_socket_accepts_delayed_mode_0666_socket(monkeypatch):
    calls = {"count": 0}
    class FakePath:
        def __init__(self, value): pass
        def lstat(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FileNotFoundError()
            return SimpleNamespace(st_mode=installer.stat.S_IFSOCK | 0o666, st_uid=777)
    monkeypatch.setattr(installer, "Path", FakePath)
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: None)
    installer.wait_for_socket("ignored", 777, timeout=15)
    assert calls["count"] == 2


def test_wait_for_socket_has_bounded_timeout(monkeypatch):
    clock = iter((0.0, 0.0, 15.1))
    class FakePath:
        def __init__(self, value): pass
        def lstat(self): raise FileNotFoundError()
    monkeypatch.setattr(installer, "Path", FakePath)
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="within 15.0"):
        installer.wait_for_socket("ignored", 1, timeout=15)


def test_failed_activation_stops_before_cleanup_and_retains_data(monkeypatch, tmp_path):
    app, store, etc = (tmp_path / name for name in ('app', 'store', 'etc'))
    for path in (app, store, etc):
        path.mkdir()
    (app / 'code.py').write_text('code')
    data = store / 'submissions.sqlite3'
    data.write_bytes(b'keep every submitted version')
    policy = etc / 'policy.json'
    policy.write_text('{}')
    unit = tmp_path / 'broker.service'
    unit.write_text('service')
    monkeypatch.setattr(installer, 'INSTALL', app)
    monkeypatch.setattr(installer, 'STORE', store)
    monkeypatch.setattr(installer, 'UNIT', unit)
    commands = []
    def run(command):
        if not commands:
            assert app.exists() and unit.exists() and data.exists()
        commands.append(command)
    monkeypatch.setattr(installer, 'run', run)
    assert installer.cleanup_failed_install([app, store, etc, policy, unit], True)
    assert commands[0] == ['systemctl', 'disable', '--now', 'researchkit-broker.service']
    assert commands[1] == ['systemctl', 'daemon-reload']
    assert data.read_bytes() == b'keep every submitted version'
    assert not app.exists() and not policy.exists() and not unit.exists()


def test_failed_stop_keeps_all_installation_paths(monkeypatch, tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    unit = tmp_path / 'broker.service'
    unit.write_text('service')
    monkeypatch.setattr(installer, 'INSTALL', app)
    monkeypatch.setattr(installer, 'UNIT', unit)
    def cannot_stop(command):
        raise RuntimeError('systemd unavailable')
    monkeypatch.setattr(installer, 'run', cannot_stop)
    assert not installer.cleanup_failed_install([app, unit], True)
    assert app.exists() and unit.exists()


def test_invalid_source_never_installs_system_packages(monkeypatch):
    args = argparse.Namespace(check=False, dry_run=False, python=None, install_system_python=True)
    monkeypatch.setattr(installer, 'report', lambda args: (
        {'missing_source': ['web_app.py'], 'source_security_errors': []}, None))
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer, 'run', lambda command: pytest.fail('unsafe source must not trigger system changes'))
    assert installer.install(args) == 1
