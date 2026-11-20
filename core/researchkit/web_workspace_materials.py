"""Explicitly add task materials without changing a runnable case's entry."""
from __future__ import annotations
import json
import os
import hashlib
import yaml
import re
from .files import KitError, identifier, reference
from .model import verify_lock
from .model import project as validate_project_model, validate_parameters, verify_lock
from .web_store import _validate_data_references, _validate_files


def _resource_receipt(files):
    try:
        receipt = json.loads(files.get('materials/algorithms/RESOURCES.json', b'{"materials":[]}'))
        if type(receipt) is not dict or set(receipt) != {'materials'} or type(receipt['materials']) is not list:
            raise ValueError()
        seen = set()
        for row in receipt['materials']:
            if type(row) is not dict or set(row) != {'kind', 'ref', 'path'} or row['kind'] != 'algorithm' or not isinstance(row['ref'], str):
                raise ValueError()
            reference(row['ref'])
            if row['ref'] in seen or row['path'] != 'materials/algorithms/' + hashlib.sha256(row['ref'].encode()).hexdigest():
                raise ValueError()
            seen.add(row['ref'])
        return receipt
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError, KitError) as exc:
        raise KitError('项目算法材料记录无法核对，未覆盖已有内容') from exc


def _known_algorithms(files):
    """Recognize exact versions already bound or received, without moving edits."""
    known = set()
    known.update(row['ref'] for row in _resource_receipt(files)['materials'])
    try:
        document = yaml.safe_load(files.get('project.yaml', b'')) or {}
        for config in document.get('plugins', {}).values():
            if not isinstance(config, dict) or not isinstance(config.get('path'), str):
                continue
            metadata = yaml.safe_load(files.get(config['path'].rstrip('/') + '/plugin.yaml', b'')) or {}
            if isinstance(metadata, dict) and metadata.get('name') and metadata.get('version'):
                known.add(str(metadata['name']) + '@' + str(metadata['version']))
        for path, content in files.items():
            if path.startswith('materials/tasks/') and path.endswith('/TASK.json'):
                receipt = json.loads(content)
                if not isinstance(receipt, dict):
                    raise ValueError()
                if not isinstance(receipt.get('materials', []), list):
                    raise ValueError()
                for row in receipt.get('materials', []):
                    if row.get('kind') == 'algorithm' and isinstance(row.get('ref'), str):
                        known.add(row['ref'])
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError, RecursionError, yaml.YAMLError) as exc:
        raise KitError('项目算法材料记录无法核对，未覆盖已有内容') from exc
    return known


def _commit(workspace, project_id, before, desired):
    from .web_composer import Composer
    _validate_files(desired)
    changes = [(path, before.get(path), desired[path]) for path in sorted(desired) if before.get(path) != desired[path]]
    if not changes:
        return
    writer = Composer(workspace)
    writer._write_journal(project_id, changes)
    descriptor = workspace._open_project_fd(project_id)
    state = dict(before)
    try:
        for path, old, value in changes:
            _, observed, _ = workspace._snapshot(project_id)
            if observed != state:
                raise KitError('项目冲突：添加期间IDE修改了项目，恢复记录已保留')
            writer._write_file(project_id, descriptor, path, value, old)
            state[path] = value
        _, after, _ = workspace._snapshot(project_id)
        if after != desired:
            raise KitError('项目冲突：添加后项目发生变化，恢复记录已保留')
        writer._remove_journal(project_id)
    finally:
        os.close(descriptor)


def _extract_contract_refs(plugin_meta):
    refs = set()
    if not isinstance(plugin_meta, dict):
        return refs
    for section in ('inputs', 'outputs'):
        block = plugin_meta.get(section)
        if isinstance(block, dict):
            for spec in block.values():
                if isinstance(spec, dict) and isinstance(spec.get('contract'), str):
                    refs.add(spec['contract'])
                elif isinstance(spec, str) and '@' in spec:
                    refs.add(spec)
    for key in ('input_contract', 'output_contract'):
        if isinstance(plugin_meta.get(key), str) and '@' in plugin_meta[key]:
            refs.add(plugin_meta[key])
    return sorted(refs)


def _verify_existing_lock_if_present(project_root: Path, before: dict[str, bytes]) -> dict[str, str]:
    has_protected = '.researchkit/lock.json' in before or any(
        p == 'task' or p.startswith(('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/'))
        for p in before
    )
    if not has_protected:
        return {}
    verify_lock(project_root)
    try:
        doc = json.loads(before.get('.researchkit/lock.json', b'{}').decode('utf-8'))
        files_map = doc.get('files')
        if isinstance(files_map, dict):
            return {str(k): str(v) for k, v in files_map.items()}
    except Exception:
        pass
    return {}


def add_algorithms(workspace, project_id, payload):
    """Add platform or local algorithms to a personal project.

    - Legacy mode (payload == {'algorithm_refs': [...]}): copies algorithm bundles
      into materials/algorithms/<sha>/ without touching project.yaml.
    - Bind mode ('bind' in payload or 'alias' in payload or 'local_algorithm_id' in payload):
      installs into .researchkit/plugins/<alias> (platform) or algorithms/<alias> (local),
      copies contracts, updates project.yaml and .researchkit/lock.json.
    """
    if type(payload) is not dict:
        raise KitError('算法材料选择字段无效')
    allowed = {'algorithm_refs', 'algorithm_ref', 'local_algorithm_id', 'alias', 'bind', 'parameters'}
    if set(payload) - allowed:
        raise KitError('算法材料选择字段无效')

    bind_mode = bool(payload.get('bind')) or ('alias' in payload) or ('local_algorithm_id' in payload) or ('algorithm_ref' in payload) or ('parameters' in payload)
    if not bind_mode:
        if set(payload) != {'algorithm_refs'}:
            raise KitError('算法材料选择字段无效')
        refs = payload['algorithm_refs']
        if type(refs) is not list or not refs or len(refs) > 128 or any(not isinstance(ref, str) for ref in refs):
            raise KitError('请选择有限的准确算法版本')
        refs = sorted(set(refs))
        components = [(ref, workspace.store.request('get_shared_component', ref=ref)) for ref in refs]
        with workspace._project_guard(project_id):
            project_root, before, _ = workspace._snapshot(project_id)
            _verify_existing_lock_if_present(project_root, before)
            desired = dict(before)
            known = _known_algorithms(before)
            receipt_path = 'materials/algorithms/RESOURCES.json'
            receipt = _resource_receipt(before)
            for ref, component in components:
                if ref in known:
                    continue
                files, _ = _validate_files(component['files'])
                base = 'materials/algorithms/' + hashlib.sha256(ref.encode()).hexdigest()
                for path, content in files.items():
                    target = base + '/' + path
                    if target in desired and desired[target] != content:
                        raise KitError('材料目录已有个人修改，未覆盖：' + target)
                    desired[target] = content
                receipt['materials'].append({'kind': 'algorithm', 'ref': ref, 'path': base})
                known.add(ref)
            if receipt['materials']:
                desired[receipt_path] = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
            _commit(workspace, project_id, before, desired)
        workspace._sync_usage(project_id)
        return {'project_id': project_id, 'applied_algorithm_refs': refs}

    # Direct bind mode with alias into project.yaml
    local_id = payload.get('local_algorithm_id')
    ref = payload.get('algorithm_ref')
    if not ref and isinstance(payload.get('algorithm_refs'), list) and len(payload['algorithm_refs']) == 1:
        ref = payload['algorithm_refs'][0]
    if bool(local_id) == bool(ref):
        raise KitError('请指定单个平台算法引用或本地算法 ID')

    raw_alias = payload.get('alias')
    user_params = payload.get('parameters')
    if user_params is not None and not isinstance(user_params, dict):
        raise KitError('算法绑定 parameters 必须是对象映射')
    if local_id:
        if not isinstance(local_id, str) or not local_id.strip():
            raise KitError('本地算法 ID 无效')
        local_algo = workspace._read_local_algorithm_bundle(local_id.strip())
        all_local, _ = _validate_files(local_algo['files'])
        if any(k.startswith('plugin/') for k in all_local):
            files = {k[7:]: v for k, v in all_local.items() if k.startswith('plugin/')}
            contracts = {k: v for k, v in all_local.items() if k.startswith('contracts/')}
        else:
            files = {k: v for k, v in all_local.items() if not k.startswith('contracts/')}
            contracts = {k: v for k, v in all_local.items() if k.startswith('contracts/')}
        meta_doc = yaml.safe_load(files['plugin.yaml'].decode('utf-8')) or {}
        algo_name = str(meta_doc.get('name') or local_algo['name'])
        default_alias = re.sub(r'[^A-Za-z0-9._-]+', '_', algo_name.split('.')[-1]).strip('._-') or 'algo'
        alias = identifier(str(raw_alias).strip() if isinstance(raw_alias, str) and raw_alias.strip() else default_alias)
        install_prefix = f'algorithms/{alias}'
        plugin_cfg = {'path': install_prefix, 'parameters': {}, 'local_source': local_id.strip()}
    else:
        if not isinstance(ref, str) or '@' not in ref:
            raise KitError('平台算法引用格式无效')
        reference(ref)
        component = workspace.store.request('get_shared_component', ref=ref)
        all_files, _ = _validate_files(component['files'])
        if any(k.startswith('plugin/') for k in all_files):
            files = {k[7:]: v for k, v in all_files.items() if k.startswith('plugin/')}
            contracts = {k: v for k, v in all_files.items() if k.startswith('contracts/')}
        else:
            files = dict(all_files)
            contracts = {}
        meta_doc = yaml.safe_load(files.get('plugin.yaml', b'').decode('utf-8')) or {}
        algo_name = str(meta_doc.get('name') or ref.split('@', 1)[0])
        default_alias = re.sub(r'[^A-Za-z0-9._-]+', '_', algo_name.split('.')[-1]).strip('._-') or 'algo'
        alias = identifier(str(raw_alias).strip() if isinstance(raw_alias, str) and raw_alias.strip() else default_alias)
        install_prefix = f'.researchkit/plugins/{alias}'
        plugin_cfg = {'path': install_prefix, 'parameters': {}, 'source_ref': ref}

    # Fetch any declared contracts missing from bundle (for both platform & local algorithms)
    for cref in _extract_contract_refs(meta_doc):
        cn, cv = reference(cref)
        rel_c = f'contracts/{cn}/{cv}.yaml'
        if rel_c not in contracts:
            try:
                c_res = workspace.store.request('get_shared_contract', ref=cref)
                c_files = c_res.get('files') if isinstance(c_res, dict) else None
                if isinstance(c_files, dict) and c_files:
                    first_bytes = next(iter(c_files.values()))
                    if isinstance(first_bytes, (bytes, bytearray)):
                        contracts[rel_c] = bytes(first_bytes)
            except KitError:
                pass

    param_rules = meta_doc.get('parameters') if isinstance(meta_doc.get('parameters'), dict) else {}
    merged_params = {
        k: rule['default']
        for k, rule in param_rules.items()
        if isinstance(rule, dict) and 'default' in rule
    }
    if user_params:
        merged_params.update(user_params)
    validate_parameters(param_rules, merged_params, alias)
    plugin_cfg['parameters'] = merged_params

    with workspace._project_guard(project_id):
        project_root, before, _ = workspace._snapshot(project_id)
        verified_lock = _verify_existing_lock_if_present(project_root, before)
        desired = dict(before)
        # Reject unresolved ports before writing any plugin/configuration.
        # An empty library must not leave a half-bound, unrunnable project.
        for cref in _extract_contract_refs(meta_doc):
            cn, cv = reference(cref)
            relative_contract = f'contracts/{cn}/{cv}.yaml'
            if relative_contract not in contracts and '.researchkit/' + relative_contract not in before:
                raise KitError('缺少契约：' + cref)
        try:
            proj_doc = yaml.safe_load(before.get('project.yaml', b'').decode('utf-8'))
        except Exception as exc:
            raise KitError('项目 project.yaml 无法解析') from exc
        if not isinstance(proj_doc, dict) or proj_doc.get('schema') != 'researchkit.project/v1':
            raise KitError('项目 project.yaml 无效')
        plugins = dict(proj_doc.get('plugins') or {})
        if alias in plugins:
            existing_cfg = plugins[alias]
            if not isinstance(existing_cfg, dict) or str(existing_cfg.get('path') or '') != install_prefix:
                raise KitError(f'项目中已存在插件别名 “{alias}”，请换一个别名')
            existing_prefix = install_prefix + '/'
            existing_plugin_files = {
                p[len(existing_prefix):]: v
                for p, v in before.items()
                if p.startswith(existing_prefix)
            }
            if existing_plugin_files != files:
                raise KitError(f'项目中已存在插件别名 “{alias}”，请换一个别名')
            same_source = False
            if local_id:
                same_source = str(existing_cfg.get('local_source') or '') == local_id.strip()
            else:
                existing_source = str(existing_cfg.get('source_ref') or '')
                if not existing_source and '.researchkit/origins.json' in before:
                    try:
                        origins_cur = json.loads(before['.researchkit/origins.json'].decode('utf-8'))
                        if isinstance(origins_cur, dict) and isinstance(origins_cur.get('algorithms'), dict):
                            existing_source = str((origins_cur['algorithms'].get(alias) or {}).get('ref') or '')
                    except Exception:
                        existing_source = ''
                if not existing_source and (install_prefix + '/plugin.yaml') in before:
                    try:
                        pmeta = yaml.safe_load(before[install_prefix + '/plugin.yaml'].decode('utf-8')) or {}
                        if isinstance(pmeta, dict) and pmeta.get('name') and pmeta.get('version'):
                            existing_source = f"{pmeta['name']}@{pmeta['version']}"
                    except Exception:
                        existing_source = ''
                same_source = existing_source == ref
            existing_params = dict(existing_cfg.get('parameters') or {}) if isinstance(existing_cfg.get('parameters'), dict) else {}
            if same_source and (user_params is None or merged_params == existing_params):
                return {
                    'project_id': project_id,
                    'alias': alias,
                    'path': install_prefix,
                    'parameters': existing_params,
                    'bound': True,
                    'added': False,
                    'idempotent': True,
                    'kind': 'local' if local_id else 'platform',
                    'ref': ref or local_id.strip(),
                }
            raise KitError(f'项目中已存在插件别名 “{alias}”，请换一个别名')
        for rel_p, data in files.items():
            target = f'{install_prefix}/{rel_p}'
            if target in desired and desired[target] != data:
                raise KitError('目标插件目录已存在不同文件：' + target)
            desired[target] = data
        for c_rel, c_data in contracts.items():
            target_c = '.researchkit/' + c_rel
            if target_c not in desired:
                desired[target_c] = c_data
        plugins[alias] = plugin_cfg
        proj_doc['plugins'] = plugins
        desired['project.yaml'] = yaml.safe_dump(proj_doc, allow_unicode=True, sort_keys=False).encode('utf-8')

        if ref:
            try:
                origins = json.loads(before.get('.researchkit/origins.json', b'{"schema":"researchkit.origins/v1","algorithms":{}}').decode('utf-8'))
                if not isinstance(origins, dict):
                    origins = {'schema': 'researchkit.origins/v1', 'algorithms': {}}
            except Exception:
                origins = {'schema': 'researchkit.origins/v1', 'algorithms': {}}
            origins.setdefault('schema', 'researchkit.origins/v1')
            algs_origin = dict(origins.get('algorithms') or {})
            algs_origin[alias] = {'ref': ref, 'path': install_prefix}
            origins['algorithms'] = algs_origin
            desired['.researchkit/origins.json'] = (json.dumps(origins, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')

        newly_locked_paths = set()
        if ref:
            for rel_p in files:
                p = f'{install_prefix}/{rel_p}'
                if p.startswith('.researchkit/plugins/'):
                    newly_locked_paths.add(p)
        for c_rel in contracts:
            p = '.researchkit/' + c_rel
            if p.startswith('.researchkit/contracts/'):
                newly_locked_paths.add(p)
        if newly_locked_paths or verified_lock:
            protected = dict(verified_lock)
            for p in newly_locked_paths:
                if p in desired:
                    protected[p] = hashlib.sha256(desired[p]).hexdigest()
            if not protected:
                protected = {
                    p: hashlib.sha256(v).hexdigest()
                    for p, v in desired.items()
                    if p == 'task' or p.startswith(('.researchkit/runtime/', '.researchkit/plugins/', '.researchkit/contracts/'))
                }
            if protected:
                desired['.researchkit/lock.json'] = (
                    json.dumps(
                        {'schema': 'researchkit.lock/v1', 'files': dict(sorted(protected.items()))},
                        ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
                    ) + '\n'
                ).encode('utf-8')

        _commit(workspace, project_id, before, desired)
        if '.researchkit/lock.json' in desired:
            verify_lock(project_root)
        entry_name = proj_doc.get('entry')
        if proj_doc.get('mode') == 'python' and isinstance(entry_name, str) and (project_root / entry_name).is_file():
            validate_project_model(project_root)
    workspace._sync_usage(project_id)
    return {
        'project_id': project_id,
        'alias': alias,
        'path': install_prefix,
        'parameters': merged_params,
        'bound': True,
        'added': True,
        'idempotent': False,
        'kind': 'local' if local_id else 'platform',
        'ref': ref or local_id,
    }



def apply(workspace,project_id,payload, *, preserve_existing=False):
    if type(payload) is not dict or set(payload)-{'task_id','material_ids'} or 'task_id' not in payload:
        raise KitError('任务材料选择字段无效')
    detail=workspace.store.request('task_materials',task_id=payload['task_id'],personal=True,include_files=True)
    available={row['material_id']:row for row in detail['materials'] if row['kind']!='template'}
    selected=payload.get('material_ids',list(available))
    if type(selected) is not list or len(selected)>256 or any(not isinstance(i,str) or i not in available for i in selected) or len(set(selected))!=len(selected):
        raise KitError('请选择当前有权领取的任务材料')
    with workspace._project_guard(project_id):
        _,before,refs=workspace._snapshot(project_id)
        desired=dict(before)
        additions=[]
        base='materials/tasks/'+detail['task_id']
        receipt_path=base+'/TASK.json'
        try:
            receipt=json.loads(before.get(receipt_path,b'{}'))
        except (ValueError,UnicodeDecodeError):raise KitError('任务材料领取记录已损坏，未覆盖')
        if (type(receipt) is not dict or type(receipt.get('applied_material_ids',[])) is not list or
                any(not isinstance(mid,str) for mid in receipt.get('applied_material_ids',[]))):
            raise KitError('任务材料领取记录已损坏，未覆盖')
        previous=set(receipt.get('applied_material_ids',[]))
        known = _known_algorithms(before)
        for mid in selected:
            item=available[mid]
            if item['kind']=='dataset':
                additions.append(item['metadata']['reference'])
            elif item['kind']=='algorithm':
                # A repeated whole-task claim must retain work on an already
                # received tool. New tools still refuse conflicting files.
                if (preserve_existing and mid in previous) or item['ref'] in known:
                    continue
                files,_=_validate_files(item['files'])
                for path,content in files.items():
                    target=base+'/'+mid+'/'+path
                    if target in desired and desired[target]!=content:
                        raise KitError('材料目录已有个人修改，未覆盖：'+target)
                    desired[target]=content
                known.add(item['ref'])
        refs=_validate_data_references(list({json.dumps(ref,sort_keys=True,separators=(',',':')):ref for ref in [*refs,*additions]}.values()))
        if additions:
            desired['DATA-REFERENCES.json']=(json.dumps(refs,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
            # Use the same format as the normal project data editor.
            workspace._references_document(desired['DATA-REFERENCES.json'])
        mids=sorted(previous|set(selected))
        desired[receipt_path]=(json.dumps({'task_id':detail['task_id'],'name':detail['name'],
            'instructions':detail['instructions'],'applied_material_ids':mids,
            'supplements':detail.get('supplements',[]),
            'materials':[{'material_id':row['material_id'],'kind':row['kind'],'ref':row['ref'],
                          'metadata':row.get('metadata',{})} for row in detail['materials'] if row['material_id'] in mids]},
            ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        note_path=base+'/README.md'
        if note_path not in before:
            note='# '+detail['name']+'\n\n'+detail.get('instructions','')+'\n\n算法工具位于本目录对应材料子目录，数据引用保存在项目根目录 DATA-REFERENCES.json。额外工具不会自动接入 case.py。\n'
            desired[note_path]=note.encode('utf-8')
        _commit(workspace, project_id, before, desired)
    workspace._sync_usage(project_id)
    return {'project_id':project_id,'task_id':detail['task_id'],'path':base,'applied_material_ids':selected}
