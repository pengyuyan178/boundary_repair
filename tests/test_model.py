"""Real loopback HTTP transport tests with a local fake provider, not real model performance."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context
from boundary_repair.adapters.model import FrozenModelAdapter, chat_endpoint, read_model_environment, _public_address
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.ports import ModelRequest


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = config(self.root, parser=False)
        self.config.env_file.parent.mkdir(parents=True)
        self.payload = {'id':'test-request','choices':[{'finish_reason':'stop','message':{'content':'{"ok": true}'}}],
                        'usage':{'completion_tokens':7}}
        self.http_status = 200
        self.calls = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path, body, self.headers.get('Authorization')))
                self.send_response(owner.http_status)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())
            def log_message(self, format, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config.env_file.write_text(f'MODEL_NAME=test-model\nMODEL_BASE_URL=http://127.0.0.1:{self.server.server_port}/v1\nMODEL_API_KEY=local-dummy\n', encoding='utf-8')
        self.config = replace(self.config, integration=replace(self.config.integration, allow_local_http=True))
        self.request = ModelRequest('Return JSON only.', 'A synthetic request', (), 'test.v1', 32)

    def test_actual_http_serialization_usage_and_seed(self):
        ctx = context()
        result = FrozenModelAdapter(self.config).complete(self.request, ctx)
        self.assertEqual(json.loads(result.text), {'ok':True})
        self.assertEqual((ctx.budget.model_calls, ctx.budget.output_tokens), (1, 7))
        path, body, authorization = self.calls[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body['max_completion_tokens'], 32)
        self.assertEqual(body['seed'], 42)
        self.assertEqual(authorization, 'Bearer local-dummy')
        self.assertNotIn('local-dummy', repr(result))

    def test_http_failure_has_no_hidden_retry(self):
        self.http_status = 429
        ctx = context()
        with self.assertRaisesRegex(ExternalServiceError, '429'):
            FrozenModelAdapter(self.config).complete(self.request, ctx)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ctx.budget.model_calls, 1)

    def test_missing_usage_charges_reservation_and_fails(self):
        del self.payload['usage']
        ctx = context()
        with self.assertRaisesRegex(ExternalServiceError, 'usage_missing'):
            FrozenModelAdapter(self.config).complete(self.request, ctx)
        self.assertEqual(ctx.budget.output_tokens, 32)

    def test_truncated_response_not_accepted(self):
        self.payload['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ExternalServiceError):
            FrozenModelAdapter(self.config).complete(self.request, context())
        self.assertEqual(len(self.calls), 1)

    def test_http_not_enabled_by_default(self):
        with self.assertRaises(ConfigurationError):
            chat_endpoint('http://127.0.0.1/v1', False)
        with self.assertRaises(ConfigurationError):
            chat_endpoint('http://example.com/v1', True)

    def test_env_does_not_execute_shell_text(self):
        sentinel = self.root / 'owned'
        self.config.env_file.write_text(f'MODEL_NAME=$(touch {sentinel})\nMODEL_BASE_URL=https://example.invalid/v1\nMODEL_API_KEY=x\n')
        values = read_model_environment(self.config)
        self.assertTrue(values['MODEL_NAME'].startswith('$('))
        self.assertFalse(sentinel.exists())

    def test_duplicate_requested_env_rejected(self):
        with self.config.env_file.open('a') as f:
            f.write('MODEL_NAME=other\n')
        with self.assertRaises(ConfigurationError):
            read_model_environment(self.config)

    def test_private_asset_dns_rejected(self):
        import socket
        with patch('socket.getaddrinfo', return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValidationError):
                _public_address('example.invalid', 443)

    def test_api_userinfo_not_accepted(self):
        with self.assertRaises(ConfigurationError):
            chat_endpoint('https://key@example.com/v1',False)

if __name__ == '__main__':
    unittest.main()
