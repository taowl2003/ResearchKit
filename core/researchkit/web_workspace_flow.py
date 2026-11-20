"""Personal submission preparation over the existing safe workspace writer."""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from .files import KitError
from .web_versions import apply_versions


def _trial_fingerprint(request_id, files, references):
    """Bind a retry token to the immutable bytes and external-reference list."""
    digest = hashlib.sha256()
    digest.update(request_id.encode('ascii'))
    for path in sorted(files):
        encoded = path.encode('utf-8')
        digest.update(len(encoded).to_bytes(4, 'big'))
        digest.update(encoded)
        digest.update(len(files[path]).to_bytes(8, 'big'))
        digest.update(files[path])
    try:
        digest.update(json.dumps(references, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':'), allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise KitError('项目试运行数据引用无效') from exc
    return digest.hexdigest()


def trial(workspace, project_id, payload):
    """Freeze the current project only for check/run, never for submission."""
    if type(payload) is not dict or set(payload) != {'request_id'}:
        raise KitError('项目试运行字段无效')
    request_id = payload.get('request_id')
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise KitError('项目试运行请求ID必须是UUID')
    with workspace._project_guard(project_id):
        _, files, references = workspace._snapshot(project_id)
        fingerprint = _trial_fingerprint(request_id, files, references)
        result = workspace.store.request('prepare_project_trial', project_id=project_id,
                                         files=files, data_references=references,
                                         request_id=request_id,
                                         request_fingerprint=fingerprint)
    workspace._sync_usage(project_id)
    return result


def preview(workspace, project_id, payload):
    if type(payload) is not dict or set(payload) - {'kind', 'result_paths'} or 'kind' not in payload:
        raise KitError('预览提交只接受版本类型')
    with workspace._project_guard(project_id):
        _, files, _ = workspace._snapshot(project_id, result_paths=payload.get('result_paths'))
        return workspace.store.request('preview_submission_versions', project_id=project_id,
                                       files=files, kind=payload['kind'])


def prepare(workspace, project_id, payload):
    fields={'kind','expected_revision','case_version','algorithm_versions','request_id'}
    if type(payload) is not dict or set(payload) - (fields | {'result_paths'}) or not fields <= set(payload):
        raise KitError('提交验证字段无效')
    try:
        if str(uuid.UUID(payload['request_id'])) != payload['request_id']:raise ValueError
    except (ValueError,TypeError,AttributeError):
        raise KitError('提交请求ID必须为UUID')
    try:
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    except (TypeError,ValueError):raise KitError('提交验证参数无效')
    known=workspace.store.request('find_submission_preparation',project_id=project_id,
                                  request_id=payload['request_id'],request_fingerprint=fingerprint)
    if known is not None:return known
    from .web_composer import Composer
    with workspace._project_guard(project_id):
        result_paths = payload.get('result_paths')
        _,before,refs=workspace._snapshot(project_id, result_paths=result_paths)
        plan=workspace.store.request('preview_submission_versions',project_id=project_id,
                                     files=before,kind=payload['kind'])
        if plan['snapshot_revision']!=payload['expected_revision']:
            raise KitError('项目冲突：预览后项目已变化，请重新预览')
        desired=apply_versions(before,plan,{'case_version':payload['case_version'],
                                          'algorithm_versions':payload['algorithm_versions']})
        # The broker freezes the exact desired bytes before local metadata is
        # written. Retrying after a disconnect finds this request first; it
        # never creates a second preparation or guesses at intervening IDE edits.
        result=workspace.store.request('prepare_submission',project_id=project_id,files=desired,
            data_references=refs,kind=payload['kind'],case_version=payload['case_version'],
            request_id=payload['request_id'],request_fingerprint=fingerprint)
        try:
            changes=[(path,before.get(path),desired[path]) for path in sorted(desired) if before.get(path)!=desired[path]]
            if changes:
                composer=Composer(workspace)
                _,observed,_=workspace._snapshot(project_id, result_paths=result_paths)
                if observed!=before:
                    return {**result,'workspace_warning':'固定提交已保存；IDE修改已保留，本地版本未写回，请重新打开项目核对。'}
                composer._write_journal(project_id,changes)
                descriptor=workspace._open_project_fd(project_id)
                state=dict(before)
                try:
                    for path,old,value in changes:
                        _,observed,_=workspace._snapshot(project_id, result_paths=result_paths)
                        if observed!=state:raise KitError('项目冲突：版本保存期间IDE已修改文件，恢复记录已保留')
                        composer._write_file(project_id,descriptor,path,value,old)
                        state[path]=value
                    _,after,_=workspace._snapshot(project_id, result_paths=result_paths)
                    if after!=desired:raise KitError('项目冲突：版本保存后IDE已修改文件，恢复记录已保留')
                    composer._remove_journal(project_id)
                finally:os.close(descriptor)
        except (KitError, OSError) as exc:
            # The central immutable version has already committed. Report the
            # durable success independently from a local metadata write issue.
            return {**result, 'workspace_warning': '固定提交已保存，但本地版本写回未完成：' + str(exc)}
    try:
        workspace._sync_usage(project_id)
    except (KitError, OSError) as exc:
        return {**result, 'workspace_warning': '固定提交已保存，但本地依赖索引未刷新：' + str(exc)}
    return result
