"""Single-call HTTP provider and explicitly labeled fixture mode. No SDK retries or shell env loading."""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from boundary_repair.adapters.storage import safe_component, write_json
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import IssueAsset, TaskInput
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


def image_mime(data: bytes) -> str | None:
    """Identify the supported image signatures for downloads and cache reads."""
    return ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
            'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
            'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else
            'image/gif' if data[:6] in {b'GIF87a', b'GIF89a'} else None)


def _download_asset(asset: IssueAsset, config: ExperimentConfig, context: RunContext) -> tuple[str, str]:
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
        mime = image_mime(data)
        if mime is None or response.getheader('Content-Type', '').split(';')[0] not in {mime, 'application/octet-stream'}:
            raise ValidationError('asset_mime_or_signature_mismatch')
        context.budget.check_deadline()
        return f'data:{mime};base64,{base64.b64encode(data).decode()}', hashlib.sha256(data).hexdigest()
    except (OSError, http.client.HTTPException) as exc:
        raise ExternalServiceError('asset_transport_failed') from exc
    finally:
        connection.close()


def case_directory(config: ExperimentConfig, context: RunContext) -> Path:
    """Resolve the task-scoped artifact directory without provider secrets."""
    return config.results_root / safe_component(context.run_id) / 'cases' / safe_component(context.instance_id)


def prepare_asset(asset: IssueAsset, config: ExperimentConfig, context: RunContext) -> tuple[str, str]:
    """Reuse hash-checked original bytes and retry only bounded transient image transfers."""
    context.budget.check_deadline()
    key = hashlib.sha256(asset.uri.encode('utf-8')).hexdigest()
    root = case_directory(config, context) / 'assets'
    metadata_path, binary_path = root / (key + '.json'), root / (key + '.bin')
    if metadata_path.is_file():
        metadata = strict_json(metadata_path.read_text(encoding='utf-8'))
        if not binary_path.is_file():
            raise ValidationError('asset_cache_integrity_failed')
        data = binary_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if (not isinstance(metadata, dict) or digest != metadata.get('sha256')
                or len(data) != metadata.get('bytes') or len(data) > config.integration.max_asset_bytes
                or metadata.get('uri_sha256') != key or image_mime(data) is None
                or image_mime(data) != metadata.get('mime')):
            raise ValidationError('asset_cache_integrity_failed')
        return f"data:{metadata['mime']};base64,{base64.b64encode(data).decode()}", digest
    attempts = []
    for attempt in range(1, config.integration.asset_attempts + 1):
        context.budget.check_deadline()
        try:
            uri, digest = _download_asset(asset, config, context)
        except (ExternalServiceError, ValidationError) as exc:
            cause = exc.__cause__ or exc
            status = int(str(exc).split(':')[1]) if str(exc).startswith('asset_http_status:') else None
            retryable = (status in {408, 429, 500, 502, 503, 504}
                         or isinstance(cause, (TimeoutError, ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected))
                         or isinstance(cause, socket.gaierror) and cause.errno == socket.EAI_AGAIN)
            if isinstance(cause, ssl.SSLError) or isinstance(exc, ValidationError):
                retryable = False
            attempts.append({'attempt': attempt, 'status': 'failed', 'error_code': str(exc),
                             'cause_type': type(cause).__name__, 'errno': getattr(cause, 'errno', None),
                             'http_status': status, 'retryable': retryable})
            write_json(root / (key + '.attempts.json'), {'source_id': asset.source_id, 'attempts': attempts})
            if not retryable or attempt == config.integration.asset_attempts:
                raise
            delay = config.integration.asset_retry_delay * 2 ** (attempt - 1)
            time.sleep(min(delay, context.budget.remaining_seconds()))
            continue
        mime, encoded = uri.removeprefix('data:').split(';base64,', 1)
        data = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValidationError('asset_download_hash_mismatch')
        root.mkdir(parents=True, exist_ok=True)
        binary_path.write_bytes(data)
        write_json(metadata_path, {'source_id': asset.source_id, 'uri_sha256': key, 'sha256': digest,
                                   'mime': mime, 'bytes': len(data)})
        attempts.append({'attempt': attempt, 'status': 'downloaded', 'sha256': digest})
        write_json(root / (key + '.attempts.json'), {'source_id': asset.source_id, 'attempts': attempts})
        return uri, digest
    raise ConfigurationError('asset_attempts_must_be_positive')


def _asset_error_code(error: ValidationError | ExternalServiceError) -> str:
    """Return a stable asset failure code without retaining exception text."""
    value = str(error)
    safe = {
        'private_asset_destination', 'asset_requires_public_https', 'asset_dns_failed',
        'asset_size_limit', 'asset_mime_or_signature_mismatch', 'asset_transport_failed',
        'asset_cache_integrity_failed', 'asset_download_hash_mismatch',
        'asset_gif_conversion_failed', 'asset_view_size_limit',
    }
    if value in safe or re.fullmatch(r'asset_http_status:[0-9]{3}', value):
        return value
    return 'asset_validation_failed' if isinstance(error, ValidationError) else 'asset_external_service_failed'


def _cached_gif_view(metadata_path: Path, binary_path: Path, original_sha256: str,
                     maximum_bytes: int) -> tuple[str, int, str] | None:
    """Read a verified first-frame PNG view for a cached GIF."""
    try:
        metadata = strict_json(metadata_path.read_text(encoding='utf-8'))
        data = binary_path.read_bytes()
    except (OSError, ValidationError):
        return None
    if (not isinstance(metadata, dict) or metadata.get('original_sha256') != original_sha256
            or metadata.get('mime') != 'image/png' or metadata.get('sha256') != hashlib.sha256(data).hexdigest()
            or metadata.get('bytes') != len(data) or len(data) > maximum_bytes
            or image_mime(data) != 'image/png' or metadata.get('frame') != 0
            or type(metadata.get('total_frames')) is not int or metadata['total_frames'] < 1
            or metadata.get('coverage') not in {'complete', 'partial'}):
        return None
    return f'data:image/png;base64,{base64.b64encode(data).decode()}', metadata['total_frames'], metadata['coverage']


def _gif_first_frame_png(data: bytes) -> tuple[bytes, int]:
    """Convert GIF frame zero to a PNG view without modifying the original bytes."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ConfigurationError('gif_view_requires_pillow') from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'GIF':
                    raise ValidationError('asset_gif_conversion_failed')
                total_frames = image.n_frames
                image.seek(0)
                frame = image.convert('RGBA')
                output = io.BytesIO()
                frame.save(output, format='PNG')
    except ValidationError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError('asset_gif_conversion_failed') from exc
    return output.getvalue(), total_frames


def prepare_asset_view(asset: IssueAsset, config: ExperimentConfig, context: RunContext) -> tuple[str, str]:
    """Return a model-safe asset view and the immutable original content hash."""
    uri, original_sha256 = prepare_asset(asset, config, context)
    mime, encoded = uri.removeprefix('data:').split(';base64,', 1)
    if mime != 'image/gif':
        return uri, original_sha256
    key = hashlib.sha256(asset.uri.encode('utf-8')).hexdigest()
    root = case_directory(config, context) / 'assets'
    metadata_path, binary_path = root / (key + '.view.json'), root / (key + '.view.png')
    cached = _cached_gif_view(metadata_path, binary_path, original_sha256, config.integration.max_asset_bytes)
    if cached is not None:
        return cached[0], original_sha256
    data = base64.b64decode(encoded, validate=True)
    png, total_frames = _gif_first_frame_png(data)
    context.budget.check_deadline()
    if len(png) > config.integration.max_asset_bytes or image_mime(png) != 'image/png':
        raise ValidationError('asset_view_size_limit')
    root.mkdir(parents=True, exist_ok=True)
    binary_path.write_bytes(png)
    write_json(metadata_path, {
        'original_sha256': original_sha256, 'mime': 'image/png',
        'sha256': hashlib.sha256(png).hexdigest(), 'bytes': len(png),
        'frame': 0, 'total_frames': total_frames,
        'coverage': 'partial' if total_frames > 1 else 'complete',
    })
    return f'data:image/png;base64,{base64.b64encode(png).decode()}', original_sha256


def _gif_view_note(asset: IssueAsset, config: ExperimentConfig, context: RunContext,
                   original_sha256: str) -> str | None:
    """Describe partial GIF coverage in the model-visible request content."""
    key = hashlib.sha256(asset.uri.encode('utf-8')).hexdigest()
    metadata_path = case_directory(config, context) / 'assets' / (key + '.view.json')
    try:
        metadata = strict_json(metadata_path.read_text(encoding='utf-8'))
    except (OSError, ValidationError):
        return None
    if (not isinstance(metadata, dict) or metadata.get('original_sha256') != original_sha256
            or metadata.get('frame') != 0 or metadata.get('coverage') != 'partial'
            or type(metadata.get('total_frames')) is not int or metadata['total_frames'] < 2):
        return None
    return f'GIF view uses frame 0 of {metadata["total_frames"]}; coverage: partial.'


def prepare_task_assets(task: TaskInput, config: ExperimentConfig, context: RunContext) -> TaskInput:
    """Prefetch usable task assets, record availability, and return an immutable usable subset."""
    if len(task.assets) > config.integration.max_assets:
        raise ValidationError('asset_count_limit')
    available: list[IssueAsset] = []
    availability: list[dict[str, str | None]] = []
    for asset in task.assets:
        try:
            _, original_sha256 = prepare_asset_view(asset, config, context)
        except (ValidationError, ExternalServiceError) as exc:
            availability.append({'source_id': asset.source_id, 'status': 'unavailable',
                                 'code': _asset_error_code(exc)})
        else:
            available.append(asset)
            availability.append({'source_id': asset.source_id, 'status': 'available',
                                 'code': None, 'original_sha256': original_sha256})
    write_json(case_directory(config, context) / 'assets' / 'availability.json', {'assets': availability})
    return replace(task, assets=tuple(available))


def response_format(request: ModelRequest, config: ExperimentConfig) -> dict:
    """Select an explicit output contract without silently falling back or retrying."""
    if request.output_schema is None:
        if request.schema_name in {'evidence.v2', 'fillings.v2', 'evidence.v3', 'evidence.v4', 'evidence.v5', 'edits.v3', 'edits.v4'}:
            raise ConfigurationError('output_schema_missing')
        return {'type': 'json_object'}
    if config.integration.response_format == 'json_object':
        return {'type': 'json_object'}
    return {'type': 'json_schema', 'json_schema': {'name': request.schema_name.replace('.', '_'),
                                                  'strict': True, 'schema': request.output_schema}}


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
        """Send one schema-constrained multimodal request and require accounted usage and normal stop.

        Structured output remains untrusted and is validated by each algorithm. Missing usage
        charges the entire reservation then fails. Fixture outputs are explicitly synthetic.
        """
        if self.config.integration.model_mode == 'fixture':
            return self._fixture(request, context)
        selected_format = response_format(request, self.config)
        values = read_model_environment(self.config)
        scheme, host, port, path = chat_endpoint(values[self.config.model.endpoint_env], self.config.integration.allow_local_http)
        if len(request.assets) > self.config.integration.max_assets:
            raise ValidationError('asset_count_limit')
        content: list[dict[str, Any]] = [{'type': 'text', 'text': request.prompt}]
        asset_hashes: list[str] = []
        for asset in request.assets:
            uri, digest = prepare_asset_view(asset, self.config, context)
            content.append({'type': 'text', 'text': 'Image asset source_id: ' + asset.source_id})
            note = _gif_view_note(asset, self.config, context, digest)
            if note is not None:
                content.append({'type': 'text', 'text': note})
            content.append({'type': 'image_url', 'image_url': {'url': uri}})
            asset_hashes.append(digest)
        limit = context.budget.begin_model_call(request.max_output_tokens)
        body = {'model': values[self.config.model.name_env], 'messages': [
            {'role': 'system', 'content': request.system}, {'role': 'user', 'content': content}],
            'max_completion_tokens': limit, 'temperature': context.policy.temperature,
            'response_format': selected_format, 'n': 1, 'stream': False, 'seed': context.seed}
        trajectory = case_directory(self.config, context) / 'trajectory'
        call_name = f'{context.budget.model_calls:03d}_{safe_component(request.schema_name)}'
        write_json(trajectory / (call_name + '.request.json'), {
            'schema_name': request.schema_name, 'system': request.system, 'prompt': request.prompt,
            'model': body['model'], 'seed': body['seed'], 'temperature': body['temperature'],
            'max_completion_tokens': limit, 'response_format': body['response_format'],
            'assets': [{'source_id': asset.source_id, 'uri': asset.uri, 'sha256': digest}
                       for asset, digest in zip(request.assets, asset_hashes)],
        })
        connection_class = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
        connection = connection_class(host, port=port, timeout=min(self.config.integration.http_timeout, context.budget.remaining_seconds()))
        try:
            connection.request('POST', path, body=json.dumps(body).encode(), headers={
                'Authorization': 'Bearer ' + values[self.config.model.api_key_env], 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                rejected_raw = response.read(16385)
                excerpt = rejected_raw.decode('utf-8', errors='replace')
                excerpt = excerpt.replace(values[self.config.model.api_key_env], '[redacted]')
                excerpt = re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]+', '[redacted]', excerpt)
                write_json(trajectory / (call_name + '.error.json'), {
                    'stage': 'model_http', 'http_status': response.status,
                    'response_format': body['response_format']['type'],
                    'response_excerpt': excerpt[:2048], 'response_truncated': len(rejected_raw) > 16384,
                    'response_prefix_sha256': hashlib.sha256(rejected_raw).hexdigest()})
                if response.status in {400, 422}:
                    try:
                        rejected = json.loads(rejected_raw)
                    except (ValueError, UnicodeDecodeError):
                        rejected = {}
                    error = rejected.get('error', {}) if isinstance(rejected, dict) else {}
                    error = error if isinstance(error, dict) else {}
                    parameter = error.get('param')
                    if error.get('code') == 'invalid_json_schema' or (
                            isinstance(parameter, str) and parameter.split('.')[0] == 'response_format'):
                        raise ConfigurationError(f'model_request_rejected:{response.status}:{body["response_format"]["type"]}')
                if response.status in {401, 403, 404}:
                    raise ConfigurationError(f'model_request_rejected:{response.status}:{body["response_format"]["type"]}')
                raise ExternalServiceError(f'model_http_status:{response.status}')
            raw = response.read(4000001)
            if len(raw) > 4000000:
                context.budget.record_output_tokens(limit)
                raise ExternalServiceError('model_response_size_limit')
            data = strict_json(raw.decode('utf-8'), maximum=4000000)
            choices = data.get('choices')
            write_json(trajectory / (call_name + '.response.json'), {
                'request_id': data.get('id'), 'model': data.get('model'), 'usage': data.get('usage'),
                'response_sha256': hashlib.sha256(raw).hexdigest(),
                'choices': [{'finish_reason': choice.get('finish_reason'),
                             'text': choice.get('message', {}).get('content')}
                            for choice in choices if isinstance(choice, dict)]
                           if isinstance(choices, list) else choices,
            })
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
            write_json(trajectory / (call_name + '.error.json'), {
                'stage': 'model_transport', 'cause_type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)})
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
