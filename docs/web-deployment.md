# 服务器安装和维护

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
