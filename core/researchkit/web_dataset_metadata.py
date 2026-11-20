"""Optional, root-reviewed dataset descriptions; never an authorization source."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from .files import KitError
from .web_datasets import DatasetRegistry

SCHEMA = 'researchkit.web-dataset-metadata/v1'
_FIELDS = {'name', 'description', 'wiki_ref', 'wiki_fact_state', 'provenance_ref',
           'format', 'usage', 'license', 'student_conditions', 'tags'}
MAX_TAGS = 32
MAX_TAG_LENGTH = 64


def normalize_tags(value: object, *, label: str = '数据集标签') -> list[str]:
    """Validate and canonicalize the one structured metadata field.

    Tags are display metadata only.  Keeping this normalization here means
    catalog files, README discovery and queue filtering agree on equality.
    """
    if type(value) is not list or len(value) > MAX_TAGS:
        raise KitError(f'{label}必须是最多{MAX_TAGS}个字符串')
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise KitError(f'{label}必须是字符串列表')
        tag = unicodedata.normalize('NFKC', item).strip()
        if (not tag or len(tag) > MAX_TAG_LENGTH or len(tag.encode('utf-8')) > 256 or
                any(character in '\r\n\x00' for character in tag)):
            raise KitError(f'{label}包含空白、过长或无效标签')
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def enrich_catalog(catalog: list[dict[str, object]], path: Path | None) -> list[dict[str, object]]:
    if path is None:
        return catalog
    # Use the registry's trusted, bounded file reader, not arbitrary Wiki paths.
    reader = DatasetRegistry(path)
    try:
        payload = json.loads(reader._read_bytes())
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise KitError('数据集说明不是有效 JSON') from exc
    if (type(payload) is not dict or set(payload) != {'schema', 'datasets'} or
            payload['schema'] != SCHEMA or type(payload['datasets']) is not dict):
        raise KitError('数据集说明格式无效')
    metadata = payload['datasets']
    if len(metadata) > 4096:
        raise KitError('数据集说明数量过多')
    for key, value in metadata.items():
        if not isinstance(key, str) or type(value) is not dict or set(value) - _FIELDS:
            raise KitError('数据集说明字段无效')
        for field, text in value.items():
            if field == 'tags':
                value[field] = normalize_tags(text)
            elif not isinstance(text, str) or len(text.encode('utf-8')) > 16384:
                raise KitError('数据集说明字段无效')
    # Metadata must not overwrite files, identities, visibility or references.
    return [{**item, **metadata.get(str(item['dataset_id']), {})} for item in catalog]
