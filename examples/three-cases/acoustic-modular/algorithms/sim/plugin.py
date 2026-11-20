"""水声环境与阵列观测仿真算子：生成多阵元几何坐标、目标轨迹真值与到达时差观测。"""
from __future__ import annotations
import math
import random

_DEFAULT_RECEIVERS = [
    (0.0, 0.0, 0.0),
    (500.0, 0.0, 10.0),
    (0.0, 500.0, -15.0),
    (-450.0, 100.0, 20.0),
    (100.0, -450.0, -10.0),
]

def run(inputs: dict, parameters: dict, context) -> dict:
    sound_speed = float(parameters.get('sound_speed_m_s', 1500.0))
    seed = int(parameters.get('seed', 2026))
    ping_count = int(parameters.get('ping_count', 10))
    noise_std = float(parameters.get('noise_std_s', 0.0))

    # 固定随机种子，让两套声速方案使用完全相同的目标和观测。
    rng = random.Random(seed)
    truth_points = [
        (rng.uniform(-220.0, 260.0), rng.uniform(-210.0, 250.0), rng.uniform(45.0, 110.0))
        for _ in range(ping_count)
    ]

    receivers = _DEFAULT_RECEIVERS
    measurements, truth = [], []
    for index, point in enumerate(truth_points, start=1):
        ping_id = f"P{index:02d}"
        # 以第一个水听器为参考，将距离差除以真实声速得到时差。
        ranges = [math.dist(point, recv) for recv in receivers]
        tdoa_list = []
        for r_idx in range(1, len(receivers)):
            ideal = (ranges[r_idx] - ranges[0]) / sound_speed
            tdoa_list.append(ideal + rng.gauss(0.0, noise_std) if noise_std > 0 else ideal)
        measurements.append({"ping_id": ping_id, "tdoa": tdoa_list})
        truth.append({"ping_id": ping_id, "x_m": point[0], "y_m": point[1], "z_m": point[2]})

    sim_data = {
        "receivers": receivers,
        "measurements": measurements,
        "truth": truth,
        "sound_speed_m_s": sound_speed
    }
    # 留下本次仿真的观测和真值，供网页查看、核对及另存为个人数据。
    context.atomic_write_json(context.result_path('simulation.json'), sim_data)
    return {"sim_data": sim_data}
