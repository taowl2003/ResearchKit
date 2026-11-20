"""水声到达时间差（TDOA）定位案例入口：调用定位算法并保存摘要。"""


def execute(context):
    # locator 是算法别名；两套方案分别向它传入1500和1480米/秒声速。
    summary = context.call('locator', inputs={})['summary']
    # 摘要包括位置均方根误差和最大误差，便于比较两套声速方案。
    context.atomic_write_json(context.result_path('summary.json'), summary)
    return summary


def run(context):
    # 使用统一入口管理方案、有效参数和最新成功产物。
    from experiment_support import run_experiment
    return run_experiment(context, execute)
