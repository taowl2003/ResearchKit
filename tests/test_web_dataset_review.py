from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def load_tool():
    spec = importlib.util.spec_from_file_location('review_web_datasets', Path('tools/review-web-datasets.py'))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def candidate(root: Path, name='rainfall', *, selected=True, metadata=None):
    unit = root / name
    return {
        'candidate_id': hashlib.sha256(str(unit).encode()).hexdigest()[:24],
        'dataset_id_hint': 'shared-' + hashlib.sha256(str(unit).encode()).hexdigest()[:16] + '-v1',
        'relative_root': name, 'root': str(unit), 'status': 'pending-review', 'selected': selected,
        'metadata': metadata or {'name': name, 'wiki_ref': 'wiki-' + name, 'wiki_fact_state': 'verified'},
    }


def document(root: Path, **kwargs):
    return {'schema': 'researchkit.web-dataset-candidates/v1', 'source_root': str(root),
            'candidates': [candidate(root)], **kwargs}


def test_generates_safe_offline_page_and_resets_all_selected(tmp_path):
    tool = load_tool()
    dangerous = '</script><img src=x onerror=alert(1)>'
    source = document(tmp_path, candidates=[candidate(tmp_path, 'first', metadata={
        'name': dangerous, 'wiki_ref': dangerous, 'wiki_fact_state': 'parent-card-only', 'description': dangerous,
    }), candidate(tmp_path, 'second', selected=True)])
    candidates = tmp_path / 'candidates.json'; candidates.write_text(json.dumps(source), encoding='utf-8')
    output = tmp_path / 'review.html'
    tool.write_review_page(candidates, output)
    page = output.read_text(encoding='utf-8')
    assert "connect-src 'none'" in page
    assert '</script><img' not in page
    assert '\\u003c/script\\u003e' in page
    embedded = page.split('<script id="candidate-data" type="application/json">', 1)[1].split('</script>', 1)[0]
    rendered = json.loads(embedded)
    assert all(item['selected'] is True for item in source['candidates'])
    assert all(item['selected'] is False for item in rendered['candidates'])
    assert rendered['candidates'][0]['metadata']['description'] == dangerous


@pytest.mark.parametrize('mutate', [
    lambda doc: doc.update(source_root='relative'),
    lambda doc: doc['candidates'][0].update(root='/elsewhere/unit'),
    lambda doc: doc['candidates'][0].update(candidate_id='0' * 24),
    lambda doc: doc['candidates'].__setitem__(0, {**doc['candidates'][0], 'metadata': {'x': 'a' * 16001}}),
])
def test_rejects_unsafe_candidate_schema_and_bounds(tmp_path, mutate):
    tool = load_tool(); payload = document(tmp_path); mutate(payload)
    path = tmp_path / 'candidates.json'; path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(tool.ReviewError):
        tool.write_review_page(path, tmp_path / 'review.html')


def test_rejects_duplicate_ids_oversized_input_and_existing_output(tmp_path):
    tool = load_tool(); payload = document(tmp_path)
    duplicate = candidate(tmp_path, 'second'); duplicate['candidate_id'] = payload['candidates'][0]['candidate_id']
    payload['candidates'].append(duplicate)
    path = tmp_path / 'candidates.json'; path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(tool.ReviewError, match='重复'):
        tool.write_review_page(path, tmp_path / 'review.html')
    path.write_text(json.dumps(document(tmp_path)) + ' ' * (4 * 1024 * 1024), encoding='utf-8')
    with pytest.raises(tool.ReviewError, match='4 MiB'):
        tool.write_review_page(path, tmp_path / 'review.html')
    path.write_text(json.dumps(document(tmp_path)), encoding='utf-8')
    output = tmp_path / 'review.html'; output.write_text('keep', encoding='utf-8')
    with pytest.raises(tool.ReviewError, match='不会覆盖'):
        tool.write_review_page(path, output)
    assert output.read_text(encoding='utf-8') == 'keep'
