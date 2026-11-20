"""Project-local experiment files, also usable by the future Web editor.

No service calls, receipts, code/result verification or background execution.
The only service use is Context.open_dataset when the researcher explicitly
chooses an authorized platform dataset.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import csv
import fcntl
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchkit import model
from researchkit.engine import Context
from researchkit.files import KitError, atomic_text, safe_path, write_json

SCHEMA = 'researchkit.experiment/v1'
RESULT_SCHEMA = 'researchkit.experiment-result/v1'
RESULT_RETENTION = 'latest-success/v1'
_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z')
_OVERRIDES: dict = {}


@contextmanager
def _scheme_lock(root: Path, scheme_id: str):
    """同方案禁止同时写入；不同方案仍可独立运行。锁不进入提交内容。"""
    if not _ID.fullmatch(scheme_id):
        raise KitError('方案 ID 无效')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        for name in ('_runs', 'experiment-locks'):
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                os.close(child)
                raise KitError('实验锁目录不安全')
            os.close(fd)
            fd = child
        lock_fd = os.open(scheme_id + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise KitError('实验锁文件不安全')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KitError('此方案正在运行或整理，请结束后重试；可同时运行另一个方案') from exc
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)


def _result_record(path: Path, scheme_id: str):
    """只识别标准实验记录，不把未知目录或用户手工文件当作可清理产物。"""
    source = path / 'experiment.json'
    if path.is_symlink() or not path.is_dir() or source.is_symlink():
        return None
    try:
        with source.open(encoding='utf-8') as stream:
            value = json.loads(stream.read(1024 * 1024 + 1))
        if (type(value) is dict and value.get('schema') == RESULT_SCHEMA
                and value.get('scheme_id') == scheme_id and value.get('batch_id') == path.name
                and value.get('status') in {'running', 'succeeded', 'failed', 'interrupted'}):
            return value
    except (OSError, ValueError):
        pass
    return None


def _result_order(record: dict):
    try:
        started = datetime.fromisoformat(record.get('started_at', '').replace('Z', '+00:00'))
        when = started.timestamp() if started.tzinfo else 0
    except (ValueError, TypeError, AttributeError):
        when = 0
    return when, record['batch_id']


def _results_directory(root: Path, scheme_id: str):
    """结果路径必须是真实目录；不沿用指向其他位置的目录链接。"""
    current = root
    for name in ('results', scheme_id):
        current = current / name
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise KitError('实验结果目录不安全')
    return current


def _safe_remove_batch(path: Path, *, keep_diagnostics: bool = False):
    """拒绝链接和外部可写目录；删除局限在当前方案的已识别批次内。"""
    for directory, folders, files in os.walk(path, followlinks=False):
        for item in [Path(directory), *(Path(directory) / n for n in folders + files)]:
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or
                    (stat.S_ISREG(info.st_mode) and info.st_nlink == 1))):
                raise KitError(f'产物含不安全文件，未自动整理：{path.name}')
    # shutil.rmtree 使用文件描述符保护目录遍历，不跟随被替换的符号链接。
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if keep_diagnostics:
            batch_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for name in os.listdir(batch_fd):
                    if name in {'experiment.json', 'parameters.json', 'report.md'}:
                        continue
                    info = os.stat(name, dir_fd=batch_fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        shutil.rmtree(name, dir_fd=batch_fd)
                    else:
                        os.unlink(name, dir_fd=batch_fd)
            finally:
                os.close(batch_fd)
        else:
            shutil.rmtree(path.name, dir_fd=parent)
    finally:
        os.close(parent)


def _compact_scheme(root: Path, scheme_id: str):
    """在方案锁内保留最新成功结果，及其之后至多一份失败诊断。"""
    directory = safe_path(root, f'results/{scheme_id}')
    if directory.is_symlink() or not directory.is_dir():
        return []
    found = [(path, record) for path in directory.iterdir()
             if (record := _result_record(path, scheme_id)) is not None]
    finished = [(path, record) for path, record in found if record['status'] != 'running']
    successes = [(path, record) for path, record in finished if record['status'] == 'succeeded']
    latest = max(successes, key=lambda row: _result_order(row[1]), default=None)
    failures = [(path, record) for path, record in finished if record['status'] in {'failed', 'interrupted'}
                and (latest is None or _result_order(record) > _result_order(latest[1]))]
    failure = max(failures, key=lambda row: _result_order(row[1]), default=None)
    retained = {row[0] for row in (latest, failure) if row is not None}
    warnings = []
    for path, record in finished:
        try:
            if _result_record(path, scheme_id) != record:
                raise KitError(f'产物记录已变化，未整理：{path.name}')
            if path not in retained:
                _safe_remove_batch(path)
            elif failure is not None and path == failure[0]:
                _safe_remove_batch(path, keep_diagnostics=True)
                record['artifacts'] = []
                write_json(path / 'experiment.json', record)
                _write_report(path, record)
        except (KitError, OSError) as exc:
            warnings.append(str(exc))
    return warnings


def _compact_after_run(root: Path, scheme_id: str):
    try:
        return _compact_scheme(root, scheme_id)
    except (KitError, OSError) as exc:
        # 整理异常不能把已经成功的实验反报成失败，更不能提前删除新结果。
        return [f'产物已保存，旧批次暂未整理：{exc}']


def compact_results(root: Path):
    """IDE 中显式整理旧批次；不会访问数据库、共享数据或固定提交。"""
    results = safe_path(root, 'results')
    if results.is_symlink():
        raise KitError('results 不能使用符号链接')
    warnings = []
    if results.is_dir():
        info = results.lstat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise KitError('实验结果目录不安全')
        for directory in sorted(results.iterdir()):
            if _ID.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink():
                with _scheme_lock(root, directory.name):
                    warnings.extend(_compact_scheme(root, directory.name))
    for warning in warnings:
        print(f'整理提示：{warning}', file=sys.stderr)
    print('已整理：每方案保留最新成功产物；失败只留最近一次参数和错误。')
    return warnings


def _json_value(value):
    """YAML values must also be ordinary finite JSON configuration values."""
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child)
        return
    raise KitError('方案参数只能使用文本、有限数值、布尔值、列表或映射')


def read_scheme(root: Path, scheme_id: str) -> dict:
    if not isinstance(scheme_id, str) or not _ID.fullmatch(scheme_id):
        raise KitError('方案名只能包含字母、数字、点、下划线和连字符')
    source = safe_path(root, f'experiments/{scheme_id}.yaml')
    if source.is_symlink():
        raise KitError('方案不能使用符号链接')
    try:
        if source.stat().st_size > 256 * 1024:
            raise KitError('方案文件过大')
        document = yaml.safe_load(source.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError, RecursionError) as exc:
        raise KitError(f'无法读取方案 {scheme_id}：{exc}') from exc
    fields = {'schema', 'name', 'description', 'case_parameters', 'algorithm_parameters'}
    if (type(document) is not dict or set(document) - fields or
            document.get('schema') != SCHEMA or
            not isinstance(document.get('name'), str) or not document['name'].strip() or
            not isinstance(document.get('description', ''), str)):
        raise KitError(f'方案 {scheme_id} 格式错误，请参考 experiments/baseline.yaml')
    for key in ('case_parameters', 'algorithm_parameters'):
        if type(document.get(key, {})) is not dict:
            raise KitError(f'方案 {key} 必须是映射')
    document.setdefault('case_parameters', {})
    document.setdefault('algorithm_parameters', {})
    _json_value(document)
    return document


def effective_document(context: Context, scheme: dict, overrides: dict | None = None):
    doc = copy.deepcopy(context.document)
    # Structural project parameters are inherited; scheme fields override them.
    doc['parameters'].update(copy.deepcopy(scheme['case_parameters']))
    if overrides:
        doc['parameters'].update(overrides)
    _json_value(doc['parameters'])
    unknown = set(scheme['algorithm_parameters']) - set(doc['plugins'])
    if unknown:
        raise KitError(f'方案引用了未加入项目的算法别名：{sorted(unknown)}')
    for alias, config in doc['plugins'].items():
        explicit = scheme['algorithm_parameters'].get(alias, {})
        if type(explicit) is not dict:
            raise KitError(f'算法 {alias} 的方案参数必须是映射')
        meta = model.plugin(safe_path(context.case_dir, config['path']))
        values = {name: copy.deepcopy(rule['default']) for name, rule in meta['parameters'].items()
                  if type(rule) is dict and 'default' in rule}
        values.update(config.get('parameters', {}))
        values.update(copy.deepcopy(explicit))
        model.validate_parameters(meta['parameters'], values, alias)
        _json_value(values)
        config['parameters'] = values
    return doc


def _artifact_list(batch: Path):
    result = []
    for source in sorted(batch.rglob('*')):
        relative = source.relative_to(batch)
        if any(part in {'_work', '__pycache__'} for part in relative.parts):
            continue
        if source.is_symlink():
            raise KitError('实验产物不能使用符号链接')
        if source.is_file() and source.name not in {'experiment.json', 'parameters.json'}:
            result.append({'path': relative.as_posix(), 'bytes': source.stat().st_size})
    return result


def _write_report(batch: Path, record: dict):
    lines = [f'# {record["name"]}', '', f'方案：`{record["scheme_id"]}`  ',
             f'批次：`{record["batch_id"]}`  ', f'状态：{record["status"]}', '',
             '参数副本：[parameters.json](parameters.json)', '', '## 结果摘要', '',
             '```json', json.dumps(record.get('summary', {'error': record.get('error', '')}),
                                ensure_ascii=False, indent=2), '```', '', '## 产物', '']
    for artifact in record['artifacts']:
        path = artifact['path']
        lines.append(f'- [{path}]({path})（{artifact["bytes"]} 字节）')
        if Path(path).suffix.lower() in {'.png', '.jpg', '.jpeg', '.svg'}:
            lines.extend(['', f'![{Path(path).name}]({path})', ''])
    (batch / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def compare_latest(root: Path, scheme_ids: list[str]):
    """Simple IDE-side comparison now, without waiting for the Web changes."""
    if len(set(scheme_ids)) < 2:
        raise KitError('请选择至少两个不同的方案进行比较')
    records = []
    for scheme_id in scheme_ids:
        read_scheme(root, scheme_id)
        results = safe_path(root, f'results/{scheme_id}')
        candidates = []
        for path in sorted(results.glob('*/experiment.json')):
            record = _result_record(path.parent, scheme_id)
            if record is not None and record['status'] == 'succeeded':
                candidates.append(record)
        if not candidates:
            raise KitError(f'方案 {scheme_id} 还没有成功完成的实验，请先运行')
        records.append(max(candidates, key=_result_order))
    columns = [record['scheme_id'] for record in records]
    rows = [['item', *columns], ['batch', *[r['batch_id'] for r in records]]]

    def flatten(prefix, value, target):
        if isinstance(value, dict):
            for key, child in sorted(value.items()):
                flatten(f'{prefix}.{key}', child, target)
        elif value is None or type(value) in (str, bool, int, float):
            target[prefix] = value

    values = []
    for record in records:
        flat = {}
        flatten('parameter', record['parameters'], flat)
        flatten('metric', record.get('summary', {}), flat)
        values.append(flat)
    for key in sorted(set().union(*(set(value) for value in values))):
        rows.append([key, *[value.get(key, '') for value in values]])
    # 同一组方案反复比较时更新同一张表，避免比较文件也无限累积。
    key = hashlib.sha256('\0'.join(sorted(set(scheme_ids))).encode()).hexdigest()[:16]
    target = root / 'results' / f'comparison-{key}.csv'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise KitError('results 不能使用符号链接')
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows(rows)
    atomic_text(target, '\ufeff' + stream.getvalue())
    for row in rows:
        print('\t'.join(str(value) for value in row))
    print(f'对比表已保存：{target}')
    return target


def run_experiment(context: Context, execute, scheme_id: str | None = None):
    """Run in this interpreter, retaining ordinary IDE breakpoints and GPU use."""
    if getattr(context, "_experiment_active", False):
        return execute(context)
    scheme_id = scheme_id or _OVERRIDES.get('scheme') or context.parameters.get('experiment_scheme', 'baseline')
    scheme = read_scheme(context.case_dir, scheme_id)
    doc = effective_document(context, scheme, _OVERRIDES.get('case_parameters'))
    doc['parameters']['experiment_scheme'] = scheme_id
    # This is a user environment check, not a requirement for platform review.
    model.environment(doc)
    with _scheme_lock(context.case_dir, scheme_id):
        return _run_batch(context, execute, scheme_id, scheme, doc)


def _run_batch(context, execute, scheme_id, scheme, doc):
    """先完整保存新批次，再整理旧结果；失败不能损坏上一次成功产物。"""
    started = datetime.now(timezone.utc)
    batch_id = started.strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid.uuid4().hex[:8]
    batch = _results_directory(context.case_dir, scheme_id) / batch_id
    batch.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = Context(context.case_dir, doc)
    runtime._experiment_active = True
    runtime._roots = {
        'result': batch,
        'processed': batch / 'processed',
        'artifact': batch / 'artifacts',
        'report': batch / 'report',
        'work': batch / '_work',
    }
    for folder in runtime._roots.values():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    parameters = {'case': copy.deepcopy(doc['parameters']),
                  'algorithms': {alias: copy.deepcopy(cfg['parameters'])
                                 for alias, cfg in doc['plugins'].items()}}
    parameters['case']['experiment_scheme'] = scheme_id
    record = {'schema': RESULT_SCHEMA, 'scheme_id': scheme_id,
              'name': scheme['name'], 'description': scheme.get('description', ''),
              'batch_id': batch_id, 'started_at': started.isoformat(),
              'status': 'running', 'parameters': parameters, 'artifacts': [],
              'execution': 'user-environment'}
    write_json(batch / 'parameters.json', parameters)
    write_json(batch / 'experiment.json', record)
    print(f'实验方案：{scheme["name"]} ({scheme_id})', flush=True)
    print(f'本次产物：{batch}', flush=True)
    try:
        summary = execute(runtime)
        _json_value(summary)
        record['status'] = 'succeeded'
        record['summary'] = summary
        return summary
    except BaseException as exc:
        record['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed'
        record['error'] = f'{type(exc).__name__}: {exc}'[:2000]
        raise
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        try:
            record['artifacts'] = _artifact_list(batch)
        except Exception as exc:
            record['artifact_warning'] = str(exc)
        write_json(batch / 'experiment.json', record)
        try:
            _write_report(batch, record)
        except OSError as exc:
            print(f'报告预览未生成，但原始产物仍保留：{exc}', file=sys.stderr)
        for warning in _compact_after_run(context.case_dir, scheme_id):
            print(f'整理提示：{warning}', file=sys.stderr)
        print(f'实验状态：{record["status"]}；产物保存在 {batch}', flush=True)


def _load_case(root: Path, doc: dict):
    entry = safe_path(root, doc.get('entry', 'case.py'))
    from researchkit.engine import _load_module
    module = _load_module(entry, '_rk_experiment_' + uuid.uuid4().hex, [root, root / 'src'])
    if not callable(getattr(module, 'run', None)):
        raise KitError('Python 案例入口需要定义 run(context)')
    return module


def main(root: Path, argv=None):
    parser = argparse.ArgumentParser(description='在当前 IDE/Python 环境按方案运行；自动保存参数和产物')
    parser.add_argument('--list', action='store_true', help='列出可用方案')
    parser.add_argument('--scheme', help='方案文件名，不含 .yaml；默认 baseline')
    parser.add_argument('--compare', nargs='+', metavar='SCHEME', help='比较各方案最近一次成功结果，并保存 CSV')
    parser.add_argument('--compact', action='store_true', help='整理现有旧批次，每方案保留最新成功产物和最近错误')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--data-root', help='本次使用本地只读数据目录/数据包，不修改方案文件')
    source.add_argument('--dataset-id', help='本次使用已加入项目且已获授权的平台数据版本')
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    try:
        if args.compact:
            if args.scheme or args.list or args.compare or args.data_root or args.dataset_id:
                raise KitError('--compact 请单独使用')
            compact_results(root)
            return 0
        if args.compare:
            if args.scheme or args.list or args.data_root or args.dataset_id:
                raise KitError('--compare 请单独使用，只需指定要比较的方案名')
            compare_latest(root, args.compare)
            return 0
        if args.list:
            for path in sorted((root / 'experiments').glob('*.yaml')):
                scheme = read_scheme(root, path.stem)
                print(f'{path.stem}\t{scheme["name"]}\t{scheme.get("description", "")}')
            return 0
        overrides = {}
        if args.data_root:
            path = Path(args.data_root).expanduser()
            if not path.is_absolute():
                path = root / path
            overrides.update(data_source='local', data_root=str(path.resolve()), dataset_id='')
        elif args.dataset_id:
            overrides.update(data_source='dataset', dataset_id=args.dataset_id, data_root='')
        _OVERRIDES.update(scheme=args.scheme, case_parameters=overrides)
        doc = model.project(root)
        model.verify_lock(root)
        module = _load_case(root, doc)
        # Always preserve the declared entry. Example wrappers recognize the
        # active Context and execute within this same batch, without nesting.
        result = run_experiment(Context(root, doc), module.run)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (KitError, ValueError, OSError) as exc:
        print(f'无法完成实验：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('实验已中断，已生成的产物保留。', file=sys.stderr)
        return 130
    finally:
        _OVERRIDES.clear()
