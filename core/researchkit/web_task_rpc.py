"""Broker boundary for teacher task delivery and exact resource selection."""
from __future__ import annotations
import json
from .files import KitError
from .web_task_delivery import TaskDelivery
from .web_sharing import ComponentService

METHODS={'list_tasks','task_materials','create_trial_task','create_resource_task','claim_resource_task','confirm_resource_task','supplement_task','revoke_task_grant',
         'delete_task','dismiss_task','validate_assignment_targets','create_assignment_distributions'}


def _list(value,label):
    if type(value) is not list or len(value)>128 or any(not isinstance(v,str) or not v or len(v)>256 for v in value) or len(set(value))!=len(value):
        raise KitError(label+'必须是不重复的有限列表')
    return value


def resolve(server,principal,payload,base_refs=()):
    from .web_assignments import AssignmentService
    # This pure validator needs no workspace access.
    if type(payload) is not dict:raise KitError('任务内容无效')
    algorithms=_list(payload.get('algorithm_refs',[]),'附加算法')
    datasets=_list(payload.get('dataset_ids',[]),'附加数据')
    service=ComponentService(server.store,server.policy.code_root/'library')
    materials=[]
    for ref in algorithms:
        item=service.component(principal,ref)
        materials.append({'ref':ref,'files':item['files'],'metadata':{'name':item.get('name',ref)}})
    catalog={row['dataset_id']:row for row in server._dataset_catalog(principal)}
    refs=[]
    for dataset_id in datasets:
        if dataset_id not in catalog:raise KitError('数据集不存在、已下架或未获授权：'+dataset_id)
        refs.extend(catalog[dataset_id]['files'])
    merged={json.dumps(ref,sort_keys=True,separators=(',',':')):ref for ref in [*base_refs,*refs]}
    return materials,list(merged.values())


def request(server,principal,method,params):
    from .web_broker import _expect_keys
    from .web_assignments import AssignmentService
    service=TaskDelivery(server.store)
    if method=='list_tasks':
        body=_expect_keys(params,{'personal'},set(),method)
        personal=body.get('personal',False)
        if type(personal) is not bool:raise KitError('personal必须为布尔值')
        items=service.list_tasks(principal,personal=personal)
        if principal.role=='teacher' and not personal:
            items=[service.get_task(principal,row['task_id'],include_materials=True) |
                   {'grants':service.list_active_grants(principal,row['task_id'])} for row in items]
            items.extend(service.list_legacy_distributions(principal))
        return {'items':items}
    if method=='task_materials':
        body=_expect_keys(params,{'task_id','personal','include_files'},{'task_id'},method)
        personal=body.get('personal',False)
        include_files=body.get('include_files',False)
        if type(personal) is not bool or type(include_files) is not bool:raise KitError('任务材料读取参数无效')
        result=service.get_task(principal,body['task_id'],include_materials=True,material_files=include_files,personal=personal)
        if not personal:
            result['grants']=service.list_active_grants(principal,body['task_id'])
        return result
    if method=='claim_resource_task':
        body=_expect_keys(params,{'task_id'},{'task_id'},method)
        return service.claim_resource_project(principal,body['task_id'])
    if method=='confirm_resource_task':
        body=_expect_keys(params,{'task_id','project_id'},{'task_id','project_id'},method)
        return service.confirm_resource_project(principal,body['task_id'],body['project_id'])
    if method=='dismiss_task':
        body=_expect_keys(params,{'task_id'},{'task_id'},method)
        return service.dismiss_task(principal,body['task_id'])
    if principal.role!='teacher':raise KitError('只有教师可以管理任务分发')
    if method=='delete_task':
        body=_expect_keys(params,{'task_id'},{'task_id'},method)
        return service.delete_task(principal,body['task_id'])
    if method=='revoke_task_grant':
        body=_expect_keys(params,{'task_id','grant_id'},{'task_id','grant_id'},method)
        grants=service.list_active_grants(principal,body['task_id'])
        if not any(row['grant_id']==body['grant_id'] for row in grants):raise KitError('授权不属于该任务或已撤销')
        return service.revoke_grant(principal,body['grant_id'])
    if method=='create_trial_task':
        body=_expect_keys(params,{'request_id','submission_id','targets','instructions'},{'request_id','submission_id','targets','instructions'},method)
        source=server.store.get_submission(body['submission_id'],principal)
        refs=source['manifest']['data_references']['references']
        names=body['targets'] or [principal.username]
        targets,refs=server._assignment_targets(principal,names,refs)
        return service.create_trial(principal,{'request_id':body['request_id'],
            'name':'试用 · '+source['project']['name'],'instructions':body['instructions']},
            source_submission_id=body['submission_id'],targets=targets)
    if method=='create_resource_task':
        body=_expect_keys(params,{'task_payload'},{'task_payload'},method)
        normalized,_fingerprint=AssignmentService._normalize(None,body['task_payload'])
        if normalized['template_ref'] is not None:
            raise KitError('资源包任务不能包含案例基线')
        materials,refs=resolve(server,principal,normalized)
        targets,refs=server._assignment_targets(principal,normalized['targets'],refs)
        return service.create_resource_task(principal,
            {key:normalized[key] for key in ('request_id','name','instructions','parent_task_id') if key in normalized},
            targets=targets,algorithm_materials=materials,dataset_references=refs)
    if method=='supplement_task':
        fields={'task_id','request_id','algorithm_refs','dataset_ids','targets','instructions'}
        body=_expect_keys(params,fields,fields,method)
        task=service.get_task(principal,body['task_id'])
        names=body['targets'] or [row['username'] for row in task['recipients']]
        materials,refs=resolve(server,principal,body)
        targets,refs=server._assignment_targets(principal,names,refs)
        return service.supplement(principal,body['task_id'],{'request_id':body['request_id'],'instructions':body['instructions']},
            targets=targets,algorithm_materials=materials,dataset_references=refs)
    fields={'target_usernames','data_references','task_payload'} if method=='validate_assignment_targets' else {'submission_id','target_usernames','request_id','task_payload'}
    required=fields-{'task_payload','request_id'}
    body=_expect_keys(params,fields,required,method)
    source=None
    if method=='create_assignment_distributions':
        source=server.store.get_submission(body['submission_id'],principal)
        refs=source['manifest']['data_references']['references']
    else:refs=body['data_references']
    payload=body.get('task_payload')
    if payload is not None:
        # Validate the same normalized request that was fingerprinted by the
        # safe staging workspace; recipients cannot be smuggled separately.
        normalized,_fingerprint=AssignmentService._normalize(None,payload)
        if normalized['targets']!=sorted(body['target_usernames']):raise KitError('任务目标与预览不一致')
        if source is not None and normalized['request_id']!=body.get('request_id'):raise KitError('任务请求ID不一致')
        materials,allrefs=resolve(server,principal,normalized,refs)
        if source is not None and normalized.get('include_baseline_algorithms') is True:
            from .web_personal_resources import baseline_tools
            materials=baseline_tools(server,principal,source,materials)
    else:
        materials,allrefs=[],refs
        normalized=None
    targets,allrefs=server._assignment_targets(principal,body['target_usernames'],allrefs)
    if method=='validate_assignment_targets':
        if normalized is not None and 'parent_task_id' in normalized:
            service.validate_parent(principal, normalized['parent_task_id'], kind='formal',
                                    delivery_mode='resources' if normalized['template_ref'] is None else 'project')
        return {'targets':[{'uid':p.uid,'username':p.username} for p in targets],'data_references':allrefs}
    if normalized is None:
        # Compatibility for old task clients: immutable baseline, no extras.
        import uuid
        normalized={'request_id':body.get('request_id') or str(uuid.uuid4()),'name':source['project']['name'],'instructions':''}
    return service.create_task(principal,{key:normalized[key] for key in ('request_id','name','instructions','parent_task_id') if key in normalized},
        baseline_submission_id=body['submission_id'],targets=targets,algorithm_materials=materials,dataset_references=allrefs)
