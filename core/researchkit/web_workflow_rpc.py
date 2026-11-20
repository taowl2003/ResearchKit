"""Authenticated workflow operations shared by broker and local UI adapter."""
from __future__ import annotations
from .files import KitError
from .web_submission_flow import SubmissionFlow

METHODS = {
    'preview_submission_versions', 'find_submission_preparation', 'prepare_submission',
    'prepare_project_trial', 'get_submission_preparation', 'review_submission_projects', 'review_project_versions',
    'review_submission', 'review_release', 'review_publish', 'review_register_test_copy',
    'review_publish_batch', 'review_jobs', 'review_job',
}

def request(store, library_root, principal, method, params, *, ready=False, authorize=None, bind=None):
    from .web_broker import _expect_keys
    if method not in METHODS:
        raise KitError('工作流请求无效')
    if method.startswith('review_'):
        if principal.role != 'teacher':
            raise KitError('只有教师可以查看提交与入库')
        filters = {'q','owner','kind','from_date','to_date','limit','cursor'}
        if method in {'review_submission_projects','review_project_versions'}:
            required = {'project_id'} if method == 'review_project_versions' else set()
            body = _expect_keys(params, filters | required, required, method)
            if method == 'review_project_versions':
                return store.list_project_versions(principal, body.pop('project_id'), **body)
            return store.list_submission_projects(principal, latest_first=True, **body)
        publish_allowed = {
            'submission_id', 'kind', 'aliases', 'expected_sha256',
            'publish_mode', 'parent_ref', 'name', 'version', 'tags', 'description',
            'readme_markdown', 'dataset_id', 'dataset_scope',
            'embedded_algorithms', 'embedded_datasets',
        }
        fields = {
            'review_submission': {'submission_id'},
            'review_release': {'submission_id'},
            'review_publish': publish_allowed,
            'review_publish_batch': {'submission_id', 'expected_sha256', 'items'},
            'review_register_test_copy': {'submission_id', 'project_id'},
            'review_jobs': {'submission_id'},
            'review_job': {'run_id'},
        }
        required_fields = (
            {'submission_id', 'kind', 'expected_sha256'} if method == 'review_publish'
            else {'submission_id', 'expected_sha256', 'items'} if method == 'review_publish_batch'
            else set() if method == 'review_jobs'
            else fields[method]
        )
        body = _expect_keys(params, fields[method], required_fields, method)
        if method == 'review_submission':
            return store.get_submission(body['submission_id'], principal)
        if method in {'review_jobs','review_job'}:
            raise KitError('网页执行记录已移除；请在 IDE 中运行项目')
        from .web_review_flow import ReviewFlow
        service = ReviewFlow(store,library_root)
        if method == 'review_release':
            return service.release(principal,body['submission_id'])
        if method == 'review_register_test_copy':
            return service.register_test_copy(principal, body['submission_id'], body['project_id'])
        if method == 'review_publish_batch':
            return service.publish_batch(principal, body['submission_id'],
                                         expected_sha256=body['expected_sha256'], items=body['items'])
        return service.publish(principal,body.pop('submission_id'),**body)
    flow = SubmissionFlow(store,library_root)
    fields = {
        'preview_submission_versions':{'project_id','files','kind'},
        'find_submission_preparation':{'project_id','request_id','request_fingerprint'},
        'prepare_submission':{'project_id','files','data_references','kind','case_version','request_id','request_fingerprint'},
        'prepare_project_trial':{'project_id','files','data_references','request_id','request_fingerprint'},
        'get_submission_preparation':{'preparation_id'},
    }
    body = _expect_keys(params,fields[method],fields[method],method)
    if method == 'preview_submission_versions':
        return flow.preview(principal,body['project_id'],body['files'],body['kind'])
    if method == 'find_submission_preparation':
        result = flow.find_direct(principal,**body)
    elif method == 'get_submission_preparation':
        result = flow.get_direct(principal,body['preparation_id'])
    elif method == 'prepare_project_trial':
        raise KitError('网页试运行已移除；请在 IDE 中检查和运行后再固定提交')
    else:
        known = flow.find_direct(principal,body['project_id'],body['request_id'],body['request_fingerprint'])
        if known is not None:
            result = known
        else:
            if authorize is not None:
                authorize(body['data_references'])
            result = flow.direct(principal,body.pop('project_id'),**body)
    if result is not None and result.get('submission_id') and bind is not None:
        bind(result['submission_id'])
    return result
