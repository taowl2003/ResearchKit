# ResearchKit 科研编程平台

通过网页管理案例、算法、数据和固定提交，在自己的服务器 IDE 中开发和运行实验；教师审核成果入库并授权复用。网页可以查看、比较已有产物，不承担 Python 或 GPU 运行。

## 学生从这里开始

**只需先读一份[学生使用指南](docs/学生使用指南.md)。** 按“首次连接 → 三个案例 → 功能备查”学习，不用在多份手册间来回切换。

1. [下载学生上手包](downloads/ResearchKit-Student.zip)，完整解压，打开里面的 **学生使用指南.html**。它与上述指南内容相同，可离线阅读。
2. 向教师取得自己的服务器账号、当前密码、连接地址和可用 Python 研究环境。
3. 双击包内 **启动器/Start-ResearchKit.cmd**。首次按提示配置；以后双击同一文件即可。
4. 按指南完成降雨、MNIST CNN、三阶段水声定位练习，再将所选代码、方案和产物保存为固定提交。

学生使用的是管理员已经部署的平台，**不必在自己的 Windows 或服务器账号重新安装平台服务，也不必下载整个源码仓库才能使用**。GitHub 账号与平台服务器账号是两回事；能读源码不表示已取得服务器或科研数据权限。真实数据集不随本仓库分发。

## 不明白某个操作时，去哪里看

先在[统一指南](docs/学生使用指南.md)搜索按钮名；每个案例的“这一步做了什么”会说明它改动的文件。需要进一步了解实现，再按下表打开源码：

| 想了解什么 | 从这些文件开始 |
| --- | --- |
| 网页有哪些页面和按钮 | [index.html](core/researchkit/web_static/index.html)、[app.js](core/researchkit/web_static/app.js) |
| 网页请求怎样进入后端 | [web_app.py](core/researchkit/web_app.py)、[web_broker.py](core/researchkit/web_broker.py) |
| 新建项目、文件编辑、个人工作区 | [web_workspace.py](core/researchkit/web_workspace.py) |
| 算法加入项目、绑定别名与参数 | [web_workspace_materials.py](core/researchkit/web_workspace_materials.py)；新建本地算法在 web_workspace.py 的 create_local_algorithm |
| 实验方案、IDE 入口、产物保留与比较 | [web_experiments.py](core/researchkit/web_experiments.py)、[experiment_support.py](core/researchkit/experiment_support.py) |
| 数据读取和只读引用 | [dataset_access.py](core/researchkit/dataset_access.py)、[web_datasets.py](core/researchkit/web_datasets.py)、[web_derived.py](core/researchkit/web_derived.py) |
| 固定提交怎样保存 | [web_store.py](core/researchkit/web_store.py)、[web_submission_flow.py](core/researchkit/web_submission_flow.py) |
| 教师审核入库、权限分配 | [web_review_flow.py](core/researchkit/web_review_flow.py)、[web_sharing.py](core/researchkit/web_sharing.py) |
| 用户反馈与教师回复 | [web_feedback.py](core/researchkit/web_feedback.py)、[web_feedback_rpc.py](core/researchkit/web_feedback_rpc.py) |
| 三个案例的完整算法实现 | [降雨](examples/three-cases/rainfall/)、[MNIST CNN](examples/three-cases/mnist-cnn/)、[三阶段水声定位](examples/three-cases/acoustic-modular/) |
| 如何验证这些行为 | [tests](tests/)；例如[三阶段案例操作验证](tests/test_student_guide_modular.py) |

算法实际编辑和调试在自己的个人项目里完成。阅读平台源码不需要修改中央数据库、服务配置或受保护的运行框架。遇到平台问题，在网页“消息反馈”中写明步骤、项目或提交 ID、报错文字；教师可在同一处回复。

## 目录与职责

| 目录 | 用途 |
| --- | --- |
| core/researchkit | 平台后端、网页和个人项目运行支持 |
| docs | 统一学生指南、兼容跳转及维护说明 |
| examples/three-cases | 教学源码；跟做指南时在网页从空白项目、算法起步 |
| tools/windows、tools/student-kit | Windows 连接程序和通用调试辅助文件 |
| tools | 文档与学生包生成、管理员安装维护工具 |
| tests | 自动化验证，不是学生项目或真实科研数据 |
| deploy | 示例策略、服务定义、锁定的服务依赖 |
| library | 旧 CLI 和首次安装所需的最小示例资源；Web 正式资源在中央存储 |
| downloads | 可直接发给学生的公共上手包，不含个人连接设置或密钥 |

个人项目在各账号的 `~/researchkit-workspace/projects/`；平台固定提交与已发布资源在中央存储；共享原始数据、托管派生数据分别由管理员配置。它们不会通过下载这个仓库自动获得或覆盖。

## 教师怎样分享给学生

把仓库地址 **https://github.com/taowl2003/ResearchKit** 发给学生即可。仓库公开时，学生可直接阅读首页和文档；不用邀请成为协作者，也不用给予修改本仓库的权限。

普通使用下载上面的学生包；想在本机搜索源码，可用 GitHub 的 **Code → Download ZIP**，或者在自己的开发目录执行：

```bash
git clone https://github.com/taowl2003/ResearchKit.git
```

源码副本只供查阅或开发，不会自动修改服务器正在运行的平台。账号和服务器连接信息由教师单独告知；自己的 connection.json、私钥、密码及私有研究数据不提交到 GitHub。

## 平台维护者

学生日常操作按统一指南；服务器安装更新由服务器管理员负责，入口在 [tools/install-web.py](tools/install-web.py)、[tools/update-web.py](tools/update-web.py) 与[部署说明](docs/web-deployment.md)。`ly` 无需获得完整 sudo 权限。

只做源码开发验证时，在自己的开发目录准备环境：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[test,web]' mistune
PYTHONPATH=core .venv/bin/python -m pytest tests -q
```

部分系统隔离、Windows PowerShell 或 CNN 验证还需对应系统条件、Node.js 或科研依赖。查看某项测试时以其环境要求为准；不要在正式用户项目中试删或直接运行管理员安装命令。

本仓库首次发布的是经过整理的当前源码，不包含历次安装包、测试残留、服务数据库、用户工作区或私有配置。可重复生成学生包和帮助页：

```bash
python tools/render-three-case-tutorial.py --check
python tools/render-web-guides.py
python tools/build-student-kit.py
```

生成文档需在开发环境安装 mistune；不增加平台服务运行依赖。
