"""Teacher-only direct review and publication of fixed Web submissions.

Supports publishing a submission as a Case (template), Algorithm, or Derived Dataset,
including "new resource" vs "new version of existing resource" (inheriting tags &
description), modified platform algorithm detection, local-to-platform algorithm
promotion, private derived dataset resolution, and pre-review test copying to ly.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .files import KitError, identifier, reference
from .web_asset_library import AssetLibrary
from .web_assets import AssetService, _contract, _dump, _refs
from .web_identity import Principal
from .web_store import SubmissionStore, _relative_path


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_UUID_SUFFIX = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')


class _PublicationFailure(Exception):
    """Internal rollback signal carrying the user-facing publication result."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__('direct review publication failed')
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _teacher(principal: object) -> tuple[int, str]:
    if (not isinstance(principal, Principal) or principal.role != 'teacher' or principal.username != 'ly'
            or isinstance(principal.uid, bool) or not isinstance(principal.uid, int) or principal.uid < 1):
        raise KitError('只有教师可以审核发布固定提交')
    return principal.uid, principal.username


def _yaml(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(data.decode('utf-8'))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise KitError(f'{label} 必须是 UTF-8 YAML 映射') from exc
    if not isinstance(value, dict):
        raise KitError(f'{label} 顶层必须是映射')
    return value


def _case_name(project_name: object, project_id: object) -> str:
    if not isinstance(project_name, str) or not isinstance(project_id, str) or not _UUID_SUFFIX.fullmatch(project_id):
        raise KitError('固定提交项目元数据无效')
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', project_name).strip('.-_') or 'case'
    suffix = '-' + project_id
    stem = stem[:128 - len(suffix)].rstrip('.-_') or 'case'
    name = stem + suffix
    try:
        return identifier(name)
    except KitError as exc:
        raise KitError('无法生成可发布案例名称') from exc


def _case_version(version: object) -> str:
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+', version):
        raise KitError('固定提交案例版本无效')
    major, minor = version.split('.')
    return f'{int(major)}.{int(minor)}.0'


def _bump_patch(version_str: str) -> str:
    parts = version_str.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return '1.0.1'
    return f'{int(parts[0])}.{int(parts[1])}.{int(parts[2]) + 1}'


def _parse_semver(version_str: str) -> tuple[int, int, int]:
    parts = version_str.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


class ReviewFlow:
    """Direct review facade over ``AssetService`` and ``DerivedDatasets``."""

    def __init__(self, store: SubmissionStore, library_root: Path | str):
        self.store = store
        self.library_root = Path(library_root)
        self.assets = AssetService(store, self.library_root)
        self._library_cache: AssetLibrary | None = None
        self._library_unavailable = False
        self._initialize()

    def _safe_catalog(self) -> dict[str, list[dict[str, Any]]]:
        try:
            if self._library_cache is None and not self._library_unavailable:
                self._library_cache = AssetLibrary(self.store, self.library_root)
            if self._library_cache is None:
                raise KitError('中心资源库不可用')
            return self._library_cache.catalog()
        except KitError:
            meta_algo = self.assets.metadata_map('algorithm')
            meta_tpl = self.assets.metadata_map('template')
            algos, tpls = [], []
            with self.store._connection() as con:
                rows = con.execute("SELECT kind, ref, asset_id FROM web_asset_published ORDER BY published_at DESC").fetchall()
            for r in rows:
                ref = str(r['ref'])
                if '@' not in ref:
                    continue
                n, v = ref.split('@', 1)
                if r['kind'] == 'algorithm':
                    m = meta_algo.get(('algorithm', ref), {})
                    algos.append({'ref': ref, 'name': n, 'version': v, 'tags': m.get('tags', []), 'description': m.get('description', '')})
                elif r['kind'] == 'template':
                    m = meta_tpl.get(('template', ref), {})
                    tpls.append({'ref': ref, 'name': n, 'version': v, 'tags': m.get('tags', []), 'description': m.get('description', '')})
            return {'plugins': algos, 'algorithms': algos, 'templates': tpls}

    @staticmethod
    def _catalog_algorithms(catalog: dict[str, Any]) -> list[dict[str, Any]]:
        return list(catalog.get('plugins') or catalog.get('algorithms') or [])

    def _initialize(self) -> None:
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                row = con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='web_direct_review_publications'"
                ).fetchone()
                columns = set() if row is None else {item['name']: item for item in con.execute('PRAGMA table_info(web_direct_review_publications)')}
                # The original record bound a teacher decision to two successful
                # execution jobs.  Direct review is now a human decision over
                # the immutable submission, so job ids are historical optional
                # provenance only.  Rebuild once when an old NOT NULL schema is
                # encountered; SQLite cannot drop a NOT NULL constraint in place.
                needs_rebuild = row is not None and (
                    "'derived'" not in str(row['sql'])
                    or 'review_type' not in columns
                    or ('check_run_id' in columns and bool(columns['check_run_id']['notnull']))
                    or ('run_id' in columns and bool(columns['run_id']['notnull']))
                )
                if needs_rebuild:
                    con.execute('ALTER TABLE web_direct_review_publications RENAME TO _old_web_direct_review_publications')
                    con.execute("""CREATE TABLE web_direct_review_publications (
                        submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('template','algorithm','derived')),
                        alias TEXT NOT NULL, ref TEXT NOT NULL, candidate_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('published','reused','failed')),
                        reviewer_uid INTEGER NOT NULL, reviewer_username TEXT NOT NULL,
                        check_run_id TEXT, run_id TEXT,
                        review_type TEXT NOT NULL CHECK(review_type IN ('manual','legacy_execution')),
                        review_note TEXT NOT NULL, published_at TEXT NOT NULL,
                        error TEXT,
                        PRIMARY KEY(submission_id,kind,alias,ref)
                    )""")
                    old_columns = {item['name'] for item in con.execute('PRAGMA table_info(_old_web_direct_review_publications)')}
                    legacy_kind = "kind" if 'kind' in old_columns else "'template'"
                    legacy_review_type = "review_type" if 'review_type' in old_columns else "'legacy_execution'"
                    con.execute(f"""INSERT INTO web_direct_review_publications(
                        submission_id,source_sha256,kind,alias,ref,candidate_id,status,
                        reviewer_uid,reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error)
                        SELECT submission_id,source_sha256,{legacy_kind},alias,ref,candidate_id,status,
                               reviewer_uid,reviewer_username,check_run_id,run_id,{legacy_review_type},review_note,published_at,error
                        FROM _old_web_direct_review_publications""")
                    con.execute('DROP TABLE _old_web_direct_review_publications')
                else:
                    con.execute("""CREATE TABLE IF NOT EXISTS web_direct_review_publications (
                        submission_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('template','algorithm','derived')),
                        alias TEXT NOT NULL, ref TEXT NOT NULL, candidate_id TEXT,
                        status TEXT NOT NULL CHECK(status IN ('published','reused','failed')),
                        reviewer_uid INTEGER NOT NULL, reviewer_username TEXT NOT NULL,
                        check_run_id TEXT, run_id TEXT,
                        review_type TEXT NOT NULL CHECK(review_type IN ('manual','legacy_execution')),
                        review_note TEXT NOT NULL, published_at TEXT NOT NULL,
                        error TEXT,
                        PRIMARY KEY(submission_id,kind,alias,ref)
                    )""")
                con.execute("""CREATE INDEX IF NOT EXISTS web_direct_review_source
                               ON web_direct_review_publications(submission_id, kind, alias)""")
                con.execute("""CREATE TABLE IF NOT EXISTS web_review_test_copies (
                    project_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL,
                    teacher_uid INTEGER NOT NULL,
                    teacher_username TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise

    def _submission(self, principal: Principal, submission_id: object) -> dict[str, Any]:
        if not isinstance(submission_id, str):
            raise KitError('提交 ID 无效')
        value = self.store.get_submission(submission_id, principal)
        with self.store._connection() as connection:
            if connection.execute('SELECT 1 FROM web_assignment_baselines WHERE submission_id=?', (submission_id,)).fetchone():
                raise KitError('任务内部基线不是个人正式固定提交')
        if value.get('kind') not in {'minor', 'major'} or not isinstance(value.get('content_sha256'), str):
            raise KitError('只能审核普通正式固定提交')
        return value

    def _existing_published_versions(self, kind: str, name: str) -> set[str]:
        catalog = self._safe_catalog()
        bucket = catalog.get('templates', []) if kind == 'template' else self._catalog_algorithms(catalog)
        existing = {
            row['version']
            for row in bucket
            if row.get('name') == name and isinstance(row.get('version'), str)
        }
        with self.store._connection() as con:
            for (ref_val,) in con.execute('SELECT ref FROM web_asset_published WHERE kind=?', (kind,)).fetchall():
                if isinstance(ref_val, str) and '@' in ref_val:
                    rn, rv = ref_val.split('@', 1)
                    if rn == name and re.fullmatch(r'\d+\.\d+\.\d+', rv):
                        existing.add(rv)
        return existing

    def _next_available_version(self, kind: str, name: str, base_version: str) -> str:
        existing = self._existing_published_versions(kind, name)
        cand = base_version if re.fullmatch(r'\d+\.\d+\.\d+', base_version or '') else '1.0.0'
        if cand not in existing:
            return cand
        major, minor, patch = _parse_semver(cand)
        for p in sorted((_parse_semver(v) for v in existing), reverse=True):
            if p[0] == major and p[1] == minor and p[2] >= patch:
                patch = p[2] + 1
                cand = f'{major}.{minor}.{patch}'
        while cand in existing:
            cand = _bump_patch(cand)
        return cand

    def _algorithms(self, files: dict[str, bytes]) -> list[dict[str, Any]]:
        project = _yaml(files.get('project.yaml', b''), 'project.yaml')
        if project.get('schema') != 'researchkit.project/v1':
            raise KitError('固定提交不是 ResearchKit 项目')
        plugins = project.get('plugins', {})
        if not isinstance(plugins, dict):
            raise KitError('项目插件配置无效')
        lock_doc: dict[str, Any] = {}
        if '.researchkit/lock.json' in files:
            try:
                loaded = json.loads(files['.researchkit/lock.json'].decode('utf-8'))
                if isinstance(loaded, dict):
                    lock_doc = loaded
            except (UnicodeDecodeError, json.JSONDecodeError):
                lock_doc = {}
        lock_plugins = lock_doc.get('plugins', {}) if isinstance(lock_doc.get('plugins'), dict) else {}
        origins_algos: dict[str, Any] = {}
        if '.researchkit/origins.json' in files:
            try:
                origins_doc = json.loads(files['.researchkit/origins.json'].decode('utf-8'))
                if isinstance(origins_doc, dict) and isinstance(origins_doc.get('algorithms'), dict):
                    origins_algos = origins_doc['algorithms']
            except (UnicodeDecodeError, json.JSONDecodeError):
                origins_algos = {}

        catalog_algos = {row['ref']: row for row in self._catalog_algorithms(self._safe_catalog()) if isinstance(row.get('ref'), str)}
        rows: list[dict[str, Any]] = []
        bad_prefixes = ('_runs/', 'results/', 'report/', 'cache/', '__pycache__/')
        for alias in sorted(plugins):
            config = plugins[alias]
            if not isinstance(alias, str) or not isinstance(config, dict):
                raise KitError('项目插件配置无效')
            try:
                identifier(alias)
                path = _relative_path(config.get('path'), label='插件路径')
            except KitError as exc:
                raise KitError('项目插件路径或别名无效') from exc
            meta = _yaml(files.get(path + '/plugin.yaml', b''), f'插件 {alias} 的 plugin.yaml')
            try:
                name = identifier(meta.get('name'))
            except KitError as exc:
                raise KitError(f'插件 {alias} 名称无效') from exc
            version = meta.get('version')
            if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
                raise KitError(f'插件 {alias} 版本无效')

            lock_entry = lock_plugins.get(alias, {}) if isinstance(lock_plugins.get(alias), dict) else {}
            origin_entry = origins_algos.get(alias, {}) if isinstance(origins_algos.get(alias), dict) else {}
            source_ref = config.get('source_ref') or origin_entry.get('ref') or lock_entry.get('ref')
            if not isinstance(source_ref, str) or '@' not in source_ref:
                candidate_ref = f'{name}@{version}'
                if candidate_ref in catalog_algos:
                    source_ref = candidate_ref
                else:
                    source_ref = None

            is_platform_path = path.startswith('.researchkit/plugins/')
            is_platform_plugin = bool(is_platform_path or (source_ref and source_ref in catalog_algos))
            is_local_plugin = not is_platform_plugin

            sub_plugin_files: dict[str, bytes] = {}
            for p, data in files.items():
                if p.startswith(path + '/'):
                    rel = p[len(path) + 1:]
                    if rel.startswith(bad_prefixes) or '/_runs/' in rel or '/results/' in rel or '/report/' in rel or '/cache/' in rel:
                        continue
                    sub_plugin_files[rel] = data

            modified_platform = False
            if is_platform_plugin and source_ref and (source_ref in catalog_algos or is_platform_path):
                upstream_full = self._existing_published_files('algorithm', source_ref)
                cand_full: dict[str, bytes] = {f'plugin/{k}': v for k, v in sub_plugin_files.items()}
                try:
                    for cref in sorted(_refs(meta)):
                        cn, cv = reference(cref)
                        c_path = f'.researchkit/contracts/{cn}/{cv}.yaml'
                        if c_path in files:
                            cand_full[f'contracts/{cn}/{cv}.yaml'] = files[c_path]
                except KitError:
                    modified_platform = True
                if not modified_platform and upstream_full is not None:
                    if not self._candidate_matches_prior('algorithm', upstream_full, cand_full):
                        modified_platform = True
                if not modified_platform and any(k.startswith('contracts/') for k in cand_full):
                    try:
                        self._verify_contract_compatibility(cand_full)
                    except KitError:
                        modified_platform = True

            suggested_next_version = None
            if modified_platform and source_ref and '@' in source_ref:
                up_name, up_ver = source_ref.split('@', 1)
                suggested_next_version = self._next_available_version('algorithm', up_name, up_ver)

            rows.append({
                'alias': alias,
                'name': name,
                'version': version,
                'ref': f'{name}@{version}',
                'path': path,
                'source_ref': source_ref,
                'is_platform_plugin': is_platform_plugin,
                'is_local_plugin': is_local_plugin,
                'modified_platform_algorithm': modified_platform,
                'upstream_ref': source_ref if is_platform_plugin else None,
                'suggested_next_version': suggested_next_version,
            })
        return rows

    def _datasets(self, submission: dict[str, Any]) -> list[dict[str, Any]]:
        manifest = submission.get('manifest', {})
        refs = []
        if isinstance(manifest, dict):
            dr = manifest.get('data_references')
            if isinstance(dr, dict) and isinstance(dr.get('references'), list):
                refs = dr['references']
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        with self.store._connection() as con:
            table_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_derived_datasets'"
            ).fetchone() is not None
            for item in refs:
                if not isinstance(item, dict) or not isinstance(item.get('dataset_id'), str):
                    continue
                did = item['dataset_id']
                if did in seen:
                    continue
                seen.add(did)
                row = con.execute(
                    'SELECT * FROM web_derived_datasets WHERE dataset_id=?', (did,)
                ).fetchone() if table_exists else None
                if row is not None:
                    keys = set(row.keys())
                    tags = []
                    if 'tags_json' in keys and row['tags_json']:
                        try:
                            tags = json.loads(row['tags_json'])
                        except Exception:
                            tags = []
                    visibility = str(row['visibility'])
                    result.append({
                        'dataset_id': did,
                        'path': item.get('path', ''),
                        'name': str(row['name']),
                        'scope': 'private_derived' if visibility == 'private' else 'platform_derived',
                        'visibility': visibility,
                        'owner_username': str(row['owner_username']),
                        'description': str(row['description']) if 'description' in keys else '',
                        'tags': tags if isinstance(tags, list) else [],
                        'readme_markdown': str(row['readme_markdown']) if 'readme_markdown' in keys else '',
                        'family_id': row['family_id'] if 'family_id' in keys else did,
                        'needs_resolution': visibility == 'private',
                    })
                else:
                    result.append({
                        'dataset_id': did,
                        'path': item.get('path', ''),
                        'name': did,
                        'scope': 'public_raw',
                        'visibility': 'public',
                        'needs_resolution': False,
                    })
        return result

    @staticmethod
    def _publication_rows(store: SubmissionStore, submission_id: str) -> list[dict[str, object]]:
        with store._connection() as con:
            rows = con.execute("""SELECT submission_id,source_sha256,kind,alias,ref,candidate_id,status,
                                         reviewer_uid,reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error
                                  FROM web_direct_review_publications
                                  WHERE submission_id=? ORDER BY kind,alias,ref""", (submission_id,)).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def release(self, principal: Principal, submission_id: str) -> dict[str, Any]:
        _teacher(principal)
        submission = self._submission(principal, submission_id)
        algorithms = self._algorithms(submission['files'])
        datasets = self._datasets(submission)
        metadata = {key: value for key, value in submission.items() if key != 'files'}
        project = submission.get('project') if isinstance(submission.get('project'), dict) else {}
        project_yaml = _yaml(submission['files'].get('project.yaml', b''), 'project.yaml')
        origin = project_yaml.get('origin') if isinstance(project_yaml.get('origin'), dict) else {}

        catalog = self._safe_catalog()
        with self.store._connection() as con:
            derived_rows = []
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_derived_datasets'").fetchone():
                for dr in con.execute("SELECT dataset_id,name,description,tags_json,family_id FROM web_derived_datasets WHERE visibility='public' ORDER BY created_at DESC").fetchall():
                    dkeys = set(dr.keys())
                    derived_rows.append({
                        'dataset_id': dr['dataset_id'],
                        'name': dr['name'],
                        'description': dr['description'] if 'description' in dkeys else '',
                        'tags': json.loads(dr['tags_json']) if 'tags_json' in dkeys and dr['tags_json'] else [],
                        'family_id': dr['family_id'] if 'family_id' in dkeys else dr['dataset_id'],
                    })

        proposed_case_name = None
        proposed_case_version = None
        try:
            proposed_case_name = _case_name(project.get('name'), submission.get('project_id'))
            proposed_case_version = _case_version(submission.get('version'))
        except KitError:
            pass

        return {
            'submission': metadata,
            'algorithms': algorithms,
            'datasets': datasets,
            'case_info': {
                'proposed_name': proposed_case_name,
                'proposed_version': proposed_case_version,
                'parent_template_ref': origin.get('template_ref') if isinstance(origin.get('template_ref'), str) else None,
            },
            'existing_assets': {
                'templates': [
                    {
                        'ref': t['ref'],
                        'name': t['name'],
                        'version': t['version'],
                        'tags': (self.assets.metadata_map('template').get(('template', t['ref']), {}).get('tags') or t.get('tags', [])),
                        'description': (self.assets.metadata_map('template').get(('template', t['ref']), {}).get('description') or t.get('description', '')),
                    }
                    for t in catalog.get('templates', [])
                ],
                'algorithms': [
                    {
                        'ref': a['ref'],
                        'name': a['name'],
                        'version': a['version'],
                        'tags': (self.assets.metadata_map('algorithm').get(('algorithm', a['ref']), {}).get('tags') or a.get('tags', [])),
                        'description': (self.assets.metadata_map('algorithm').get(('algorithm', a['ref']), {}).get('description') or a.get('description', '')),
                    }
                    for a in self._catalog_algorithms(catalog)
                ],
                'derived': derived_rows,
            },
            'publications': self._publication_rows(self.store, str(submission['id'])),
            'review_mode': 'manual',
        }

    def register_test_copy(self, principal: Principal, submission_id: str, project_id: str) -> dict[str, object]:
        uid, username = _teacher(principal)
        self._submission(principal, submission_id)
        if not isinstance(project_id, str) or not project_id.strip():
            raise KitError('测试项目 ID 无效')
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                con.execute(
                    """INSERT INTO web_review_test_copies(project_id,submission_id,teacher_uid,teacher_username,created_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(project_id) DO UPDATE SET submission_id=excluded.submission_id,
                       teacher_uid=excluded.teacher_uid,teacher_username=excluded.teacher_username,created_at=excluded.created_at""",
                    (project_id.strip(), submission_id, uid, username, _now()),
                )
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
        return {'project_id': project_id.strip(), 'submission_id': submission_id, 'owner_username': username}

    @staticmethod
    def _kind(value: object) -> str:
        if value == 'case':
            return 'template'
        if value in {'template', 'algorithm', 'derived'}:
            return str(value)
        raise KitError('发布类型只能是案例、算法或派生数据集')

    @staticmethod
    def _aliases(value: object, available: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        if kind in {'template', 'derived'}:
            if kind == 'template' and value not in (None, [], (), ''):
                raise KitError('发布案例时不能指定算法别名')
            return []
        if isinstance(value, str):
            raw = [value]
        elif isinstance(value, (list, tuple)):
            raw = list(value)
        else:
            raise KitError('发布算法时必须选择一个或多个算法别名')
        if not raw or any(not isinstance(alias, str) for alias in raw) or len(set(raw)) != len(raw):
            raise KitError('发布算法别名无效或重复')
        by_alias = {row['alias']: row for row in available}
        if any(alias not in by_alias for alias in raw):
            raise KitError('选择了固定提交中不存在的算法别名')
        return [by_alias[alias] for alias in raw]

    def _record(self, submission: dict[str, Any], kind: str, alias: str, ref: str, candidate_id: str | None,
                status: str, principal: Principal, note: str,
                error: str | None = None, *, connection=None) -> dict[str, object]:
        uid, username = _teacher(principal)
        if connection is None:
            with self.store.transaction() as con:
                con.execute("""INSERT INTO web_direct_review_publications(
                    submission_id,source_sha256,kind,alias,ref,candidate_id,status,reviewer_uid,
                    reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(submission_id,kind,alias,ref) DO UPDATE SET
                      source_sha256=excluded.source_sha256,candidate_id=excluded.candidate_id,status=excluded.status,
                      reviewer_uid=excluded.reviewer_uid,reviewer_username=excluded.reviewer_username,
                      check_run_id=excluded.check_run_id,run_id=excluded.run_id,review_type=excluded.review_type,review_note=excluded.review_note,
                      published_at=excluded.published_at,error=excluded.error""",
                    (submission['id'], submission['content_sha256'], kind, alias, ref, candidate_id, status,
                     uid, username, None, None, 'manual', note, _now(), error))
                row = con.execute("""SELECT submission_id,source_sha256,kind,alias,ref,candidate_id,status,
                                     reviewer_uid,reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error
                                     FROM web_direct_review_publications WHERE submission_id=? AND kind=? AND alias=? AND ref=?""",
                                  (submission['id'], kind, alias, ref)).fetchone()
        else:
            con = connection
            con.execute("""INSERT INTO web_direct_review_publications(
                submission_id,source_sha256,kind,alias,ref,candidate_id,status,reviewer_uid,
                reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(submission_id,kind,alias,ref) DO UPDATE SET
                  source_sha256=excluded.source_sha256,candidate_id=excluded.candidate_id,status=excluded.status,
                  reviewer_uid=excluded.reviewer_uid,reviewer_username=excluded.reviewer_username,
                  check_run_id=excluded.check_run_id,run_id=excluded.run_id,review_type=excluded.review_type,review_note=excluded.review_note,
                  published_at=excluded.published_at,error=excluded.error""",
                (submission['id'], submission['content_sha256'], kind, alias, ref, candidate_id, status,
                 uid, username, None, None, 'manual', note, _now(), error))
            row = con.execute("""SELECT submission_id,source_sha256,kind,alias,ref,candidate_id,status,
                                 reviewer_uid,reviewer_username,check_run_id,run_id,review_type,review_note,published_at,error
                                 FROM web_direct_review_publications WHERE submission_id=? AND kind=? AND alias=? AND ref=?""",
                              (submission['id'], kind, alias, ref)).fetchone()
        return {key: row[key] for key in row.keys()}

    def _candidate(self, principal: Principal, submission: dict[str, Any], kind: str,
                   name: str, version: str, alias: str | None,
                   *, source_override: dict[str, bytes] | None = None,
                   refs_override: list[dict[str, object]] | None = None, connection=None) -> tuple[dict[str, Any], bool]:
        candidate = self.assets.create(
            principal, str(submission['id']), kind, name, version, alias,
            source_override=source_override, refs_override=refs_override, connection=connection,
        )
        reused = candidate['status'] == 'published'
        if candidate['status'] == 'draft':
            note = (f'人工审核发布；审核人 {principal.username}；时间 {_now()}；来源提交 {submission["id"]}。')
            candidate = self.assets.review(principal, str(candidate['candidate_id']),
                                           str(candidate['candidate_sha256']), note, None, connection=connection,
                                           validated_evidence=True)
        elif candidate['status'] not in {'reviewed', 'published'}:
            raise KitError('资产发布记录状态无效')
        return candidate, reused

    def _ensure_library(self) -> AssetLibrary | None:
        if self._library_cache is None and not self._library_unavailable:
            try:
                self._library_cache = AssetLibrary(self.store, self.library_root)
            except KitError:
                self._library_unavailable = True
                self._library_cache = None
        return self._library_cache

    def _existing_published_files(self, kind: str, ref: str, *, connection=None) -> dict[str, bytes] | None:
        prior = self.assets.published_files(kind, ref, connection=connection)
        if prior is not None:
            return dict(prior)
        lib = self._ensure_library()
        if lib is not None and kind == 'algorithm':
            try:
                u_meta, u_files, _digest = lib._plugin_source(ref)
                merged: dict[str, bytes] = {f'plugin/{k}': v for k, v in u_files.items()}
                for cref in sorted(_refs(u_meta)):
                    cn, cv = reference(cref)
                    _cdoc, cbytes = lib._contract_source(cref)
                    merged[f'contracts/{cn}/{cv}.yaml'] = cbytes
                return merged
            except KitError:
                pass
        return None

    @staticmethod
    def _candidate_matches_prior(kind: str, prior: dict[str, bytes], candidate_files: dict[str, bytes]) -> bool:
        if prior == candidate_files:
            return True
        if set(prior.keys()) != set(candidate_files.keys()):
            return False
        for path, old_bytes in prior.items():
            new_bytes = candidate_files[path]
            if old_bytes == new_bytes:
                continue
            if kind == 'algorithm' and (path == 'plugin/plugin.yaml' or (path.startswith('contracts/') and path.endswith('.yaml'))):
                try:
                    d1 = yaml.safe_load(old_bytes.decode('utf-8'))
                    d2 = yaml.safe_load(new_bytes.decode('utf-8'))
                except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
                    return False
                if isinstance(d1, dict) and isinstance(d2, dict) and d1 == d2:
                    continue
            return False
        return True

    def _verify_contract_compatibility(self, candidate_files: dict[str, bytes], *, connection=None) -> None:
        """Reject conflicting contract definitions for existing contract IDs before reuse or publish."""
        lib = self._ensure_library()
        contract_items: list[tuple[str, str, dict[str, Any]]] = []
        for path, raw_bytes in sorted(candidate_files.items()):
            if not (path.startswith('contracts/') or path.startswith('.researchkit/contracts/')) or not path.endswith('.yaml'):
                continue
            cref = self.assets._contract_ref(path)
            parsed = _contract(raw_bytes, cref)
            contract_items.append((path, cref, parsed))
        if not contract_items:
            return
        if lib is not None:
            for _path, cref, parsed in contract_items:
                known_central = lib.central.contract(cref)
                if known_central is not None and known_central[0] != parsed:
                    raise KitError(f'契约 {cref} 与平台内置契约定义不一致，请升级契约版本后再入库')
        if connection is None:
            with self.store._connection() as con:
                self._verify_contract_compatibility_in_connection(con, contract_items)
            return
        self._verify_contract_compatibility_in_connection(connection, contract_items)

    @staticmethod
    def _verify_contract_compatibility_in_connection(con, contract_items: list[tuple[str, str, dict[str, Any]]]) -> None:
            has_contracts = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='web_asset_contracts'"
            ).fetchone() is not None
            has_pub_files = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='web_asset_published_files'"
            ).fetchone() is not None
            for _path, cref, parsed in contract_items:
                if has_contracts:
                    row = con.execute(
                        'SELECT contract_json FROM web_asset_contracts WHERE contract_ref=?', (cref,)
                    ).fetchone()
                    if row is not None and json.loads(row['contract_json']) != parsed:
                        raise KitError(f'契约 {cref} 已存在且定义不一致，请升级契约版本后再入库')
                if has_pub_files:
                    cn, cv = reference(cref)
                    pub_rows = con.execute(
                        'SELECT content FROM web_asset_published_files WHERE path IN (?, ?)',
                        (f'contracts/{cn}/{cv}.yaml', f'.researchkit/contracts/{cn}/{cv}.yaml'),
                    ).fetchall()
                    for prow in pub_rows:
                        if _contract(bytes(prow['content']), cref) != parsed:
                            raise KitError(f'契约 {cref} 与已发布契约定义不一致，请升级契约版本后再入库')

    def _verify_parent_exists(self, kind: str, parent_ref: object) -> str:
        clean = parent_ref.strip() if isinstance(parent_ref, str) else ''
        if not clean:
            raise KitError('作为已有资源的新版本入库时，必须选择同类别已有父版本')
        if kind == 'derived':
            with self.store._connection() as con:
                row = None
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_derived_datasets'").fetchone():
                    row = con.execute(
                        "SELECT visibility FROM web_derived_datasets WHERE dataset_id=?", (clean,)
                    ).fetchone()
            if row is None or str(row['visibility']) != 'public':
                raise KitError(f'找不到同类别已入库父级派生数据集：{clean}')
            return clean
        if '@' not in clean:
            raise KitError('已有资源引用格式必须为 name@version')
        reference(clean)
        catalog = self._safe_catalog()
        if kind == 'template':
            known_refs = {row.get('ref') for row in catalog.get('templates', [])}
        elif kind == 'algorithm':
            known_refs = {row.get('ref') for row in self._catalog_algorithms(catalog)}
        else:
            known_refs = set()
        if clean not in known_refs and self._existing_published_files(kind, clean) is None:
            raise KitError(f'找不到同类别已有父资源：{clean}')
        return clean

    def _inherit_metadata(self, kind: str, parent_ref: str, tags: object, description: object,
                          fallback_name: str, *, connection=None) -> tuple[list[str], str, str, str | None, str]:
        meta_map = self.assets.metadata_map(kind, connection=connection)
        parent_meta = meta_map.get((kind, parent_ref), {}) if parent_ref else {}
        clean_tags = AssetService._clean_tags(tags) if tags not in (None, [], '') else list(parent_meta.get('tags') or [])
        clean_desc = AssetService._clean_description(description) if isinstance(description, str) and description.strip() else str(parent_meta.get('description') or '')
        family_id = str(parent_meta.get('family_id') or parent_ref or '')
        display_name = str(parent_meta.get('display_name') or fallback_name or '')
        return clean_tags, clean_desc, family_id, (parent_ref or None), display_name

    def _publish_derived_record(
        self,
        principal: Principal,
        dataset_id: str,
        *,
        publish_mode: str = 'new',
        parent_ref: str = '',
        tags: object = None,
        description: str | None = None,
        readme_markdown: str = '',
        name_override: str = '',
        dataset_scope: str = 'derived',
        require_metadata: bool = False,
        connection=None,
    ) -> dict[str, object]:
        from .web_derived import DerivedDatasets
        if connection is None:
            context = self.store.transaction()
        else:
            from contextlib import nullcontext
            context = nullcontext(connection)
        with context as con:
                row = con.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?', (dataset_id,)).fetchone()
                if row is None:
                    raise KitError(f'找不到待入库的派生数据集：{dataset_id}')
                clean_parent = parent_ref.strip() if isinstance(parent_ref, str) else ''
                target_parent = clean_parent if (publish_mode == 'new_version' and clean_parent) else None
                parent_row, family_id = DerivedDatasets.validate_parent_lineage(
                    con, dataset_id, target_parent, current_row=row,
                )
                keys = set(row.keys())
                existing_tags = json.loads(row['tags_json']) if 'tags_json' in keys and row['tags_json'] else []
                parent_tags = []
                if parent_row is not None and 'tags_json' in set(parent_row.keys()) and parent_row['tags_json']:
                    parent_tags = json.loads(parent_row['tags_json'])
                if publish_mode == 'new_version':
                    if not clean_parent or parent_row is None or str(parent_row['visibility']) != 'public':
                        raise KitError('作为已有派生数据集的新版本入库时，必须指定有效的已入库公共/派生数据集父版本')
                if publish_mode == 'new':
                    cand_tags = AssetService._clean_tags(tags) if tags is not None else existing_tags
                    cand_desc = AssetService._clean_description(description) if description is not None else (str(row['description']) if 'description' in keys and row['description'] else '')
                    if not cand_tags or not cand_desc:
                        raise KitError('作为新派生数据集入库必须填写标签（至少 1 个）和说明')
                    final_tags, final_desc = cand_tags, cand_desc
                else:
                    final_tags = AssetService._clean_tags(tags) if tags not in (None, [], '') else (parent_tags or existing_tags)
                    final_desc = AssetService._clean_description(description)
                    if not final_desc:
                        if parent_row is not None and 'description' in set(parent_row.keys()) and parent_row['description']:
                            final_desc = str(parent_row['description'])
                        elif 'description' in keys and row['description']:
                            final_desc = str(row['description'])
                final_name = (name_override or (parent_row['name'] if parent_row is not None else row['name'])).strip() or row['name']
                src_refs = json.loads(row['source_refs_json'] or '[]')
                final_readme = str(readme_markdown or '').strip()
                if not final_readme:
                    if parent_row is not None and 'readme_markdown' in set(parent_row.keys()) and parent_row['readme_markdown']:
                        final_readme = str(parent_row['readme_markdown'])
                    elif 'readme_markdown' in keys and row['readme_markdown']:
                        final_readme = str(row['readme_markdown'])
                    else:
                        final_readme = DerivedDatasets.build_readme(
                            final_name, final_desc, final_tags,
                            src_refs if isinstance(src_refs, list) else [],
                        )
                parent_id = target_parent if target_parent else (row['parent_dataset_id'] if 'parent_dataset_id' in keys else None)

                con.execute(
                    """UPDATE web_derived_datasets
                       SET visibility='public', name=?, description=?, tags_json=?, readme_markdown=?,
                           parent_dataset_id=?, family_id=?
                       WHERE dataset_id=?""",
                    (
                        final_name,
                        final_desc,
                        json.dumps(final_tags, ensure_ascii=False),
                        final_readme,
                        parent_id,
                        family_id,
                        dataset_id,
                    ),
                )
                if dataset_scope in {'public', 'promote_public'}:
                    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_dataset_policies'").fetchone():
                        con.execute(
                            """INSERT INTO web_dataset_policies(dataset_id, public, updated_at)
                               VALUES(?, 1, ?)
                               ON CONFLICT(dataset_id) DO UPDATE SET public=1, updated_at=excluded.updated_at""",
                            (dataset_id, _now()),
                        )
        return {
            'dataset_id': dataset_id,
            'name': final_name,
            'visibility': 'public',
            'dataset_scope': dataset_scope,
            'tags': final_tags,
            'description': final_desc,
            'readme_markdown': final_readme,
            'family_id': family_id,
            'parent_dataset_id': parent_id,
        }

    @staticmethod
    def _batch_items(items: object) -> list[dict[str, Any]]:
        """Validate and normalize the small public batch-publication protocol.

        A batch item deliberately describes one publishable selection.  This
        keeps the UI's per-resource metadata choices unambiguous and, more
        importantly, prevents one repeated selection from creating two
        candidates for the same source resource.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 32:
            raise KitError('批量入库项目必须是 1 到 32 个选择项')
        normalized: list[dict[str, Any]] = []
        templates = 0
        algorithm_aliases: set[str] = set()
        dataset_ids: set[str] = set()
        for index, supplied in enumerate(items):
            if not isinstance(supplied, dict):
                raise KitError(f'批量入库第 {index + 1} 项必须是对象')
            item = dict(supplied)
            kind = item.get('kind')
            if kind not in {'template', 'algorithm', 'derived'}:
                raise KitError(f'批量入库第 {index + 1} 项类型无效')
            common = {'kind', 'publish_mode', 'parent_ref', 'name', 'version', 'tags', 'description'}
            specific = {'template': {'embedded_algorithms', 'embedded_datasets'},
                        'algorithm': {'alias', 'aliases'},
                        'derived': {'dataset_id', 'dataset_scope', 'readme_markdown'}}
            if set(item) - common - specific[kind]:
                raise KitError(f'批量入库第 {index + 1} 项含有不适用的字段')
            mode = item.get('publish_mode', 'new')
            if mode not in {'new', 'new_version'}:
                raise KitError(f'批量入库第 {index + 1} 项入库模式无效')
            item['kind'] = kind
            item['publish_mode'] = mode
            item['_index'] = index
            if kind == 'template':
                templates += 1
                if templates > 1:
                    raise KitError('一次批量入库最多选择一个案例')
                if item.get('alias') not in (None, '') or item.get('aliases') not in (None, [], ()):
                    raise KitError('批量案例项不能指定算法别名')
                if item.get('dataset_id') not in (None, ''):
                    raise KitError('批量案例项不能指定派生数据集')
            elif kind == 'algorithm':
                has_alias = 'alias' in item
                has_aliases = 'aliases' in item
                if has_alias == has_aliases:
                    raise KitError('批量算法项必须且只能提供 alias 或 aliases:[alias]')
                if has_alias:
                    alias = item['alias']
                else:
                    aliases = item['aliases']
                    if not isinstance(aliases, list) or len(aliases) != 1:
                        raise KitError('批量算法项的 aliases 必须恰好包含一个别名')
                    alias = aliases[0]
                if not isinstance(alias, str) or not alias:
                    raise KitError('批量算法项别名无效')
                if alias in algorithm_aliases:
                    raise KitError(f'批量入库重复选择了算法别名：{alias}')
                algorithm_aliases.add(alias)
                item['alias'] = alias
                item['aliases'] = [alias]
            else:
                dataset_id = item.get('dataset_id')
                if not isinstance(dataset_id, str) or not dataset_id.strip():
                    raise KitError('批量派生数据项必须选择一个私有派生数据集')
                dataset_id = dataset_id.strip()
                if dataset_id in dataset_ids:
                    raise KitError(f'批量入库重复选择了派生数据集：{dataset_id}')
                dataset_ids.add(dataset_id)
                item['dataset_id'] = dataset_id
            normalized.append(item)
        return normalized

    def _batch_embedded_algorithm(self, item: dict[str, Any], algorithm: dict[str, Any]) -> dict[str, object] | None:
        """Translate an independently selected algorithm into case handling.

        The explicit target version is retained so a later case publication
        reuses the just-published algorithm instead of choosing the next patch.
        """
        if not algorithm.get('is_local_plugin') and not algorithm.get('modified_platform_algorithm'):
            return None
        mode = str(item['publish_mode'])
        parent_ref = str(item.get('parent_ref') or '').strip()
        requested_version = str(item.get('version') or '').strip()
        if algorithm.get('modified_platform_algorithm'):
            upstream_ref = str(algorithm.get('upstream_ref') or '')
            upstream_name = upstream_ref.split('@', 1)[0] if '@' in upstream_ref else ''
            requested_name = str(item.get('name') or '').strip()
            if requested_name and requested_name != upstream_name:
                raise KitError(f'已修改平台算法必须保留原算法名称：{upstream_name}')
        if mode == 'new_version' or algorithm.get('modified_platform_algorithm'):
            source_parent = (str(algorithm.get('upstream_ref') or '')
                             if algorithm.get('modified_platform_algorithm') else parent_ref or str(algorithm.get('upstream_ref') or ''))
            source_parent = self._verify_parent_exists('algorithm', source_parent)
            target_name, base_version = source_parent.split('@', 1)
            target_version = requested_version or self._next_available_version('algorithm', target_name, base_version)
        else:
            target_name = identifier(str(item.get('name') or algorithm['name']).strip())
            target_version = requested_version or str(algorithm['version'])
            source_parent = parent_ref
        decision: dict[str, object] = {
            'action': 'new_version' if mode == 'new_version' else 'new',
            'name': target_name,
            'version': target_version,
            'parent_ref': source_parent,
        }
        for key in ('tags', 'description'):
            if key in item:
                decision[key] = item[key]
        return decision

    @staticmethod
    def _batch_embedded_dataset(item: dict[str, Any]) -> dict[str, object]:
        action = 'new_version' if item['publish_mode'] == 'new_version' else (
            'public' if item.get('dataset_scope') in {'public', 'promote_public'} else 'publish_derived'
        )
        decision: dict[str, object] = {'action': action}
        for key in ('parent_ref', 'name', 'tags', 'description', 'readme_markdown'):
            if key in item:
                decision[key] = item[key]
        return decision

    def publish_batch(
        self,
        principal: Principal,
        submission_id: str,
        *,
        expected_sha256: str,
        items: object,
    ) -> dict[str, Any]:
        """Publish selected resources as one all-or-nothing review action.

        Item results retain request order, while mutations run datasets,
        algorithms, then the one optional template so embedded references can
        reuse exact resources chosen independently in the same batch.
        """
        _teacher(principal)
        batch = self._batch_items(items)
        # Do central-library setup before the sole writer transaction, exactly
        # as ``publish`` does for the single-resource path.
        self._ensure_library()
        central = self.assets.prepare_central()
        try:
            with self.store.transaction() as con:
                submission = self._submission(principal, submission_id)
                if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
                    raise KitError('固定提交摘要无效')
                if expected_sha256 != submission['content_sha256']:
                    raise KitError('固定提交内容已变化，请重新打开审核页面')

                algorithms = {str(row['alias']): row for row in self._algorithms(submission['files'])}
                source_datasets = {str(row['dataset_id']): row for row in self._datasets(submission)}
                reused_derived: set[str] = set()
                template = next((item for item in batch if item['kind'] == 'template'), None)
                template_algorithms: dict[str, object] = {}
                template_datasets: dict[str, object] = {}
                if template is not None:
                    supplied_algorithms = template.get('embedded_algorithms')
                    supplied_datasets = template.get('embedded_datasets')
                    if supplied_algorithms is not None and not isinstance(supplied_algorithms, dict):
                        raise KitError('案例的嵌入算法配置必须是映射')
                    if supplied_datasets is not None and not isinstance(supplied_datasets, dict):
                        raise KitError('案例的嵌入派生数据配置必须是映射')
                    template_algorithms.update(supplied_algorithms or {})
                    template_datasets.update(supplied_datasets or {})

                for item in batch:
                    if item['kind'] == 'algorithm':
                        alias = str(item['alias'])
                        algorithm = algorithms.get(alias)
                        if algorithm is None:
                            raise KitError('选择了固定提交中不存在的算法别名')
                        if template is not None:
                            if alias in template_algorithms:
                                raise KitError(f'算法 {alias} 同时有案例嵌入配置和独立入库配置')
                            decision = self._batch_embedded_algorithm(item, algorithm)
                            if decision is not None:
                                template_algorithms[alias] = decision
                    elif item['kind'] == 'derived':
                        dataset_id = str(item['dataset_id'])
                        source = source_datasets.get(dataset_id)
                        # Source membership is immutable on the submission;
                        # visibility is read from the same transaction which
                        # later changes it to public.
                        row = con.execute(
                            'SELECT visibility FROM web_derived_datasets WHERE dataset_id=?', (dataset_id,)
                        ).fetchone()
                        if source is None or row is None:
                            raise KitError('只能入库该固定提交实际引用的私有派生数据集')
                        if str(row['visibility']) != 'private':
                            prior = con.execute(
                                '''SELECT 1 FROM web_direct_review_publications
                                   WHERE submission_id=? AND source_sha256=? AND kind='derived'
                                     AND alias=? AND ref=? AND status IN ('published','reused')''',
                                (submission_id, submission['content_sha256'], dataset_id, dataset_id),
                            ).fetchone()
                            if prior is None or str(row['visibility']) != 'public':
                                raise KitError('只能入库该固定提交实际引用的私有派生数据集')
                            reused_derived.add(dataset_id)
                            continue
                        if template is not None:
                            if dataset_id in template_datasets:
                                raise KitError(f'派生数据集 {dataset_id} 同时有案例嵌入配置和独立入库配置')
                            template_datasets[dataset_id] = self._batch_embedded_dataset(item)

                item_results: dict[int, dict[str, Any]] = {}
                published: list[dict[str, Any]] = []
                for kind in ('derived', 'algorithm', 'template'):
                    for item in batch:
                        if item['kind'] != kind:
                            continue
                        kwargs = {
                            key: item[key]
                            for key in ('publish_mode', 'parent_ref', 'name', 'version', 'tags', 'description',
                                        'readme_markdown', 'dataset_id', 'dataset_scope')
                            if key in item
                        }
                        if kind == 'algorithm':
                            algorithm = algorithms[str(item['alias'])]
                            if template is not None and algorithm.get('modified_platform_algorithm'):
                                decision = template_algorithms.get(str(item['alias']))
                                if isinstance(decision, dict):
                                    # The case path always preserves the upstream
                                    # algorithm name for a modified platform
                                    # component.  Publish the standalone choice
                                    # under that same exact target as well.
                                    kwargs['name'] = decision['name']
                                    kwargs['version'] = decision['version']
                                    kwargs['publish_mode'] = 'new_version'
                                    kwargs['parent_ref'] = str(algorithm.get('upstream_ref') or '')
                            result = self.publish(
                                principal, submission_id, 'algorithm', [item['alias']], expected_sha256,
                                _connection=con, _central=central, **kwargs,
                            )
                        elif kind == 'derived':
                            dataset_id = str(item['dataset_id'])
                            if dataset_id in reused_derived:
                                note = f'人工审核发布；审核人 {principal.username}；来源提交 {submission["id"]}。'
                                rec = self._record(
                                    submission, 'derived', dataset_id, dataset_id, None, 'reused', principal,
                                    note, connection=con,
                                )
                                result = {'published': [{**rec, 'dataset_id': dataset_id, 'visibility': 'public'}], 'reused': True}
                            else:
                                result = self.publish(
                                    principal, submission_id, 'derived', None, expected_sha256,
                                    require_metadata=True, _connection=con, _central=central, **kwargs,
                                )
                        else:
                            kwargs['embedded_algorithms'] = template_algorithms
                            kwargs['embedded_datasets'] = template_datasets
                            result = self.publish(
                                principal, submission_id, 'template', None, expected_sha256,
                                _connection=con, _central=central, **kwargs,
                            )
                        rows = list(result.get('published') or [])
                        reused = bool(result.get('reused'))
                        item_result: dict[str, Any] = {
                            'index': item['_index'], 'kind': kind,
                            'status': 'reused' if reused else 'published',
                            'published': rows, 'reused': reused,
                        }
                        for key in ('promoted_algorithms', 'promoted_datasets'):
                            if key in result:
                                item_result[key] = result[key]
                        item_results[item['_index']] = item_result
                        published.extend(rows)
                ordered = [item_results[index] for index in range(len(batch))]
                return {
                    'ok': True,
                    'published': published,
                    'reused': any(item['reused'] for item in ordered),
                    'items': ordered,
                }
        except _PublicationFailure as exc:
            return {
                'ok': False,
                'rolled_back': True,
                'published': [],
                'reused': False,
                'items': [],
                'failures': list(exc.result.get('failures') or []),
            }
        except KitError as exc:
            raise KitError(f'批量审核发布失败，已全部回滚：{exc}') from exc

    def publish(
        self,
        principal: Principal,
        submission_id: str,
        kind: str,
        aliases: object = None,
        expected_sha256: str = '',
        *,
        publish_mode: str = 'new',
        parent_ref: str = '',
        name: str = '',
        version: str = '',
        tags: object = None,
        description: str | None = None,
        readme_markdown: str = '',
        dataset_id: str = '',
        dataset_scope: str = 'derived',
        embedded_algorithms: object = None,
        embedded_datasets: object = None,
        require_metadata: bool = False,
        _connection=None,
        _central=None,
    ) -> dict[str, Any]:
        _teacher(principal)
        # Asset/Central library schema work and its one-time trusted import use
        # their own transactions.  Do that before opening the one transaction
        # which owns this complete direct publication.
        if _connection is None:
            self._ensure_library()
            central = self.assets.prepare_central()
            try:
                with self.store.transaction() as con:
                    return self.publish(
                        principal, submission_id, kind, aliases, expected_sha256,
                        publish_mode=publish_mode, parent_ref=parent_ref, name=name,
                        version=version, tags=tags, description=description,
                        readme_markdown=readme_markdown, dataset_id=dataset_id,
                        dataset_scope=dataset_scope, embedded_algorithms=embedded_algorithms,
                        embedded_datasets=embedded_datasets, require_metadata=require_metadata,
                        _connection=con, _central=central,
                    )
            except _PublicationFailure as exc:
                # The transaction rolled back every tentative candidate, asset,
                # metadata, grant and derived-data mutation.  The caller still
                # receives the review failure without any compensating writes.
                return exc.result
        submission = self._submission(principal, submission_id)
        if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
            raise KitError('固定提交摘要无效')
        if expected_sha256 != submission['content_sha256']:
            raise KitError('固定提交内容已变化，请重新打开审核页面')
        if publish_mode not in {'new', 'new_version'}:
            raise KitError('入库模式只能是“作为新资源”或“作为已有资源的新版本”')

        normalized_kind = self._kind(kind)
        note = f'人工审核发布；审核人 {principal.username}；来源提交 {submission["id"]}。'

        if publish_mode == 'new' and normalized_kind in {'template', 'algorithm'}:
            req_tags = AssetService._clean_tags(tags)
            req_desc = AssetService._clean_description(description)
            if not req_tags or not req_desc:
                raise KitError('作为新资源入库必须填写标签（至少 1 个）和说明')

        if normalized_kind == 'derived':
            if not isinstance(dataset_id, str) or not dataset_id.strip():
                raise KitError('请选择要入库的私有派生数据集')
            published_ds = self._publish_derived_record(
                principal,
                dataset_id.strip(),
                publish_mode=publish_mode,
                parent_ref=parent_ref.strip() if isinstance(parent_ref, str) else '',
                tags=tags,
                description=description,
                readme_markdown=readme_markdown,
                name_override=name,
                dataset_scope=dataset_scope,
                require_metadata=require_metadata,
                connection=_connection,
            )
            rec = self._record(
                submission, 'derived', dataset_id.strip(), dataset_id.strip(), None,
                'published', principal, note, connection=_connection,
            )
            return {'published': [{**rec, **published_ds}], 'reused': False}

        algorithms = self._algorithms(submission['files'])
        selected = self._aliases(aliases, algorithms, normalized_kind)

        if normalized_kind == 'algorithm':
            prepared = []
            failures = []
            row_parents: dict[str, str] = {}
            for row in selected:
                target_name = row['name']
                target_version = str(version).strip() if version else row['version']
                p_ref = parent_ref.strip() if isinstance(parent_ref, str) else ''
                mod_err = None
                if row.get('modified_platform_algorithm'):
                    up_ref = str(row.get('upstream_ref') or row['ref'])
                    up_name, up_ver = up_ref.split('@', 1)
                    if not p_ref and target_name == up_name:
                        p_ref = up_ref
                    if f'{target_name}@{target_version}' == up_ref or (publish_mode == 'new' and target_name == up_name and target_version == up_ver):
                        mod_err = f'算法 {row["alias"]} 已修改平台算法 {up_ref}，同一精确版本不能覆盖；请修改版本后作为新版本入库'
                if publish_mode == 'new_version':
                    p_ref = self._verify_parent_exists('algorithm', p_ref or row.get('upstream_ref'))
                    if len(selected) == 1:
                        target_name = p_ref.split('@', 1)[0]
                        if not version:
                            target_version = self._next_available_version('algorithm', target_name, p_ref.split('@', 1)[1])
                            mod_err = None
                        if row.get('modified_platform_algorithm') and target_version == p_ref.split('@', 1)[1]:
                            mod_err = f'算法 {row["alias"]} 已修改平台算法，请修改版本号后作为新版本入库'
                elif publish_mode == 'new' and name and len(selected) == 1:
                    target_name = identifier(name.strip())
                row_parents[row['alias']] = p_ref
                ref = f'{target_name}@{target_version}'
                if mod_err:
                    failures.append({'alias': row['alias'], 'ref': ref, 'error': mod_err})
                    continue
                try:
                    candidate, reused = self._candidate(
                        principal, submission, 'algorithm', target_name, target_version, row['alias'], connection=_connection,
                    )
                    prepared.append((row['alias'], target_name, target_version, row['alias'], candidate, reused))
                except KitError as exc:
                    failures.append({'alias': row['alias'], 'ref': ref, 'error': str(exc)})
            for alias, target_name, target_version, _ca, candidate, _reused in prepared:
                ref = f'{target_name}@{target_version}'
                try:
                    cand_files = dict(candidate.get('files') or {})
                    self._verify_contract_compatibility(cand_files, connection=_connection)
                    prior = self._existing_published_files('algorithm', ref, connection=_connection)
                    if prior is not None and not self._candidate_matches_prior('algorithm', prior, cand_files):
                        raise KitError('同一精确版本已存在不同内容；请修改版本后重新入库')
                except KitError as exc:
                    failures.append({'alias': alias, 'ref': ref, 'error': str(exc)})
            if failures:
                raise _PublicationFailure({'published': [], 'reused': False, 'failures': failures})

            published = []
            any_reused = False
            for alias, target_name, target_version, _ca, candidate, reused in prepared:
                ref = f'{target_name}@{target_version}'
                try:
                    cand_files = dict(candidate.get('files') or {})
                    self._verify_contract_compatibility(cand_files, connection=_connection)
                    prior = self._existing_published_files('algorithm', ref, connection=_connection)
                    if prior is not None:
                        if not self._candidate_matches_prior('algorithm', prior, cand_files):
                            raise KitError('同一精确版本已存在不同内容；请修改版本后重新入库')
                        reused = True
                    else:
                        self.assets.publish(principal, str(candidate['candidate_id']), str(candidate['candidate_sha256']), connection=_connection, central=_central)
                    eff_parent = row_parents.get(alias) or (parent_ref if publish_mode == 'new_version' else '')
                    c_tags, c_desc, fam_id, par_val, disp = self._inherit_metadata(
                        'algorithm',
                        eff_parent,
                        tags,
                        description,
                        target_name, connection=_connection,
                    )
                    self.assets.set_metadata(
                        principal, 'algorithm', ref,
                        description=c_desc, tags=c_tags,
                        parent_ref=par_val,
                        display_name=disp, connection=_connection,
                    )
                    rec = self._record(
                        submission, 'algorithm', alias, ref, str(candidate['candidate_id']),
                        'reused' if reused else 'published', principal, note, connection=_connection,
                    )
                    published.append(rec)
                    any_reused = any_reused or reused
                except KitError as exc:
                    failures.append({'alias': alias, 'ref': ref, 'error': str(exc)})
                    raise _PublicationFailure({
                        'published': [], 'reused': False, 'failures': failures,
                    })
            return {'published': published, 'reused': any_reused}

        # Case (Template) Publication
        project = submission.get('project')
        if not isinstance(project, dict):
            raise KitError('固定提交项目元数据无效')

        p_ref = parent_ref.strip() if isinstance(parent_ref, str) else ''
        if publish_mode == 'new_version':
            p_ref = self._verify_parent_exists('template', p_ref)
            case_name = p_ref.split('@', 1)[0]
            case_version = str(version).strip() if version else self._next_available_version(
                'template', case_name, p_ref.split('@', 1)[1]
            )
        else:
            case_name = identifier(name.strip()) if name else _case_name(project.get('name'), submission.get('project_id'))
            case_version = str(version).strip() if version else _case_version(submission.get('version'))

        source_files = dict(submission['files'])
        project_doc = _yaml(source_files.get('project.yaml', b''), 'project.yaml')
        plugins_cfg = dict(project_doc.get('plugins', {}))
        origins_doc: dict[str, Any] = {'schema': 'researchkit.origins/v1', 'algorithms': {}}
        if '.researchkit/origins.json' in source_files:
            try:
                loaded_origins = json.loads(source_files['.researchkit/origins.json'].decode('utf-8'))
                if isinstance(loaded_origins, dict):
                    origins_doc = loaded_origins
                    origins_doc.setdefault('schema', 'researchkit.origins/v1')
                    origins_doc.setdefault('algorithms', {})
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        emb_alg_cfg = embedded_algorithms if isinstance(embedded_algorithms, dict) else {}
        emb_ds_cfg = embedded_datasets if isinstance(embedded_datasets, dict) else {}

        promoted_algorithms: list[dict[str, object]] = []
        promoted_datasets: list[dict[str, object]] = []
        ref = f'{case_name}@{case_version}'
        in_template_publish = False

        try:
            for alg in algorithms:
                alias = alg['alias']
                decision = emb_alg_cfg.get(alias, {}) if isinstance(emb_alg_cfg.get(alias), dict) else {}
                action = decision.get('action')
                if alg['modified_platform_algorithm']:
                    if action in {'keep_local', 'keep_old', 'skip', 'unchanged'}:
                        raise KitError(f'算法 {alias} 已修改平台算法，必须强制作为新版本入库')
                    upstream_ref = str(alg['upstream_ref'] or f"{alg['name']}@{alg['version']}")
                    up_name, up_ver = upstream_ref.split('@', 1)
                    explicit_ver = str(decision.get('version') or '').strip()
                    if explicit_ver == up_ver:
                        raise KitError(f'算法 {alias} 已修改平台算法 {upstream_ref}，新版本号不能与原版本 {up_ver} 相同')
                    new_ver = explicit_ver or self._next_available_version('algorithm', up_name, up_ver)
                    cand, reused = self._candidate(principal, submission, 'algorithm', up_name, new_ver, alias, connection=_connection)
                    new_ref = f'{up_name}@{new_ver}'
                    cand_files = dict(cand.get('files') or {})
                    self._verify_contract_compatibility(cand_files, connection=_connection)
                    prior = self._existing_published_files('algorithm', new_ref, connection=_connection)
                    if prior is None:
                        self.assets.publish(principal, str(cand['candidate_id']), str(cand['candidate_sha256']), connection=_connection, central=_central)
                    elif not self._candidate_matches_prior('algorithm', prior, cand_files):
                        if explicit_ver:
                            raise KitError(f'算法 {alias} 指定的版本 {new_ref} 已存在不同内容；请指定新的版本号后再入库')
                        new_ver = self._next_available_version('algorithm', up_name, _bump_patch(new_ver))
                        new_ref = f'{up_name}@{new_ver}'
                        cand, reused = self._candidate(principal, submission, 'algorithm', up_name, new_ver, alias, connection=_connection)
                        cand_files_retry = dict(cand.get('files') or {})
                        self._verify_contract_compatibility(cand_files_retry, connection=_connection)
                        prior_retry = self._existing_published_files('algorithm', new_ref, connection=_connection)
                        if prior_retry is not None and not self._candidate_matches_prior('algorithm', prior_retry, cand_files_retry):
                            raise KitError(f'同一精确版本 {new_ref} 已存在不同内容；请指定新的版本号后再入库')
                        if prior_retry is None:
                            self.assets.publish(principal, str(cand['candidate_id']), str(cand['candidate_sha256']), connection=_connection, central=_central)
                    a_tags, a_desc, a_fam, a_par, a_disp = self._inherit_metadata(
                        'algorithm', upstream_ref, decision.get('tags'), decision.get('description', ''), up_name, connection=_connection,
                    )
                    self.assets.set_metadata(principal, 'algorithm', new_ref, description=a_desc, tags=a_tags, parent_ref=upstream_ref, display_name=a_disp, connection=_connection)
                    old_prefix = alg['path'].rstrip('/') + '/'
                    new_prefix = f'.researchkit/plugins/{alias}/'
                    moved = {p: d for p, d in source_files.items() if p.startswith(old_prefix)}
                    if old_prefix != new_prefix:
                        for p in list(moved.keys()):
                            del source_files[p]
                            source_files[new_prefix + p[len(old_prefix):]] = moved[p]
                    plugin_yaml_path = new_prefix + 'plugin.yaml'
                    pmeta = _yaml(source_files.get(plugin_yaml_path, b''), 'plugin.yaml')
                    pmeta['name'] = up_name
                    pmeta['version'] = new_ver
                    source_files[plugin_yaml_path] = _dump(pmeta)
                    cfg_entry = dict(plugins_cfg.get(alias, {}))
                    cfg_entry['path'] = f'.researchkit/plugins/{alias}'
                    cfg_entry['source_ref'] = new_ref
                    cfg_entry.setdefault('parameters', {})
                    cfg_entry.pop('local_source', None)
                    plugins_cfg[alias] = cfg_entry
                    if isinstance(origins_doc.get('algorithms'), dict):
                        origins_doc['algorithms'][alias] = {'ref': new_ref, 'path': f'.researchkit/plugins/{alias}'}
                    promoted_algorithms.append({'alias': alias, 'ref': new_ref, 'mode': 'forced_new_version'})
                elif alg['is_local_plugin'] and action in {'promote', 'new', 'new_version'}:
                    if action in {'promote', 'new'}:
                        if not AssetService._clean_tags(decision.get('tags')) or not AssetService._clean_description(decision.get('description')):
                            raise KitError(f'将本地算法 {alias} 纳入平台新算法时必须填写标签和说明')
                    if action == 'new_version':
                        p_alg_ref = self._verify_parent_exists('algorithm', decision.get('parent_ref'))
                        target_alg_name, base_v = p_alg_ref.split('@', 1)
                        explicit_local_ver = str(decision.get('version') or '').strip()
                        target_alg_ver = explicit_local_ver or self._next_available_version('algorithm', target_alg_name, base_v)
                    else:
                        p_alg_ref = ''
                        target_alg_name = identifier(str(decision.get('name') or alg['name']).strip())
                        explicit_local_ver = str(decision.get('version') or '').strip()
                        target_alg_ver = explicit_local_ver or self._next_available_version('algorithm', target_alg_name, alg['version'])
                    cand, reused = self._candidate(principal, submission, 'algorithm', target_alg_name, target_alg_ver, alias, connection=_connection)
                    new_ref = f'{target_alg_name}@{target_alg_ver}'
                    cand_files = dict(cand.get('files') or {})
                    self._verify_contract_compatibility(cand_files, connection=_connection)
                    prior = self._existing_published_files('algorithm', new_ref, connection=_connection)
                    if prior is not None:
                        if not self._candidate_matches_prior('algorithm', prior, cand_files):
                            raise KitError(f'同一精确版本 {new_ref} 已存在不同内容；请确认并指定新的版本号后再入库')
                    else:
                        self.assets.publish(principal, str(cand['candidate_id']), str(cand['candidate_sha256']), connection=_connection, central=_central)
                    a_tags, a_desc, a_fam, a_par, a_disp = self._inherit_metadata(
                        'algorithm', p_alg_ref, decision.get('tags'), decision.get('description', ''), target_alg_name, connection=_connection,
                    )
                    self.assets.set_metadata(principal, 'algorithm', new_ref, description=a_desc, tags=a_tags, parent_ref=a_par, display_name=a_disp, connection=_connection)
                    old_prefix = alg['path'].rstrip('/') + '/'
                    new_prefix = f'.researchkit/plugins/{alias}/'
                    moved = {p: d for p, d in source_files.items() if p.startswith(old_prefix)}
                    for p in list(moved.keys()):
                        del source_files[p]
                        source_files[new_prefix + p[len(old_prefix):]] = moved[p]
                    plugin_yaml_path = new_prefix + 'plugin.yaml'
                    pmeta = _yaml(source_files.get(plugin_yaml_path, b''), 'plugin.yaml')
                    pmeta['name'] = target_alg_name
                    pmeta['version'] = target_alg_ver
                    source_files[plugin_yaml_path] = _dump(pmeta)
                    cfg_entry = dict(plugins_cfg.get(alias, {}))
                    cfg_entry['path'] = f'.researchkit/plugins/{alias}'
                    cfg_entry['source_ref'] = new_ref
                    cfg_entry.setdefault('parameters', {})
                    cfg_entry.pop('local_source', None)
                    plugins_cfg[alias] = cfg_entry
                    if isinstance(origins_doc.get('algorithms'), dict):
                        origins_doc['algorithms'][alias] = {'ref': new_ref, 'path': f'.researchkit/plugins/{alias}'}
                    promoted_algorithms.append({'alias': alias, 'ref': new_ref, 'mode': 'promoted'})

            project_doc['plugins'] = plugins_cfg
            source_files['project.yaml'] = _dump(project_doc)
            if isinstance(origins_doc.get('algorithms'), dict) and origins_doc['algorithms']:
                source_files['.researchkit/origins.json'] = (json.dumps(origins_doc, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
            if '.researchkit/lock.json' in source_files:
                protected = {
                    p: hashlib.sha256(v).hexdigest()
                    for p, v in source_files.items()
                    if p == 'task' or p.startswith(('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/'))
                }
                source_files['.researchkit/lock.json'] = (
                    json.dumps(
                        {'schema': 'researchkit.lock/v1', 'files': dict(sorted(protected.items()))},
                        ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
                    ) + '\n'
                ).encode('utf-8')

            for ds in self._datasets(submission):
                if not ds.get('needs_resolution'):
                    continue
                did = str(ds['dataset_id'])
                ds_dec = emb_ds_cfg.get(did, {}) if isinstance(emb_ds_cfg.get(did), dict) else {}
                ds_action = str(ds_dec.get('action') or 'keep_private')
                if ds_action == 'keep_private':
                    continue
                if ds_action in {'publish_derived', 'derived', 'promote_public', 'public', 'new_version'}:
                    ds_mode = 'new_version' if (ds_action == 'new_version' or ds_dec.get('parent_ref')) else 'new'
                    ds_scope = 'public' if ds_action in {'promote_public', 'public'} else 'derived'
                    ds_tags_arg = ds_dec.get('tags') if 'tags' in ds_dec else None
                    ds_desc_arg = str(ds_dec.get('description') or '') if 'description' in ds_dec else None
                    pub_ds = self._publish_derived_record(
                        principal,
                        did,
                        publish_mode=ds_mode,
                        parent_ref=str(ds_dec.get('parent_ref') or ''),
                        tags=ds_tags_arg,
                        description=ds_desc_arg,
                        readme_markdown=str(ds_dec.get('readme_markdown') or ''),
                        name_override=str(ds_dec.get('name') or ''),
                        dataset_scope=ds_scope,
                        require_metadata=(ds_mode == 'new'),
                        connection=_connection,
                    )
                    promoted_datasets.append({**pub_ds, 'mode': ds_action})
                else:
                    raise KitError(f'不支持的私有派生数据集处理选项：{ds_action}')

            in_template_publish = True
            manifest_refs = submission.get('manifest', {}).get('data_references', {}).get('references')
            use_override = source_files if source_files != submission['files'] else None
            candidate, reused = self._candidate(
                principal, submission, 'template', case_name, case_version, None,
                source_override=use_override, refs_override=manifest_refs if use_override is not None else None,
                connection=_connection,
            )
            tpl_files = dict(candidate.get('files') or {})
            self._verify_contract_compatibility(tpl_files, connection=_connection)
            prior = self.assets.published_files('template', ref, connection=_connection)
            if prior is not None and prior != tpl_files:
                raise KitError('同一精确版本已存在不同内容；请修改案例版本后重新提交')
            if prior is None:
                self.assets.publish(principal, str(candidate['candidate_id']), str(candidate['candidate_sha256']), connection=_connection, central=_central)
                reused = False
            else:
                reused = True
            c_tags, c_desc, fam_id, par_val, disp = self._inherit_metadata(
                'template',
                p_ref if publish_mode == 'new_version' else '',
                tags,
                description,
                str(project.get('name') or case_name), connection=_connection,
            )
            self.assets.set_metadata(
                principal, 'template', ref,
                description=c_desc, tags=c_tags,
                parent_ref=par_val,
                display_name=disp, connection=_connection,
            )
            rec = self._record(
                submission, 'template', '', ref, str(candidate['candidate_id']),
                'reused' if reused else 'published', principal, note, connection=_connection,
            )
            return {
                'published': [rec],
                'reused': reused,
                'promoted_algorithms': promoted_algorithms,
                'promoted_datasets': promoted_datasets,
            }
        except KitError as exc:
            if in_template_publish:
                raise _PublicationFailure({
                    'published': [], 'reused': False,
                    'failures': [{'alias': None, 'ref': ref, 'error': str(exc)}],
                })
            raise
