"""Independent plain baselines; same evidence/candidate plumbing, no expressivity or scope ranking."""
from dataclasses import dataclass

from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.repair import (
    BoundaryAssessment, ExpressivityVerdict, LocalizationResult,
    SynthesisResult,
)
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import BehaviorConstraint, ClaimKind, ContractSet, Coverage, InterpretationSpace, SolverStatus, Witness
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.evidence import evidence_request_v6, parse_evidence_v6
from boundary_repair.kernel.retrieval import candidate_boundaries, scope_snippets
from boundary_repair.ports import ModelPort, ProgramPort


@dataclass(frozen=True, slots=True)
class PlainControls:
    """Three independently selectable controls; sharing parsing is not sharing research conclusions."""
    model: ModelPort
    program: ProgramPort
    maximum: int = 40
    response_tokens: int = 8000
    context_files: int = 8
    max_context_chars: int = 100000

    def recover(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> ContractSet:
        """C1: retain the first sourced interpretation as explicit assumptions without entailment."""
        code = scope_snippets(self.program.source_scope(snapshot, context, task.problem_statement))
        interfaces = self.program.observation_interfaces(snapshot, context)
        scenarios = self.program.observation_scenarios(snapshot, context)
        result = self.model.complete(evidence_request_v6(task, code, self.response_tokens, interfaces, scenarios), context)
        theory = InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.PARTIAL)
        try:
            evidence = parse_evidence_v6(result.text, task, code, interfaces, scenarios)
        except ValidationError:
            return ContractSet((), (), (), (), theory, ('evidence_extraction_unavailable:validation',), 'unavailable')
        grouped = {cid for group in evidence.interpretation_groups for option in group for cid in option}
        selected = {cid for group in evidence.interpretation_groups for cid in group[0]}
        must, may, frames, witnesses = [], [], [], []
        for claim in evidence.claims:
            if claim.kind == ClaimKind.OBSERVATION:
                continue
            cases = claim.binding_alternatives[0] if claim.binding_alternatives else claim.entry_cases
            constraint = BehaviorConstraint(claim.claim_id, claim.kind, claim.targets, claim.statement, claim.source_ids,
                claim.description, cases, 'assumed_first_binding' if cases else 'unbound', claim.binding_alternatives)
            if claim.claim_id in grouped and claim.claim_id not in selected:
                may.append(constraint)
                continue
            (frames if claim.kind == ClaimKind.FRAME else must).append(constraint)
            witnesses.extend(Witness(f'{claim.claim_id}:{i}', (case.target,), (case.target.context,),
                SolverStatus.UNKNOWN, claim.source_ids, case.interface, (case.expected,)) for i, case in enumerate(cases))
        return ContractSet(tuple(must), tuple(may), tuple(frames), tuple(witnesses), theory,
            ('plain_control_first_interpretation_assumed_not_entailed',), 'partial' if must or frames else 'unavailable',
            interpretation_groups=evidence.interpretation_groups, sources=evidence.sources,
            specification_policy='first_sourced_interpretation')

    def locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot,
               context: RunContext) -> LocalizationResult:
        """C2: same fixed candidate pool, original lexical order, no solver or negative certificates."""
        boundaries = candidate_boundaries(task, self.program.index(snapshot, context), snapshot, context, self.maximum)
        return LocalizationResult(tuple(BoundaryAssessment(b, ExpressivityVerdict.UNKNOWN, b.readable_features,
                                                          None, ('plain_lexical_control',)) for b in boundaries))

    def synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult,
                   snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult:
        """C3: generate once in the top available scope; do not use six-dimensional cost ordering."""
        from boundary_repair.algorithms.rendering import TransactionRenderer
        from boundary_repair.algorithms.synthesis import scoped_localization_plan
        scope = self.program.source_scope(snapshot, context, task.problem_statement)
        plan = scoped_localization_plan(contracts, scope, localization, snapshot, context)
        self.program.freeze_plan(plan, context)
        transaction = TransactionRenderer(self.model, self.response_tokens).render(task, plan, context)
        patch = self.program.compile(task, plan, transaction, snapshot, context)
        return SynthesisResult(plan, patch, plan.unresolved)
