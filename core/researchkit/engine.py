"""Isolated project execution and the legacy-compatible Context API."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import traceback
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files import KitError, atomic_text, inside, read_json, reject_links, safe_path, sha256, write_json, write_yaml
from . import model


def _load_module(path: Path, package: str, search: list[Path]):
    """Load a source file as a package child with isolated relative imports."""
    cached = sys.modules.get(package + '.entry')
    if cached is not None:
        return cached
    parent = types.ModuleType(package)
    parent.__path__ = [str(directory) for directory in search if directory.is_dir()]
    parent.__package__ = package
    sys.modules[package] = parent
    spec = importlib.util.spec_from_file_location(package + '.entry', path)
    if spec is None or spec.loader is None:
        raise KitError(f'无法加载代码：{path.name}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def load_plugin(directory: Path, alias: str):
    """Load one plugin without placing its bare modules on the global import path."""
    directory = Path(directory).resolve()
    key = hashlib.sha256(f'{alias}\0{directory}'.encode()).hexdigest()[:20]
    return _load_module(directory / 'plugin.py', f'_researchkit_plugin_{key}',
                        [directory, directory / 'src'])


class Context:
    def __init__(self, root: Path, document: dict):
        self.case_dir = root.resolve()
        self.document = document
        self._roots = {
            'processed': self.case_dir / 'data' / 'processed',
            'artifact': self.case_dir / 'artifacts',
            'result': self.case_dir / 'results',
            'report': self.case_dir / 'report',
            'work': self.case_dir / '_work',
        }
        for path in self._roots.values():
            path.mkdir(parents=True, exist_ok=True)
        self._calls: list[dict] = []
        self._plugin_modules: dict[tuple[str, Path], Any] = {}

    def _path(self, kind: str, *parts: str) -> Path:
        return safe_path(self._roots[kind], '/'.join(parts)) if parts else self._roots[kind]

    def processed_path(self, *parts: str) -> Path:
        return self._path('processed', *parts)

    def artifact_path(self, *parts: str) -> Path:
        return self._path('artifact', *parts)

    def result_path(self, *parts: str) -> Path:
        return self._path('result', *parts)

    def report_path(self, *parts: str) -> Path:
        return self._path('report', *parts)

    def work_path(self, *parts: str) -> Path:
        return self._path('work', *parts)

    def data_path(self, *parts: str) -> Path:
        local = self.case_dir / '.researchkit/local.json'
        config = read_json(local) if local.exists() else {}
        mapped = config.get('data_root')
        if mapped is not None:
            if not isinstance(mapped, str) or not mapped:
                raise KitError('local.json 的 data_root 必须是路径字符串')
            root = Path(mapped).expanduser()
            if not root.is_absolute():
                root = self.case_dir / root
        else:
            root = self.case_dir / 'data' / 'raw'
        return safe_path(root, '/'.join(parts)) if parts else root

    def open_dataset(self, dataset_id: str, path: str = 'data.bin', *, mode: str = 'rb', encoding: str = 'utf-8'):
        """Read a declared dataset without copying it into the project."""
        from .dataset_access import open_dataset
        return open_dataset(self.case_dir, dataset_id, path, mode=mode, encoding=encoding)

    def _managed(self, path: Path) -> Path:
        path = Path(path)
        if not path.is_absolute():
            path = self.case_dir / path
        if not any(inside(path, root) for root in self._roots.values()):
            raise KitError(f'写入路径不属于项目托管目录：{path}')
        return path

    def atomic_write_text(self, path: Path, value: str) -> None:
        atomic_text(self._managed(path), value)

    def atomic_write_json(self, path: Path, value: Any) -> None:
        write_json(self._managed(path), value)

    def atomic_write_yaml(self, path: Path, value: Any) -> None:
        write_yaml(self._managed(path), value)

    def call(self, alias: str, *, inputs: dict) -> dict:
        if alias not in self.document['plugins']:
            raise KitError(f'未声明算法：{alias}')
        cfg = self.document['plugins'][alias]
        folder = safe_path(self.case_dir, cfg['path'])
        meta = model.plugin(folder)
        model.ports(self.case_dir, meta['inputs'], inputs, f'{alias} 输入')
        values = {name: rule['default'] for name, rule in meta['parameters'].items()
                  if isinstance(rule, dict) and 'default' in rule}
        values.update(cfg.get('parameters', {}))
        model.validate_parameters(meta['parameters'], values, alias)
        key = (alias, folder.resolve())
        if key not in self._plugin_modules:
            self._plugin_modules[key] = load_plugin(folder, alias)
        module = self._plugin_modules[key]
        outputs = module.run(inputs, values, self)
        model.ports(self.case_dir, meta['outputs'], outputs, f'{alias} 输出')
        self._calls.append({'alias': alias, 'inputs': sorted(inputs), 'outputs': sorted(outputs)})
        return outputs

    @property
    def parameters(self) -> dict:
        return self.document['parameters']

    @property
    def call_records(self) -> list[dict]:
        return list(self._calls)

    @property
    def plugin_records(self) -> list[dict]:
        return [{'alias': alias, 'path': config['path']} for alias, config in self.document['plugins'].items()]


def _worker(root: Path, result_path: Path) -> int:
    try:
        document = model.project(root)
        model.verify_lock(root)
        model.environment(document)
        if document['mode'] == 'python':
            entry = safe_path(root, document.get('entry', 'case.py'))
            for search in (root / 'src', root / 'code', root):
                if search.is_dir():
                    sys.path.insert(0, str(search))
            context = Context(root, document)
            result = _load_module(entry, '_researchkit_case', [root, root / 'src', root / 'code']).run(context)
        else:
            command = document['command']
            executable = sys.executable if command[0] in {'python', 'python3'} else command[0]
            completed = subprocess.run([executable, *command[1:]], cwd=root, check=False)
            if completed.returncode:
                raise KitError(f'命令退出码 {completed.returncode}')
            result = {'exit_code': 0}
        model.verify_lock(root)
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise KitError(f'运行结果需要可序列化为 JSON：{exc}') from exc
        write_json(result_path, {'status': 'success', 'result': result, 'error': None})
        return 0
    except Exception as exc:
        traceback.print_exc()
        write_json(result_path, {'status': 'failed', 'result': None, 'error': str(exc)})
        return 1


def run_project(root: Path) -> dict:
    """Run user code in a separate interpreter, keeping a durable per-run record."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise KitError(f'项目目录不存在：{root}')
    reject_links(root)
    state = root / '.researchkit'
    state.mkdir(exist_ok=True)
    with (state / 'run.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KitError('此项目已有运行任务，请等待完成') from exc
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        run_dir = root / '_runs' / run_id
        run_dir.mkdir(parents=True)
        original_lock_hash = None
        outcome = {'status': 'failed', 'result': None, 'error': None}
        try:
            document = model.project(root)
            model.verify_lock(root)
            original_lock_hash = sha256(state / 'lock.json')
            model.environment(document)
            runtime = state / 'runtime'
            path = runtime if (runtime / 'researchkit').is_dir() else Path(__file__).resolve().parent.parent
            env = os.environ.copy()
            env['PYTHONPATH'] = os.pathsep.join([str(path), env.get('PYTHONPATH', '')])
            worker_record = run_dir / 'worker.json'
            command = [sys.executable, '-m', 'researchkit.engine', '--worker', str(root), str(worker_record)]
            with (run_dir / 'console.log').open('w', encoding='utf-8') as log:
                process = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
            if worker_record.exists():
                outcome = read_json(worker_record)
            else:
                outcome['error'] = f'工作进程退出码 {process.returncode}；请查看 console.log'
            if process.returncode and outcome.get('status') == 'success':
                outcome = {'status': 'failed', 'result': None, 'error': f'工作进程退出码 {process.returncode}'}
            if outcome.get('status') == 'success':
                try:
                    model.verify_lock(root)
                    if sha256(state / 'lock.json') != original_lock_hash:
                        raise KitError('运行期间固定依赖锁发生变化')
                except KitError as exc:
                    outcome = {'status': 'failed', 'result': None, 'error': str(exc)}
                    with (run_dir / 'console.log').open('a', encoding='utf-8') as log:
                        log.write(f'运行后依赖校验失败：{exc}\n')
        except Exception as exc:
            outcome = {'status': 'failed', 'result': None, 'error': str(exc)}
            atomic_text(run_dir / 'console.log', f'{type(exc).__name__}: {exc}\n')
        try:
            input_hash, stats = model.run_fingerprint(root)
            outputs = model.output_files(root)
            output_stats = model.output_stats(root)
            local = model.local_fingerprint(root)
        except Exception as exc:
            input_hash, stats, outputs, output_stats, local = '', {}, {}, {}, ''
            outcome = {'status': 'failed', 'result': None, 'error': f'运行后无法读取项目状态：{exc}'}
        record = {
            'run_id': run_id, 'status': outcome.get('status', 'failed'),
            'result': outcome.get('result'), 'error': outcome.get('error'),
            'input_hash': input_hash, 'input_stats': stats,
            'local_fingerprint': local, 'outputs': outputs, 'output_stats': output_stats,
        }
        write_json(run_dir / 'record.json', record)
        write_json(state / 'last-run.json', record)
        if record['status'] != 'success':
            raise KitError(f'运行失败（{run_id}）：{record["error"]}；详见 _runs/{run_id}/console.log')
        return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('root', type=Path)
    parser.add_argument('record', nargs='?', type=Path)
    args = parser.parse_args(argv)
    if args.worker:
        if args.record is None:
            parser.error('worker requires record path')
        return _worker(args.root.resolve(), args.record.resolve())
    try:
        print(json.dumps(run_project(args.root), ensure_ascii=False, default=str))
        return 0
    except KitError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
