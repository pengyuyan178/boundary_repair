"""Repair-interface expressivity: model-restricted certificates, never global file exclusion."""
from dataclasses import dataclass, replace
from itertools import combinations
import hashlib

from boundary_repair.domain.repair import BoundaryAssessment, Feature, ExpressivityVerdict, LocalRepairModel, LocalizationResult, RepairBoundary
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import ContractSet, Coverage, SolverStatus, Term
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.retrieval import candidate_boundaries
from boundary_repair.kernel.terms import literal, literal_assignments, substitute, symbol, symbols
from boundary_repair.ports import LogicPort, ProgramPort


@dataclass(frozen=True, slots=True)
class ExpressivityLocalization:
    """Shared retrieval + checked local summaries + stable three-valued interface assessment."""
    program: ProgramPort
    logic: LogicPort
    maximum: int = 40

    def locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot,
               context: RunContext) -> LocalizationResult:
        """Assess a fixed candidate pool; no candidate patch or evaluator outcome is consumed."""
        boundaries = self.enumerate_boundaries(task, contracts, snapshot, context)
        assessments = []
        for boundary in boundaries:
            context.budget.check_deadline()
            model = self.build_local_model(boundary, contracts, snapshot, context)
            assessments.append(self.assess_expressivity(model, contracts, context))
        return self.rank_boundaries(tuple(assessments), contracts, context)

    def enumerate_boundaries(self, task: TaskInput, contracts: ContractSet,
                             snapshot: RepositorySnapshot, context: RunContext) -> tuple[RepairBoundary, ...]:
        """L1: enumerate read/modify interfaces; identical retrieval is reused by the plain control."""
        return candidate_boundaries(task, self.program.index(snapshot, context), snapshot, context, self.maximum)

    def build_local_model(self, boundary: RepairBoundary, contracts: ContractSet,
                          snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel:
        """L2: pull MUST/frame requirements through the supported identity continuation.

        Only matching, cited witnesses may constrain local outputs. MAY never produces a hard
        exclusion. Missing witnesses, projections or mappings downgrade the model to PARTIAL.
        """
        model = self.program.summarize(boundary, contracts.witnesses, snapshot, context)
        constraints = list(model.constraints)
        covered = set()
        reliable = model.coverage == Coverage.COMPLETE
        for index, witness in enumerate(model.witnesses):
            targets = witness.observations
            assignments = literal_assignments(targets[0].context) if len(targets) == 1 else None
            if assignments is None:
                reliable = False
                continue
            for claim in contracts.must + contracts.frames:
                if not targets or targets[0] not in claim.targets:
                    continue
                replacements = {name: literal(value) for name, value in assignments.items()}
                replacements.update({'return': model.output_terms[index], 'base:return': symbol(f'base_out:{index}')})
                relation = substitute(claim.relation, replacements)
                if set(symbols(relation)) - {f'local_out:{index}', f'base_out:{index}'}:
                    reliable = False
                    continue
                constraints.append(relation)
                covered.add(claim.constraint_id)
        if {c.constraint_id for c in contracts.must + contracts.frames} - covered:
            reliable = False
        return replace(model, constraints=tuple(constraints), coverage=Coverage.COMPLETE if reliable else Coverage.PARTIAL)

    def assess_expressivity(self, model: LocalRepairModel, contracts: ContractSet,
                            context: RunContext) -> BoundaryAssessment:
        """L3: require all four premises and complete declared scope before issuing a certificate."""
        assumptions = model.assumptions
        missing = tuple(name for name in ('reads_complete', 'pure_deterministic', 'witnesses_reachable', 'downstream_sound')
                        if not getattr(assumptions, name))
        if model.coverage != Coverage.COMPLETE or missing or not model.witnesses:
            return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN,
                                      model.boundary.readable_features, None,
                                      missing + ('unsupported_or_partial_local_semantics',))
        answer = self.logic.check(model.constraints, context)
        if answer.status == SolverStatus.UNKNOWN:
            return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN,
                                      model.boundary.readable_features, None, answer.diagnostics)
        verdict = ExpressivityVerdict.FEASIBLE if answer.status == SolverStatus.SAT else ExpressivityVerdict.INEXPRESSIBLE
        source_version = '|'.join(span.content_sha256 for span in model.boundary.sites)
        certificate = f'entry-boolean:{model.boundary.boundary_id}:{source_version}:{answer.certificate}'
        required = model.boundary.readable_features
        if verdict == ExpressivityVerdict.FEASIBLE and len(required) <= 8:
            required = self.minimum_features(model, context)
        return BoundaryAssessment(model.boundary, verdict, required, certificate,
                                  ('proof_scope:direct_boolean_function_entry_not_UI_reachability',))

    def minimum_features(self, model: LocalRepairModel, context: RunContext) -> tuple[Feature, ...]:
        """Find a smallest read subset by adding equal-output constraints for input collisions.

        Unsupported input valuations leave the original interface intact. Each subset is solved
        against all joint obligations, not just pairwise disjointness or lexical importance.
        """
        features = tuple(sorted(model.boundary.readable_features, key=lambda f: f.feature_id))
        inputs = [literal_assignments(term) for term in model.input_terms]
        if any(row is None for row in inputs):
            return features
        for size in range(len(features) + 1):
            for subset in combinations(features, size):
                extra = []
                names = tuple(f.feature_id for f in subset)
                for i, left in enumerate(inputs):
                    for j in range(i):
                        if all(name in left and name in inputs[j] and left[name] == inputs[j][name] for name in names):
                            extra.append(Term('eq', (model.output_terms[i], model.output_terms[j])))
                answer = self.logic.check(model.constraints + tuple(extra), context)
                if answer.status == SolverStatus.SAT:
                    return subset
        return features

    def rank_boundaries(self, assessments: tuple[BoundaryAssessment, ...], contracts: ContractSet,
                         context: RunContext) -> LocalizationResult:
        """L4: retain every assessment for auditing; only certified inexpressible interfaces are excluded downstream."""
        order = {ExpressivityVerdict.FEASIBLE: 0, ExpressivityVerdict.UNKNOWN: 1, ExpressivityVerdict.INEXPRESSIBLE: 2}
        ranked = tuple(sorted(assessments, key=lambda a: (order[a.verdict],
                       -a.boundary.relevance, len(a.unresolved), len(a.required_features), a.boundary.boundary_id)))
        usable = any(a.verdict != ExpressivityVerdict.INEXPRESSIBLE or not a.certificate for a in ranked)
        return LocalizationResult(ranked, () if usable else ('no_admissible_interface',))
