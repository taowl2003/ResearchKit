"""Small Chinese command line for the teacher and student workflow."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import traceback
from pathlib import Path

import yaml

from .files import KitError
from . import model, projects


def _parser(for_task: bool = False) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='./task' if for_task else 'rk',
                                description='项目操作：检查、运行、提交与编辑算法' if for_task
                                else 'ResearchKit：创建、运行并分享可搬移的研究项目')
    sub = p.add_subparsers(dest='action')
    if for_task:
        for name in ('run', 'check', 'status', 'submit'):
            q = sub.add_parser(name, help={'run': '运行当前项目', 'check': '检查项目依赖',
                                            'status': '查看最近运行状态', 'submit': '提交学生项目'}[name])
            q.add_argument('project', nargs='?', type=Path)
            if name == 'submit':
                q.add_argument('--output', type=Path)
        q = sub.add_parser('edit', help='复制固定算法为可编辑副本')
        q.add_argument('alias')
        q.add_argument('--project', type=Path)
        return p
    sub.add_parser('catalog', help='查看教学案例和算法')
    q = sub.add_parser('new', help='创建项目')
    q.add_argument('name', help='项目名或目标路径')
    q.add_argument('--template', default='hello', help='hello 或 saved:<收藏名>')
    q = sub.add_parser('import', help='复制脚本或旧 HYDRO 案例为工作项目')
    q.add_argument('source')
    q.add_argument('name', help='项目名或目标路径')
    q.add_argument('--command', help='脚本运行 argv，例如 "python main.py --input data/a.csv"')
    q.add_argument('--platform', type=Path, help='导入旧案例时的只读旧平台目录')
    for name in ('run', 'doctor', 'check', 'status', 'share', 'submit', 'save'):
        q = sub.add_parser(name)
        q.add_argument('project', nargs='?', type=Path)
        if name in {'share', 'submit'}:
            q.add_argument('--output', type=Path)
        if name == 'save':
            q.add_argument('--name')
    q = sub.add_parser('receive', help='教师收取学生提交的 zip')
    q.add_argument('archive', type=Path)
    q.add_argument('--output', type=Path)
    q = sub.add_parser('edit', help='高级：复制固定算法到项目 algorithms/ 后修改')
    q.add_argument('alias')
    q.add_argument('--project', type=Path)
    q = sub.add_parser('add', help='高级：从新算法库添加算法')
    q.add_argument('ref', help='名称@精确版本')
    q.add_argument('--as', dest='alias', required=True)
    q.add_argument('--param', action='append', default=[], help='参数 key=值，可重复')
    q.add_argument('--project', type=Path)
    q = sub.add_parser('publish', help='高级：测试并发布独立算法版本')
    q.add_argument('source', type=Path)
    q.add_argument('--contracts', type=Path, help='插件引用契约所在目录；项目内算法可自动发现')
    return p


def _project_path(value: Path | None, default_project: Path | None) -> Path:
    candidate = value or default_project or Path.cwd()
    candidate = Path(candidate).expanduser().resolve()
    if not (candidate / 'project.yaml').is_file():
        raise KitError(f'这里没有 project.yaml：{candidate}；先用 rk new NAME 创建项目')
    return candidate


def _output_path(name: str) -> Path:
    p = Path(name).expanduser()
    return p.resolve() if p.is_absolute() or len(p.parts) > 1 or name in {'.', '..'} else projects.home() / 'projects' / name


def _params(items: list[str]) -> dict:
    values = {}
    for item in items:
        if '=' not in item:
            raise KitError(f'参数应写为 key=值：{item}')
        key, raw = item.split('=', 1)
        if not key or key in values:
            raise KitError(f'参数名为空或重复：{key}')
        values[key] = yaml.safe_load(raw)
    return values


def _dispatch(args, default_project: Path | None) -> dict:
    action = args.action
    if action == 'catalog':
        library = projects.home() / 'library'
        cases = [f"{p.parent.parent.relative_to(library / 'cases').as_posix()}@{p.parent.name}"
                 for p in (library / 'cases').rglob('project.yaml')]
        algorithms = [f"{p.parent.parent.relative_to(library / 'algorithms').as_posix()}@{p.parent.name}"
                      for p in (library / 'algorithms').rglob('plugin.yaml')]
        saved = ([p.name for p in (library / 'projects').iterdir()]
                 if (library / 'projects').is_dir() else [])
        return {'cases': sorted(cases), 'algorithms': sorted(algorithms),
                'saved': sorted(saved)}
    if action == 'new':
        return projects.new_project(_output_path(args.name), args.template)
    if action == 'import':
        command = shlex.split(args.command) if args.command else None
        return projects.import_project(Path(args.source), _output_path(args.name), command, args.platform)
    if action in {'run', 'doctor', 'check', 'status', 'share', 'submit', 'save'}:
        root = _project_path(args.project, default_project)
        if action == 'run':
            from .engine import run_project
            return run_project(root)
        if action in {'doctor', 'check'}:
            return model.doctor(root)
        if action == 'status':
            return model.freshness(root)
        from . import exchange
        if action == 'share':
            return exchange.share_project(root, args.output)
        if action == 'submit':
            return exchange.submit_project(root, args.output)
        return exchange.save_project(root, args.name)
    if action == 'receive':
        from .exchange import receive_submission
        return receive_submission(args.archive, args.output)
    if action == 'edit':
        return projects.edit_plugin(_project_path(args.project, default_project), args.alias)
    if action == 'add':
        return projects.add_plugin(_project_path(args.project, default_project), args.ref,
                                   args.alias, _params(args.param))
    if action == 'publish':
        return projects.publish_plugin(args.source, args.contracts)
    raise KitError(f'未知命令：{action}')


def _menu(for_task: bool = False) -> str:
    if for_task:
        return ('当前项目（学生）\n'
                '  ./task check          检查项目与依赖\n'
                '  ./task run            运行案例\n'
                '  ./task status         查看运行状态\n'
                '  ./task submit         打包提交\n'
                '  ./task edit ALIAS     复制算法到 algorithms/ 后编辑\n'
                '项目说明：README-ResearchKit.md；更多用法：./task --help。')
    return ('ResearchKit（本地）\n'
            '  rk new hello-1            创建教学项目\n'
            '  rk run projects/hello-1   运行项目\n'
            '  rk share projects/hello-1 分享给学生\n'
            '学生：./task run → ./task submit\n'
            '教师：rk receive 提交.zip → rk save 项目\n'
            '更多命令：rk --help；算法进阶：rk edit / add / publish。')


def main(argv: list[str] | None = None, *, default_project: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = '--json' in argv
    debug = '--debug' in argv
    argv = [x for x in argv if x not in {'--json', '--debug'}]
    if not argv:
        print(_menu(default_project is not None))
        return 0
    try:
        args = _parser(default_project is not None).parse_args(argv)
        if not args.action:
            print(_menu(default_project is not None))
            return 0
        result = _dispatch(args, default_project)
        if json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.action == 'run':
            print(f"运行成功：{result.get('run_id')}；结果：{json.dumps(result.get('result'), ensure_ascii=False)}")
        elif 'path' in result:
            print(f"完成：{result['path']}")
            if 'report' in result:
                print(f"查看报告：{result['report']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (KitError, OSError, ValueError) as exc:
        if debug:
            traceback.print_exc()
        elif json_output:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print(f'错误：{exc}', file=sys.stderr)
        return 1
