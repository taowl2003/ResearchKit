from __future__ import annotations

import copy
import json

import pytest

from researchkit.files import KitError
from researchkit.web_versions import _lock, apply_versions, preview_versions


def _plugin(name: str = 'example.scale', version: str = '1.0.0', body: str = 'return inputs["value"]') -> dict[str, bytes]:
    return {
        'plugin.yaml': (
            'schema: hydro.plugin/v1\n'
            f'name: {name}\nversion: {version}\nentry: plugin.py\n'
            'inputs:\n  value: scalar@1.0.0\noutputs:\n  value: scalar@1.0.0\n'
            'parameters:\n  factor:\n    type: number\n    required: true\n'
        ).encode(),
        'plugin.py': f'def run(inputs, parameters, context):\n    {body}\n'.encode(),
    }


def _files(*, protected: bool = True, factor: int = 2, plugins: dict[str, tuple[str, dict[str, bytes]]] | None = None) -> dict[str, bytes]:
    plugins = plugins or {'scale': ('example.scale', _plugin())}
    configs = []
    files: dict[str, bytes] = {
        'case.py': b'def run(context):\n    return context.call("scale", inputs={"value": 3})\n',
        '.researchkit/contracts/scalar/1.0.0.yaml': b'id: scalar@1.0.0\ntype: number\n',
        'task': b'#!/bin/sh\n',
    }
    prefix = '.researchkit/plugins' if protected else 'algorithms'
    for alias, (_name, contents) in plugins.items():
        configs.append(f'  {alias}:\n    path: {prefix}/{alias}\n    parameters:\n      factor: {factor}\n')
        for rel, data in contents.items():
            files[f'{prefix}/{alias}/{rel}'] = data
    files['project.yaml'] = (
        'schema: researchkit.project/v1\nname: example\nmode: python\nentry: case.py\nplugins:\n'
        + ''.join(configs) + 'parameters: {}\ndata: {}\n'
        'web_composition:\n  schema: researchkit.web-composition/v1\n  steps:\n'
        + ''.join(f'  - alias: {alias}\n    ref: {name}@1.0.0\n    parameters: {{}}\n    inputs: {{}}\n'
                  for alias, (name, _contents) in plugins.items())
        + '  draft_contracts: []\n'
    ).encode()
    files['.researchkit/lock.json'] = _lock(files)
    return files


def _change_plugin(files: dict[str, bytes], alias: str = 'scale') -> dict[str, bytes]:
    result = dict(files)
    path = next(path for path in result if path.endswith(f'/{alias}/plugin.py'))
    result[path] += b'\n# improved\n'
    return result


def test_changed_algorithm_requires_new_version_and_safely_copies_protected_runtime():
    before = _files()
    current = _change_plugin(before)
    # A changed protected dependency may only arrive through a trusted sealed
    # source; refreshing the lock models that situation.  A later test covers
    # the unsealed/tampered counterpart.
    current['.researchkit/lock.json'] = _lock(current)
    plan = preview_versions(current, before, previous_version='1.0')
    algorithm = plan['algorithms'][0]
    assert algorithm['changed'] is True
    assert algorithm['suggested_version'] == '1.0.1'
    assert plan['case'] == {**plan['case'], 'current_version': '1.0', 'changed': True, 'suggested_version': '1.1', 'fingerprint': plan['case']['fingerprint']}
    with pytest.raises(KitError, match='高于原版本'):
        apply_versions(current, plan, {'case_version': '1.1', 'algorithms': {'scale': '1.0.0'}})
    applied = apply_versions(current, plan, {'case_version': '1.1', 'algorithm_versions': {'scale': '1.0.1'}})
    assert applied['.researchkit/plugins/scale/plugin.yaml'] == current['.researchkit/plugins/scale/plugin.yaml']
    assert b'version: 1.0.1' in applied['algorithms/scale/plugin.yaml']
    assert b'path: algorithms/scale' in applied['project.yaml']
    assert b'ref: example.scale@1.0.1' in applied['project.yaml']
    assert json.loads(applied['.researchkit/lock.json']) == json.loads(_lock(applied))


def test_case_parameters_do_not_bump_algorithm_and_no_changes_reuse_versions():
    before = _files(protected=False, factor=2)
    current = _files(protected=False, factor=3)
    changed = preview_versions(current, before, previous_version='1.0')
    assert changed['case']['changed'] is True
    assert changed['algorithms'][0]['changed'] is False
    assert changed['algorithms'][0]['suggested_version'] == '1.0.0'
    same = preview_versions(before, before, previous_version='1.0')
    assert same['case']['changed'] is False
    assert same['case']['suggested_version'] == '1.0'
    assert same['algorithms'][0]['changed'] is False
    assert apply_versions(before, same, {'case_version': '1.0', 'algorithms': {'scale': '1.0.0'}}) == before


def test_explicit_algorithm_label_change_also_requires_a_new_case_version():
    before=_files(protected=False)
    changed=dict(before)
    path='algorithms/scale/plugin.yaml'
    changed[path]=changed[path].replace(b'version: 1.0.0',b'version: 1.0.1')
    plan=preview_versions(changed,before,previous_version='1.0')
    assert plan['case']['changed'] and plan['case']['suggested_version']=='1.1'
    assert plan['algorithms'][0]['changed'] and plan['algorithms'][0]['suggested_version']=='1.0.1'


def test_contract_changes_bump_algorithm_but_small_artifacts_only_bump_case():
    before = _files(protected=False)
    current = dict(before)
    current['.researchkit/contracts/scalar/1.0.0.yaml'] = b'id: scalar@1.0.0\ntype: integer\n'
    plan = preview_versions(current, before, previous_version='1.0')
    assert plan['algorithms'][0]['changed'] is True
    assert plan['algorithms'][0]['suggested_version'] == '1.0.1'
    noisy = dict(before)
    noisy['results/out.csv'] = b'x\n'
    noisy['_runs/old/log'] = b'old\n'
    noisy['algorithms/scale/__pycache__/plugin.pyc'] = b'not source'
    clean_plan = preview_versions(noisy, before, previous_version='1.0')
    assert clean_plan['case']['changed'] is True
    assert clean_plan['algorithms'][0]['changed'] is False


def test_first_claim_uses_baseline_and_multiple_plugins_are_independent():
    plugins = {
        'scale': ('example.scale', _plugin()),
        'offset': ('example.offset', _plugin('example.offset', body='return inputs["value"] + 1')),
    }
    baseline = _files(protected=False, plugins=plugins)
    current = _change_plugin(baseline, 'offset')
    plan = preview_versions(current, None, baseline=baseline, previous_version=None)
    assert isinstance(plan['snapshot_revision'], str)
    assert plan['comparison']['source'] == 'baseline'
    assert plan['case']['suggested_version'] == '0.1'
    assert preview_versions(current, None, baseline=baseline, previous_version=None, kind='major')['case']['suggested_version'] == '1.0'
    rows = {row['alias']: row for row in plan['algorithms']}
    assert rows['scale']['changed'] is False
    assert rows['offset']['changed'] is True
    assert rows['offset']['suggested_version'] == '1.0.1'


def test_protected_runtime_tamper_and_path_traversal_are_rejected():
    before = _files()
    current = _change_plugin(before)
    current['task'] = b'evil\n'
    plan = preview_versions(current, before, previous_version='1.0')
    with pytest.raises(KitError, match='锁已失效'):
        apply_versions(current, plan, {'case_version': '1.1', 'algorithms': {'scale': '1.0.1'}})
    invalid = _files(protected=False)
    invalid['project.yaml'] = invalid['project.yaml'].replace(b'path: algorithms/scale', b'path: ../outside')
    with pytest.raises(KitError, match='非法路径'):
        preview_versions(invalid, None)


def test_alias_rename_matches_only_unique_name_and_version_choices_are_checked():
    before = _files(protected=False)
    current = _files(protected=False)
    current['project.yaml'] = current['project.yaml'].replace(b'  scale:\n', b'  resize:\n').replace(b'algorithms/scale', b'algorithms/resize').replace(b'alias: scale', b'alias: resize')
    for suffix in ('plugin.yaml', 'plugin.py'):
        current[f'algorithms/resize/{suffix}'] = current.pop(f'algorithms/scale/{suffix}')
    current['.researchkit/lock.json'] = _lock(current)
    plan = preview_versions(current, before, previous_version='1.0')
    assert plan['algorithms'][0]['previous_version'] == '1.0.0'
    assert plan['algorithms'][0]['changed'] is False
    with pytest.raises(KitError, match='未变化算法'):
        apply_versions(current, plan, {'case_version': '1.0', 'algorithms': {'resize': '1.0.1'}})
