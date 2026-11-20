#!/usr/bin/env python3
"""Root/admin safe per-project cleanup and audit CLI for ResearchKit Web.

This tool executes ONLY verified project-purge plans identified by project UUID
and plan_token; it never accepts arbitrary directory paths from callers.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

def _resolve_core_dir() -> Path:
    candidates: list[Path] = []
    env_root = os.environ.get('RKWEB_APP_ROOT', '').strip()
    if env_root:
        candidates.append(Path(env_root) / 'core')
    script_parent = Path(__file__).resolve().parents[1]
    candidates.extend([
        script_parent / 'core',
        Path('/opt/researchkit-web/app/core'),
        Path('/home/ly/ResearchKit/core'),
    ])
    for cand in candidates:
        if (cand / 'researchkit').is_dir():
            return cand
    return script_parent / 'core'


CORE_DIR = _resolve_core_dir()
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

def _reexec_venv_if_needed() -> None:
    if os.environ.get('RKWEB_PURGE_NO_REEXEC') == '1':
        return
    need_reexec = sys.version_info < (3, 10)
    if not need_reexec:
        try:
            import yaml  # noqa: F401
            import researchkit.web_project_purge  # noqa: F401
            return
        except Exception:
            need_reexec = True
    if need_reexec:
        for _venv_py in (
            Path('/opt/researchkit-web/venv/bin/python'),
            Path('/opt/researchkit-web/.venv/bin/python'),
            Path('/home/ly/ResearchKit/.web-venv/bin/python'),
        ):
            if _venv_py.is_file() and Path(sys.executable).resolve() != _venv_py.resolve():
                os.execv(str(_venv_py), [str(_venv_py), *sys.argv])


_reexec_venv_if_needed()

import yaml  # noqa: F401
from researchkit.files import KitError, atomic_text, safe_path
from researchkit.web_broker import BrokerClient
from researchkit.web_identity import Policy, Principal, load_policy
from researchkit.web_project_purge import ProjectPurge, _CONFIRMATION
from researchkit.web_store import SubmissionStore


DEFAULT_POLICY = '/etc/researchkit-web/policy.json'


def _teacher_principal(policy: Policy, username: str | None = None) -> Principal:
    for p in policy.principals.values():
        if username and p.username == username:
            return p
        if not username and p.role == 'teacher' and p.username == 'ly':
            return p
    for p in policy.principals.values():
        if p.role == 'teacher':
            return p
    raise KitError('policy 中未找到教师或指定操作账号')


def _workspace_info_for(policy: Policy, owner_uid: int, project_id: str) -> tuple[dict[str, object], Path | None, Path | None]:
    ws_root = policy.workspaces.get(owner_uid)
    if ws_root is None:
        return {'in_workspace': False, 'exists': False}, None, None
    ws_path = Path(ws_root)
    state_file = ws_path / '.researchkit-web-state.json'
    if not state_file.is_file():
        return {'in_workspace': False, 'exists': False}, ws_path, None
    try:
        payload = json.loads(state_file.read_text(encoding='utf-8'))
        entry = payload.get('projects', {}).get(project_id)
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            return {'in_workspace': False, 'exists': False}, ws_path, None
        proj_dir = safe_path(ws_path, entry['path'])
        proj_dir.relative_to(ws_path / 'projects')
    except (OSError, ValueError, KitError):
        return {'in_workspace': False, 'exists': False}, ws_path, None
    return {
        'in_workspace': True,
        'relative_path': entry['path'],
        'path': str(proj_dir),
        'exists': os.path.lexists(proj_dir),
    }, ws_path, proj_dir


def _remove_workspace_copy(ws_path: Path, project_id: str) -> dict[str, object] | None:
    projects_root = ws_path / 'projects'
    state_file = ws_path / '.researchkit-web-state.json'
    archive_file = ws_path / '.researchkit-web-archive.json'
    if not state_file.is_file():
        return None
    payload = json.loads(state_file.read_text(encoding='utf-8'))
    projects = payload.get('projects', {})
    entry = projects.pop(project_id, None)
    if entry is None:
        return None
    target = safe_path(ws_path, entry['path'])
    target.relative_to(projects_root)
    if os.path.islink(target):
        raise KitError(f'拒绝删除符号链接目录：{target}')
    if target.exists():
        shutil.rmtree(target)
    atomic_text(state_file, json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
    os.chmod(state_file, 0o600)
    if archive_file.is_file():
        arch = json.loads(archive_file.read_text(encoding='utf-8'))
        if isinstance(arch.get('projects'), list) and project_id in arch['projects']:
            arch['projects'] = [x for x in arch['projects'] if x != project_id]
            atomic_text(archive_file, json.dumps(arch, ensure_ascii=False, sort_keys=True) + '\n')
            os.chmod(archive_file, 0o600)
    return {'kind': 'workspace_copy', 'ref': entry['path'], 'name': f"工作区本地目录 {entry['path']}"}


def _backup_database(store: SubmissionStore) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_dir = store.directory / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f'submissions-before-purge-{stamp}.sqlite3'
    with store._connection() as con:
        con.execute('VACUUM INTO ?', (str(target),))
    return target


def _unregister_dataset(policy: Policy, store: SubmissionStore, identifier_or_root: str) -> dict[str, object]:
    """Unregister raw dataset metadata without deleting /home/share/Datasets."""
    removed_ids: list[str] = []
    trees_path = getattr(policy, 'dataset_tree_registry', None)
    if trees_path is not None and Path(trees_path).is_file():
        doc = json.loads(Path(trees_path).read_text(encoding='utf-8'))
        records = doc.get('datasets', doc) if isinstance(doc, dict) else {}
        if isinstance(records, dict):
            for did, rec in list(records.items()):
                rel = rec.get('relative_root') or rec.get('root') if isinstance(rec, dict) else ''
                if did == identifier_or_root or rel == identifier_or_root or str(rel).rstrip('/').endswith(identifier_or_root.rstrip('/')):
                    removed_ids.append(did)
                    del records[did]
            if removed_ids:
                atomic_text(Path(trees_path), json.dumps(doc, ensure_ascii=False, indent=2) + '\n')

    meta_path = getattr(policy, 'dataset_metadata', None)
    if meta_path is not None and Path(meta_path).is_file() and removed_ids:
        mdoc = json.loads(Path(meta_path).read_text(encoding='utf-8'))
        entries = mdoc.get('datasets', mdoc) if isinstance(mdoc, dict) else {}
        changed = False
        if isinstance(entries, dict):
            for did in removed_ids:
                if did in entries:
                    del entries[did]
                    changed = True
        if changed:
            atomic_text(Path(meta_path), json.dumps(mdoc, ensure_ascii=False, indent=2) + '\n')

    with store._connection() as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            for did in removed_ids:
                con.execute("DELETE FROM web_asset_lifecycle WHERE kind='dataset' AND ref=?", (did,))
                con.execute("DELETE FROM web_asset_lifecycle_history WHERE kind='dataset' AND ref=?", (did,))
                con.execute("DELETE FROM web_cleanup_deleted WHERE kind='dataset' AND ref=?", (did,))
            con.execute(
                '''UPDATE data_management_candidates
                   SET status='pending', registered_dataset_id=NULL, error=NULL
                   WHERE dataset_id=? OR registered_dataset_id=? OR relative_root=?''',
                (identifier_or_root, identifier_or_root, identifier_or_root),
            )
            con.execute('COMMIT')
        except Exception:
            if con.in_transaction:
                con.execute('ROLLBACK')
            raise

    return {'unregistered_dataset_ids': removed_ids, 'target': identifier_or_root}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ResearchKit Web 全链路按项目清理与审计工具')
    parser.add_argument('--policy', default=DEFAULT_POLICY, help=f'policy.json 路径（默认 {DEFAULT_POLICY}）')
    parser.add_argument('--dev-policy', action='store_true', help='跳过生产目录权限校验（用于测试）')
    parser.add_argument('--via-broker', action='store_true', help='通过运行中的 broker socket 执行')
    parser.add_argument('--user', default=None, help='操作账号用户名（默认为教师 ly）')
    parser.add_argument('--list', action='store_true', help='列出可清理的项目')
    parser.add_argument('--preview', action='store_true', help='预览指定项目的清理范围与计划令牌')
    parser.add_argument('--apply', action='store_true', help='按已核对的计划令牌执行项目清理')
    parser.add_argument('--audit', action='store_true', help='查看最近按项目清理审计记录')
    parser.add_argument('--unregister-dataset', default=None, help='撤销原始数据集登记（绝不删除 /home/share/Datasets）')
    parser.add_argument('--project', default=None, help='目标项目 UUID')
    parser.add_argument('--plan-token', default=None, help='由 --preview 生成的 64 位计划令牌')
    parser.add_argument('--select', action='append', default=None, help='勾选需连带清理的产出资产（如 algorithm:mnist.cnn@1.0.0）')
    parser.add_argument('--delete-produced', action='store_true', help='自动勾选所有无外部引用的本项目产出资产')
    parser.add_argument('--no-reset-task', action='store_true', help='不重置关联任务/分发的待领取状态')
    parser.add_argument('--remove-workspace', action='store_true', help='同时删除本地工作区项目目录与索引')
    parser.add_argument('--backup', action='store_true', help='执行清理前备份 submissions.sqlite3')
    parser.add_argument('--confirm', action='store_true', help='确认执行不可逆清理')
    parser.add_argument('--limit', type=int, default=30, help='审计记录条数')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式')
    args = parser.parse_args(argv)

    policy = load_policy(args.policy, production=not args.dev_policy)
    principal = _teacher_principal(policy, args.user)

    if args.via_broker:
        client = BrokerClient(policy.broker_socket, policy.broker_uid)
        if args.list:
            out = client.request('project_purge_list')
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if args.audit:
            out = client.request('project_purge_audit', limit=args.limit)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if not args.project:
            parser.error('必须指定 --project <project_id>')
        ws_info, ws_path, _ = _workspace_info_for(policy, principal.uid, args.project)
        if args.preview:
            kwargs: dict[str, object] = {
                'project_id': args.project,
                'reset_task': not args.no_reset_task,
                'workspace_info': ws_info,
            }
            if args.select is not None:
                kwargs['selected_keys'] = args.select
            out = client.request('project_purge_preview', **kwargs)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if args.apply:
            if not args.confirm or not args.plan_token:
                parser.error('--apply 必须同时提供 --plan-token 和 --confirm')
            kwargs = {
                'project_id': args.project,
                'plan_token': args.plan_token,
                'confirmation': _CONFIRMATION,
                'reset_task': not args.no_reset_task,
                'workspace_info': ws_info,
            }
            if args.select is not None:
                kwargs['selected_keys'] = args.select
            receipt = client.request('project_purge_execute', **kwargs)
            if args.remove_workspace and ws_path is not None:
                removed = _remove_workspace_copy(ws_path, args.project)
                if removed and isinstance(receipt.get('audit_id'), str):
                    client.request('project_purge_update_audit', audit_id=receipt['audit_id'], deleted_extra=[removed])
                    receipt.setdefault('deleted', []).append(removed)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        parser.error('请指定 --list / --preview / --apply / --audit')

    store = SubmissionStore(policy.submission_store)
    service = ProjectPurge(store, policy.code_root / 'library', policy)

    if args.unregister_dataset:
        if os.geteuid() not in (0, policy.broker_uid):
            raise KitError('撤销数据集元数据登记需要 root 或服务账户权限')
        out = _unregister_dataset(policy, store, args.unregister_dataset)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.list:
        out = service.list_projects(principal)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.audit:
        out = service.list_audit(principal, limit=args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not args.project:
        parser.error('必须指定 --project <project_id>')

    with store._connection() as con:
        row = con.execute('SELECT owner_uid FROM projects WHERE id=?', (args.project,)).fetchone()
    owner_uid = int(row['owner_uid']) if row is not None else principal.uid
    ws_info, ws_path, _ = _workspace_info_for(policy, owner_uid, args.project)

    if args.preview:
        selected = None if args.select is None and args.delete_produced else (args.select if args.select is not None else None)
        out = service.preview(
            principal, args.project,
            selected_keys=selected,
            reset_task=not args.no_reset_task,
            workspace_info=ws_info,
        )
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            p = out['project']
            print(f"项目：{p['project_name']} ({p['project_id']}) · 所有者：{p['owner_username']}")
            print(f"固定/小版本提交：{p['submission_count']} · 运行：{p['job_count']} · 工作区：{ws_info.get('relative_path', '无')}")
            print(f"计划令牌：{out['plan_token']}")
            print(f"可执行：{out['can_execute']}")
            if out['produced_items']:
                print('产出资产：')
                for item in out['produced_items']:
                    print(f"  - [{item['kind']}] {item['key']} selected={item['selected']} can_delete={item['can_delete']}")
            if out['retained_shared_items']:
                print('保留共享资源：')
                for item in out['retained_shared_items']:
                    print(f"  - [{item['kind']}] {item['ref']} ({item['reason']})")
        return 0

    if args.apply:
        if not args.confirm or not args.plan_token:
            parser.error('--apply 必须同时提供 --plan-token 和 --confirm')
        if args.backup:
            bpath = _backup_database(store)
            print(f'已生成数据库备份：{bpath}', file=sys.stderr)
        selected = args.select
        if args.delete_produced and selected is None:
            preview_doc = service.preview(principal, args.project, reset_task=not args.no_reset_task, workspace_info=ws_info)
            selected = [i['key'] for i in preview_doc['produced_items'] if i['selected']]
        receipt = service.execute(
            principal, args.project,
            plan_token=args.plan_token,
            confirmation=_CONFIRMATION,
            selected_keys=selected,
            reset_task=not args.no_reset_task,
            workspace_info=ws_info,
        )
        if args.remove_workspace and ws_path is not None:
            removed = _remove_workspace_copy(ws_path, args.project)
            if removed and isinstance(receipt.get('audit_id'), str):
                service.update_audit(principal, receipt['audit_id'], deleted_extra=[removed])
                receipt.setdefault('deleted', []).append(removed)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0

    parser.error('请指定操作：--list / --preview / --apply / --audit / --unregister-dataset')
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KitError as exc:
        print(f'错误：{exc}', file=sys.stderr)
        raise SystemExit(2)
