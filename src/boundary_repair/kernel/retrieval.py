"""Shared candidate retrieval, independent of the three research mechanisms."""
import hashlib
import re
from dataclasses import replace
from pathlib import PurePosixPath

from boundary_repair.domain.repair import EditKind, EditScope, Feature, RepairBoundary, SyntaxHole
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import ProgramIndex, RepositorySnapshot, TaskInput
from boundary_repair.kernel.files import source_slice
from boundary_repair.kernel.terms import symbol


def scope_snippets(scope: EditScope) -> tuple[dict[str, object], ...]:
    """Expose contiguous evidence with its exact original character and byte offsets."""
    files = {f.file_id: f for f in scope.files}
    return tuple({'path': r.path, 'source': r.source, 'start_char': r.start_char,
                  'start_byte': r.start_byte, 'start_line': r.start_line,
                  'truncated': not files[r.file_id].complete} for r in scope.regions)


DOCUMENT_SUFFIXES = frozenset({'.adoc', '.md', '.rst', '.txt'})
DOCUMENT_NAMES = frozenset({'changelog', 'code_of_conduct', 'contributing', 'license', 'licence',
                            'readme', 'security'})
EXAMPLE_PARTS = frozenset({'demo', 'demos', 'example', 'examples', 'sample', 'samples'})


def _explicit_path_reference(query: str, path: str) -> bool:
    """Match one normalized repository-relative path when the issue names it literally."""
    normalized = query.replace('\\', '/').casefold()
    escaped = re.escape(path.casefold())
    return re.search(r'(?<![A-Za-z0-9_.\-/])' + escaped + r'(?![A-Za-z0-9_.\-/])', normalized) is not None


def source_priority(query: str, path: str) -> int:
    """Rank literal issue-referenced files first, then production source before supporting material."""
    relative = PurePosixPath(path)
    parts = tuple(part.casefold() for part in relative.parts)
    suffix = relative.suffix.casefold()
    if _explicit_path_reference(query, path):
        return -1
    supporting = (suffix in DOCUMENT_SUFFIXES or relative.stem.casefold() in DOCUMENT_NAMES
                  or any(part in EXAMPLE_PARTS for part in parts)
                  or '.github' in parts or 'issue_template' in parts or 'issue-templates' in parts)
    return 1 if supporting else 0


def lexical_tokens(text: str) -> set[str]:
    """Split identifiers and camel-case components without task-specific vocabulary."""
    ignored = {'the', 'and', 'should', 'return', 'false', 'true', 'when', 'from', 'this', 'that', 'with'}
    expanded = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    return {word.lower() for word in re.findall(r'[A-Za-z_$][\w$]{2,}', text + ' ' + expanded)} - ignored


def lexical_score(query: str, text: str) -> int:
    """Normalize lexical overlap so large unrelated files do not win by vocabulary size."""
    tokens = lexical_tokens(text)
    return round(1000 * len(lexical_tokens(query) & tokens) / (20 + len(tokens)) ** 0.5)


def location_score(query: str, path: str, symbol: str, text: str) -> int:
    """Prefer named source and call-site bindings over incidental body mentions."""
    words = lexical_tokens(query)
    return (lexical_score(query, text) + 250 * len(words & lexical_tokens(path))
            + 500 * len(words & lexical_tokens(symbol)))


def context_excerpt(text: str, query: str, maximum: int, focus: int | None = None) -> tuple[str, int]:
    """Return one contiguous original window around a source location or lexical hit."""
    if len(text) <= maximum:
        return text, 0
    if focus is None:
        offsets, offset = [], 0
        for line in text.splitlines(keepends=True):
            offsets.append((lexical_score(query, line), -offset, offset))
            offset += len(line)
        focus = max(offsets, default=(0, 0, 0))[2]
    start = max(0, min(len(text) - maximum, focus - maximum // 3))
    return text[start:start + maximum], start


ATOMIC_KINDS = {'BooleanReturn', 'Condition', 'Consumer', 'AttributeValue', 'RegExp',
                'ReturnValue', 'Initializer', 'Argument', 'Statement', 'AssignmentValue'}


def syntax_holes(boundary: RepairBoundary, index: ProgramIndex, snapshot: RepositorySnapshot,
                 query: str, prefix: str, certified: bool = False) -> tuple[SyntaxHole, ...]:
    """Declare nonoverlapping local replacement sites before any code is generated."""
    selected = []
    for parent in boundary.sites:
        data, start, end = source_slice(snapshot.root, parent)
        if parent.node_kind in ATOMIC_KINDS and parent.node_count <= 128:
            candidates = [parent]
        else:
            candidates = [site for site in index.locations if site.path == parent.path
                          and site.node_kind in ATOMIC_KINDS and site.node_count <= 128
                          and start <= site.start_byte < site.end_byte <= end]
        if not candidates and PurePosixPath(parent.path).suffix.lower() not in {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}:
            offset = start
            for line in data[start:end].splitlines(keepends=True):
                if line.strip():
                    candidates.append(replace(parent, start_byte=offset, end_byte=offset + len(line),
                                              start_line=data[:offset].count(b'\n') + 1,
                                              end_line=data[:offset].count(b'\n') + 1,
                                              node_kind='TextLine', content_sha256=hashlib.sha256(line).hexdigest()))
                offset += len(line)
        candidates.sort(key=lambda site: (site.node_kind == 'Statement',
            -location_score(query, site.path, site.symbol, data[site.start_byte:site.end_byte].decode('utf-8')),
            site.end_byte - site.start_byte, site.start_byte, site.node_kind))
        for site in candidates:
            if len(selected) >= 64:
                break
            if any(site.path == other.path and site.start_byte < other.end_byte and other.start_byte < site.end_byte
                   for other in selected):
                continue
            selected.append(site)
    return tuple(SyntaxHole(f'{prefix}:{i}', site,
                            'boolean-expression' if certified and site.node_kind == 'BooleanReturn'
                            else 'consumer-branch' if site.node_kind == 'Consumer'
                            else 'local:' + site.node_kind,
                            tuple(f.feature_id for f in boundary.readable_features) if certified else ())
                 for i, site in enumerate(sorted(selected, key=lambda s: (s.path, s.start_byte))))


def snippets(index: ProgramIndex, snapshot: RepositorySnapshot, query: str, limit: int = 8,
             max_chars: int = 100000) -> tuple[dict[str, object], ...]:
    """Collect a stable top-k file context under a total character limit."""
    ranked = []
    for span in index.locations:
        if span.node_kind != 'File':
            continue
        data, start, end = source_slice(snapshot.root, span)
        source = data[start:end].decode('utf-8')
        ranked.append((-location_score(query, span.path, '', source), span.path, source))
    result = []
    remaining = max_chars
    for ordinal, (_, path, source) in enumerate(sorted(ranked)[:limit]):
        allowance = max(1, remaining // (min(limit, len(ranked)) - ordinal))
        excerpt, start = context_excerpt(source, query, allowance)
        result.append({'path': path, 'source': excerpt, 'start_char': start, 'truncated': len(excerpt) != len(source)})
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return tuple(result)


def candidate_boundaries(task: TaskInput, index: ProgramIndex, snapshot: RepositorySnapshot,
                         context: RunContext, maximum: int = 40) -> tuple[RepairBoundary, ...]:
    """Enumerate identical pools for research and plain localization, with explicit read interfaces.

    Boolean-return interfaces include a constant-only edit and a parameter-reading edit. Other
    AST nodes retain UNKNOWN semantics. File-level fallback remains available for unsupported
    source languages, but its broad effect is not confused with a proven minimal repair.
    """
    interfaces = {span: names for span, names in index.read_interfaces}
    ranked = []
    for span in index.locations:
        context.budget.check_deadline()
        data, start, end = source_slice(snapshot.root, span)
        text = data[start:end].decode('utf-8')
        score = location_score(task.problem_statement, span.path, span.symbol, text)
        kind = {'BooleanReturn': EditKind.REFINE_GUARD, 'Condition': EditKind.REFINE_GUARD,
                'Consumer': EditKind.SPLIT_CONSUMER, 'Assignment': EditKind.REBIND_IDENTITY,
                'RegExp': EditKind.REFINE_GUARD}.get(span.node_kind, EditKind.FREEFORM)
        names = interfaces.get(span, ())
        modes = [('parameters', names), ('constant', ())] if span.node_kind == 'BooleanReturn' else [('scope', ())]
        for mode, reads in modes:
            key = f'{span.path}:{start}:{end}:{mode}:{span.content_sha256}'
            identifier = hashlib.sha256(key.encode()).hexdigest()[:16]
            features = tuple(Feature(name, symbol(name), (f'code:{span.path}:{span.start_line}',)) for name in reads)
            boundary = RepairBoundary(identifier, (span,), kind, features, ('return',) if span.node_kind == 'BooleanReturn' else (), score)
            specificity = 0 if span.node_kind == 'BooleanReturn' else 1 if span.node_kind != 'File' else 2
            ranked.append((-score, specificity, span.path, start, mode, boundary))
    return tuple(row[-1] for row in sorted(ranked, key=lambda x: x[:-1])[:maximum])
