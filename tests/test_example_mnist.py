"""CPU-sized, meaningful checks for the real PyTorch MNIST example.

The integration run against the server's registered MNIST dataset is owned by
the top-level build/test workflow.  These tests deliberately use independent
temporary IDX train/test files and never touch shared datasets or workspaces.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import struct

import pytest

np = pytest.importorskip('numpy', reason='MNIST example needs NumPy')


torch = pytest.importorskip('torch', reason='MNIST example needs PyTorch')
pytest.importorskip('matplotlib', reason='MNIST example writes plot artifacts')


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'three-cases' / 'mnist-cnn'


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def plugin():
    return _load_module(EXAMPLE / 'algorithms' / 'cnn' / 'plugin.py', 'rk_example_mnist_plugin')


@pytest.fixture(scope='module')
def case():
    return _load_module(EXAMPLE / 'case.py', 'rk_example_mnist_case')


def _digit_image(label: int, variant: int) -> bytes:
    """Create a small, separable digit-like image without duplicating samples."""
    value = np.zeros((28, 28), dtype=np.uint8)
    column = 2 + label * 2
    row = 3 + ((label + variant) % 20)
    value[4:24, column:column + 2] = 255
    value[row:row + 2, 4:24] = 255
    value[24 - (label % 5):26 - (label % 5), 3 + variant % 3:5 + variant % 3] = 128
    return value.tobytes()


def _idx_images(rows: list[bytes]) -> bytes:
    return struct.pack('>IIII', 2051, len(rows), 28, 28) + b''.join(rows)


def _idx_labels(labels: list[int]) -> bytes:
    return struct.pack('>II', 2049, len(labels)) + bytes(labels)


def _mnist_files(*, train_each: int = 3, test_each: int = 2) -> dict[str, bytes]:
    train_labels = [label for label in range(10) for _ in range(train_each)]
    test_labels = [label for label in range(10) for _ in range(test_each)]
    # The test split intentionally uses a different variant range and bytes.
    train_images = [_digit_image(label, index) for index, label in enumerate(train_labels)]
    test_images = [_digit_image(label, 100 + index) for index, label in enumerate(test_labels)]
    assert b''.join(train_images) != b''.join(test_images)
    return {
        'raw/train-images-idx3-ubyte': _idx_images(train_images),
        'raw/train-labels-idx1-ubyte': _idx_labels(train_labels),
        'raw/t10k-images-idx3-ubyte': _idx_images(test_images),
        'raw/t10k-labels-idx1-ubyte': _idx_labels(test_labels),
    }


class _DatasetContext:
    def __init__(self, root: Path, files: dict[str, bytes]) -> None:
        self.root = root
        self.files = files
        self.dataset_calls: list[tuple[str, str, str]] = []
        self.parameters = {
            'data_source': 'dataset',
            'dataset_id': 'test-mnist-v1',
            'data_root': '',
        }

    def open_dataset(self, dataset_id: str, path: str, *, mode: str = 'rb'):
        self.dataset_calls.append((dataset_id, path, mode))
        assert mode == 'rb'
        return io.BytesIO(self.files[path])

    def result_path(self, name: str) -> Path:
        path = self.root / 'results' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_path(self, name: str) -> Path:
        path = self.root / 'artifacts' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def atomic_write_json(self, path: Path, value) -> None:
        Path(path).write_text(json.dumps(value, sort_keys=True), encoding='utf-8')

    def atomic_write_text(self, path: Path, value: str) -> None:
        Path(path).write_text(value, encoding='utf-8')


def _parameters(*, epochs: int = 2) -> dict:
    return {
        'epochs': epochs,
        'learning_rate': 0.01,
        'batch_size': 10,
        'train_limit': 30,
        'test_limit': 20,
        'seed': 17,
        'device': 'cpu',
        'num_workers': 0,
    }


def test_plugin_trains_convolution_and_keeps_train_test_io_separate(tmp_path, plugin):
    torch.set_num_threads(1)
    context = _DatasetContext(tmp_path, _mnist_files())
    parameters = _parameters()

    # With the same seed, this is exactly the first convolutional layer before
    # the algorithm's backward/optimizer updates.
    plugin._seed_everything(parameters['seed'])
    initial = plugin._MnistCnn().state_dict()['features.0.weight'].clone()

    output = plugin.run({}, parameters, context)
    summary = output['summary']

    assert set(output) == {'summary'}
    assert summary['train_samples'] == 30
    assert summary['test_samples'] == 20
    assert summary['device'] == 'cpu'
    assert 0.0 <= summary['accuracy'] <= 1.0
    assert context.dataset_calls == [
        ('test-mnist-v1', 'raw/train-images-idx3-ubyte', 'rb'),
        ('test-mnist-v1', 'raw/train-labels-idx1-ubyte', 'rb'),
        ('test-mnist-v1', 'raw/t10k-images-idx3-ubyte', 'rb'),
        ('test-mnist-v1', 'raw/t10k-labels-idx1-ubyte', 'rb'),
    ]

    checkpoint = torch.load(context.artifact_path('checkpoint.pt'), map_location='cpu', weights_only=False)
    trained = checkpoint['model_state_dict']['features.0.weight']
    assert not torch.equal(initial, trained), '卷积层必须经过反向传播更新'

    metrics = json.loads(context.result_path('metrics.json').read_text(encoding='utf-8'))
    assert len(metrics['history']) == 2
    confusion = np.loadtxt(context.result_path('confusion_matrix.csv'), delimiter=',', skiprows=1, usecols=range(1, 11))
    assert int(confusion.sum()) == 20
    predictions = context.result_path('predictions.csv').read_text(encoding='utf-8').strip().splitlines()
    assert len(predictions) == 21
    for path in (
        context.artifact_path('training_curve.png'),
        context.artifact_path('confusion_matrix.png'),
        context.artifact_path('checkpoint.pt'),
    ):
        assert path.is_file() and path.stat().st_size > 0


def test_case_execute_uses_empty_plugin_input_and_writes_summary(tmp_path, case):
    expected = {'accuracy': 0.75, 'test_samples': 4, 'device': 'cpu'}

    class Context:
        def __init__(self) -> None:
            self.calls = []

        def call(self, alias: str, *, inputs: dict):
            self.calls.append((alias, inputs))
            return {'summary': expected}

        def result_path(self, name: str) -> Path:
            return tmp_path / 'results' / name

        def atomic_write_json(self, path: Path, value) -> None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(value), encoding='utf-8')

    context = Context()
    assert case.execute(context) == expected
    assert context.calls == [('cnn', {})]
    assert json.loads((tmp_path / 'results' / 'summary.json').read_text(encoding='utf-8')) == expected


def test_local_mode_requires_explicit_root_and_reads_readonly_idx(tmp_path, plugin):
    raw = tmp_path / 'mnist' / 'raw'
    raw.mkdir(parents=True)
    for name, content in _mnist_files(train_each=1, test_each=1).items():
        (tmp_path / 'mnist' / name).write_bytes(content)

    class Context:
        parameters = {'data_source': 'local', 'data_root': str(tmp_path / 'mnist'), 'dataset_id': ''}

        def open_dataset(self, *_args, **_kwargs):
            raise AssertionError('local 模式不能调用注册数据集接口')

    images, labels = plugin._load_split(Context(), split='train', limit=10)
    assert images.shape == (10, 1, 28, 28)
    assert labels.tolist() == list(range(10))

    Context.parameters = {'data_source': 'local', 'data_root': 'relative-path', 'dataset_id': ''}
    with pytest.raises(ValueError, match='绝对路径'):
        plugin._load_split(Context(), split='test', limit=10)
