"""Function-level checks for bounded retrieval and explicitly simulated external service edges."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, snapshot
from boundary_repair.adapters.frontend import retrieval_score, source_context
from boundary_repair.adapters.model import PinnedHTTPSConnection, prepare_asset
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.domain.task import IssueAsset
from boundary_repair.experiments.evaluation import EvaluationReport, run_evaluation
from boundary_repair.kernel.terms import boolean_environments


class RemainingAdapterTests(unittest.TestCase):
    def test_source_fingerprint_includes_frontend(self):
        from boundary_repair.adapters.storage import source_fingerprint
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'core.py').write_text('x = 1')
            (root / 'parse.cjs').write_text('module.exports = 1')
            first = source_fingerprint(root)
            (root / 'parse.cjs').write_text('module.exports = 2')
            self.assertNotEqual(first, source_fingerprint(root))

    def test_source_context_honors_character_budget(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = config(root, parser=False)
            conf = replace(conf, integration=replace(conf.integration, max_context_chars=17, context_files=1))
            result = source_context(snapshot(root), 'shouldShow ui', conf)
            self.assertEqual(sum(len(row['source']) for row in result), 17)
            self.assertGreater(retrieval_score('ui hidden', 'ui.js', 'hidden'), 0)
            self.assertEqual(result[0]['truncated'], 'True')

    def test_complete_boolean_environments_and_limit(self):
        values = boolean_environments(('a', 'b'))
        self.assertEqual(len(values), 4)
        self.assertEqual(values[0], {'a': False, 'b': False})
        with self.assertRaises(ValidationError):
            boolean_environments(tuple(str(i) for i in range(9)))

    def test_pinned_connection_uses_original_tls_hostname(self):
        connection = PinnedHTTPSConnection('images.example.com', '93.184.216.34', 4)
        connection._context = MagicMock()
        with patch('socket.create_connection') as tcp:
            connection.connect()
            tcp.assert_called_once_with(('93.184.216.34', 443), timeout=4)
            connection._context.wrap_socket.assert_called_once_with(tcp.return_value, server_hostname='images.example.com')
        connection.close()

    def test_pinned_connection_closes_socket_on_tls_failure(self):
        connection = PinnedHTTPSConnection('images.example.com', '93.184.216.34', 4)
        connection._context = MagicMock()
        connection._context.wrap_socket.side_effect = OSError('simulated certificate failure')
        with patch('socket.create_connection') as tcp:
            with self.assertRaises(OSError):
                connection.connect()
            tcp.return_value.close.assert_called_once()

    def test_image_transport_conversion_and_hash_with_simulated_tls(self):
        data = b'\x89PNG\r\n\x1a\nsynthetic-payload-not-a-real-decoded-image'
        with TemporaryDirectory() as temporary:
            conf = config(Path(temporary), parser=False)
            with patch('boundary_repair.adapters.model._public_address', return_value='93.184.216.34'), \
                 patch('boundary_repair.adapters.model.PinnedHTTPSConnection') as transport:
                response = transport.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = data
                response.getheader.return_value = 'image/png'
                uri, digest = prepare_asset(IssueAsset('https://images.example.com/a.png', 'image-1'), conf, context())
                self.assertTrue(uri.startswith('data:image/png;base64,'))
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                transport.return_value.close.assert_called_once()

    def test_image_redirect_and_oversize_fail_closed(self):
        with TemporaryDirectory() as temporary:
            conf = config(Path(temporary), parser=False)
            conf = replace(conf, integration=replace(conf.integration, max_asset_bytes=8))
            with patch('boundary_repair.adapters.model._public_address', return_value='93.184.216.34'), \
                 patch('boundary_repair.adapters.model.PinnedHTTPSConnection') as transport:
                response = transport.return_value.getresponse.return_value
                response.status = 302
                with self.assertRaises(ExternalServiceError):
                    prepare_asset(IssueAsset('https://images.example.com/a.png', 'image-1'), conf, context())
                response.status = 200
                response.read.return_value = b'x' * 9
                with self.assertRaises(ValidationError):
                    prepare_asset(IssueAsset('https://images.example.com/a.png', 'image-1'), conf, context())

    def test_evaluation_wrapper_builds_explicit_request_without_feedback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = config(root, parser=False)
            image_manifest = root / 'images.json'
            image_manifest.write_text('{}')
            conf = replace(conf, target='server', isolation='docker', image_manifest=image_manifest,
                           harness_python=Path(sys.executable), harness_revision='version:explicit-test')
            batch = conf.results_root / 'batch'
            batch.mkdir(parents=True)
            (batch / 'predictions.jsonl').write_text('{}\n')
            grader = MagicMock()
            grader.evaluate.return_value = EvaluationReport('example', 1, 0, 0, 1, 1)
            result = run_evaluation(conf, batch, 'example', grader)
            self.assertEqual(result.graded, 0)
            request = grader.evaluate.call_args.args[0]
            self.assertEqual(request.harness_revision, 'version:explicit-test')
            self.assertEqual(request.predictions, batch / 'predictions.jsonl')
            with self.assertRaises(ConfigurationError):
                run_evaluation(replace(conf, target='local'), batch, 'example', grader)


if __name__ == '__main__':
    unittest.main()
