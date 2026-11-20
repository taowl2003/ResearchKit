"""三维水声定位解算与评估算子：根据时差方程求解声源三维坐标，并评估定位误差。"""
from __future__ import annotations
import csv
import io
import math

def _solve(matrix, vector):
    size = len(vector)
    augmented = [list(map(float, row)) + [float(val)] for row, val in zip(matrix, vector)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            raise ValueError('阵列或时差方程退化，无法定位')
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        factor = augmented[col][col]
        augmented[col] = [v / factor for v in augmented[col]]
        for row in range(size):
            if row != col:
                scale = augmented[row][col]
                augmented[row] = [a - scale * b for a, b in zip(augmented[row], augmented[col])]
    return [row[-1] for row in augmented]

def _estimate(receivers, tdoa_list, sound_speed):
    ref = receivers[0]
    matrix, vector = [], []
    for idx, recv in enumerate(receivers[1:]):
        dist_diff = tdoa_list[idx] * sound_speed
        row = [2.0 * (recv[ax] - ref[ax]) for ax in range(3)] + [2.0 * dist_diff]
        right = sum(v*v for v in recv) - sum(v*v for v in ref) - dist_diff*dist_diff
        matrix.append(row); vector.append(right)
    # 四个未知量为三维坐标和参考接收点距离，至少需要本例的五个接收点。
    x, y, z, _ = _solve(matrix, vector)
    return x, y, z

def run(inputs: dict, parameters: dict, context) -> dict:
    if 'bearing_data' not in inputs:
        raise ValueError('locator 必须接收 bearing_data 输入端口')
    data = inputs['bearing_data']
    sound_speed = float(parameters.get('sound_speed_m_s', 1500.0))
    truth_map = {t['ping_id']: (t['x_m'], t['y_m'], t['z_m']) for t in data['truth']}
    rows = []
    for item in data['measurements']:
        tx, ty, tz = truth_map[item['ping_id']]
        x, y, z = _estimate(data['receivers'], item['tdoa'], sound_speed)
        err = math.dist((x, y, z), (tx, ty, tz))
        rows.append({'ping_id': item['ping_id'], 'x_m': x, 'y_m': y, 'z_m': z,
                     'true_x_m': tx, 'true_y_m': ty, 'true_z_m': tz, 'error_m': err})

    # 使用已知仿真真值评估误差；实际无真值数据不能直接套用此指标。
    rmse = math.sqrt(sum(r['error_m']**2 for r in rows) / len(rows))
    summary = {'count': len(rows), 'rmse_m': rmse, 'max_error_m': max(r['error_m'] for r in rows), 'sound_speed_m_s': sound_speed}

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['ping_id', 'x_m', 'y_m', 'z_m', 'true_x_m', 'true_y_m', 'true_z_m', 'error_m'])
    writer.writeheader(); writer.writerows(rows)
    context.atomic_write_text(context.result_path('localized_trajectory.csv'), output.getvalue())
    return {'summary': summary}
