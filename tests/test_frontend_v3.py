"""Bounded per-file optional semantic-analysis tests; no model or benchmark invocation."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from helpers import config, context, parser_module
from boundary_repair.adapters.frontend import analyze_sources
from boundary_repair.adapters.process import ProcessResult
from boundary_repair.domain.errors import BudgetExceeded, ExternalServiceError


class OptionalFrontendAnalysisTests(unittest.TestCase):
    def _config(self, root: Path):
        return config(root)

    @staticmethod
    def _parser_result(file: dict[str, str]) -> ProcessResult:
        payload = {'parser': 'typescript', 'version': '5.8.3', 'files': [{
            'path': file['path'], 'locations': [], 'functions': [], 'identifiers': ['shown'],
            'semantic_hash': 'a' * 64, 'consumer_guards': [], 'unsupported': [],
        }]}
        return ProcessResult(0, json.dumps(payload).encode(), b'')

    def test_multiple_selected_files_are_sent_in_separate_requests(self):
        selected = [{'path': 'first.ts', 'source': 'export const first = true;'},
                    {'path': 'second.ts', 'source': 'export const second = false;'}]
        requests = []

        def parse_once(arguments, **kwargs):
            request = json.loads(kwargs['input_data'])
            requests.append(request)
            self.assertEqual(kwargs['max_output'], 8 * 1024 * 1024)
            self.assertLessEqual(kwargs['timeout'], 90)
            return self._parser_result(request['files'][0])

        with TemporaryDirectory() as temporary, \
             patch('boundary_repair.adapters.frontend.resolve_node', return_value='node'), \
             patch('boundary_repair.adapters.frontend.run_process', side_effect=parse_once):
            result = analyze_sources(selected, self._config(Path(temporary)), context())

        self.assertEqual([[item['path'] for item in request['files']] for request in requests],
                         [['first.ts'], ['second.ts']])
        self.assertEqual([row['analysis_status'] for row in result['files']], ['complete', 'complete'])

    def test_single_failure_keeps_other_selected_file_available_to_text_path(self):
        selected = [{'path': 'good.ts', 'source': 'export const good = true;'},
                    {'path': 'failed.ts', 'source': 'export const failed = false;'}]

        def parse_or_fail(arguments, **kwargs):
            file = json.loads(kwargs['input_data'])['files'][0]
            if file['path'] == 'failed.ts':
                raise ExternalServiceError('process_output_limit')
            return self._parser_result(file)

        with TemporaryDirectory() as temporary, \
             patch('boundary_repair.adapters.frontend.resolve_node', return_value='node'), \
             patch('boundary_repair.adapters.frontend.run_process', side_effect=parse_or_fail):
            result = analyze_sources(selected, self._config(Path(temporary)), context())

        self.assertEqual([row['path'] for row in result['files']], ['good.ts', 'failed.ts'])
        self.assertEqual(result['files'][0]['identifiers'], ['shown'])
        self.assertEqual(result['files'][1]['analysis_status'], 'unavailable')
        self.assertEqual(result['files'][1]['unsupported'], ['analysis_output_limit'])
        self.assertEqual(selected[0]['source'], 'export const good = true;')

    def test_budget_exceeded_is_not_converted_to_partial_analysis(self):
        selected = [{'path': 'ui.ts', 'source': 'export const visible = true;'}]
        with TemporaryDirectory() as temporary, \
             patch('boundary_repair.adapters.frontend.resolve_node', return_value='node'), \
             patch('boundary_repair.adapters.frontend.run_process', side_effect=BudgetExceeded('process_timeout')):
            with self.assertRaises(BudgetExceeded):
                analyze_sources(selected, self._config(Path(temporary)), context())


@unittest.skipUnless(parser_module(), 'install pinned TypeScript 5.8.3 for frontend parser tests')
class TypeScriptFrontendAnalysisTests(unittest.TestCase):
    def test_typescript_583_parses_simple_fixture(self):
        with TemporaryDirectory() as temporary:
            result = analyze_sources([{'path': 'visible.ts',
                                       'source': 'export function visible(active: boolean) { return active; }'}],
                                     config(Path(temporary)), context())
        row = result['files'][0]
        self.assertEqual(result['parser'], 'typescript')
        self.assertEqual(result['version'], '5.8.3')
        self.assertEqual(row['analysis_status'], 'complete')
        self.assertTrue(any(item['name'] == 'visible' for item in row['functions']))
        self.assertTrue(any(item['node_kind'] == 'BooleanReturn' for item in row['locations']))

    def test_location_limit_is_explicit_partial_diagnostic(self):
        source = '\n'.join(f'const value{index} = {index};' for index in range(4097))
        with TemporaryDirectory() as temporary:
            result = analyze_sources([{'path': 'many.ts', 'source': source}], config(Path(temporary)), context())
        row = result['files'][0]
        self.assertLessEqual(len(row['locations']), 4096)
        self.assertIn('analysis_location_limit', row['unsupported'])
        self.assertEqual(row['analysis_status'], 'partial')

    def test_existing_syntax_diagnostics_have_unknown_semantics(self):
        with TemporaryDirectory() as temporary:
            result = analyze_sources([{'path': 'broken.ts', 'source': 'export const broken = ;'}],
                                     config(Path(temporary)), context())
        row = result['files'][0]
        self.assertIsNone(row['semantic_hash'])
        self.assertEqual(row['analysis_status'], 'partial')
        self.assertTrue(any(item.startswith('syntax:') for item in row['unsupported']))


if __name__ == '__main__':
    unittest.main()