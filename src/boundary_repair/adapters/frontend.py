"""Trusted parser bridge and deterministic source retrieval; no target-code imports."""
import json
import re
import shutil
from pathlib import Path
from typing import Any

from boundary_repair.adapters.process import run_process
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import RepositorySnapshot
from boundary_repair.kernel.files import allowed_source


def resolve_node(config: ExperimentConfig) -> str:
    """Resolve explicit Node files/directories, then PATH; never execute a target repo binary."""
    for raw in config.node_candidates:
        path = Path(raw)
        for candidate in (path, path / 'bin' / 'node', path / 'node', path / 'node.exe'):
            if candidate.is_file():
                return str(candidate.resolve())
    located = shutil.which('node')
    if located is None:
        raise ConfigurationError('node_executable_missing')
    return located


def collect_sources(snapshot: RepositorySnapshot, config: ExperimentConfig) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    """Read bounded regular UTF-8 production sources, recording every coverage truncation."""
    import os
    from boundary_repair.kernel.files import EXCLUDED, TEST_PARTS
    files: list[dict[str, str]] = []
    diagnostics: list[str] = []
    for directory, children, names in os.walk(snapshot.root, followlinks=False):
        children[:] = sorted(d for d in children if d not in EXCLUDED | TEST_PARTS
                             and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(snapshot.root).as_posix()
            if path.is_symlink() or not allowed_source(relative):
                continue
            if len(files) >= config.integration.max_files:
                diagnostics.append('index_file_limit')
                return files, tuple(diagnostics)
            if path.stat().st_size > config.integration.max_file_bytes:
                diagnostics.append(f'file_too_large:{relative}')
                continue
            try:
                source = path.read_bytes().decode('utf-8')
            except UnicodeDecodeError:
                diagnostics.append(f'non_utf8:{relative}')
                continue
            files.append({'path': relative, 'source': source})
    return files, tuple(diagnostics)


def parse_sources(files: list[dict[str, str]], config: ExperimentConfig, context: RunContext) -> dict[str, Any]:
    """Parse source strings through a pinned, trusted compiler; text mode is explicitly partial."""
    if config.integration.parser_mode == 'text':
        return {'parser': 'text', 'version': '1', 'files': [{'path': f['path'], 'locations': [],
                'functions': [], 'identifiers': sorted(set(re.findall(r'\b[A-Za-z_$][\w$]*', f['source']))),
                'unsupported': ['text_only_no_ast']} for f in files]}
    script = Path(__file__).resolve().parents[1] / 'frontend' / 'parse.cjs'
    args = [resolve_node(config), str(script)]
    if config.integration.parser_module is not None:
        module = config.integration.parser_module.resolve()
        if not module.exists():
            raise ConfigurationError('parser_module_missing')
        args.append(str(module))
    result = run_process(args, input_data=json.dumps({'files': files}).encode(),
                         timeout=min(90, context.budget.remaining_seconds()), max_output=64000000)
    if result.returncode:
        raise ExternalServiceError('typescript_parser_failed_install_pinned_dependency')
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExternalServiceError('invalid_parser_ipc') from exc
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        raise ExternalServiceError('invalid_parser_schema')
    return data

_ANALYSIS_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_ANALYSIS_TIMEOUT_SECONDS = 90
_ANALYSIS_IDENTIFIER_LIMIT = 8192


def _analysis_input(file: dict[str, str]) -> dict[str, str]:
    """Validate a selected source before optional analysis; malformed callers are programming errors."""
    if not isinstance(file, dict) or type(file.get('path')) is not str or type(file.get('source')) is not str:
        raise TypeError('analysis_file_requires_string_path_and_source')
    return {'path': file['path'], 'source': file['source']}


def _analysis_unavailable(path: str, diagnostic: str) -> dict[str, Any]:
    """Keep a failed optional analysis indexable without inventing semantic metadata."""
    return {'path': path, 'locations': [], 'functions': [], 'identifiers': [], 'semantic_hash': None,
            'consumer_guards': [], 'unsupported': [diagnostic], 'analysis_status': 'unavailable'}


def _text_analysis(file: dict[str, str]) -> dict[str, Any]:
    """Return a bounded lexical fallback when AST analysis is explicitly disabled."""
    identifiers: list[str] = []
    seen: set[str] = set()
    unsupported = ['text_only_no_ast']
    for match in re.finditer(r'\b[A-Za-z_$][\w$]*', file['source']):
        identifier = match.group(0)
        if identifier in seen:
            continue
        if len(identifiers) >= _ANALYSIS_IDENTIFIER_LIMIT:
            unsupported.append('analysis_identifier_limit')
            break
        seen.add(identifier)
        identifiers.append(identifier)
    return {'path': file['path'], 'locations': [], 'functions': [], 'identifiers': sorted(identifiers),
            'semantic_hash': None, 'consumer_guards': [], 'unsupported': unsupported,
            'analysis_status': 'partial'}


def _analysis_parser_args(config: ExperimentConfig) -> list[str]:
    """Resolve the trusted parser once so configuration errors are never hidden per file."""
    script = Path(__file__).resolve().parents[1] / 'frontend' / 'parse.cjs'
    args = [resolve_node(config), str(script)]
    if config.integration.parser_module is not None:
        module = config.integration.parser_module.resolve()
        if not module.exists():
            raise ConfigurationError('parser_module_missing')
        args.append(str(module))
    return args


def _parse_selected_source(file: dict[str, str], args: list[str], context: RunContext) -> dict[str, Any]:
    """Parse exactly one selected source, retaining strict IPC validation for optional analysis."""
    result = run_process(args, input_data=json.dumps({'files': [file]}).encode(),
                         timeout=min(_ANALYSIS_TIMEOUT_SECONDS, context.budget.remaining_seconds()),
                         max_output=_ANALYSIS_MAX_OUTPUT_BYTES)
    if result.returncode:
        raise ExternalServiceError('typescript_parser_failed_install_pinned_dependency')
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExternalServiceError('invalid_parser_ipc') from exc
    if (not isinstance(data, dict) or not isinstance(data.get('files'), list)
            or len(data['files']) != 1 or not isinstance(data['files'][0], dict)
            or data['files'][0].get('path') != file['path']):
        raise ExternalServiceError('invalid_parser_schema')
    return data


def _analysis_row_is_compatible(row: dict[str, Any]) -> bool:
    """Reject malformed parser IPC before a partial result reaches ProgramAdapter indexing."""
    list_fields = ('locations', 'functions', 'consumer_guards')
    return (all(field in row and isinstance(row[field], list) for field in list_fields)
            and all(isinstance(item, dict) for field in list_fields for item in row[field])
            and isinstance(row.get('identifiers'), list)
            and all(isinstance(item, str) for item in row['identifiers'])
            and isinstance(row.get('unsupported'), list)
            and all(isinstance(item, str) for item in row['unsupported'])
            and (row.get('semantic_hash') is None or isinstance(row['semantic_hash'], str)))


def analyze_sources(files: list[dict[str, str]], config: ExperimentConfig, context: RunContext) -> dict[str, Any]:
    """Optionally analyze selected sources one at a time without blocking textual retrieval.

    This is deliberately distinct from ``parse_sources``: strong materialization keeps the latter's
    all-or-nothing contract. Here only trusted-parser execution, IPC, output-limit, and explicit
    parser-support failures become a per-file unavailable/partial result. Budget and configuration
    errors propagate unchanged.
    """
    selected = [_analysis_input(file) for file in files]
    if config.integration.parser_mode == 'text':
        return {'parser': 'text', 'version': '1', 'files': [_text_analysis(file) for file in selected]}
    args = _analysis_parser_args(config)
    rows: list[dict[str, Any]] = []
    parser, version = 'typescript', None
    for file in selected:
        try:
            data = _parse_selected_source(file, args, context)
        except ExternalServiceError as exc:
            diagnostic = ('analysis_output_limit' if str(exc) == 'process_output_limit'
                          else 'analysis_execution_failed')
            rows.append(_analysis_unavailable(file['path'], diagnostic))
            continue
        row = data['files'][0]
        if not _analysis_row_is_compatible(row):
            rows.append(_analysis_unavailable(file['path'], 'analysis_execution_failed'))
            continue
        unsupported = row['unsupported']
        row = dict(row)
        row['analysis_status'] = 'partial' if unsupported else 'complete'
        rows.append(row)
        parser = data.get('parser') if isinstance(data.get('parser'), str) else parser
        version = data.get('version') if isinstance(data.get('version'), str) else version
    return {'parser': parser, 'version': version, 'files': rows}


def retrieval_score(query: str, path: str, source: str) -> float:
    """Deterministic lexical relevance: path/identifier matches outweigh repeated body tokens."""
    tokens = {t.lower() for t in re.findall(r'[A-Za-z_$][\w$]{2,}', query)}
    stop = {'the', 'and', 'this', 'that', 'with', 'from', 'have', 'should', 'when', 'return', 'true', 'false'}
    tokens -= stop
    path_tokens = set(re.findall(r'[a-z_]+', path.lower()))
    identifiers = {t.lower() for t in re.findall(r'[A-Za-z_$][\w$]*', source)}
    return 5.0 * len(tokens & path_tokens) + 2.0 * len(tokens & identifiers)


def source_context(snapshot: RepositorySnapshot, query: str, config: ExperimentConfig) -> list[dict[str, str]]:
    """Rank raw base files and cap characters before model calls; never include tests or history."""
    files, _ = collect_sources(snapshot, config)
    ranked = sorted(files, key=lambda f: (-retrieval_score(query, f['path'], f['source']), f['path']))
    selected, remaining = [], config.integration.max_context_chars
    for item in ranked[:config.integration.context_files]:
        text = item['source'][:remaining]
        selected.append({'path': item['path'], 'source': text, 'truncated': str(len(text) < len(item['source']))})
        remaining -= len(text)
        if remaining <= 0:
            break
    return selected
