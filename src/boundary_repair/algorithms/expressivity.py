"""Repair-interface expressivity: model-restricted certificates, never global file exclusion."""
from dataclasses import dataclass, replace
from itertools import combinations

from boundary_repair.domain.repair import BoundaryAssessment, Feature, ExpressivityVerdict, InterfaceCertificate, LocalRepairModel, LocalizationResult, RepairBoundary
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import ContractSet, Coverage, SolverStatus, Term
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.retrieval import candidate_boundaries
from boundary_repair.kernel.terms import literal_assignments, substitute
from boundary_repair.kernel.terms import symbols, typed_key
from boundary_repair.kernel.boolean import synthesize_finite
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
        if model.proof_scope == 'finite_source_projection':
            cases = {f'{claim.constraint_id}:{number}': (claim, case)
                     for claim in contracts.must + contracts.frames for number, case in enumerate(claim.entry_cases)}
            valid = True
            for witness in contracts.witnesses:
                if witness.interface is None or witness.interface.kind != 'local_projection':
                    continue
                obligation = cases.get(witness.witness_id)
                if obligation is None:
                    valid = False
                    continue
                claim, case = obligation
                if (witness.interface != case.interface or witness.observations != (case.target,)
                        or witness.source_ids != claim.source_ids or len(witness.expected_values) != 1
                        or typed_key(witness.expected_values[0]) != typed_key(case.expected)):
                    valid = False
            remaining = tuple('uncovered_entry_case:' + f'{claim.constraint_id}:{number}'
                              for claim in contracts.must + contracts.frames
                              for number, case in enumerate(claim.entry_cases)
                              if f'{claim.constraint_id}:{number}' not in model.covered_obligations)
            remaining += tuple('unbound_hard_constraint:' + claim.constraint_id
                               for claim in contracts.must + contracts.frames if not claim.entry_cases)
            return replace(model, diagnostics=model.diagnostics + remaining + (() if valid else ('projection_witness_obligation_mismatch',)),
                           coverage=model.coverage if valid else Coverage.PARTIAL)
        constraints = list(model.constraints)
        covered = set()
        allowed = []
        reliable = model.coverage == Coverage.COMPLETE
        obligations = {f'{claim.constraint_id}:{number}': (claim, case)
                       for claim in contracts.must + contracts.frames
                       for number, case in enumerate(claim.entry_cases)}
        for index, witness in enumerate(model.witnesses):
            obligation = obligations.get(witness.witness_id)
            if obligation is None:
                reliable = False
                continue
            claim, case = obligation
            if (witness.interface != case.interface or witness.observations != (case.target,)
                    or not claim.source_ids or witness.source_ids != claim.source_ids):
                reliable = False
                continue
            relation = substitute(case.relation, {'return': model.output_terms[index]})
            constraints.append(relation)
            covered.add(witness.witness_id)
            allowed.append((case.expected,))
        relevant = {key for key, (_, case) in obligations.items()
                    if any(case.interface.site.path == site.path and case.interface.site.start_byte == site.start_byte
                           and case.interface.site.end_byte == site.end_byte for site in boundary.sites)}
        if relevant - covered:
            reliable = False
        diagnostics = tuple('unbound_hard_constraint:' + claim.constraint_id
                            for claim in contracts.must + contracts.frames if not claim.entry_cases)
        diagnostics += tuple('uncovered_entry_case:' + key for key in sorted(set(obligations) - covered))
        return replace(model, constraints=tuple(constraints), coverage=Coverage.COMPLETE if reliable else Coverage.PARTIAL,
                       diagnostics=model.diagnostics + diagnostics,
                       allowed_outputs=tuple(allowed), covered_obligations=tuple(sorted(covered)))

    def assess_expressivity(self, model: LocalRepairModel, contracts: ContractSet,
                            context: RunContext) -> BoundaryAssessment:
        """L3: require all four premises and complete declared scope before issuing a certificate."""
        assumptions = model.assumptions
        missing = tuple(name for name in ('reads_complete', 'pure_deterministic', 'witnesses_reachable', 'downstream_sound')
                        if not getattr(assumptions, name))
        if model.coverage != Coverage.COMPLETE or missing or not model.witnesses:
            return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN,
                                      model.boundary.readable_features, None,
                                      missing + model.diagnostics + ('unsupported_or_partial_local_semantics',))
        answer = self.logic.check(model.constraints, context)
        if answer.status == SolverStatus.UNKNOWN:
            return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN,
                                      model.boundary.readable_features, None, answer.diagnostics)
        verdict = ExpressivityVerdict.FEASIBLE if answer.status == SolverStatus.SAT else ExpressivityVerdict.INEXPRESSIBLE
        source_version = '|'.join(span.content_sha256 for span in model.boundary.sites)
        certificate = f'entry-boolean:{model.boundary.boundary_id}:{source_version}:{answer.certificate}'
        required = model.boundary.readable_features
        if model.proof_scope == 'finite_source_projection':
            construction = None
            if verdict == ExpressivityVerdict.FEASIBLE:
                cases = tuple((literal_assignments(term), allowed) for term, allowed in zip(model.input_terms, model.allowed_outputs))
                expression = synthesize_finite(tuple(f.feature_id for f in required), cases, model.grammar_atoms,
                                              model.grammar_literals, model.output_sort, context)
                if expression is None:
                    return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN, required, None,
                        model.diagnostics + ('finite_grammar_realization_not_found',), model.covered_obligations,
                        proof_scope=model.proof_scope)
                construction = expression.source
                required = tuple(f for f in required if f.feature_id in symbols(expression.term))
            proof = InterfaceCertificate(model.covered_obligations, model.input_terms, model.allowed_outputs,
                model.output_domain, model.assumptions,
                tuple(dict.fromkeys(w.interface.snapshot_sha256 for w in model.witnesses)),
                tuple(dict.fromkeys(w.interface.summary_key for w in model.witnesses)),
                boundary_id=model.boundary.boundary_id,
                readable_features=tuple(f.feature_id for f in model.boundary.readable_features),
                specification_policy=contracts.specification_policy)
            certificate = f'projection-v1:{model.boundary.boundary_id}:{source_version}:{answer.certificate}'
            return BoundaryAssessment(model.boundary, verdict, required, certificate,
                model.diagnostics + ('proof_scope:declared_entry_and_source_projection',
                                     'entry_case_evidence_interpretation_not_verified',
                                     'whole_UI_reachability_and_unobserved_effects_unproved'),
                model.covered_obligations, construction, model.proof_scope, proof)
        if verdict == ExpressivityVerdict.FEASIBLE and len(required) <= 8:
            required = self.minimum_features(model, context)
        construction = None
        if verdict == ExpressivityVerdict.FEASIBLE:
            cases = tuple((literal_assignments(term), allowed) for term, allowed in zip(model.input_terms, model.allowed_outputs))
            expression = synthesize_finite(tuple(f.feature_id for f in required), cases, model.grammar_atoms,
                                          model.grammar_literals, 'boolean', context)
            if expression is None:
                return BoundaryAssessment(model.boundary, ExpressivityVerdict.UNKNOWN, required, None,
                    model.diagnostics + ('finite_grammar_realization_not_found',), model.covered_obligations)
            construction = expression.source
        proof = InterfaceCertificate(model.covered_obligations, model.input_terms, model.allowed_outputs,
            model.output_domain, model.assumptions,
            tuple(dict.fromkeys(w.interface.snapshot_sha256 for w in model.witnesses)), (),
            boundary_id=model.boundary.boundary_id,
            readable_features=tuple(f.feature_id for f in model.boundary.readable_features),
            specification_policy=contracts.specification_policy)
        return BoundaryAssessment(model.boundary, verdict, required, certificate,
                                  model.diagnostics + ('proof_scope:direct_boolean_function_entry_not_UI_reachability',
                                   'entry_case_evidence_interpretation_not_verified'), model.covered_obligations,
                                   construction, model.proof_scope, proof)

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
