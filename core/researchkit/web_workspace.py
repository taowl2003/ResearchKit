"""Per-user, same-directory workspace operations for the local Web service.

This module deliberately never accepts a username or a workspace path from an
HTTP request.  A caller supplies the already authenticated principal and its
single policy-selected workspace.  The small state file maps broker-issued
project UUIDs to directories below that workspace; it is a convenience index,
not an authority for ownership.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import yaml

from .files import KitError, atomic_text, copy_tree, safe_path, write_yaml
from .projects import install_runtime, seal_lock
from .web_identity import Principal
from .web_store import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, _validate_data_references


MAX_EDIT_BYTES = 1024 * 1024
_STATE_NAME = '.researchkit-web-state.json'
_PROJECTS_DIRECTORY = 'projects'
_TRANSIENT = {
    '.researchkit/local.json', '.researchkit/last-run.json', '.researchkit/run.lock',
    '.researchkit/review.json', '.researchkit/review.md', '_submission.json',
    '.researchkit/derived-imports.json',
}
_SKIP_DIRECTORIES = {'_runs', '__pycache__', '.pytest_cache', '.git', '.venv', 'venv', 'caches'}
_PROTECTED_PREFIXES = ('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/')
_PROTECTED_FILES = {'.researchkit/lock.json', 'task'}
_DATA_REFERENCES_NAME = 'DATA-REFERENCES.json'
_SOURCE_SUFFIXES = {'.py', '.pyc', '.sh', '.bash', '.m', '.r', '.jl', '.ipynb'}
_FOUNDATION_CONTRACTS = {
    'mapping@1.0.0': (
        b'schema: hydro.contract/v1\n'
        b'id: mapping@1.0.0\n'
        b'type: mapping\n'
    ),
}
_RECOVERY_PROJECT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_STAGING_DIR_NAME = re.compile(r'^\.purge-staging-[A-Za-z0-9._-]{1,128}\Z')
_RECOVERY_PROJECT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_STAGING_DIR_NAME = re.compile(r'^\.purge-staging-[A-Za-z0-9._-]{1,128}\Z')


class StoreClient(Protocol):
    def request(self, method: str, **params: object) -> dict[str, object] | list[dict[str, object]]: ...


def _error(message: str) -> KitError:
    return KitError(message)


def _relative(value: object, *, label: str = '路径') -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise _error(f'{label}必须是非空项目内相对路径')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or any(part in ('', '.') for part in path.parts):
        raise _error(f'{label}越界')
    return path.as_posix()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_component(value: str) -> str:
    cleaned = ''.join(char if char.isalnum() or char in '._-' else '-' for char in value).strip('.-')
    return (cleaned or 'project')[:48]


def _secure_directory(path: Path, *, create: bool = False) -> Path:
    """Open every component without following links; optionally create only its leaf."""
    if not path.is_absolute() or '..' in path.parts:
        raise _error('工作区路径不安全')
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts[1:]) - 1
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not (create and final):
                    raise _error(f'工作区目录不存在：{path}')
                os.mkdir(part, 0o700, dir_fd=descriptor)
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _error('工作区包含非目录或链接')
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise _error('工作区路径不能由其他用户写入')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise _error('工作区必须由当前用户拥有且权限为 0700')
    finally:
        os.close(descriptor)
    return path

class Workspace:
    """Safe operations over one principal's own IDE-visible project directory."""

    def __init__(self, principal: Principal, root: Path | str, store: StoreClient,
                 *, library_root: Path | str | None = None):
        self.principal = principal
        self.root = _secure_directory(Path(root), create=True)
        self.store = store
        self.library_root = Path(library_root) if library_root is not None else Path(__file__).resolve().parents[2] / 'library'
        self.projects_root = _secure_directory(self.root / _PROJECTS_DIRECTORY, create=True)
        self._state_path = self.root / _STATE_NAME
        self._state_lock_path = self.root / '.researchkit-web-state.lock'
        self._archive_path = self.root / '.researchkit-web-archive.json'
        self._thread_lock = threading.RLock()
        self._lock_depth = 0

    @contextmanager
    def _state_lock(self):
        tlock = getattr(self, '_thread_lock', None)
        if tlock is None:
            tlock = threading.RLock()
            self._thread_lock = tlock
            self._lock_depth = 0
        with tlock:
            if getattr(self, '_lock_depth', 0) > 0:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            descriptor = os.open(self._state_lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                    raise _error('项目索引锁不安全')
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _load_state(self) -> dict[str, dict[str, str]]:
        if not self._state_path.exists():
            return {}
        info = os.lstat(self._state_path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            raise _error('项目索引不是当前用户的普通文件')
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise _error('项目索引权限必须为 0600')
        try:
            payload = json.loads(self._state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise _error('项目索引已损坏') from exc
        if type(payload) is not dict or payload.get('schema') != 'researchkit.web-workspace/v1' or type(payload.get('projects')) is not dict:
            raise _error('项目索引格式无效')
        result: dict[str, dict[str, str]] = {}
        for project_id, entry in payload['projects'].items():
            if not isinstance(project_id, str) or type(entry) is not dict:
                raise _error('项目索引格式无效')
            relative = entry.get('path')
            name = entry.get('name')
            if not isinstance(relative, str) or not isinstance(name, str):
                raise _error('项目索引格式无效')
            result[project_id] = {'path': _relative(relative), 'name': name}
        return result

    def _save_state(self, projects: dict[str, dict[str, str]]) -> None:
        payload = {'schema': 'researchkit.web-workspace/v1', 'projects': projects}
        atomic_text(self._state_path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
        os.chmod(self._state_path, 0o600)

    def _project_path(self, project_id: str) -> Path:
        entry = self._load_state().get(project_id)
        if entry is None:
            raise _error('项目不在当前工作区')
        path = safe_path(self.root, entry['path'])
        try:
            path.relative_to(self.projects_root)
        except ValueError as exc:
            raise _error('项目索引路径越界') from exc
        _secure_directory(path, create=False)
        return path

    def _broker_project(self, project_id: str) -> dict[str, object]:
        try:
            result = self.store.request('get_project', project_id=project_id)
        except KitError as exc:
            if '不存在' in str(exc) or '已清理' in str(exc):
                entry = self._load_state().get(project_id)
                if entry is not None:
                    return {
                        'project_id': project_id,
                        'name': str(entry.get('name') or project_id),
                        'source': 'local',
                        'central_purged': True,
                    }
            raise
        if not isinstance(result, dict):
            raise _error('提交服务返回的项目记录无效')
        return result

    def _resolve_recovery_staging(self, data: dict[str, object]) -> Path | None:
        """Resolve and strictly confine a recovery staging directory inside `projects_root`."""
        projects_real = self.projects_root.resolve()
        sname = data.get('staging_name')
        spath = data.get('staging_path')
        if sname is not None:
            if not isinstance(sname, str) or not _STAGING_DIR_NAME.fullmatch(sname):
                return None
            cand = self.projects_root / sname
        elif isinstance(spath, str) and spath:
            if '\x00' in spath:
                return None
            raw = Path(spath)
            if not raw.is_absolute() or '..' in raw.parts:
                return None
            if not _STAGING_DIR_NAME.fullmatch(raw.name):
                return None
            try:
                rel = raw.relative_to(self.projects_root)
            except ValueError:
                try:
                    rel = raw.relative_to(projects_real)
                except ValueError:
                    return None
            if len(rel.parts) != 1:
                return None
            cand = self.projects_root / raw.name
        else:
            return None
        if os.path.lexists(cand):
            st = os.lstat(cand)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                return None
            if cand.resolve().parent != projects_real:
                return None
        return cand

    def _resolve_recovery_orig(self, pid: str, data: dict[str, object], state: dict[str, dict[str, str]]) -> Path | None:
        """Resolve and strictly confine a recovery target project path inside `projects_root`."""
        projects_real = self.projects_root.resolve()
        root_real = self.root.resolve()
        orel = data.get('orig_rel')
        opath = data.get('orig_path')
        if orel is not None:
            try:
                rel_s = _relative(orel, label='恢复目录')
                cand = safe_path(self.root, rel_s)
                rel = cand.relative_to(self.projects_root)
            except Exception:
                return None
        elif isinstance(opath, str) and opath:
            if '\x00' in opath:
                return None
            raw = Path(opath)
            if not raw.is_absolute() or '..' in raw.parts:
                return None
            try:
                rel = raw.relative_to(self.projects_root)
            except ValueError:
                try:
                    rel = raw.relative_to(projects_real)
                except ValueError:
                    return None
            if not rel.parts or any(part.startswith('.purge-') or part in ('', '.') for part in rel.parts):
                return None
            cand = self.projects_root.joinpath(*rel.parts)
        else:
            return None

        if len(rel.parts) < 1:
            return None
        cur = self.projects_root
        for part in rel.parts[:-1]:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                return None
        try:
            cur_resolved = cur.resolve()
            if cur_resolved != projects_real:
                cur_resolved.relative_to(projects_real)
        except ValueError:
            return None
        if os.path.lexists(cand):
            st = os.lstat(cand)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                return None
            try:
                cand.resolve().relative_to(root_real)
                cand.resolve().relative_to(projects_real)
            except ValueError:
                return None
        expected_entry = state.get(pid)
        if expected_entry is not None:
            if rel.as_posix() != Path(expected_entry['path']).relative_to(_PROJECTS_DIRECTORY).as_posix():
                return None
        return cand

    def recover_pending_purges(self) -> list[dict[str, object]]:
        """Finish or restore any interrupted workspace-side purge after crash."""
        recovered: list[dict[str, object]] = []
        if not hasattr(self, 'projects_root') or not self.projects_root.is_dir():
            return recovered
        with self._state_lock():
            journals = sorted(self.projects_root.glob('.purge-recovery-*.json'))
            if not journals:
                return recovered
            state = self._load_state()
            for jpath in journals:
                try:
                    jst = os.lstat(jpath)
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISLNK(jst.st_mode)
                    or not stat.S_ISREG(jst.st_mode)
                    or jst.st_nlink != 1
                    or jst.st_uid != os.geteuid()
                    or jpath.parent != self.projects_root
                ):
                    recovered.append({'project_id': '', 'action': 'rejected_unsafe_journal', 'journal': jpath.name})
                    continue
                try:
                    data = json.loads(jpath.read_text(encoding='utf-8'))
                except Exception:
                    continue
                if not isinstance(data, dict) or not isinstance(data.get('project_id'), str):
                    recovered.append({'project_id': '', 'action': 'rejected_unsafe_journal', 'journal': jpath.name})
                    continue
                pid = str(data['project_id'])
                if not _RECOVERY_PROJECT_ID.fullmatch(pid) or jpath.name != f'.purge-recovery-{pid}.json':
                    recovered.append({'project_id': pid, 'action': 'rejected_unsafe_journal', 'journal': jpath.name})
                    continue
                orig_path = self._resolve_recovery_orig(pid, data, state)
                staging_path = self._resolve_recovery_staging(data)
                if orig_path is None or staging_path is None:
                    recovered.append({'project_id': pid, 'action': 'rejected_unsafe_journal', 'journal': jpath.name})
                    continue

                central_exists = True
                try:
                    self.store.request('get_project', project_id=pid)
                except KitError as exc:
                    if '不存在' in str(exc) or '已清理' in str(exc):
                        central_exists = False
                    else:
                        raise

                if central_exists:
                    if os.path.lexists(staging_path) and not os.path.lexists(orig_path):
                        os.rename(staging_path, orig_path)
                    try:
                        jpath.unlink()
                    except OSError:
                        pass
                    recovered.append({'project_id': pid, 'action': 'restored_workspace'})
                    continue

                popped = state.pop(pid, None)
                archived = self._load_archived_projects()
                archive_changed = pid in archived
                if archive_changed:
                    archived.discard(pid)
                if popped is not None:
                    self._save_state(state)
                if archive_changed:
                    self._save_archived_projects(archived)

                cached_deleted = data.get('deleted_extra') if isinstance(data.get('deleted_extra'), list) else []
                deleted_extra: list[dict[str, object]] = [d for d in cached_deleted if isinstance(d, dict)]
                failed_extra: list[dict[str, object]] = []
                if os.path.lexists(staging_path):
                    rel = popped['path'] if isinstance(popped, dict) else staging_path.name
                    try:
                        shutil.rmtree(staging_path)
                        item = {'kind': 'workspace_copy', 'ref': rel, 'name': f'工作区本地目录 {rel}'}
                        if item not in deleted_extra:
                            deleted_extra.append(item)
                        if isinstance(popped, dict) or data.get('deleted_extra') != deleted_extra:
                            updated_journal = dict(data)
                            updated_journal['deleted_extra'] = deleted_extra
                            atomic_text(jpath, json.dumps(updated_journal, ensure_ascii=False) + '\n')
                    except OSError as exc:
                        failed_extra.append({'kind': 'workspace_copy', 'ref': rel, 'error': str(exc)})

                audit_synced = (not deleted_extra and not failed_extra) or (self.principal.role != 'teacher')
                if (deleted_extra or failed_extra) and self.principal.role == 'teacher':
                    try:
                        audits = self.store.request('project_purge_audit', limit=100)
                        target_aid = data.get('audit_id') if isinstance(data.get('audit_id'), str) else None
                        match_audit = None
                        if isinstance(audits, list):
                            if target_aid:
                                match_audit = next((a for a in audits if isinstance(a, dict) and a.get('audit_id') == target_aid), None)
                            if match_audit is None:
                                match_audit = next((a for a in audits if isinstance(a, dict) and a.get('project_id') == pid), None)
                        if match_audit is not None:
                            self.store.request(
                                'project_purge_update_audit',
                                audit_id=match_audit['audit_id'],
                                deleted_extra=deleted_extra or None,
                                failed_extra=failed_extra or None,
                            )
                            audit_synced = True
                    except Exception:
                        audit_synced = False

                if audit_synced and not failed_extra:
                    try:
                        jpath.unlink()
                    except OSError:
                        pass
                    recovered.append({'project_id': pid, 'action': 'completed_purge'})
                else:
                    recovered.append({'project_id': pid, 'action': 'pending_audit_update'})
        return recovered

    @contextmanager
    def _project_guard(self, project_id: str):
        """Serialize Web mutations and recover an interrupted composition first.

        IDE writers do not use this lock; the composer and text editor also
        check disk revisions before replacing any files.
        """
        with self._state_lock():
            self._broker_project(project_id)
            from .web_composer import Composer
            Composer(self).recover(project_id)
            yield

    def composition_catalog(self) -> dict[str, object]:
        from .web_composer import Composer
        return Composer(self).catalog()

    def read_composition(self, project_id: str) -> dict[str, object]:
        from .web_composer import Composer
        with self._project_guard(project_id):
            result = Composer(self).read(project_id)
        self._sync_usage(project_id)
        return result

    def preview_composition(self, project_id: str, payload: dict[str, object]) -> dict[str, object]:
        from .web_composer import Composer
        with self._project_guard(project_id):
            return Composer(self).preview(project_id, payload)

    def apply_composition(self, project_id: str, payload: dict[str, object]) -> dict[str, object]:
        from .web_composer import Composer
        with self._project_guard(project_id):
            result = Composer(self).apply(project_id, payload)
        self._sync_usage(project_id)
        return result

    def _new_path(self, name: str, project_id: str) -> Path:
        return self.projects_root / f'{_safe_component(name)}-{project_id[:8]}'

    def create_project(self, name: str, template_ref: str | None = None) -> dict[str, object]:
        if not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 200:
            raise _error('项目名称不能为空且不得超过 200 字节')
        template = None
        if template_ref is not None:
            if not isinstance(template_ref, str) or not template_ref:
                raise _error('模板必须是精确版本引用')
            template = self.store.request('get_shared_template', ref=template_ref)
            from .web_store import _validate_files
            template['files'], _ = _validate_files(template['files'])
        record = self.store.request('create_project', name=name.strip())
        if not isinstance(record, dict) or not isinstance(record.get('project_id'), str):
            raise _error('提交服务未返回项目 ID')
        project_id = record['project_id']
        destination = self._new_path(name, project_id)
        if destination.exists():
            raise _error('项目目录已存在')
        temporary = Path(tempfile.mkdtemp(prefix='.rk-web-', dir=self.projects_root))
        try:
            document = {
                'schema': 'researchkit.project/v1', 'name': name.strip(), 'mode': 'python', 'entry': 'case.py',
                'plugins': {}, 'parameters': {}, 'data': {},
                'requires': {'python': '3.11', 'modules': [], 'minimums': {}},
                'origin': {'kind': 'self-created'},
            }
            if template is not None:
                for rel, content in template['files'].items():
                    target = temporary / _relative(rel)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open('xb') as stream:
                        stream.write(content)
                try:
                    document = yaml.safe_load(template['files']['project.yaml'].decode('utf-8'))
                except (KeyError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
                    raise _error('共享模板配置无效') from exc
                if not isinstance(document, dict) or document.get('schema') != 'researchkit.project/v1':
                    raise _error('共享模板配置无效')
                document['name'] = name.strip()
                document['origin'] = {'kind': 'web-template/v1', 'template_ref': template_ref,
                                      'source_origin': document.get('origin', {})}
            else:
                atomic_text(temporary / 'case.py', '"""Edit this project in the IDE or Web editor."""\n\n\ndef run(context):\n    return {}\n')
            write_yaml(temporary / 'project.yaml', document)
            if not (temporary / 'README.md').exists():
                atomic_text(temporary / 'README.md', f'# {name.strip()}\n\n这是当前账号自己的 ResearchKit 项目。\n')
            install_runtime(temporary)
            seal_lock(temporary)
            atomic_text(
                temporary / 'README-ResearchKit.md',
                '# ResearchKit 项目\n\n'
                '本项目由当前账号在 Web 创建；代码、实验方案和运行产物由你在自己的 IDE/Python 环境中维护。'
                '可按需要使用本机 GPU；Web 不执行、检查或要求运行成功。\n\n'
                '在 Web 的“实验方案与产物”中保存参数方案、浏览或选择本项目的产物。'
                '固定提交时确认要带入的小型产物；大文件可登记为派生数据。教师按代码、参数和产物进行人工审核入库。\n\n'
                'IDE 运行方式：先在 Web 启用实验入口，再执行 `python run_experiment.py --scheme baseline`。'
                '也可以直接编辑 `case.py`、`project.yaml` 与 `algorithms/` 中的本地算法。\n',
            )
            os.chmod(temporary, 0o700)
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                import shutil
                shutil.rmtree(temporary)
        with self._state_lock():
            projects = self._load_state()
            projects[project_id] = {'path': destination.relative_to(self.root).as_posix(), 'name': name.strip()}
            self._save_state(projects)
        self._sync_usage(project_id)
        return self.describe_project(project_id)

    def register_existing(self, name: str, relative_directory: str) -> dict[str, object]:
        """Register only an existing directory already below this user's projects root."""
        relative_directory = _relative(relative_directory, label='导入目录')
        directory = safe_path(self.root, relative_directory)
        try:
            directory.relative_to(self.projects_root)
        except ValueError as exc:
            raise _error('只能导入当前工作区 projects/ 下的目录') from exc
        directory_fd = self._open_absolute_directory(directory)
        try:
            self._check_tree(directory_fd, collect=False)
            project_document = self._read_regular(directory_fd, 'project.yaml')
        finally:
            os.close(directory_fd)
        if not project_document:
            raise _error('导入目录缺少 project.yaml')
        record = self.store.request('create_project', name=name)
        if not isinstance(record, dict) or not isinstance(record.get('project_id'), str):
            raise _error('提交服务未返回项目 ID')
        with self._state_lock():
            projects = self._load_state()
            projects[record['project_id']] = {'path': relative_directory, 'name': name}
            self._save_state(projects)
        return self.describe_project(record['project_id'])

    def list_distributions(self) -> list[dict[str, object]]:
        result = self.store.request('list_distributions')
        if not isinstance(result, list):
            raise _error('分发服务返回无效')
        return result

    def export_package(self, submission_id: str) -> bytes:
        from .web_packages import pack_package
        result = self.store.request('export_package', submission_id=submission_id)
        return pack_package(result['files'], result['bundle'])

    def import_package(self, name: str, data: bytes) -> dict[str, object]:
        from .web_packages import unpack_package
        files, bundle = unpack_package(data)
        try:
            document = yaml.safe_load(files['project.yaml'].decode('utf-8'))
        except (KeyError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
            raise _error('ZIP 中 project.yaml 必须是 UTF-8 ResearchKit 配置') from exc
        if not isinstance(document, dict) or document.get('schema') != 'researchkit.project/v1':
            raise _error('ZIP 中 project.yaml 必须使用 researchkit.project/v1')
        if not isinstance(name, str) or not name.strip():
            raise _error('请填写导入后的项目名称')
        project = self.store.request('import_package', name=name.strip(), files=files, bundle=bundle)
        project_id = project['project_id']
        with self._state_lock():
            projects = self._load_state()
            if project_id not in projects:
                destination = self._materialize_distribution(project_id, files,
                                                            imported=not bool(project.get('distribution_id')))
                projects[project_id] = {'path': destination.relative_to(self.root).as_posix(), 'name': str(project['name'])}
                self._save_state(projects)
            else:
                self._project_path(project_id)
        if project.get('distribution_id'):
            self.store.request('confirm_distribution', distribution_id=project['distribution_id'])
        return self.describe_project(project_id)

    def component_access(self, *, include_inactive: bool = False) -> list[dict[str, object]]:
        return self.store.request('get_component_access', include_inactive=include_inactive)

    def set_component_access(self, payload: dict[str, object]) -> dict[str, object]:
        if type(payload) is not dict or set(payload) != {'kind', 'ref', 'public', 'targets', 'expected_revision'}:
            raise _error('共享权限请求字段无效')
        return self.store.request('set_component_access', **payload)

    def create_distribution(self, submission_id: str, target_username: str) -> dict[str, object]:
        if self.principal.role != 'teacher':
            raise _error('只有教师可以分发')
        if not isinstance(submission_id, str) or not isinstance(target_username, str) or not target_username.strip():
            raise _error('请选择固定提交并填写目标服务器账号')
        result = self.store.request('create_distribution', submission_id=submission_id,
                                    target_username=target_username.strip())
        if not isinstance(result, dict):
            raise _error('分发服务返回无效')
        return result

    def assignment_preview(self, payload: object) -> dict[str, object]:
        """Validate a teacher's direct task before a fixed baseline is made."""
        # Lazy import avoids a Workspace/AssignmentService import cycle while
        # keeping the HTTP layer dependent on Workspace alone.
        from .web_assignments import AssignmentService
        return AssignmentService(self).preview(payload)

    def create_assignment(self, payload: object) -> dict[str, object]:
        """Create one hidden task baseline and atomically dispatch it."""
        from .web_assignments import AssignmentService
        return AssignmentService(self).create(payload)

    def _materialize_distribution(self, project_id: str, files: dict[str, bytes], *, imported: bool = False) -> Path:
        """Materialize a fixed snapshot via directory FDs; never overwrite an existing project."""
        from .web_store import _record_id, _validate_files
        project_id = _record_id(project_id, label='领取项目 ID')
        files, _ = _validate_files(files)
        parent_fd = self._open_absolute_directory(self.projects_root)
        name = ('imported-' if imported else 'assigned-') + project_id
        destination = self.projects_root / name
        temporary = '.rk-claim-' + uuid.uuid4().hex
        made_temporary = False
        try:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                # Recover a crash after rename but before registry save only if every byte still matches.
                if (not stat.S_ISDIR(existing.st_mode) or existing.st_uid != os.geteuid()
                        or stat.S_IMODE(existing.st_mode) != 0o700):
                    raise _error('领取目标目录不安全；不会覆盖已有文件')
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    observed = os.fstat(fd)
                    if (observed.st_dev, observed.st_ino) != (existing.st_dev, existing.st_ino):
                        raise _error('领取目标目录发生变化')
                    entries = self._check_tree(fd, collect=True)
                    if {entry['path'] for entry in entries} != set(files):
                        raise _error('领取目录已有不同文件；请保留现有目录后重试，不会自动覆盖')
                    for path, content in files.items():
                        if self._read_regular(fd, path, limit=MAX_FILE_BYTES) != content:
                            raise _error('领取目录已有修改；请保留现有目录后重试，不会自动覆盖')
                finally:
                    os.close(fd)
                return destination
            os.mkdir(temporary, 0o700, dir_fd=parent_fd)
            made_temporary = True
            staged_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                for path, content in sorted(files.items()):
                    directory_fd = os.dup(staged_fd)
                    try:
                        parts = path.split('/')
                        for part in parts[:-1]:
                            try:
                                os.mkdir(part, 0o700, dir_fd=directory_fd)
                            except FileExistsError:
                                pass
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                            os.close(directory_fd)
                            directory_fd = child
                        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o700 if path == 'task' else 0o600, dir_fd=directory_fd)
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                os.fsync(staged_fd)
            finally:
                os.close(staged_fd)
            # Link-free path check above and the per-workspace lock handle cooperating clients.
            # Refuse a concurrent destination instead of replacing user files.
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _error('领取目标目录已出现，请重试；不会覆盖')
            from .web_composer import _rename_noreplace
            _rename_noreplace(parent_fd, temporary, name)
            made_temporary = False
            os.fsync(parent_fd)
            return destination
        finally:
            if made_temporary:
                import shutil
                shutil.rmtree(temporary, dir_fd=parent_fd)
            os.close(parent_fd)

    def claim_distribution(self, distribution_id: str) -> dict[str, object]:
        result = self.store.request('claim_distribution', distribution_id=distribution_id)
        if not isinstance(result, dict) or not isinstance(result.get('project'), dict) or not isinstance(result.get('files'), dict):
            raise _error('领取服务返回无效')
        project = result['project']
        project_id = project['project_id']
        with self._state_lock():
            projects = self._load_state()
            if project_id not in projects:
                destination = self._materialize_distribution(project_id, result['files'])
                projects[project_id] = {'path': destination.relative_to(self.root).as_posix(), 'name': str(project['name'])}
                self._save_state(projects)
            else:
                # A retry must keep IDE edits; it may not recreate a removed registered directory.
                self._project_path(project_id)
        self.store.request('confirm_distribution', distribution_id=distribution_id)
        return self.describe_project(project_id)

    def diff_baseline(self, submission_id: str, relative_path: str) -> dict[str, object]:
        """Compare a fixed submission with its centrally recorded teacher baseline."""
        import difflib
        submitted = self.store.request('get_submission', submission_id=submission_id)
        distribution_id = submitted.get('project', {}).get('distribution_id')
        if distribution_id:
            baseline = self.store.request('get_distribution_baseline', distribution_id=distribution_id)
        elif submitted.get('project', {}).get('bundle_id'):
            baseline = self.store.request('get_package_baseline', project_id=submitted['project']['project_id'])
        else:
            raise _error('该提交没有可信教师分发基线')
        path = _relative(relative_path)
        left = baseline['files'].get(path, b'')
        right = submitted['files'].get(path, b'')
        if path not in baseline['files'] and path not in submitted['files']:
            raise _error('两个版本中均没有该文件')
        if len(left) > MAX_EDIT_BYTES or len(right) > MAX_EDIT_BYTES:
            raise _error('文件超过 Web 比较上限')
        try:
            before, after = left.decode('utf-8'), right.decode('utf-8')
        except UnicodeError as exc:
            raise _error('二进制文件不能在 Web 中比较') from exc
        diff = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                           fromfile='教师基线/' + path, tofile=submission_id + '/' + path))
        if len(diff.encode('utf-8')) > MAX_EDIT_BYTES:
            raise _error('差异超过 Web 查看上限')
        return {'path': path, 'baseline_submission_id': baseline['submission_id'],
                'submission_id': submission_id, 'diff': diff}

    def _load_archived_projects(self) -> set[str]:
        try:
            descriptor = os.open(self._archive_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
        except FileNotFoundError:
            if self._archive_path.is_symlink():
                raise _error('项目归档索引不安全')
            return set()
        except OSError as exc:
            raise _error('项目归档索引不安全或无法读取') from exc
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_size > MAX_EDIT_BYTES):
                raise _error('项目归档索引不安全')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_EDIT_BYTES + 1)
            if len(raw) > MAX_EDIT_BYTES:
                raise _error('项目归档索引过大')
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise _error('项目归档索引已损坏') from exc
            if (type(payload) is not dict or set(payload) != {'schema', 'uid', 'username', 'projects'} or
                    payload['schema'] != 'researchkit.web-project-archive/v1' or
                    type(payload['uid']) is not int or payload['uid'] != self.principal.uid or
                    payload['username'] != self.principal.username or type(payload['projects']) is not list):
                raise _error('项目归档索引格式或所属账号无效')
            ids = payload['projects']
            if any(not isinstance(value, str) or len(value) != 36 for value in ids):
                raise _error('项目归档索引项目 ID 无效')
            try:
                if any(str(uuid.UUID(value)) != value for value in ids) or len(set(ids)) != len(ids):
                    raise ValueError
            except ValueError as exc:
                raise _error('项目归档索引项目 ID 无效') from exc
            return set(ids)
        finally:
            os.close(descriptor)

    def _save_archived_projects(self, ids: set[str]) -> None:
        payload = {'schema': 'researchkit.web-project-archive/v1', 'uid': self.principal.uid,
                   'username': self.principal.username, 'projects': sorted(ids)}
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n'
        if len(content.encode('utf-8')) > MAX_EDIT_BYTES:
            raise _error('项目归档索引过大')
        atomic_text(self._archive_path, content)
        os.chmod(self._archive_path, 0o600)

    def set_project_archived(self, project_id: str, archived: bool) -> dict[str, object]:
        if type(archived) is not bool or not isinstance(project_id, str):
            raise _error('项目归档参数无效')
        with self._state_lock():
            if project_id not in self._load_state():
                raise _error('项目不在当前工作区')
            # The personal index is not ownership authority. Verify the broker
            # record without requiring the IDE directory to remain available.
            self._broker_project(project_id)
            ids = self._load_archived_projects()
            if archived:
                ids.add(project_id)
            else:
                ids.discard(project_id)
            self._save_archived_projects(ids)
        return {'project_id': project_id, 'archived': archived}

    def personal_resource_visibility(self) -> dict[str, list[str]]:
        from .web_resource_visibility import get
        return get(self)

    def set_personal_resource_visibility(self, payload: object) -> dict[str, list[str]]:
        from .web_resource_visibility import update
        return update(self, payload)

    def _workspace_project_info(self, project_id: str) -> dict[str, object]:
        with self._state_lock():
            entry = self._load_state().get(project_id)
        if entry is None:
            return {'in_workspace': False, 'exists': False}
        path = safe_path(self.root, entry['path'])
        try:
            path.relative_to(self.projects_root)
        except ValueError:
            return {'in_workspace': False, 'exists': False}
        return {
            'in_workspace': True,
            'relative_path': entry['path'],
            'path': str(path),
            'exists': os.path.lexists(path),
        }

    def list_purgeable_projects(self) -> list[dict[str, object]]:
        rows = self.store.request('project_purge_list')
        if not isinstance(rows, list):
            raise _error('项目清理列表格式无效')
        with self._state_lock():
            state = self._load_state()
        for row in rows:
            if isinstance(row, dict):
                pid = row.get('project_id')
                row['in_workspace'] = isinstance(pid, str) and pid in state
                if row['in_workspace']:
                    row['workspace_path'] = state[pid]['path']
        return rows

    def list_project_purge_audit(self, limit: int = 30) -> list[dict[str, object]]:
        rows = self.store.request('project_purge_audit', limit=limit)
        if not isinstance(rows, list):
            raise _error('项目清理审计记录格式无效')
        return rows

    def preview_project_purge(self, project_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        if not isinstance(project_id, str):
            raise _error('项目 ID 无效')
        if payload is not None and type(payload) is not dict:
            raise _error('清理预览参数必须是对象')
        opts = payload or {}
        extra_keys = set(opts) - {'selected_keys', 'reset_task'}
        if extra_keys:
            raise _error('清理预览包含无效字段')
        ws_info = self._workspace_project_info(project_id)
        params: dict[str, object] = {'project_id': project_id, 'workspace_info': ws_info}
        if 'selected_keys' in opts:
            params['selected_keys'] = opts['selected_keys']
        if 'reset_task' in opts:
            params['reset_task'] = opts['reset_task']
        result = self.store.request('project_purge_preview', **params)
        if not isinstance(result, dict):
            raise _error('清理预览结果格式无效')
        return result

    def purge_project(self, project_id: str, payload: dict[str, object]) -> dict[str, object]:
        """Purge ONLY central project records via management; personal directories use `delete_personal_project`."""
        if not isinstance(project_id, str) or type(payload) is not dict:
            raise _error('按项目清理参数无效')
        allowed = {'plan_token', 'confirmation', 'selected_keys', 'reset_task'}
        if set(payload) - allowed:
            raise _error('按项目清理包含无效字段')
        ws_info = self._workspace_project_info(project_id)
        params: dict[str, object] = {
            'project_id': project_id,
            'plan_token': payload.get('plan_token'),
            'confirmation': payload.get('confirmation'),
            'workspace_info': ws_info,
        }
        if 'selected_keys' in payload:
            params['selected_keys'] = payload['selected_keys']
        if 'reset_task' in payload:
            params['reset_task'] = payload['reset_task']

        receipt = self.store.request('project_purge_execute', **params)
        if not isinstance(receipt, dict):
            raise _error('项目清理回执格式无效')
        return receipt

    def delete_personal_project(self, project_id: str, confirmation: object = '') -> dict[str, object]:
        """Remove ONLY the user's personal workspace copy and local index without deleting central submissions/jobs."""
        if not isinstance(project_id, str) or not project_id:
            raise _error('项目 ID 无效')
        if confirmation != '确认删除本地副本':
            raise _error('请输入“确认删除本地副本”确认移除工作区本地目录')
        with self._state_lock():
            self.recover_pending_purges()
            in_local_state = project_id in self._load_state()
        if not in_local_state:
            raise _error('项目不在当前工作区')
        try:
            self._broker_project(project_id)
        except KitError:
            pass
        # Web execution is retired. Local removal must not query an obsolete
        # jobs API, and the user confirms their own IDE processes are stopped.
        removed_path = None
        with self._state_lock():
            state = self._load_state()
            entry = state.get(project_id)
            if entry is None:
                raise _error('项目不在当前工作区')
            path = safe_path(self.root, entry['path'])
            try:
                path.relative_to(self.projects_root)
            except ValueError as exc:
                raise _error('工作区项目路径越界，已保留本地目录') from exc
            if os.path.islink(path):
                raise _error('拒绝删除指向工作区外部的符号链接目录')
            if path.exists():
                staging = self.projects_root / f'.delete-local-{uuid.uuid4().hex}'
                os.rename(path, staging)
                try:
                    shutil.rmtree(staging)
                except OSError as exc:
                    if not os.path.lexists(path):
                        try:
                            os.rename(staging, path)
                        except OSError:
                            pass
                    raise _error(f'删除工作区本地目录失败：{exc}') from exc
                removed_path = entry['path']
            state.pop(project_id, None)
            self._save_state(state)
            archived = self._load_archived_projects()
            if project_id in archived:
                archived.discard(project_id)
                self._save_archived_projects(archived)
        return {'project_id': project_id, 'deleted_local': True, 'workspace_path': removed_path}

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, object]]:
        if type(include_archived) is not bool:
            raise _error('include_archived 必须是布尔值')
        self.recover_pending_purges()
        with self._state_lock():
            projects = self._load_state()
            archived = self._load_archived_projects()
        result = []
        for project_id, entry in projects.items():
            if project_id in archived and not include_archived:
                continue
            try:
                value = self.describe_project(project_id)
            except KitError:
                # A renamed IDE directory should be visible as unavailable, not silently treated as another path.
                value = {'project_id': project_id, 'name': entry['name'], 'unavailable': True}
            result.append({**value, 'archived': project_id in archived})
        return result

    def describe_project(self, project_id: str) -> dict[str, object]:
        with self._project_guard(project_id):
            return self._describe_project_unlocked(project_id)

    def _describe_project_unlocked(self, project_id: str) -> dict[str, object]:
        broker = self._broker_project(project_id)
        path = self._project_path(project_id)
        root_fd = self._open_project_fd(project_id)
        try:
            files = self._check_tree(root_fd, collect=True)
            try:
                document = yaml.safe_load(self._read_regular(root_fd, 'project.yaml').decode('utf-8'))
                if not isinstance(document, dict):
                    document = None
            except (KitError, UnicodeError, yaml.YAMLError):
                document = None
        finally:
            os.close(root_fd)
        return {'project_id': project_id, 'name': broker.get('name'), 'source': broker.get('source'),
                'path': str(path), 'relative_path': path.relative_to(self.root).as_posix(),
                'files': files, 'project_yaml': document,
                **{key: broker[key] for key in ('distribution_id', 'baseline_submission_id', 'baseline_sha256', 'central_purged') if key in broker}}

    @staticmethod
    def _is_protected(path: str) -> bool:
        return path in _PROTECTED_FILES or path.startswith(_PROTECTED_PREFIXES)

    def _open_absolute_directory(self, path: Path) -> int:
        """Open every absolute component by descriptor and retain the final directory."""
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            for part in path.parts[1:]:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
                        info.st_uid not in {0, os.geteuid()} or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                    raise _error('项目目录包含不安全的父目录')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                try:
                    observed = os.fstat(child)
                    if observed.st_dev != info.st_dev or observed.st_ino != info.st_ino:
                        raise _error('项目目录在打开时发生变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_project_fd(self, project_id: str) -> int:
        """Open this registered project from / without traversing a symlink."""
        return self._open_absolute_directory(self._project_path(project_id))

    def _open_parent_fd(self, root_fd: int, rel: str) -> tuple[int, str]:
        parts = _relative(rel).split('/')
        descriptor = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
                        info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                    raise _error('文件父目录不安全')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                try:
                    observed = os.fstat(child)
                    if observed.st_dev != info.st_dev or observed.st_ino != info.st_ino:
                        raise _error('文件父目录在打开时发生变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor, parts[-1]
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _regular_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _read_regular_from_parent(self, parent_fd: int, name: str, *, limit: int) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _error('文件不存在') from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _error('文件必须是无链接的普通文件')
        if info.st_size > limit:
            raise _error('文本文件超过 Web 编辑上限')
        identity = self._regular_identity(info)
        descriptor = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or self._regular_identity(observed) != identity:
                raise _error('文件在读取时发生变化')
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            content = b''.join(chunks)
            final = os.fstat(descriptor)
            if self._regular_identity(final) != identity:
                raise _error('文件在读取时发生变化')
            return content, identity
        finally:
            os.close(descriptor)

    def _read_regular(self, root_fd: int, rel: str, *, limit: int = MAX_EDIT_BYTES) -> bytes:
        parent_fd, name = self._open_parent_fd(root_fd, rel)
        try:
            return self._read_regular_from_parent(parent_fd, name, limit=limit)[0]
        finally:
            os.close(parent_fd)

    @contextmanager
    def _file_lock(self, project_id: str, rel: str):
        lock_root = _secure_directory(self.root / '.researchkit-web-file-locks', create=True)
        lock_name = hashlib.sha256((project_id + '\0' + rel).encode('utf-8')).hexdigest() + '.lock'
        lock_fd = os.open(lock_root / lock_name, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise _error('项目文件锁不安全')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _parent_still_current(self, root_fd: int, rel: str, parent_identity: tuple[int, int]) -> bool:
        descriptor = os.dup(root_fd)
        try:
            for part in _relative(rel).split('/')[:-1]:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    return False
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            observed = os.fstat(descriptor)
            return (observed.st_dev, observed.st_ino) == parent_identity
        except OSError:
            return False
        finally:
            os.close(descriptor)

    def _replace_regular(self, root_fd: int, parent_fd: int, rel: str, name: str, expected: tuple[int, int, int, int, int, int], data: bytes) -> None:
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        temporary = '.rk-web-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=parent_fd)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if self._regular_identity(current) != expected:
                raise _error('编辑冲突：文件已被 IDE 或其他编辑器修改')
            observed_parent = os.fstat(parent_fd)
            if ((observed_parent.st_dev, observed_parent.st_ino) != parent_identity or
                    not self._parent_still_current(root_fd, rel, parent_identity)):
                raise _error('文件父目录在保存时发生变化')
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except Exception:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise

    def read_text(self, project_id: str, relative_path: str) -> dict[str, object]:
        with self._project_guard(project_id):
            return self._read_text_unlocked(project_id, relative_path)

    def _read_text_unlocked(self, project_id: str, relative_path: str) -> dict[str, object]:
        self._broker_project(project_id)
        root_fd = self._open_project_fd(project_id)
        try:
            content = self._read_regular(root_fd, relative_path)
        finally:
            os.close(root_fd)
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise _error('Web 仅编辑 UTF-8 文本文件') from exc
        path = _relative(relative_path)
        return {'path': path, 'text': text, 'sha256': _digest(content), 'editable': not self._is_protected(path)}

    def save_text(self, project_id: str, relative_path: str, text: str, expected_sha256: str) -> dict[str, object]:
        if not isinstance(text, str) or not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise _error('保存参数无效')
        try:
            data = text.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise _error('文本必须是 UTF-8') from exc
        if len(data) > MAX_EDIT_BYTES:
            raise _error('文本文件超过 Web 编辑上限')
        relative_path = _relative(relative_path)
        if self._is_protected(relative_path):
            raise _error('该固定依赖文件为只读')
        self._broker_project(project_id)
        with self._project_guard(project_id), self._file_lock(project_id, relative_path):
            root_fd = self._open_project_fd(project_id)
            try:
                parent_fd, name = self._open_parent_fd(root_fd, relative_path)
                try:
                    current, identity = self._read_regular_from_parent(parent_fd, name, limit=MAX_EDIT_BYTES)
                    if _digest(current) != expected_sha256:
                        raise _error('编辑冲突：文件已被 IDE 或其他编辑器修改')
                    # Read twice while the per-file Web lock is held; IDE does not honor it.
                    repeated, repeated_identity = self._read_regular_from_parent(parent_fd, name, limit=MAX_EDIT_BYTES)
                    if _digest(repeated) != expected_sha256 or repeated_identity != identity:
                        raise _error('编辑冲突：文件已被 IDE 或其他编辑器修改')
                    self._replace_regular(root_fd, parent_fd, relative_path, name, identity, data)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
        if relative_path in {'project.yaml', 'DATA-REFERENCES.json'}:
            self._sync_usage(project_id)
        return {'path': relative_path, 'sha256': _digest(data), 'size': len(data)}

    @staticmethod
    def _references_document(content: bytes) -> list[dict[str, object]]:
        """Decode the only project file that may name externally stored data."""
        try:
            value = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _error('DATA-REFERENCES.json 必须是 UTF-8 JSON 列表') from exc
        return _validate_data_references(value)

    def _project_references_unlocked(self, project_id: str) -> dict[str, object]:
        """Read references through a project descriptor; never follow a link."""
        root_fd = self._open_project_fd(project_id)
        try:
            try:
                content, identity = self._read_regular_from_parent(root_fd, _DATA_REFERENCES_NAME,
                                                                    limit=MAX_EDIT_BYTES)
            except KitError as exc:
                if str(exc) != '文件不存在':
                    raise
                return {'exists': False, 'sha256': None, 'references': [], 'identity': None}
        finally:
            os.close(root_fd)
        return {'exists': True, 'sha256': _digest(content), 'references': self._references_document(content),
                'identity': identity}

    @staticmethod
    def _reference_groups(references: list[dict[str, object]]) -> list[dict[str, object]]:
        groups: dict[str, dict[str, object]] = {}
        for reference in references:
            dataset_id = str(reference['dataset_id'])
            group = groups.setdefault(dataset_id, {'dataset_id': dataset_id, 'file_count': 0, 'total_size': 0})
            group['file_count'] = int(group['file_count']) + 1
            group['total_size'] = int(group['total_size']) + int(reference['size'])
        return [groups[dataset_id] for dataset_id in sorted(groups)]

    def project_datasets(self, project_id: str) -> dict[str, object]:
        """Return the stable project-side reference state for the attach CAS."""
        with self._project_guard(project_id):
            state = self._project_references_unlocked(project_id)
        references = state['references']
        assert isinstance(references, list)
        groups = self._reference_groups(references)
        if groups:
            try:
                ex_map = self.store.request(
                    'check_dataset_existence',
                    dataset_ids=[str(g['dataset_id']) for g in groups],
                )
                if isinstance(ex_map, dict):
                    for g in groups:
                        info = ex_map.get(str(g['dataset_id']))
                        if isinstance(info, dict):
                            g['stale'] = not bool(info.get('exists', True))
                            if info.get('name'):
                                g['name'] = info['name']
                            if g['stale']:
                                g['stale_message'] = '该数据集已被管理员清理'
            except KitError:
                pass
        return {'exists': state['exists'], 'sha256': state['sha256'], 'references': references,
                'datasets': groups}

    def remove_stale_dataset_reference(self, project_id: str, dataset_id: object,
                                       expected_sha256: object = None) -> dict[str, object]:
        """Remove one cleaned or unwanted dataset reference from DATA-REFERENCES.json."""
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise _error('数据集 ID 无效')
        target_id = dataset_id.strip()
        with self._project_guard(project_id), self._file_lock(project_id, _DATA_REFERENCES_NAME):
            before = self._project_references_unlocked(project_id)
            if expected_sha256 is not None and before['sha256'] != expected_sha256:
                raise _error('编辑冲突：DATA-REFERENCES.json 已被 IDE 或其他编辑器修改')
            if not before['exists']:
                return {'exists': False, 'sha256': None, 'references': [], 'datasets': []}
            old_refs = before['references']
            assert isinstance(old_refs, list)
            filtered = [r for r in old_refs if str(r.get('dataset_id')) != target_id]
            filtered = _validate_data_references(filtered)
            encoded = (json.dumps(filtered, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
            root_fd = self._open_project_fd(project_id)
            try:
                parent_fd, name = self._open_parent_fd(root_fd, _DATA_REFERENCES_NAME)
                try:
                    current, identity = self._read_regular_from_parent(parent_fd, name, limit=MAX_EDIT_BYTES)
                    if before['sha256'] != _digest(current) or identity != before['identity']:
                        raise _error('编辑冲突：DATA-REFERENCES.json 已被 IDE 或其他编辑器修改')
                    self._replace_regular(root_fd, parent_fd, _DATA_REFERENCES_NAME, name, identity, encoded)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
        self._sync_usage(project_id)
        return self.project_datasets(project_id)

    def _catalog_datasets(self) -> dict[str, list[dict[str, object]]]:
        """Normalize only the broker's already-authorized catalog payload.

        The client supplies dataset IDs, never file paths.  Re-expanding the
        selected IDs here prevents a browser from inventing a host path or a
        subset of a version that was not registered by the service.
        """
        catalog = self.store.request('get_dataset_catalog')
        if not isinstance(catalog, list):
            raise _error('受控数据目录返回无效')
        values: dict[str, list[dict[str, object]]] = {}
        for item in catalog:
            if type(item) is not dict or not isinstance(item.get('dataset_id'), str) or item['dataset_id'] in values:
                raise _error('受控数据目录条目无效')
            dataset_id = item['dataset_id']
            raw_files = item.get('files')
            if not isinstance(raw_files, list):
                raise _error('受控数据目录文件条目无效')
            candidate: list[dict[str, object]] = []
            for file in raw_files:
                if type(file) is not dict:
                    raise _error('受控数据目录文件条目无效')
                # Metadata added to catalog entries is deliberately ignored;
                # references must remain the exact four-field submission form.
                if set(file) - {'dataset_id', 'path', 'size', 'sha256'}:
                    raise _error('受控数据目录文件条目无效')
                if file.get('dataset_id', dataset_id) != dataset_id:
                    raise _error('受控数据目录文件归属无效')
                candidate.append({'dataset_id': dataset_id, 'path': file.get('path'),
                                  'size': file.get('size'), 'sha256': file.get('sha256')})
            values[dataset_id] = _validate_data_references(candidate)
        return values

    def _create_regular(self, root_fd: int, parent_fd: int, rel: str, name: str, data: bytes) -> None:
        """Create a new project file without replacing an IDE-created file."""
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        temporary = '.rk-web-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                             0o600, dir_fd=parent_fd)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            observed_parent = os.fstat(parent_fd)
            if ((observed_parent.st_dev, observed_parent.st_ino) != parent_identity or
                    not self._parent_still_current(root_fd, rel, parent_identity)):
                raise _error('文件父目录在保存时发生变化')
            # link(2) is an atomic create-if-absent operation.  The temporary
            # name is removed immediately, so the resulting project file has
            # the normal single link required by workspace snapshots.
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError as exc:
            raise _error('编辑冲突：DATA-REFERENCES.json 已被 IDE 或其他编辑器创建') from exc
        except Exception:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise

    def add_project_datasets(self, project_id: str, dataset_ids: object,
                             expected_sha256: object) -> dict[str, object]:
        """Merge whole authorized datasets into DATA-REFERENCES.json by CAS.

        A dataset ID is an immutable registered version.  The caller cannot
        select individual catalog files, and any pre-existing valid manual
        references remain intact.
        """
        if (not isinstance(dataset_ids, list) or not dataset_ids or len(dataset_ids) > 128 or
                any(not isinstance(value, str) or not value for value in dataset_ids) or
                len(set(dataset_ids)) != len(dataset_ids)):
            raise _error('数据集选择必须是 1 至 128 个不重复的数据集 ID')
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or
                                            len(expected_sha256) != 64 or
                                            any(char not in '0123456789abcdef' for char in expected_sha256)):
            raise _error('数据引用版本标记无效')
        catalog = self._catalog_datasets()
        missing = sorted(set(dataset_ids) - set(catalog))
        if missing:
            raise _error(f'数据集不存在、未登记或当前账号未获授权：{missing[0]}')
        additions = [reference for dataset_id in dataset_ids for reference in catalog[dataset_id]]
        with self._project_guard(project_id), self._file_lock(project_id, _DATA_REFERENCES_NAME):
            before = self._project_references_unlocked(project_id)
            if before['sha256'] != expected_sha256:
                raise _error('编辑冲突：DATA-REFERENCES.json 已被 IDE 或其他编辑器修改')
            old_references = before['references']
            assert isinstance(old_references, list)
            merged = list(old_references)
            present = {(str(reference['dataset_id']), str(reference['path'])) for reference in merged}
            for reference in additions:
                key = str(reference['dataset_id']), str(reference['path'])
                if key not in present:
                    merged.append(reference)
                    present.add(key)
            # Preserve the standard, bounded store format before changing disk.
            merged = _validate_data_references(merged)
            encoded = (json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
            if len(encoded) > MAX_EDIT_BYTES:
                raise _error('DATA-REFERENCES.json 超过 Web 编辑上限')
            root_fd = self._open_project_fd(project_id)
            try:
                if before['exists']:
                    parent_fd, name = self._open_parent_fd(root_fd, _DATA_REFERENCES_NAME)
                    try:
                        current, identity = self._read_regular_from_parent(parent_fd, name, limit=MAX_EDIT_BYTES)
                        if _digest(current) != expected_sha256 or identity != before['identity']:
                            raise _error('编辑冲突：DATA-REFERENCES.json 已被 IDE 或其他编辑器修改')
                        self._replace_regular(root_fd, parent_fd, _DATA_REFERENCES_NAME, name, identity, encoded)
                    finally:
                        os.close(parent_fd)
                else:
                    # `_create_regular` makes the absent-state expectation a
                    # real CAS condition instead of silently replacing a file
                    # the IDE created after our initial read.
                    self._create_regular(root_fd, root_fd, _DATA_REFERENCES_NAME,
                                         _DATA_REFERENCES_NAME, encoded)
            finally:
                os.close(root_fd)
        self._sync_usage(project_id)
        return {'exists': True, 'sha256': _digest(encoded), 'references': merged,
                'datasets': self._reference_groups(merged)}

    def _derived_imports(self, root_fd: int) -> dict[str, dict[str, object]]:
        try:
            raw = self._read_regular(root_fd, '.researchkit/derived-imports.json', limit=MAX_EDIT_BYTES)
        except KitError as exc:
            if str(exc) == '文件不存在':
                return {}
            raise
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _error('派生数据导入记录已损坏') from exc
        if type(value) is not dict or value.get('schema') != 'researchkit.web-derived-imports/v1' or type(value.get('files')) is not dict:
            raise _error('派生数据导入记录无效')
        result = {}
        for path, reference in value['files'].items():
            rel = _relative(path)
            if not rel.startswith(('data/processed/', 'artifacts/', 'results/', 'report/', '_work/')):
                raise _error('派生数据导入记录只能指向项目产物')
            result[rel] = _validate_data_references([reference])[0]
        return result

    def _remember_derived_import(self, root_fd: int, path: str, reference: dict[str, object]) -> bool:
        # Only explicit data/output locations may be omitted from a submission.
        # Never omit source code, contracts, runtime, or project configuration.
        if (not path.startswith(('data/processed/', 'artifacts/', 'results/', 'report/', '_work/'))
                or Path(path).suffix.lower() in _SOURCE_SUFFIXES):
            return False
        records = self._derived_imports(root_fd)
        records[path] = _validate_data_references([reference])[0]
        payload = (json.dumps({'schema': 'researchkit.web-derived-imports/v1', 'files': records}, sort_keys=True) + '\n').encode()
        if len(payload) > MAX_EDIT_BYTES:
            raise _error('派生数据导入记录过大')
        rel = '.researchkit/derived-imports.json'
        parent, name = self._open_parent_fd(root_fd, rel)
        try:
            try:
                current, identity = self._read_regular_from_parent(parent, name, limit=MAX_EDIT_BYTES)
            except KitError as exc:
                if str(exc) != '文件不存在':
                    raise
                self._create_regular(root_fd, parent, rel, name, payload)
            else:
                self._replace_regular(root_fd, parent, rel, name, identity, payload)
        finally:
            os.close(parent)
        return True

    def upload_project_file_as_derived(self, project_id: str, relative_path: object,
                                       name: object, *, description: object = '',
                                       tags: object = None, readme_markdown: object = '',
                                       parent_dataset_id: object = None) -> dict[str, object]:
        """Stream one safe current-project file into the managed derived store.

        The broker never receives a user home path or file descriptor.  A
        descriptor opened below the already-owned project is read in bounded
        chunks, and both the descriptor and named file must retain the same
        identity through the upload.
        """
        path = _relative(relative_path, label='派生数据源文件')
        if self._is_protected(path):
            raise _error('不能将固定运行依赖登记为派生数据')
        if (not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 200 or
                any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise _error('派生数据名称不能为空且不得超过 200 字节')
        upload_id: str | None = None
        committed = False
        with self._project_guard(project_id), self._file_lock(project_id, path):
            references = self._project_references_unlocked(project_id)['references']
            assert isinstance(references, list)
            root_fd = self._open_project_fd(project_id)
            try:
                parent_fd, basename = self._open_parent_fd(root_fd, path)
                try:
                    initial = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
                    if (stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode) or
                            initial.st_nlink != 1):
                        raise _error('派生数据源必须是无链接的普通项目文件')
                    source_fd = os.open(basename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
                    try:
                        opened = os.fstat(source_fd)
                        if self._regular_identity(opened) != self._regular_identity(initial):
                            raise _error('派生数据源在打开时发生变化')
                        extra_meta = {}
                        if isinstance(description, str) and description.strip():
                            extra_meta['description'] = description.strip()
                        if tags is not None:
                            extra_meta['tags'] = tags
                        if isinstance(readme_markdown, str) and readme_markdown.strip():
                            extra_meta['readme_markdown'] = readme_markdown.strip()
                        if isinstance(parent_dataset_id, str) and parent_dataset_id.strip():
                            extra_meta['parent_dataset_id'] = parent_dataset_id.strip()
                        begun = self.store.request('begin_derived_upload', name=name.strip(),
                                                   expected_size=initial.st_size, expected_sha256=None,
                                                   source_refs=references, **extra_meta)
                        if (not isinstance(begun, dict) or not isinstance(begun.get('upload_id'), str) or
                                not begun['upload_id'] or begun.get('chunk_size') != 1024 * 1024):
                            raise _error('派生数据服务未返回有效上传会话')
                        upload_id = begun['upload_id']
                        digest = hashlib.sha256()
                        offset = 0
                        while True:
                            chunk = os.read(source_fd, 1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            response = self.store.request('append_derived_upload', upload_id=upload_id,
                                                          offset=offset, data=chunk)
                            if not isinstance(response, dict) or response.get('received') != offset + len(chunk):
                                raise _error('派生数据服务未确认完整上传分块')
                            offset += len(chunk)
                        final = os.fstat(source_fd)
                        named_final = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
                        if (self._regular_identity(final) != self._regular_identity(initial) or
                                self._regular_identity(named_final) != self._regular_identity(initial) or
                                offset != initial.st_size):
                            raise _error('派生数据源在上传时发生变化')
                        result = self.store.request('commit_derived_upload', upload_id=upload_id,
                                                    sha256=digest.hexdigest())
                        if not isinstance(result, dict):
                            raise _error('派生数据服务返回无效')
                        committed = True
                        files = result.get('files')
                        if not isinstance(files, list) or len(files) != 1:
                            raise _error('派生数据服务缺少固定文件引用')
                        reference = _validate_data_references(files)[0]
                        if reference['size'] != offset or reference['sha256'] != digest.hexdigest():
                            raise _error('派生数据服务返回的固定内容不匹配')
                        remembered = self._remember_derived_import(root_fd, path, reference)
                        return {**result, 'source_reference_only_after_attach': remembered}
                    finally:
                        os.close(source_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
                if upload_id is not None and not committed:
                    try:
                        self.store.request('abort_derived_upload', upload_id=upload_id)
                    except Exception:
                        # The original upload or source-integrity error is more
                        # useful.  The broker also expires abandoned sessions.
                        pass

    def derived_catalog(self, **filters: object) -> dict[str, object]:
        result = self.store.request('get_derived_catalog', **filters)
        # The first broker implementation returned the same plain record array
        # as get_dataset_catalog.  Normalize it at the local boundary so the
        # HTTP contract remains paginatable without exposing that transport
        # compatibility detail to the browser.
        if isinstance(result, list):
            return {'items': result, 'next_cursor': None}
        if not isinstance(result, dict):
            raise _error('派生数据服务返回无效')
        return result

    def dataset_files(self, dataset_id: str, **filters: object) -> dict[str, object]:
        result = self.store.request('get_dataset_files', dataset_id=dataset_id, **filters)
        if not isinstance(result, dict):
            raise _error('数据文件浏览服务返回无效')
        return result

    def save_job_artifacts_as_derived(self, run_id: str, paths: object, name: object = None) -> dict[str, object]:
        if not isinstance(run_id, str) or not isinstance(paths, list) or not paths:
            raise _error('请选择至少一个运行产物')
        if any(not isinstance(path, str) or not path for path in paths):
            raise _error('运行产物路径无效')
        if name is not None and (not isinstance(name, str) or len(name.encode('utf-8')) > 200):
            raise _error('派生数据名称无效')
        result = self.store.request('save_run_artifacts_as_derived', run_id=run_id, paths=paths, name_prefix=name)
        if not isinstance(result, dict):
            raise _error('派生数据服务返回无效')
        return result

    def grant_derived_review(self, dataset_id: object, submission_id: object) -> dict[str, object]:
        if not isinstance(dataset_id, str) or not isinstance(submission_id, str):
            raise _error('派生数据审核授权参数无效')
        result = self.store.request('grant_derived_review', dataset_id=dataset_id, submission_id=submission_id)
        if not isinstance(result, dict):
            raise _error('派生数据服务返回无效')
        return result

    def publish_derived(self, dataset_id: object) -> dict[str, object]:
        if not isinstance(dataset_id, str):
            raise _error('派生数据发布参数无效')
        result = self.store.request('publish_derived', dataset_id=dataset_id)
        if not isinstance(result, dict):
            raise _error('派生数据服务返回无效')
        return result

    def _check_tree(self, root_fd: int, *, collect: bool) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        self._walk_fd(root_fd, '', entries, collect)
        return entries

    def _walk_fd(self, parent_fd: int, prefix: str, entries: list[dict[str, object]], collect: bool) -> None:
        for name in sorted(os.listdir(parent_fd)):
            rel = f'{prefix}/{name}' if prefix else name
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _error('项目目录在扫描时发生变化')
            if stat.S_ISLNK(info.st_mode):
                raise _error(f'项目不能含软链接：{rel}')
            if stat.S_ISDIR(info.st_mode):
                if name in _SKIP_DIRECTORIES:
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent_fd)
                try:
                    after = os.fstat(descriptor)
                    if after.st_ino != info.st_ino or after.st_dev != info.st_dev:
                        raise _error('项目目录在扫描时发生变化')
                    self._walk_fd(descriptor, rel, entries, collect)
                finally:
                    os.close(descriptor)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _error(f'项目只能含无链接的普通文件：{rel}')
            if collect:
                entries.append({'path': rel, 'size': info.st_size, 'editable': not self._is_protected(rel)})

    def _tree_metadata(self, root_fd: int) -> dict[str, tuple[int, int, int, int, int]]:
        """Capture safe file identities before and after a submission snapshot."""
        result: dict[str, tuple[int, int, int, int, int]] = {}
        try:
            def visit(fd: int, prefix: str) -> None:
                for name in sorted(os.listdir(fd)):
                    rel = f'{prefix}/{name}' if prefix else name
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise _error(f'项目不能含软链接：{rel}')
                    if stat.S_ISDIR(info.st_mode):
                        if name in _SKIP_DIRECTORIES:
                            continue
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                        try:
                            visit(child, rel)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        if rel not in _TRANSIENT:
                            result[rel] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    else:
                        raise _error(f'项目含不支持的特殊文件：{rel}')
            visit(root_fd, '')
            return result
        finally:
            pass

    def _snapshot(self, project_id: str, *, result_paths: list[str] | None = None) -> tuple[Path, dict[str, bytes], list[dict[str, object]]]:
        self._broker_project(project_id)
        selected = None
        if result_paths is not None:
            if type(result_paths) is not list or len(result_paths) > MAX_FILES:
                raise _error('请选择有限数量的项目产物')
            from .web_result_files import result_path
            selected = {result_path(path) for path in result_paths}
            if len(selected) != len(result_paths):
                raise _error('产物选择不能重复')
        def included(path):
            return selected is None or not path.startswith('results/') or path in selected
        root = self._project_path(project_id)
        root_fd = self._open_project_fd(project_id)
        packed: dict[str, bytes] = {}
        total = 0
        try:
            self._check_tree(root_fd, collect=False)
            initial = {path: value for path, value in self._tree_metadata(root_fd).items() if included(path)}
            imports = self._derived_imports(root_fd)
            reference_only = {}
            if imports:
                declared = self._project_references_unlocked(project_id)['references']
                catalog = self._catalog_datasets()
                for path, reference in imports.items():
                    if reference in declared and Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
                        if reference not in catalog.get(str(reference['dataset_id']), []):
                            raise _error('项目派生数据不存在或当前账号未获授权')
                        reference_only[path] = reference
            def visit(fd: int, prefix: str) -> None:
                nonlocal total
                for name in sorted(os.listdir(fd)):
                    rel = f'{prefix}/{name}' if prefix else name
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if name in _SKIP_DIRECTORIES:
                            continue
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                        try:
                            visit(child, rel)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        if rel in _TRANSIENT or info.st_nlink != 1 or not included(rel):
                            continue
                        if rel in reference_only:
                            reference = reference_only[rel]
                            if info.st_size != reference['size']:
                                raise _error('本地产物与已保存派生版本不同，请保存新版本后重新引用')
                            source = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                            try:
                                digest = hashlib.sha256()
                                while True:
                                    block = os.read(source, 1024 * 1024)
                                    if not block:
                                        break
                                    digest.update(block)
                                if digest.hexdigest() != reference['sha256'] or self._regular_identity(os.fstat(source)) != self._regular_identity(info):
                                    raise _error('本地产物与已保存派生版本不同，请保存新版本后重新引用')
                            finally:
                                os.close(source)
                            continue
                        if len(packed) >= MAX_FILES or info.st_size > MAX_FILE_BYTES:
                            raise _error('提交文件数或单个文件大小超过上限')
                        file_fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
                        try:
                            after = os.fstat(file_fd)
                            if (after.st_ino != info.st_ino or after.st_dev != info.st_dev or after.st_nlink != 1 or after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns or after.st_ctime_ns != info.st_ctime_ns):
                                raise _error('提交文件在读取时发生变化')
                            chunks = []
                            remaining = info.st_size + 1
                            while remaining:
                                chunk = os.read(file_fd, min(1024 * 1024, remaining))
                                if not chunk:
                                    break
                                chunks.append(chunk)
                                remaining -= len(chunk)
                            content = b''.join(chunks)
                        finally:
                            os.close(file_fd)
                        final = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (len(content) != info.st_size or final.st_ino != info.st_ino or final.st_dev != info.st_dev or final.st_size != info.st_size or final.st_mtime_ns != info.st_mtime_ns or final.st_ctime_ns != info.st_ctime_ns):
                            raise _error('提交文件在读取时发生变化')
                        total += len(content)
                        if total > MAX_TOTAL_BYTES:
                            raise _error('提交文件总量超过上限')
                        packed[rel] = content
                    else:
                        raise _error(f'项目含不支持的特殊文件：{rel}')
            visit(root_fd, '')
            if selected is not None and selected - set(packed) - set(reference_only):
                raise _error('所选产物已变化或已移除，请刷新后重新选择')
        finally:
            os.close(root_fd)
        verification_fd = self._open_project_fd(project_id)
        try:
            changed = initial != {path: value for path, value in self._tree_metadata(verification_fd).items() if included(path)}
        finally:
            os.close(verification_fd)
        if changed:
            raise _error('提交冲突：IDE 在快照期间修改了项目，请重试')
        references: list[dict[str, object]] = []
        raw = packed.get('DATA-REFERENCES.json')
        if raw is not None:
            try:
                candidate = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                raise _error('DATA-REFERENCES.json 必须是 UTF-8 JSON 列表') from exc
            if not isinstance(candidate, list):
                raise _error('DATA-REFERENCES.json 必须是 JSON 列表')
            references = candidate
        return root, packed, references

    def _sync_usage(self, project_id: str) -> None:
        """Send metadata from this user's own workspace, never browse other homes.

        The index supplements immutable submissions.  A failed refresh must not
        turn a successful file save into an apparent failure; cleanup retains
        original bytes and its report states this coverage limitation.
        """
        from .web_lifecycle import AssetLifecycle
        try:
            with self._project_guard(project_id):
                _root, files, references = self._snapshot(project_id)
                usage = AssetLifecycle.extract_usage(files, references)
            self.store.request('record_project_usage', project_id=project_id, usage=usage)
        except KitError:
            pass

    def submission_preview(self, project_id: str, payload: dict) -> dict:
        from .web_workspace_flow import preview
        return preview(self, project_id, payload)

    def apply_task_materials(self, project_id: str, payload: dict) -> dict:
        from .web_workspace_materials import apply
        return apply(self, project_id, payload)

    def submission_prepare(self, project_id: str, payload: dict) -> dict:
        from .web_workspace_flow import prepare
        return prepare(self, project_id, payload)

    def project_trial(self, project_id: str, payload: dict) -> dict:
        raise _error('网页运行已移除，请在自己的 IDE 中运行并保存产物')

    def enable_experiments(self, project_id: str) -> dict:
        from .web_experiments import enable
        return enable(self, project_id)

    def update_experiment_helper(self, project_id: str) -> dict:
        from .web_experiments import update_helper
        return update_helper(self, project_id)

    def experiment_catalog(self, project_id: str) -> dict:
        from .web_experiments import catalog
        return catalog(self, project_id)

    def save_experiment(self, project_id: str, payload: dict) -> dict:
        from .web_experiments import save
        return save(self, project_id, payload)

    def delete_experiment(self, project_id: str, payload: dict) -> dict:
        from .web_experiments import delete
        return delete(self, project_id, payload)

    def delete_experiment_result(self, project_id: str, payload: dict) -> dict:
        from .web_experiments import delete_result
        return delete_result(self, project_id, payload)

    def submission_experiments(self, submission_id: str, *, review: bool = False) -> dict:
        from .web_experiments import summarize_snapshot
        if review and self.principal.role != 'teacher':
            raise _error('只有教师可以审核其他账号的固定提交')
        record = self.store.request('review_submission' if review else 'get_submission', submission_id=submission_id)
        return {'submission_id': submission_id, **summarize_snapshot(record['files'])}

    def project_resources(self, project_id: str) -> dict:
        from .web_project_resources import summarize
        return summarize(self, project_id)

    def submit(self, project_id: str, kind: str, *, assignment_request_id: str | None = None,
               assignment_fingerprint: str | None = None) -> dict[str, object]:
        if kind not in {'minor', 'major'}:
            raise _error('提交类型必须是 minor 或 major')
        if (assignment_request_id is None) != (assignment_fingerprint is None):
            raise _error('任务基线请求标记不完整')
        if assignment_request_id is not None:
            if (not isinstance(assignment_request_id, str) or not isinstance(assignment_fingerprint, str) or
                    len(assignment_fingerprint) != 64):
                raise _error('任务基线请求标记无效')
        with self._project_guard(project_id):
            _root, files, references = self._snapshot(project_id)
            if assignment_request_id is None:
                result = self.store.request('submit', project_id=project_id, kind=kind, files=files,
                                            data_references=references)
            else:
                result = self.store.request('submit_assignment_baseline', project_id=project_id, kind=kind,
                                            files=files, data_references=references,
                                            request_id=assignment_request_id,
                                            fingerprint=assignment_fingerprint)
        if not isinstance(result, dict):
            raise _error('提交服务返回无效')
        self._sync_usage(project_id)
        return result

    def list_submissions(self, project_id: str | None = None, *, include_minor: bool = True) -> list[dict[str, object]]:
        result = self.store.request('list_submissions', project_id=project_id, include_minor=include_minor)
        if not isinstance(result, list):
            raise _error('提交服务返回无效')
        return result

    def list_submission_projects(self, **filters: object) -> dict[str, object]:
        result = self.store.request('list_submission_projects', **filters)
        if not isinstance(result, dict):
            raise _error('提交服务返回无效')
        return result

    def list_project_versions(self, project_id: str, **filters: object) -> dict[str, object]:
        result = self.store.request('list_project_versions', project_id=project_id, **filters)
        if not isinstance(result, dict):
            raise _error('提交服务返回无效')
        return result

    def get_submission(self, submission_id: str, relative_path: str | None = None, *, review: bool = False) -> dict[str, object]:
        result = self.store.request('review_submission' if review else 'get_submission', submission_id=submission_id)
        if not isinstance(result, dict):
            raise _error('提交服务返回无效')
        if relative_path is None:
            return {key: value for key, value in result.items() if key != 'files'}
        files = result.get('files')
        path = _relative(relative_path)
        if not isinstance(files, dict) or not isinstance(files.get(path), bytes):
            raise _error('提交中没有该文件')
        content = files[path]
        if len(content) > MAX_EDIT_BYTES:
            raise _error('提交文件超过 Web 查看上限')
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            text = None
        return {'submission_id': submission_id, 'path': path, 'sha256': _digest(content),
                'size': len(content), 'text': text, 'binary': text is None}

    def diff_submissions(self, before: str, after: str, relative_path: str) -> dict[str, object]:
        import difflib
        left = self.get_submission(before, relative_path)
        right = self.get_submission(after, relative_path)
        if left['binary'] or right['binary']:
            raise _error('二进制文件不能在 Web 中比较')
        diff = ''.join(difflib.unified_diff(str(left['text']).splitlines(True), str(right['text']).splitlines(True),
                                            fromfile=before, tofile=after, n=3))
        if len(diff.encode('utf-8')) > MAX_EDIT_BYTES:
            raise _error('差异超过 Web 查看上限')
        return {'path': _relative(relative_path), 'before': before, 'after': after, 'diff': diff}

    # ---- Local Algorithms (personal workspace scratch algorithms) ----
    def _local_algorithms_root(self) -> Path:
        root = self.root / '.researchkit-local-algorithms'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def _read_local_algorithm_bundle(self, algo_id: str) -> dict[str, object]:
        root = self._local_algorithms_root()
        target = safe_path(root, algo_id)
        if target.parent != root or not target.is_dir():
            raise _error('本地算法不存在')
        meta_path = target / 'metadata.json'
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except Exception:
                meta = {}
        files = {}
        for sub in sorted(target.rglob('*')):
            if sub.is_file() and sub.name != 'metadata.json':
                rel = sub.relative_to(target).as_posix()
                files[rel] = sub.read_bytes()
        if 'plugin.yaml' not in files or 'plugin.py' not in files:
            raise _error('本地算法缺少 plugin.yaml 或 plugin.py')
        p_meta = yaml.safe_load(files['plugin.yaml'].decode('utf-8')) or {}
        return {
            'id': algo_id,
            'name': str(p_meta.get('name') or meta.get('name') or algo_id),
            'version': str(p_meta.get('version') or '1.0.0'),
            'description': str(meta.get('description') or p_meta.get('description') or ''),
            'tags': meta.get('tags') if isinstance(meta.get('tags'), list) else [],
            'parameters': p_meta.get('parameters') if isinstance(p_meta.get('parameters'), dict) else {},
            'inputs': p_meta.get('inputs') if isinstance(p_meta.get('inputs'), dict) else {},
            'outputs': p_meta.get('outputs') if isinstance(p_meta.get('outputs'), dict) else {},
            'updated_at': str(meta.get('updated_at') or ''),
            'plugin_yaml': files['plugin.yaml'].decode('utf-8', errors='replace'),
            'plugin_py': files['plugin.py'].decode('utf-8', errors='replace'),
            'files': files,
        }

    def list_local_algorithms(self) -> list[dict[str, object]]:
        root = self._local_algorithms_root()
        items = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith('.'):
                continue
            try:
                bundle = self._read_local_algorithm_bundle(child.name)
                bundle.pop('files', None)
                items.append(bundle)
            except KitError:
                continue
        items.sort(key=lambda r: str(r.get('updated_at') or ''), reverse=True)
        return items

    def create_local_algorithm(self, payload: object) -> dict[str, object]:
        if type(payload) is not dict:
            raise _error('本地算法请求无效')
        from .files import identifier
        raw_name = payload.get('name')
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise _error('算法标识名不能为空')
        name = identifier(raw_name.strip())
        version = str(payload.get('version') or '1.0.0').strip()
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise _error('算法版本必须为 x.y.z 格式')
        description = str(payload.get('description') or '').strip()
        raw_tags = payload.get('tags', [])
        if isinstance(raw_tags, str):
            tags = [t.strip() for t in re.split(r'[,，\n]+', raw_tags) if t.strip()]
        elif isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        else:
            tags = []

        from .files import reference as _ref_check
        from .model import check_type as _check_param_type

        type_aliases = {'int': 'integer', 'float': 'number', 'bool': 'boolean', 'str': 'string'}

        def _clean_params(raw):
            if raw is None:
                return {}
            if isinstance(raw, str):
                raw = yaml.safe_load(raw) if raw.strip() else {}
            if not isinstance(raw, dict):
                raise _error('本地算法 parameters 必须是映射对象')
            out = {}
            for pk, rule in raw.items():
                identifier(str(pk))
                if not isinstance(rule, dict):
                    raise _error(f'参数 {pk} 规则必须是映射')
                r_copy = dict(rule)
                raw_type = str(r_copy.get('type', 'any'))
                norm_type = type_aliases.get(raw_type, raw_type)
                r_copy['type'] = norm_type
                if 'default' in r_copy:
                    if not _check_param_type(norm_type, r_copy['default']):
                        raise _error(f'参数 {pk} 默认值类型与 {norm_type} 不符')
                else:
                    sample = {'integer': 0, 'number': 0.0, 'string': '', 'boolean': False, 'mapping': {}, 'sequence': []}.get(norm_type)
                    _check_param_type(norm_type, sample)
                out[str(pk)] = r_copy
            return out

        def _clean_ports(raw, label):
            if raw is None:
                return {}
            if isinstance(raw, str):
                raw = yaml.safe_load(raw) if raw.strip() else {}
            if not isinstance(raw, dict):
                raise _error(f'本地算法 {label} 必须是映射对象')
            out = {}
            for port, cref in raw.items():
                identifier(str(port))
                if not isinstance(cref, str):
                    raise _error(f'{label}.{port} 契约引用必须是字符串')
                _ref_check(cref.strip())
                out[str(port)] = cref.strip()
            return out

        params_obj = _clean_params(payload.get('parameters')) if 'parameters' in payload else None
        inputs_obj = _clean_ports(payload.get('inputs'), 'inputs') if 'inputs' in payload else None
        outputs_obj = _clean_ports(payload.get('outputs'), 'outputs') if 'outputs' in payload else None

        plugin_yaml_text = payload.get('plugin_yaml')
        if not isinstance(plugin_yaml_text, str) or not plugin_yaml_text.strip():
            doc = {
                'schema': 'hydro.plugin/v1',
                'name': name,
                'version': version,
                'description': description,
                'entry': 'plugin.py',
                'parameters': params_obj if params_obj is not None else {},
                'inputs': inputs_obj if inputs_obj is not None else {},
                'outputs': outputs_obj if outputs_obj is not None else {},
            }
            plugin_yaml_text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
        else:
            parsed = yaml.safe_load(plugin_yaml_text)
            if not isinstance(parsed, dict):
                raise _error('plugin.yaml 必须是有效 YAML 映射')
            parsed['schema'] = parsed.get('schema') or 'hydro.plugin/v1'
            parsed['entry'] = parsed.get('entry') or 'plugin.py'
            parsed['name'] = name
            parsed['version'] = version
            parsed['parameters'] = params_obj if params_obj is not None else _clean_params(parsed.get('parameters'))
            parsed['inputs'] = inputs_obj if inputs_obj is not None else _clean_ports(parsed.get('inputs'), 'inputs')
            parsed['outputs'] = outputs_obj if outputs_obj is not None else _clean_ports(parsed.get('outputs'), 'outputs')
            plugin_yaml_text = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)

        plugin_py_text = payload.get('plugin_py')
        if not isinstance(plugin_py_text, str) or not plugin_py_text.strip():
            plugin_py_text = (
                'def run(inputs, parameters, context):\n'
                '    # 在此编写本地算法逻辑\n'
                '    return {}\n'
            )

        algo_id = _safe_component(f'{name}-{version}') + '-' + uuid.uuid4().hex[:8]
        target = self._local_algorithms_root() / algo_id
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        atomic_text(target / 'plugin.yaml', plugin_yaml_text)
        atomic_text(target / 'plugin.py', plugin_py_text)
        try:
            plugin_doc = yaml.safe_load(plugin_yaml_text)
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            raise _error('plugin.yaml 必须是有效 YAML 映射') from exc
        if not isinstance(plugin_doc, dict):
            raise _error('plugin.yaml 必须是有效 YAML 映射')
        # A fully empty platform library still needs one portable, well-known
        # contract for a beginner's local algorithm.  We intentionally ship
        # only this exact contract; other declared contracts remain explicit
        # dependencies and must be supplied by an approved platform resource.
        declared_contracts = set()
        for section in ('inputs', 'outputs'):
            ports = plugin_doc.get(section)
            if isinstance(ports, dict):
                declared_contracts.update(value for value in ports.values() if isinstance(value, str))
        for ref in sorted(declared_contracts & set(_FOUNDATION_CONTRACTS)):
            contract_name, contract_version = ref.split('@', 1)
            contract_path = target / 'contracts' / contract_name / f'{contract_version}.yaml'
            contract_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_text(contract_path, _FOUNDATION_CONTRACTS[ref].decode('utf-8'))
        atomic_text(
            target / 'metadata.json',
            json.dumps({'name': name, 'version': version, 'description': description, 'tags': tags, 'updated_at': now}, ensure_ascii=False, indent=2),
        )
        res = self._read_local_algorithm_bundle(algo_id)
        res.pop('files', None)
        return res

    def delete_local_algorithm(self, algo_id: str) -> dict[str, object]:
        if not isinstance(algo_id, str) or not algo_id.strip():
            raise _error('本地算法 ID 无效')
        root = self._local_algorithms_root()
        target = safe_path(root, algo_id.strip())
        if target.parent != root or not target.is_dir():
            raise _error('本地算法不存在')
        import shutil
        shutil.rmtree(target)
        return {'id': algo_id.strip(), 'deleted': True}

    def copy_submission_for_test(self, submission_id: str) -> dict[str, object]:
        """Copy a fixed submission into ly's workspace as '[测试] <name>-<version>' for direct testing."""
        if self.principal.role != 'teacher' or self.principal.username != 'ly':
            raise _error('只有教师 ly 可以复制到自己的工作区测试')
        sub = self.store.request('review_submission', submission_id=submission_id)
        if not isinstance(sub, dict) or not isinstance(sub.get('files'), dict):
            raise _error('固定提交读取无效')
        from .web_broker import _decode_files
        raw_files = sub['files']
        if raw_files and isinstance(next(iter(raw_files.values())), str):
            files = _decode_files(raw_files)
        else:
            files = raw_files
        proj_info = sub.get('project') or {}
        orig_name = str(proj_info.get('name') or 'submission')
        orig_ver = str(sub.get('version') or '1.0')
        copy_name = f'[测试] {orig_name}-{orig_ver}'
        if len(copy_name.encode('utf-8')) > 200:
            copy_name = copy_name[:80]

        record = self.store.request('create_project', name=copy_name)
        project_id = record['project_id']
        destination = self._new_path(copy_name, project_id)
        temporary = Path(tempfile.mkdtemp(prefix='.rk-test-', dir=self.projects_root))
        try:
            for rel, content in files.items():
                rel_clean = _relative(rel)
                target = temporary / rel_clean
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open('xb') as stream:
                    stream.write(content)
            try:
                doc = yaml.safe_load(files.get('project.yaml', b'').decode('utf-8')) or {}
            except Exception:
                doc = {}
            if isinstance(doc, dict) and doc.get('schema') == 'researchkit.project/v1':
                doc['name'] = copy_name
                doc['origin'] = {'kind': 'web-review-test-copy/v1', 'source_submission_id': submission_id}
                write_yaml(temporary / 'project.yaml', doc)
            refs = ((sub.get('manifest') or {}).get('data_references') or {}).get('references')
            if isinstance(refs, list) and refs:
                atomic_text(temporary / _DATA_REFERENCES_NAME, json.dumps(refs, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
            install_runtime(temporary)
            seal_lock(temporary)
            os.chmod(temporary, 0o700)
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                import shutil
                shutil.rmtree(temporary)
        with self._state_lock():
            projects = self._load_state()
            projects[project_id] = {'path': destination.relative_to(self.root).as_posix(), 'name': copy_name}
            self._save_state(projects)
        self.store.request('review_register_test_copy', submission_id=submission_id, project_id=project_id)
        self._sync_usage(project_id)
        return self.describe_project(project_id)
