"""A deliberately small typed logic language. No eval, execution, or implicit coercion."""
from __future__ import annotations

import json
import math
from itertools import product
from typing import Any, Mapping

from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.specification import Scalar, Term

ARITY = {"literal": (0, 0), "symbol": (0, 0), "not": (1, 1), "and": (0, 128),
         "or": (0, 128), "eq": (2, 2), "ne": (2, 2), "implies": (2, 2),
         "lt": (2, 2), "le": (2, 2), "add": (2, 2), "sub": (2, 2),
         "ite": (3, 3), "domain": (2, 129)}


def literal(value: Scalar) -> Term:
    """Construct a scalar literal; validation occurs before solving/serialization."""
    return Term("literal", value=value)


def symbol(name: str) -> Term:
    """Construct a named variable; symbols are Boolean unless a finite domain is declared."""
    return Term("symbol", value=name)


def term_from_json(value: object, depth: int = 0) -> Term:
    """Parse the exact JSON AST schema, rejecting extra keys, deep trees and non-scalars."""
    if depth > 24 or not isinstance(value, dict) or set(value) - {"op", "args", "value"}:
        raise ValidationError("invalid_term_shape")
    op = value.get("op")
    args = value.get("args", [])
    if op not in ARITY or not isinstance(args, list):
        raise ValidationError("unsupported_term_operator")
    lo, hi = ARITY[op]
    if not lo <= len(args) <= hi:
        raise ValidationError("invalid_term_arity")
    scalar = value.get("value")
    if type(scalar) not in {str, int, float, bool, type(None)}:
        raise ValidationError("invalid_literal")
    if type(scalar) is float and not math.isfinite(scalar):
        raise ValidationError("nonfinite_literal")
    if op == "symbol" and (not isinstance(scalar, str) or not scalar or len(scalar) > 256):
        raise ValidationError("invalid_symbol")
    if op not in {"symbol", "literal"} and scalar is not None:
        raise ValidationError("operator_value_not_allowed")
    return Term(op, tuple(term_from_json(a, depth + 1) for a in args), scalar)


def term_json(term: Term) -> dict[str, Any]:
    """Serialize a term without provider-specific objects or executable text."""
    return {"op": term.op, "args": [term_json(a) for a in term.args], "value": term.value}


def typed_key(value: Scalar) -> tuple[str, str]:
    """Keep Boolean true distinct from integer 1 and missing information distinct from null."""
    return type(value).__name__, json.dumps(value, ensure_ascii=False, sort_keys=True)


def walk(term: Term) -> tuple[Term, ...]:
    """Return deterministic pre-order nodes; callers validate untrusted trees first."""
    return (term,) + tuple(n for a in term.args for n in walk(a))


def symbols(term: Term) -> tuple[str, ...]:
    """Collect unique variable names in stable lexical order."""
    return tuple(sorted({str(n.value) for n in walk(term) if n.op == "symbol"}))


def substitute(term: Term, replacements: Mapping[str, Term]) -> Term:
    """Capture-free substitution in a binder-free expression language."""
    if term.op == "symbol" and str(term.value) in replacements:
        return replacements[str(term.value)]
    return Term(term.op, tuple(substitute(a, replacements) for a in term.args), term.value)


def _boolean(value: Scalar) -> bool:
    """Reject Python truthiness as a substitute for a Boolean sort."""
    if type(value) is not bool:
        raise ValidationError("boolean_sort_required")
    return value


def evaluate(term: Term, environment: Mapping[str, Scalar]) -> Scalar:
    """Interpret supported finite expressions with strict sorts, never Python eval."""
    op = term.op
    if op == "literal":
        return term.value
    if op == "symbol":
        if str(term.value) not in environment:
            raise ValidationError("unassigned_symbol")
        return environment[str(term.value)]
    values = tuple(evaluate(a, environment) for a in term.args)
    if op == "not":
        return not _boolean(values[0])
    if op in {"and", "or", "implies"}:
        flags = tuple(_boolean(v) for v in values)
        return all(flags) if op == "and" else (any(flags) if op == "or" else not flags[0] or flags[1])
    if op in {"eq", "ne"}:
        if type(values[0]) is not type(values[1]):
            raise ValidationError("equality_sort_mismatch")
        result = values[0] == values[1]
        return result if op == "eq" else not result
    if op == "domain":
        return typed_key(values[0]) in {typed_key(v) for v in values[1:]}
    if op == "ite":
        if type(values[1]) is not type(values[2]):
            raise ValidationError("branch_sort_mismatch")
        return values[1] if _boolean(values[0]) else values[2]
    if op in {"lt", "le", "add", "sub"}:
        if any(type(v) not in {int, float} for v in values) or type(values[0]) is not type(values[1]):
            raise ValidationError("numeric_sort_required")
        a, b = values
        return {"lt": lambda: a < b, "le": lambda: a <= b,
                "add": lambda: a + b, "sub": lambda: a - b}[op]()
    raise ValidationError("unsupported_operator")


def finite_domains(assertions: tuple[Term, ...]) -> dict[str, tuple[Scalar, ...]]:
    """Read only top-level domain declarations. Nested domains cannot restrict the universe."""
    domains: dict[str, tuple[Scalar, ...]] = {}
    for term in assertions:
        if term.op != "domain":
            continue
        if term.args[0].op != "symbol" or any(a.op != "literal" for a in term.args[1:]):
            raise ValidationError("domain_requires_symbol_and_literals")
        name = str(term.args[0].value)
        values = tuple(a.value for a in term.args[1:])
        if len({type(v) for v in values}) != 1:
            raise ValidationError("mixed_domain_sorts")
        if name in domains:
            previous = {typed_key(v) for v in domains[name]}
            values = tuple(v for v in values if typed_key(v) in previous)
        domains[name] = tuple(dict((typed_key(v), v) for v in values).values())
    for name in sorted({s for term in assertions for s in symbols(term)}):
        domains.setdefault(name, (False, True))
    return dict(sorted(domains.items()))


def literal_assignments(term: Term) -> dict[str, Scalar] | None:
    """Extract a conjunction of symbol=literal assignments; None means unsupported, not a value."""
    if term == literal(True):
        return {}
    parts = term.args if term.op == "and" else (term,)
    result: dict[str, Scalar] = {}
    for part in parts:
        if part.op != "eq" or len(part.args) != 2:
            return None
        a, b = part.args
        if a.op == "literal":
            a, b = b, a
        if a.op != "symbol" or b.op != "literal":
            return None
        name = str(a.value)
        if name in result and typed_key(result[name]) != typed_key(b.value):
            return None
        result[name] = b.value
    return result


def boolean_environments(names: tuple[str, ...]) -> tuple[dict[str, Scalar], ...]:
    """Enumerate the complete Boolean input domain, capped to keep local synthesis bounded."""
    if len(names) > 8:
        raise ValidationError("boolean_domain_too_large")
    return tuple(dict(zip(names, values)) for values in product((False, True), repeat=len(names)))


def infer_sort(term: Term, variable_sorts: Mapping[str, type]) -> type:
    """Statically validate all operator branches, including those in an inconsistent/empty domain."""
    if term.op == 'literal':
        return type(term.value)
    if term.op == 'symbol':
        return variable_sorts[str(term.value)]
    sorts = tuple(infer_sort(a, variable_sorts) for a in term.args)
    if term.op in {'and', 'or', 'not', 'implies'}:
        if any(kind is not bool for kind in sorts):
            raise ValidationError('boolean_sort_required')
        return bool
    if term.op in {'eq', 'ne', 'domain'}:
        if len(set(sorts)) != 1:
            raise ValidationError('equality_or_domain_sort_mismatch')
        return bool
    if term.op in {'lt', 'le', 'add', 'sub'}:
        if len(set(sorts)) != 1 or sorts[0] not in {int, float}:
            raise ValidationError('numeric_sort_required')
        return bool if term.op in {'lt', 'le'} else sorts[0]
    if term.op == 'ite':
        if sorts[0] is not bool or sorts[1] is not sorts[2]:
            raise ValidationError('branch_sort_mismatch')
        return sorts[1]
    raise ValidationError('unsupported_operator')


def declared_sorts(assertions: tuple[Term, ...], domains: Mapping[str, tuple[Scalar, ...]]) -> dict[str, type]:
    """Preserve the declared sort even if intersecting finite domains becomes empty."""
    result = {name: type(values[0]) if values else bool for name, values in domains.items()}
    declared = {}
    for term in assertions:
        if term.op == 'domain':
            name, kind = str(term.args[0].value), type(term.args[1].value)
            if name in declared and declared[name] is not kind:
                raise ValidationError('conflicting_declared_sorts')
            declared[name] = kind
            result[name] = kind
    return result


def reduce_equalities(assertions: tuple[Term, ...], domains: dict[str, tuple[Scalar, ...]]) -> dict[str, tuple[Scalar, ...]]:
    """Soundly narrow domains using conjunctive equalities, never conditional/disjunctive facts."""
    reduced = dict(domains)
    pending = list(assertions)
    atoms = []
    while pending:
        term = pending.pop()
        if term.op == 'and':
            pending.extend(term.args)
        else:
            atoms.append(term)
    changed = True
    while changed:
        changed = False
        for term in atoms:
            if term.op == 'symbol':
                term = Term('eq', (term, literal(True)))
            elif term.op == 'not' and term.args[0].op == 'symbol':
                term = Term('eq', (term.args[0], literal(False)))
            if term.op != 'eq':
                continue
            a, b = term.args
            if a.op == 'literal':
                a, b = b, a
            if a.op != 'symbol' or b.op not in {'symbol', 'literal'}:
                continue
            names = [str(a.value)] + ([str(b.value)] if b.op == 'symbol' else [])
            other = reduced[str(b.value)] if b.op == 'symbol' else (b.value,)
            intersection = {typed_key(value) for value in reduced[str(a.value)]} & {typed_key(value) for value in other}
            for name in names:
                values = tuple(value for value in reduced[name] if typed_key(value) in intersection)
                if values != reduced[name]:
                    reduced[name] = values
                    changed = True
    return reduced
