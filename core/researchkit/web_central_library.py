"""Immutable central copies of the administrator supplied Web library.

The command line library remains on disk for command line users, but the Web
broker must not serve a moving filesystem tree.  This module performs one
careful, transactional import of that trusted tree and subsequently serves
only the copied SQLite records.  A failed import leaves no completion marker,
so a later healthy start can retry without treating a partial import as a
library.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files import KitError, reference
from .web_library import SharedLibrary, revision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files_revision(kind: str, ref: str, files: dict[str, bytes]) -> str:
    """Give the import report a domain-separated immutable identity."""
    return hashlib.sha256((kind + '\n' + ref + '\n' + revision(files)).encode('utf-8')).hexdigest()


def _materialize(rows: list[Any], label: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for row in rows:
        path, value = str(row['path']), bytes(row['content'])
        if path in result or len(value) != int(row['size']) or _hash(value) != str(row['sha256']):
            raise KitError(f'{label}存储已损坏')
        result[path] = value
    if not result:
        raise KitError(f'{label}文件不存在')
    return result


def _object_exists(connection: Any, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone() is not None


class CentralLibrary:
    """Read central builtins and make the one-time trusted import if needed."""

    _MARKER = 'builtin-import-v1'

    def __init__(self, store, library_root: Path | str):
        self.store = store
        self.library_root = Path(library_root)
        self._initialize()
        self._ensure_imported()

    @classmethod
    def already_imported(cls, store) -> bool:
        """Check the marker without touching a source-library path."""
        with store._connection() as con:
            if not _object_exists(con, 'web_central_library_state'):
                return False
            return con.execute(
                'SELECT 1 FROM web_central_library_state WHERE marker=?', (cls._MARKER,)
            ).fetchone() is not None

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute('''CREATE TABLE IF NOT EXISTS web_central_library_state (
                    marker TEXT PRIMARY KEY, source_revision TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                )''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_central_builtin_assets (
                    kind TEXT NOT NULL CHECK(kind IN ('algorithm','template')),
                    ref TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
                    source_revision TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    origin TEXT NOT NULL CHECK(origin='builtin'), imported_at TEXT NOT NULL,
                    PRIMARY KEY(kind,ref)
                )''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_central_builtin_files (
                    kind TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    PRIMARY KEY(kind,ref,path),
                    FOREIGN KEY(kind,ref) REFERENCES web_central_builtin_assets(kind,ref)
                        ON DELETE CASCADE
                )''')
                con.execute('''CREATE TABLE IF NOT EXISTS web_central_builtin_contracts (
                    contract_ref TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL,
                    contract_json TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='builtin')
                )''')
                # The content normalizer is introduced together with this
                # release.  Keep the import narrowly coupled to its explicit
                # public API rather than duplicating its migration logic.
                from .web_content import normalize
                normalize(con, 'web_central_builtin_files')
                normalize(con, 'web_central_builtin_contracts')
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    def _complete(self) -> bool:
        with self.store._connection() as con:
            return con.execute(
                'SELECT 1 FROM web_central_library_state WHERE marker=?', (self._MARKER,)
            ).fetchone() is not None

    def _read_static(self) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, tuple[dict[str, Any], bytes]], str]:
        """Read every source byte before opening the destination transaction."""
        library = SharedLibrary(self.library_root)
        catalog = library.catalog()
        contracts: dict[str, tuple[dict[str, Any], bytes]] = {}
        for row in catalog['contracts']:
            ref = str(row['id'])
            document, data = library._contract_source(ref)
            contracts[ref] = (document, data)

        assets: dict[tuple[str, str], dict[str, Any]] = {}
        for row in catalog['plugins']:
            ref = str(row['ref'])
            metadata, source_files, _source_hash = library._plugin_source(ref)
            dependencies = sorted(set(metadata['inputs'].values()) | set(metadata['outputs'].values()))
            files = {'plugin/' + path: data for path, data in source_files.items()}
            for contract_ref in dependencies:
                try:
                    _document, data = contracts[contract_ref]
                    contract_name, contract_version = reference(contract_ref)
                except (KeyError, KitError) as exc:
                    raise KitError(f'共享算法依赖的契约不存在：{contract_ref}') from exc
                files[f'contracts/{contract_name}/{contract_version}.yaml'] = data
            name, version = reference(ref)
            assets[('algorithm', ref)] = {
                'name': name, 'version': version, 'metadata': {
                    'inputs': metadata['inputs'], 'outputs': metadata['outputs'],
                    'parameters': metadata['parameters'],
                }, 'files': files,
            }
        for row in catalog['templates']:
            ref = str(row['ref'])
            document, files, _source_hash = library._template_source(ref)
            name, version = reference(ref)
            assets[('template', ref)] = {
                'name': name, 'version': version,
                'metadata': {'project': document}, 'files': files,
            }
        source = {
            'assets': [(kind, ref, _files_revision(kind, ref, value['files']))
                       for (kind, ref), value in sorted(assets.items())],
            'contracts': [(ref, _hash(data)) for ref, (_doc, data) in sorted(contracts.items())],
        }
        return assets, contracts, hashlib.sha256(_json(source).encode('utf-8')).hexdigest()

    def _published_conflicts(self, con: Any, assets: dict[tuple[str, str], dict[str, Any]],
                             contracts: dict[str, tuple[dict[str, Any], bytes]]) -> None:
        """Existing published refs may coexist only as byte-identical copies.

        This matters for a database upgraded after an older Web release had
        already published a resource with the same exact ref as a bundled
        resource.  There is never an implicit winner for differing bytes.
        """
        if _object_exists(con, 'web_asset_published') and _object_exists(con, 'web_asset_published_files'):
            for (kind, ref), item in assets.items():
                published = con.execute(
                    'SELECT 1 FROM web_asset_published WHERE kind=? AND ref=?', (kind, ref)
                ).fetchone()
                if published is None:
                    continue
                rows = con.execute(
                    'SELECT path,sha256,size,content FROM web_asset_published_files '
                    'WHERE kind=? AND ref=? ORDER BY path', (kind, ref)
                ).fetchall()
                if _materialize(rows, '已发布资产') != item['files']:
                    raise KitError(f'已发布资产与内置资源使用相同版本但内容不同：{ref}')
        if _object_exists(con, 'web_asset_contracts'):
            for ref, (document, data) in contracts.items():
                row = con.execute(
                    'SELECT sha256,content,contract_json FROM web_asset_contracts WHERE contract_ref=?', (ref,)
                ).fetchone()
                if row is None:
                    continue
                try:
                    known = json.loads(str(row['contract_json']))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise KitError('已发布契约存储已损坏') from exc
                published_data = bytes(row['content'])
                if _hash(published_data) != row['sha256']:
                    raise KitError('已发布契约存储已损坏')
                parsed = SharedLibrary(self.library_root)._safe_yaml(published_data, '已发布契约')
                if known != document or parsed != document:
                    raise KitError(f'已发布契约与内置契约使用相同版本但定义不同：{ref}')

    def _ensure_imported(self) -> None:
        # Do this cheap marker read first.  Once complete, *no* source path is
        # traversed again; installed trees remain CLI-only compatibility data.
        if self._complete():
            return
        assets, contracts, source_revision = self._read_static()
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                if con.execute('SELECT 1 FROM web_central_library_state WHERE marker=?', (self._MARKER,)).fetchone():
                    con.execute('COMMIT')
                    return
                existing = con.execute('SELECT count(*) FROM web_central_builtin_assets').fetchone()[0]
                existing_contracts = con.execute('SELECT count(*) FROM web_central_builtin_contracts').fetchone()[0]
                if existing or existing_contracts:
                    # A prior transaction cannot have committed partial rows,
                    # so treating unexpected unmarked rows as corruption is
                    # safer than overwriting provenance.
                    raise KitError('中心内置资源迁移状态不完整；请从备份恢复后重试')
                self._published_conflicts(con, assets, contracts)
                timestamp = _now()
                for (kind, ref), item in sorted(assets.items()):
                    con.execute(
                        '''INSERT INTO web_central_builtin_assets
                           (kind,ref,name,version,source_revision,metadata_json,origin,imported_at)
                           VALUES (?,?,?,?,?,?,?,?)''',
                        (kind, ref, item['name'], item['version'],
                         _files_revision(kind, ref, item['files']), _json(item['metadata']), 'builtin', timestamp),
                    )
                    con.executemany(
                        '''INSERT INTO web_central_builtin_files(kind,ref,path,sha256,size,content)
                           VALUES (?,?,?,?,?,?)''',
                        [(kind, ref, path, _hash(data), len(data), data)
                         for path, data in sorted(item['files'].items())],
                    )
                for ref, (document, data) in sorted(contracts.items()):
                    con.execute(
                        '''INSERT INTO web_central_builtin_contracts
                           (contract_ref,sha256,content,contract_json,origin) VALUES (?,?,?,?,?)''',
                        (ref, _hash(data), data, _json(document), 'builtin'),
                    )
                con.execute(
                    'INSERT INTO web_central_library_state(marker,source_revision,imported_at) VALUES (?,?,?)',
                    (self._MARKER, source_revision, timestamp),
                )
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    def report(self) -> dict[str, object]:
        with self.store._connection() as con:
            state = con.execute(
                'SELECT source_revision,imported_at FROM web_central_library_state WHERE marker=?', (self._MARKER,)
            ).fetchone()
            return {
                'imported': state is not None,
                'origin': 'builtin',
                'source_revision': None if state is None else str(state['source_revision']),
                'imported_at': None if state is None else str(state['imported_at']),
                'assets': int(con.execute('SELECT count(*) FROM web_central_builtin_assets').fetchone()[0]),
                'contracts': int(con.execute('SELECT count(*) FROM web_central_builtin_contracts').fetchone()[0]),
            }

    def asset_files(self, kind: str, ref: str) -> dict[str, bytes] | None:
        if kind not in {'algorithm', 'template'}:
            raise KitError('中心资源类型无效')
        with self.store._connection() as con:
            exists = con.execute(
                'SELECT 1 FROM web_central_builtin_assets WHERE kind=? AND ref=?', (kind, ref)
            ).fetchone()
            if exists is None:
                return None
            rows = con.execute(
                'SELECT path,sha256,size,content FROM web_central_builtin_files '
                'WHERE kind=? AND ref=? ORDER BY path', (kind, ref)
            ).fetchall()
        return _materialize(rows, '中心内置资源')

    def asset_record(self, kind: str, ref: str) -> dict[str, object] | None:
        with self.store._connection() as con:
            row = con.execute(
                'SELECT * FROM web_central_builtin_assets WHERE kind=? AND ref=?', (kind, ref)
            ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(str(row['metadata_json']))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KitError('中心内置资源元数据已损坏') from exc
        if not isinstance(metadata, dict):
            raise KitError('中心内置资源元数据已损坏')
        return {'kind': str(row['kind']), 'ref': str(row['ref']), 'name': str(row['name']),
                'version': str(row['version']), 'origin': str(row['origin']), 'metadata': metadata}

    def records(self) -> list[dict[str, object]]:
        with self.store._connection() as con:
            rows = con.execute('SELECT * FROM web_central_builtin_assets ORDER BY kind,ref').fetchall()
        result = []
        for row in rows:
            item = self.asset_record(str(row['kind']), str(row['ref']))
            if item is not None:
                result.append(item)
        return result

    def contracts(self) -> dict[str, bytes]:
        with self.store._connection() as con:
            rows = con.execute(
                'SELECT contract_ref,sha256,content,contract_json FROM web_central_builtin_contracts ORDER BY contract_ref'
            ).fetchall()
        result: dict[str, bytes] = {}
        for row in rows:
            data = bytes(row['content'])
            if _hash(data) != str(row['sha256']):
                raise KitError('中心内置契约存储已损坏')
            try:
                document = json.loads(str(row['contract_json']))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise KitError('中心内置契约元数据已损坏') from exc
            if not isinstance(document, dict) or document.get('id') != str(row['contract_ref']):
                raise KitError('中心内置契约元数据已损坏')
            result[str(row['contract_ref'])] = data
        return result

    def contract(self, ref: str) -> tuple[dict[str, Any], bytes] | None:
        with self.store._connection() as con:
            row = con.execute(
                'SELECT contract_ref,sha256,content,contract_json FROM web_central_builtin_contracts WHERE contract_ref=?', (ref,)
            ).fetchone()
        if row is None:
            return None
        data = bytes(row['content'])
        if _hash(data) != str(row['sha256']):
            raise KitError('中心内置契约存储已损坏')
        try:
            document = json.loads(str(row['contract_json']))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KitError('中心内置契约元数据已损坏') from exc
        if not isinstance(document, dict) or document.get('id') != ref:
            raise KitError('中心内置契约元数据已损坏')
        return document, data
