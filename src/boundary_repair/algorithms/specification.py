"""Open-world partial specification: quoted evidence -> alternatives -> model-relative MUST/MAY."""

from dataclasses import dataclass, replace
import json

from boundary_repair.domain.errors import EvidenceConflict, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import (
    BehaviorConstraint,
    ClaimKind,
    ContractSet,
    Coverage,
    EntityBinding,
    EvidenceBundle,
    EvidenceClaim,
    InterpretationSpace,
    ObservationInterface,
    SolverStatus,
    Term,
    Witness,
)
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.evidence import evidence_request_v6, parse_evidence_v4, parse_evidence_v6
from boundary_repair.kernel.codec import plain
from boundary_repair.kernel.retrieval import lexical_score, scope_snippets
from boundary_repair.kernel.terms import symbol
from boundary_repair.ports import LogicPort, ModelPort, ProgramPort


def claim_identity(claim: EvidenceClaim) -> str:
    """Canonicalize identical source-bound propositions across alternative interpretations."""
    def canonical(term: Term) -> list:
        """Normalize commutative finite logical terms without interpreting free text."""
        if term.op in {'and', 'or', 'eq', 'ne'}:
            return [term.op, sorted((canonical(a) for a in term.args), key=repr)]
        return [term.op, term.value, [canonical(a) for a in term.args]]
    statement = (['uninterpreted', claim.description] if claim.statement.op == 'symbol'
                 and str(claim.statement.value).startswith('uninterpreted:') else canonical(claim.statement))
    cases = lambda entries: sorted((plain(case) for case in entries), key=lambda item: json.dumps(item, sort_keys=True))
    description = claim.description if not claim.entry_cases else ''
    return json.dumps([claim.kind.value, statement, description, plain(claim.targets), cases(claim.entry_cases),
                       sorted((cases(option) for option in claim.binding_alternatives), key=repr)],
                      ensure_ascii=False, sort_keys=True)


class _EvidenceResponseValidationError(ValidationError):
    """Marks only locally parsed evidence responses as unavailable."""


@dataclass(frozen=True, slots=True)
class SpecificationRecovery:
    """One evidence call; reasoning quantifies only the recorded interpretation theory."""

    model: ModelPort
    program: ProgramPort
    logic: LogicPort
    response_tokens: int = 8000
    context_files: int = 8
    max_context_chars: int = 100000

    def recover(
        self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext
    ) -> ContractSet:
        """Run S1-S4 once; only invalid evidence or an inconsistent theory becomes unavailable.

        Provider, configuration, workspace, and budget errors are deliberately not caught.
        The adapter saves the raw response before parsing. Invalid evidence is never re-requested.
        """
        context.budget.check_deadline()
        try:
            evidence = self.extract_evidence(task, snapshot, context)
        except _EvidenceResponseValidationError as error:
            unavailable = self._unavailable("evidence_extraction_unavailable:validation")
            return replace(unavailable, diagnostics=unavailable.diagnostics +
                           ('evidence_validation_detail:' + str(error.__cause__),))
        if not any(claim.kind != ClaimKind.OBSERVATION for claim in evidence.claims):
            return self._unavailable("evidence_extraction_unavailable:no_normative_claims")
        try:
            bindings = self.bind_entities(evidence, snapshot, context)
            space = self.build_interpretation_space(evidence, bindings, context)
            contracts = self.derive_contracts(evidence, bindings, space, context)
        except EvidenceConflict:
            return self._unavailable("evidence_extraction_unavailable:conflict")
        normative = tuple(claim for claim in evidence.claims if claim.kind != ClaimKind.OBSERVATION)
        status = 'complete' if (space.coverage == Coverage.COMPLETE
                                and all(claim.entry_cases for claim in normative)) else 'partial'
        return replace(contracts, extraction_status=status)

    @staticmethod
    def _unavailable(diagnostic: str) -> ContractSet:
        """Return an explicit empty result without preserving raw model text in diagnostics."""
        theory = InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.PARTIAL)
        return ContractSet(
            must=(),
            may=(),
            frames=(),
            witnesses=(),
            theory=theory,
            diagnostics=(diagnostic,),
            extraction_status="unavailable",
        )

    def extract_evidence(
        self,
        task: TaskInput,
        snapshot: RepositorySnapshot,
        context: RunContext,
    ) -> EvidenceBundle:
        """S1: recover evidence and program-bound entry cases in one frozen-scope request."""
        scope = self.program.source_scope(snapshot, context, query=task.problem_statement)
        code = scope_snippets(scope)
        interfaces = self.program.observation_interfaces(snapshot, context)
        scenarios = self.program.observation_scenarios(snapshot, context)
        response = self.model.complete(
            evidence_request_v6(task, code, self.response_tokens, interfaces, scenarios), context
        )
        return self._parse_evidence_response(response.text, task, code, interfaces, scenarios)

    @staticmethod
    def _parse_evidence_response(
        text: str, task: TaskInput, code: tuple[dict[str, object], ...],
        interfaces: tuple[ObservationInterface, ...] = (),
        scenarios: tuple | None = None,
    ) -> EvidenceBundle:
        """Translate only the untrusted response parser's validation failures."""
        try:
            return (parse_evidence_v4(text, task, code, interfaces) if scenarios is None
                    else parse_evidence_v6(text, task, code, interfaces, scenarios))
        except ValidationError as error:
            raise _EvidenceResponseValidationError from error

    def bind_entities(
        self, evidence: EvidenceBundle, snapshot: RepositorySnapshot, context: RunContext
    ) -> tuple[EntityBinding, ...]:
        """S2: retain all exact symbol bindings; lexical alternatives are explicitly PARTIAL.

        Exact bindings exhaust only names in the indexed snapshot, not all possible visual-to-code
        meanings. An index truncation propagates PARTIAL and cannot establish global coverage.
        """
        index = self.program.index(snapshot, context)
        entities = sorted(
            {target.entity_id for claim in evidence.claims for target in claim.targets}
        )
        bindings = []
        for entity in entities:
            exact = tuple(span for span in index.locations if span.symbol == entity)
            candidates = (
                exact
                or tuple(
                    span
                    for span in index.locations
                    if lexical_score(entity, span.path + " " + span.symbol) > 0
                )[:12]
            )
            complete = bool(exact) and not index.unsupported_constructs
            choices = tuple(symbol(f"bind:{entity}:{i}") for i in range(len(candidates)))
            bindings.append(
                EntityBinding(
                    entity, candidates, choices, Coverage.COMPLETE if complete else Coverage.PARTIAL
                )
            )
        for claim in evidence.claims:
            if claim.binding_alternatives:
                sites = tuple(dict.fromkeys(case.interface.site for option in claim.binding_alternatives for case in option))
                choices = tuple(symbol(f'bind:{claim.claim_id}:{i}') for i in range(len(claim.binding_alternatives)))
                coverage = Coverage.COMPLETE if all(claim.binding_alternatives) else Coverage.PARTIAL
                bindings.append(EntityBinding('claim:' + claim.claim_id, sites, choices, coverage))
        return tuple(bindings)

    def build_interpretation_space(
        self, evidence: EvidenceBundle, bindings: tuple[EntityBinding, ...], context: RunContext
    ) -> InterpretationSpace:
        """S3: encode exactly-one alternatives and whole conjunctions; never flatten all_of.

        Current observations never constrain desired output values. COMPLETE applies only to this
        finite acceptance theory, not every real-world interpretation or repository entity.

        """
        legacy_grouped = {claim_id for group in evidence.choice_groups for claim_id in group}
        v3_grouped = {
            claim_id
            for group in evidence.interpretation_groups
            for alternative in group
            for claim_id in alternative
        }
        grouped = legacy_grouped | v3_grouped
        assumptions = tuple(
            symbol("accept:" + claim.claim_id)
            for claim in evidence.claims
            if claim.claim_id not in grouped
        )
        choices = []
        for group in evidence.choice_groups:
            variables = tuple(symbol("accept:" + claim_id) for claim_id in group)
            choices.append(Term("or", variables))
            choices.extend(
                Term("not", (Term("and", (left, right)),))
                for position, left in enumerate(variables)
                for right in variables[position + 1 :]
            )
        for group_number, group in enumerate(evidence.interpretation_groups, start=1):
            selectors = tuple(
                symbol(
                    f"select:interpretation:{group_number:04d}:alternative:{alternative_number:04d}"
                )
                for alternative_number in range(1, len(group) + 1)
            )
            choices.append(Term("or", selectors))
            choices.extend(
                Term("not", (Term("and", (left, right)),))
                for position, left in enumerate(selectors)
                for right in selectors[position + 1 :]
            )
            for claim_id in sorted({c for alternative in group for c in alternative}):
                membership = Term('or', tuple(s for s, alternative in zip(selectors, group) if claim_id in alternative))
                choices.append(Term('eq', (symbol('accept:' + claim_id), membership)))
        for claim in evidence.claims:
            if not claim.binding_alternatives:
                continue
            acceptance = symbol('accept:' + claim.claim_id)
            selectors = tuple(symbol(f'bind:{claim.claim_id}:{i}') for i in range(len(claim.binding_alternatives)))
            choices.append(Term('eq', (acceptance, Term('or', selectors))))
            choices.extend(Term('not', (Term('and', (left, right)),))
                           for i, left in enumerate(selectors) for right in selectors[i + 1:])
        result = self.logic.check(assumptions + tuple(choices), context)
        if result.status == SolverStatus.UNSAT:
            raise EvidenceConflict("inconsistent_acceptance_theory")
        return InterpretationSpace(
            assumptions,
            tuple(choices),
            result.status,
            Coverage.COMPLETE if result.status == SolverStatus.SAT else Coverage.PARTIAL,
        )

    def derive_contracts(
        self,
        evidence: EvidenceBundle,
        bindings: tuple[EntityBinding, ...],
        space: InterpretationSpace,
        context: RunContext,
    ) -> ContractSet:
        """S4: classify accepted requirements by entailment; uncertain frames remain MAY.

        Witnesses contain cited input contexts only. Reachability remains UNKNOWN until a
        supported code adapter establishes entry-local reachability. MUST is theory-relative,
        not a perception proof.
        """
        if space.consistency == SolverStatus.UNSAT:
            raise EvidenceConflict("vacuous_entailment_rejected")
        must, may, frames, witnesses = [], [], [], []
        diagnostics = ["MUST_is_relative_to_normalized_acceptance_theory_not_a_perception_proof",
                       'entry_cases_are_evidence_interpretations_not_verified_UI_grounding']
        normalized = {}
        for item in evidence.claims:
            normalized.setdefault(claim_identity(item), []).append(item)
        for equivalent in normalized.values():
            claim = equivalent[0]
            if claim.kind == ClaimKind.OBSERVATION:
                continue
            acceptance = Term('or', tuple(symbol('accept:' + item.claim_id) for item in equivalent))
            negated = self.logic.check(
                space.assumptions + space.choices + (Term("not", (acceptance,)),),
                context,
            )
            positive = self.logic.check(space.assumptions + space.choices + (acceptance,), context)
            bound = list(claim.entry_cases)
            for option in claim.binding_alternatives:
                for case in option:
                    if case in bound:
                        continue
                    membership = []
                    for item in equivalent:
                        membership.extend(symbol(f'bind:{item.claim_id}:{i}')
                                          for i, alternative in enumerate(item.binding_alternatives) if case in alternative)
                    absent = self.logic.check(space.assumptions + space.choices +
                        (Term('not', (Term('or', tuple(membership)),)),), context)
                    if space.coverage == Coverage.COMPLETE and absent.status == SolverStatus.UNSAT:
                        bound.append(case)
            sources = tuple(dict.fromkeys(s for item in equivalent for s in item.source_ids))
            constraint = BehaviorConstraint(
                claim.claim_id,
                claim.kind,
                claim.targets,
                claim.statement,
                sources,
                description=claim.description,
                entry_cases=tuple(bound),
                binding_status='program_bound' if bound else 'ambiguous' if claim.binding_alternatives else 'unbound',
                binding_alternatives=claim.binding_alternatives,
            )
            certain = (
                space.consistency == SolverStatus.SAT
                and space.coverage == Coverage.COMPLETE
                and negated.status == SolverStatus.UNSAT
            )
            if certain:
                (frames if claim.kind == ClaimKind.FRAME else must).append(constraint)
                if not bound:
                    diagnostics.append('unbound_hard_constraint:' + claim.claim_id)
                for number, case in enumerate(bound):
                    target = case.target
                    witnesses.append(
                        Witness(
                            f"{claim.claim_id}:{number}",
                            (target,),
                            (target.context,),
                            SolverStatus.UNKNOWN,
                            sources,
                            case.interface,
                            (case.expected,),
                        )
                    )
            elif positive.status != SolverStatus.UNSAT:
                may.append(constraint)
            else:
                diagnostics.append("rejected_interpretation:" + claim.claim_id)
        if any(binding.coverage == Coverage.PARTIAL for binding in bindings):
            diagnostics.append("entity_bindings_partial")
        if context.policy.require_witness and not witnesses:
            diagnostics.append("no_cited_witness_no_hard_pruning")
        return ContractSet(
            must=tuple(must),
            may=tuple(may),
            frames=tuple(frames),
            witnesses=tuple(witnesses),
            theory=space,
            diagnostics=tuple(diagnostics),
            interpretation_groups=evidence.interpretation_groups,
            sources=evidence.sources,
            bindings=bindings,
        )
