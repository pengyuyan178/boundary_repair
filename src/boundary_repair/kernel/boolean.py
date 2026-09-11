"""Truth-table-guided Boolean expression synthesis, restricted to a declared finite grammar."""
from dataclasses import dataclass
from itertools import combinations
import json
import math
import re

from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import Scalar, Term
from boundary_repair.kernel.terms import evaluate, literal, symbol, symbols, typed_key, walk


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


def simple_regex(pattern: str, flags: str = '') -> tuple | None:
    """Parse an ASCII regex subset with literals, classes, anchors, alternatives and single quantifiers."""
    if not pattern.isascii() or len(pattern) > 256 or flags not in {'', 'i'}:
        return None
    branches, current, escaped, bracket = [], '', False, False
    for char in pattern:
        if char == '|' and not escaped and not bracket:
            branches.append(current)
            current = ''
            continue
        current += char
        if not escaped:
            if char == '[':
                bracket = True
            elif char == ']':
                bracket = False
        escaped = not escaped and char == '\\'
    branches.append(current)
    result = []
    universe = frozenset(chr(i) for i in range(128))
    digits = frozenset('0123456789')
    words = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')
    spaces = frozenset(' \t\r\n\v\f')
    escapes = {'d': digits, 'w': words, 's': spaces,
               'D': universe - digits, 'W': universe - words, 'S': universe - spaces}
    for branch in branches:
        start, end = branch.startswith('^'), False
        index, tokens = int(start), []
        while index < len(branch):
            char = branch[index]
            if char == '$' and index == len(branch) - 1:
                end = True
                index += 1
                continue
            negated = False
            if char == '[':
                close = index + 1
                while close < len(branch) and (branch[close] != ']' or branch[close - 1] == '\\'):
                    close += 1
                if close == len(branch):
                    return None
                body = branch[index + 1:close]
                negated = body.startswith('^')
                body = body[int(negated):]
                if not body or '\\' in body:
                    return None
                letters, offset = set(), 0
                while offset < len(body):
                    if offset + 2 < len(body) and body[offset + 1] == '-':
                        if ord(body[offset]) > ord(body[offset + 2]):
                            return None
                        letters.update(chr(i) for i in range(ord(body[offset]), ord(body[offset + 2]) + 1))
                        offset += 3
                    else:
                        letters.add(body[offset])
                        offset += 1
                index = close + 1
            elif char == '\\':
                index += 1
                if index >= len(branch):
                    return None
                code = branch[index]
                if code in escapes:
                    letters = set(escapes[code])
                elif code in 'nrt':
                    letters = {dict(n='\n', r='\r', t='\t')[code]}
                elif not code.isalnum():
                    letters = {code}
                else:
                    return None
                index += 1
            elif char in '(){}^$*+?]':
                return None
            else:
                letters = set(universe - {'\r', '\n'}) if char == '.' else {char}
                index += 1
            if flags == 'i':
                letters = {c.lower() for c in letters}
            quantifier = branch[index] if index < len(branch) and branch[index] in '*+?' else ''
            index += bool(quantifier)
            tokens.append((frozenset(letters), negated, quantifier))
        result.append((start, end, tuple(tokens)))
    return tuple(result)


def regex_matches(parsed: tuple, text: str, flags: str) -> bool:
    """Evaluate supported regex membership with bounded position sets rather than backtracking."""
    value = text.lower() if flags == 'i' else text
    for anchored, ending, tokens in parsed:
        positions = {0} if anchored else set(range(len(value) + 1))
        for letters, negated, quantifier in tokens:
            reached = set(positions) if quantifier in {'*', '?'} else set()
            pending = list(positions)
            visited = set()
            while pending:
                index = pending.pop()
                if index in visited:
                    continue
                visited.add(index)
                if index < len(value) and ((value[index] in letters) != negated):
                    reached.add(index + 1)
                    if quantifier in {'*', '+'}:
                        pending.append(index + 1)
            positions = reached
        if len(value) in positions if ending else bool(positions):
            return True
    return False


def program_term(raw: dict) -> Term:
    """Decode a program-owned projection term without extending the model's evidence language."""
    return Term(raw['op'], tuple(program_term(arg) for arg in raw.get('args', [])), raw.get('value'))


def program_sort(term: Term, sorts: dict[str, str]) -> str | None:
    """Check the supported scalar projection algebra and regular-rule subset."""
    if term.op == 'literal':
        if type(term.value) in {int, float}:
            return 'number' if math.isfinite(term.value) else None
        return {bool: 'boolean', str: 'string', type(None): 'null'}.get(type(term.value))
    if term.op == 'symbol':
        return sorts.get(str(term.value))
    arguments = [program_sort(arg, sorts) for arg in term.args]
    if any(sort is None for sort in arguments):
        return None
    if term.op in {'not', 'and', 'or'}:
        return 'boolean' if all(sort == 'boolean' for sort in arguments) else None
    if term.op in {'eq', 'ne'}:
        return 'boolean'
    if term.op in {'lt', 'le'}:
        return 'boolean' if arguments == ['number', 'number'] else None
    if term.op == 'ite':
        return arguments[1] if arguments[0] == 'boolean' and arguments[1] == arguments[2] else None
    if term.op == 'regex_test' and arguments == ['string', 'string', 'string']:
        if term.args[0].op == term.args[1].op == 'literal' and simple_regex(term.args[0].value, term.args[1].value) is not None:
            return 'boolean'
    return None


def program_evaluate(term: Term, values: dict[str, Scalar]) -> Scalar:
    """Evaluate validated pure projection terms using JavaScript scalar equality and ASCII rules."""
    if term.op == 'literal':
        return float(term.value) if type(term.value) in {int, float} else term.value
    if term.op == 'symbol':
        return values[str(term.value)]
    if term.op == 'ite':
        return program_evaluate(term.args[1] if program_evaluate(term.args[0], values) else term.args[2], values)
    args = tuple(program_evaluate(arg, values) for arg in term.args)
    if term.op == 'regex_test':
        return regex_matches(simple_regex(args[0], args[1]), args[2], args[1])
    if term.op in {'eq', 'ne'}:
        equal = typed_key(args[0]) == typed_key(args[1])
        return equal if term.op == 'eq' else not equal
    return evaluate(Term(term.op, tuple(literal(value) for value in args)), {})


def program_source(term: Term) -> str:
    """Emit JavaScript only from the declared pure scalar expression grammar."""
    if term.op == 'literal':
        return json.dumps(term.value, ensure_ascii=False)
    if term.op == 'symbol':
        if not re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*', str(term.value)) or term.value == '$edit':
            raise ValidationError('unsupported_finite_feature_name')
        return str(term.value)
    if term.op == 'not':
        return '!(' + program_source(term.args[0]) + ')'
    if term.op == 'ite':
        return '(' + ' ? '.join([program_source(term.args[0]),
            program_source(term.args[1]) + ' : ' + program_source(term.args[2])]) + ')'
    if term.op == 'regex_test':
        return f'/{term.args[0].value}/{term.args[1].value}.test({program_source(term.args[2])})'
    token = {'and': '&&', 'or': '||', 'eq': '===', 'ne': '!==', 'lt': '<', 'le': '<='}[term.op]
    return '(' + f' {token} '.join(program_source(arg) for arg in term.args) + ')'


def synthesize_finite(names: tuple[str, ...], cases: tuple[tuple[dict[str, Scalar], tuple[Scalar, ...]], ...],
                      atoms: tuple[Term, ...], literals: tuple[Scalar, ...], output_sort: str,
                      context: RunContext, max_nodes: int = 9) -> BooleanExpression | None:
    """Search a bounded pure grammar against downstream-allowed outputs for every cited witness."""
    if not cases or any(not allowed for _, allowed in cases):
        return None
    sorts = {name: program_sort(literal(cases[0][0][name]), {}) for name in names}
    initial = [literal(value) for value in literals + (False, True)] + [symbol(name) for name in names]
    initial += [atom for atom in atoms if set(symbols(atom)) <= set(names)]
    for name in names:
        for value in literals:
            if sorts[name] == program_sort(literal(value), {}):
                initial.append(Term('eq', (symbol(name), literal(value))))
    levels, seen, total = {}, set(), 0
    for size in range(1, max_nodes + 1):
        context.budget.check_deadline()
        candidates = [term for term in initial if len(walk(term)) == size]
        candidates += [Term('not', (term,)) for term in levels.get(size - 1, []) if program_sort(term, sorts) == 'boolean']
        for left_size in range(1, size - 1):
            for left in levels.get(left_size, []):
                if program_sort(left, sorts) != 'boolean':
                    continue
                for right in levels.get(size - left_size - 1, []):
                    if program_sort(right, sorts) == 'boolean':
                        candidates.extend(Term(op, (left, right)) for op in ('and', 'or'))
        for guard_size in range(1, size - 2):
            for yes_size in range(1, size - guard_size - 1):
                no_size = size - guard_size - yes_size - 1
                for guard in levels.get(guard_size, []):
                    if program_sort(guard, sorts) != 'boolean':
                        continue
                    for yes in levels.get(yes_size, []):
                        for no in levels.get(no_size, []):
                            if program_sort(yes, sorts) == program_sort(no, sorts) == output_sort:
                                candidates.append(Term('ite', (guard, yes, no)))
                                if len(candidates) > 65536:
                                    return None
        accepted = []
        for term in candidates:
            total += 1
            if total % 64 == 0:
                context.budget.check_deadline()
            sort = program_sort(term, sorts)
            if sort is None:
                continue
            signature = tuple(typed_key(program_evaluate(term, values)) for values, _ in cases)
            if signature in seen:
                continue
            seen.add(signature)
            accepted.append(term)
            if sort == output_sort and all(actual in {typed_key(v) for v in allowed}
                                           for actual, (_, allowed) in zip(signature, cases)):
                return BooleanExpression(term, program_source(term), size)
            if len(seen) > 4096:
                return None
        levels[size] = accepted
    return None
