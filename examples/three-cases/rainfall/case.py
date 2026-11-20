"""降雨基础框架入口，后续 CNN 和水声案例也可复用。

实验入口先读取方案并合并参数，再调用本文件。方案通过 algorithm_alias
选择项目中已经加入的算法；本文件负责调用算法，并保存供网页审核的摘要。
"""


def execute(context):
    # algorithm_alias 是调用名称，必须对应 project.yaml 的 plugins 中的别名。
    # 切换到 cnn 或 locator 只需改方案，不用改本入口或受保护的平台算法。
    alias = context.parameters.get('algorithm_alias', 'scale')
    summary = context.call(alias, inputs={})['summary']
    # result_path 指向当前方案本次结果目录，不会写回原始数据。
    context.atomic_write_json(context.result_path('summary.json'), summary)
    return summary


def run(context):
    # 统一辅助入口负责参数、产物和保留最新成功结果；IDE 可照常打断点。
    from experiment_support import run_experiment
    return run_experiment(context, execute)
