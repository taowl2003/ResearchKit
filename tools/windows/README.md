# ResearchKit Windows 启动器（兼容 Web v0.8）

学生只需阅读 [学生使用指南](../../docs/学生使用指南.md)，按首次连接、三个案例、功能备查的顺序学习。交付学生时使用统一上手包，解压后根目录只有这一份 HTML 指南，启动程序在“启动器”子目录。首次按提示输入自己的账号，并由 SSH 读取当前服务器密码，自动生成并登记本机专用密钥。已有 SSH 别名的用户直接填写别名即可。需要 Windows OpenSSH 客户端，服务器须允许账号通过密码完成首次认证。

以后只双击 Start-ResearchKit.cmd，保留一个连接窗口，可最小化。关闭连接不删除项目和固定提交。启动器不安装或升级服务器程序，不在 Windows 安装 Python，不保存服务器密码。私钥只在当前 Windows 用户的 .ssh 目录中保存，连接信息在启动器旁的 connection.json；不要把已配置文件夹或私钥转发给学生，交付原始 ZIP。不会修改全局 SSH config、Windows 系统设置或服务器登录策略。

维护者可在解压目录用下列选项诊断，不作为日常步骤：

```cmd
Start-ResearchKit.cmd -Check
Start-ResearchKit.cmd -NoBrowser
Start-ResearchKit.cmd -SelfTest
Start-ResearchKit.cmd -Setup
```

`-Check` 检查本机程序、连接参数和端口；`-SelfTest` 检查参数与启动地址，均不连接服务器。`-NoBrowser` 建立实际连接并打印地址；`-Setup` 重新运行首次连接向导。Configure-ResearchKit.cmd 保留为只保存已有 SSH 别名的工具。原网页自动打开和单窗口已由用户验证；新增密码登记和不同 Windows／网络仍需现场验收。

网页中遇到平台问题，可以打开“消息反馈”给管理员发消息，之后在同一处查看回复。项目的代码、方案 YAML、运行和调试都在服务器 IDE 中完成；网页负责中央数据登记、固定提交、教师审核发布和授权，查看／比较已有产物不会重跑代码。首次缺入口时，在个人项目卡片提交按钮下方展开“IDE 实验方案与产物”生成入口；之后使用 `python run_experiment.py --scheme <方案ID>` 即可日常运行。仅更新本启动包里的指南，启动脚本与首次认证流程保持一致。

首次从空平台开始，按学生使用指南第二部分1.1的“网页新建空项目”开始，不复制成品项目。算法库手动填写名称、版本、标签、说明、参数 JSON 和契约 JSON，`plugin.py` 留空创建接口骨架；加入项目后在服务器 IDE 写完整实现。它不自动保存或入库。
