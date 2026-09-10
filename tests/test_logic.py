"""Finite solver and logic AST acceptance tests; no API or benchmark inputs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import unittest
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.domain.specification import Term, SolverStatus
from boundary_repair.domain.runtime import RunContext, SearchPolicy, BudgetLedger, BudgetLimits
from boundary_repair.kernel.terms import literal as L, symbol as S, term_from_json, literal_assignments
from boundary_repair.domain.errors import ValidationError


def context():
    return RunContext('test', 'test', 42, SearchPolicy(), BudgetLedger(BudgetLimits()))

class LogicTests(unittest.TestCase):
    def test_sat_assignment(self):
        result = LogicAdapter().check((S('x'),), context())
        self.assertEqual(result.status, SolverStatus.SAT)
        self.assertEqual(dict(result.assignment), {'x': True})

    def test_contradiction_certificate(self):
        result = LogicAdapter().check((S('x'), Term('not', (S('x'),))), context())
        self.assertEqual(result.status, SolverStatus.UNSAT)
        self.assertIn('finite-v1:unsat:0', result.certificate)

    def test_finite_integer_domain(self):
        result = LogicAdapter().check((Term('domain', (S('x'), L(0), L(1))),
                                      Term('eq', (S('x'), L(1)))), context())
        self.assertEqual(dict(result.assignment), {'x': 1})

    def test_mixed_sort_unknown(self):
        result = LogicAdapter().check((Term('eq', (L(True), L(1))),), context())
        self.assertEqual(result.status, SolverStatus.UNKNOWN)

    def test_nested_domain_does_not_truncate(self):
        result = LogicAdapter().check((Term('not', (Term('domain', (S('x'), L(False))),)),), context())
        self.assertEqual(dict(result.assignment), {'x': True})

    def test_truncation_unknown(self):
        result = LogicAdapter(max_assignments=1).check((Term('or', (S('x'), S('y'))),), context())
        self.assertEqual(result.status, SolverStatus.UNKNOWN)

    def test_unsupported_unknown(self):
        result = LogicAdapter().check((Term('execute', value='print(1)'),), context())
        self.assertEqual(result.status, SolverStatus.UNKNOWN)

    def test_unknown_values_not_null(self):
        self.assertIsNone(literal_assignments(Term('or', (S('x'), S('y')))))
        self.assertEqual(literal_assignments(Term('eq', (S('x'), L(None)))), {'x': None})

    def test_deadline_unknown(self):
        ctx = context()
        ctx.budget.started_at = -1e9
        self.assertEqual(LogicAdapter().check((L(True),), ctx).status, SolverStatus.UNKNOWN)

    def test_empty_theory_sat(self):
        self.assertEqual(LogicAdapter().check((), context()).status, SolverStatus.SAT)

    def test_unsupported_unreachable_branch_unknown(self):
        bad = Term('or', (L(True), Term('eq', (L(True), L(1)))))
        self.assertEqual(LogicAdapter().check((bad,), context()).status, SolverStatus.UNKNOWN)

    def test_extra_json_field_rejected(self):
        with self.assertRaises(ValidationError):
            term_from_json({'op': 'literal', 'value': True, 'trusted': True})

if __name__ == '__main__':
    unittest.main()
