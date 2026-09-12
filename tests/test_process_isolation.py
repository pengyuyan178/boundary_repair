"""Process boundaries, answer-free inputs and grading completeness on synthetic fixtures."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from boundary_repair.adapters.dataset import load_generation_tasks
from boundary_repair.adapters.storage import write_json
from boundary_repair.domain.errors import ConfigurationError, DatasetFormatError, ValidationError
from boundary_repair.experiments.evaluation import parse_case_reports
from boundary_repair.experiments.runner import run_generation
from helpers import config

SPEC = importlib.util.spec_from_file_location('isolated_layers', ROOT / 'scripts/run_layers.py')
layers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(layers)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_preparation_exports_only_public_fields_in_fixed_order(self):
        raw = self.root / 'answers.json'
        rows = [{'instance_id': f'demo__ui-{i}', 'repo': 'demo/ui', 'base_commit': 'a'*40,
                 'problem_statement': 'Synthetic input', 'patch': 'PRIVATE_GOLD_SENTINEL',
                 'test_patch': 'PRIVATE_TEST_SENTINEL', 'FAIL_TO_PASS': ['PRIVATE_TEST_NAME'],
                 'image': 'demo/image:latest', 'image_assets': {
                     'problem_statement': ['https://example.invalid/issue.png'],
                     'test_patch': ['https://example.invalid/answer.png']}} for i in range(10)]
        write_json(raw, rows)
        one, two = self.root / 'one', self.root / 'two'
        first = layers.prepare_inputs(raw, one, 'dev', '101')
        second = layers.prepare_inputs(raw, two, 'dev', '101')
        self.assertEqual(first['selected_instances'], second['selected_instances'])
        self.assertEqual(first['modules'], '101')
        text = (one / 'tasks.json').read_text()
        self.assertNotIn('PRIVATE_', text)
        self.assertNotIn('answer.png', text)
        raw.unlink()
        tasks, manifest = layers.read_inputs(one)
        self.assertEqual(len(tasks), 10)
        self.assertEqual(manifest['seed'], 42)

    def test_public_reader_rejects_answers_in_task_or_asset(self):
        task = {'instance_id': 'demo__ui-1', 'repo': 'demo/ui', 'base_commit': 'a'*40,
                'problem_statement': 'Synthetic issue', 'assets': []}
        data = self.root / 'tasks.json'
        for field in ('patch', 'test_patch', 'hints_text', 'FAIL_TO_PASS', 'PASS_TO_PASS'):
            with self.subTest(field=field):
                write_json(data, [{**task, field: 'PRIVATE'}])
                with self.assertRaises(DatasetFormatError):
                    load_generation_tasks(data)
        task['assets'] = [{'uri': 'https://example.invalid/x.png', 'source_id': 'issue-0',
                           'media_type': None, 'patch': 'PRIVATE'}]
        write_json(data, [task])
        with self.assertRaises(DatasetFormatError):
            load_generation_tasks(data)

    def test_server_generation_is_blocked_before_inputs_or_output(self):
        settings = replace(config(self.root), target='server')
        with patch.dict('os.environ', {'BOUNDARY_GENERATION_ROLE': ''}):
            with patch('boundary_repair.experiments.runner.BatchStore.create') as create:
                with self.assertRaisesRegex(ConfigurationError, 'isolated_worker'):
                    run_generation(settings, (), 'test', None, None)
                create.assert_not_called()

    def test_worker_has_no_host_or_answer_mounts(self):
        with patch.object(layers.os, 'getuid', return_value=1000, create=True), \
             patch.object(layers.os, 'getgid', return_value=1000, create=True):
            args = layers.worker_command('sha256:runtime', ROOT, self.root/'input',
                self.root/'base', self.root/'output', 'synthetic-worker', 'generate', 'synthetic')
        mounts = [args[i+1] for i, arg in enumerate(args) if arg == '--mount']
        self.assertEqual(len(mounts), 6)
        self.assertEqual(sum(',readonly' in mount for mount in mounts), 5)
        self.assertFalse(any('docker.sock' in item or 'dataset' in item for item in mounts))
        self.assertIn('--read-only', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertNotIn('--network=host', args)

    def test_actual_container_audit_rejects_extra_mount(self):
        path = self.root/'output'
        details = {'Image': 'sha256:runtime', 'Config': {'User': '1000:1000'},
                   'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True,
                       'NetworkMode': 'none', 'PidMode': '', 'IpcMode': 'private',
                       'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges']},
                   'Mounts': [{'Type': 'bind', 'Destination': '/output',
                               'Source': str(path.resolve()), 'RW': True}]}
        layers.container_audit(details, 'sha256:runtime', {'/output': path})
        details['Mounts'].append({'Type': 'bind', 'Destination': '/answers', 'Source': '/dataset', 'RW': False})
        with self.assertRaises(ValidationError):
            layers.container_audit(details, 'sha256:runtime', {'/output': path})


class GradingCompletenessTests(unittest.TestCase):
    def check_grade(self, body, code=0, resolved=True, chart=True, include_end=True):
        with TemporaryDirectory() as name:
            root = Path(name)
            iid = 'chartjs__Chart.js-123' if chart else 'demo__ui-1'
            write_json(root/'report.json', {iid: {'resolved': resolved}})
            text = '>>>>> Start Test Output\n' + body + '\n'
            if include_end:
                text += f'>>>>> End Test Output\n>>>>> Test Exit Code: {code}\n'
            (root/'test_output.txt').write_text(text)
            return parse_case_reports(root, (iid,))

    def test_incomplete_chart_success_is_not_a_grade(self):
        self.assertEqual(self.check_grade('Chrome Headless 1: Executed 47 of 1545 DISCONNECTED', 1), {})

    def test_both_browsers_must_finish(self):
        self.assertEqual(self.check_grade('Chrome Headless 1: Executed 10 of 10 SUCCESS'), {})

    def test_complete_browser_failure_is_valid_unresolved(self):
        body = 'Chrome Headless 1: Executed 10 of 10 (1 FAILED)\nFirefox 1: Executed 10 of 10 (1 FAILED)'
        self.assertEqual(self.check_grade(body, 1, False), {'chartjs__Chart.js-123': False})

    def test_complete_browser_success_is_retained(self):
        body = 'Chrome Headless 1: Executed 10 of 10 SUCCESS\nFirefox 1: Executed 10 of 10 SUCCESS'
        self.assertEqual(self.check_grade(body), {'chartjs__Chart.js-123': True})

    def test_disconnection_after_last_test_is_rejected(self):
        body = 'Chrome 1: Executed 10 of 10\nFirefox 1: Executed 10 of 10 DISCONNECTED'
        self.assertEqual(self.check_grade(body), {})

    def test_missing_end_or_nonzero_success_is_rejected(self):
        self.assertEqual(self.check_grade('passed', chart=False, include_end=False), {})
        self.assertEqual(self.check_grade('passed', code=1, chart=False), {})

    def test_missing_log_is_not_a_grade(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            write_json(root/'report.json', {'demo__ui-1': {'resolved': True}})
            self.assertEqual(parse_case_reports(root, ('demo__ui-1',)), {})


if __name__ == '__main__':
    unittest.main()
