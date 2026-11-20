"""水声定位三阶段流水线主入口"""
from __future__ import annotations

def execute(context):
    # 步骤 1：调用仿真算子生成阵列与观测
    sim_out = context.call('sim', inputs={})
    
    # 步骤 2：调用测向/时差预处理算子（可单点替换）
    doa_out = context.call('doa', inputs={'sim_data': sim_out['sim_data']})
    
    # 步骤 3：调用定位解算算子（可单点替换）
    loc_out = context.call('locator', inputs={'bearing_data': doa_out['bearing_data']})
    
    summary = loc_out['summary']
    context.atomic_write_json(context.result_path('summary.json'), summary)
    return summary

def run(context):
    from experiment_support import run_experiment
    return run_experiment(context, execute)
