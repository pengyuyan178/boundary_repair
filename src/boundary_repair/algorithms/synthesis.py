"""Property-scoped plans, lexicographic risk ordering, finite Boolean synthesis and single-call holes."""
from dataclasses import dataclass, replace
import hashlib
import json

from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import (
    EditKind, ExpressivityVerdict, HoleFilling, LocalizationResult, PatchPlan, ScopeCost,
    SynthesisResult, SyntaxHole,
)
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import ClaimKind, ContractSet, Coverage
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.boolean import synthesize_boolean
from boundary_repair.kernel.codec import plain, require_keys, strict_json, text_field
from boundary_repair.kernel.files import source_slice
from boundary_repair.kernel.terms import evaluate, literal_assignments, symbols
from boundary_repair.ports import LogicPort, ModelPort, ModelRequest, ProgramPort

def boolean_cases(plan: PatchPlan) -> tuple[tuple[dict, bool], ...] | None:
    """Convert explicit return-value obligations to finite examples; unsupported is not empty."""
    if len(plan.holes) != 1:
        return None
    hole = plan.holes[0]
    cases = []
    for obligation in plan.obligations:
        if len(obligation.targets) != 1:
            return None
        target = obligation.targets[0]
        if target.entity_id != hole.site.symbol or target.property_name != 'return':
            return None
        values = literal_assignments(target.context)
        if values is None or any(type(v) is not bool for v in values.values()):
            return None
        relation = obligation.relation
        if relation.op != 'eq' or len(relation.args) != 2:
            return None
        left, right = relation.args
        if left.op == 'literal':
            left, right = right, left
        if left.op != 'symbol' or left.value != 'return' or right.op != 'literal' or type(right.value) is not bool:
            return None
        cases.append((values, right.value))
    return tuple(cases) if cases else None


@dataclass(frozen=True, slots=True)
class ScopeSynthesis:
    """Plan before rendering code. The official evaluator is absent from all dependencies."""
    model: ModelPort
    program: ProgramPort
    logic: LogicPort
    response_tokens: int = 8000
    max_context_chars: int = 100000

    def synthesize(self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult,
                   snapshot: RepositorySnapshot, context: RunContext) -> SynthesisResult:
        """Run G1-G3 and syntax/hash materialization once; keep unresolved obligations in output."""
        context.budget.check_deadline()
        plans = self.enumerate_plans(contracts, localization, snapshot, context)
        plan = self.select_minimal_scope(plans, contracts, snapshot, context)
        fillings = self.fill_holes(task, plan, snapshot, context)
        patch = self.program.materialize(task, plan, fillings, snapshot, context)
        return SynthesisResult(plan, patch, plan.unresolved)

    def enumerate_plans(self, contracts: ContractSet, localization: LocalizationResult,
                        snapshot: RepositorySnapshot, context: RunContext) -> tuple[PatchPlan, ...]:
        """G1: produce bounded, fixed plans and count candidate/idea budgets before generation.

        Primitive types constrain scope but do not pretend every consumer/alias transform has a
        proved semantic adapter. UNKNOWN candidates remain usable, with explicit obligations.
        Multiple samples are not adaptive retries; this version emits one selected filling.
        """
        plans = []
        kinds = set()
        available = context.budget.limits.max_patch_candidates - context.budget.patch_candidates
        for assessment in localization.assessments:
            if len(plans) >= available:
                break
            if assessment.verdict == ExpressivityVerdict.INEXPRESSIBLE and assessment.certificate:
                continue
            boundary = assessment.boundary
            if boundary.edit_kind not in kinds:
                if context.budget.ideas >= context.budget.limits.max_ideas:
                    continue
                context.budget.claim_ideas()
                kinds.add(boundary.edit_kind)
            holes = tuple(SyntaxHole(f'{boundary.boundary_id}:{i}', site,
                          'boolean-expression' if site.node_kind == 'BooleanReturn' and assessment.verdict == ExpressivityVerdict.FEASIBLE
                          else 'source-fragment', tuple(f.feature_id for f in assessment.required_features))
                          for i, site in enumerate(boundary.sites))
            context.budget.claim_candidates()
            unresolved = tuple(c.constraint_id for c in contracts.must + contracts.frames)
            if assessment.verdict == ExpressivityVerdict.UNKNOWN:
                unresolved += ('local_semantics_unknown',)
            plans.append(PatchPlan('plan:' + boundary.boundary_id, (boundary.boundary_id,), boundary.edit_kind,
                                   holes, contracts.must + contracts.frames, (), None, unresolved, contracts.may))
        if not plans:
            raise NoAdmissiblePatch('no_plan_within_declared_interfaces')
        return tuple(plans)

    def select_minimal_scope(self, plans: tuple[PatchPlan, ...], contracts: ContractSet,
                             snapshot: RepositorySnapshot, context: RunContext) -> PatchPlan:
        """G2: rank unresolved requirements, frames, protected risks, extra scope, constants, size.

        Analysis is conservative: an effect touching a protected property is a risk, not a proved
        violation. No empty effect is interpreted as zero risk. No pre-fill obligation is claimed
        discharged. Relevance order breaks exact cost ties without reading test outcomes.
        """
        ranked = []
        for ordinal, plan in enumerate(plans):
            effects = self.program.effects(plan, snapshot, context)
            unknown = sum(effect.coverage == Coverage.PARTIAL for effect in effects) or int(not effects)
            protected = 0
            target_pairs = {(t.entity_id, t.property_name) for c in contracts.must for t in c.targets}
            for frame in contracts.frames:
                if any(effect.coverage == Coverage.PARTIAL or effect.target.property_name == '*'
                       or any((effect.target.entity_id, effect.target.property_name) == (t.entity_id, t.property_name)
                              for t in frame.targets) for effect in effects):
                    protected += 1
            extras = sum((effect.target.entity_id, effect.target.property_name) not in target_pairs for effect in effects)
            # Unparsed spans have unknown AST size, not a fabricated byte-to-node conversion.
            raw_size = sum(hole.site.node_count if hole.site.node_count > 0 else 1000000
                           for hole in plan.holes)
            supported = all(h.expected_type == 'boolean-expression' for h in plan.holes)
            cost = ScopeCost(len(contracts.must) + (0 if supported else unknown),
                             len(contracts.frames) + unknown, protected, extras + unknown, 0, raw_size)
            unresolved = plan.unresolved + (('partial_effect_summary',) if unknown else ())
            ranked.append((cost, ordinal, replace(plan, effects=effects, cost=cost, unresolved=unresolved)))
        if not ranked:
            raise NoAdmissiblePatch('empty_plan_pool')
        return min(ranked, key=lambda row: row[:2])[2]

    def fill_holes(self, task: TaskInput, plan: PatchPlan, snapshot: RepositorySnapshot,
                   context: RunContext) -> tuple[HoleFilling, ...]:
        """G3: synthesize the supported Boolean grammar first; otherwise one JSON hole-filling call.

        Missing samples, unsupported frames or an exhausted finite grammar do not create a
        success. General code is produced once and remains semantically unproven. No error text
        or evaluation result is fed back to a second model attempt.
        """
        if len(plan.holes) == 1 and plan.holes[0].expected_type == 'boolean-expression':
            cases = boolean_cases(plan)
            if cases is not None:
                expression = synthesize_boolean(plan.holes[0].allowed_symbols, cases, context)
                if expression is not None:
                    return (HoleFilling(plan.holes[0].hole_id, expression.source),)
        from boundary_repair.algorithms.rendering import HoleRenderer
        return HoleRenderer(self.model, self.response_tokens, self.max_context_chars).render(task, plan, snapshot, context)
