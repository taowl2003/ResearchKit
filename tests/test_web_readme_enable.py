import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


def load():
    spec = importlib.util.spec_from_file_location('readme_enable', Path('tools/enable-web-readme-data.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(tmp_path, monkeypatch):
    module = load()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, '_root_owned', lambda info: True)
    # tmp_path has an untrusted shared ancestor; production _trusted validates it.
    def trusted(path, directory=False):
        info = path.lstat()
        assert path.is_dir() if directory else path.is_file()
        return info
    monkeypatch.setattr(module, '_trusted', trusted)
    monkeypatch.setattr(module, '_raw_root', lambda path: module._path(path))
    monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=1000))
    source = tmp_path / 'source'; code = tmp_path / 'app/core/researchkit'
    (source / 'core/researchkit').mkdir(parents=True); code.mkdir(parents=True)
    for name in module.FILES:
        content = ('# ' + name).encode()
        (source / 'core/researchkit' / name).write_bytes(content)
        (code / name).write_bytes(content)
    monkeypatch.setattr(module, 'ROOT', source)
    units = tmp_path / 'units'; units.mkdir(); (units / module.UNIT).write_text('[Service]\n')
    monkeypatch.setattr(module, 'UNIT_ROOT', units)
    etc = tmp_path / 'etc'; etc.mkdir()
    raw = tmp_path / 'raw'; raw.mkdir(); (raw / 'existing').write_text('untouched')
    config = etc / 'data-management.json'
    config.write_text(json.dumps({'schema': 'researchkit.web-data-management/v1', 'raw_root': str(raw),
                                  'wiki_root': '/existing/wiki', 'scan_interval_seconds': 3600, 'seconds_per_dataset': 123}))
    policy = etc / 'policy.json'
    policy.write_text(json.dumps({'schema': 'researchkit.web-policy/v1', 'runner': {'uid': 980},
                                  'code_root': str(tmp_path / 'app'), 'dataset_management_config': str(config)}))
    calls = []
    def run(args):
        calls.append(args)
        return 'inactive' if args[:2] == ['systemctl', 'show'] else ''
    monkeypatch.setattr(module, '_run', run)
    actions = []
    helpers = SimpleNamespace(snapshot_receiving_root=lambda root: {'mode': 0o755, 'acl': b'prior-acl'},
                              prepare_receiving_root=lambda root, uid: actions.append(('prepare', root, uid)),
                              restore_receiving_root=lambda root, old: actions.append(('restore', root, old)))
    monkeypatch.setattr(module, '_permissions_module', lambda path: helpers)
    mkdtemp = module.tempfile.mkdtemp
    monkeypatch.setattr(module.tempfile, 'mkdtemp', lambda **kwargs: mkdtemp(prefix=kwargs['prefix'], dir=tmp_path))
    return module, policy, config, raw, calls, actions, helpers


def test_check_is_read_only_and_requires_stopped_services(state, monkeypatch):
    module, policy, config, raw, calls, actions, helpers = state
    before = config.read_bytes()
    assert module.inspect(policy)[0]['ready']
    assert config.read_bytes() == before and not actions
    monkeypatch.setattr(module, '_run', lambda args: 'active')
    assert not module.inspect(policy)[0]['ready']
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1000)
    assert '管理员' in module.inspect(policy)[0]['errors'][0]


def test_receiving_root_inside_a_registered_version_is_rejected(state):
    module, policy, config, raw, calls, actions, helpers = state
    manifest = raw.parent / 'existing-manifest.json'
    manifest.write_text(json.dumps({'root':str(raw.parent)}))
    registry = raw.parent / 'trees.json'
    registry.write_text(json.dumps({'datasets':[{'manifest':str(manifest)}]}))
    value=json.loads(policy.read_text());value['dataset_tree_registry']=str(registry)
    policy.write_text(json.dumps(value))
    report,_=module.inspect(policy)
    assert not report['ready'] and '已有固定数据版本' in report['errors'][0]
    assert not actions


def test_enable_preserves_config_and_is_repeatable(state):
    module, policy, config, raw, calls, actions, helpers = state
    assert module.enable(policy)['enabled']
    content = json.loads(config.read_text())
    assert content['wiki_root'] == '/existing/wiki' and content['seconds_per_dataset'] == 123
    assert content['discovery_mode'] == 'readme' and content['prepare_permissions'] is True
    assert content['scan_interval_seconds'] == 60
    target = module.UNIT_ROOT / (module.UNIT + '.d') / 'researchkit-readme.conf'
    text = target.read_text()
    assert 'BindReadOnlyPaths=\n' in text and 'BindPaths="' + str(raw) + '"' in text
    assert 'ReadWritePaths="' + str(raw) + '"' in text
    assert 'CapabilityBoundingSet=\nCapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER' in text
    assert actions == [('prepare', raw, 1000)]
    assert (raw / 'existing').read_text() == 'untouched'
    assert module.enable(policy)['enabled'] and json.loads(config.read_text()) == content
    assert not any(args[:2] == ['systemctl', 'start'] for args in calls)


def test_failure_restores_configuration_dropin_and_root_snapshot(state, monkeypatch):
    module, policy, config, raw, calls, actions, helpers = state
    directory = module.UNIT_ROOT / (module.UNIT + '.d'); directory.mkdir()
    target = directory / 'researchkit-readme.conf'; target.write_text('old config\n')
    before = config.read_bytes()
    def fail(root, uid):
        raise RuntimeError('ACL failed')
    helpers.prepare_receiving_root = fail
    with pytest.raises(RuntimeError, match='已回滚配置和权限'):
        module.enable(policy)
    assert config.read_bytes() == before and target.read_text() == 'old config\n'
    assert actions == [('restore', raw, {'mode': 0o755, 'acl': b'prior-acl'})]
    assert ['systemctl', 'daemon-reload'] in calls


def test_check_rejects_missing_or_changed_code_and_bad_paths(state):
    module, policy, config, raw, calls, actions, helpers = state
    code = Path(json.loads(policy.read_text())['code_root']) / 'core/researchkit' / module.FILES[0]
    code.write_text('old')
    assert '更新代码' in module.inspect(policy)[0]['errors'][0]
    with pytest.raises(RuntimeError, match='格式'):
        module.dropin(Path('/home/share/%h'))


def test_check_detects_import_failure_before_any_changes(state, monkeypatch):
    module, policy, config, raw, calls, actions, helpers = state
    before = config.read_bytes()
    def fail(path):
        raise TypeError("dataclass() got an unexpected keyword argument 'slots'")
    monkeypatch.setattr(module, '_permissions_module', fail)
    report, inspected = module.inspect(policy)
    assert not report['ready'] and inspected is None
    assert '权限模块无法加载' in report['errors'][0] and 'slots' in report['errors'][0]
    with pytest.raises(RuntimeError, match='尚未修改'):
        module.enable(policy)
    assert config.read_bytes() == before and not actions
    assert not list(config.parent.parent.glob('researchkit-readme-backup.*'))
    assert not any(args[:2] == ['systemctl', 'daemon-reload'] for args in calls)


def test_enable_reuses_the_module_loaded_during_preflight(state, monkeypatch):
    module, policy, config, raw, calls, actions, helpers = state
    loaded = []
    def permissions(path):
        loaded.append(path)
        assert len(loaded) == 1
        return helpers
    monkeypatch.setattr(module, '_permissions_module', permissions)
    assert module.enable(policy)['enabled']
    assert actions == [('prepare', raw, 1000)]


def test_unsafe_runtime_is_rejected_without_launching(state, monkeypatch):
    module, policy, config, raw, calls, actions, helpers = state
    def fail(path, directory=False):
        raise RuntimeError('runtime is writable')
    monkeypatch.setattr(module, '_trusted', fail)
    def unexpected(*args, **kwargs):
        pytest.fail('must not execute an untrusted interpreter')
    monkeypatch.setattr(module.subprocess, 'run', unexpected)
    assert module.main(['--policy', str(policy), '--check']) == 1
    assert not actions


def test_bootstrap_rejects_wrong_environment_without_running_tool(tmp_path):
    module = load()
    marker = tmp_path / 'should-not-exist'
    script = tmp_path / 'tool.py'
    script.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
    result = subprocess.run([sys.executable, '-I', '-B', '-c', module._RUNTIME_BOOTSTRAP,
                             str(tmp_path / 'not-the-venv'), str(script)], capture_output=True, text=True)
    assert result.returncode != 0 and 'Python 环境无效' in result.stderr
    assert not marker.exists()


def test_system_python39_launches_installed_runtime_and_imports_real_app(state):
    """Exercise the reported CLI path, including the actual dataclass imports.

    Only OS trust/service checks are replaced in a disposable script: no root,
    production policy, ACL changes or real service operations are involved.
    """
    old_python = Path('/usr/bin/python3')
    version = subprocess.run([str(old_python), '-I', '-c', 'import sys; print(sys.version_info[:2])'],
                             capture_output=True, text=True)
    if version.stdout.strip() != '(3, 9)':
        pytest.skip('regression requires system Python 3.9')
    module, policy, config, raw, calls, actions, helpers = state
    repository = Path.cwd()
    installed_code = Path(json.loads(policy.read_text())['code_root']) / 'core/researchkit'
    shutil.copytree(repository / 'core/researchkit', installed_code, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    trace = config.parent.parent / 'runtime-trace.jsonl'
    script = trace.parent / 'enable.py'
    settings = '''
APP_VENV = Path({venv!r})
ROOT = Path({root!r})
UNIT_ROOT = Path({units!r})
os.geteuid = lambda: 0
def _trusted(path, directory=False):
    info = path.lstat()
    assert path.is_dir() if directory else path.is_file()
    return info
_raw_root = lambda path: _path(path)
_run = lambda arguments: 'inactive'
with open({trace!r}, 'a') as stream:
    stream.write(json.dumps({{'version': list(sys.version_info[:2]), 'isolated': sys.flags.isolated}}) + '\\n')
'''.format(venv=sys.prefix, root=str(repository), units=str(module.UNIT_ROOT), trace=str(trace))
    source = (repository / 'tools/enable-web-readme-data.py').read_text()
    script.write_text(source.replace("\nif __name__ == '__main__':", settings + "\nif __name__ == '__main__':"))
    before = config.read_bytes()
    result = subprocess.run([str(old_python), str(script), '--policy', str(policy), '--check'],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report['ready'] and report['errors'] == []
    assert report['python'] == str(Path(sys.prefix) / 'bin/python')
    executions = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(executions) == 2 and executions[0]['version'] == [3, 9]
    assert executions[1]['version'] >= [3, 11] and executions[1]['isolated'] == 1
    assert config.read_bytes() == before and not actions
    assert not list(installed_code.rglob('__pycache__'))
    # The same check must fail before mutation if the installed app cannot load.
    (installed_code / 'web_identity.py').write_text("raise TypeError('broken app import')\n")
    result = subprocess.run([str(old_python), str(script), '--policy', str(policy), '--check'],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 1, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert not report['ready'] and 'broken app import' in report['errors'][0]
    assert config.read_bytes() == before and not list(installed_code.rglob('__pycache__'))
