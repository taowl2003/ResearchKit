#!/usr/bin/env python3
"""Refresh editor snippets in the tutorial from the canonical case source."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ('rainfall', 'scale'),
    ('mnist-cnn', 'cnn'),
)


def _replace(guide: str, key: str, language: str, content: str) -> str:
    pattern = (rf'(<!-- BEGIN TUTORIAL: {key} -->\n).*?'
               rf'(\n<!-- END TUTORIAL: {key} -->)')
    replacement = f'```{language}\n{content.rstrip()}\n```'
    guide, count = re.subn(pattern, lambda match: match[1] + replacement + match[2], guide, flags=re.S)
    if count != 1:
        raise ValueError(f'Expected exactly one tutorial marker: {key}')
    return guide

def outputs() -> dict[Path, str]:
    guide_path = ROOT / 'docs/学生使用指南.md'
    guide = guide_path.read_text()
    for key, relative in {
        'framework-entry': 'rainfall/case.py',
        'acoustic-modular-entry': 'acoustic-modular/case.py',
    }.items():
        source = (ROOT / 'examples/three-cases' / relative).read_text().rstrip()
        guide = _replace(guide, key, 'python', source)
    for case, alias in CASES:
        source = ROOT / 'examples/three-cases' / case / 'algorithms' / alias
        meta = yaml.safe_load((source / 'plugin.yaml').read_text())
        guide = _replace(guide, f'{case}-plugin', 'python', (source / 'plugin.py').read_text())
        for field in ('parameters', 'inputs', 'outputs'):
            content = json.dumps(meta[field], ensure_ascii=False, indent=2)
            guide = _replace(guide, f'{case}-{field}', 'json', content)
    for alias in ('sim', 'doa', 'locator'):
        source = ROOT / 'examples/three-cases/acoustic-modular/algorithms' / alias
        meta = yaml.safe_load((source / 'plugin.yaml').read_text())
        key = f'acoustic-modular-{alias}'
        guide = _replace(guide, key + '-plugin', 'python', (source / 'plugin.py').read_text())
        guide = _replace(guide, key + '-parameters', 'json', json.dumps(meta['parameters'], ensure_ascii=False, indent=2))
    return {guide_path: guide}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for path, value in outputs().items():
        if args.check:
            if not path.is_file() or path.read_text() != value:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(value)
    if stale:
        parser.exit(1, 'Tutorial output needs regeneration: ' + ', '.join(stale) + '\n')

if __name__ == '__main__':
    main()
