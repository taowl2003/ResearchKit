"""读取只读降雨记录，按方案倍率缩放，另存处理结果与图表。"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from zipfile import ZipFile


def _parameters(context):
    # 案例参数描述数据来源；它们与下面的算法倍率参数分开管理。
    values = context.parameters
    source = values.get('data_source', 'local')
    dataset_id = values.get('dataset_id', '')
    root = values.get('data_root', 'sample-data')
    data_format = values.get('data_format', 'directory')
    if source not in {'local', 'dataset'}:
        raise ValueError('data_source 必须是 local 或 dataset')
    if data_format not in {'directory', 'zip'}:
        raise ValueError('data_format 必须是 directory 或 zip')
    if not isinstance(dataset_id, str) or not isinstance(root, str):
        raise ValueError('dataset_id 和 data_root 必须是字符串')
    return source, dataset_id, root, data_format


def _local_path(context, root: str, filename: str) -> Path:
    candidate = (Path(context.case_dir) / root / filename).resolve()
    case_dir = Path(context.case_dir).resolve()
    if case_dir not in candidate.parents:
        raise ValueError('本地数据路径必须位于项目目录内')
    return candidate


def _read_rows(context):
    source, dataset_id, root, data_format = _parameters(context)
    # 单文件托管版本使用 data.bin；原始目录数据保留 rainfall.csv 等文件名。
    # open_dataset 按已加入项目的准确版本只读访问，不复制或修改源数据。
    filename = context.parameters.get('data_file', 'rainfall.csv')
    if (not isinstance(filename, str) or not filename or
            Path(filename).is_absolute() or '..' in Path(filename).parts):
        raise ValueError('data_file 必须是数据集内的相对文件路径')
    if source == 'dataset':
        if not dataset_id:
            raise ValueError('dataset 模式必须填写 dataset_id')
        if data_format == 'directory':
            relative = '/'.join(part for part in (root.strip('/'), filename) if part)
            with context.open_dataset(dataset_id, relative, mode='r') as stream:
                return list(csv.DictReader(stream))
        with context.open_dataset(dataset_id, 'data.bin', mode='rb') as stream:
            with ZipFile(io.BytesIO(stream.read())) as archive:
                with archive.open(filename) as raw:
                    return list(csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8')))

    if data_format == 'directory':
        with _local_path(context, root, filename).open(encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream))
    with ZipFile(_local_path(context, root, '').with_suffix('.zip')) as archive:
        with archive.open(filename) as raw:
            return list(csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8')))


def _svg(rows):
    # 生成无需额外绘图库的 SVG；坐标单位与数据中的毫米一致。
    values = [row['scaled_rainfall_mm'] for row in rows]
    maximum = max(values) if values else 1.0
    width, height, pad = 640, 260, 42
    step = (width - 2 * pad) / max(1, len(values) - 1)
    points = []
    for index, value in enumerate(values):
        x = pad + index * step
        y = height - pad - (value / maximum) * (height - 2 * pad)
        points.append(f'{x:.1f},{y:.1f}')
    labels = ''.join(f'<text x="{pad + index * step:.1f}" y="{height - pad + 18}" text-anchor="middle" font-size="11">{row["day"]}</text>'
                     for index, row in enumerate(rows))
    y_labels = ''.join(f'<text x="{pad - 8}" y="{height - pad - value / maximum * (height - 2 * pad) + 4:.1f}" text-anchor="end" font-size="11">{value:.1f}</text>'
                       for value in (0.0, maximum / 2, maximum))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><path d="M {pad} {pad} V {height-pad} H {width-pad}" stroke="#274c77" fill="none"/>
<polyline points="{' '.join(points)}" fill="none" stroke="#1d70a2" stroke-width="3"/>{labels}{y_labels}
<text x="{width / 2:.1f}" y="{height - 2}" text-anchor="middle" font-size="12">day</text><text x="14" y="{height / 2:.1f}" text-anchor="middle" font-size="12" transform="rotate(-90 14 {height / 2:.1f})">rainfall (mm)</text><text x="{pad}" y="24" font-size="16">Processed rainfall (mm)</text></svg>'''


def run(inputs, parameters, context):
    if inputs != {}:
        raise ValueError('scale 不接受输入端口')
    # parameters 是算法参数：默认值、项目值、方案值已经按顺序合并。
    factor = parameters['factor']
    if not isinstance(factor, (int, float)) or isinstance(factor, bool) or not math.isfinite(factor):
        raise ValueError('factor 必须是有限数值')
    rows = []
    for row in _read_rows(context):
        day = row.get('day', '')
        try:
            rainfall = float(row['rainfall_mm'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('rainfall.csv 需要 day 和 rainfall_mm 列') from exc
        if not math.isfinite(rainfall):
            raise ValueError('rainfall_mm 必须是有限数值')
        rows.append({'day': day, 'rainfall_mm': rainfall, 'scaled_rainfall_mm': rainfall * factor})
    if not rows:
        raise ValueError('rainfall.csv 不可为空')
    # 原始总量和平均值用于核对；scaled_total 才是倍率处理后的总量。
    raw_total = sum(row['rainfall_mm'] for row in rows)
    summary = {'count': len(rows), 'raw_total': raw_total, 'raw_mean': raw_total / len(rows),
               'scaled_total': sum(row['scaled_rainfall_mm'] for row in rows), 'factor': factor}
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['day', 'rainfall_mm', 'scaled_rainfall_mm'])
    writer.writeheader(); writer.writerows(rows)
    # 产物写入方案结果目录；原 CSV 不变，也不会自动成为平台数据资源。
    context.atomic_write_text(context.result_path('processed_rainfall.csv'), output.getvalue())
    context.atomic_write_text(context.result_path('rainfall_chart.svg'), _svg(rows))
    return {'summary': summary}
