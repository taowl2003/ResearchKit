import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from researchkit.files import KitError
from researchkit.web_dataset_discovery import discover, discover_one, source_units, snapshot_fingerprint
from researchkit.web_dataset_readme import scan
from researchkit.web_dataset_trees import tree_metadata


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    def open_dir(path, **_kwargs):
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with patch('researchkit.web_datasets.DatasetRegistry._open_directory', side_effect=open_dir):
        yield root


def config(root):
    return {'raw_root': root, 'discovery_mode': 'readme', 'prepare_permissions': True,
            'seconds_per_dataset': 10}


def policy():
    return SimpleNamespace(dataset_tree_registry=None, dataset_metadata=None)


def test_nested_boundary_and_description_refresh(raw):
    group = raw / 'GIS'; group.mkdir()
    a = group / 'a'; a.mkdir()
    (a / 'Readme.md').write_text('第一版', encoding='utf-8')
    (a / 'data.csv').write_text('123')
    nested = a / 'nested'; nested.mkdir()
    (nested / 'README.md').write_text('not a separate dataset')
    candidate = discover(config(raw), policy())[0]
    assert candidate['relative_root'] == 'GIS/a'
    assert candidate['draft_metadata']['description'] == '第一版'
    assert candidate['kind'] == 'new'
    assert len(source_units(config(raw))) == 1
    (a / 'Readme.md').write_text('第二版说明', encoding='utf-8')
    current = discover_one(config(raw), policy(), 'GIS/a')
    assert current['draft_metadata']['description'] == '第二版说明'
    assert current['source_fingerprint'] == candidate['source_fingerprint']
    assert current['metadata_digest'] != candidate['metadata_digest']
    with pytest.raises(KitError, match='上级目录'):
        discover_one(config(raw), policy(), 'GIS/a/nested')


def test_invalid_marker_is_isolated(raw):
    invalid = raw / 'bad'; invalid.mkdir()
    (invalid / 'README.md').symlink_to('/etc/passwd')
    good = raw / 'good'; good.mkdir()
    (good / 'README.md').write_text('')
    found = discover(config(raw), policy())
    assert [(item['relative_root'], item['kind']) for item in found] == [('bad', 'error'), ('good', 'new')]
    (invalid / 'README.md').unlink()
    (invalid / 'README.md').write_text('')
    (invalid / 'readme.md').write_text('')
    assert '多个' in scan(raw, seconds=10)['bad']['error']


def test_readme_frontmatter_tags_are_bounded_and_removed_from_description(raw):
    tagged = raw / 'tagged'; tagged.mkdir()
    (tagged / 'README.md').write_text('---\ntags:\n  - Climate\n  - 教学\n  - Climate\n---\n# 正文\n\n只显示正文。', encoding='utf-8')
    string_tags = raw / 'string-tags'; string_tags.mkdir()
    (string_tags / 'README.md').write_text('---\n标签: 气象，教学、公开\n---\n正文', encoding='utf-8')
    found = scan(raw, seconds=10)
    assert found['tagged']['tags'] == ['Climate', '教学']
    assert found['tagged']['description'] == '# 正文\n\n只显示正文。'
    assert 'tags:' not in found['tagged']['description']
    assert found['string-tags']['tags'] == ['气象', '教学', '公开']
    candidates = {item['relative_root']: item for item in discover(config(raw), policy())}
    assert candidates['tagged']['original_metadata']['tags'] == ['Climate', '教学']


def test_invalid_readme_tags_do_not_block_other_candidates(raw):
    broken = raw / 'broken'; broken.mkdir()
    (broken / 'README.md').write_text('---\ntags: [valid, 4]\n---\n正文', encoding='utf-8')
    good = raw / 'good'; good.mkdir()
    (good / 'README.md').write_text('---\ntags: [valid]\n---\n正文', encoding='utf-8')
    found = scan(raw, seconds=10)
    assert 'README tags' in found['broken']['error']
    assert found['good']['tags'] == ['valid']


def test_untrusted_tree_rejects_hardlinks_and_ignores_only_root_readme(raw):
    a = raw / 'a'; a.mkdir()
    (a / 'README.md').write_text('x')
    (a / 'data').write_text('payload')
    fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = tree_metadata(fd, readme_mode=True, trusted=False)
        (a / 'README.md').write_text('different')
        after = tree_metadata(fd, readme_mode=True, trusted=False)
        assert snapshot_fingerprint(before) == snapshot_fingerprint(after)
        os.link(a / 'data', a / 'hardlink')
        with pytest.raises(KitError):
            tree_metadata(fd, readme_mode=True, trusted=False)
    finally:
        os.close(fd)


def test_v2_verify_accepts_readme_edits_but_rejects_data_edits(raw):
    from researchkit.web_dataset_trees import TreeRegistry, TREE_SCHEMA_README
    a = raw / 'a'; a.mkdir()
    (a / 'README.md').write_text('first')
    (a / 'data').write_text('first')
    fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Confine ownership mock to this synthetic tree; production checks remain intact.
        with patch('researchkit.web_datasets._root_owned', return_value=True):
            snapshot = tree_metadata(fd, hash_files=True, readme_mode=True)
            manifest = {'schema': TREE_SCHEMA_README, **snapshot}
            (a / 'README.md').write_text('changed explanation')
            TreeRegistry._verify(fd, manifest, lambda: False)
            (a / 'data').write_text('changed data')
            with pytest.raises(KitError, match='发生变化'):
                TreeRegistry._verify(fd, manifest, lambda: False)
    finally:
        os.close(fd)


def test_v2_removed_readme_does_not_change_pinned_data(raw):
    from researchkit.web_dataset_trees import TreeRegistry, TREE_SCHEMA_README
    a = raw / 'a'; a.mkdir()
    (a / 'README.md').write_text('notes')
    (a / 'data').write_text('data')
    fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with patch('researchkit.web_datasets._root_owned', return_value=True):
            manifest = {'schema': TREE_SCHEMA_README, **tree_metadata(fd, hash_files=True, readme_mode=True)}
            (a / 'README.md').unlink()
            TreeRegistry._verify(fd, manifest, lambda: False)
            os.mkfifo(a / 'README.md')
            with pytest.raises(KitError, match='普通文件'):
                TreeRegistry._verify(fd, manifest, lambda: False)
    finally:
        os.close(fd)


def test_registered_missing_marker_preserves_published_metadata(raw):
    from researchkit.web_dataset_discovery import _discover_readmes
    from researchkit.web_dataset_trees import TREE_SCHEMA_README
    a = raw / 'a'; a.mkdir()
    (a / 'data').write_text('data')
    fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with patch('researchkit.web_datasets._root_owned', return_value=True):
            snapshot = tree_metadata(fd, hash_files=True, readme_mode=True)
            doc = {'schema': TREE_SCHEMA_README, **snapshot}
            published = {'description': 'original', 'provenance_ref': 'README:README.md#sha256=old'}
            with patch('researchkit.web_dataset_discovery._registered', return_value=({str(a): ('old-v1', doc, 'platform-users')}, {'old-v1': published})):
                item = _discover_readmes(config(raw), policy())[0]
            assert item['kind'] == 'unchanged'
            assert item['original_metadata']['description'] == 'original'
            assert item['readme_managed'] is False
    finally:
        os.close(fd)
