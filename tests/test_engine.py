from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from researchkit import engine, model
from researchkit.files import KitError, write_json


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'sample'
        self.root.mkdir()
        self.plugin = self.root / 'algorithms' / 'scale'
        self.plugin.mkdir(parents=True)
        (self.plugin / 'helper.py').write_text('def scale(x, factor):\n    return x * factor\n')
        (self.plugin / 'plugin.py').write_text(
            'from .helper import scale\n'
            'def run(inputs, parameters, context):\n'
            '    return {"value": scale(inputs["value"], parameters["factor"])}\n')
        self._yaml(self.plugin / 'plugin.yaml', {
            'schema': 'hydro.plugin/v1', 'name': 'scale', 'version': '1.0.0',
            'entry': 'plugin.py', 'inputs': {'value': 'number@1.0.0'},
            'outputs': {'value': 'number@1.0.0'},
            'parameters': {'factor': {'type': 'number', 'required': True, 'minimum': 0}},
        })
        contract = self.root / '.researchkit' / 'contracts' / 'number' / '1.0.0.yaml'
        self._yaml(contract, {'schema': 'hydro.contract/v1', 'id': 'number@1.0.0', 'type': 'number'})
        self.case = self.root / 'case.py'
        self.case.write_text(
            'def run(context):\n'
            '    out = context.call("scale", inputs={"value": context.parameters["base"]})\n'
            '    context.atomic_write_json(context.result_path("summary.json"), out)\n'
            '    context.atomic_write_text(context.report_path("report.md"), "done")\n'
            '    return out\n')
        self.doc = {'schema': 'researchkit.project/v1', 'name': 'sample', 'mode': 'python',
                    'entry': 'case.py', 'parameters': {'base': 3},
                    'plugins': {'scale': {'path': 'algorithms/scale', 'parameters': {'factor': 2}}},
                    'data': {}, 'requires': {'python': '3.11', 'modules': []}}
        self.save()

    def _yaml(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(value))

    def save(self):
        self._yaml(self.root / 'project.yaml', self.doc)
        write_json(self.root / '.researchkit' / 'lock.json',
                   {'schema': 'researchkit.lock/v1', 'files': model.protected_files(self.root)})

    def test_legacy_context_and_multifile_plugin(self):
        record = engine.run_project(self.root)
        self.assertEqual(record['status'], 'success')
        self.assertEqual(record['result'], {'value': 6})
        self.assertEqual(model.freshness(self.root)['status'], 'success')
        self.assertTrue((self.root / '_runs' / record['run_id'] / 'console.log').exists())

    def test_src_relative_import_and_plugin_namespaces(self):
        source = self.plugin / 'src' / 'hydro_demo'
        source.mkdir(parents=True)
        (source / '__init__.py').write_text('')
        (source / 'backend.py').write_text('def scale(x, factor):\n    return x * factor\n')
        (self.plugin / 'plugin.py').write_text(
            'from .hydro_demo.backend import scale\n'
            'calls = 0\n'
            'def run(inputs, parameters, context):\n'
            '    global calls\n'
            '    calls += 1\n'
            '    return {"value": scale(inputs["value"], parameters["factor"]) + calls}\n')
        other = self.root / 'algorithms' / 'other'
        other.mkdir()
        shutil.copy2(self.plugin / 'plugin.yaml', other / 'plugin.yaml')
        (other / 'helper.py').write_text('def scale(x, factor):\n    return x + 100\n')
        (other / 'plugin.py').write_text(
            'from .helper import scale\n'
            'def run(inputs, parameters, context):\n'
            '    return {"value": scale(inputs["value"], parameters["factor"])}\n')
        self.doc['plugins']['other'] = {'path': 'algorithms/other', 'parameters': {'factor': 2}}
        code = self.root / 'code'
        code.mkdir()
        (code / 'case_helper.py').write_text('def base():\n    return 3\n')
        self.case.write_text(
            'from case_helper import base\n'
            'def run(context):\n'
            '    a = context.call("scale", inputs={"value": base()})["value"]\n'
            '    b = context.call("other", inputs={"value": base()})["value"]\n'
            '    c = context.call("scale", inputs={"value": base()})["value"]\n'
            '    return {"first": a, "other": b, "again": c}\n')
        self.save()
        record = engine.run_project(self.root)
        self.assertEqual(record['result'], {'first': 7, 'other': 103, 'again': 8})

    def test_fixed_dependency_changed_during_run_fails(self):
        contract = self.root / '.researchkit' / 'contracts' / 'number' / '1.0.0.yaml'
        self.case.write_text(
            'def run(context):\n'
            '    path = context.case_dir / ".researchkit/contracts/number/1.0.0.yaml"\n'
            '    path.write_text(path.read_text() + "# tampered\\n")\n'
            '    return {"value": 6}\n')
        with self.assertRaisesRegex(KitError, '固定依赖发生变化'):
            engine.run_project(self.root)
        record = json.loads((self.root / '.researchkit' / 'last-run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertIn('固定依赖', record['error'])

    def test_copy_and_same_content_preserve_freshness(self):
        engine.run_project(self.root)
        relocated = self.root.parent / 'relocated'
        shutil.copytree(self.root, relocated, copy_function=shutil.copy2)
        self.assertEqual(model.freshness(relocated)['status'], 'success')
        for rel in ('case.py', 'results/summary.json'):
            path = relocated / rel
            path.write_bytes(path.read_bytes())
        self.assertEqual(model.freshness(relocated)['status'], 'success')
        (relocated / 'results' / 'summary.json').write_text('{"value":999}')
        self.assertEqual(model.freshness(relocated)['status'], 'stale')

    def test_static_checks_do_not_import_code(self):
        self.case.write_text('from pathlib import Path\nPath(__file__).with_suffix(".loaded").write_text("yes")\n' + self.case.read_text())
        model.project(self.root)
        model.doctor(self.root)
        self.assertFalse(self.case.with_suffix('.loaded').exists())
        engine.run_project(self.root)
        self.assertTrue(self.case.with_suffix('.loaded').exists())

    def test_port_error_records_failure_and_clears_success(self):
        engine.run_project(self.root)
        self.case.write_text('def run(context):\n    return context.call("scale", inputs={"wrong": 3})\n')
        with self.assertRaisesRegex(KitError, '端口不匹配'):
            engine.run_project(self.root)
        record = json.loads((self.root / '.researchkit' / 'last-run.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['result'])
        self.assertEqual(model.freshness(self.root)['status'], 'failed')

    def test_code_and_outputs_become_stale(self):
        engine.run_project(self.root)
        self.case.write_text(self.case.read_text() + '\n# change\n')
        self.assertEqual(model.freshness(self.root)['status'], 'stale')
        engine.run_project(self.root)
        (self.root / 'results' / 'summary.json').write_text('{"value":999}')
        self.assertEqual(model.freshness(self.root)['status'], 'stale')

    def test_local_mapping_and_external_data_become_stale(self):
        data = self.root.parent / 'raw'
        data.mkdir()
        (data / 'measure.txt').write_text('one')
        write_json(self.root / '.researchkit' / 'local.json', {'data_root': str(data)})
        engine.run_project(self.root)
        (data / 'measure.txt').write_text('two')
        self.assertEqual(model.freshness(self.root)['status'], 'stale')
        record = json.loads((self.root / '.researchkit' / 'last-run.json').read_text())
        self.assertNotIn(str(data), json.dumps(record))

    def test_path_protection_and_parameter_errors(self):
        ctx = engine.Context(self.root, self.doc)
        with self.assertRaises(KitError):
            ctx.atomic_write_text(self.root / 'case.py', 'bad')
        with self.assertRaises(KitError):
            ctx.result_path('../case.py')
        self.doc['plugins']['scale']['parameters']['factor'] = 'two'
        self.save()
        with self.assertRaisesRegex(KitError, '应为 number'):
            model.project(self.root)

    def test_lock_prevents_parallel_run(self):
        import fcntl
        lock = self.root / '.researchkit' / 'run.lock'
        with lock.open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(KitError, '已有运行任务'):
                engine.run_project(self.root)

    def test_command_mode_argv(self):
        self.doc['mode'] = 'command'
        self.doc['command'] = ['python', 'script.py', 'a;touch must_not_exist']
        self.doc.pop('entry')
        (self.root / 'script.py').write_text('import sys\nfrom pathlib import Path\nPath("results").mkdir(exist_ok=True)\nPath("results/argv.txt").write_text(sys.argv[1])\n')
        self.save()
        record = engine.run_project(self.root)
        self.assertEqual(record['status'], 'success')
        self.assertEqual((self.root / 'results' / 'argv.txt').read_text(), 'a;touch must_not_exist')
        self.assertFalse((self.root / 'must_not_exist').exists())


if __name__ == '__main__':
    unittest.main()
