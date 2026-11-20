"""Create portable workspaces and promote independently tested algorithms."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .files import KitError, atomic_text, copy_tree, identifier, read_yaml, reference, safe_path, staging, write_json, write_yaml
from .model import plugin, protected_files, project, validate_parameters


def home() -> Path:
    """Local ResearchKit store. RK_HOME makes a relocated installation explicit."""
    return Path(os.environ.get('RK_HOME', Path(__file__).resolve().parents[2])).expanduser().resolve()


def _library_plugin(ref: str) -> Path:
    name, version = reference(ref)
    return safe_path(home(), f'library/algorithms/{name}/{version}')


def _copy_contracts(source: Path, project_root: Path, metadata: dict) -> None:
    for ref in set(metadata['inputs'].values()) | set(metadata['outputs'].values()):
        name, version = reference(ref)
        candidates = (source / name / f'{version}.yaml', source / f'{name}/{version}.yaml')
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            raise KitError(f'缺少契约 {ref}；请确认插件来源中的 contracts/ 目录')
        destination = project_root / '.researchkit/contracts' / name / f'{version}.yaml'
        if destination.exists() and destination.read_bytes() != found.read_bytes():
            raise KitError(f'契约 {ref} 内容冲突')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, destination)


def install_runtime(project_root: Path) -> None:
    """Install the current package and a self locating ./task launcher."""
    project_root = Path(project_root)
    target = project_root / '.researchkit/runtime/researchkit'
    if target.exists():
        shutil.rmtree(target)
    copy_tree(Path(__file__).resolve().parent, target)
    task = project_root / 'task'
    task.write_text(
        '#!/usr/bin/env python3\n'
        '"""ResearchKit project entry point; paths are relative to this file."""\n'
        'import sys\nfrom pathlib import Path\n'
        'root = Path(__file__).resolve().parent\n'
        'sys.path.insert(0, str(root / ".researchkit" / "runtime"))\n'
        'try:\n    from researchkit.cli import main\n'
        'except ModuleNotFoundError as exc:\n'
        '    if exc.name == "yaml":\n'
        '        print("缺少 PyYAML；请在当前 Python 环境安装项目依赖后重试。", file=sys.stderr)\n'
        '        raise SystemExit(1)\n'
        '    raise\n'
        'raise SystemExit(main(default_project=root))\n', encoding='utf-8')
    task.chmod(0o755)
    doc = read_yaml(project_root / 'project.yaml')
    entry = doc.get('entry', 'case.py') if doc.get('mode') == 'python' else ' '.join(doc.get('command', []))
    aliases = ', '.join(doc.get('plugins', {})) or '无'
    guide = (
        f'# {doc.get("name", project_root.name)}：学生操作指南\n\n'
        f'入口：\u0060{entry}\u0060；配置：\u0060project.yaml\u0060。'
        '先在 project.yaml 中检查 parameters、插件参数与 data，按需准备数据。\n\n'
        '在本目录用当前 Python 环境执行：\n\n'
        '\u0060\u0060\u0060bash\n'
        './task check\n'
        './task run\n'
        './task status\n'
        './task submit\n'
        '\u0060\u0060\u0060\n\n'
        f'当前算法别名：{aliases}。如需修改固定算法，运行 '
        '\u0060./task edit ALIAS\u0060，再编辑生成的 '
        '\u0060algorithms/ALIAS/\u0060。修改后重新 run，再 submit。\n'
    )
    (project_root / 'README-ResearchKit.md').write_text(guide, encoding='utf-8')


def seal_lock(project_root: Path) -> None:
    write_json(Path(project_root) / '.researchkit/lock.json',
               {'schema': 'researchkit.lock/v1', 'files': protected_files(Path(project_root))})


def _template(name: str | None) -> Path:
    if name is None or name in {'hello', 'hello@1.0.0'}:
        return home() / 'library/cases/hello/1.0.0'
    if name.startswith('saved:'):
        return safe_path(home(), 'library/projects/' + identifier(name[6:]))
    raise KitError(f'没有模板 {name}。可用：hello、saved:<收藏名>')


def new_project(output: Path, template: str | None = None) -> dict:
    source = _template(template)
    if not source.is_dir():
        raise KitError(f'模板不存在：{template or "hello"}')
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(source.resolve()) or source.resolve().is_relative_to(output):
        raise KitError('模板来源和新项目目录不能相互包含')
    with staging(output) as temporary:
        copy_tree(source, temporary, exclude={'_runs', '.researchkit/last-run.json',
                                              '.researchkit/lock.json', '.researchkit/bundle.json',
                                              '.researchkit/saved.json', '.researchkit/review.json',
                                              '.researchkit/review.md', '_submission.json',
                                              'results', 'report'})
        doc = read_yaml(temporary / 'project.yaml')
        doc['name'] = output.name
        write_yaml(temporary / 'project.yaml', doc)
        install_runtime(temporary)
        seal_lock(temporary)
        project(temporary)
    return {'path': str(output), 'name': output.name, 'template': template or 'hello'}


def _script_source(source: Path, command: list[str] | None = None) -> tuple[Path, str]:
    if source.is_file() and source.suffix == '.py':
        return source.parent, source.name
    if source.is_dir():
        if command:
            return source, command[1] if len(command) > 1 else command[0]
        if (source / 'main.py').is_file():
            return source, 'main.py'
    raise KitError('普通脚本导入需要 .py 文件、含 main.py 的目录，或给目录指定 --command')


def _legacy_requirements(case: dict, release: Path) -> dict:
    modules = case.get('dependencies', {}).get('python', [])
    if not isinstance(modules, list):
        raise KitError('旧案例 dependencies.python 必须是模块列表')
    environment = case.get('environment', {}).get('name')
    if not isinstance(environment, str) or not environment:
        raise KitError('旧案例缺少 environment.name')
    env_path = safe_path(release / 'environments', f'{identifier(environment)}.yaml')
    if not env_path.is_file():
        raise KitError(f'快照缺少环境声明 {environment}')
    env = read_yaml(env_path)
    if env.get('schema') != 'hydro.environment/v1':
        raise KitError(f'环境声明格式错误：{environment}')
    required = env.get('required_modules', [])
    minimums = env.get('module_minimums', {})
    if not isinstance(required, list) or not isinstance(minimums, dict):
        raise KitError(f'环境声明模块格式错误：{environment}')
    python = env.get('python', {}).get('minimum', '3.11')
    return {'python': str(python), 'modules': list(dict.fromkeys(
            x for x in modules + required if x != 'yaml')), 'minimums': minimums}


def _legacy_import(source: Path, temporary: Path, platform: Path) -> dict:
    case = read_yaml(source / 'case.yaml')
    if case.get('schema') != 'hydro.case/v1' or not isinstance(case.get('case'), dict):
        raise KitError('case.yaml 不是 hydro.case/v1')
    info = case['case']
    entry = info.get('entry', 'case.py')
    if not safe_path(source, entry).is_file():
        raise KitError(f'旧案例入口不存在：{entry}')
    platform_version = case.get('platform', {}).get('version')
    reference(f'release@{platform_version}')
    release = platform / 'releases' / str(platform_version) / 'runtime'
    if not release.is_dir():
        raise KitError(f'缺少旧平台 release {platform_version} 快照；请提供包含此版本的 --platform 目录')
    # The case source is a working copy; old run output is deliberately omitted.
    copy_tree(source, temporary, exclude={'results', 'report', '_runs', 'case.yaml'})
    configs = {}
    for alias, old in case.get('plugins', {}).items():
        identifier(alias)
        ref = f"{old.get('name')}@{old.get('version')}"
        name, version = reference(ref)
        plugin_source = release / 'plugins' / name / version
        if not plugin_source.is_dir():
            raise KitError(f'快照缺少插件 {ref}；不会改用当前平台副本')
        dest = temporary / '.researchkit/plugins' / alias
        copy_tree(plugin_source, dest)
        meta = plugin(dest)
        if f"{meta['name']}@{meta['version']}" != ref:
            raise KitError(f'快照插件身份不匹配：{ref}')
        params = old.get('parameters', {})
        validate_parameters(meta['parameters'], params, alias)
        _copy_contracts(release / 'contracts', temporary, meta)
        configs[alias] = {'path': f'.researchkit/plugins/{alias}', 'parameters': params}
    doc = {'schema': 'researchkit.project/v1', 'name': temporary.name, 'mode': 'python',
           'entry': entry, 'plugins': configs, 'parameters': case.get('parameters', {}),
           'data': case.get('data', {}), 'requires': _legacy_requirements(case, release),
           'origin': {'kind': 'hydro.case', 'case': f"{info.get('name')}@{info.get('version')}",
                      'platform': str(platform_version)}}
    return doc


def import_project(source: Path, output: Path, command: list[str] | None = None,
                   platform: Path | None = None) -> dict:
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if not source.exists():
        raise KitError(f'导入来源不存在：{source}')
    if source.is_dir() and (output.is_relative_to(source) or source.is_relative_to(output)):
        raise KitError('导入来源和目标目录不能相互包含；请选独立的新目录')
    legacy = source.is_dir() and (source / 'case.yaml').is_file()
    if legacy and platform is None:
        raise KitError('旧 HYDRO 案例导入需要 --platform 指向旧平台根目录')
    if not legacy and platform is not None:
        raise KitError('--platform 仅用于含 case.yaml 的旧 HYDRO 案例')
    with staging(output) as temporary:
        if legacy:
            doc = _legacy_import(source, temporary, Path(platform).expanduser().resolve())
        else:
            base, entry = _script_source(source, command)
            if source.is_file():
                shutil.copy2(source, temporary / entry)
            else:
                copy_tree(base, temporary, exclude={'results', 'report', '_runs', '.researchkit',
                                                    'project.yaml', 'task'})
            argv = command or ['python', entry]
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
                raise KitError('command 必须是非空 argv 数组')
            doc = {'schema': 'researchkit.project/v1', 'name': output.name, 'mode': 'command',
                   'command': argv, 'plugins': {}, 'parameters': {}, 'data': {},
                   'requires': {'python': '3.11', 'modules': [], 'minimums': {}},
                   'origin': {'kind': 'script', 'entry': entry}}
        doc['name'] = output.name
        write_yaml(temporary / 'project.yaml', doc)
        install_runtime(temporary)
        seal_lock(temporary)
        project(temporary)
    return {'path': str(output), 'name': output.name, 'mode': doc['mode'],
            'plugins': list(doc['plugins'])}


def edit_plugin(project_root: Path, alias: str) -> dict:
    project_root = Path(project_root).resolve()
    doc = project(project_root)
    if alias not in doc['plugins']:
        raise KitError(f'项目没有算法别名 {alias}')
    current = safe_path(project_root, doc['plugins'][alias]['path'])
    editable = project_root / 'algorithms' / alias
    if editable.exists():
        raise KitError(f'可编辑副本已存在：{editable}')
    copy_tree(current, editable)
    doc['plugins'][alias]['path'] = f'algorithms/{alias}'
    write_yaml(project_root / 'project.yaml', doc)
    return {'path': str(editable), 'alias': alias}


def add_plugin(project_root: Path, ref: str, alias: str, parameters: dict) -> dict:
    project_root = Path(project_root).resolve()
    doc = project(project_root)
    identifier(alias)
    if alias in doc['plugins'] or (project_root / '.researchkit/plugins' / alias).exists():
        raise KitError(f'算法别名已存在：{alias}')
    source = _library_plugin(ref)
    if not source.is_dir():
        raise KitError(f'新算法库没有 {ref}；请先 publish')
    meta = plugin(source)
    if f"{meta['name']}@{meta['version']}" != ref:
        raise KitError(f'算法身份不匹配：{ref}')
    validate_parameters(meta['parameters'], parameters, alias)
    destination = project_root / '.researchkit/plugins' / alias
    # Validate all contract sources before changing the project.
    contract_targets = []
    for used in set(meta['inputs'].values()) | set(meta['outputs'].values()):
        name, version = reference(used)
        original = home() / 'library/contracts' / name / f'{version}.yaml'
        target = project_root / '.researchkit/contracts' / name / f'{version}.yaml'
        if not original.is_file():
            raise KitError(f'缺少契约 {used}；请先把它发布到新算法库')
        if target.exists() and target.read_bytes() != original.read_bytes():
            raise KitError(f'契约 {used} 内容冲突')
        contract_targets.append((target, target.exists()))
    old_doc = (project_root / 'project.yaml').read_text(encoding='utf-8')
    lock_path = project_root / '.researchkit/lock.json'
    old_lock = lock_path.read_text(encoding='utf-8')
    try:
        copy_tree(source, destination)
        _copy_contracts(home() / 'library/contracts', project_root, meta)
        doc['plugins'][alias] = {'path': f'.researchkit/plugins/{alias}', 'parameters': parameters}
        write_yaml(project_root / 'project.yaml', doc)
        seal_lock(project_root)
        project(project_root)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        atomic_text(project_root / 'project.yaml', old_doc)
        atomic_text(lock_path, old_lock)
        for target, existed in contract_targets:
            if not existed:
                target.unlink(missing_ok=True)
        raise
    return {'path': str(destination), 'alias': alias, 'ref': ref}


def _contract_source(source: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    for parent in source.parents:
        if (parent / 'project.yaml').is_file():
            return parent / '.researchkit/contracts'
    return home() / 'library/contracts'


def publish_plugin(source: Path, contracts: Path | None = None) -> dict:
    source = Path(source).expanduser().resolve()
    meta = plugin(source)
    ref = f"{meta['name']}@{meta['version']}"
    destination = _library_plugin(ref)
    if destination.exists():
        raise KitError(f'版本已存在：{ref}；请提升 plugin.yaml 中的版本号')
    contract_root = _contract_source(source, contracts)
    contract_copies = []
    for used in set(meta['inputs'].values()) | set(meta['outputs'].values()):
        name, version = reference(used)
        original = contract_root / name / f'{version}.yaml'
        target = home() / 'library/contracts' / name / f'{version}.yaml'
        if not original.is_file():
            raise KitError(f'缺少契约 {used}；用 --contracts 指向契约目录')
        if target.exists() and original.read_bytes() != target.read_bytes():
            raise KitError(f'算法库契约 {used} 已存在但内容不同')
        contract_copies.append((original, target))
    test = source / 'test_plugin.py'
    if not test.is_file():
        raise KitError('发布算法前请编写并通过 test_plugin.py')
    runner = (
        'import importlib.util, sys, unittest\n'
        'from pathlib import Path\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from researchkit.engine import load_plugin\n'
        'source, test = Path(sys.argv[2]), Path(sys.argv[3])\n'
        'sys.modules["plugin"] = load_plugin(source, "publish")\n'
        'spec = importlib.util.spec_from_file_location("researchkit_publication_test", test)\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'sys.modules[spec.name] = module\n'
        'spec.loader.exec_module(module)\n'
        'classes = [x for x in vars(module).values() if isinstance(x, type) '
        'and issubclass(x, unittest.TestCase) and x is not unittest.TestCase]\n'
        'suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(x) '
        'for x in classes)\n'
        'if suite.countTestCases() == 0: '
        'raise SystemExit("test_plugin.py 至少需要一个 unittest.TestCase 测试")\n'
        'result = unittest.TextTestRunner(verbosity=2).run(suite)\n'
        'raise SystemExit(0 if result.wasSuccessful() else 1)\n'
    )
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    package_parent = str(Path(__file__).resolve().parent.parent)
    with tempfile.TemporaryDirectory(prefix='rk-test-') as neutral:
        try:
            result = subprocess.run(
                [sys.executable, '-B', '-c', runner, package_parent, str(source), str(test)],
                cwd=neutral, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise KitError('算法测试超过 120 秒，未发布') from exc
    if result.returncode:
        raise KitError('算法测试未通过：\n' + result.stdout[-4000:])
    for original, target in contract_copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(original, target)
    with staging(destination) as temporary:
        copy_tree(source, temporary)
    return {'path': str(destination), 'ref': ref, 'tests': 'passed'}
