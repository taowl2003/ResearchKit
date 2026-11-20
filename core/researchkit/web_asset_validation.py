"""Internal, append-only execution snapshots for asset-candidate validation.

Candidate extraction remains deliberately inert.  This module is the narrow
bridge to the existing isolated job runner: it builds an entirely new fixed
project from candidate bytes, installs the *current* trusted runtime and lock,
then queues that internal snapshot.  It never imports candidate code while
preparing a validation.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .files import KitError
from .model import check_type, validate_parameters
from .projects import install_runtime, seal_lock
from .web_composer import _json_value
from .web_assets import AssetService, _teacher
from .web_identity import Principal
from .web_jobs import Jobs
from .web_store import SubmissionStore, _json, _validate_data_references, _validate_files


_HASH_LENGTH = 64
_MAX_CASES = 32
_MAX_CASES_BYTES = 64 * 1024
_CASE_KEYS = {'inputs', 'parameters', 'expected_outputs'}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _finite_copy(value: object, *, label: str, depth: int = 0) -> object:
    """Copy exactly finite JSON values, retaining bool-versus-int distinction."""
    if depth > 32:
        raise KitError(f'{label} 嵌套过深')
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            try:
                value.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise KitError(f'{label} 必须是 UTF-8 JSON') from exc
        return value
    if type(value) is float:
        if value != value or value in {float('inf'), float('-inf')}:
            raise KitError(f'{label} 不能包含 NaN 或无穷数')
        return value
    if type(value) is list:
        return [_finite_copy(item, label=label, depth=depth + 1) for item in value]
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise KitError(f'{label} 的对象键必须是字符串')
            result[key] = _finite_copy(item, label=label, depth=depth + 1)
        return result
    raise KitError(f'{label} 必须是有限 JSON 值')


def _finite_json(value: object, *, label: str) -> object:
    """Apply the shared bounded/cycle-safe JSON validation before copying."""
    try:
        _json_value(value)
    except KitError as exc:
        raise KitError(f'{label} 必须是有限、受限的 JSON：{exc}') from exc
    return _finite_copy(value, label=label)


def _cases(value: object, metadata: object) -> list[dict[str, object]]:
    if type(value) is not list or not 1 <= len(value) <= _MAX_CASES:
        raise KitError('算法验证 cases 必须是 1 到 32 项的列表')
    checked = _finite_json(value, label='算法验证 cases')
    try:
        encoded = _json(checked).encode('utf-8')
    except (TypeError, ValueError) as exc:  # defensive; _finite_json is strict
        raise KitError('算法验证 cases 必须是有限 JSON') from exc
    if len(encoded) > _MAX_CASES_BYTES:
        raise KitError('算法验证 cases 总大小不能超过 64 KiB')
    if type(metadata) is not dict:
        raise KitError('候选算法元数据已损坏')
    inputs = metadata.get('inputs')
    outputs = metadata.get('outputs')
    rules = metadata.get('parameters')
    if type(inputs) is not dict or type(outputs) is not dict or type(rules) is not dict:
        raise KitError('候选算法端口或参数元数据已损坏')
    contracts = metadata.get('_asset_validation_contract_types')
    if type(contracts) is not dict:
        raise KitError('候选算法契约元数据已损坏')
    defaults = {name: rule['default'] for name, rule in rules.items()
                if type(rule) is dict and 'default' in rule}
    result: list[dict[str, object]] = []
    for index, raw in enumerate(checked):
        if type(raw) is not dict or set(raw) != _CASE_KEYS:
            raise KitError(f'算法验证第 {index + 1} 项必须只含 inputs、parameters、expected_outputs')
        case_inputs, parameters, expected = raw['inputs'], raw['parameters'], raw['expected_outputs']
        if type(case_inputs) is not dict or type(parameters) is not dict or type(expected) is not dict:
            raise KitError(f'算法验证第 {index + 1} 项的端口和参数必须是对象')
        if set(case_inputs) != set(inputs) or set(expected) != set(outputs):
            raise KitError(f'算法验证第 {index + 1} 项端口与候选算法声明不匹配')
        for port, ref in inputs.items():
            if not isinstance(ref, str) or ref not in contracts:
                raise KitError('候选算法契约元数据已损坏')
            if not check_type(contracts[ref], case_inputs[port]):
                raise KitError(f'算法验证第 {index + 1} 项端口 {port} 类型不符合 {ref}')
        for port, ref in outputs.items():
            if not isinstance(ref, str) or ref not in contracts:
                raise KitError('候选算法契约元数据已损坏')
            if not check_type(contracts[ref], expected[port]):
                raise KitError(f'算法验证第 {index + 1} 项端口 {port} 类型不符合 {ref}')
        values = dict(defaults)
        values.update(parameters)
        validate_parameters(rules, values, f'算法验证第 {index + 1} 项参数')
        result.append({'inputs': case_inputs, 'parameters': parameters, 'expected_outputs': expected})
    return result


def _strict_case_program(cases: list[dict[str, object]]) -> bytes:
    """Generate trusted control flow with JSON data, never caller supplied code."""
    literal = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    source = f'''"""Generated internal candidate validation; do not edit."""
import json

CASES = json.loads({literal!r})

def _same(expected, actual):
    if type(expected) is not type(actual):
        return False
    if type(expected) is dict:
        return set(expected) == set(actual) and all(_same(expected[key], actual[key]) for key in expected)
    if type(expected) is list:
        return len(expected) == len(actual) and all(_same(left, right) for left, right in zip(expected, actual))
    return expected == actual

def _json_value(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True

def run(context):
    report = {{'schema': 'researchkit.asset-validation/v1', 'passed': True, 'cases': []}}
    failures = 0
    for index, case in enumerate(CASES, 1):
        entry = {{'index': index, 'expected_outputs': case['expected_outputs']}}
        try:
            context.document['plugins']['candidate']['parameters'] = case['parameters']
            actual = context.call('candidate', inputs=case['inputs'])
            entry['actual_outputs'] = actual if _json_value(actual) else '<not finite JSON>'
            if _same(case['expected_outputs'], actual):
                entry['status'] = 'passed'
            else:
                entry['status'] = 'failed'
                entry['error'] = '输出的类型或值与 expected_outputs 不一致'
                failures += 1
        except Exception as exc:
            entry['status'] = 'failed'
            entry['error'] = f'{{type(exc).__name__}}: {{exc}}'
            failures += 1
        report['cases'].append(entry)
    report['passed'] = failures == 0
    context.atomic_write_json(context.result_path('asset-validation.json'), report)
    if failures:
        raise RuntimeError(f'{{failures}} 个算法验证用例失败；详见 results/asset-validation.json')
    return {{'asset_validation': report}}
'''
    return source.encode('utf-8')


def _yaml_mapping(data: bytes, label: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(data.decode('utf-8'))
        _finite_json(value, label=label)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise KitError(f'{label} 必须是安全的 UTF-8 YAML 映射') from exc
    if type(value) is not dict:
        raise KitError(f'{label} 必须是映射')
    return value


@dataclass(frozen=True)
class ValidationPlan:
    candidate_id: str
    candidate_sha256: str
    kind: str
    operation: str
    cases: list[dict[str, object]]
    cases_sha256: str
    files: dict[str, bytes]
    data_references: list[dict[str, object]]


class AssetValidationService:
    """Create and list append-only validation jobs without changing candidates."""

    def __init__(self, store: SubmissionStore, library_root: Path | str):
        self.store = store
        self.library_root = Path(library_root)
        self.assets = AssetService(store, self.library_root)
        self.jobs = Jobs(store)
        self._initialize()

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('''CREATE TABLE IF NOT EXISTS web_asset_validation_snapshots (
                    submission_id TEXT PRIMARY KEY REFERENCES submissions(id),
                    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
                    candidate_id TEXT NOT NULL, candidate_sha256 TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')),
                    created_at TEXT NOT NULL
                )''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_asset_candidate_validations (
                    validation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    candidate_sha256 TEXT NOT NULL, submission_id TEXT NOT NULL UNIQUE
                        REFERENCES submissions(id), content_sha256 TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE REFERENCES web_jobs(run_id),
                    kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')),
                    operation TEXT NOT NULL CHECK(operation IN ('check','run')),
                    cases_json TEXT NOT NULL, cases_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )''')
                con.execute('''CREATE INDEX IF NOT EXISTS web_asset_candidate_validations_list
                    ON web_asset_candidate_validations(candidate_id, created_at DESC, validation_id DESC)''')
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    @staticmethod
    def _candidate_hash(value: object) -> str:
        if not isinstance(value, str) or len(value) != _HASH_LENGTH or any(ch not in '0123456789abcdef' for ch in value):
            raise KitError('候选摘要格式无效')
        return value

    def _source_requires(self, candidate: dict[str, object]) -> dict[str, object]:
        submission_id, source_sha = candidate['submission_id'], candidate['source_sha256']
        if not isinstance(submission_id, str) or not isinstance(source_sha, str):
            raise KitError('候选来源记录已损坏')
        with self.store._connection() as con:
            source, source_files = self.store._source_files(con, submission_id)
        if source['content_sha256'] != source_sha:
            raise KitError('候选来源固定提交摘要不一致')
        project = _yaml_mapping(source_files.get('project.yaml', b''), '候选来源 project.yaml')
        requires = project.get('requires', {})
        if type(requires) is not dict:
            raise KitError('候选来源 requires 必须是映射')
        _finite_json(requires, label='候选来源 requires')
        return requires

    @staticmethod
    def _runtime_locked(files: dict[str, bytes]) -> dict[str, bytes]:
        """Install only trusted current runtime bytes; candidate code is not loaded."""
        packed, _ = _validate_files(files)
        with tempfile.TemporaryDirectory(prefix='rk-asset-validation-') as folder:
            root = Path(folder)
            for path, data in packed.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            install_runtime(root)
            seal_lock(root)
            result: dict[str, bytes] = {}
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    result[path.relative_to(root).as_posix()] = path.read_bytes()
        _validate_files(result)
        return result

    def _algorithm_files(self, candidate: dict[str, object], cases: list[dict[str, object]]) -> dict[str, bytes]:
        candidate_files = candidate.get('files')
        metadata = candidate.get('metadata')
        if type(candidate_files) is not dict or type(metadata) is not dict:
            raise KitError('算法候选内容已损坏')
        requires = self._source_requires(candidate)
        files: dict[str, bytes] = {}
        for path, data in candidate_files.items():
            if not isinstance(path, str) or not isinstance(data, bytes):
                raise KitError('算法候选文件已损坏')
            if path.startswith('plugin/'):
                files['.researchkit/plugins/candidate/' + path[len('plugin/'):]] = data
            elif path.startswith('contracts/'):
                files['.researchkit/contracts/' + path[len('contracts/'):]] = data
        if '.researchkit/plugins/candidate/plugin.py' not in files or '.researchkit/plugins/candidate/plugin.yaml' not in files:
            raise KitError('算法候选缺少插件入口')
        document = {
            'schema': 'researchkit.project/v1', 'name': 'asset-candidate-validation',
            'mode': 'python', 'entry': 'case.py',
            # model.project validates the starting configuration before the
            # generated case can switch parameters for later cases.
            'plugins': {'candidate': {'path': '.researchkit/plugins/candidate',
                                      'parameters': cases[0]['parameters']}},
            'parameters': {}, 'data': {}, 'requires': requires,
            'origin': {'kind': 'web-asset-validation/v1', 'candidate_id': candidate['candidate_id'],
                       'candidate_sha256': candidate['candidate_sha256']},
        }
        files['project.yaml'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode('utf-8')
        files['case.py'] = _strict_case_program(cases)
        return self._runtime_locked(files)

    def _template_files(self, candidate: dict[str, object]) -> dict[str, bytes]:
        candidate_files = candidate.get('files')
        if type(candidate_files) is not dict:
            raise KitError('模板候选内容已损坏')
        files = {path: data for path, data in candidate_files.items()
                 if isinstance(path, str) and isinstance(data, bytes)}
        if len(files) != len(candidate_files):
            raise KitError('模板候选文件已损坏')
        # Asset extraction has already removed old runtime/lock/output bytes;
        # this always puts a fresh trusted runtime and lock in their place.
        return self._runtime_locked(files)

    def prepare(self, principal: Principal, candidate_id: object, expected_sha256: object,
                operation: object, cases: object = None) -> ValidationPlan:
        _teacher(principal)
        if not isinstance(candidate_id, str):
            raise KitError('候选 ID 无效')
        expected = self._candidate_hash(expected_sha256)
        if not isinstance(operation, str) or operation not in {'check', 'run'}:
            raise KitError('候选验证 operation 必须是 check 或 run')
        candidate = self.assets.get_candidate(principal, candidate_id)
        if candidate['candidate_sha256'] != expected:
            raise KitError('候选内容已变化或验证摘要不匹配')
        kind = candidate['kind']
        if kind == 'algorithm':
            if operation != 'run':
                raise KitError('算法候选首版验证仅支持 run')
            metadata = dict(candidate['metadata']) if type(candidate['metadata']) is dict else None
            contracts = candidate.get('static_validation', {}).get('contracts') if type(candidate.get('static_validation')) is dict else None
            if metadata is None or type(contracts) is not list:
                raise KitError('算法候选元数据已损坏')
            contract_types = {item.get('id'): item.get('type') for item in contracts if type(item) is dict}
            if len(contract_types) != len(contracts) or not all(isinstance(ref, str) and isinstance(kind, str)
                                                                for ref, kind in contract_types.items()):
                raise KitError('算法候选契约元数据已损坏')
            metadata['_asset_validation_contract_types'] = contract_types
            checked_cases = _cases(cases, metadata)
            files = self._algorithm_files(candidate, checked_cases)
        elif kind == 'template':
            if cases is not None:
                raise KitError('模板候选验证不接受 cases')
            checked_cases = []
            files = self._template_files(candidate)
        else:  # pragma: no cover - candidate schema has a database CHECK
            raise KitError('候选类型无效')
        references = candidate.get('data_references')
        references = _validate_data_references(references)
        return ValidationPlan(candidate_id=candidate['candidate_id'], candidate_sha256=expected,
                              kind=kind, operation=operation, cases=checked_cases,
                              cases_sha256=_hash(checked_cases), files=files,
                              data_references=references)

    def _insert_snapshot(self, con: Any, principal: Principal, plan: ValidationPlan) -> tuple[str, str]:
        uid, username = _teacher(principal)
        files, file_manifest = _validate_files(plan.files)
        references = _validate_data_references(plan.data_references)
        manifest = {
            'schema': 'researchkit.web-submission/v1',
            'snapshot': 'internal asset-candidate validation snapshot',
            'files': file_manifest,
            'data_references': {'status': 'unverified', 'references': references,
                                'snapshot': 'external data is referenced only and was not traversed or verified'},
        }
        content_sha256 = hashlib.sha256(_json(manifest).encode('utf-8')).hexdigest()
        project_id, submission_id, created_at = str(uuid.uuid4()), str(uuid.uuid4()), _now()
        con.execute('INSERT INTO projects(id, owner_uid, owner_username, source, name, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (project_id, uid, username, 'self-created', '内部候选验证', created_at))
        con.execute('''INSERT INTO submissions(id, project_id, sequence, version_major, version_minor, kind,
            parent_submission_id, content_sha256, manifest_json, created_at)
            VALUES (?, ?, 1, 1, 0, 'major', NULL, ?, ?, ?)''',
                    (submission_id, project_id, content_sha256, _json(manifest), created_at))
        con.executemany('INSERT INTO submission_files(submission_id, path, sha256, size, content) VALUES (?, ?, ?, ?, ?)',
                        [(submission_id, item['path'], item['sha256'], item['size'], files[item['path']])
                         for item in file_manifest])
        con.execute('''INSERT INTO web_asset_validation_snapshots(
            submission_id, project_id, candidate_id, candidate_sha256, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?)''',
                    (submission_id, project_id, plan.candidate_id, plan.candidate_sha256, plan.kind, created_at))
        return submission_id, content_sha256

    @staticmethod
    def _record(row: Any, job: dict[str, object]) -> dict[str, object]:
        try:
            cases = json.loads(row['cases_json'])
        except (TypeError, ValueError) as exc:
            raise KitError('候选验证记录已损坏') from exc
        if _hash(cases) != row['cases_sha256']:
            raise KitError('候选验证用例摘要不匹配')
        if (job.get('run_id') != row['run_id'] or job.get('submission_id') != row['submission_id'] or
                job.get('content_sha256') != row['content_sha256'] or job.get('operation') != row['operation']):
            raise KitError('候选验证作业关联已损坏')
        return {'candidate_id': row['candidate_id'], 'candidate_sha256': row['candidate_sha256'],
                'submission_id': row['submission_id'], 'content_sha256': row['content_sha256'],
                'run_id': row['run_id'], 'kind': row['kind'], 'operation': row['operation'],
                'cases': cases, 'cases_sha256': row['cases_sha256'], 'created_at': row['created_at'],
                'job': job}

    def create(self, principal: Principal, plan: ValidationPlan) -> dict[str, object]:
        _teacher(principal)
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                candidate = con.execute('''SELECT candidate_sha256, kind FROM web_asset_candidates
                    WHERE candidate_id = ?''', (plan.candidate_id,)).fetchone()
                if (candidate is None or candidate['candidate_sha256'] != plan.candidate_sha256 or
                        candidate['kind'] != plan.kind):
                    raise KitError('候选内容已变化或验证摘要不匹配')
                submission_id, content_sha256 = self._insert_snapshot(con, principal, plan)
                job = self.jobs.queue_internal(con, principal, submission_id, content_sha256, plan.operation)
                created_at, validation_id = _now(), str(uuid.uuid4())
                con.execute('''INSERT INTO web_asset_candidate_validations(
                    validation_id,candidate_id,candidate_sha256,submission_id,content_sha256,run_id,
                    kind,operation,cases_json,cases_sha256,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                            (validation_id, plan.candidate_id, plan.candidate_sha256, submission_id, content_sha256,
                             job['run_id'], plan.kind, plan.operation, _json(plan.cases), plan.cases_sha256, created_at))
                row = con.execute('SELECT * FROM web_asset_candidate_validations WHERE validation_id = ?',
                                  (validation_id,)).fetchone()
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        if row is None:  # pragma: no cover
            raise KitError('候选验证记录无法创建')
        return self._record(row, job)

    def list(self, principal: Principal, candidate_id: object) -> list[dict[str, object]]:
        _teacher(principal)
        if not isinstance(candidate_id, str):
            raise KitError('候选 ID 无效')
        # Candidate visibility is both the authorization gate and a clean 404
        # equivalent for unknown IDs.
        candidate = self.assets.get_candidate(principal, candidate_id)
        with self.store._connection() as con:
            rows = con.execute('''SELECT * FROM web_asset_candidate_validations
                WHERE candidate_id = ? ORDER BY created_at DESC, validation_id DESC LIMIT 200''',
                               (candidate_id,)).fetchall()
            records = []
            for row in rows:
                if (row['candidate_sha256'] != candidate['candidate_sha256'] or
                        row['kind'] != candidate['kind']):
                    raise KitError('候选验证候选关联已损坏')
                snapshot = con.execute('''SELECT candidate_id, candidate_sha256, kind FROM web_asset_validation_snapshots
                    WHERE submission_id = ?''', (row['submission_id'],)).fetchone()
                if (snapshot is None or snapshot['candidate_id'] != row['candidate_id'] or
                        snapshot['candidate_sha256'] != row['candidate_sha256'] or snapshot['kind'] != row['kind']):
                    raise KitError('候选验证内部快照记录已损坏')
                job_row = con.execute('SELECT * FROM web_jobs WHERE run_id = ?', (row['run_id'],)).fetchone()
                if job_row is None:
                    raise KitError('候选验证作业记录已损坏')
                records.append(self._record(row, Jobs._job(job_row)))
        return records
