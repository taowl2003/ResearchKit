"""Project-local experiment files, also usable by the future Web editor.

No service calls, receipts, code/result verification or background execution.
The only service use is Context.open_dataset when the researcher explicitly
chooses an authorized platform dataset.
"""
from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchkit import model
from researchkit.engine import Context
from researchkit.files import KitError, safe_path, write_json

SCHEMA = 'researchkit.experiment/v1'
RESULT_SCHEMA = 'researchkit.experiment-result/v1'
_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z')
_OVERRIDES: dict = {}


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
            record = json.loads(path.read_text(encoding='utf-8'))
            if (record.get('schema') == RESULT_SCHEMA and record.get('status') == 'succeeded'
                    and record.get('scheme_id') == scheme_id):
                candidates.append(record)
        if not candidates:
            raise KitError(f'方案 {scheme_id} 还没有成功完成的实验，请先运行')
        records.append(candidates[-1])
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
    target = root / 'results' / ('comparison-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
                                 + '-' + uuid.uuid4().hex[:8] + '.csv')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows(rows)
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
    started = datetime.now(timezone.utc)
    batch_id = started.strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid.uuid4().hex[:8]
    batch = safe_path(context.case_dir, f'results/{scheme_id}/{batch_id}')
    batch.mkdir(parents=True, exist_ok=False)
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
        folder.mkdir(parents=True, exist_ok=True)
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--data-root', help='本次使用本地只读数据目录/数据包，不修改方案文件')
    source.add_argument('--dataset-id', help='本次使用已加入项目且已获授权的平台数据版本')
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    try:
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
