"""Executable examples in help must match the actual Web-first workflow."""
from pathlib import Path
import re
import json
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_manual_has_one_web_first_case_appendix_and_no_preinstalled_algorithm_dependency():
    manual = (ROOT/'docs/平台完整使用与管理手册.md').read_text()
    guide = (ROOT/'docs/学生使用指南.md').read_text()
    assert '学生使用指南.md' in manual
    assert 'examples/three-cases/' in guide
    assert 'example.scale@1.0.0' not in guide
    assert '算法配置与编排' not in guide
    def tutorial_block(key, language):
        match = re.search(rf'<!-- BEGIN TUTORIAL: {key} -->\n```{language}\n(.*?)```\n<!-- END TUTORIAL: {key} -->', guide, re.S)
        assert match, key
        return match.group(1).rstrip()

    assert tutorial_block('framework-entry', 'python') == (ROOT/'examples/three-cases/rainfall/case.py').read_text().rstrip()
    assert tutorial_block('acoustic-modular-entry', 'python') == (ROOT/'examples/three-cases/acoustic-modular/case.py').read_text().rstrip()
    for case, alias in (('rainfall', 'scale'), ('mnist-cnn', 'cnn')):
        source = ROOT/'examples/three-cases'/case/'algorithms'/alias
        assert tutorial_block(f'{case}-plugin', 'python') == (source/'plugin.py').read_text().rstrip()
        metadata = yaml.safe_load((source/'plugin.yaml').read_text())
        for field in ('parameters', 'inputs', 'outputs'):
            assert json.loads(tutorial_block(f'{case}-{field}', 'json')) == metadata[field]
    # These are whole editor documents; pasting only a bare case parameter
    # mapping would silently lose parameters in the existing scheme editor.
    schemes=[document for document in (json.loads(block) for block in re.findall(r'```json\n(.*?)```',guide,re.S))
             if 'case_parameters' in document]
    assert len(schemes)==2
    assert [doc['case_parameters']['algorithm_alias'] for doc in schemes]==['scale','cnn']
    assert all('algorithm_parameters' in doc for doc in schemes)
    for alias in ('sim','doa','locator'):
        source=ROOT/'examples/three-cases/acoustic-modular/algorithms'/alias
        key='acoustic-modular-'+alias
        assert tutorial_block(key+'-plugin','python')==(source/'plugin.py').read_text().rstrip()
        assert json.loads(tutorial_block(key+'-parameters','json'))==yaml.safe_load((source/'plugin.yaml').read_text())['parameters']
        number={'sim':1,'doa':2,'locator':3}[alias]
        section=guide[guide.index(f'#### 算子 {number}：'):]
        metadata=yaml.safe_load((source/'plugin.yaml').read_text())
        for field,label in (('inputs','输入契约'),('outputs','输出契约')):
            assert json.loads(re.search(rf'- {label}：`(.*?)`',section).group(1))==metadata[field]


def test_blank_algorithm_tutorial_uses_complete_ide_sources_without_static_examples():
    assert not (ROOT/'core/researchkit/web_static/tutorial-algorithms.json').exists()
    guide = (ROOT/'docs/学生使用指南.md').read_text()
    assert 'plugin.py` 文本框**留空**' in guide
    assert '手动填标识名 `mnist.cnn`' in guide
    assert 'acoustic.simulator' in guide and 'acoustic.doa' in guide and 'acoustic.locator' in guide
