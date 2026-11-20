"""项目内的 MNIST 手写数字十分类 CNN，使用用户已有数据，不自动下载。

通过 context.open_dataset 读取已授权的只读数据，也可显式指定本地数据目录。
训练集和测试集分别读取；结果、图表和模型只写入当前实验的产物目录。
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import time
from typing import BinaryIO

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


_IDX_FILES = {
    'train_images': 'train-images-idx3-ubyte',
    'train_labels': 'train-labels-idx1-ubyte',
    'test_images': 't10k-images-idx3-ubyte',
    'test_labels': 't10k-labels-idx1-ubyte',
}


class _MnistCnn(nn.Module):
    """两组卷积、ReLU和池化提取特征，再由全连接层输出十个类别分数。"""

    def __init__(self) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
        )
        # 28×28图像经两次二倍池化变成7×7，最后一层输出未经Softmax的分数。
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(32 * 7 * 7, 64),
            nn.ReLU(),
            nn.Linear(64, 10),
        )

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(values))


def _positive_int(parameters: dict, name: str) -> int:
    value = int(parameters[name])
    if value < 1:
        raise ValueError(f'{name} 必须是正整数')
    return value


def _resolve_device(value: object) -> torch.device:
    requested = str(value)
    # auto 优先使用当前解释器能访问的 GPU；显式 cuda 不可用时直接报错。
    if requested == 'auto':
        return torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if requested == 'cpu':
        return torch.device('cpu')
    if requested == 'cuda':
        if not torch.cuda.is_available():
            raise RuntimeError('方案指定 device=cuda，但当前 Python 环境没有可用 CUDA GPU')
        return torch.device('cuda')
    raise ValueError('device 只能是 auto、cpu 或 cuda')


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    # 固定种子和卷积策略，便于教学比较；不同PyTorch/CUDA版本仍可能有差异。
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def _read_exact(stream: BinaryIO, size: int, label: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ValueError(f'{label} 长度不足：需要 {size} 字节，实际 {len(value)} 字节')
    return value


def _load_idx_images(stream: BinaryIO, limit: int, label: str) -> np.ndarray:
    # IDX采用大端整数头；先验证格式和样本数，再读取指定数量的28×28图像。
    magic, count, rows, columns = struct.unpack('>IIII', _read_exact(stream, 16, label))
    if magic != 2051 or (rows, columns) != (28, 28):
        raise ValueError(f'{label} 不是 28×28 的 MNIST 图像 IDX 文件')
    if limit > count:
        raise ValueError(f'{label} 只有 {count} 个样本，不能请求 {limit} 个')
    raw = _read_exact(stream, limit * rows * columns, label)
    return np.frombuffer(raw, dtype=np.uint8).copy().reshape(limit, 1, rows, columns)


def _load_idx_labels(stream: BinaryIO, limit: int, label: str) -> np.ndarray:
    magic, count = struct.unpack('>II', _read_exact(stream, 8, label))
    if magic != 2049:
        raise ValueError(f'{label} 不是 MNIST 标签 IDX 文件')
    if limit > count:
        raise ValueError(f'{label} 只有 {count} 个标签，不能请求 {limit} 个')
    labels = np.frombuffer(_read_exact(stream, limit, label), dtype=np.uint8).copy()
    if np.any(labels > 9):
        raise ValueError(f'{label} 包含非十分类标签')
    return labels


def _local_root(context) -> Path:
    root = str(context.parameters.get('data_root', '')).strip()
    if not root:
        raise ValueError('data_source=local 时必须在方案中明确填写绝对 data_root')
    path = Path(root).expanduser()
    if not path.is_absolute():
        raise ValueError('data_root 必须是显式绝对路径')
    if not path.is_dir():
        raise ValueError(f'本地 MNIST 数据目录不存在：{path}')
    raw = path / 'raw'
    return raw if raw.is_dir() else path


def _read_local_file(root: Path, filename: str):
    path = root / filename
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'本地 MNIST 文件不存在或不是普通文件：{path}')
    return path.open('rb')


def _load_split(context, *, split: str, limit: int) -> tuple[np.ndarray, np.ndarray]:
    if split not in {'train', 'test'}:
        raise ValueError(f'未知数据划分：{split}')
    # 官方 train 和 t10k 是两个独立数据划分，不能把测试样本混进训练。
    image_name = _IDX_FILES[f'{split}_images']
    label_name = _IDX_FILES[f'{split}_labels']
    source = str(context.parameters.get('data_source', '')).strip()
    if source == 'dataset':
        dataset_id = str(context.parameters.get('dataset_id', '')).strip()
        if not dataset_id:
            raise ValueError('data_source=dataset 时必须在方案中填写已授权的 dataset_id')
        with context.open_dataset(dataset_id, f'raw/{image_name}', mode='rb') as image_stream:
            images = _load_idx_images(image_stream, limit, f'{split} 图像')
        with context.open_dataset(dataset_id, f'raw/{label_name}', mode='rb') as label_stream:
            labels = _load_idx_labels(label_stream, limit, f'{split} 标签')
    elif source == 'local':
        root = _local_root(context)
        with _read_local_file(root, image_name) as image_stream:
            images = _load_idx_images(image_stream, limit, f'{split} 图像')
        with _read_local_file(root, label_name) as label_stream:
            labels = _load_idx_labels(label_stream, limit, f'{split} 标签')
    else:
        raise ValueError("data_source 必须是 'dataset' 或 'local'")
    if len(images) != len(labels):
        raise ValueError(f'{split} 图像和标签数量不一致')
    return images, labels


def _write_csv(context, name: str, header: list[str], rows) -> None:
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    context.atomic_write_text(context.result_path(name), buffer.getvalue())


def _save_figure(context, name: str, figure) -> None:
    destination = Path(context.artifact_path(name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f'.{destination.name}.', suffix='.png', dir=destination.parent, delete=False,
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        figure.savefig(temporary, dpi=150, bbox_inches='tight')
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
        plt.close(figure)


def _save_checkpoint(context, model: nn.Module, summary: dict) -> None:
    destination = Path(context.artifact_path('checkpoint.pt'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f'.{destination.name}.', suffix='.pt', dir=destination.parent, delete=False,
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        cpu_state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
        torch.save({'model_state_dict': cpu_state, 'summary': summary}, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _plot_history(history: list[dict], context) -> None:
    figure, axes = plt.subplots(1, 2, figsize=(9, 3.5))
    epochs = [row['epoch'] for row in history]
    axes[0].plot(epochs, [row['train_loss'] for row in history], marker='o')
    axes[0].set(title='Training loss', xlabel='Epoch', ylabel='Cross-entropy')
    axes[1].plot(epochs, [row['test_accuracy'] for row in history], marker='o')
    axes[1].set(title='Test accuracy', xlabel='Epoch', ylabel='Accuracy')
    for axis in axes:
        axis.grid(alpha=0.25)
    figure.tight_layout()
    _save_figure(context, 'training_curve.png', figure)


def _plot_confusion(confusion: np.ndarray, context) -> None:
    figure, axis = plt.subplots(figsize=(6, 5))
    image = axis.imshow(confusion, cmap='Blues')
    figure.colorbar(image, ax=axis, label='Samples')
    axis.set(
        title='MNIST confusion matrix', xlabel='Predicted label', ylabel='True label',
        xticks=range(10), yticks=range(10),
    )
    figure.tight_layout()
    _save_figure(context, 'confusion_matrix.png', figure)


def _evaluate(model: nn.Module, loader: DataLoader, device: torch.device):
    # 评估不更新模型参数；CrossEntropyLoss接收原始分数，Softmax只用于置信度。
    model.eval()
    loss_fn = nn.CrossEntropyLoss(reduction='sum')
    total_loss = 0.0
    total = 0
    correct = 0
    predictions: list[tuple[int, int, float]] = []
    with torch.no_grad():
        for images, labels in loader:
            images, labels = images.to(device), labels.to(device)
            logits = model(images)
            total_loss += float(loss_fn(logits, labels).item())
            probabilities = torch.softmax(logits, dim=1)
            confidence, predicted = torch.max(probabilities, dim=1)
            correct += int((predicted == labels).sum().item())
            total += int(labels.numel())
            predictions.extend(
                (int(true), int(pred), float(score))
                for true, pred, score in zip(labels.cpu(), predicted.cpu(), confidence.cpu())
            )
    return total_loss / max(total, 1), correct, total, predictions


def run(inputs, parameters, context):
    """按当前方案训练分类器，记录测试指标，并保存供审核和比较的产物。"""
    if inputs != {}:
        raise ValueError('mnist.cnn 不接收算法输入；数据源由案例方案参数指定')
    epochs = _positive_int(parameters, 'epochs')
    batch_size = _positive_int(parameters, 'batch_size')
    train_limit = _positive_int(parameters, 'train_limit')
    test_limit = _positive_int(parameters, 'test_limit')
    num_workers = int(parameters['num_workers'])
    if num_workers < 0:
        raise ValueError('num_workers 不能小于 0')
    learning_rate = float(parameters['learning_rate'])
    if not 0.0 < learning_rate <= 10.0:
        raise ValueError('learning_rate 必须在 (0, 10] 内')
    seed = int(parameters['seed'])
    device = _resolve_device(parameters['device'])
    _seed_everything(seed)
    started = time.monotonic()

    train_images, train_labels = _load_split(context, split='train', limit=train_limit)
    test_images, test_labels = _load_split(context, split='test', limit=test_limit)
    # 像素从0—255归一化为0—1；保持训练、测试样本相互独立。
    train_tensor = torch.from_numpy(train_images).to(torch.float32).div_(255.0)
    test_tensor = torch.from_numpy(test_images).to(torch.float32).div_(255.0)
    train_targets = torch.from_numpy(train_labels.astype(np.int64, copy=False))
    test_targets = torch.from_numpy(test_labels.astype(np.int64, copy=False))
    train_dataset = TensorDataset(train_tensor, train_targets)
    test_dataset = TensorDataset(test_tensor, test_targets)
    train_generator = torch.Generator().manual_seed(seed)
    loader_options = {'batch_size': batch_size, 'num_workers': num_workers, 'pin_memory': device.type == 'cuda'}
    # 只打乱训练集；测试集保持原顺序，预测表中的样本索引才可对应。
    train_loader = DataLoader(train_dataset, shuffle=True, generator=train_generator, **loader_options)
    test_loader = DataLoader(test_dataset, shuffle=False, **loader_options)

    model = _MnistCnn().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_fn = nn.CrossEntropyLoss()
    history: list[dict] = []
    # 每轮：训练前向计算→损失→反向传播→更新权重，再评估测试指标。
    # 测试指标用于演示比较；正式调参应另设验证集，最终测试集不参与调参。
    for epoch in range(1, epochs + 1):
        model.train()
        weighted_loss = 0.0
        seen = 0
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = loss_fn(logits, labels)
            loss.backward()
            optimizer.step()
            batch_count = int(labels.numel())
            weighted_loss += float(loss.item()) * batch_count
            seen += batch_count
        test_loss, correct, total, _ = _evaluate(model, test_loader, device)
        history.append({
            'epoch': epoch,
            'train_loss': weighted_loss / max(seen, 1),
            'test_loss': test_loss,
            'test_accuracy': correct / max(total, 1),
        })

    test_loss, correct, total, predictions = _evaluate(model, test_loader, device)
    # 混淆矩阵的行是真实类别、列是预测类别，便于找出容易混淆的数字。
    confusion = np.zeros((10, 10), dtype=np.int64)
    for true, predicted, _confidence in predictions:
        confusion[true, predicted] += 1
    elapsed = time.monotonic() - started
    summary = {
        'algorithm': 'mnist.cnn@1.0.0',
        'accuracy': float(correct / max(total, 1)),
        'correct_count': int(correct),
        'test_samples': int(total),
        'train_samples': int(len(train_dataset)),
        'test_loss': float(test_loss),
        'epochs': int(epochs),
        'learning_rate': float(learning_rate),
        'batch_size': int(batch_size),
        'seed': int(seed),
        'device': str(device),
        'elapsed_seconds': float(elapsed),
        'artifacts': [
            'metrics.json', 'history.csv', 'predictions.csv', 'confusion_matrix.csv',
            'artifacts/training_curve.png', 'artifacts/confusion_matrix.png', 'artifacts/checkpoint.pt',
        ],
    }
    # context路径接口将表格、图和模型放到本方案当前批次；不修改MNIST原数据。
    context.atomic_write_json(context.result_path('metrics.json'), {'summary': summary, 'history': history})
    _write_csv(
        context,
        'history.csv',
        ['epoch', 'train_loss', 'test_loss', 'test_accuracy'],
        ((row['epoch'], row['train_loss'], row['test_loss'], row['test_accuracy']) for row in history),
    )
    _write_csv(
        context,
        'predictions.csv',
        ['sample_index', 'true_label', 'predicted_label', 'confidence'],
        ((index, true, predicted, confidence) for index, (true, predicted, confidence) in enumerate(predictions)),
    )
    _write_csv(
        context,
        'confusion_matrix.csv',
        ['true_label'] + [f'predicted_{label}' for label in range(10)],
        ((label, *confusion[label].tolist()) for label in range(10)),
    )
    _plot_history(history, context)
    _plot_confusion(confusion, context)
    _save_checkpoint(context, model, summary)
    return {'summary': summary}
