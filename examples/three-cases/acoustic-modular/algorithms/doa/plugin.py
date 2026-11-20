"""水声时差预处理算子：整理观测；本教学实现不估计方位角。"""
from __future__ import annotations

def run(inputs: dict, parameters: dict, context) -> dict:
    if 'sim_data' not in inputs:
        raise ValueError('doa 算子必须接收 sim_data 输入端口')
    sim_data = inputs['sim_data']
    method = str(parameters.get('method', 'direct_tdoa'))
    processed = []
    for item in sim_data['measurements']:
        tdoa = list(item['tdoa'])
        # 本教程用 direct_tdoa 原样传递；平滑只用于另行研究，勿混淆实验条件。
        if method == 'smooth_tdoa' and len(processed) > 0:
            prev = processed[-1]['tdoa']
            tdoa = [0.7 * c + 0.3 * p for c, p in zip(tdoa, prev)]
        processed.append({'ping_id': item['ping_id'], 'tdoa': tdoa})

    return {'bearing_data': {
        'receivers': sim_data['receivers'],
        'measurements': processed,
        'truth': sim_data['truth'],
        'method': method
    }}
