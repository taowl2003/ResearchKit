#!/usr/bin/env python3
"""Generate an offline review page for web dataset candidates; never reads datasets."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path


SCHEMA = 'researchkit.web-dataset-candidates/v1'
MAX_BYTES = 4 * 1024 * 1024
MAX_CANDIDATES = 4096
MAX_PATH_BYTES = 4096
MAX_METADATA_FIELDS = 32
MAX_METADATA_KEY_BYTES = 128
MAX_METADATA_VALUE_BYTES = 16000
ID_RE = re.compile(r'[0-9a-f]{24}\Z')
DATASET_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


class ReviewError(ValueError):
    pass


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError('候选 JSON 含重复键')
        result[key] = value
    return result


def _bounded_string(value, label, maximum):
    if not isinstance(value, str) or '\x00' in value or len(value.encode('utf-8')) > maximum:
        raise ReviewError('%s 必须是受限的 UTF-8 字符串' % label)
    return value


def _absolute_path(value, label):
    value = _bounded_string(value, label, MAX_PATH_BYTES)
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ReviewError('%s 必须是无 .. 的绝对路径' % label)
    return path


def _relative_path(value, label):
    value = _bounded_string(value, label, MAX_PATH_BYTES)
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not value or '\\' in value or any(part in ('', '.') for part in path.parts):
        raise ReviewError('%s 必须是安全的相对路径' % label)
    return path


def read_candidates(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ReviewError('候选文件必须是最多 4 MiB 的普通文件')
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReviewError('候选文件必须是有限 UTF-8 JSON') from exc


def validate_candidates(document):
    if type(document) is not dict or set(document) != {'schema', 'source_root', 'candidates'} or document.get('schema') != SCHEMA:
        raise ReviewError('候选 schema 无效')
    source_root = _absolute_path(document['source_root'], 'source_root')
    candidates = document['candidates']
    if type(candidates) is not list or len(candidates) > MAX_CANDIDATES:
        raise ReviewError('candidates 必须是不超过 4096 项的列表')
    ids, roots = set(), set()
    for index, candidate in enumerate(candidates):
        label = 'candidates[%d]' % index
        if type(candidate) is not dict or set(candidate) != {'candidate_id', 'dataset_id_hint', 'relative_root', 'root', 'status', 'selected', 'metadata'}:
            raise ReviewError('%s 字段无效' % label)
        candidate_id = candidate['candidate_id']
        if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id) or candidate_id in ids:
            raise ReviewError('%s candidate_id 无效或重复' % label)
        ids.add(candidate_id)
        if not isinstance(candidate['dataset_id_hint'], str) or not DATASET_ID_RE.fullmatch(candidate['dataset_id_hint']):
            raise ReviewError('%s dataset_id_hint 无效' % label)
        _bounded_string(candidate['status'], label + '.status', 128)
        if type(candidate['selected']) is not bool:
            raise ReviewError('%s selected 必须是布尔值' % label)
        root = _absolute_path(candidate['root'], label + '.root')
        relative = _relative_path(candidate['relative_root'], label + '.relative_root')
        try:
            root.relative_to(source_root)
        except ValueError as exc:
            raise ReviewError('%s root 不属于 source_root' % label) from exc
        if root == source_root or root != source_root / relative:
            raise ReviewError('%s root 与 relative_root 不匹配' % label)
        if str(root) in roots:
            raise ReviewError('%s root 重复' % label)
        roots.add(str(root))
        if candidate_id != hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:24]:
            raise ReviewError('%s candidate_id 与 root 不匹配' % label)
        metadata = candidate['metadata']
        if type(metadata) is not dict or len(metadata) > MAX_METADATA_FIELDS:
            raise ReviewError('%s metadata 无效或字段过多' % label)
        for key, value in metadata.items():
            _bounded_string(key, label + '.metadata key', MAX_METADATA_KEY_BYTES)
            _bounded_string(value, label + '.metadata.' + key, MAX_METADATA_VALUE_BYTES)
    return document


def _json_for_script(document):
    return json.dumps(document, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def render_html(document, script):
    # The page always begins with an empty review.  Keep every other candidate
    # value intact so its downloaded document remains acceptable to the batch
    # preparer.
    review = json.loads(json.dumps(document, ensure_ascii=False))
    for candidate in review['candidates']:
        candidate['selected'] = False
    data = _json_for_script(review)
    return """<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\">
<title>ResearchKit 数据集审核</title>
<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}input{margin:.25rem}button{margin:.4rem}.filters{display:flex;flex-wrap:wrap;gap:.5rem}article{border:1px solid #bbb;padding:1rem;margin:1rem 0}h2{display:inline;margin-left:.6rem;font-size:1.1rem}details p{white-space:pre-wrap}#notice{min-height:1.5em}</style>
</head>
<body>
<h1>ResearchKit 数据集候选审核</h1>
<p>此页面完全离线运行；下载的 JSON 可交给 <code>prepare-web-dataset-trees.py</code>。</p>
<p>这里显示的是 Wiki 说明快照。卡片关联成功不代表许可已经审核；空白说明请先确认再选。勾选和下载不会启用数据集。关闭或刷新页面会丢失未下载的选择。</p>
<div class=\"filters\"><label>名称 <input id=\"name-filter\" type=\"search\"></label><label>路径 <input id=\"path-filter\" type=\"search\"></label><label>Wiki 索引 <input id=\"wiki-filter\" type=\"search\"></label></div>
<p><button id=\"select-visible\" type=\"button\">选中当前筛选项</button><button id=\"clear-selected\" type=\"button\">取消选中</button><button id=\"download\" type=\"button\">下载审核 JSON</button></p>
<p id=\"count\"></p><p id=\"notice\" aria-live=\"polite\"></p><main id=\"cards\"></main>
<script id=\"candidate-data\" type=\"application/json\">%s</script>
<script>%s</script>
</body>
</html>
""" % (data, script)


def write_review_page(candidates, output):
    document = validate_candidates(read_candidates(candidates))
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ReviewError('输出文件已存在，不会覆盖')
    if not output.parent.is_dir():
        raise ReviewError('输出目录不存在')
    script = Path(__file__).with_name('dataset-review.js').read_text(encoding='utf-8')
    payload = render_html(document, script)
    try:
        descriptor = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ReviewError('输出文件已存在，不会覆盖') from exc
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(payload)
    except Exception:
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_review_page(args.candidates, args.output)
    except (ReviewError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
