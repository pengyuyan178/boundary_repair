"""Complete finite-domain decision procedure, with UNKNOWN on unsupported or bounded-out input."""
import hashlib
import json
import math
from dataclasses import dataclass
from itertools import product

from boundary_repair.domain.errors import BudgetExceeded, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import SolverAnswer, SolverStatus, Term
from boundary_repair.kernel.terms import evaluate, finite_domains, term_from_json, term_json, walk, infer_sort, declared_sorts, reduce_equalities


@dataclass(frozen=True, slots=True)
class LogicAdapter:
    """Exhaustive Boolean/explicit finite scalar logic, not an unbounded JS theorem prover."""
    max_assignments: int = 65536
    max_nodes: int = 4096

    def check(self, assertions: tuple[Term, ...], context: RunContext) -> SolverAnswer:
        """Validate all branches, enumerate the declared domain, and hash the exact checked theory.

        SAT requires a model; UNSAT requires exhaustive coverage. Mixed sorts, unsupported ASTs,
        node/assignment limits and deadline expiry return UNKNOWN, never a negative certificate.
        """
        try:
            context.budget.check_deadline()
            if sum(len(walk(a)) for a in assertions) > self.max_nodes:
                return SolverAnswer(SolverStatus.UNKNOWN, diagnostics=("logic_node_limit",))
            checked = tuple(term_from_json(term_json(a)) for a in assertions)
            domains = finite_domains(checked)
            sorts = declared_sorts(checked, domains)
            if any(infer_sort(term, sorts) is not bool for term in checked):
                raise ValidationError("assertion_not_boolean")
            domains = reduce_equalities(checked, domains)
            count = math.prod(len(v) for v in domains.values())
            if count > self.max_assignments:
                return SolverAnswer(SolverStatus.UNKNOWN, diagnostics=("finite_domain_limit",))
            raw = json.dumps([term_json(t) for t in checked], sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(raw.encode()).hexdigest()
            first_model = None
            # Do not short-circuit at SAT: a later assignment can expose a sort violation.
            for index, values in enumerate(product(*domains.values())):
                if index % 64 == 0:
                    context.budget.check_deadline()
                env = dict(zip(domains, values))
                results = tuple(evaluate(t, env) for t in checked)
                if any(type(v) is not bool for v in results):
                    raise ValidationError("assertion_not_boolean")
                if all(results) and first_model is None:
                    first_model = tuple(env.items())
            context.budget.check_deadline()
            status = SolverStatus.SAT if first_model is not None else SolverStatus.UNSAT
            certificate = f"finite-v1:{status.value}:{count}:{digest}"
            return SolverAnswer(status, certificate, ("complete_declared_finite_domain",), first_model or ())
        except (ValidationError, BudgetExceeded, RecursionError) as exc:
            return SolverAnswer(SolverStatus.UNKNOWN, diagnostics=(str(exc) if isinstance(exc, ValidationError)
                                                                      else type(exc).__name__,))
