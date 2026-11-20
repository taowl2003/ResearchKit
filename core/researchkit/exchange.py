"""Portable project bundles, untrusted submissions, and local project snapshots.

None of these operations imports or runs code from a project.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import model
from .files import KitError, copy_tree, read_json, safe_path, snapshot, staging, write_json

SHARE_EXCLUDE = {
    '.researchkit/local.json', '.researchkit/last-run.json',
    '.researchkit/review.json', '.researchkit/review.md',
    '.researchkit/bundle.json', '.researchkit/run.lock', '_submission.json',
}
SUBMIT_EXCLUDE = {
    '.researchkit/local.json', '.researchkit/review.json',
    '.researchkit/review.md', '.researchkit/run.lock', '_submission.json',
}
MUTABLE = {
    '.researchkit/local.json', '.researchkit/last-run.json',
    '.researchkit/review.json', '.researchkit/review.md',
    '.researchkit/run.lock', '_submission.json',
}
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_MEMBER = 512 * 1024 * 1024
MAX_MEMBERS = 10000


def _home() -> Path:
    from .projects import home
    return home()


def _slug(value: str) -> str:
    result = re.sub(r'[^a-zA-Z0-9._-]+', '-', value).strip('.-')[:60]
    return result or 'project'


def _unique(base: Path, stem: str, suffix: str = '') -> Path:
    return base / f'{stem}-{uuid.uuid4().hex[:12]}{suffix}'


def _outside_project(project: Path, target: Path) -> None:
    if target == project or target.is_relative_to(project) or project.is_relative_to(target):
        raise KitError(f'输出位置不能与源项目重叠：{target}')


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _protected(files: dict[str, str]) -> dict[str, str]:
    return {p: h for p, h in files.items()
            if p == 'task' or p.startswith('task/') or
            (p.startswith('.researchkit/') and p not in MUTABLE)}


def _normal_files(files: dict[str, str]) -> dict[str, str]:
    return {p: h for p, h in files.items() if p not in MUTABLE}


def _bundle_id(root: Path) -> str:
    doc = read_json(root / '.researchkit/bundle.json')
    value = doc.get('id')
    if doc.get('schema') != 'researchkit.bundle/v1' or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise KitError('共享包身份文件无效')
    return value


def _baseline(bundle_id: str) -> dict:
    path = _home() / 'library/bundles' / bundle_id / 'baseline.json'
    if not path.is_file():
        raise KitError(f'未知共享包 {bundle_id}；请在持有教师基线的 ResearchKit 中收取')
    doc = read_json(path)
    files = doc.get('files')
    if doc.get('schema') != 'researchkit.baseline/v1' or doc.get('id') != bundle_id or not isinstance(files, dict) or not files or not all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for k, v in files.items()):
        raise KitError('教师基线清单损坏，已拒绝收取')
    return doc


def _compare(files: dict[str, str], baseline: dict) -> dict:
    expected = _normal_files(baseline['files'])
    actual = _normal_files(files)
    if _protected(expected) != _protected(actual):
        changed = sorted(p for p in set(_protected(expected)) | set(_protected(actual))
                         if expected.get(p) != actual.get(p))
        raise KitError('受保护的任务或固定依赖发生变化，已拒绝：' + ', '.join(changed[:8]))
    return {
        'changed': sorted(p for p in expected.keys() & actual.keys() if expected[p] != actual[p]),
        'added': sorted(actual.keys() - expected.keys()),
        'deleted': sorted(expected.keys() - actual.keys()),
    }


def share_project(project: Path, output: Path | None = None) -> dict:
    """Export a self contained directory and retain an independent teacher baseline."""
    project = Path(project).resolve()
    doc = model.project(project)
    model.verify_lock(project)
    home = _home()
    target = Path(output).resolve() if output is not None else _unique(home / 'exports', _slug(doc['name']))
    bundle_id = uuid.uuid4().hex
    baseline_target = home / 'library/bundles' / bundle_id
    _outside_project(project, target)
    _outside_project(project, baseline_target)
    if target.exists() or baseline_target.exists():
        raise KitError(f'目标已存在：{target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    baseline_target.parent.mkdir(parents=True, exist_ok=True)
    temp_export = Path(tempfile.mkdtemp(prefix='.rk-share-', dir=target.parent))
    temp_baseline = Path(tempfile.mkdtemp(prefix='.rk-base-', dir=baseline_target.parent))
    issued = False
    try:
        copy_tree(project, temp_export, exclude=SHARE_EXCLUDE)
        write_json(temp_export / '.researchkit/bundle.json', {
            'schema': 'researchkit.bundle/v1', 'id': bundle_id, 'project': doc['name']})
        files = snapshot(temp_export)
        write_json(temp_baseline / 'baseline.json', {
            'schema': 'researchkit.baseline/v1', 'id': bundle_id,
            'project': doc['name'], 'created_at': _stamp(), 'files': files})
        # Publish only once both complete; rollback either rename on failure.
        if target.exists() or baseline_target.exists():
            raise KitError('输出或教师基线目标已存在')
        os.rename(temp_export, target)
        issued = True
        os.rename(temp_baseline, baseline_target)
        issued = False
    finally:
        if issued and target.exists():
            shutil.rmtree(target)
        for temp in (temp_export, temp_baseline):
            if temp.exists():
                shutil.rmtree(temp)
    return {'path': str(target), 'bundle_id': bundle_id, 'baseline': str(baseline_target / 'baseline.json')}


def submit_project(project: Path, output: Path | None = None) -> dict:
    """Package a student project and its *claimed* run state without running it."""
    project = Path(project).resolve()
    bundle_id = _bundle_id(project)
    model.project(project)
    model.verify_lock(project)
    freshness = model.freshness(project)
    target = Path(output).resolve() if output is not None else _unique(project.parent, _slug(project.name) + '-submission', '.zip')
    _outside_project(project, target)
    if target.exists():
        raise KitError(f'目标已存在：{target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.rk-submit-', dir=target.parent) as temp_name:
        frozen = Path(temp_name) / 'project'
        copy_tree(project, frozen, exclude=SUBMIT_EXCLUDE)
        files = snapshot(frozen)
        write_json(frozen / '_submission.json', {
            'schema': 'researchkit.submission/v1', 'bundle_id': bundle_id,
            'created_at': _stamp(), 'files': files, 'claimed_freshness': freshness,
            'student_code_executed_by_submission': False})
        temporary_archive = Path(temp_name) / 'submission.zip'
        with zipfile.ZipFile(temporary_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for rel in sorted(snapshot(frozen)):
                archive.write(frozen / rel, arcname=rel)
        if temporary_archive.stat().st_size > MAX_ARCHIVE:
            raise KitError('提交包超过大小上限')
        try:
            os.link(temporary_archive, target)
        except FileExistsError as exc:
            raise KitError(f'目标已存在：{target}') from exc
    return {'path': str(target), 'bundle_id': bundle_id, 'freshness': freshness}


def _archive_names(archive: zipfile.ZipFile, *, max_members: int = MAX_MEMBERS,
                   max_member: int = MAX_MEMBER, max_expanded: int = MAX_EXPANDED) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > max_members:
        raise KitError('提交包文件数超过上限')
    names: set[str] = set()
    regular: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if not name or '\\' in name or '\x00' in name or name.startswith('/') or name.startswith('//'):
            raise KitError(f'提交包含非法路径：{name!r}')
        parts = name.rstrip('/').split('/')
        if any(p in ('', '.', '..') for p in parts) or ':' in parts[0]:
            raise KitError(f'提交包含越界路径：{name!r}')
        key = '/'.join(parts)
        if key in names:
            raise KitError(f'提交包含重复条目：{key}')
        names.add(key)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (info.is_dir() and kind == stat.S_IFREG) or (not info.is_dir() and kind == stat.S_IFDIR):
            raise KitError(f'提交包含链接或特殊文件：{key}')
        if info.flag_bits & 1:
            raise KitError('不支持加密提交包')
        if not info.is_dir():
            regular.add(key)
            if info.file_size > max_member:
                raise KitError(f'提交包单个文件超过上限：{key}')
            total += info.file_size
            if total > max_expanded:
                raise KitError('提交包解压后超过大小上限')
    for key in names:
        parts = key.split('/')
        if any('/'.join(parts[:n]) in regular for n in range(1, len(parts))):
            raise KitError(f'提交包路径与文件冲突：{key}')
    return infos


def _extract(archive_path: Path, destination: Path) -> None:
    if not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE:
        raise KitError('提交包不存在或超过大小上限')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = _archive_names(archive)
            destination.mkdir(parents=True, exist_ok=True)
            for info in infos:
                name = info.filename.rstrip('/')
                path = safe_path(destination, name)
                if info.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, path.open('xb') as sink:
                    count = 0
                    while block := source.read(1024 * 1024):
                        count += len(block)
                        if count > MAX_MEMBER:
                            raise KitError(f'提交包单个文件超过上限：{name}')
                        sink.write(block)
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise KitError(f'提交包损坏或无法解压：{exc}') from exc


def receive_submission(archive: Path, output: Path | None = None) -> dict:
    """Verify a zip against the teacher's immutable baseline, then report static changes."""
    archive = Path(archive).resolve()
    target = Path(output).resolve() if output is not None else _unique(_home() / 'inbox', 'received')
    with staging(target) as staged:
        root = staged / 'project'
        _extract(archive, root)
        manifest = read_json(root / '_submission.json')
        bundle_id = _bundle_id(root)
        files = snapshot(root, exclude={'_submission.json'})
        if manifest.get('schema') != 'researchkit.submission/v1' or manifest.get('bundle_id') != bundle_id or manifest.get('files') != files:
            raise KitError('提交清单损坏、身份不符或文件哈希不匹配，已拒绝')
        baseline = _baseline(bundle_id)
        changes = _compare(files, baseline)
        # The exact launcher was checked against the teacher baseline above.
        # Zip extraction does not preserve mode bits.
        (root / 'task').chmod(0o755)
        # Parsing metadata is safe; project code is never imported or run here.
        model.project(root)
        model.verify_lock(root)
        observed = model.freshness(root)
        claimed = manifest.get('claimed_freshness')
        if not isinstance(claimed, dict):
            raise KitError('提交摘要中的运行状态无效，已拒绝')
        review = {
            'schema': 'researchkit.review/v1', 'status': 'accepted', 'bundle_id': bundle_id,
            'changed': changes['changed'], 'added': changes['added'], 'deleted': changes['deleted'],
            'claimed_freshness': claimed, 'observed_freshness': observed,
            'student_code_executed': False, 'teacher_verified_results': False,
        }
        write_json(staged / 'review.json', review)
        lines = [f'# 静态收取报告：{bundle_id}', '',
                 '状态：已接收；受保护的任务与固定依赖和教师基线一致。',
                 '学生代码执行：否。教师结果验证：否。',
                 f"学生声明的运行状态：{claimed.get('status', '未知')}。",
                 f"根据包内文件静态重算的状态：{observed.get('status', '未知')}。",
                 '运行记录和状态来自学生包，仅供审阅，不能视为教师验证。', '']
        for title, key in [('修改', 'changed'), ('新增', 'added'), ('删除', 'deleted')]:
            lines.extend([f'## {title}文件', ''])
            lines.extend([f'- `{name}`' for name in changes[key]] or ['- 无'])
            lines.append('')
        (staged / 'review.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'path': str(target), 'report': str(target / 'review.md'), 'status': 'accepted',
            'bundle_id': bundle_id, **changes, 'claimed_freshness': claimed,
            'observed_freshness': observed, 'student_code_executed': False}


def save_project(project: Path, name: str | None = None) -> dict:
    """Keep a complete local project snapshot, including editable algorithms and run evidence."""
    project = Path(project).resolve()
    doc = model.project(project)
    model.verify_lock(project)
    bundle_file = project / '.researchkit/bundle.json'
    if bundle_file.exists():
        bundle_id = _bundle_id(project)
        _compare(snapshot(project), _baseline(bundle_id))
    from .files import identifier
    selected = identifier(name if name is not None else _slug(doc['name']))
    target = _home() / 'library/projects' / selected
    with staging(target) as staged:
        # The normal project copy preserves local algorithms, results, report,
        # and last-run evidence. `_runs` is scratch data and is not required by freshness.
        copy_tree(project, staged, exclude={'_submission.json', '.researchkit/review.json', '.researchkit/review.md', '.researchkit/local.json', '.researchkit/run.lock'})
        write_json(staged / '.researchkit/saved.json', {
            'schema': 'researchkit.saved/v1', 'name': selected, 'saved_at': _stamp()})
    return {'path': str(target), 'name': selected}
