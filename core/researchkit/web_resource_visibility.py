"""Personal list preferences, independent of grants and resource storage.

Removing a row from one's own list must never revoke dataset access, mutate
the central library, or touch copies already bound to a project.
"""
from __future__ import annotations

import json
import os
import re
import stat

from .files import KitError, atomic_text, reference

FILENAME = '.researchkit-web-resource-visibility.json'
SCHEMA = 'researchkit.web-resource-visibility/v1'
MAX_BYTES = 1024 * 1024
MAX_ITEMS = 2000
FIELDS = {'algorithm': 'hidden_algorithms', 'dataset': 'hidden_datasets'}


def _validate_ref(kind: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise KitError('个人资源标识无效')
    if kind == 'algorithm':
        reference(value)
    elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise KitError('个人数据集标识无效')
    return value


def _load(workspace) -> dict[str, list[str]]:
    path = workspace.root / FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        if path.is_symlink():
            raise KitError('个人资源显示设置不安全')
        return {field: [] for field in FIELDS.values()}
    except OSError as exc:
        raise KitError('个人资源显示设置不安全或无法读取') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or
                info.st_size > MAX_BYTES):
            raise KitError('个人资源显示设置不安全')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise KitError('个人资源显示设置过大')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise KitError('个人资源显示设置已损坏') from exc
        if (type(payload) is not dict or
                set(payload) != {'schema', 'uid', 'username', *FIELDS.values()} or
                payload['schema'] != SCHEMA or type(payload['uid']) is not int or
                payload['uid'] != workspace.principal.uid or
                payload['username'] != workspace.principal.username):
            raise KitError('个人资源显示设置格式或所属账号无效')
        result = {}
        for kind, field in FIELDS.items():
            values = payload[field]
            if type(values) is not list or len(values) > MAX_ITEMS:
                raise KitError('个人资源显示设置列表无效')
            values = [_validate_ref(kind, value) for value in values]
            if len(set(values)) != len(values):
                raise KitError('个人资源显示设置存在重复标识')
            result[field] = sorted(values)
        return result
    finally:
        os.close(fd)


def get(workspace) -> dict[str, list[str]]:
    with workspace._state_lock():
        return _load(workspace)


def update(workspace, payload: object) -> dict[str, list[str]]:
    if (type(payload) is not dict or set(payload) != {'kind', 'ref', 'hidden'} or
            type(payload['kind']) is not str or payload['kind'] not in FIELDS or
            type(payload['hidden']) is not bool):
        raise KitError('个人资源显示请求必须包含 kind、ref 和 hidden')
    kind, hidden = payload['kind'], payload['hidden']
    ref = _validate_ref(kind, payload['ref'])
    with workspace._state_lock():
        values = _load(workspace)
        selected = set(values[FIELDS[kind]])
        if hidden:
            selected.add(ref)
        else:
            selected.discard(ref)
        if len(selected) > MAX_ITEMS:
            raise KitError('个人资源显示设置条目过多')
        values[FIELDS[kind]] = sorted(selected)
        stored = {'schema': SCHEMA, 'uid': workspace.principal.uid,
                  'username': workspace.principal.username, **values}
        raw = json.dumps(stored, ensure_ascii=False, sort_keys=True) + '\n'
        if len(raw.encode('utf-8')) > MAX_BYTES:
            raise KitError('个人资源显示设置过大')
        path = workspace.root / FILENAME
        atomic_text(path, raw)
        os.chmod(path, 0o600)
        return values
