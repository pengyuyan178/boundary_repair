"""Shared candidate retrieval, independent of the three research mechanisms."""
import hashlib
import re

from boundary_repair.domain.repair import EditKind, Feature, RepairBoundary
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import ProgramIndex, RepositorySnapshot, TaskInput
from boundary_repair.kernel.files import source_slice
from boundary_repair.kernel.terms import symbol


def lexical_score(query: str, text: str) -> int:
    """Score distinct identifiers rather than repeated words; no gold-file hints are accepted."""
    ignored = {'the', 'and', 'should', 'return', 'false', 'true', 'when', 'from', 'this', 'that', 'with'}
    words = {x.lower() for x in re.findall(r'[A-Za-z_$][\w$]{2,}', query)} - ignored
    tokens = {x.lower() for x in re.findall(r'[A-Za-z_$][\w$]{2,}', text)}
    return len(words & tokens)


def snippets(index: ProgramIndex, snapshot: RepositorySnapshot, query: str, limit: int = 8,
             max_chars: int = 100000) -> tuple[dict[str, object], ...]:
    """Collect a stable top-k file context under a total character limit."""
    ranked = []
    for span in index.locations:
        if span.node_kind != 'File':
            continue
        data, start, end = source_slice(snapshot.root, span)
        source = data[start:end].decode('utf-8')
        ranked.append((-lexical_score(query, span.path + '\n' + source), span.path, source))
    result = []
    remaining = max_chars
    for _, path, source in sorted(ranked)[:limit]:
        excerpt = source[:remaining]
        result.append({'path': path, 'source': excerpt, 'truncated': len(excerpt) != len(source)})
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
        score = lexical_score(task.problem_statement, span.symbol + ' ' + span.path + ' ' + text)
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
