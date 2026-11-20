"""Bounded, inert previews/downloads of project or immutable-submission results."""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET

from .files import KitError

PREVIEW_LIMIT = 2 * 1024 * 1024
DOWNLOAD_LIMIT = 64 * 1024 * 1024


def result_path(value):
    if (not isinstance(value, str) or not value.startswith('results/') or
            '\\' in value or '\x00' in value or len(value) > 1024 or
            any(part in {'', '.', '..'} for part in value.split('/'))):
        raise KitError('只允许查看项目 results/ 中的产物文件')
    return value


def project_file(workspace, project_id, path, *, download=False):
    path = result_path(path)
    with workspace._project_guard(project_id):
        workspace._broker_project(project_id)
        descriptor = workspace._open_project_fd(project_id)
        try:
            raw = workspace._read_regular(descriptor, path,
                                          limit=DOWNLOAD_LIMIT if download else PREVIEW_LIMIT)
        finally:
            os.close(descriptor)
    return presentation(path, raw, download=download)


def submission_file(workspace, submission_id, path, *, review=False, download=False):
    path = result_path(path)
    if review and workspace.principal.role != 'teacher':
        raise KitError('只有教师可以审核其他账号的固定提交')
    record = workspace.store.request('review_submission' if review else 'get_submission',
                                     submission_id=submission_id)
    raw = record.get('files', {}).get(path)
    if not isinstance(raw, bytes):
        raise KitError('固定提交中没有该产物')
    return presentation(path, raw, download=download)


_TAGS = {'svg', 'g', 'path', 'polyline', 'polygon', 'line', 'rect', 'circle',
         'ellipse', 'text', 'tspan', 'defs', 'clipPath', 'title', 'desc'}
_ATTRS = {'width', 'height', 'viewBox', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
          'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points', 'fill', 'stroke',
          'stroke-width', 'stroke-dasharray', 'opacity', 'fill-opacity',
          'stroke-opacity', 'font-size', 'font-family', 'font-weight',
          'text-anchor', 'dominant-baseline', 'transform', 'id'}


def inert_svg(raw):
    if len(raw) > PREVIEW_LIMIT or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I):
        raise KitError('SVG 说明过大或包含不支持的文档声明，请下载后自行查看')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise KitError('SVG 格式无效') from exc
    if root.tag.split('}')[-1] != 'svg':
        raise KitError('SVG 根元素无效')
    count = 0

    def clean(node, depth=0):
        nonlocal count
        if depth > 64:
            raise KitError('SVG 层级过深，请下载后自行查看')
        count += 1
        if count > 20000:
            raise KitError('SVG 元素过多，请下载后自行查看')
        local = node.tag.split('}')[-1] if isinstance(node.tag, str) else ''
        result = ET.Element('{http://www.w3.org/2000/svg}' + local)
        for key, value in node.attrib.items():
            # No scripts, URLs, styles, links, animation or external resources.
            if key not in _ATTRS or len(value) > 200000:
                continue
            if re.search(r'(?:url\s*\(|javascript:|data:|https?:|[<>])', value, re.I):
                continue
            result.set(key, value)
        result.text = node.text
        result.tail = node.tail
        for child in node:
            child_tag = child.tag.split('}')[-1] if isinstance(child.tag, str) else ''
            if child_tag in _TAGS:
                result.append(clean(child, depth + 1))
        return result

    return ET.tostring(clean(root), encoding='utf-8', xml_declaration=True)


def presentation(path, raw, *, download=False):
    if len(raw) > (DOWNLOAD_LIMIT if download else PREVIEW_LIMIT):
        raise KitError('产物超过网页查看上限，请在 IDE 查看或保存为派生数据')
    name = PurePosixPath(path).name
    if download:
        return raw, 'application/octet-stream', name
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == '.svg':
        return inert_svg(raw), 'image/svg+xml', name
    for extension, media, prefix in (
        ('.png', 'image/png', b'\x89PNG\r\n\x1a\n'),
        ('.jpg', 'image/jpeg', b'\xff\xd8\xff'),
        ('.jpeg', 'image/jpeg', b'\xff\xd8\xff'),
        ('.gif', 'image/gif', b'GIF8'),
    ):
        if suffix == extension and raw.startswith(prefix):
            return raw, media, name
    if suffix in {'.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.log'}:
        try:
            raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise KitError('产物不是 UTF-8 文本，请下载查看') from exc
        # Even HTML-looking text is delivered as inert plain text.
        return raw, 'text/plain; charset=utf-8', name
    raise KitError('此格式仅支持下载，在 IDE 中打开')
