"""在服务器自己的研究解释器中运行，只检查，不安装或修改配置。"""
from __future__ import annotations
import argparse
import importlib
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cnn', action='store_true', help='额外检查CNN所需库及GPU可用性')
    args = parser.parse_args(argv)
    print('当前解释器：', sys.executable)
    print('Python版本：', sys.version.split()[0])
    failed = sys.version_info < (3, 11)
    if failed:
        print('需要Python 3.11或更新版本；请选自己的科研环境，不使用系统Python或平台服务环境。')
    for name in (['yaml', 'numpy', 'matplotlib', 'torch'] if args.cnn else ['yaml']):
        try:
            module = importlib.import_module(name)
            print(name + '：可用，版本 ' + str(getattr(module, '__version__', '未知')))
            if name == 'torch':
                available = module.cuda.is_available()
                print('CUDA GPU：' + ('可用' if available else '当前解释器不可用；auto会使用CPU。若需GPU请核对科研环境。'))
        except Exception as exc:
            failed = True
            print(name + '：不可用，' + str(exc))
    print('环境检查' + ('未通过。按首页说明准备个人环境，或将输出发给教师。' if failed else '通过。可继续对应案例。'))
    return 1 if failed else 0

if __name__ == '__main__':
    raise SystemExit(main())
