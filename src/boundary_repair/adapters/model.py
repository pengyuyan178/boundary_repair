"""Single-call HTTP provider and explicitly labeled fixture mode. No SDK retries or shell env loading."""
from __future__ import annotations
import base64
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import IssueAsset
from boundary_repair.kernel.codec import strict_json
from boundary_repair.ports import ModelRequest, ModelResponse


def read_model_environment(config: ExperimentConfig) -> dict[str, str]:
    """Read only configured env names at call time; no expansion, execution or secret logging."""
    names = {config.model.name_env, config.model.endpoint_env, config.model.api_key_env}
    values: dict[str, str] = {}
    if config.env_file.is_file():
        for line in config.env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key not in names:
                continue
            if key in values:
                raise ConfigurationError('duplicate_model_env_name')
            if value[:1] in {'"', "'"}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError('malformed_quoted_env_value')
                value = value[1:-1]
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0]
            values[key] = value
    for name in names:
        if name in os.environ:
            values[name] = os.environ[name]
        if not values.get(name):
            raise ConfigurationError(f'missing_model_env:{name}')
    return values


def _public_address(host: str, port: int) -> str:
    """Resolve once and require every returned address to be globally routable."""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ExternalServiceError('asset_dns_failed') from exc
    addresses = sorted({r[4][0] for r in records})
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValidationError('private_asset_destination')
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS verifies the original hostname but connects to the already checked public IP."""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        """Keep a single resolved address to prevent a second DNS lookup/rebinding race."""
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        """Establish TCP to pinned IP, then verify TLS for the requested public hostname."""
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def prepare_asset(asset: IssueAsset, config: ExperimentConfig, context: RunContext) -> tuple[str, str]:
    """Download only direct public HTTPS images, no redirects; return data URI and content hash."""
    parsed = urlsplit(asset.uri)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValidationError('asset_requires_public_https')
    address = _public_address(parsed.hostname, 443)
    connection = PinnedHTTPSConnection(parsed.hostname, address, min(config.integration.http_timeout, context.budget.remaining_seconds()))
    try:
        target = parsed.path or '/'
        if parsed.query:
            target += '?' + parsed.query
        connection.request('GET', target, headers={'Accept': 'image/png,image/jpeg,image/webp,image/gif'})
        response = connection.getresponse()
        if response.status != 200:
            raise ExternalServiceError(f'asset_http_status:{response.status}')
        data = response.read(config.integration.max_asset_bytes + 1)
        if len(data) > config.integration.max_asset_bytes:
            raise ValidationError('asset_size_limit')
        mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else
                'image/gif' if data[:6] in {b'GIF87a', b'GIF89a'} else None)
        if mime is None or response.getheader('Content-Type', '').split(';')[0] not in {mime, 'application/octet-stream'}:
            raise ValidationError('asset_mime_or_signature_mismatch')
        context.budget.check_deadline()
        return f'data:{mime};base64,{base64.b64encode(data).decode()}', hashlib.sha256(data).hexdigest()
    except (OSError, http.client.HTTPException) as exc:
        raise ExternalServiceError('asset_transport_failed') from exc
    finally:
        connection.close()


def chat_endpoint(endpoint: str, allow_local_http: bool) -> tuple[str, str, int, str]:
    """Normalize an explicitly configured API base, never guessing a provider or model."""
    parsed = urlsplit(endpoint)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError('invalid_model_endpoint')
    if parsed.scheme != 'https':
        if not (allow_local_http and parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
            raise ConfigurationError('model_endpoint_requires_https')
    path = parsed.path.rstrip('/')
    if not path.endswith('/chat/completions'):
        path += '/chat/completions'
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), path


@dataclass(frozen=True, slots=True)
class FrozenModelAdapter:
    """Stateless provider; each task carries its own budget. Every failed request counts once."""
    config: ExperimentConfig

    def complete(self, request: ModelRequest, context: RunContext) -> ModelResponse:
        """Send one JSON-mode multimodal chat request and require accounted usage and normal stop.

        Structured output remains untrusted and is validated by each algorithm. Missing usage
        charges the entire reservation then fails. Fixture outputs are explicitly synthetic.
        """
        if self.config.integration.model_mode == 'fixture':
            return self._fixture(request, context)
        values = read_model_environment(self.config)
        scheme, host, port, path = chat_endpoint(values[self.config.model.endpoint_env], self.config.integration.allow_local_http)
        if len(request.assets) > self.config.integration.max_assets:
            raise ValidationError('asset_count_limit')
        content: list[dict[str, Any]] = [{'type': 'text', 'text': request.prompt}]
        asset_hashes: list[str] = []
        for asset in request.assets:
            uri, digest = prepare_asset(asset, self.config, context)
            content.append({'type': 'image_url', 'image_url': {'url': uri}})
            asset_hashes.append(digest)
        limit = context.budget.begin_model_call(request.max_output_tokens)
        body = {'model': values[self.config.model.name_env], 'messages': [
            {'role': 'system', 'content': request.system}, {'role': 'user', 'content': content}],
            'max_completion_tokens': limit, 'temperature': context.policy.temperature,
            'response_format': {'type': 'json_object'}, 'n': 1, 'stream': False, 'seed': context.seed}
        connection_class = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
        connection = connection_class(host, port=port, timeout=min(self.config.integration.http_timeout, context.budget.remaining_seconds()))
        try:
            connection.request('POST', path, body=json.dumps(body).encode(), headers={
                'Authorization': 'Bearer ' + values[self.config.model.api_key_env], 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                # No retries, no credential-bearing URLs or provider response body in the error.
                raise ExternalServiceError(f'model_http_status:{response.status}')
            raw = response.read(4000001)
            if len(raw) > 4000000:
                context.budget.record_output_tokens(limit)
                raise ExternalServiceError('model_response_size_limit')
            data = strict_json(raw.decode('utf-8'), maximum=4000000)
            usage = data.get('usage', {}).get('completion_tokens') if isinstance(data.get('usage'), dict) else None
            if type(usage) is not int or usage < 0:
                context.budget.record_output_tokens(limit)
                raise ExternalServiceError('provider_usage_missing')
            context.budget.record_output_tokens(usage)
            context.budget.check_deadline()
            choices = data.get('choices')
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
                raise ExternalServiceError('model_response_not_complete')
            text = choices[0].get('message', {}).get('content')
            if not isinstance(text, str) or not text.strip():
                raise ExternalServiceError('model_content_missing_or_refused')
            request_id = str(data.get('id', 'unavailable'))
            response_digest = hashlib.sha256(raw).hexdigest()
            # Metadata only: no hidden reasoning or authorization values are persisted here.
            return ModelResponse(text, usage, request_id + ':sha256:' + response_digest,
                                 tuple(asset_hashes), values[self.config.model.name_env])
        except (OSError, UnicodeDecodeError, http.client.HTTPException) as exc:
            raise ExternalServiceError('model_transport_failed') from exc
        finally:
            connection.close()

    def _fixture(self, request: ModelRequest, context: RunContext) -> ModelResponse:
        """Read a schema-keyed recorded response solely for explicit offline integration tests."""
        path = self.config.integration.fixture_file
        if path is None:
            raise ConfigurationError('fixture_file_missing')
        data = strict_json(path.read_text(encoding='utf-8'))
        responses = data.get('responses', {})
        if request.schema_name not in responses:
            raise ValidationError('fixture_schema_response_missing')
        text = json.dumps(responses[request.schema_name], ensure_ascii=False)
        estimated = max(1, len(text.encode()) // 4)
        maximum = context.budget.begin_model_call(request.max_output_tokens)
        if estimated > maximum:
            raise ValidationError('fixture_output_exceeds_budget')
        context.budget.record_output_tokens(estimated)
        return ModelResponse(text, estimated, 'fixture:' + hashlib.sha256(text.encode()).hexdigest(), (), 'fixture-not-a-model')
