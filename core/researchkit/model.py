from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path

from .files import KitError, digest, file_stats, identifier, read_json, read_yaml, reference, reject_links, safe_path, sha256, snapshot

PROJECT_SCHEMA = 'researchkit.project/v1'
MANAGED = ('data/processed', 'artifacts', 'results', 'report', '_work')
OUTPUTS = ('results', 'report')
TRANSIENT = {'.researchkit/local.json', '.researchkit/last-run.json', '.researchkit/review.json', '.researchkit/run.lock', '_submission.json'}


def project(root: Path) -> dict:
    doc = read_yaml(root / 'project.yaml')
    if doc.get('schema') != PROJECT_SCHEMA or not isinstance(doc.get('name'), str):
        raise KitError('project.yaml 必须使用 researchkit.project/v1 并填写 name')
    if doc.get('mode') not in {'python', 'command'}:
        raise KitError('mode 必须是 python 或 command')
    if doc['mode'] == 'python':
        entry = safe_path(root, doc.get('entry', 'case.py'))
        if not entry.is_file():
            raise KitError(f'项目入口不存在：{entry.name}')
        signature(entry, ['context'])
    else:
        command = doc.get('command')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise KitError('command 必须是非空参数数组，例如 [python, main.py]')
    for field in ('parameters', 'plugins', 'data', 'requires'):
        if not isinstance(doc.get(field, {}), dict):
            raise KitError(f'{field} 必须是映射')
        doc.setdefault(field, {})
    for alias, cfg in doc['plugins'].items():
        identifier(alias)
        if not isinstance(cfg, dict) or not isinstance(cfg.get('parameters', {}), dict):
            raise KitError(f'算法 {alias} 配置或参数错误')
        directory = safe_path(root, cfg.get('path'))
        meta = plugin(directory)
        values = {key: rule['default'] for key, rule in meta['parameters'].items()
                  if isinstance(rule, dict) and 'default' in rule}
        values.update(cfg.get('parameters', {}))
        validate_parameters(meta['parameters'], values, alias)
        for ref in list(meta['inputs'].values()) + list(meta['outputs'].values()):
            contract(root, ref)
    return doc


def signature(path: Path, names: list[str]) -> None:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise KitError(f'{path.name} 语法错误：{exc}') from exc
    run = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run'), None)
    if run is None or [x.arg for x in run.args.posonlyargs + run.args.args] != names or run.args.vararg or run.args.kwarg or run.args.kwonlyargs:
        raise KitError(f'{path.name} 需要定义 def run({", ".join(names)})')


def plugin(directory: Path) -> dict:
    meta = read_yaml(directory / 'plugin.yaml')
    if meta.get('schema') != 'hydro.plugin/v1' or meta.get('entry') != 'plugin.py':
        raise KitError(f'{directory.name} 算法入口须为 plugin.py，schema 须为 hydro.plugin/v1')
    identifier(meta.get('name'))
    reference(f"{meta.get('name')}@{meta.get('version')}")
    for key in ('inputs', 'outputs', 'parameters'):
        if not isinstance(meta.get(key), dict):
            raise KitError(f'{directory.name} 的 {key} 必须是映射')
    for key in ('inputs', 'outputs'):
        for port, ref in meta[key].items():
            identifier(port)
            reference(ref)
    signature(directory / 'plugin.py', ['inputs', 'parameters', 'context'])
    return meta


def check_type(kind: str, value) -> bool:
    types = {'number': lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
             'integer': lambda x: isinstance(x, int) and not isinstance(x, bool),
             'string': lambda x: isinstance(x, str), 'boolean': lambda x: isinstance(x, bool),
             'mapping': lambda x: isinstance(x, dict), 'sequence': lambda x: isinstance(x, (list, tuple)),
             'any': lambda x: True}
    if kind not in types:
        raise KitError(f'不支持的契约/参数类型：{kind}')
    return types[kind](value)


def validate_parameters(rules: dict, values: dict, label: str) -> None:
    for key in rules:
        identifier(key)
    extra = set(values) - set(rules)
    if extra:
        raise KitError(f'{label} 未声明参数：{sorted(extra)}')
    for key, rule in rules.items():
        if not isinstance(rule, dict):
            raise KitError(f'{label}.{key} 的参数规则必须是映射')
        if rule.get('required') and key not in values:
            raise KitError(f'{label} 缺少参数 {key}；用 --param {key}=值 设置')
        if key in values:
            value = values[key]
            if not check_type(rule.get('type', 'any'), value):
                raise KitError(f'{label}.{key} 应为 {rule.get("type")}')
            if 'enum' in rule and (not isinstance(rule['enum'], list) or value not in rule['enum']):
                raise KitError(f'{label}.{key} 不在允许值内')
            for bound, op in [('minimum', lambda a,b: a < b), ('maximum', lambda a,b: a > b)]:
                if bound in rule:
                    if not check_type('number', value) or not check_type('number', rule[bound]):
                        raise KitError(f'{label}.{key} 的 {bound} 仅适用于数值')
                    if op(value, rule[bound]):
                        raise KitError(f'{label}.{key} 超出 {bound}={rule[bound]}')


def contract(root: Path, ref: str) -> dict:
    name, version = reference(ref)
    doc = read_yaml(safe_path(root, f'.researchkit/contracts/{name}/{version}.yaml'))
    if doc.get('schema') != 'hydro.contract/v1' or doc.get('id') != ref:
        raise KitError(f'契约身份不匹配：{ref}')
    if not isinstance(doc.get('type'), str):
        raise KitError(f'契约 {ref} 缺少 type')
    check_type(doc['type'], None)
    return doc


def ports(root: Path, definitions: dict, values, label: str) -> None:
    if not isinstance(values, dict) or set(values) != set(definitions):
        raise KitError(f'{label} 端口不匹配；需要 {sorted(definitions)}')
    for key, ref in definitions.items():
        kind = contract(root, ref)['type']
        if not check_type(kind, values[key]):
            raise KitError(f'{label}.{key} 违反 {ref}，应为 {kind}')


def environment(doc: dict) -> dict:
    requirements = doc.get('requires', {})
    minimum_python = requirements.get('python', '3.11')
    if not isinstance(minimum_python, str) or not re.fullmatch(r'\d+(?:\.\d+){1,2}', minimum_python):
        raise KitError('requires.python 必须是版本号，例如 3.11')
    wanted = tuple(int(x) for x in minimum_python.split('.'))
    if sys.version_info[:len(wanted)] < wanted:
        raise KitError(f'需要 Python >= {requirements["python"]}')
    modules = requirements.get('modules', [])
    if not isinstance(modules, list) or not all(isinstance(x, str) and x for x in modules):
        raise KitError('requires.modules 必须是模块名数组')
    observed = {}
    for name in dict.fromkeys(['yaml'] + modules):
        try:
            found = importlib.util.find_spec(name.split('.')[0])
        except (ValueError, ImportError):
            found = None
        if found is None:
            raise KitError(f'缺少 Python 模块 {name}。请切换到已安装该依赖的环境后运行。')
        distribution = {'yaml': 'PyYAML'}.get(name, name.split('.')[0])
        try:
            observed[name] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            observed[name] = 'unknown'
    minimums = requirements.get('minimums', {})
    if not isinstance(minimums, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in minimums.items()):
        raise KitError('requires.minimums 必须是模块名到版本号的映射')
    for name, minimum in minimums.items():
        current = observed.get(name, '')
        numeric = lambda s: tuple(int(x) for x in re.findall(r'^\d+(?:\.\d+)*', str(s))[0].split('.'))
        try:
            a, b = numeric(current), numeric(minimum)
        except (IndexError, ValueError):
            raise KitError(f'无法确认 {name} >= {minimum}，当前 {current}') from None
        width = max(len(a),len(b))
        if a + (0,) * (width-len(a)) < b + (0,) * (width-len(b)):
            raise KitError(f'{name} 版本 {current} 低于 {minimum}')
    return {'python': '.'.join(map(str, sys.version_info[:3])), 'modules': observed}


def verify_lock(root: Path) -> None:
    lock = read_json(root / '.researchkit/lock.json')
    expected = lock.get('files', {})
    if lock.get('schema') != 'researchkit.lock/v1' or not expected:
        raise KitError('项目依赖锁缺失或无效')
    actual = protected_files(root)
    if expected != actual:
        changes = [p for p in sorted(set(actual) | set(expected)) if actual.get(p) != expected.get(p)]
        raise KitError('固定依赖发生变化；请用 edit 创建算法副本：' + ', '.join(changes[:5]))


def protected_files(root: Path) -> dict:
    records = {}
    for rel in ('task', '.researchkit/runtime', '.researchkit/plugins', '.researchkit/contracts'):
        path = root / rel
        if path.is_file():
            records[rel] = sha256(path)
        elif path.is_dir():
            for sub, value in snapshot(path).items():
                records[f'{rel}/{sub}'] = value
    return records


def input_files(root: Path) -> dict:
    return snapshot(root, exclude=set(OUTPUTS) | TRANSIENT | {'.researchkit/review.md'})


def input_stats(root: Path) -> dict:
    return file_stats(root, exclude=set(OUTPUTS) | TRANSIENT | {'.researchkit/review.md'})


def output_files(root: Path) -> dict:
    records = {}
    for rel in ('results', 'report', 'data/processed', 'artifacts'):
        path = root / rel
        if path.is_dir():
            for sub, value in snapshot(path).items():
                records[f'{rel}/{sub}'] = value
    return records


def output_stats(root: Path) -> dict:
    return {k:v for k,v in file_stats(root).items() if k.startswith(('results/', 'report/', 'data/processed/', 'artifacts/'))}


def local_fingerprint(root: Path) -> str:
    """One-way fingerprint; machine paths stay out of shared records."""
    path = root / '.researchkit/local.json'
    if not path.exists():
        return digest({})
    doc = read_json(path)
    data_root = doc.get('data_root')
    if data_root is None:
        return digest(doc)
    if not isinstance(data_root, str) or not data_root:
        raise KitError('local.json 的 data_root 必须是路径字符串')
    mapped = Path(data_root).expanduser()
    if not mapped.is_absolute():
        mapped = root / mapped
    if mapped.is_dir():
        state = file_stats(mapped)
    elif mapped.is_file():
        stat = mapped.stat()
        state = {'file': [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]}
    else:
        state = {'missing': True}
    return digest({'mapping': doc, 'state': state})


def run_fingerprint(root: Path) -> tuple[str, dict]:
    files = input_files(root)
    return digest({'files': files, 'local': local_fingerprint(root)}), input_stats(root)


def freshness(root: Path) -> dict:
    path = root / '.researchkit/last-run.json'
    if not path.exists():
        return {'status': 'not-run', 'message': '尚未在 ResearchKit 中运行'}
    record = read_json(path)
    local = local_fingerprint(root)
    changed = local != record.get('local_fingerprint')
    if not changed and record.get('input_stats') != input_stats(root):
        # Copies and archive extraction change timestamps without changing inputs.
        changed = run_fingerprint(root)[0] != record.get('input_hash')
    if not changed and record.get('output_stats') != output_stats(root):
        changed = output_files(root) != record.get('outputs', {})
    status = record.get('status')
    if status not in {'success', 'failed'}:
        status = 'failed'
    return {'status': 'stale' if changed else status,
            'message': '代码、数据或结果在运行后发生变化' if changed else '当前文件对应最近一次运行',
            'result': record.get('result'), 'run_id': record.get('run_id')}


def doctor(root: Path) -> dict:
    reject_links(root)
    doc = project(root)
    verify_lock(root)
    env = environment(doc)
    return {'status': 'ready', 'name': doc['name'], 'environment': env, 'run': freshness(root)}
