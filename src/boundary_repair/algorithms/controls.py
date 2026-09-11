"""Independent plain baselines; same evidence/candidate plumbing, no expressivity or scope ranking."""
from dataclasses import dataclass

from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.repair import (
    BoundaryAssessment, ExpressivityVerdict, LocalizationResult,
    SynthesisResult,
)
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import BehaviorConstraint, ClaimKind, ContractSet, Coverage, InterpretationSpace, SolverStatus
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.evidence import evidence_request_v4, parse_evidence_v4
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
        """C1: one ordinary evidence/caption request; all proposed requirements remain MAY."""
        code = scope_snippets(self.program.source_scope(snapshot, context, task.problem_statement))
        interfaces = self.program.observation_interfaces(snapshot, context)
        result = self.model.complete(evidence_request_v4(task, code, self.response_tokens, interfaces), context)
        theory = InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.PARTIAL)
        try:
            evidence = parse_evidence_v4(result.text, task, code, interfaces)
        except ValidationError:
            return ContractSet((), (), (), (), theory, ('evidence_extraction_unavailable:validation',), 'unavailable')
        constraints = tuple(BehaviorConstraint(c.claim_id, c.kind, c.targets, c.statement, c.source_ids,
                                               c.description, c.entry_cases)
                            for c in evidence.claims if c.kind != ClaimKind.OBSERVATION)
        return ContractSet((), constraints, (), (), theory,
                           ('plain_control_no_entailment_no_reachability',), 'partial' if constraints else 'unavailable',
                           interpretation_groups=evidence.interpretation_groups, sources=evidence.sources)

    def locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot,
               context: RunContext) -> LocalizationResult:
        """C2: same fixed candidate pool, original lexical order, no solver or negative certificates."""
        boundaries = candidate_boundaries(task, self.program.index(snapshot, context), snapshot, context, self.maximum)
        return LocalizationResult(tuple(BoundaryAssessment(b, ExpressivityVerdict.UNKNOWN, b.readable_features,
                                                          None, ('plain_lexical_control',)) for b in boundaries))

    def synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult,
                   snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult:
        """C3: generate once in the top available scope; do not use six-dimensional cost ordering."""
        from boundary_repair.algorithms.rendering import TransactionRenderer, transaction_plan
        scope = self.program.source_scope(snapshot, context, task.problem_statement)
        plan = transaction_plan(contracts, scope, context, 'plain')
        self.program.freeze_plan(plan, context)
        transaction = TransactionRenderer(self.model, self.response_tokens).render(task, plan, context)
        patch = self.program.compile(task, plan, transaction, snapshot, context)
        return SynthesisResult(plan, patch, plan.unresolved)
