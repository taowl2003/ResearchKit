"""Loopback-only Tornado UI for a single authenticated Linux workspace.

It is intentionally a per-user process.  SSH authenticates the user that
starts it, ``current_principal`` verifies that UID against the protected
policy, and the broker obtains the same UID again through ``SO_PEERCRED``.
There is no username login or account-selection endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import socket
import re
import pwd
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import tornado.httpserver
import tornado.ioloop
import tornado.netutil
import tornado.web

from .files import KitError
from .web_identity import IdentityError, Principal, current_principal, load_policy
from .web_store import SubmissionStore
from .web_workspace import Workspace, _secure_directory
from .web_packages import MAX_PACKAGE_BYTES


MAX_HTTP_BODY = 1_200_000
_STATIC = Path(__file__).with_name('web_static')


def _asset_view(value):
    """Candidate HTTP detail contains a manifest, never implicit file bytes."""
    value = dict(value)
    if 'files' in value:
        import hashlib
        value['files'] = [{'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                          for path, content in sorted(value['files'].items())]
    return value


class LocalStoreClient:
    """Demo-only direct adapter; production always uses the UDS broker."""
    def __init__(self, store: SubmissionStore, principal: Principal, *, library_root: Path | str | None = None):
        self.store, self.principal = store, principal
        from .web_task_delivery import TaskDelivery
        TaskDelivery(store)
        self.library_root = Path(library_root) if library_root is not None else Path(__file__).resolve().parents[2] / 'library'

    def request(self, method: str, **params: object) -> dict[str, object] | list[dict[str, object]]:
        from .web_execution_policy import retired_method
        retired_method(method)
        from .web_feedback_rpc import METHODS as FEEDBACK_METHODS, request as feedback_request
        if method in FEEDBACK_METHODS:
            return feedback_request(self.store, self.principal, method, params)
        if method == 'list_project_jobs':
            from .web_personal_resources import project_jobs
            if set(params) != {'project_id'}:
                raise KitError('项目执行记录参数无效')
            return project_jobs(self.store, self.principal, params['project_id'])
        if method == 'relate_dataset_version':
            raise KitError('本地演示没有已登记的数据集版本')
        from .web_workflow_rpc import METHODS as WORKFLOW_METHODS, request as workflow_request
        if method in WORKFLOW_METHODS:
            return workflow_request(self.store, self.library_root, self.principal, method, params, ready=False)
        from .web_task_rpc import METHODS as TASK_METHODS, request as task_request
        if method in TASK_METHODS:
            from types import SimpleNamespace
            def targets(principal,names,refs):
                if principal.role != 'teacher' or names != [principal.username]:
                    raise KitError('本地演示只能向自己分发；跨用户请使用正式 broker')
                if refs:raise KitError('本地演示不支持外部数据任务')
                return [principal],[]
            server=SimpleNamespace(store=self.store,policy=SimpleNamespace(code_root=self.library_root.parent),
                _dataset_catalog=lambda principal:[],_assignment_targets=targets)
            return task_request(server,self.principal,method,params)
        from .web_broker import (ASSET_METHODS, ASSET_VALIDATION_METHODS, asset_request,
                                 asset_validation_request)
        if method in ASSET_METHODS:
            return asset_request(self.store, self.library_root, self.principal, method, params)
        if method in ASSET_VALIDATION_METHODS:
            if method == 'list_asset_candidate_validations':
                return asset_validation_request(self.store, self.library_root, self.principal, method, params,
                                                ready=False)
            raise KitError('本地演示不允许创建候选验证；请使用正式执行服务')
        if method in {'list_asset_bin','preview_asset_cleanup','execute_asset_cleanup','get_asset_cleanup','retry_asset_cleanup'}:
            from .web_cleanup import Cleanup
            service = Cleanup(self.store, self.library_root)
            operation={'list_asset_bin':service.list_bin,'preview_asset_cleanup':service.preview,
                       'execute_asset_cleanup':service.execute,'get_asset_cleanup':service.status,
                       'retry_asset_cleanup':service.retry}[method]
            return operation(self.principal, **params)
        if method in {'get_asset_lifecycle', 'set_asset_lifecycle', 'record_project_usage'}:
            from .web_lifecycle import AssetLifecycle
            lifecycle = AssetLifecycle(self.store, self.library_root)
            if method == 'record_project_usage':
                return lifecycle.record_usage(self.principal, **params)
            operation = lifecycle.report if method == 'get_asset_lifecycle' else lifecycle.change
            return operation(self.principal, **params)
        if method == 'get_dataset_catalog':
            return []
        if method == 'get_dataset_readme':
            raise KitError('本地演示没有已授权的 README 数据集')
        if method == 'get_derived_catalog':
            return {'items': [], 'next_cursor': None}
        if method in {'begin_derived_upload', 'append_derived_upload', 'commit_derived_upload', 'abort_derived_upload', 'save_run_artifacts_as_derived', 'grant_derived_review', 'publish_derived'}:
            raise KitError('本地演示不提供托管派生数据；请使用正式 broker 和配置的数据区')
        if method == 'execution_status':
            from .web_runner import LIMITS
            return {'enabled': False, 'reason': '本地演示不提供运行服务', 'limits': LIMITS}
        if method == 'request_job':
            raise KitError('本地演示不允许执行项目')
        if method in {'list_jobs', 'get_job', 'cancel_job', 'job_artifacts'}:
            from .web_jobs import Jobs
            jobs = Jobs(self.store)
            if method == 'list_jobs':
                return jobs.list(self.principal, params.get('submission_id'), personal=True)
            if method == 'job_artifacts':
                return {'files': jobs.artifacts(self.principal, params['run_id'])}
            return getattr(jobs, 'get' if method == 'get_job' else 'cancel')(self.principal, params['run_id'])
        if method == 'export_package':
            return self.store.export_package(params['submission_id'], self.principal)
        if method == 'import_package':
            return self.store.import_package(self.principal, params['name'], params['files'], params.get('bundle'))
        if method == 'get_package_baseline':
            return self.store.get_package_baseline(params['project_id'], self.principal)
        if method in {'get_component_catalog', 'get_shared_component', 'get_shared_contract',
                      'get_shared_template', 'get_component_access', 'set_component_access', 'validate_draft_contracts'}:
            from .web_sharing import ComponentService
            service = ComponentService(self.store, self.library_root)
            if method == 'validate_draft_contracts':
                return service.validate_drafts(self.principal, params['contracts'])
            if method == 'get_component_catalog':
                return service.catalog(self.principal)
            if method == 'get_component_access':
                return service.access(self.principal, **params)
            if method == 'set_component_access':
                if type(params['targets']) is not list or len(params['targets']) > 100:
                    raise KitError('授权账号必须是最多 100 项的列表')
                if any(name != self.principal.username for name in params['targets']):
                    raise KitError('本地演示只能向自己授权；跨用户请使用正式 broker')
                return service.set_access(self.principal, params['kind'], params['ref'], params['public'],
                                          [self.principal for _ in params['targets']], params['expected_revision'])
            operation = {'get_shared_component': service.component, 'get_shared_contract': service.contract,
                         'get_shared_template': service.template}[method]
            return operation(self.principal, params['ref'])
        if method == 'get_assignment_baseline':
            return self.store.assignment_baseline(self.principal,params['request_id'],params['fingerprint'])
        if method == 'get_storage_capacity':
            if self.principal.role != 'teacher':
                raise KitError('只有教师可以查看平台存储统计')
            from .web_content import capacity
            return capacity(self.store)
        if method in {'validate_assignment_targets', 'create_assignment_distributions'}:
            if self.principal.role != 'teacher':
                raise KitError('只有教师可以布置任务')
            names = params.get('target_usernames')
            if type(names) is not list or not names or len(names) > 100 or len(set(names)) != len(names):
                raise KitError('请选择有效且不重复的目标账号')
            if names != [self.principal.username]:
                raise KitError('本地演示只能向自己分发；跨用户请使用正式 broker')
            if method == 'validate_assignment_targets':
                if params.get('data_references'):
                    raise KitError('本地演示不支持外部数据任务')
                return {'targets':[{'uid':self.principal.uid,'username':self.principal.username}], 'data_references':[]}
            return {'distributions':self.store.create_distributions(self.principal, params['submission_id'], [self.principal])}
        if method == 'submit_assignment_baseline':
            return self.store.submit(params['project_id'],self.principal,params['kind'],params['files'],params['data_references'],
                                     assignment_request_id=params['request_id'],assignment_fingerprint=params['fingerprint'])
        if method == 'create_project':
            return self.store.create_project(self.principal, str(params.get('name', '')))
        if method == 'get_project':
            return self.store.get_project(str(params['project_id']), self.principal)
        if method == 'submit':
            return self.store.submit(str(params['project_id']), self.principal, str(params['kind']),
                                     params['files'], params['data_references'])  # type: ignore[arg-type]
        if method == 'get_submission':
            value = self.store.get_submission(str(params['submission_id']), self.principal)
            if (value['project']['owner_uid'],value['project']['owner_username']) != (self.principal.uid,self.principal.username):
                raise KitError('个人固定提交只显示自己的项目')
            return value
        if method == 'list_submissions':
            return self.store.list_submissions(self.principal, params.get('project_id'),
                                               include_minor=bool(params.get('include_minor', True)), personal=True)
        if method == 'list_submission_projects':
            return self.store.list_submission_projects(self.principal, personal=True, **params)
        if method == 'list_project_versions':
            return self.store.list_project_versions(self.principal, params['project_id'],
                                                    personal=True, **{key: value for key, value in params.items() if key != 'project_id'})
        if method == 'create_distribution':
            if params.get('target_username') != self.principal.username:
                raise KitError('本地演示只能向自己分发；跨用户请使用正式 broker')
            return self.store.create_distribution(self.principal, str(params['submission_id']), self.principal)
        if method == 'list_distributions':
            return self.store.list_distributions(self.principal, personal=True)
        if method == 'claim_distribution':
            return self.store.claim_distribution(str(params['distribution_id']), self.principal)
        if method == 'confirm_distribution':
            return self.store.confirm_distribution(str(params['distribution_id']), self.principal)
        if method == 'get_distribution_baseline':
            return self.store.get_distribution_baseline(str(params['distribution_id']), self.principal)
        if method in {'project_purge_list', 'project_purge_preview', 'project_purge_execute', 'project_purge_audit', 'project_purge_update_audit'}:
            from .web_project_purge import ProjectPurge
            service = ProjectPurge(self.store, self.library_root)
            if method == 'project_purge_list':
                return service.list_projects(self.principal)
            if method == 'project_purge_audit':
                return service.list_audit(self.principal, **{k: v for k, v in params.items()})
            if method == 'project_purge_update_audit':
                return service.update_audit(self.principal, **{k: v for k, v in params.items()})
            if method == 'project_purge_preview':
                return service.preview(self.principal, **{k: v for k, v in params.items()})
            return service.execute(self.principal, **{k: v for k, v in params.items()})
        raise KitError('不支持的提交服务操作')


class BaseHandler(tornado.web.RequestHandler):
    def initialize(self, workspace: Workspace, token_hash: bytes, executor: ThreadPoolExecutor, demo: bool) -> None:
        self.workspace, self.token_hash, self.executor, self.demo = workspace, token_hash, executor, demo

    def set_default_headers(self) -> None:
        self.set_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'")
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header('Cache-Control', 'no-store')

    def _host_ok(self) -> bool:
        host = self.request.headers.get('Host', '')
        return bool(re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?', host))

    def _origin_ok(self) -> bool:
        origin = self.request.headers.get('Origin')
        if origin is None:
            return True
        return origin == f'{self.request.protocol}://{self.request.headers.get("Host", "")}'

    def prepare(self) -> None:
        if not self._host_ok() or not self._origin_ok():
            raise tornado.web.HTTPError(403, '仅允许本机 Host 与同源请求')
        limit = MAX_PACKAGE_BYTES if self.request.method == 'POST' and self.request.path == '/api/imports' else MAX_HTTP_BODY
        if len(self.request.body) > limit:
            raise tornado.web.HTTPError(413, '请求内容超过大小上限')

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        error = kwargs.get('exc_info', (None, None, None))[1]
        message = str(error) if error else self._reason
        self.set_header('Content-Type', 'application/json; charset=utf-8')
        self.finish(json.dumps({'error': message}, ensure_ascii=False))

    async def blocking(self, action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, lambda: action(*args, **kwargs))


class MainHandler(BaseHandler):
    def get(self) -> None:
        self.set_header('Content-Type', 'text/html; charset=utf-8')
        self.write((_STATIC / 'index.html').read_bytes())


class StaticHandler(BaseHandler):
    def get(self, filename: str) -> None:
        content_types = {
            'app.js': 'application/javascript', 'composer.js': 'application/javascript',
            'data-manager.js': 'application/javascript', 'assignments.js': 'application/javascript',
            'lifecycle.js': 'application/javascript', 'cleanup.js': 'application/javascript',
            'project-purge.js': 'application/javascript',
            'feedback.js': 'application/javascript',
            'review-batch.js': 'application/javascript', 'experiments.js': 'application/javascript',
            'workflow.js': 'application/javascript',
            'markdown.js': 'application/javascript',
            'readme-export.js': 'application/javascript',
            'app.css': 'text/css', 'help.html': 'text/html',
        }
        if filename not in content_types:
            raise tornado.web.HTTPError(404)
        self.set_header('Content-Type', content_types[filename] + '; charset=utf-8')
        self.write((_STATIC / filename).read_bytes())


class ApiHandler(BaseHandler):
    def prepare(self) -> None:
        super().prepare()
        header = self.request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        # Compare a hash only.  The token itself is deliberately absent from logs, state, and URLs after JS strips it.
        provided = __import__('hashlib').sha256(token.encode('utf-8')).digest()
        if not hmac.compare_digest(provided, self.token_hash):
            raise tornado.web.HTTPError(403, '缺少或无效的本机会话令牌')

    def _json(self) -> dict[str, object]:
        if not self.request.headers.get('Content-Type', '').split(';', 1)[0].lower() == 'application/json':
            raise tornado.web.HTTPError(415, '请求必须为 application/json')
        try:
            body = json.loads(self.request.body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise tornado.web.HTTPError(400, 'JSON 请求无效') from exc
        if type(body) is not dict:
            raise tornado.web.HTTPError(400, 'JSON 请求必须为对象')
        return body

    def _result(self, value: object, status: int = 200) -> None:
        self.set_status(status)
        self.set_header('Content-Type', 'application/json; charset=utf-8')
        self.finish(json.dumps(value, ensure_ascii=False))

    def _submission_browser_query(self) -> dict[str, object]:
        allowed = {'q', 'owner', 'kind', 'from_date', 'to_date', 'limit', 'cursor'}
        supplied = set(self.request.query_arguments)
        if supplied - allowed:
            raise KitError('提交浏览筛选参数无效')
        result: dict[str, object] = {}
        for name in allowed:
            values = self.get_query_arguments(name)
            if len(values) > 1:
                raise KitError(f'{name} 不能重复')
            if values:
                result[name] = values[0]
        if 'limit' in result:
            raw = result['limit']
            if not isinstance(raw, str) or not re.fullmatch(r'[1-9][0-9]*', raw):
                raise KitError('limit 必须是正整数')
            result['limit'] = int(raw)
        return result

    def _derived_catalog_query(self) -> dict[str, object]:
        allowed = {'q', 'visibility', 'limit', 'cursor'}
        supplied = set(self.request.query_arguments)
        if supplied - allowed:
            raise KitError('派生数据筛选参数无效')
        result: dict[str, object] = {}
        for name in allowed:
            values = self.get_query_arguments(name)
            if len(values) > 1:
                raise KitError(f'{name} 不能重复')
            if values:
                result[name] = values[0]
        if 'visibility' in result and result['visibility'] not in {'mine', 'available', 'public'}:
            raise KitError('visibility 必须是 mine、available 或 public')
        if 'limit' in result:
            raw = result['limit']
            if not isinstance(raw, str) or not re.fullmatch(r'[1-9][0-9]*', raw):
                raise KitError('limit 必须是正整数')
            result['limit'] = int(raw)
        return result

    async def get(self, route: str = '') -> None:
        try:
            from .web_execution_policy import retired_http
            retired_http(route)
            if route.startswith('review/') and self.workspace.principal.role != 'teacher':
                raise tornado.web.HTTPError(403, '只有教师可以审核固定提交')
            if re.fullmatch(r'projects/[^/]+/experiments', route):
                if self.request.query_arguments:
                    raise KitError('实验方案列表不接受查询参数')
                self._result(await self.blocking(self.workspace.experiment_catalog, route.split('/')[1]))
                return
            if re.fullmatch(r'(?:review/)?submissions/[^/]+/experiments', route):
                if self.request.query_arguments:
                    raise KitError('固定提交方案不接受查询参数')
                review = route.startswith('review/')
                submission_id = route.split('/')[2 if review else 1]
                self._result(await self.blocking(self.workspace.submission_experiments, submission_id, review=review))
                return
            if re.fullmatch(r'(?:projects/[^/]+|(?:review/)?submissions/[^/]+)/result-file', route):
                from . import web_result_files
                if (set(self.request.query_arguments) - {'path', 'download'} or
                        len(self.get_query_arguments('path')) != 1 or
                        len(self.get_query_arguments('download')) > 1 or
                        self.get_query_argument('download', 'false') not in {'true', 'false'}):
                    raise KitError('产物查看参数无效')
                path = self.get_query_argument('path')
                download = self.get_query_argument('download', 'false') == 'true'
                if route.startswith('projects/'):
                    raw, media, name = await self.blocking(web_result_files.project_file, self.workspace,
                                                           route.split('/')[1], path, download=download)
                else:
                    review = route.startswith('review/')
                    raw, media, name = await self.blocking(web_result_files.submission_file, self.workspace,
                        route.split('/')[2 if review else 1], path, review=review, download=download)
                self.set_header('Content-Type', media)
                self.set_header('Content-Security-Policy', "default-src 'none'; sandbox")
                self.set_header('X-Content-Type-Options', 'nosniff')
                if download:
                    from urllib.parse import quote
                    self.set_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name))
                self.finish(raw)
                return
            if route.startswith('data-management') and self.workspace.principal.role != 'teacher':
                raise tornado.web.HTTPError(403, '只有教师可以管理数据集')
            if route.startswith('review/') and self.workspace.principal.role != 'teacher':
                raise tornado.web.HTTPError(403, '只有教师可以查看提交与入库')
            if route == 'feedback' or re.fullmatch(r'feedback/[^/]+', route):
                fields = {'page', 'status'} if route == 'feedback' else {'before_id'}
                if set(self.request.query_arguments) - fields:
                    raise KitError('消息列表参数不合法')
                params = {}
                for name in fields:
                    values = self.get_query_arguments(name)
                    if len(values) > 1:
                        raise KitError('消息参数不能重复')
                    if values:
                        if name in {'page', 'before_id'}:
                            if not re.fullmatch(r'[1-9][0-9]{0,9}', values[0]):
                                raise KitError('消息分页参数必须是正整数')
                            params[name] = int(values[0])
                        else:
                            params[name] = values[0]
                if route == 'feedback':
                    result = await self.blocking(self.workspace.store.request, 'feedback_list', **params)
                else:
                    result = await self.blocking(self.workspace.store.request, 'feedback_get',
                                                 thread_id=route.split('/')[1], **params)
                self._result(result)
            elif route == 'tasks':
                personal=self.get_query_argument('personal','false')
                if set(self.request.query_arguments)-{'personal'} or personal not in {'true','false'}:raise KitError('任务列表参数无效')
                self._result(await self.blocking(self.workspace.store.request,'list_tasks',personal=personal=='true'))
            elif re.fullmatch(r'projects/[^/]+/resources', route):
                if self.request.query_arguments:
                    raise KitError('项目依赖查询不接受其他参数')
                self._result(await self.blocking(self.workspace.project_resources, route.split('/')[1]))
            elif re.fullmatch(r'projects/[^/]+/jobs', route):
                if self.request.query_arguments:
                    raise KitError('项目执行记录参数无效')
                self._result(await self.blocking(self.workspace.store.request, 'list_project_jobs', project_id=route.split('/')[1]))
            elif re.fullmatch(r'tasks/[^/]+/materials',route):
                personal=self.get_query_argument('personal','false')
                if set(self.request.query_arguments)-{'personal'} or personal not in {'true','false'}:raise KitError('材料参数无效')
                self._result(await self.blocking(self.workspace.store.request,'task_materials',task_id=route.split('/')[1],personal=personal=='true'))
            elif route == 'review/submission-projects':
                self._result(await self.blocking(self.workspace.store.request, 'review_submission_projects', **self._submission_browser_query()))
            elif re.fullmatch(r'review/submission-projects/[^/]+/versions',route):
                self._result(await self.blocking(self.workspace.store.request, 'review_project_versions',project_id=route.split('/')[2], **self._submission_browser_query()))
            elif re.fullmatch(r'review/submissions/[^/]+/release',route):
                self._result(await self.blocking(self.workspace.store.request,'review_release',submission_id=route.split('/')[2]))
            elif re.fullmatch(r'review/submissions/[^/]+(?:/file)?',route):
                self._result(await self.blocking(self.workspace.get_submission,route.split('/')[2],
                    self.get_query_argument('path') if route.endswith('/file') else None,review=True))
            elif route == 'review/jobs':
                self._result(await self.blocking(self.workspace.store.request,'review_jobs',submission_id=self.get_query_argument('submission_id',None)))
            elif re.fullmatch(r'review/jobs/[^/]+',route):
                self._result(await self.blocking(self.workspace.store.request,'review_job',run_id=route.split('/')[2]))
            elif re.fullmatch(r'submission-preparations/[^/]+',route):
                self._result(await self.blocking(self.workspace.store.request,'get_submission_preparation',preparation_id=route.split('/')[1]))
            elif route == 'me':
                principal = self.workspace.principal
                self._result({'uid': principal.uid, 'username': principal.username, 'role': principal.role, 'demo': self.demo})
            elif route == 'personal-resource-visibility':
                if self.request.query_arguments:
                    raise KitError('个人资源显示设置不接受查询参数')
                self._result(await self.blocking(self.workspace.personal_resource_visibility))
            elif route == 'storage':
                self._result(await self.blocking(self.workspace.store.request, 'get_storage_capacity'))
            elif route == 'execution':
                self._result(await self.blocking(self.workspace.store.request, 'execution_status'))
            elif route == 'datasets':
                self._result(await self.blocking(self.workspace.store.request, 'get_dataset_catalog'))
            elif re.fullmatch(r'datasets/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/readme', route):
                if self.request.query_arguments:
                    raise KitError('README 查看不接受额外参数')
                self._result(await self.blocking(self.workspace.store.request, 'get_dataset_readme', dataset_id=route.split('/')[1]))
            elif route == 'data-management':
                if self.request.query_arguments:
                    raise KitError('数据管理状态不接受查询参数')
                self._result(await self.blocking(self.workspace.store.request, 'data_management_status'))
            elif route == 'data-management/candidates':
                if set(self.request.query_arguments) - {'status', 'query', 'offset', 'limit', 'include_inactive', 'tags', 'tag_mode'}:
                    raise KitError('数据审核列表参数不合法')
                filters = {}
                for name in ('status', 'query', 'offset', 'limit', 'include_inactive', 'tags', 'tag_mode'):
                    values = self.get_query_arguments(name)
                    if len(values) > 1:
                        raise KitError('数据审核列表参数不能重复')
                    if values:
                        if name in {'offset', 'limit'} and not re.fullmatch(r'[0-9]+', values[0]):
                            raise KitError('数据审核分页参数不合法')
                        if name == 'include_inactive' and values[0] not in {'true', 'false'}:
                            raise KitError('include_inactive 必须是 true 或 false')
                        if name == 'tags':
                            try:
                                filters[name] = json.loads(values[0])
                            except (ValueError, RecursionError) as exc:
                                raise KitError('标签筛选必须是 JSON 数组') from exc
                            if type(filters[name]) is not list:
                                raise KitError('标签筛选必须是 JSON 数组')
                        else:
                            filters[name] = (values[0] == 'true') if name == 'include_inactive' else int(values[0]) if name in {'offset', 'limit'} else values[0]
                self._result(await self.blocking(self.workspace.store.request, 'data_management_list', **filters))
            elif re.fullmatch(r'data-management/candidates/[a-f0-9]{24}', route):
                if self.request.query_arguments:
                    raise KitError('数据审核详情不接受查询参数')
                self._result(await self.blocking(self.workspace.store.request, 'data_management_get',
                                                 candidate_id=route.split('/')[2]))
            elif route == 'derived-datasets':
                self._result(await self.blocking(self.workspace.derived_catalog,
                                                 **self._derived_catalog_query()))
            elif route == 'derived-datasets/access':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以查看派生数据集权限')
                if self.request.query_arguments:
                    raise KitError('派生数据集权限列表不接受查询参数')
                self._result(await self.blocking(self.workspace.store.request, 'get_dataset_access_map'))
            elif re.fullmatch(r'derived-datasets/[A-Za-z0-9._-]+/readme', route):
                if self.request.query_arguments:
                    raise KitError('派生数据集 README 查看不接受额外参数')
                self._result(await self.blocking(self.workspace.store.request, 'get_derived_readme', dataset_id=route.split('/')[1]))
            elif route == 'local-algorithms':
                if self.request.query_arguments:
                    raise KitError('本地算法列表不接受查询参数')
                self._result(await self.blocking(self.workspace.list_local_algorithms))
            elif route == 'asset-cleanup/check':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以检查资产清理引用')
                if set(self.request.query_arguments) != {'kind', 'ref'}:
                    raise KitError('资产清理检查需要 kind 和 ref 参数')
                kind_vals = self.get_query_arguments('kind')
                ref_vals = self.get_query_arguments('ref')
                if len(kind_vals) != 1 or len(ref_vals) != 1:
                    raise KitError('kind 和 ref 参数不能重复')
                self._result(await self.blocking(self.workspace.store.request, 'check_asset_cleanup',
                                                 kind=kind_vals[0], ref=ref_vals[0]))
            elif re.fullmatch(r'datasets/[^/]+/files', route):
                if set(self.request.query_arguments) - {'q', 'offset', 'limit'}:
                    raise KitError('数据文件浏览参数无效')
                filters = {}
                for name in ('q', 'offset', 'limit'):
                    values = self.get_query_arguments(name)
                    if len(values) > 1:
                        raise KitError('数据文件浏览参数不能重复')
                    if values:
                        if name != 'q' and not re.fullmatch(r'[0-9]+', values[0]):
                            raise KitError('数据文件浏览分页参数无效')
                        filters[name] = int(values[0]) if name != 'q' else values[0]
                self._result(await self.blocking(self.workspace.dataset_files, route.split('/')[1], **filters))
            elif route == 'asset-candidates':
                self._result(await self.blocking(self.workspace.store.request, 'list_asset_candidates'))
            elif re.fullmatch(r'asset-candidates/[^/]+/validations', route):
                self._result(await self.blocking(self.workspace.store.request,
                                                 'list_asset_candidate_validations',
                                                 candidate_id=route.split('/')[1]))
            elif re.fullmatch(r'asset-candidates/[^/]+(?:/file)?', route):
                value = await self.blocking(self.workspace.store.request, 'get_asset_candidate', candidate_id=route.split('/')[1])
                if route.endswith('/file'):
                    path = self.get_query_argument('path')
                    content = value['files'].get(path)
                    if content is None:
                        raise KitError('候选文件不存在')
                    if len(content) > 1024**2:
                        raise KitError('候选文本预览超过 1 MiB 上限')
                    try:
                        self._result({'path': path, 'text': content.decode('utf-8')})
                    except UnicodeDecodeError as exc:
                        raise KitError('候选文件不是 UTF-8 文本，只显示清单信息') from exc
                else:
                    self._result(_asset_view(value))
            elif route == 'jobs':
                self._result(await self.blocking(self.workspace.store.request, 'list_jobs', submission_id=self.get_query_argument('submission_id', None)))
            elif re.fullmatch(r'jobs/[^/]+/artifacts.zip', route):
                import io
                import zipfile
                value = await self.blocking(self.workspace.store.request, 'job_artifacts', run_id=route.split('/')[1])
                content = io.BytesIO()
                with zipfile.ZipFile(content, 'w', compression=zipfile.ZIP_STORED) as archive:
                    for path, data in value['files'].items():
                        archive.writestr(path, data)
                self.set_header('Content-Type', 'application/zip')
                self.set_header('Content-Disposition', 'attachment; filename="researchkit-results.zip"')
                self.finish(content.getvalue())
            elif re.fullmatch(r'jobs/[^/]+', route):
                self._result(await self.blocking(self.workspace.store.request, 'get_job', run_id=route.split('/')[1]))
            elif route == 'distributions':
                self._result(await self.blocking(self.workspace.list_distributions))
            elif route == 'baseline-diff':
                self._result(await self.blocking(self.workspace.diff_baseline, self.get_query_argument('submission_id'),
                                                  self.get_query_argument('path')))
            elif route == 'projects':
                if set(self.request.query_arguments) - {'include_archived'}:
                    raise KitError('项目列表筛选参数无效')
                values = self.get_query_arguments('include_archived')
                if len(values) > 1 or (values and values[0] not in {'true', 'false'}):
                    raise KitError('include_archived 必须是单个 true 或 false')
                self._result(await self.blocking(self.workspace.list_projects,
                                                 include_archived=bool(values and values[0] == 'true')))
            elif route == 'catalog':
                self._result(await self.blocking(self.workspace.composition_catalog))
            elif route == 'asset-bin':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403,'只有教师可以查看收起列表和回收站')
                if set(self.request.query_arguments)-{'state','kind','q','offset','limit'}:
                    raise KitError('回收站列表参数无效')
                filters={}
                for name in ('state','kind','q','offset','limit'):
                    values=self.get_query_arguments(name)
                    if len(values)>1:raise KitError('回收站列表参数不能重复')
                    if values:
                        if name in {'offset','limit'} and not re.fullmatch(r'[0-9]+',values[0]):raise KitError('回收站分页参数无效')
                        filters[name]=int(values[0]) if name in {'offset','limit'} else values[0]
                self._result(await self.blocking(self.workspace.store.request,'list_asset_bin',**filters))
            elif re.fullmatch(r'asset-cleanup/[a-f0-9-]{36}',route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403,'只有教师可以查看永久清理状态')
                if self.request.query_arguments:raise KitError('清理状态不接受查询参数')
                self._result(await self.blocking(self.workspace.store.request,'get_asset_cleanup',request_id=route.split('/')[1]))
            elif route == 'asset-lifecycle':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以查看资产引用')
                if set(self.request.query_arguments) - {'kind', 'ref', 'offset', 'limit'}:
                    raise KitError('引用查询参数不合法')
                filters = {}
                for name in ('kind', 'ref', 'offset', 'limit'):
                    values = self.get_query_arguments(name)
                    if len(values) > 1:
                        raise KitError('引用查询参数不能重复')
                    if values:
                        if name in {'offset', 'limit'} and not re.fullmatch(r'[0-9]+', values[0]):
                            raise KitError('引用查询分页参数不合法')
                        filters[name] = int(values[0]) if name in {'offset', 'limit'} else values[0]
                if not {'kind', 'ref'} <= set(filters):
                    raise KitError('请指定资产类型和精确版本')
                self._result(await self.blocking(self.workspace.store.request, 'get_asset_lifecycle', **filters))
            elif route == 'component-access':
                if set(self.request.query_arguments) - {'include_inactive'}:
                    raise KitError('共享列表参数不合法')
                values = self.get_query_arguments('include_inactive')
                if len(values) > 1 or (values and values[0] not in {'true', 'false'}):
                    raise KitError('include_inactive 必须是单个 true 或 false')
                self._result(await self.blocking(self.workspace.component_access, include_inactive=bool(values and values[0] == 'true')))
            elif re.fullmatch(r'projects/[^/]+/datasets', route):
                self._result(await self.blocking(self.workspace.project_datasets, route.split('/')[1]))
            elif re.fullmatch(r'projects/[^/]+/composition', route):
                self._result(await self.blocking(self.workspace.read_composition, route.split('/')[1]))
            elif route == 'project-purge/projects':
                self._result(await self.blocking(self.workspace.list_purgeable_projects))
            elif route == 'project-purge/audit':
                self._result(await self.blocking(self.workspace.list_project_purge_audit))
            elif re.fullmatch(r'projects/[^/]+/purge-preview', route):
                self._result(await self.blocking(self.workspace.preview_project_purge, route.split('/')[1]))
            elif route.startswith('projects/') and route.endswith('/file'):
                project_id = route.split('/')[1]
                self._result(await self.blocking(self.workspace.read_text, project_id, self.get_query_argument('path')))
            elif route.startswith('projects/'):
                self._result(await self.blocking(self.workspace.describe_project, route.split('/')[1]))
            elif route == 'submissions':
                project_id = self.get_query_argument('project_id', None)
                include_minor = self.get_query_argument('include_minor', 'true').lower() != 'false'
                self._result(await self.blocking(self.workspace.list_submissions, project_id, include_minor=include_minor))
            elif route == 'submission-projects':
                self._result(await self.blocking(self.workspace.list_submission_projects,
                                                 **self._submission_browser_query()))
            elif re.fullmatch(r'submission-projects/[^/]+/versions', route):
                self._result(await self.blocking(self.workspace.list_project_versions, route.split('/')[1],
                                                 **self._submission_browser_query()))
            elif route.startswith('submissions/') and route.endswith('/file'):
                self._result(await self.blocking(self.workspace.get_submission, route.split('/')[1], self.get_query_argument('path')))
            elif route.startswith('submissions/'):
                self._result(await self.blocking(self.workspace.get_submission, route.split('/')[1]))
            elif route == 'diff':
                self._result(await self.blocking(self.workspace.diff_submissions, self.get_query_argument('before'),
                                                  self.get_query_argument('after'), self.get_query_argument('path')))
            else:
                raise tornado.web.HTTPError(404)
        except KitError as exc:
            raise tornado.web.HTTPError(409 if '冲突' in str(exc) else 400, str(exc)) from exc

    async def post(self, route: str = '') -> None:
        from .web_execution_policy import retired_http
        retired_http(route)
        if route == 'imports':
            if self.request.headers.get('Content-Type', '').split(';', 1)[0].lower() != 'application/zip':
                raise tornado.web.HTTPError(415, '导入请求必须为 application/zip')
            try:
                self._result(await self.blocking(self.workspace.import_package, self.get_query_argument('name'),
                                                self.request.body), 201)
            except KitError as exc:
                raise tornado.web.HTTPError(409 if '冲突' in str(exc) else 400, str(exc)) from exc
            return
        body = self._json()
        try:
            if re.fullmatch(r'projects/[^/]+/experiments/update-helper', route):
                if self.request.query_arguments or body:
                    raise KitError('更新实验入口不接受额外参数')
                self._result(await self.blocking(self.workspace.update_experiment_helper, route.split('/')[1]))
                return
            if re.fullmatch(r'projects/[^/]+/experiments/enable', route):
                if self.request.query_arguments or body:
                    raise KitError('准备实验入口不接受额外参数')
                self._result(await self.blocking(self.workspace.enable_experiments, route.split('/')[1]))
                return
            if re.fullmatch(r'projects/[^/]+/experiments(?:/delete)?', route):
                if self.request.query_arguments:
                    raise KitError('方案保存不接受查询参数')
                operation = self.workspace.delete_experiment if route.endswith('/delete') else self.workspace.save_experiment
                self._result(await self.blocking(operation, route.split('/')[1], body))
                return
            if re.fullmatch(r'projects/[^/]+/experiment-results/delete', route):
                if self.request.query_arguments:
                    raise KitError('删除产物不接受查询参数')
                self._result(await self.blocking(self.workspace.delete_experiment_result, route.split('/')[1], body))
                return
            if route.startswith('data-management') and self.workspace.principal.role != 'teacher':
                raise tornado.web.HTTPError(403, '只有教师可以管理数据集')
            if route == 'feedback' or re.fullmatch(r'feedback/[^/]+/(reply|read|status)', route):
                if self.request.query_arguments:
                    raise KitError('发送消息不接受查询参数')
                if route == 'feedback':
                    result = await self.blocking(self.workspace.store.request, 'feedback_create', **body)
                    self._result(result, 201)
                else:
                    if 'thread_id' in body:
                        raise KitError('消息请求不能覆盖会话编号')
                    operation = route.split('/')[2]
                    if operation == 'status' and (self.workspace.principal.role != 'teacher'
                                                 or self.workspace.principal.username != 'ly'):
                        raise tornado.web.HTTPError(403, '只有管理员可以更改处理状态')
                    result = await self.blocking(self.workspace.store.request, 'feedback_' + operation,
                                                 thread_id=route.split('/')[1], **body)
                    self._result(result)
            elif route == 'tasks/trial':
                self._result(await self.blocking(self.workspace.store.request,'create_trial_task',**body),201)
            elif route == 'datasets/versions':
                self._result(await self.blocking(self.workspace.store.request, 'relate_dataset_version', **body))
            elif re.fullmatch(r'projects/[^/]+/algorithm-materials', route):
                from .web_workspace_materials import add_algorithms
                self._result(await self.blocking(add_algorithms, self.workspace, route.split('/')[1], body))
            elif re.fullmatch(r'tasks/[^/]+/claim', route):
                from .web_task_claim import claim
                self._result(await self.blocking(claim, self.workspace, route.split('/')[1], body))
            elif re.fullmatch(r'tasks/[^/]+/supplements',route):
                self._result(await self.blocking(self.workspace.store.request,'supplement_task',task_id=route.split('/')[1],**body),201)
            elif re.fullmatch(r'tasks/[^/]+/grants/[^/]+/revoke',route):
                if body:raise KitError('撤销不接受其他字段')
                self._result(await self.blocking(self.workspace.store.request,'revoke_task_grant',task_id=route.split('/')[1],grant_id=route.split('/')[3]))
            elif re.fullmatch(r'projects/[^/]+/task-materials',route):
                self._result(await self.blocking(self.workspace.apply_task_materials,route.split('/')[1],body))
            elif re.fullmatch(r'projects/[^/]+/trial',route):
                self._result(await self.blocking(self.workspace.project_trial,route.split('/')[1],body),202)
            elif re.fullmatch(r'projects/[^/]+/submission-(preview|prepare)',route):
                operation=self.workspace.submission_preview if route.endswith('preview') else self.workspace.submission_prepare
                self._result(await self.blocking(operation,route.split('/')[1],body),200)
            elif re.fullmatch(r'review/submissions/[^/]+/test-copy', route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以复制到工作区测试')
                if body:
                    raise KitError('复制测试请求不接受其他字段')
                self._result(await self.blocking(self.workspace.copy_submission_for_test, route.split('/')[2]), 201)
            elif re.fullmatch(r'review/submissions/[^/]+/publish-batch',route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403,'只有教师可以审核入库')
                if set(body) != {'expected_sha256', 'items'}:
                    raise KitError('批量审核入库字段无效')
                self._result(await self.blocking(self.workspace.store.request,'review_publish_batch',submission_id=route.split('/')[2],**body))
            elif re.fullmatch(r'review/submissions/[^/]+/publish',route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403,'只有教师可以审核入库')
                allowed = {
                    'kind', 'aliases', 'expected_sha256', 'publish_mode', 'parent_ref',
                    'name', 'version', 'tags', 'description', 'readme_markdown',
                    'dataset_id', 'dataset_scope', 'embedded_algorithms', 'embedded_datasets',
                }
                required = {'kind', 'expected_sha256'}
                if set(body) - allowed or required - set(body):
                    raise KitError('审核入库字段无效')
                if body.get('publish_mode', 'new') == 'new':
                    tags_val = body.get('tags')
                    desc_val = body.get('description')
                    has_tags = (isinstance(tags_val, list) and any(str(t).strip() for t in tags_val)) or (isinstance(tags_val, str) and bool(tags_val.strip()))
                    has_desc = isinstance(desc_val, str) and bool(desc_val.strip())
                    if not has_tags or not has_desc:
                        raise KitError('作为新资源入库必须填写标签（至少 1 个）和说明')
                self._result(await self.blocking(self.workspace.store.request,'review_publish',submission_id=route.split('/')[2],**body))
            elif route == 'assets/metadata':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以编辑资产标签与说明')
                allowed = {'kind', 'ref', 'description', 'tags', 'parent_ref', 'display_name'}
                required = {'kind', 'ref'}
                if set(body) - allowed or required - set(body):
                    raise KitError('资产元数据字段无效')
                self._result(await self.blocking(self.workspace.store.request, 'set_asset_metadata', **body))
            elif re.fullmatch(r'derived-datasets/[A-Za-z0-9._-]+/access', route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以修改派生数据集权限')
                if set(body) != {'public', 'targets'}:
                    raise KitError('派生数据集权限请求字段无效')
                self._result(await self.blocking(self.workspace.store.request, 'set_dataset_access',
                                                 dataset_id=route.split('/')[1], **body))
            elif route == 'asset-cleanup/direct':
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以执行直接清理')
                if self.request.query_arguments:
                    raise KitError('永久清理不接受查询参数')
                allowed = {'kind', 'ref', 'confirmation', 'force_student_projects'}
                required = {'kind', 'ref', 'confirmation'}
                if set(body) - allowed or required - set(body):
                    raise KitError('直接清理请求字段无效')
                self._result(await self.blocking(self.workspace.store.request, 'direct_asset_cleanup', **body))
            elif re.fullmatch(r'tasks/[^/]+/delete', route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403, '只有教师可以删除任务')
                if body:
                    raise KitError('删除任务不接受其他字段')
                self._result(await self.blocking(self.workspace.store.request, 'delete_task', task_id=route.split('/')[1]))
            elif re.fullmatch(r'tasks/[^/]+/dismiss', route):
                if body:
                    raise KitError('移除任务记录不接受其他字段')
                self._result(await self.blocking(self.workspace.store.request, 'dismiss_task', task_id=route.split('/')[1]))
            elif route == 'local-algorithms':
                self._result(await self.blocking(self.workspace.create_local_algorithm, body), 201)
            elif re.fullmatch(r'local-algorithms/[^/]+/delete', route):
                if body:
                    raise KitError('删除本地算法不接受其他字段')
                self._result(await self.blocking(self.workspace.delete_local_algorithm, route.split('/')[1]))
            elif re.fullmatch(r'projects/[^/]+/stale-datasets/remove', route):
                allowed = {'dataset_id', 'expected_sha256'}
                if 'dataset_id' not in body or set(body) - allowed:
                    raise KitError('移除失效数据集字段无效')
                self._result(await self.blocking(self.workspace.remove_stale_dataset_reference,
                                                 route.split('/')[1], body['dataset_id'], body.get('expected_sha256')))
            elif route in {'asset-cleanup/preview','asset-cleanup/execute'} or re.fullmatch(r'asset-cleanup/[a-f0-9-]{36}/retry',route):
                if self.workspace.principal.role != 'teacher':
                    raise tornado.web.HTTPError(403,'只有教师可以永久清理回收站')
                if self.request.query_arguments:raise KitError('永久清理不接受查询参数')
                expected={'items'} if route.endswith('/preview') else set() if route.endswith('/retry') else {'items','plan_token','confirmation'}
                if set(body)!=expected:raise KitError('永久清理请求字段无效')
                method='preview_asset_cleanup' if route.endswith('/preview') else 'retry_asset_cleanup' if route.endswith('/retry') else 'execute_asset_cleanup'
                params={'request_id':route.split('/')[1]} if route.endswith('/retry') else body
                self._result(await self.blocking(self.workspace.store.request,method,**params))
            elif route == 'data-management/scan':
                if body:
                    raise KitError('数据扫描不接受目录或身份参数')
                self._result(await self.blocking(self.workspace.store.request, 'data_management_scan'), 202)
            elif re.fullmatch(r'data-management/candidates/[a-f0-9]{24}/review', route):
                if set(body) - {'revision', 'decision', 'metadata', 'readers'} or {'revision', 'decision'} - set(body):
                    raise KitError('数据审核字段不合法')
                self._result(await self.blocking(self.workspace.store.request, 'data_management_review',
                                                 candidate_id=route.split('/')[2], **body))
            elif re.fullmatch(r'data-management/candidates/[a-f0-9]{24}/access', route):
                if set(body) != {'revision', 'access_token', 'readers'}:
                    raise KitError('数据授权字段不合法')
                self._result(await self.blocking(self.workspace.store.request, 'data_management_access',
                                                 candidate_id=route.split('/')[2], **body), 202)
            elif route == 'asset-candidates':
                required = {'submission_id', 'kind', 'name', 'version'}
                if set(body) - (required | {'alias'}) or required - set(body):
                    raise KitError('候选提取字段不合法')
                value = await self.blocking(self.workspace.store.request, 'create_asset_candidate', **body)
                self._result(_asset_view(value), 201)
            elif re.fullmatch(r'asset-candidates/[^/]+/(review|publish)', route):
                required = {'expected_sha256', 'notes'} if route.endswith('/review') else {'expected_sha256'}
                allowed = required | ({'validation_run_id'} if route.endswith('/review') else set())
                if set(body) - allowed or required - set(body):
                    raise KitError('候选审阅或发布字段不合法')
                value = await self.blocking(self.workspace.store.request,
                                           'review_asset_candidate' if route.endswith('/review') else 'publish_asset_candidate',
                                           candidate_id=route.split('/')[1], **body)
                self._result(_asset_view(value))
            elif re.fullmatch(r'asset-candidates/[^/]+/validations', route):
                required = {'expected_sha256', 'operation'}
                allowed = required | {'cases'}
                if set(body) - allowed or required - set(body):
                    raise KitError('候选验证字段不合法')
                self._result(await self.blocking(self.workspace.store.request,
                                                 'create_asset_candidate_validation',
                                                 candidate_id=route.split('/')[1], **body), 201)
            elif re.fullmatch(r'submissions/[^/]+/jobs', route):
                if set(body) != {'operation'}:
                    raise KitError('运行请求只接受 operation')
                self._result(await self.blocking(self.workspace.store.request, 'request_job',
                                                submission_id=route.split('/')[1], operation=body['operation']), 201)
            elif re.fullmatch(r'jobs/[^/]+/cancel', route):
                if body:
                    raise KitError('取消请求不接受其他字段')
                self._result(await self.blocking(self.workspace.store.request, 'cancel_job', run_id=route.split('/')[1]))
            elif re.fullmatch(r'submissions/[^/]+/package', route):
                if body:
                    raise KitError('导出请求不接受其他字段')
                submission_id = route.split('/')[1]
                package = await self.blocking(self.workspace.export_package, submission_id)
                self.set_header('Content-Type', 'application/zip')
                self.set_header('Content-Disposition', f'attachment; filename="researchkit-{submission_id}.zip"')
                self.finish(package)
            elif route == 'assignments/preview':
                self._result(await self.blocking(self.workspace.assignment_preview, body))
            elif route == 'assignments':
                self._result(await self.blocking(self.workspace.create_assignment, body), 201)
            elif route == 'distributions':
                self._result(await self.blocking(self.workspace.create_distribution, body.get('submission_id'), body.get('target_username')), 201)
            elif route.startswith('distributions/') and route.endswith('/claim'):
                self._result(await self.blocking(self.workspace.claim_distribution, route.split('/')[1]))
            elif re.fullmatch(r'projects/[^/]+/composition/preview', route):
                self._result(await self.blocking(self.workspace.preview_composition, route.split('/')[1], body))
            elif route == 'projects':
                self._result(await self.blocking(self.workspace.create_project, body.get('name'), body.get('template_ref')), 201)
            elif re.fullmatch(r'projects/[^/]+/purge-preview', route):
                self._result(await self.blocking(self.workspace.preview_project_purge, route.split('/')[1], body))
            elif re.fullmatch(r'projects/[^/]+/purge', route):
                self._result(await self.blocking(self.workspace.purge_project, route.split('/')[1], body))
            elif re.fullmatch(r'projects/[^/]+/delete-local', route):
                allowed = {'confirmation'}
                if set(body) - allowed:
                    raise KitError('删除本地副本请求包含无效字段')
                self._result(await self.blocking(self.workspace.delete_personal_project, route.split('/')[1], body.get('confirmation', '')))
            elif re.fullmatch(r'projects/[^/]+/(?:archive|restore)', route):
                if body:
                    raise KitError('项目归档或恢复请求不接受其他字段')
                self._result(await self.blocking(self.workspace.set_project_archived, route.split('/')[1],
                                                 route.endswith('/archive')))
            elif re.fullmatch(r'projects/[^/]+/derived', route):
                allowed = {'path', 'name', 'description', 'tags', 'readme_markdown', 'parent_dataset_id'}
                required = {'path', 'name'}
                if set(body) - allowed or required - set(body):
                    raise KitError('项目派生数据请求字段不合法')
                self._result(await self.blocking(self.workspace.upload_project_file_as_derived,
                                                 route.split('/')[1], body['path'], body['name'],
                                                 description=body.get('description', ''),
                                                 tags=body.get('tags'),
                                                 readme_markdown=body.get('readme_markdown', ''),
                                                 parent_dataset_id=body.get('parent_dataset_id')), 201)
            elif re.fullmatch(r'jobs/[^/]+/derived', route):
                if set(body) - {'paths', 'name'} or 'paths' not in body:
                    raise KitError('运行产物保存请求字段不合法')
                self._result(await self.blocking(self.workspace.save_job_artifacts_as_derived,
                                                 route.split('/')[1], body['paths'], body.get('name')), 201)
            elif re.fullmatch(r'derived-datasets/[^/]+/review', route):
                if set(body) != {'submission_id'}:
                    raise KitError('派生数据审核授权请求字段不合法')
                self._result(await self.blocking(self.workspace.grant_derived_review,
                                                 route.split('/')[1], body['submission_id']))
            elif re.fullmatch(r'derived-datasets/[^/]+/publish', route):
                if body:
                    raise KitError('派生数据发布请求不接受其他字段')
                self._result(await self.blocking(self.workspace.publish_derived, route.split('/')[1]))
            elif route.startswith('projects/') and route.endswith('/import'):
                self._result(await self.blocking(self.workspace.register_existing, body.get('name'), body.get('path')), 201)
            elif route.startswith('projects/') and route.endswith('/submit'):
                self._result(await self.blocking(self.workspace.submit, route.split('/')[1], body.get('kind')))
            else:
                raise tornado.web.HTTPError(404)
        except KitError as exc:
            raise tornado.web.HTTPError(409 if '冲突' in str(exc) else 400, str(exc)) from exc

    async def put(self, route: str = '') -> None:
        body = self._json()
        if route == 'personal-resource-visibility':
            try:
                if self.request.query_arguments:
                    raise KitError('个人资源显示设置不接受查询参数')
                self._result(await self.blocking(self.workspace.set_personal_resource_visibility, body))
            except KitError as exc:
                raise tornado.web.HTTPError(400, str(exc)) from exc
            return
        if route == 'asset-lifecycle':
            if self.workspace.principal.role != 'teacher':
                raise tornado.web.HTTPError(403, '只有教师可以清理资产')
            try:
                if set(body) != {'kind', 'ref', 'action', 'expected_revision'}:
                    raise KitError('资产清理请求字段不合法')
                self._result(await self.blocking(self.workspace.store.request, 'set_asset_lifecycle', **body))
            except KitError as exc:
                raise tornado.web.HTTPError(409 if any(word in str(exc) for word in ('冲突', '引用', '不能移入')) else 400, str(exc)) from exc
            return
        if route == 'component-access':
            try:
                self._result(await self.blocking(self.workspace.set_component_access, body))
            except KitError as exc:
                raise tornado.web.HTTPError(409 if '冲突' in str(exc) else 400, str(exc)) from exc
            return
        if re.fullmatch(r'projects/[^/]+/composition', route):
            try:
                self._result(await self.blocking(self.workspace.apply_composition, route.split('/')[1], body))
            except KitError as exc:
                raise tornado.web.HTTPError(409 if '冲突' in str(exc) else 400, str(exc)) from exc
            return
        if re.fullmatch(r'projects/[^/]+/datasets', route):
            try:
                if set(body) != {'dataset_ids', 'expected_sha256'}:
                    raise KitError('项目数据集请求字段不合法')
                self._result(await self.blocking(self.workspace.add_project_datasets, route.split('/')[1],
                                                 body['dataset_ids'], body['expected_sha256']))
            except KitError as exc:
                status = 409 if '冲突' in str(exc) else 400
                raise tornado.web.HTTPError(status, str(exc)) from exc
            return
        if not (route.startswith('projects/') and route.endswith('/file')):
            raise tornado.web.HTTPError(404)
        try:
            self._result(await self.blocking(self.workspace.save_text, route.split('/')[1], body.get('path'),
                                              body.get('text'), body.get('expected_sha256')))
        except KitError as exc:
            status = 409 if '编辑冲突' in str(exc) else 400
            raise tornado.web.HTTPError(status, str(exc)) from exc


def application(workspace: Workspace, token: str, *, demo: bool = False) -> tornado.web.Application:
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError('Web 会话令牌长度不足')
    token_hash = __import__('hashlib').sha256(token.encode('utf-8')).digest()
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='researchkit-web')
    common = {'workspace': workspace, 'token_hash': token_hash, 'executor': executor, 'demo': demo}
    return tornado.web.Application([
        (r'/', MainHandler, common), (r'/static/(.*)', StaticHandler, common), (r'/api/?(.*)', ApiHandler, common),
    ], default_handler_class=BaseHandler, default_handler_args=common, max_body_size=MAX_HTTP_BODY, xsrf_cookies=False,
       compress_response=False, debug=False)


def _ensure_workspace(policy) -> None:
    """Validate identity and create only the policy-selected 0700 workspace leaf."""
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise IdentityError('root 或身份不一致的进程不能启动 Web')
    principal = policy.principal(uid)
    record = pwd.getpwuid(uid)
    if record.pw_name != principal.username:
        raise IdentityError('策略用户名与当前 Linux 账号不一致')
    path = policy.workspace(principal)
    home = Path(record.pw_dir)
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise IdentityError('策略工作区不在当前账号 home 下') from exc
    try:
        _secure_directory(path, create=True)
    except KitError as exc:
        raise IdentityError(str(exc)) from exc


def _demo_workspace() -> Workspace:
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise IdentityError('demo 不能由 root 或身份不一致的进程启动')
    import pwd
    username = pwd.getpwuid(uid).pw_name
    principal = Principal(uid=uid, username=username, role='teacher' if username == 'ly' else 'student')
    root = Path(__file__).resolve().parents[2] / '.web-preview'
    if root.exists():
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise IdentityError('demo 目录不安全')
    else:
        root.mkdir(mode=0o700)
    store = SubmissionStore(root / 'submissions')
    return Workspace(principal, root / 'workspace', LocalStoreClient(store, principal))


def _production_workspace(policy_path: str, socket_path: str) -> Workspace:
    policy = load_policy(policy_path, production=True)
    _ensure_workspace(policy)
    principal = current_principal(policy)
    from .web_broker import BrokerClient
    client = BrokerClient(socket_path, expected_broker_uid=policy.broker_uid)
    return Workspace(principal, policy.workspace(principal), client, library_root=policy.code_root / 'library')


def bind_loopback_server(app: tornado.web.Application, port: int) -> tuple[tornado.httpserver.HTTPServer, list[socket.socket], int]:
    """Bind only IPv4 loopback and return the OS-selected effective port."""
    sockets = tornado.netutil.bind_sockets(port, address='127.0.0.1')
    server = tornado.httpserver.HTTPServer(app, xheaders=False, max_body_size=MAX_PACKAGE_BYTES)
    server.add_sockets(sockets)
    return server, sockets, int(sockets[0].getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ResearchKit per-user loopback Web workspace')
    parser.add_argument('--policy', default='/etc/researchkit-web/policy.json')
    parser.add_argument('--broker-socket', default='/run/researchkit-web/broker.sock')
    parser.add_argument('--port', type=int, default=0, help='本机监听端口；默认 0 由系统分配')
    parser.add_argument('--demo', action='store_true', help='仅当前账号的本地预览，不可用于正式多用户部署')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('端口必须在 0 到 65535 之间')
    workspace = _demo_workspace() if args.demo else _production_workspace(args.policy, args.broker_socket)
    token = secrets.token_urlsafe(32)
    app = application(workspace, token, demo=args.demo)
    _server, _sockets, actual_port = bind_loopback_server(app, args.port)
    print(f'http://127.0.0.1:{actual_port}/#token={token}', flush=True)
    if args.demo:
        print('DEMO：仅当前账号、本地 .web-preview 存储；不能作为正式多用户部署。', flush=True)
    tornado.ioloop.IOLoop.current().start()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
