"""Teacher-only purge plans for explicitly selected recycle-bin assets.

Immutable submissions, personal workspaces and execution evidence are outside
this service.  Raw files are handled by the existing restricted root publisher;
only broker-owned copies/blobs are removed here.  Persisted item phases allow
retries without deleting a later replacement at the original path.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import stat
import time
import uuid
from pathlib import Path

from .files import KitError
from .web_lifecycle import AssetLifecycle, _check_kind_ref, _json, _now, _table, _teacher


def reserved(connection, kind, ref):
    if not _table(connection, 'web_cleanup_deleted'):
        return False
    return connection.execute('SELECT 1 FROM web_cleanup_deleted WHERE kind=? AND ref=?', (kind, ref)).fetchone() is not None


def _ensure_preserved_grants_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS web_preserved_dataset_grants (
        source_template_ref TEXT NOT NULL,
        resource_ref TEXT NOT NULL,
        reference_json TEXT NOT NULL DEFAULT '{}',
        public INTEGER NOT NULL DEFAULT 0,
        target_uid INTEGER NOT NULL DEFAULT 0,
        target_username TEXT NOT NULL DEFAULT '',
        grantor_uid INTEGER NOT NULL,
        grantor_username TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    con.execute("""CREATE INDEX IF NOT EXISTS web_preserved_dataset_lookup
        ON web_preserved_dataset_grants(resource_ref, target_uid, target_username)""")


def _preserve_cascaded_grants_for_template(con, template_ref: str, now: str) -> None:
    """Materialize active cascaded grants before deleting a Case template so student projects keep access."""
    if not _table(con, 'web_case_cascaded_grants'):
        return
    rows = con.execute(
        """SELECT template_ref, resource_kind, resource_ref, reference_json,
                  public, target_uid, target_username, grantor_uid, grantor_username
           FROM web_case_cascaded_grants WHERE template_ref=?""",
        (template_ref,),
    ).fetchall()
    if not rows:
        return
    for crow in rows:
        rkind = str(crow['resource_kind'])
        rref = str(crow['resource_ref'])
        is_pub = bool(crow['public'])
        t_uid = int(crow['target_uid'] or 0)
        t_user = str(crow['target_username'] or '')
        if rkind == 'algorithm':
            if _table(con, 'component_policies'):
                if is_pub:
                    con.execute(
                        """INSERT INTO component_policies(kind, ref, public) VALUES (?, ?, 1)
                           ON CONFLICT(kind, ref) DO UPDATE SET public=1""",
                        ('algorithm', rref),
                    )
                else:
                    con.execute(
                        'INSERT OR IGNORE INTO component_policies(kind, ref, public) VALUES (?, ?, 0)',
                        ('algorithm', rref),
                    )
            if t_uid >= 1 and t_user and _table(con, 'grants'):
                con.execute(
                    """INSERT OR IGNORE INTO grants(kind, ref, target_uid, target_username)
                       VALUES (?, ?, ?, ?)""",
                    ('algorithm', rref, t_uid, t_user),
                )
        elif rkind == 'dataset':
            if _table(con, 'web_derived_datasets') and con.execute(
                'SELECT 1 FROM web_derived_datasets WHERE dataset_id=?', (rref,)
            ).fetchone() is not None:
                if _table(con, 'web_dataset_policies'):
                    if is_pub:
                        con.execute(
                            """INSERT INTO web_dataset_policies(dataset_id, public, updated_at)
                               VALUES (?, 1, ?)
                               ON CONFLICT(dataset_id) DO UPDATE SET public=1, updated_at=excluded.updated_at""",
                            (rref, now),
                        )
                    else:
                        con.execute(
                            'INSERT OR IGNORE INTO web_dataset_policies(dataset_id, public, updated_at) VALUES (?, 0, ?)',
                            (rref, now),
                        )
                if t_uid >= 1 and t_user and _table(con, 'web_dataset_grants'):
                    con.execute(
                        'INSERT OR IGNORE INTO web_dataset_grants(dataset_id, target_uid, target_username) VALUES (?, ?, ?)',
                        (rref, t_uid, t_user),
                    )
            _ensure_preserved_grants_table(con)
            con.execute(
                """INSERT INTO web_preserved_dataset_grants
                   (source_template_ref, resource_ref, reference_json, public,
                    target_uid, target_username, grantor_uid, grantor_username, updated_at)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM web_preserved_dataset_grants
                       WHERE resource_ref=? AND reference_json=? AND public=?
                         AND target_uid=? AND target_username=? AND grantor_uid=?
                   )""",
                (
                    template_ref, rref, str(crow['reference_json'] or '{}'), int(is_pub),
                    t_uid, t_user, int(crow['grantor_uid']), str(crow['grantor_username']), now,
                    rref, str(crow['reference_json'] or '{}'), int(is_pub),
                    t_uid, t_user, int(crow['grantor_uid']),
                ),
            )


def require_available(connection, kind, ref):
    if reserved(connection, kind, ref):
        raise KitError('该版本正在永久清理或已经清理，请保留历史副本并使用新的版本')


class Cleanup:
    def __init__(self, store, library_root=None, policy=None):
        self.store, self.policy = store, policy
        self.lifecycle = AssetLifecycle(store, library_root, policy)
        self.lifecycle._prepare_library()
        self.derived = None
        if policy is not None and getattr(policy, 'derived_dataset_root', None) is not None:
            from .web_derived import DerivedDatasets
            self.derived = DerivedDatasets(store, policy.derived_dataset_root)
        with store._connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS web_cleanup_requests (
                request_id TEXT PRIMARY KEY, plan_token TEXT NOT NULL UNIQUE,
                items_json TEXT NOT NULL, actor_uid INTEGER NOT NULL,
                actor_username TEXT NOT NULL, created_at TEXT NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS web_cleanup_items (
                item_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES web_cleanup_requests(request_id),
                ordinal INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, source TEXT NOT NULL,
                info_json TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
                error TEXT, lease_token TEXT, lease_expires REAL, bytes_removed INTEGER NOT NULL DEFAULT 0)''')
            con.execute('''CREATE TABLE IF NOT EXISTS web_cleanup_deleted (
                kind TEXT NOT NULL, ref TEXT NOT NULL, item_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind,ref))''')
            _ensure_preserved_grants_table(con)
            con.execute('''CREATE TABLE IF NOT EXISTS web_direct_cleanup_audit (
                audit_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
                status TEXT NOT NULL, affected_student_references INTEGER NOT NULL DEFAULT 0,
                error TEXT, actor_uid INTEGER NOT NULL, actor_username TEXT NOT NULL, created_at TEXT NOT NULL)''')

    @staticmethod
    def _selection(items):
        if type(items) is not list or not 1 <= len(items) <= 30:
            raise KitError('请明确选择1至30项回收站内容')
        result = []
        for row in items:
            if type(row) is not dict or set(row) != {'kind', 'ref'}:
                raise KitError('只能指定资产类别和准确版本，不能指定目录或身份')
            kind, ref = _check_kind_ref(row['kind'], row['ref'])
            result.append((kind, ref))
        if len(set(result)) != len(result):
            raise KitError('清理选择不能重复')
        return sorted(result)

    def _info(self, con, kind, ref):
        require_available(con, kind, ref)
        self.lifecycle._verify_exists(con, kind, ref)
        state, generation = self.lifecycle._state(con, kind, ref)
        info = {'kind': kind, 'ref': ref, 'name': ref, 'state': state,
                'revision': self.lifecycle._revision(kind, ref, state, generation), 'estimated_bytes': 0}
        if kind in {'algorithm', 'template'}:
            published = con.execute('SELECT * FROM web_asset_published WHERE kind=? AND ref=?', (kind, ref)).fetchone()
            info['source'] = 'web' if published is not None else 'system'
            if published is not None:
                candidate = con.execute('SELECT * FROM web_asset_candidates WHERE candidate_id=?', (published['candidate_id'],)).fetchone()
                if candidate is None:
                    raise KitError('发布记录的候选缺失，不能清理')
                info['_identity'] = candidate['candidate_sha256']
                info['candidate_copies'] = con.execute('SELECT count(*) FROM web_asset_candidates WHERE kind=? AND ref=?',(kind,ref)).fetchone()[0]
                # Verify the exact published bytes before planning a purge.
                operation = self.lifecycle._library._plugin_source if kind == 'algorithm' else self.lifecycle._library._template_source
                operation(ref)
                info['estimated_bytes'] = con.execute('SELECT coalesce(sum(size),0) FROM web_asset_published_files WHERE kind=? AND ref=?', (kind, ref)).fetchone()[0]
                info['estimated_bytes'] += con.execute('''SELECT coalesce(sum(f.size),0) FROM web_asset_candidate_files f
                    JOIN web_asset_candidates c ON c.candidate_id=f.candidate_id WHERE c.kind=? AND c.ref=?''', (kind, ref)).fetchone()[0]
            if _table(con, 'web_central_builtin_assets'):
                builtin = con.execute('SELECT source_revision FROM web_central_builtin_assets WHERE kind=? AND ref=?', (kind,ref)).fetchone()
                if builtin is not None:
                    operation = self.lifecycle._library._plugin_source if kind == 'algorithm' else self.lifecycle._library._template_source
                    operation(ref)
                    info['source'] = 'central'
                    info['_builtin_identity'] = builtin['source_revision']
                    info.setdefault('_identity',builtin['source_revision'])
                    info.setdefault('candidate_copies',0)
                    info['estimated_bytes'] += con.execute('SELECT coalesce(sum(size),0) FROM web_central_builtin_files WHERE kind=? AND ref=?', (kind,ref)).fetchone()[0]
            return info
        if _table(con, 'web_derived_datasets'):
            row = con.execute("SELECT * FROM web_derived_datasets WHERE dataset_id=? AND visibility='public'", (ref,)).fetchone()
            if row is not None:
                if self.policy is None or getattr(self.policy, 'derived_dataset_root', None) is None:
                    raise KitError('托管数据区未配置')
                derived = self.derived
                # Its protected broker-owned directories are checked by the
                # normal data service.  Never take a path from a Web request.
                derived._ensure_root()
                blob = derived._blob_path(row['blob_name'])
                fd = os.open(blob, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    observed = os.fstat(fd)
                    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_uid != os.geteuid() or observed.st_size != row['size']:
                        raise KitError('托管数据文件身份不符')
                finally:
                    os.close(fd)
                info.update(source='derived', name=row['name'], estimated_bytes=row['size'],
                            _record=dict(row), _identity=[observed.st_dev, observed.st_ino, observed.st_size])
                return info
        from .web_dataset_trees import TreeRegistry
        from .web_datasets import DatasetRegistry
        tree = TreeRegistry(getattr(self.policy, 'dataset_tree_registry', None))
        records = tree._load()
        if ref in records:
            record = {key: value for key, value in records[ref].items() if key != 'readers_set'}
            document = tree._manifest(record)
            from .web_dataset_metadata import enrich_catalog
            metadata=enrich_catalog([{'dataset_id':ref}],getattr(self.policy,'dataset_metadata',None))[0]
            info.update(source='raw', name=metadata.get('name') or Path(document['root']).name, file_count=record['file_count'], estimated_bytes=record['total_size'], _record=record,
                        _identity={'schema': document['schema'], 'root': document['root'], 'root_stat': document['root_stat']})
            return info
        legacy = DatasetRegistry(getattr(self.policy, 'dataset_registry', None))._load()
        if ref in legacy:
            info['source'] = 'legacy'
            return info
        raise KitError('没有找到可清理的登记版本')

    def _platform_dataset_storage(self, con, ref: str) -> dict[str, object]:
        """Locate a platform dataset through trusted service registration.

        This deliberately returns a *reporting* path only.  Direct cleanup no
        longer receives a path from the browser and never unlinks this path:
        retired dataset registrations must keep old projects and fixed
        submissions readable until a root administrator makes a separate,
        deliberate filesystem decision.
        """
        if _table(con, 'web_derived_datasets'):
            row = con.execute(
                "SELECT * FROM web_derived_datasets WHERE dataset_id=? AND visibility='public'",
                (ref,),
            ).fetchone()
            if row is not None:
                if self.derived is None:
                    raise KitError('派生数据目录未配置')
                # _blob_path accepts the broker-issued UUID in the database;
                # no request-controlled directory component reaches here.
                return {
                    'source': 'derived',
                    'path': str(self.derived._blob_path(str(row['blob_name']))),
                    'name': str(row['name']),
                }

        from .web_dataset_trees import TreeRegistry
        from .web_datasets import DatasetRegistry

        policy = self.policy
        tree = TreeRegistry(getattr(policy, 'dataset_tree_registry', None))
        records = tree._load()
        if ref in records:
            manifest = tree._manifest(records[ref])
            return {'source': 'raw-tree', 'path': str(manifest['root']), 'name': ref}

        legacy = DatasetRegistry(getattr(policy, 'dataset_registry', None))._load()
        record = legacy.get(ref)
        if record is not None:
            root = Path(record['root'])
            files = list(record['files'])
            # Old single-file registrations are clearer when the handoff
            # note identifies the actual registered file, rather than only
            # its containing directory.
            path = root / str(files[0]['path']) if len(files) == 1 else root
            return {'source': 'raw-legacy', 'path': str(path), 'name': ref}
        raise KitError('该接口只支持已登记的原始数据集或公开派生数据集；私有学生派生数据不能由平台清理入口移除')

    def list_bin(self, principal, *, state='trashed', kind='all', q='', offset=0, limit=50):
        _teacher(principal)
        if not isinstance(state, str) or not isinstance(kind, str) or state not in {'retired', 'trashed'} or kind not in {'all', 'algorithm', 'template', 'dataset'} or not isinstance(q, str) or len(q) > 256 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise KitError('收起列表筛选参数无效')
        with self.store._connection() as con:
            rows = con.execute('''SELECT l.* FROM web_asset_lifecycle l
                WHERE l.state=? AND NOT EXISTS(SELECT 1 FROM web_cleanup_deleted d
                WHERE d.kind=l.kind AND d.ref=l.ref AND d.status='completed') ORDER BY l.updated_at DESC,l.kind,l.ref''', (state,)).fetchall()
            result = []
            for row in rows:
                if kind != 'all' and row['kind'] != kind:
                    continue
                # Pending raw requests stay visible with their status so that
                # a failed cleanup is not mistaken for a successful deletion.
                pending = con.execute('SELECT i.* FROM web_cleanup_items i JOIN web_cleanup_deleted d ON d.item_id=i.item_id WHERE d.kind=? AND d.ref=?', (row['kind'], row['ref'])).fetchone()
                if pending is not None:
                    original = json.loads(pending['info_json'])
                    item = {key: original[key] for key in ('kind','ref','name','source','state','revision')}
                    item.update(cleanup_status=pending['status'], request_id=pending['request_id'], cleanup_error=pending['error'])
                else:
                    item = {'kind':row['kind'],'ref':row['ref'],'name':row['ref'],'state':state,
                            'revision':self.lifecycle._revision(row['kind'],row['ref'],state,row['generation']),
                            'source':'unknown'}
                    if row['kind'] in {'algorithm','template'}:
                        item['source'] = 'web' if con.execute('SELECT 1 FROM web_asset_published WHERE kind=? AND ref=?',(row['kind'],row['ref'])).fetchone() else 'system'
                        if _table(con,'web_central_builtin_assets') and con.execute('SELECT 1 FROM web_central_builtin_assets WHERE kind=? AND ref=?',(row['kind'],row['ref'])).fetchone():
                            item['source']='central'
                    else:
                        derived = con.execute("SELECT name FROM web_derived_datasets WHERE dataset_id=? AND visibility='public'",(row['ref'],)).fetchone() if _table(con,'web_derived_datasets') else None
                        if derived is not None:
                            item.update(source='derived',name=derived['name'])
                        else:
                            item['source']='raw'
                result.append({key: value for key, value in item.items() if not key.startswith('_')})
        if self.policy is not None and getattr(self.policy,'dataset_metadata',None) is not None:
            from .web_dataset_metadata import enrich_catalog
            try:
                names={r['dataset_id']:r.get('name') for r in enrich_catalog([{'dataset_id':r['ref']} for r in result if r['kind']=='dataset'],self.policy.dataset_metadata)}
                for item in result:
                    if names.get(item['ref']):item['name']=names[item['ref']]
            except KitError:
                pass  # Listing remains available; the destructive preview fails closed.
        result=[r for r in result if q.casefold() in (r['ref']+' '+r['name']).casefold()]
        return {'items': result[offset:offset+limit], 'total': len(result), 'offset': offset, 'limit': limit}

    def _plan(self, con, selected):
        selected_set = set(selected)
        output, identities = [], []
        for kind, ref in selected:
            blockers, advisories = [], []
            try:
                info = self._info(con, kind, ref)
                identities.append(info)
                if info['state'] != 'trashed':
                    blockers.append({'label': '尚未进入回收站', 'suggestion': '先将这个准确版本移入回收站，再预览永久清理。'})
                if info['source'] == 'raw':
                    advisories.append(f"将永久删除这个已登记目录中的全部 {info['file_count']} 个数据文件以及根README、清单和登记；父集合与其它数据集保留。")
                if info['source'] == 'derived':
                    advisories.append('将永久删除这一版本的托管文件和登记；来源项目、原始数据和运行历史保留。')
                if info['source'] in {'web','central'}:
                    advisories.append(f"将删除此准确版本的发布副本和 {info['candidate_copies']} 个候选副本；来源固定提交、验证记录和共用契约说明保留。")
                if info['source'] in {'system', 'legacy'}:
                    blockers.append({'label': '系统预装或旧式登记内容', 'suggestion': '网页只清理Web入库副本和托管/README目录数据；这项由管理员单独核对，不删除教学样例或任意系统目录。'})
                if info['source'] == 'raw':
                    from .web_dataset_discovery import load_management_config
                    config = load_management_config(getattr(self.policy, 'dataset_management_config', None))
                    from .web_dataset_trees import TREE_SCHEMA_README
                    if not config.get('prepare_permissions') or info['_identity']['schema'] != TREE_SCHEMA_README:
                        blockers.append({'label': '共享目录清理服务未就绪', 'suggestion': '请管理员确认已启用README受限数据服务；旧式清单不能在网页永久删除。'})
                    else:
                        from .web_cleanup_raw import _registered_roots, _validate_root
                        _, root, *_ = _validate_root(self.policy, config, ref, info['_record'])
                        # Detect shared/nested registrations in the preview,
                        # before a confirmation locks the cleanup task. There
                        # is no quarantine name yet, so check the source only.
                        _registered_roots(self.policy, dataset_id=ref, root=root,
                                          holding=root, allow_target_missing=False)
                report = self.lifecycle._report(con, kind, ref, 0, 1_000_000)
                for note in report['notes'][1:]:
                    blockers.append({'label': '无法完成核对', 'detail': note, 'suggestion': '先修复这条元数据或路径问题，重新预览；不能凭零引用跳过核对。'})
                for use in report['references']:
                    typ = use['type']
                    # A live task material is an independently revocable
                    # right.  It has to be withdrawn from the named task and
                    # recipient first; selecting the same platform resource
                    # must never silently remove grants from other tasks.
                    if typ == 'task-material':
                        supplement = ('；补充记录 ' + str(use['supplement_id'])) if use.get('supplement_id') else ''
                        blockers.append({
                            'label': use['label'],
                            'detail': ' · '.join(str(use.get(key, '')) for key in ('task_name', 'target_username', 'task_id') if use.get(key)),
                            'suggestion': '先在“布置任务”中打开该任务，撤销这位接收者对此资源的授权' + supplement + '；'
                                          '其它任务、公开授权和手工授权不会被一并撤销。',
                            'kind': use['kind'], 'ref': use['ref'], 'task_id': use['task_id'],
                            'target_username': use['target_username'],
                        })
                        continue
                    if typ == 'task-material-history':
                        advisories.append('已撤销任务仍保留独立的材料代码副本作为历史；永久清理平台资源不会删除这些副本。')
                        continue
                    pair = (use.get('kind'), use.get('ref'))
                    if pair in selected_set:
                        continue
                    if kind == 'algorithm' and typ == 'asset-candidate' and not con.execute("SELECT 1 FROM web_asset_published WHERE kind='template' AND ref=?",(use.get('ref'),)).fetchone():
                        advisories.append('未发布候选保留自己的固定代码副本，不会随平台算法一起删除。')
                        continue
                    if kind in {'algorithm', 'template'} and typ in {'fixed-submission','web-project-summary','template-instance','distribution','package_bundle','trusted-baseline','package-baseline'}:
                        if '已有项目和固定提交保存独立代码副本，保留这些历史，不会删除个人目录。' not in advisories:
                            advisories.append('已有项目和固定提交保存独立代码副本，保留这些历史，不会删除个人目录。')
                        continue
                    if typ == 'derived-dataset':
                        advisories.append('派生数据只记录来源，使用自己的独立文件；其来源说明和文件保留。')
                        continue
                    detail = ' · '.join(str(use.get(key,'')) for key in ('owner_username','project_id','submission_id','detail') if use.get(key))
                    suggestion = '先处理该引用，再重新预览。'
                    if typ == 'asset-candidate' and not con.execute('SELECT 1 FROM web_asset_published WHERE kind=? AND ref=?',(use.get('kind'),use.get('ref'))).fetchone():
                        suggestion = '这是未发布候选的外部数据引用，尚不属于回收站中的已登记资产；请保留这个数据版本。不能为清理而顺带删除未选中的候选或历史。'
                    elif typ in {'platform-template','asset-candidate'}:
                        suggestion = '若这个案例也是测试内容，先将它移入回收站，并与本项一起勾选；系统会先清理案例，再清理它使用的算法或数据。未入回收站的内容不会连带删除。'
                        if use.get('kind') and use.get('ref'):
                            suggestion = f"先处理 {use['ref']}：" + suggestion
                    elif typ == 'fixed-submission':
                        suggestion = '该固定提交仍从外部读取此数据。本次不删除历史提交，因此必须保留这个数据版本；只改当前项目不能解除旧提交的引用。'
                    elif typ == 'web-project-summary':
                        suggestion = '由这个账号打开项目，移除或替换该数据引用并在网页保存/重新提交，再预览；不要直接删除别人目录。'
                    blocker = {'label': use['label'], 'detail': detail, 'suggestion': suggestion}
                    if use.get('kind') and use.get('ref'):
                        blocker.update(kind=use['kind'], ref=use['ref'])
                    blockers.append(blocker)
                if kind == 'dataset' and _table(con, 'data_management_jobs'):
                    if con.execute('''SELECT 1 FROM data_management_jobs j JOIN data_management_candidates c ON c.candidate_id=j.candidate_id
                        WHERE (c.registered_dataset_id=? OR c.dataset_id=?) AND j.status IN ('queued','running') AND j.kind='publish' LIMIT 1''', (ref, ref)).fetchone():
                        blockers.append({'label': '有尚未结束的登记或授权任务', 'suggestion': '等待数据集管理中的任务完成后重新预览。'})
            except (KitError, OSError, ValueError) as error:
                info = {'kind':kind, 'ref':ref, 'name':ref, 'estimated_bytes':0}
                from .web_broker import _safe_message
                blockers.append({'label':'无法核对选中内容', 'detail':_safe_message(error), 'suggestion':'刷新回收站并修复登记状态后重试。'})
            output.append({key:value for key,value in info.items() if not key.startswith('_')} | {'can_delete':not blockers,'blockers':blockers,'advisories':list(dict.fromkeys(advisories))})
        order = sorted(selected, key=lambda pair: ({'template':0,'algorithm':1,'dataset':2}[pair[0]],pair[1]))
        # Identity and references are part of the token, not merely lifecycle
        # state. A new dependency invalidates the confirmed preview.
        public = {'items':output,'order':[{'kind':k,'ref':r} for k,r in order],
                  'total_bytes':sum(item['estimated_bytes'] for item in output),
                  'can_execute':all(item['can_delete'] for item in output),
                  'notes':['只永久删除本次明确勾选的回收站入库副本/数据文件；个人项目、固定提交和运行历史保留。',
                           '依赖先于被依赖内容清理：通常先案例模板，再算法，最后数据。未勾选或尚未入回收站的内容不会自动加入。',
                           '算法/案例内容在数据库中删除后空间可复用；预计字节不是立即释放的磁盘空间承诺。数据文件清理成功后才计入删除字节。',
                           '未同步的IDE修改和平台外使用无法保证覆盖；维护者需确认所选内容确实是可丢弃的测试内容。']}
        public['plan_token'] = hashlib.sha256(_json({'plan':public,'identities':identities}).encode()).hexdigest()
        return public, { (row['kind'],row['ref']):row for row in identities }

    def preview(self, principal, items):
        _teacher(principal)
        selected = self._selection(items)
        with self.store._connection() as con:
            return self._plan(con, selected)[0]

    def execute(self, principal, items, plan_token, confirmation):
        uid, username = _teacher(principal)
        selected = self._selection(items)
        if confirmation != '永久清理' or not isinstance(plan_token, str) or len(plan_token) != 64:
            raise KitError('必须先预览，并明确输入“永久清理”确认')
        selection_json = _json([{'kind':k,'ref':r} for k,r in selected])
        with self.store._connection() as con:
            con.execute('PRAGMA secure_delete=ON')
            con.execute('BEGIN IMMEDIATE')
            try:
                previous = con.execute('SELECT * FROM web_cleanup_requests WHERE plan_token=?', (plan_token,)).fetchone()
                if previous is not None:
                    if previous['items_json'] != selection_json or previous['actor_uid'] != uid or previous['actor_username'] != username:
                        raise KitError('清理确认与请求不一致')
                    request_id = previous['request_id']
                else:
                    plan, identities = self._plan(con, selected)
                    if plan['plan_token'] != plan_token:
                        raise KitError('清理预览冲突，内容或引用已变化，请重新预览')
                    if not plan['can_execute']:
                        raise KitError('存在引用或未完成核对，不能永久清理；请按预览建议处理')
                    request_id = str(uuid.uuid4())
                    con.execute('INSERT INTO web_cleanup_requests VALUES (?,?,?,?,?,?)', (request_id,plan_token,selection_json,uid,username,_now()))
                    for number, item in enumerate(plan['order']):
                        kind, ref = item['kind'],item['ref']; info=identities[(kind,ref)]; item_id=str(uuid.uuid4())
                        con.execute('INSERT INTO web_cleanup_items(item_id,request_id,ordinal,kind,ref,source,info_json,status,phase) VALUES(?,?,?,?,?,?,?,\'queued\',\'prepared\')',
                                    (item_id,request_id,number,kind,ref,info['source'],_json(info)))
                        con.execute('INSERT INTO web_cleanup_deleted VALUES(?,?,?,\'pending\',?)',(kind,ref,item_id,_now()))
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction: con.execute('ROLLBACK')
                raise
        self._drain_local(request_id)
        return self.status(principal, request_id, process=False)

    @staticmethod
    def _delete_web(con, info):
        kind, ref = info['kind'],info['ref']
        # Task material bytes are independent historical copies.  A cleanup
        # of a published resource must neither remove them nor revoke another
        # task's grants implicitly.  The exact live-grant check is repeated
        # inside the destructive transaction as defence in depth.
        if all(_table(con, table) for table in ('web_task_grants', 'web_task_materials', 'web_task_recipients')):
            active = con.execute('''SELECT 1 FROM web_task_grants grants
                JOIN web_task_materials materials ON materials.id=grants.material_id
                JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
                JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
                  AND recipients.target_uid=grants.target_uid
                  AND recipients.target_username=grants.target_username
                WHERE grants.grant_kind=? AND grants.resource_ref=? AND materials.kind=?
                  AND materials.ref=? AND grants.revoked_at IS NULL
                  AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL) LIMIT 1''',
                (kind, ref, kind, ref)).fetchone()
            if active is not None:
                raise KitError('任务材料仍在授权，不能永久清理；请先撤销准确任务授权')
        # All code-bearing platform copies of this exact version are removed;
        # fixed source/validation submissions and their evidence remain.
        candidates = [r['candidate_id'] for r in con.execute('SELECT candidate_id FROM web_asset_candidates WHERE kind=? AND ref=?',(kind,ref))]
        for candidate_id in candidates:
            con.execute('DELETE FROM web_asset_candidate_files WHERE candidate_id=?',(candidate_id,))
        con.execute('DELETE FROM web_asset_published_files WHERE kind=? AND ref=?',(kind,ref))
        con.execute('DELETE FROM web_asset_published WHERE kind=? AND ref=?',(kind,ref))
        con.execute('DELETE FROM web_asset_candidates WHERE kind=? AND ref=?',(kind,ref))
        if _table(con,'web_central_builtin_assets'):
            con.execute('DELETE FROM web_central_builtin_files WHERE kind=? AND ref=?',(kind,ref))
            con.execute('DELETE FROM web_central_builtin_assets WHERE kind=? AND ref=?',(kind,ref))
        if kind == 'template':
            _preserve_cascaded_grants_for_template(con, ref, _now())
            if _table(con, 'web_case_cascaded_grants'):
                con.execute('DELETE FROM web_case_cascaded_grants WHERE template_ref=?', (ref,))
        elif _table(con, 'web_case_cascaded_grants'):
            con.execute('DELETE FROM web_case_cascaded_grants WHERE resource_kind=? AND resource_ref=?', (kind, ref))
        for table in ('grants','component_policies'):
            if _table(con,table): con.execute(f'DELETE FROM {table} WHERE kind=? AND ref=?',(kind,ref))
        # Shared contract definitions are intentionally retained: they are
        # bounded schema metadata and may reserve semantics for other assets.

    def _complete(self, con, row, amount):
        con.execute("UPDATE web_cleanup_items SET status='completed',phase='completed',error=NULL,bytes_removed=? WHERE item_id=?",(amount,row['item_id']))
        con.execute("UPDATE web_cleanup_deleted SET status='completed' WHERE item_id=?",(row['item_id'],))

    def _drain_local(self, request_id):
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise KitError('清理请求ID无效')
        directory = Path(self.store.directory)
        fd = os.open(directory / ('.cleanup-' + request_id + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            identity = os.fstat(fd)
            if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid() or identity.st_nlink != 1 or stat.S_IMODE(identity.st_mode) != 0o600:
                raise KitError('清理操作锁不受信任')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._drain_local_unlocked(request_id)
        finally:
            os.close(fd)

    def _drain_local_unlocked(self, request_id):
        with self.store._connection() as con:
            rows=con.execute("SELECT * FROM web_cleanup_items WHERE request_id=? AND source<>'raw' AND status IN ('queued','running') ORDER BY ordinal",(request_id,)).fetchall()
        for row in rows:
            with self.store._connection() as con:
                if con.execute("SELECT 1 FROM web_cleanup_items WHERE request_id=? AND ordinal<? AND status<>'completed'",(request_id,row['ordinal'])).fetchone():
                    break
            info=json.loads(row['info_json'])
            try:
                if row['source']=='derived': self._purge_derived(row,info)
                else:
                    with self.store._connection() as con:
                        con.execute('PRAGMA secure_delete=ON');con.execute('BEGIN IMMEDIATE')
                        current=con.execute('SELECT * FROM web_cleanup_items WHERE item_id=?',(row['item_id'],)).fetchone()
                        if current['status']!='completed':
                            self._delete_web(con,info);self._complete(con,row,info['estimated_bytes'])
                        con.execute('COMMIT')
            except Exception as error:
                from .web_broker import _safe_message
                with self.store._connection() as con:
                    con.execute("UPDATE web_cleanup_items SET status='failed',error=? WHERE item_id=?",(_safe_message(error),row['item_id']))
                break

    def _purge_derived(self,row,info):
        # The item journal is committed before rename. Both names are confined
        # to the protected broker blob directory; the UUID cannot be a path.
        derived=self.derived;derived._ensure_root()
        blob=derived._blob_path(info['_record']['blob_name'])
        holding=blob.parent/('.cleanup-'+row['item_id']+'.blob')
        def verify(path):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                st=os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_nlink!=1 or [st.st_dev,st.st_ino,st.st_size]!=info['_identity']:
                    raise KitError('待清理托管文件已变化，停止删除')
            finally:os.close(fd)
        with self.store._connection() as con:
            current=con.execute('SELECT phase FROM web_cleanup_items WHERE item_id=?',(row['item_id'],)).fetchone()['phase']
        if current=='prepared':
            if os.path.lexists(holding): verify(holding)
            else:
                verify(blob)
                # Protected parent has no untrusted writers. Refuse an
                # unexpected holding name rather than overwriting it.
                if os.path.lexists(holding):raise KitError('清理临时文件冲突')
                os.rename(blob,holding)
            with self.store._connection() as con:
                con.execute('BEGIN IMMEDIATE')
                actual=con.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?',(info['ref'],)).fetchone()
                if actual is None or dict(actual)!=info['_record']:raise KitError('托管登记已变化，停止清理')
                con.execute('DELETE FROM web_derived_review_grants WHERE dataset_id=?',(info['ref'],))
                if _table(con, 'web_derived_legacy_source_grants'):
                    con.execute('DELETE FROM web_derived_legacy_source_grants WHERE dataset_id=?', (info['ref'],))
                con.execute('DELETE FROM web_derived_datasets WHERE dataset_id=?',(info['ref'],))
                con.execute("UPDATE web_cleanup_items SET phase='detached',status='running',error=NULL WHERE item_id=?",(row['item_id'],))
                con.execute('COMMIT')
        if os.path.lexists(holding):verify(holding);os.unlink(holding)
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE');self._complete(con,row,info['estimated_bytes']);con.execute('COMMIT')

    def status(self,principal,request_id,*,process=True):
        _teacher(principal)
        try: uuid.UUID(request_id)
        except (ValueError,TypeError,AttributeError):raise KitError('清理请求ID无效')
        with self.store._connection() as con:
            if con.execute('SELECT 1 FROM web_cleanup_requests WHERE request_id=?',(request_id,)).fetchone() is None:
                raise KitError('清理请求不存在')
        if process:self._drain_local(request_id)
        with self.store._connection() as con:
            rows=con.execute('SELECT * FROM web_cleanup_items WHERE request_id=? ORDER BY ordinal',(request_id,)).fetchall()
        states={row['status'] for row in rows}
        state='failed' if 'failed' in states else 'completed' if states=={'completed'} else 'running' if 'running' in states else 'queued'
        return {'request_id':request_id,'status':state,'items':[{'kind':r['kind'],'ref':r['ref'],'status':r['status'],'bytes_removed':r['bytes_removed'],'error':r['error']} for r in rows],
                'error':'；'.join(r['error'] for r in rows if r['error']) or None}

    def retry(self,principal,request_id):
        _teacher(principal);self.status(principal,request_id,process=False)
        with self.store._connection() as con:
            con.execute("UPDATE web_cleanup_items SET status='queued',error=NULL WHERE request_id=? AND status='failed'",(request_id,))
        self._drain_local(request_id)
        return self.status(principal,request_id,process=False)

    def publisher_restart(self):
        # The root publisher holds its lifetime flock before registering, so
        # a restarted worker can resume its old receipt immediately.
        with self.store._connection() as con:
            con.execute("UPDATE web_cleanup_items SET status='queued',lease_token=NULL,lease_expires=NULL WHERE source='raw' AND status='running'")

    def claim_raw(self):
        with self.store._connection() as con:
            pending=con.execute("SELECT DISTINCT request_id FROM web_cleanup_items WHERE source<>'raw' AND status IN ('queued','running')").fetchall()
        for request in pending:
            self._drain_local(request['request_id'])
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("SELECT i.* FROM web_cleanup_items i WHERE source='raw' AND (status='queued' OR (status='running' AND lease_expires<?)) AND NOT EXISTS(SELECT 1 FROM web_cleanup_items previous WHERE previous.request_id=i.request_id AND previous.ordinal<i.ordinal AND previous.status<>'completed') ORDER BY i.rowid LIMIT 1",(time.time(),)).fetchone()
            if row is None:con.execute('COMMIT');return None
            # Only claim after all earlier batch items finished. Do not
            # silently execute later dependencies after a preceding failure.
            if con.execute("SELECT 1 FROM web_cleanup_items WHERE request_id=? AND ordinal<? AND status<>'completed'",(row['request_id'],row['ordinal'])).fetchone():
                con.execute('COMMIT');return None
            token=uuid.uuid4().hex
            con.execute("UPDATE web_cleanup_items SET status='running',lease_token=?,lease_expires=? WHERE item_id=?",(token,time.time()+7200,row['item_id']))
            con.execute('COMMIT')
        info=json.loads(row['info_json'])
        return {'item_id':row['item_id'],'lease_token':token,'request':{'request_id':row['item_id'],'dataset_id':row['ref'],'expected_entry':info['_record']}}

    def finish_raw(self,item_id,lease_token,result=None,error=''):
        if not isinstance(error,str) or len(error)>4096:raise KitError('清理回传格式无效')
        with self.store._connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT * FROM web_cleanup_items WHERE item_id=?',(item_id,)).fetchone()
            if row is None or row['source']!='raw' or row['status']!='running' or row['lease_token']!=lease_token:
                raise KitError('清理任务租约无效')
            if not error and (type(result)is not dict or result.get('dataset_id')!=row['ref'] or result.get('deleted')is not True):raise KitError('清理回传身份不一致')
            if error:con.execute("UPDATE web_cleanup_items SET status='failed',error=? WHERE item_id=?",(error,item_id))
            else:
                self._complete(con,row,json.loads(row['info_json'])['estimated_bytes'])
                if _table(con,'data_management_candidates'):
                    con.execute("UPDATE data_management_candidates SET registered_dataset_id=NULL,dataset_id=NULL,status='ignored',kind='missing',error='这个数据版本已永久清理',updated_at=? WHERE registered_dataset_id=?",(_now(),row['ref']))
            con.execute('COMMIT')
        return {'item_id':item_id,'status':'failed' if error else 'completed'}

    def check_cleanup_references(self, principal, kind, ref):
        # Direct platform cleanup has one dependency rule: an algorithm or a
        # dataset cannot disappear from the platform catalogue while an active
        # platform Case still names it.  Student projects and submissions keep
        # their own code/reference history, so they are never a direct-cleanup
        # blocker and are not candidates for a forced side-effect.
        _teacher(principal)
        norm_kind = 'template' if kind == 'case' else ('dataset' if kind in {'derived', 'derived-dataset'} else kind)
        norm_kind, norm_ref = _check_kind_ref(norm_kind, ref)
        self.lifecycle._prepare_library()
        blocking_cases = []
        student_references = []
        notes = []
        with self.store._connection() as con:
            self.lifecycle._verify_exists(con, norm_kind, norm_ref)
            if norm_kind in {'algorithm', 'dataset'}:
                t_refs, t_notes = self.lifecycle._installed_refs(norm_kind, norm_ref)
                notes.extend(t_notes)
                for item in t_refs:
                    if item.get('type') == 'platform-template':
                        blocking_cases.append({
                            'ref': item.get('ref') or item.get('detail') or '',
                            'label': item.get('label') or '平台案例正在使用此资源',
                        })
            if norm_kind == 'dataset':
                self._platform_dataset_storage(con, norm_ref)
        can_delete = len(blocking_cases) == 0 and len(notes) == 0
        requires_force = False
        message = ''
        if blocking_cases:
            names = '、'.join(c['ref'] for c in blocking_cases if c.get('ref'))
            message = f'仍有平台案例正在使用该{"算法" if norm_kind == "algorithm" else "数据集"}（{names}），请先清理这些案例后再清理。'
        elif notes:
            message = '；'.join(notes)
        return {
            'kind': norm_kind,
            'ref': norm_ref,
            'can_delete': can_delete,
            'requires_force': requires_force,
            'blocking_cases': blocking_cases,
            'student_references': student_references,
            'student_project_refs': student_references,
            'notes': notes,
            'message': message,
        }

    def _check_cleanup_references_unlocked(self, con, kind: str, ref: str):
        norm_kind = 'template' if kind == 'case' else ('dataset' if kind in {'derived', 'derived-dataset'} else kind)
        norm_kind, norm_ref = _check_kind_ref(norm_kind, ref)
        self.lifecycle._prepare_library()
        blocking_cases = []
        student_references = []
        notes = []
        self.lifecycle._verify_exists(con, norm_kind, norm_ref)
        if norm_kind in {'algorithm', 'dataset'}:
            t_refs, t_notes = self.lifecycle._installed_refs(norm_kind, norm_ref)
            notes.extend(t_notes)
            for item in t_refs:
                if item.get('type') == 'platform-template':
                    blocking_cases.append({
                        'ref': item.get('ref') or item.get('detail') or '',
                    'label': item.get('label') or '平台案例正在使用此资源',
                })
        if norm_kind == 'dataset':
            self._platform_dataset_storage(con, norm_ref)
        can_delete = len(blocking_cases) == 0 and len(notes) == 0
        requires_force = False
        message = ''
        if blocking_cases:
            names = '、'.join(c['ref'] for c in blocking_cases if c.get('ref'))
            kind_label = '算法' if norm_kind == 'algorithm' else '数据集'
            message = f'仍有平台案例正在使用该{kind_label}（{names}），请先清理这些案例后再清理。'
        elif notes:
            message = '；'.join(notes)
        return {
            'kind': norm_kind,
            'ref': norm_ref,
            'can_delete': can_delete,
            'requires_force': requires_force,
            'blocking_cases': blocking_cases,
            'student_references': student_references,
            'student_project_refs': student_references,
            'notes': notes,
            'message': message,
        }

    def direct_cleanup(self, principal, kind, ref, confirmation='', *, force_student_projects=False):
        # Directly clean up one platform Case, Algorithm, or Derived Dataset by layer.
        actor_uid, actor_username = _teacher(principal)
        if confirmation != '永久清理':
            raise KitError('请输入“永久清理”确认执行')
        norm_kind_hint = 'template' if kind == 'case' else ('dataset' if kind in {'derived', 'derived-dataset'} else kind)
        norm_kind, norm_ref = _check_kind_ref(norm_kind_hint, ref)
        now = _now()
        audit_id = str(uuid.uuid4())

        with self.store._connection() as con:
            con.execute('PRAGMA secure_delete=ON')
            if norm_kind in {'template', 'algorithm'}:
                con.execute('BEGIN IMMEDIATE')
                try:
                    check = self._check_cleanup_references_unlocked(con, norm_kind, norm_ref)
                    if not check['can_delete']:
                        raise KitError(check['message'] or '存在引用阻断，不能清理')
                    published = con.execute('SELECT 1 FROM web_asset_published WHERE kind=? AND ref=?', (norm_kind, norm_ref)).fetchone()
                    builtin = _table(con, 'web_central_builtin_assets') and con.execute(
                        'SELECT 1 FROM web_central_builtin_assets WHERE kind=? AND ref=?', (norm_kind, norm_ref)
                    ).fetchone()
                    if not published and not builtin:
                        raise KitError('仅支持在网页清理已入库或平台库登记的资源版本')
                    if norm_kind == 'template':
                        _preserve_cascaded_grants_for_template(con, norm_ref, now)
                    candidates = [r['candidate_id'] for r in con.execute('SELECT candidate_id FROM web_asset_candidates WHERE kind=? AND ref=?', (norm_kind, norm_ref))]
                    for cid in candidates:
                        con.execute('DELETE FROM web_asset_candidate_files WHERE candidate_id=?', (cid,))
                    con.execute('DELETE FROM web_asset_published_files WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                    con.execute('DELETE FROM web_asset_published WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                    con.execute('DELETE FROM web_asset_candidates WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                    if _table(con, 'web_central_builtin_assets'):
                        con.execute('DELETE FROM web_central_builtin_files WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                        con.execute('DELETE FROM web_central_builtin_assets WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                    for table in ('grants', 'component_policies', 'web_asset_metadata', 'web_asset_lifecycle', 'web_asset_lifecycle_history'):
                        if _table(con, table):
                            con.execute(f'DELETE FROM {table} WHERE kind=? AND ref=?', (norm_kind, norm_ref))
                    if norm_kind == 'template' and _table(con, 'web_case_cascaded_grants'):
                        con.execute('DELETE FROM web_case_cascaded_grants WHERE template_ref=?', (norm_ref,))
                    elif _table(con, 'web_case_cascaded_grants'):
                        con.execute('DELETE FROM web_case_cascaded_grants WHERE resource_kind=? AND resource_ref=?', (norm_kind, norm_ref))
                    con.execute(
                        """INSERT INTO web_direct_cleanup_audit
                           (audit_id, kind, ref, status, affected_student_references, error, actor_uid, actor_username, created_at)
                           VALUES (?, ?, ?, 'completed', 0, NULL, ?, ?, ?)""",
                        (audit_id, norm_kind, norm_ref, actor_uid, actor_username, now),
                    )
                    con.execute('COMMIT')
                except Exception:
                    if con.in_transaction:
                        con.execute('ROLLBACK')
                    raise
                return {'audit_id': audit_id, 'kind': norm_kind, 'ref': norm_ref, 'status': 'completed', 'deleted': True}

            # A platform dataset is removed from *new selection* only.  Its
            # registry/derived row, ACLs, provenance and bytes are retained:
            # old submissions must continue to mount their exact immutable
            # reference, and filesystem deletion remains a root-only manual
            # maintenance decision.  Keep the legacy force argument accepted
            # for old clients, but deliberately give it no destructive role.
            con.execute('BEGIN IMMEDIATE')
            try:
                check = self._check_cleanup_references_unlocked(con, 'dataset', norm_ref)
                if not check['can_delete']:
                    raise KitError(check['message'] or '存在引用阻断，不能清理')
                storage = self._platform_dataset_storage(con, norm_ref)
                state, generation = self.lifecycle._state(con, 'dataset', norm_ref)
                if state != 'retired':
                    generation += 1
                    con.execute(
                        '''INSERT INTO web_asset_lifecycle
                           (kind,ref,state,generation,updated_at,updated_by_uid,updated_by_username)
                           VALUES ('dataset', ?, 'retired', ?, ?, ?, ?)
                           ON CONFLICT(kind,ref) DO UPDATE SET state=excluded.state,
                               generation=excluded.generation,updated_at=excluded.updated_at,
                               updated_by_uid=excluded.updated_by_uid,
                               updated_by_username=excluded.updated_by_username''',
                        (norm_ref, generation, now, actor_uid, actor_username),
                    )
                    con.execute(
                        '''INSERT INTO web_asset_lifecycle_history
                           (kind,ref,action,actor_uid,actor_username,created_at)
                           VALUES ('dataset', ?, 'retire', ?, ?, ?)''',
                        (norm_ref, actor_uid, actor_username, now),
                    )
                con.execute(
                    """INSERT INTO web_direct_cleanup_audit
                       (audit_id, kind, ref, status, affected_student_references, error, actor_uid, actor_username, created_at)
                       VALUES (?, 'dataset', ?, 'completed', 0, NULL, ?, ?, ?)""",
                    (audit_id, norm_ref, actor_uid, actor_username, now),
                )
                con.execute('COMMIT')
            except Exception:
                if con.in_transaction:
                    con.execute('ROLLBACK')
                raise
            return {
                'audit_id': audit_id,
                'kind': 'dataset',
                'ref': norm_ref,
                'status': 'completed',
                'deleted': True,
                'platform_removed': True,
                'physical_files_deleted': False,
                'manual_cleanup': {
                    'path': storage['path'],
                    'message': '已移除平台中的新建选择登记；磁盘文件、原始/派生登记、授权和历史引用均已保留。若确需删除磁盘文件，请由 root 在确认没有平台外用途后单独处理。',
                },
            }
