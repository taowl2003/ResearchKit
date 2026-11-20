#!/usr/bin/env python3
"""Prepare a reviewed source snapshot; never upload or read user workspaces.

Only explicit source roots and public teaching artifacts are selected. Existing
output directories are rejected. Operational deployment history stays local.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import stat
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
SOURCE_ROOTS=('core','examples','tools','tests','deploy',
              'library/cases','library/algorithms','library/contracts')
SOURCE_SUFFIXES={'.py','.js','.css','.html','.json','.yaml','.yml','.md','.txt',
                 '.cmd','.ps1','.service','.lock','.example','.csv','.zip','.whl'}
DOCS=('学生使用指南.md','学生端快速上手指南.md','平台完整使用与管理手册.md',
      '科研编程平台三大全流程实战案例.md','科研编程平台三大全流程实战案例(三阶段模块化版).md',
      '案例三-水声定位三阶段模块化实战指南.md','IDE断点调试与多方案配置指南.md',
      'IDE-DEBUG-GUIDE.md','three-cases-walkthrough.md','web-user-guide.md')
FORBIDDEN={'connection.json','authorized_keys','.env','id_ed25519','id_rsa',
           'metadata.local.json','policy.json'}

MAINTENANCE='''# 服务器安装和维护

这部分由服务器管理员操作；学生连接现有平台只需读[学生使用指南](学生使用指南.md)。本仓库不包含运行中的数据库、注册表、用户目录或真实数据集。

## 先区分源码和已安装服务

- GitHub 源码：当前平台实现、教学算法、测试及安装工具。
- 正式程序：默认在 /opt/researchkit-web，必须经过管理员安装或更新才生效。
- 个人研究目录：各账号的 ~/researchkit-workspace/projects/。
- 中央提交和资源：默认 /var/lib/researchkit-web/submissions/。
- 数据文件：由管理员配置共享原始根和托管派生根；不随 git clone 下载。

学生不需要部署服务，也不需要 sudo。当前网页不执行 Python，科研运行在个人 IDE 环境；旧 runner 不应重新启用。

## 已部署服务器更新

先在独立位置取得并核对需要安装的准确提交，完成测试、备份及维护窗口安排；不要对正在使用的个人项目直接执行 git pull 或安装命令。以下由原有管理员在 root 终端按顺序执行，工作目录是已核对的源码根目录。

1. 让用户保存编辑并关闭各自连接。在启用了数据管理服务时暂停 researchkit-data-manager.service，确认 inactive；已退役的 researchkit-runner.service 保持停用。
2. 执行 `/usr/bin/python3 tools/update-web.py --check`。必须 ready=true、errors=[] 才继续。
3. 执行 `/usr/bin/python3 tools/update-web.py`，保存代码和数据库备份路径；失败立即停止，不执行后续步骤。
4. 启动之前已配置的数据管理服务，核对它和 researchkit-broker.service 为 active。让用户重开各自启动器。

更新器复用已安装的服务环境，保留数据文件与授权。降级涉及数据库迁移时，必须恢复相配套的代码和数据库备份。不会给 ly 账号提权。

## 新服务器首次安装

由系统管理员先阅读 tools/install-web.py 的 --help 和 --check 输出，准备受信任的 Python 3.11/3.12、专用服务账号及系统服务；再决定安装。deploy/requirements-web.lock 与 deploy/wheels/ 提供锁定的服务依赖，GPU 科研环境单独准备，不装进服务环境。

数据服务的配置工具是 tools/configure-web-data.py、tools/enable-web-readme-data.py 和 tools/install-web-data-manager.py。共享目录、可信路径与权限必须满足预检；不能把已有服务器的 /etc 策略或私钥直接复制进本仓库。原始数据按 README 边界经教师网页审核登记和授权。

管理员应按本机发行版和部署条件验证完整安装，仓库下载成功不等于服务器已完成部署。现有服务器日常新增数据无需重新安装。
'''

ARCHITECTURE='''# 当前平台结构与阅读边界

当前 Web 工作流是：个人网页创建/复用项目 → 服务器 IDE 编辑与运行 → 网页查看产物、固定提交 → 教师审核入库与权限分配。案例、算法、契约和固定提交由中央存储管理；library/ 是 CLI 兼容和安装引导示例，不是第二套实时 Web 案例库。

## 用户与存储

SSH 身份确认账号；ly 同一次登录有个人研究区和教师管理区，学生只有自己的个人区。个人项目是独立可编辑副本，算法加入项目时复制代码，数据加入项目时记录准确版本的只读引用，不复制大数据。固定提交保存不可变代码、方案、所选小产物与引用；相同正文按内容去重。

## 阅读顺序

首页 README 按功能链接主要源码。web_app.py 处理个人 Web 请求，web_workspace.py 管理工作区文件，web_broker.py 处理受控中央操作；web_store.py 和 web_content.py 管理固定版本及正文。web_review_flow.py 执行教师审核，web_sharing.py 管理资源授权。数据读取由 dataset_access.py、web_datasets.py、web_derived.py 等核验。

experiment_support.py 在个人研究环境执行方案并记录产物；web_experiments.py 管理方案文件、准备入口和只读产物查看。源码中仍有旧任务、编排、后台作业及迁移模块，服务兼容和迁移需要它们，它们不代表当前学生必须走这些入口。当前网页不会运行 Python，旧后台执行记录不再是正常使用入口。

## 编辑与清理

algorithms/<别名>/ 是个人项目的可编辑本地算法；.researchkit/plugins/、runtime、contracts 及锁是固定依赖，不手工篡改。案例与算法模板删除不修改个人代码副本；数据页面移除登记不自动删除磁盘数据。用户归档只隐藏项目，确认删除本地副本才移除本人目录；中央固定提交独立保留。

完整按钮操作和文件变化看[学生使用指南](学生使用指南.md)。服务器维护见[部署说明](web-deployment.md)。
'''

def selected_files(root: Path):
    selected={}
    for relative in SOURCE_ROOTS:
        start=root/relative
        for path in sorted(start.rglob('*')):
            parts=path.relative_to(root).parts
            if any(part.startswith('.') or part=='__pycache__' for part in parts):
                continue
            if path.is_symlink():
                raise ValueError('Refuse source symlink: '+str(path.relative_to(root)))
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if path.name in FORBIDDEN or path.suffix in {'.sqlite3','.db','.blob','.log'}:
                raise ValueError('Private file in source roots: '+str(path.relative_to(root)))
            if path.stat().st_nlink!=1:
                raise ValueError('Refuse hardlinked source: '+str(path.relative_to(root)))
            selected[path.relative_to(root).as_posix()]=path
    for name in ('README.md','.gitignore','pyproject.toml','rk'):
        selected[name]=root/name
    for name in DOCS:
        selected['docs/'+name]=root/'docs'/name
    return selected

def verify_text(name: str,data: bytes):
    try:text=data.decode('utf-8-sig')
    except UnicodeDecodeError:
        if Path(name).suffix not in {'.zip','.whl'}:
            raise ValueError('Unexpected binary: '+name)
        return
    patterns=(r'(?m)^-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED)? ?PRIVATE KEY-----',
              r'github_pat_[A-Za-z0-9_]{35,}',r'gh[pousr]_[A-Za-z0-9]{35,}')
    if any(re.search(pattern,text) for pattern in patterns):
        raise ValueError('Possible credential; inspect without publishing: '+name)

def prepare(destination: Path):
    # Import only the pure student archive validator, never application code.
    spec=importlib.util.spec_from_file_location('student_kit',ROOT/'tools/build-student-kit.py')
    kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
    package=ROOT/'exports/ResearchKit-学生上手包-合并指南-20261120.zip'
    with ZipFile(package) as archive:
        student={n.removeprefix(kit.TOP+'/'):archive.read(n) for n in archive.namelist()}
    assert kit.validate(student)==1
    assert student==kit.outputs(),'Student kit differs from current guide'
    selected=selected_files(ROOT)
    destination.mkdir(parents=True,exist_ok=False)
    try:
        for name,path in selected.items():
            data=path.read_bytes();verify_text(name,data)
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
            target.chmod(0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)
        # Only the current maintenance guide is published, not old machine logs.
        (destination/'docs/web-deployment.md').write_text(MAINTENANCE)
        (destination/'docs/architecture.md').write_text(ARCHITECTURE)
        downloads=destination/'downloads';downloads.mkdir()
        shutil.copyfile(package,downloads/'ResearchKit-Student.zip')
        digest=hashlib.sha256(package.read_bytes()).hexdigest()
        (downloads/'ResearchKit-Student.zip.sha256').write_text(digest+'  ResearchKit-Student.zip\n')
        (destination/'PUBLICATION.json').write_text(json.dumps({
            'schema':'researchkit.source-publication/v1','date':'2026-11-20',
            'student_guide':'docs/学生使用指南.md','student_package':'downloads/ResearchKit-Student.zip',
            'student_sha256':digest,'excluded':['user workspaces','private configs and keys',
                'databases','real research datasets','old releases','operational handoff logs'],
            'source_changes':'README/ignore rules; current public maintenance/architecture summaries',
            'application_source':'copied without changing application logic',
        },ensure_ascii=False,indent=2)+'\n')
        records=[]
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                records.append(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(destination).as_posix()}')
        (destination/'SOURCE-SHA256SUMS').write_text('\n'.join(records)+'\n')
        return {'destination':str(destination),'files':len(records)+1,
                'bytes':sum(p.stat().st_size for p in destination.rglob('*') if p.is_file()),
                'student_html_documents':1,'credential_scan_passed':True}
    except Exception:
        # Keep failed preparation for inspection; never touch the source.
        raise

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.output),ensure_ascii=False))

if __name__=='__main__':main()
