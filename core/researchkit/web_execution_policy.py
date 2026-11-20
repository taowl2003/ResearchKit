"""Retired Web execution is rejected before any jobs or records are touched."""
from __future__ import annotations

import re

from .files import KitError

MESSAGE = '网页检查、运行和旧执行记录已移除；请在个人 IDE 运行，在项目中查看参数与产物'
RETIRED_METHODS = {
    'execution_status', 'request_job', 'list_jobs', 'get_job', 'cancel_job', 'job_artifacts',
    'list_project_jobs', 'save_run_artifacts_as_derived', 'prepare_project_trial',
    'review_jobs', 'review_job', 'list_asset_candidate_validations', 'create_asset_candidate_validation',
}


def retired_method(method):
    if isinstance(method, str) and (method in RETIRED_METHODS or method.startswith('runner_')):
        raise KitError(MESSAGE)


def retired_http(route):
    if (route == 'execution' or route == 'jobs' or route.startswith('jobs/') or
            route == 'review/jobs' or route.startswith('review/jobs/') or
            re.fullmatch(r'projects/[^/]+/(?:jobs|trial)', route) or
            re.fullmatch(r'submissions/[^/]+/jobs', route) or
            re.fullmatch(r'asset-candidates/[^/]+/validations', route)):
        import tornado.web
        raise tornado.web.HTTPError(410, MESSAGE)
