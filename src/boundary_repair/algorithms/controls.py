"""Independent plain baselines; same evidence/candidate plumbing, no expressivity or scope ranking."""
from dataclasses import dataclass
import json

from boundary_repair.domain.errors import NoAdmissiblePatch
from boundary_repair.domain.repair import (
    BoundaryAssessment, EditKind, ExpressivityVerdict, LocalizationResult, PatchPlan,
    SynthesisResult, SyntaxHole,
)
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import BehaviorConstraint, ClaimKind, ContractSet, Coverage, InterpretationSpace, SolverStatus
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.evidence import EVIDENCE_SCHEMA, EVIDENCE_SYSTEM, parse_evidence
from boundary_repair.kernel.retrieval import candidate_boundaries, snippets
from boundary_repair.ports import ModelPort, ModelRequest, ProgramPort


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
        code = snippets(self.program.index(snapshot, context), snapshot, task.problem_statement, self.context_files, self.max_context_chars)
        prompt = json.dumps({'issue': task.problem_statement, 'assets': [a.source_id for a in task.assets],
                             'base_code': code, 'output_example': EVIDENCE_SCHEMA}, ensure_ascii=False)
        result = self.model.complete(ModelRequest(EVIDENCE_SYSTEM, prompt, task.assets, 'evidence.v1', self.response_tokens), context)
        evidence = parse_evidence(result.text, task, code)
        constraints = tuple(BehaviorConstraint(c.claim_id, c.kind, c.targets, c.statement, c.source_ids)
                            for c in evidence.claims if c.kind != ClaimKind.OBSERVATION)
        return ContractSet((), constraints, (), (), InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.PARTIAL),
                           ('plain_control_no_entailment_no_reachability',))

    def locate(self, task: TaskInput, contracts: ContractSet, snapshot: RepositorySnapshot,
               context: RunContext) -> LocalizationResult:
        """C2: same fixed candidate pool, original lexical order, no solver or negative certificates."""
        boundaries = candidate_boundaries(task, self.program.index(snapshot, context), snapshot, context, self.maximum)
        return LocalizationResult(tuple(BoundaryAssessment(b, ExpressivityVerdict.UNKNOWN, b.readable_features,
                                                          None, ('plain_lexical_control',)) for b in boundaries))

    def synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult,
                   snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult:
        """C3: generate once in the top available scope; do not use six-dimensional cost ordering."""
        available = [a for a in localization.assessments if a.verdict != ExpressivityVerdict.INEXPRESSIBLE or not a.certificate]
        if not available:
            raise NoAdmissiblePatch('plain_no_candidate')
        boundary = available[0].boundary
        context.budget.claim_ideas()
        context.budget.claim_candidates()
        holes = tuple(SyntaxHole(f'plain:{i}', span, 'source-fragment', ()) for i, span in enumerate(boundary.sites))
        plan = PatchPlan('plain:' + boundary.boundary_id, (boundary.boundary_id,), EditKind.FREEFORM,
                         holes, contracts.must + contracts.frames, (),
                         unresolved=('plain_generated_semantics_unproved',), soft_obligations=contracts.may)
        # Sharing schema validation and constrained byte emission does not run G1/G2 or Boolean synthesis.
        from boundary_repair.algorithms.rendering import HoleRenderer
        fillings = HoleRenderer(self.model, self.response_tokens, self.max_context_chars).render(task, plan, snapshot, context)
        patch = self.program.materialize(task, plan, fillings, snapshot, context)
        return SynthesisResult(plan, patch, plan.unresolved)
