"""Static reads of the administrator-installed shared library; never import code."""
from __future__ import annotations
import hashlib
import os
import stat
from pathlib import Path
from typing import Any
import yaml
from .files import KitError, identifier, reference
from .web_store import MAX_FILE_BYTES

_MAX_LIBRARY_FILES = 512
_MAX_LIBRARY_BYTES = 4 * 1024 * 1024
_TYPES = {'number', 'integer', 'string', 'boolean', 'mapping', 'sequence', 'any'}
_fail = KitError

def revision(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        encoded = path.encode('utf-8')
        digest.update(len(encoded).to_bytes(4, 'big'))
        digest.update(encoded)
        digest.update(len(files[path]).to_bytes(8, 'big'))
        digest.update(files[path])
    return digest.hexdigest()


class SharedLibrary:
    def __init__(self, root: Path | str):
        self.library_root = Path(root)

    # Shared library -----------------------------------------------------
    def _library_files(self, root: Path) -> dict[str, bytes]:
        """Read a small shared-library tree through no-follow directory FDs."""
        if not root.is_absolute():
            raise _fail('共享库路径不安全')
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            for part in root.parts[1:]:
                try:
                    info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise _fail('共享算法目录不存在') from exc
                except OSError as exc:
                    raise _fail('共享算法目录不可读取') from exc
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                        or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                    raise _fail('共享算法目录不安全')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                try:
                    observed = os.fstat(child)
                    if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
                        raise _fail('共享算法目录在读取时变化')
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            result: dict[str, bytes] = {}
            total = 0
            def visit(folder_fd: int, prefix: str) -> None:
                nonlocal total
                for name in sorted(os.listdir(folder_fd)):
                    if name == '__pycache__':
                        continue
                    rel = f'{prefix}/{name}' if prefix else name
                    info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise _fail(f'共享算法不能含链接：{rel}')
                    if stat.S_ISDIR(info.st_mode):
                        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                            raise _fail(f'共享算法目录所有者不安全：{rel}')
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=folder_fd)
                        try:
                            observed = os.fstat(child)
                            if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
                                raise _fail('共享算法目录在读取时变化')
                            visit(child, rel)
                        finally:
                            os.close(child)
                        continue
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                        raise _fail(f'共享算法只能含当前账号或 root 的普通文件：{rel}')
                    if info.st_size > MAX_FILE_BYTES or len(result) >= _MAX_LIBRARY_FILES:
                        raise _fail('共享算法文件数或文件大小超过上限')
                    file_fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=folder_fd)
                    try:
                        observed = os.fstat(file_fd)
                        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or (observed.st_dev, observed.st_ino, observed.st_size) != (info.st_dev, info.st_ino, info.st_size):
                            raise _fail('共享算法文件在读取时变化')
                        chunks: list[bytes] = []
                        remaining = info.st_size + 1
                        while remaining:
                            chunk = os.read(file_fd, min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        value = b''.join(chunks)
                        final = os.fstat(file_fd)
                        if len(value) != info.st_size or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                            raise _fail('共享算法文件在读取时变化')
                    finally:
                        os.close(file_fd)
                    total += len(value)
                    if total > _MAX_LIBRARY_BYTES:
                        raise _fail('共享算法总大小超过上限')
                    result[rel] = value
            visit(descriptor, '')
            return result
        finally:
            os.close(descriptor)

    def _safe_yaml(self, data: bytes, label: str) -> dict[str, Any]:
        try:
            document = yaml.safe_load(data.decode('utf-8'))
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            raise _fail(f'{label} 必须是 UTF-8 YAML 映射') from exc
        if not isinstance(document, dict):
            raise _fail(f'{label} 顶层必须是映射')
        return document

    def _plugin_from_files(self, files: dict[str, bytes], ref: str) -> dict[str, Any]:
        meta = self._safe_yaml(files.get('plugin.yaml', b''), 'plugin.yaml')
        if meta.get('schema') != 'hydro.plugin/v1' or meta.get('entry') != 'plugin.py':
            raise _fail(f'{ref} 不是受支持的算法')
        if f"{meta.get('name')}@{meta.get('version')}" != ref:
            raise _fail(f'共享算法身份不匹配：{ref}')
        try:
            reference(ref)
        except KitError as exc:
            raise _fail('算法引用必须是精确版本') from exc
        for key in ('inputs', 'outputs', 'parameters'):
            if not isinstance(meta.get(key), dict):
                raise _fail(f'{ref} 的 {key} 必须是映射')
        for ports in (meta['inputs'], meta['outputs']):
            for name, contract_ref in ports.items():
                try:
                    identifier(name)
                    reference(contract_ref)
                except KitError as exc:
                    raise _fail(f'{ref} 的端口声明无效') from exc
        if 'plugin.py' not in files:
            raise _fail(f'{ref} 缺少 plugin.py')
        return meta

    def _plugin_source(self, ref: str) -> tuple[dict[str, Any], dict[str, bytes], str]:
        try:
            name, version = reference(ref)
        except KitError as exc:
            raise _fail('算法引用必须是名称@x.y.z') from exc
        root = self.library_root / 'algorithms' / name / version
        files = self._library_files(root)
        meta = self._plugin_from_files(files, ref)
        h = revision(files)
        return meta, files, h

    def _contract_source(self, ref: str) -> tuple[dict[str, Any], bytes]:
        try:
            name, version = reference(ref)
        except KitError as exc:
            raise _fail('契约引用必须是名称@x.y.z') from exc
        folder = self.library_root / 'contracts' / name
        files = self._library_files(folder)
        name_in_folder = f'{version}.yaml'
        data = files.get(name_in_folder)
        if data is None:
            raise _fail(f'共享契约不存在：{ref}')
        doc = self._safe_yaml(data, '契约')
        if doc.get('schema') != 'hydro.contract/v1' or doc.get('id') != ref or doc.get('type') not in _TYPES:
            raise _fail(f'共享契约身份或类型无效：{ref}')
        if not isinstance(doc.get('description', ''), str):
            raise _fail(f'共享契约描述无效：{ref}')
        return doc, data

    def _template_from_files(self, files: dict[str, bytes], ref: str) -> dict[str, Any]:
        """Validate a project template as inert data, without importing it."""
        document = self._safe_yaml(files.get('project.yaml', b''), 'project.yaml')
        if (document.get('schema') != 'researchkit.project/v1' or
                document.get('mode') != 'python' or document.get('entry', 'case.py') != 'case.py'):
            raise _fail(f'{ref} 不是受支持的 Python 项目模板')
        for key in ('name', 'plugins', 'parameters', 'data', 'requires'):
            if key not in document:
                raise _fail(f'{ref} 的 project.yaml 缺少 {key}')
        if (not isinstance(document['name'], str) or not isinstance(document['plugins'], dict) or
                not isinstance(document['parameters'], dict) or not isinstance(document['data'], dict) or
                not isinstance(document['requires'], dict) or 'case.py' not in files):
            raise _fail(f'{ref} 的 project.yaml 或 case.py 无效')
        # A Web template is copied as source data.  Its runtime launcher and
        # generated run state belong to a separately created workspace.
        forbidden = {'task', 'task.py'}
        if any(path in forbidden or path.startswith('.researchkit/runtime/') or path.startswith('_runs/')
               for path in files):
            raise _fail(f'{ref} 不能包含运行时或 task 启动器')
        return document

    def _template_source(self, ref: str) -> tuple[dict[str, Any], dict[str, bytes], str]:
        try:
            name, version = reference(ref)
        except KitError as exc:
            raise _fail('模板引用必须是名称@x.y.z') from exc
        root = self.library_root / 'cases' / name / version
        files = self._library_files(root)
        document = self._template_from_files(files, ref)
        return document, files, revision(files)

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        algorithms = self.library_root / 'algorithms'
        contracts = self.library_root / 'contracts'
        # Validate the complete roots through descriptor traversal before
        # listing children by display path.
        self._library_files(algorithms)
        self._library_files(contracts)
        plugins: list[dict[str, Any]] = []
        contract_rows: list[dict[str, Any]] = []
        for base, is_plugin in ((algorithms, True), (contracts, False)):
            try:
                entries = sorted(os.scandir(base), key=lambda item: item.name)
            except OSError as exc:
                raise _fail('共享库目录不存在或不可读取') from exc
            for item in entries:
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise _fail('共享库包含链接或非目录条目')
                try:
                    identifier(item.name)
                    versions = sorted(os.scandir(item.path), key=lambda child: child.name)
                except (OSError, KitError) as exc:
                    raise _fail('共享库版本目录无效') from exc
                for version in versions:
                    vi = version.stat(follow_symlinks=False)
                    if is_plugin:
                        if stat.S_ISLNK(vi.st_mode) or not stat.S_ISDIR(vi.st_mode):
                            raise _fail('共享库版本目录不安全')
                        ref = f'{item.name}@{version.name}'
                        meta, _files, _hash = self._plugin_source(ref)
                        plugins.append({'ref': ref, 'name': meta['name'], 'version': meta['version'],
                                        'inputs': meta['inputs'], 'outputs': meta['outputs'],
                                        'parameters': meta['parameters']})
                    else:
                        if stat.S_ISLNK(vi.st_mode) or not stat.S_ISREG(vi.st_mode) or not version.name.endswith('.yaml'):
                            raise _fail('共享契约文件不安全')
                        ref = f'{item.name}@{version.name[:-5]}'
                        doc, _data = self._contract_source(ref)
                        contract_rows.append({'id': ref, 'type': doc['type'], 'description': doc.get('description', '')})
        templates: list[dict[str, Any]] = []
        cases = self.library_root / 'cases'
        try:
            info = os.lstat(cases)
        except FileNotFoundError:
            # Small test and migration libraries predate project templates.
            pass
        except OSError as exc:
            raise _fail('共享模板目录不可读取') from exc
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _fail('共享模板目录不安全')
            self._library_files(cases)
            try:
                entries = sorted(os.scandir(cases), key=lambda item: item.name)
            except OSError as exc:
                raise _fail('共享模板目录不可读取') from exc
            for item in entries:
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise _fail('共享模板包含链接或非目录条目')
                try:
                    identifier(item.name)
                    versions = sorted(os.scandir(item.path), key=lambda child: child.name)
                except (OSError, KitError) as exc:
                    raise _fail('共享模板版本目录无效') from exc
                for version in versions:
                    vi = version.stat(follow_symlinks=False)
                    if stat.S_ISLNK(vi.st_mode) or not stat.S_ISDIR(vi.st_mode):
                        raise _fail('共享模板版本目录不安全')
                    ref = f'{item.name}@{version.name}'
                    _document, _files, _hash = self._template_source(ref)
                    templates.append({'ref': ref, 'name': item.name, 'version': version.name})
        return {'plugins': plugins, 'contracts': contract_rows, 'templates': templates}
