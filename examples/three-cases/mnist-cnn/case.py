"""MNIST 十分类案例入口。

运行入口由项目根目录的 experiment_support.py 提供：它选择 experiments/
中的方案、合并参数，成功后保留每个方案的最新结果。本文件只描述案例
本身，因而也可以在调试时直接调用 execute(context)。
"""
from __future__ import annotations


def execute(context):
    # cnn 是项目里绑定的算法别名；学习率、轮数等由当前实验方案决定。
    output = context.call('cnn', inputs={})
    summary = output['summary']
    # 将准确率等摘要保存到本次结果目录，网页和 IDE 都能读取。
    context.atomic_write_json(context.result_path('summary.json'), summary)
    return summary


def run(context):
    from experiment_support import run_experiment

    return run_experiment(context, execute)
