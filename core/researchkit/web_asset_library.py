"""Central Web view of built-in and teacher-published reusable resources."""
from __future__ import annotations

from pathlib import Path

from .files import KitError
from .web_assets import AssetService
from .web_central_library import CentralLibrary
from .web_library import SharedLibrary, revision, _TYPES


class AssetLibrary(SharedLibrary):
    """Serve only immutable database copies after the initial trusted import.

    ``SharedLibrary`` is inherited for its small, inert YAML/file validators.
    None of the source-reading methods are used to resolve Web resources after
    :class:`CentralLibrary` has completed its first import.
    """

    def __init__(self, store, root: Path | str):
        super().__init__(root)
        self.assets = AssetService(store, root)
        self.central = CentralLibrary(store, root)

    def _asset_files(self, kind: str, ref: str) -> dict[str, bytes] | None:
        builtin = self.central.asset_files(kind, ref)
        published = self.assets.published_files(kind, ref)
        if builtin is None:
            return published
        if published is None:
            return builtin
        if builtin != published:
            raise KitError('中心内置资源与 Web 发布资源使用相同版本但内容不同')
        # Byte-identical old publications are accepted as the same immutable
        # resource.  The builtin record is its canonical Web source.
        return builtin

    def _plugin_source(self, ref):
        files = self._asset_files('algorithm', ref)
        if files is None:
            raise KitError(f'共享算法不存在：{ref}')
        plugin = {path[7:]: value for path, value in files.items() if path.startswith('plugin/')}
        if not plugin:
            raise KitError(f'共享算法存储已损坏：{ref}')
        return self._plugin_from_files(plugin, ref), plugin, revision(plugin)

    def _template_source(self, ref):
        files = self._asset_files('template', ref)
        if files is None:
            raise KitError(f'共享模板不存在：{ref}')
        return self._template_from_files(files, ref), files, revision(files)

    def _contract_source(self, ref):
        builtin = self.central.contract(ref)
        published = self.assets.published_contracts().get(ref)
        if builtin is None and published is None:
            raise KitError(f'共享契约不存在：{ref}')
        if builtin is not None:
            document, data = builtin
            if published is not None:
                published_document = self._safe_yaml(published, '已发布契约')
                # YAML serialization is not the contract identity.  Older
                # published candidates may format an otherwise identical
                # definition differently, and must remain usable after the
                # builtin import.
                if published_document != document:
                    raise KitError('中心内置契约与 Web 发布契约定义冲突')
        else:
            data = published
            document = self._safe_yaml(data, '已发布契约')
        if (document.get('schema') != 'hydro.contract/v1' or document.get('id') != ref
                or document.get('type') not in _TYPES or not isinstance(document.get('description', ''), str)):
            raise KitError('已发布契约身份或类型无效')
        return document, data

    def catalog(self):
        records = self.central.records()
        catalog = {'plugins': [], 'contracts': [], 'templates': []}
        known: set[tuple[str, str]] = set()
        for asset in records:
            kind, ref = str(asset['kind']), str(asset['ref'])
            known.add((kind, ref))
            if kind == 'algorithm':
                meta = asset['metadata']
                if not all(isinstance(meta.get(key), dict) for key in ('inputs', 'outputs', 'parameters')):
                    raise KitError('中心内置算法元数据已损坏')
                catalog['plugins'].append({'ref': ref, 'name': asset['name'], 'version': asset['version'],
                                           'inputs': meta['inputs'], 'outputs': meta['outputs'],
                                           'parameters': meta['parameters']})
            else:
                catalog['templates'].append({'ref': ref, 'name': asset['name'], 'version': asset['version']})

        contracts: dict[str, dict[str, object]] = {}
        for ref in self.central.contracts():
            document, _data = self._contract_source(ref)
            contracts[ref] = {'id': ref, 'type': document['type'], 'description': document.get('description', '')}

        for asset in self.assets.published_records():
            kind, ref = asset['kind'], asset['ref']
            if (kind, ref) in known:
                # Verify the documented byte-identical coexistence rule now,
                # rather than silently allowing metadata to choose a source.
                self._asset_files(kind, ref)
                continue
            known.add((kind, ref))
            if kind == 'algorithm':
                meta = asset['metadata']
                catalog['plugins'].append({'ref': ref, 'name': asset['name'], 'version': asset['version'],
                                           'inputs': meta['inputs'], 'outputs': meta['outputs'],
                                           'parameters': meta['parameters']})
            else:
                catalog['templates'].append({'ref': ref, 'name': asset['name'], 'version': asset['version']})
        for ref, data in self.assets.published_contracts().items():
            document = self._safe_yaml(data, '已发布契约')
            row = {'id': ref, 'type': document.get('type'), 'description': document.get('description', '')}
            known_contract = contracts.get(ref)
            if known_contract is not None and known_contract != row:
                raise KitError('中心内置契约与 Web 发布契约定义冲突')
            contracts[ref] = row
        catalog['contracts'] = [contracts[key] for key in sorted(contracts)]
        meta_map = self.assets.metadata_map()
        defaults = {
            ('template', 'hello@1.0.0'): {'description': '基础示范案例模板', 'tags': ['示例']},
            ('algorithm', 'example.scale@1.0.0'): {'description': '示例缩放算法', 'tags': ['示例']},
        }
        for kind_key, kind_name in (('plugins', 'algorithm'), ('templates', 'template')):
            for row in catalog[kind_key]:
                ref = str(row['ref'])
                meta = meta_map.get((kind_name, ref))
                default_meta = defaults.get((kind_name, ref), {})
                row['description'] = meta['description'] if meta is not None else default_meta.get('description', '')
                row['tags'] = meta['tags'] if meta is not None else list(default_meta.get('tags', []))
                row['family_id'] = meta['family_id'] if meta is not None else f'{kind_name}:{ref}'
                row['parent_ref'] = meta['parent_ref'] if meta is not None else None
                if meta is not None and meta.get('display_name'):
                    row['display_name'] = meta['display_name']
        for key in ('plugins', 'templates'):
            catalog[key].sort(key=lambda row: row['ref'])
        return catalog
