"""无额外数值库依赖的TDOA教学定位：用时差和阵列坐标解算声源位置。"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from zipfile import ZipFile


_FILES = ('hydrophones.csv', 'tdoa_measurements.csv', 'true_trajectory.csv')


def _config(context):
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
    path = (Path(context.case_dir) / root / filename).resolve()
    if Path(context.case_dir).resolve() not in path.parents:
        raise ValueError('本地数据路径必须位于项目目录内')
    return path


def _read_tables(context):
    # 支持已授权目录或托管ZIP，也支持项目内合成数据；所有输入只读。
    source, dataset_id, root, data_format = _config(context)
    tables = {}
    if source == 'dataset':
        if not dataset_id:
            raise ValueError('dataset 模式必须填写 dataset_id')
        if data_format == 'directory':
            for name in _FILES:
                relative = '/'.join(part for part in (root.strip('/'), name) if part)
                with context.open_dataset(dataset_id, relative, mode='r') as stream:
                    tables[name] = list(csv.DictReader(stream))
        else:
            with context.open_dataset(dataset_id, 'data.bin', mode='rb') as stream:
                raw = stream.read()
            with ZipFile(io.BytesIO(raw)) as archive:
                for name in _FILES:
                    with archive.open(name) as entry:
                        tables[name] = list(csv.DictReader(io.TextIOWrapper(entry, encoding='utf-8')))
        return tables
    if data_format == 'directory':
        for name in _FILES:
            with _local_path(context, root, name).open(encoding='utf-8', newline='') as stream:
                tables[name] = list(csv.DictReader(stream))
    else:
        archive_path = _local_path(context, root, '')
        if archive_path.suffix != '.zip':
            archive_path = archive_path.with_suffix('.zip')
        with ZipFile(archive_path) as archive:
            for name in _FILES:
                with archive.open(name) as entry:
                    tables[name] = list(csv.DictReader(io.TextIOWrapper(entry, encoding='utf-8')))
    return tables


def _finite(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} 必须是数值') from exc
    if not math.isfinite(result):
        raise ValueError(f'{label} 必须是有限数值')
    return result


def _solve(matrix, vector):
    """用部分选主元的消元法解小型方程组；退化阵列或方程直接报错。"""
    size = len(vector)
    if len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError('定位方程维度错误')
    augmented = [list(map(float, row)) + [float(value)] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            raise ValueError('水听器阵列或时差方程退化，无法定位')
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        factor = augmented[column][column]
        augmented[column] = [value / factor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            scale = augmented[row][column]
            augmented[row] = [a - scale * b for a, b in zip(augmented[row], augmented[column])]
    answer = [row[-1] for row in augmented]
    if not all(math.isfinite(value) for value in answer):
        raise ValueError('定位解包含非有限值')
    return answer


def _estimate(receivers, tdoa_row, sound_speed):
    # 以0号水听器为参考，时差乘以方案声速，转换为传播距离差。
    reference = receivers[0]
    matrix, vector = [], []
    for index, receiver in enumerate(receivers[1:], start=1):
        seconds = _finite(tdoa_row[f'tdoa_to_ref_s_{index}'], f'第 {index} 个时差')
        distance_difference = seconds * sound_speed
        row = [2.0 * (receiver[axis] - reference[axis]) for axis in range(3)]
        row.append(2.0 * distance_difference)
        right = (sum(value * value for value in receiver) - sum(value * value for value in reference)
                 - distance_difference * distance_difference)
        matrix.append(row); vector.append(right)
    # 四个未知量为x、y、z及到参考水听器的距离；真值没有进入方程。
    x, y, z, _reference_range = _solve(matrix, vector)
    return x, y, z


def _axis_ticks(low, high, count=4):
    span = max(1.0, high - low)
    margin = span * 0.06
    low -= margin
    high += margin
    return low, high, [low + index * (high - low) / count for index in range(count + 1)]


def _trajectory_svg(rows):
    # 比较XY平面的投影，须结合坐标刻度和误差指标判断，不能只看曲线外形。
    width, height, pad = 700, 360, 62
    xs = [value for row in rows for value in (row['x_m'], row['true_x_m'])]
    ys = [value for row in rows for value in (row['y_m'], row['true_y_m'])]
    x_low, x_high, x_ticks = _axis_ticks(min(xs), max(xs))
    y_low, y_high, y_ticks = _axis_ticks(min(ys), max(ys))
    def point(x, y):
        return (pad + (x - x_low) / (x_high - x_low) * (width - 2 * pad),
                height - pad - (y - y_low) / (y_high - y_low) * (height - 2 * pad))
    def polyline(keys):
        return ' '.join(f'{point(row[keys[0]], row[keys[1]])[0]:.1f},{point(row[keys[0]], row[keys[1]])[1]:.1f}' for row in rows)
    x_labels = ''.join(f'<text x="{point(value, y_low)[0]:.1f}" y="{height - pad + 18}" text-anchor="middle" font-size="11">{value:.0f}</text>'
                       for value in x_ticks)
    y_labels = ''.join(f'<text x="{pad - 8}" y="{point(x_low, value)[1] + 4:.1f}" text-anchor="end" font-size="11">{value:.0f}</text>'
                       for value in y_ticks)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><path d="M {pad} {pad} V {height-pad} H {width-pad}" stroke="#274c77" fill="none"/>
<polyline points="{polyline(('true_x_m', 'true_y_m'))}" fill="none" stroke="#3b7a57" stroke-width="3"/><polyline points="{polyline(('x_m', 'y_m'))}" fill="none" stroke="#c95d20" stroke-width="3" stroke-dasharray="6 3"/>
{x_labels}{y_labels}<text x="{width / 2:.1f}" y="{height - 5}" text-anchor="middle" font-size="13">x (m)</text><text x="16" y="{height / 2:.1f}" text-anchor="middle" font-size="13" transform="rotate(-90 16 {height / 2:.1f})">y (m)</text>
<text x="{pad}" y="22" font-size="16">XY projection: green=true, orange=estimated</text></svg>'''


def _error_svg(rows):
    width, height, pad = 700, 260, 55
    maximum = max(row['error_m'] for row in rows) or 1.0
    step = (width - 2 * pad) / max(1, len(rows) - 1)
    points = ' '.join(f'{pad + i * step:.1f},{height-pad-row["error_m"] / maximum * (height-2*pad):.1f}' for i, row in enumerate(rows))
    x_labels = ''.join(f'<text x="{pad + i * step:.1f}" y="{height - pad + 18}" text-anchor="middle" font-size="10">{row["ping_id"]}</text>'
                       for i, row in enumerate(rows))
    y_labels = ''.join(f'<text x="{pad - 8}" y="{height - pad - value / maximum * (height - 2 * pad) + 4:.1f}" text-anchor="end" font-size="11">{value:.2f}</text>'
                       for value in (0.0, maximum / 2, maximum))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><path d="M {pad} {pad} V {height-pad} H {width-pad}" stroke="#274c77" fill="none"/><polyline points="{points}" fill="none" stroke="#a23b72" stroke-width="3"/>{x_labels}{y_labels}<text x="{width / 2:.1f}" y="{height - 5}" text-anchor="middle" font-size="13">ping</text><text x="15" y="{height / 2:.1f}" text-anchor="middle" font-size="13" transform="rotate(-90 15 {height / 2:.1f})">error (m)</text><text x="{pad}" y="22" font-size="16">Position error (m)</text></svg>'''


def _receivers(rows):
    result = {}
    for row in rows:
        value = row.get('receiver_id')
        try:
            receiver_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('hydrophones.csv 的 receiver_id 必须为 0 到 4 的整数') from exc
        if str(receiver_id) != str(value).strip() or receiver_id in result:
            raise ValueError('hydrophones.csv 的 receiver_id 必须唯一且为 0 到 4')
        result[receiver_id] = (_finite(row.get('x_m'), '水听器 x'),
                               _finite(row.get('y_m'), '水听器 y'),
                               _finite(row.get('z_m'), '水听器 z'))
    if set(result) != set(range(5)):
        raise ValueError('hydrophones.csv 必须包含唯一的 receiver_id 0 到 4')
    return [result[index] for index in range(5)]


def _truth(rows):
    result = {}
    for row in rows:
        ping_id = row.get('ping_id', '').strip()
        if not ping_id or ping_id in result:
            raise ValueError('true_trajectory.csv 的 ping_id 必须非空且唯一')
        result[ping_id] = (_finite(row.get('x_m'), '真值 x'), _finite(row.get('y_m'), '真值 y'),
                           _finite(row.get('z_m'), '真值 z'))
    return result


def run(inputs, parameters, context):
    if inputs != {}:
        raise ValueError('locator 不接受输入端口')
    # 两套实验共用同一份观测，只改变这里收到的声速参数。
    sound_speed = _finite(parameters.get('sound_speed_m_s'), 'sound_speed_m_s')
    if sound_speed <= 0:
        raise ValueError('sound_speed_m_s 必须大于零')
    tables = _read_tables(context)
    receivers = _receivers(tables['hydrophones.csv'])
    truth = _truth(tables['true_trajectory.csv'])
    rows = []
    seen_pings = set()
    for measurement in tables['tdoa_measurements.csv']:
        ping_id = measurement.get('ping_id', '').strip()
        if not ping_id or ping_id in seen_pings:
            raise ValueError('tdoa_measurements.csv 的 ping_id 必须非空且唯一')
        seen_pings.add(ping_id)
        if ping_id not in truth:
            raise ValueError(f'测量 {ping_id} 缺少真值行')
        x, y, z = _estimate(receivers, measurement, sound_speed)
        # 先完成定位，再取真值计算误差；真值只参与评估和绘图。
        tx, ty, tz = truth[ping_id]
        error = math.dist((x, y, z), (tx, ty, tz))
        rows.append({'ping_id': ping_id, 'x_m': x, 'y_m': y, 'z_m': z, 'true_x_m': tx, 'true_y_m': ty,
                     'true_z_m': tz, 'error_m': error})
    if not rows:
        raise ValueError('tdoa_measurements.csv 不可为空')
    if set(truth) != seen_pings:
        raise ValueError('测量记录与真值记录的 ping_id 必须一一对应')
    # RMSE是三维位置误差的均方根，max_error_m是最坏点的误差，单位均为米。
    summary = {'count': len(rows), 'rmse_m': math.sqrt(sum(row['error_m'] ** 2 for row in rows) / len(rows)),
               'max_error_m': max(row['error_m'] for row in rows), 'sound_speed_m_s': sound_speed}
    output = io.StringIO()
    fields = ['ping_id', 'x_m', 'y_m', 'z_m', 'true_x_m', 'true_y_m', 'true_z_m', 'error_m']
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    # 输出定位坐标、轨迹图与误差图，供网页审核；输入观测和真值表不变。
    context.atomic_write_text(context.result_path('localized_trajectory.csv'), output.getvalue())
    context.atomic_write_text(context.result_path('trajectory.svg'), _trajectory_svg(rows))
    context.atomic_write_text(context.result_path('position_error.svg'), _error_svg(rows))
    return {'summary': summary}
