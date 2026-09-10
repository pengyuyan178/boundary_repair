"""Truth-table-guided Boolean expression synthesis, restricted to a declared finite grammar."""
from dataclasses import dataclass
from itertools import combinations
import re

from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import Scalar, Term
from boundary_repair.kernel.terms import evaluate, literal, symbol


@dataclass(frozen=True, slots=True)
class BooleanExpression:
    """One pure Boolean AST, its emitted JS text, and node cost."""
    term: Term
    source: str
    size: int


def synthesize_boolean(names: tuple[str, ...], cases: tuple[tuple[dict[str, Scalar], bool], ...],
                       context: RunContext, max_nodes: int = 9) -> BooleanExpression | None:
    """Enumerate typed !/&&/|| expressions by AST size, quotienting by behavior on all witnesses.

    This is a finite, explicitly bounded grammar, not unrestricted program synthesis. Missing
    reads, conflicting examples, and unsupported names are not silently replaced by constants.
    Every returned expression is rechecked against all requirements and preservation examples.
    """
    if not cases or len(names) > 8 or any(not re.fullmatch(r'[A-Za-z_$][\w$]*', n) for n in names):
        return None
    if any(any(name not in values or type(values[name]) is not bool for name in names)
           or type(target) is not bool for values, target in cases):
        return None
    goal = tuple(target for _, target in cases)
    by_signature: dict[tuple[bool, ...], BooleanExpression] = {}
    levels: dict[int, list[BooleanExpression]] = {}
    candidates = [BooleanExpression(literal(False), 'false', 1), BooleanExpression(literal(True), 'true', 1)]
    candidates += [BooleanExpression(symbol(name), name, 1) for name in sorted(names)]
    for size in range(1, max_nodes + 1):
        context.budget.check_deadline()
        if size > 1:
            candidates = [BooleanExpression(Term('not', (x.term,)), f'!({x.source})', size)
                          for x in levels.get(size-1, [])]
            for left_size in range(1, size-1):
                right_size = size - 1 - left_size
                for a in levels.get(left_size, []):
                    for b in levels.get(right_size, []):
                        if a.source > b.source:
                            continue
                        for op, token in (('and', '&&'), ('or', '||')):
                            candidates.append(BooleanExpression(Term(op, (a.term, b.term)),
                                                                f'({a.source} {token} {b.source})', size))
        accepted = []
        for number, expression in enumerate(sorted(candidates, key=lambda e: e.source)):
            if number % 128 == 0:
                context.budget.check_deadline()
            signature = tuple(evaluate(expression.term, values) for values, _ in cases)
            if any(type(value) is not bool for value in signature):
                raise ValidationError('boolean_synthesis_sort_error')
            if signature in by_signature:
                continue
            by_signature[signature] = expression
            accepted.append(expression)
            if signature == goal:
                return expression
        levels[size] = accepted
        if len(by_signature) > 4096:
            return None
    return None
