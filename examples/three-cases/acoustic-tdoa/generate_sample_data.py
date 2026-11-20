"""在当前项目内生成固定种子的合成水声观测，并打包为可托管的ZIP。

仅为教学模拟，不代表真实海试精度；不读写共享原始数据目录。
"""
from __future__ import annotations

import csv
import math
import random
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent / 'sample-data'
SOUND_SPEED = 1500.0
RANDOM_SEED = 2026
RECEIVERS = [(0.0, 0.0, 0.0), (500.0, 0.0, 10.0), (0.0, 500.0, -15.0), (-450.0, 100.0, 20.0), (100.0, -450.0, -10.0)]


def _trajectory():
    """固定随机种子生成十个声源位置，便于两套方案复用完全相同的观测。"""
    generator = random.Random(RANDOM_SEED)
    return [(generator.uniform(-220.0, 260.0), generator.uniform(-210.0, 250.0),
             generator.uniform(45.0, 110.0)) for _ in range(10)]


def _write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def build(root=ROOT):
    root.mkdir(parents=True, exist_ok=True)
    _write_csv(root / 'hydrophones.csv', ['receiver_id', 'x_m', 'y_m', 'z_m'],
               [{'receiver_id': index, 'x_m': x, 'y_m': y, 'z_m': z}
                for index, (x, y, z) in enumerate(RECEIVERS)])
    measurements, truth = [], []
    # 先按几何距离和1500米/秒生成无噪声时差，之后不要为1480方案重生成数据。
    for index, point in enumerate(_trajectory(), start=1):
        ranges = [math.dist(point, receiver) for receiver in RECEIVERS]
        row = {'ping_id': f'P{index:02d}'}
        for receiver_index in range(1, len(RECEIVERS)):
            row[f'tdoa_to_ref_s_{receiver_index}'] = f'{(ranges[receiver_index] - ranges[0]) / SOUND_SPEED:.12f}'
        measurements.append(row)
        # 真值独立保存，只供定位算法完成解算后的误差评估。
        truth.append({'ping_id': f'P{index:02d}', 'x_m': point[0], 'y_m': point[1], 'z_m': point[2]})
    _write_csv(root / 'tdoa_measurements.csv', ['ping_id', 'tdoa_to_ref_s_1', 'tdoa_to_ref_s_2', 'tdoa_to_ref_s_3', 'tdoa_to_ref_s_4'], measurements)
    _write_csv(root / 'true_trajectory.csv', ['ping_id', 'x_m', 'y_m', 'z_m'], truth)
    (root / 'README.md').write_text('# 合成水声 TDOA 定位数据\n\n标签：水声、TDOA、被动定位、教学演示\n\n五个水听器、十个声源位置；观测按 1500 m/s、随机种子 2026 生成。真值仅用于评估，不应进入定位解算。\n', encoding='utf-8')
    # ZIP含三张CSV和README，便于作为单文件保存为个人派生数据。
    zip_path = root.parent / 'sample-data.zip'
    with ZipFile(zip_path, 'w', ZIP_DEFLATED) as archive:
        for name in (*_FILES, 'README.md'):
            archive.write(root / name, name)
    return zip_path


_FILES = ('hydrophones.csv', 'tdoa_measurements.csv', 'true_trajectory.csv')


if __name__ == '__main__':
    print(build())
