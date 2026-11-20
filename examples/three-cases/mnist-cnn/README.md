# MNIST CNN 十分类实验

标签：`深度学习`、`CNN`、`MNIST`、`图像分类`、`GPU`、`参数对比`

使用真实 MNIST 的独立训练集和测试集；两层卷积均参加反向传播。基线使用学习率 0.001，对照使用 0.0003；其他参数相同。此案例不下载数据，也不改共享数据文件。

在当前科研 Python 环境运行 `python run_experiment.py --scheme baseline --data-root /home/share/Datasets/Others/MNIST`。路径是本服务器已有数据的例子；其他部署按实际路径修改。也可把已登记并加入项目的真实数据 ID 写入方案，使用 `Context.open_dataset` 只读访问。

`device: auto` 自动选择当前环境可见的 CUDA GPU，否则使用 CPU。需指定某张 GPU 时，可使用 IDE 启动环境变量 `CUDA_VISIBLE_DEVICES`；不会更改服务器或平台服务配置。

案例包的完整操作步骤以随包的“先读这里-三个案例操作指南.md”为准。

## 审核入库可复制的信息

- 案例说明：在个人科研环境中使用可编辑的 PyTorch CNN 完成 MNIST 十分类，按方案保存参数、训练曲线、混淆矩阵、预测结果和模型。
- 案例标签：深度学习、MNIST、图像分类、GPU、参数对比。
- 算法说明：两层卷积和十分类全连接网络，使用 Adam 与交叉熵训练；独立 train/test IDX 数据；支持 CPU/CUDA，自动生成指标和模型产物。
- 算法标签：CNN、PyTorch、MNIST、图像分类。

这些是供审核表单填写的建议，并不声称已经写入现有平台元数据。若平台已有 `mnist.cnn@1.0.0`，入库时选择合适的新版本，不覆盖旧算法。
