"""Trusted parser bridge and deterministic source retrieval; no target-code imports."""
import hashlib
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
