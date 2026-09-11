"""Property-scoped plans, lexicographic risk ordering, finite Boolean synthesis and single-call holes."""
from dataclasses import dataclass, replace
import hashlib
import json

from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import (
    BoundaryGuidance, EditScope, ExpressivityVerdict, HoleFilling, LocalizationResult, PatchPlan,
    RepairBoundary, PlanAssessment, ScopeCost, SemanticScopeCost, SyntaxHole, SynthesisResult,
)
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import ContractSet, Coverage
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.boolean import synthesize_boolean
from boundary_repair.kernel.files import source_slice
from boundary_repair.kernel.retrieval import syntax_holes
from boundary_repair.ports import LogicPort, ModelPort, ProgramPort

def boolean_cases(plan: PatchPlan) -> tuple[tuple[dict, bool], ...] | None:
    """Convert explicit return-value obligations to finite examples; unsupported is not empty."""
    if len(plan.holes) != 1:
        return None
    hole = plan.holes[0]
    cases = []
    for obligation in plan.obligations:
        if not obligation.entry_cases:
            return None
        for case in obligation.entry_cases:
            if case.interface.site != hole.site:
                return None
            cases.append((dict(case.inputs), case.expected))
    return tuple(cases) if cases else None


def boundary_edit_targets(boundary: RepairBoundary, scope: EditScope,
                          snapshot: RepositorySnapshot) -> tuple[str, ...]:
    """Map every visible site to enclosing syntax units or a text window, never by symbol name."""
    targets = []
    for site in boundary.sites:
        windows = tuple(r for r in scope.regions if r.path == site.path)
        if not windows:
            return ()
        _, start, end = source_slice(snapshot.root, site)
        region = next((r for r in windows if r.start_byte <= start < end <= r.end_byte), None)
        if region is None:
            return ()
        if region.edit_mode == 'text':
            targets.append(region.region_id)
            continue
        blocks = tuple(b for b in scope.blocks if b.region_id == region.region_id)
        enclosing = tuple(b for b in blocks if b.start_byte <= start and end <= b.end_byte)
        if enclosing:
            block = min(enclosing, key=lambda b: (b.end_byte - b.start_byte, b.block_id))
            targets.append(block.block_id)
        else:
            contained = tuple(b.block_id for b in blocks
                              if start <= b.start_byte < b.end_byte <= end)
            if not contained:
                return ()
            targets.extend(contained)
    return tuple(dict.fromkeys(targets))


def scoped_localization_plan(contracts: ContractSet, scope: EditScope, localization: LocalizationResult,
                             snapshot: RepositorySnapshot, context: RunContext) -> PatchPlan:
    """Select a localization-led generation focus without excluding broader freeform repairs."""
    from boundary_repair.algorithms.rendering import transaction_plan
    plan = transaction_plan(contracts, scope, context, 'scoped')
    guidance = []
    unresolved = list(plan.unresolved + localization.diagnostics)
    for assessment in localization.assessments:
        context.budget.check_deadline()
        if assessment.verdict != ExpressivityVerdict.UNKNOWN and not assessment.certificate:
            assessment = replace(assessment, verdict=ExpressivityVerdict.UNKNOWN,
                                 unresolved=assessment.unresolved + ('missing_interface_certificate',))
        targets = boundary_edit_targets(assessment.boundary, scope, snapshot)
        guidance.append(BoundaryGuidance(assessment, targets))
        unresolved.extend('boundary:' + assessment.boundary.boundary_id + ':' + reason
                          for reason in assessment.unresolved)
        if not targets:
            unresolved.append('unmapped_boundary:' + assessment.boundary.boundary_id)
    order = {ExpressivityVerdict.FEASIBLE: 0, ExpressivityVerdict.UNKNOWN: 1,
             ExpressivityVerdict.INEXPRESSIBLE: 2}
    guidance.sort(key=lambda g: order[g.assessment.verdict])
    primary = next((g for g in guidance if g.edit_targets
                    and g.assessment.verdict != ExpressivityVerdict.INEXPRESSIBLE), None)
    selected = (primary.assessment.boundary.boundary_id,) if primary else ()
    target_order = tuple(dict.fromkeys(target for g in guidance for target in g.edit_targets))
    priority = {target: rank for rank, target in enumerate(target_order)}
    blocks = tuple(sorted(scope.blocks, key=lambda b: priority.get(b.block_id, len(priority))))
    region_order = tuple(dict.fromkeys(
        next((b.region_id for b in scope.blocks if b.block_id == target), target) for target in target_order))
    region_priority = {target: rank for rank, target in enumerate(region_order)}
    regions = tuple(sorted(scope.regions, key=lambda r: region_priority.get(r.region_id, len(region_priority))))
    unresolved.append('freeform_edits_not_certified_by_local_interfaces')
    if primary is None:
        unresolved.append('no_mapped_admissible_interface_focus')
    return replace(plan, boundary_ids=selected, boundary_guidance=tuple(guidance),
                   edit_scope=replace(scope, blocks=blocks, regions=regions),
                   unresolved=tuple(dict.fromkeys(unresolved)))


def merged_ranges(ranges: tuple[tuple[str, int, int], ...]) -> tuple[tuple[str, int, int], ...]:
    """Union original-byte intervals without double-counting nested editing capabilities."""
    merged = []
    for path, start, end in sorted(set(ranges)):
        if merged and merged[-1][0] == path and start <= merged[-1][2]:
            old = merged.pop()
            merged.append((path, old[1], max(old[2], end)))
        else:
            merged.append((path, start, end))
    return tuple(merged)


def scope_ranges(scope: EditScope) -> tuple[tuple[str, int, int], ...]:
    """Measure all writable original bytes, including text windows and complete-file operations."""
    paths = {r.region_id: r.path for r in scope.regions}
    return merged_ranges(tuple((paths[b.region_id], b.start_byte, b.end_byte) for b in scope.blocks)
                         + tuple((r.path, r.start_byte, r.end_byte) for r in scope.regions if r.edit_mode == 'text')
                         + tuple((f.path, 0, f.size) for f in scope.files if f.complete))


def scope_identity(scope: EditScope) -> tuple:
    """Compare actual operation catalogs independently of presentation order."""
    return (tuple(sorted((b.block_id, b.region_id, b.start_byte, b.end_byte) for b in scope.blocks)),
            tuple(sorted((r.region_id, r.start_byte, r.end_byte) for r in scope.regions if r.edit_mode == 'text')),
            tuple(sorted(f.file_id for f in scope.files if f.complete)), tuple(sorted(scope.creation_roots)))


def contains_range(ranges: tuple[tuple[str, int, int], ...], site: tuple[str, int, int]) -> bool:
    """Check direct source intervention coverage, not semantic requirement satisfaction."""
    path, start, end = site
    return any(p == path and a <= start and end <= b for p, a, b in ranges)


def constraint_anchors(claims: tuple, scope: EditScope, snapshot: RepositorySnapshot) -> tuple[dict, tuple[str, ...]]:
    """Bind entry-case sites to visible original bytes; citations alone do not establish repair locations."""
    anchors, unknown = {}, []
    for claim in claims:
        sites = []
        incomplete = not claim.entry_cases
        for interface in dict.fromkeys(case.interface for case in claim.entry_cases):
            if interface.snapshot_sha256 != snapshot.tree_sha256:
                raise ValidationError('stale_observation_interface:' + interface.interface_id)
            site = interface.site
            windows = tuple(r for r in scope.regions if r.path == site.path)
            if not windows:
                incomplete = True
                continue
            _, start, end = source_slice(snapshot.root, site)
            if not any(r.start_byte <= start < end <= r.end_byte for r in windows):
                incomplete = True
                continue
            sites.append((site.path, start, end))
        anchors[claim.constraint_id] = tuple(dict.fromkeys(sites))
        if incomplete:
            unknown.append(claim.constraint_id)
    return anchors, tuple(unknown)


def restricted_scope(scope: EditScope, targets: tuple[str, ...]) -> EditScope:
    """Restrict every write channel while retaining full source windows for selected syntax blocks."""
    blocks = tuple(b for b in scope.blocks if b.block_id in targets)
    regions = tuple(r for r in scope.regions if r.region_id in {b.region_id for b in blocks}
                    or (r.edit_mode == 'text' and r.region_id in targets))
    paths = {r.path for r in regions}
    return replace(scope, files=tuple(replace(f, complete=False) for f in scope.files if f.path in paths),
                   regions=regions, blocks=blocks, creation_roots=())


def scoped_plans(contracts: ContractSet, scope: EditScope, localization: LocalizationResult,
                 snapshot: RepositorySnapshot, context: RunContext) -> tuple[PatchPlan, ...]:
    """Enumerate bounded freeform permission plans, retaining one conservative full-scope alternative."""
    base = scoped_localization_plan(contracts, scope, localization, snapshot, context)
    scope = base.edit_scope
    base = replace(base, read_scope=scope)
    plans, seen = [base], {scope_identity(scope)}
    must, _ = constraint_anchors(contracts.must, scope, snapshot)
    required = tuple(dict.fromkeys(site for sites in must.values() for site in sites))
    groups = list(dict.fromkeys(g.edit_targets for g in base.boundary_guidance if g.edit_targets))
    footprints = {targets: scope_ranges(restricted_scope(scope, targets)) for targets in groups}

    def joint_targets(seed: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Cover bound requirements using whole boundary groups, retaining their companion positions."""
        targets = seed
        while True:
            context.budget.check_deadline()
            ranges = scope_ranges(restricted_scope(scope, targets))
            missing = tuple(site for site in required if not contains_range(ranges, site))
            options = [(sum(contains_range(footprints[group], site) for site in missing),
                        -sum(end - start for _, start, end in footprints[group]), -rank, group)
                       for rank, group in enumerate(groups)]
            if not missing or not options or max(options)[0] == 0:
                return targets
            targets = tuple(dict.fromkeys(targets + max(options)[3]))

    joint = joint_targets()
    candidates = ([joint] if joint else []) + groups
    candidates.extend(joint_targets(group) for group in groups)
    candidates = list(dict.fromkeys(candidates))

    def add(targets: tuple[str, ...]) -> None:
        """Count each distinct permission plan once under the shared freeform grammar idea."""
        context.budget.check_deadline()
        candidate = restricted_scope(scope, targets)
        identity = scope_identity(candidate)
        if not candidate.regions or identity in seen:
            return
        seen.add(identity)
        context.budget.claim_candidates()
        key = hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()[:16]
        guidance = tuple(replace(g, edit_targets=boundary_edit_targets(g.assessment.boundary, candidate, snapshot))
                         for g in base.boundary_guidance)
        primary = next((g for g in guidance if g.edit_targets
                        and g.assessment.verdict != ExpressivityVerdict.INEXPRESSIBLE), None)
        plans.append(replace(base, plan_id='scoped:scope:' + key, edit_scope=candidate,
                             boundary_ids=(primary.assessment.boundary.boundary_id,) if primary else (),
                             boundary_guidance=guidance))

    for targets in candidates:
        if context.budget.patch_candidates >= context.budget.limits.max_patch_candidates:
            break
        add(targets)
    blocks = {b.block_id: b for b in scope.blocks}
    for targets in candidates:
        expanded = targets
        while context.budget.patch_candidates < context.budget.limits.max_patch_candidates:
            larger = []
            for target in expanded:
                block = blocks.get(target)
                ancestors = tuple(b for b in scope.blocks if block is not None and b.region_id == block.region_id
                                  and b.start_byte <= block.start_byte and block.end_byte <= b.end_byte
                                  and (b.start_byte, b.end_byte) != (block.start_byte, block.end_byte))
                larger.append(min(ancestors, key=lambda b: (b.end_byte - b.start_byte, b.block_id)).block_id
                              if ancestors else target)
            next_targets = tuple(dict.fromkeys(larger))
            if next_targets == expanded:
                break
            add(next_targets)
            expanded = next_targets
    exhausted = context.budget.patch_candidates >= context.budget.limits.max_patch_candidates
    return tuple(replace(p, unresolved=tuple(dict.fromkeys(p.unresolved + (
        'source_intervention_coverage_not_semantic_satisfaction',
        'transitive_effects_unknown', 'candidate_pool_minimum_not_global_minimum',
    ) + (('scope_candidate_budget_reached',) if exhausted else ())))) for p in plans)


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
        from boundary_repair.algorithms.rendering import TransactionRenderer
        from boundary_repair.domain.repair import EditRegion, EditTransaction, SourceEdit
        import hashlib
        scope = self.program.source_scope(snapshot, context, task.problem_statement)
        projected = tuple(a for a in localization.assessments if a.verdict == ExpressivityVerdict.FEASIBLE
                          and a.proof_scope == 'finite_source_projection' and a.certificate
                          and a.proof is not None and a.construction is not None and len(a.boundary.sites) == 1)
        hard = contracts.must + contracts.frames
        if (projected and contracts.must and contracts.extraction_status != 'unavailable'
                and all(c.entry_cases and all(case.interface.kind == 'local_projection' for case in c.entry_cases)
                        for c in hard)):
            plans = self.projection_plans(contracts, scope, localization, projected, snapshot, context)
            plan = self.select_projection_scope(plans, contracts, context)
            self.program.freeze_plan(plan, context)
            if plan.generation_mode == 'certified_projection':
                transaction = EditTransaction(tuple(SourceEdit('replace_region', f.hole_id, f.source_text)
                                                     for f in plan.fixed_fillings))
            else:
                transaction = TransactionRenderer(self.model, self.response_tokens).render(task, plan, context)
            patch = self.program.compile(task, plan, transaction, snapshot, context)
            return SynthesisResult(plan, patch, plan.unresolved)
        feasible = LocalizationResult(tuple(a for a in localization.assessments
                                           if a.verdict == ExpressivityVerdict.FEASIBLE))
        plans = self.enumerate_plans(contracts, feasible, snapshot, context, task.problem_statement, allow_empty=True) if feasible.assessments else ()
        supported = tuple(p for p in plans if all(h.expected_type == 'boolean-expression' for h in p.holes)
                          and len(p.holes) == 1 and len(p.holes[0].allowed_symbols) <= 2
                          and boolean_cases(p) is not None and contracts.extraction_status != 'unavailable')
        if supported:
            plan = self.select_minimal_scope(supported, contracts, snapshot, context)
            files = {f.path: f for f in scope.files}
            regions = []
            for hole in plan.holes:
                data, start, end = source_slice(snapshot.root, hole.site)
                regions.append(EditRegion(hole.hole_id, files[hole.site.path].file_id, hole.site.path,
                                          start, end, hole.site.start_line, data[start:end].decode('utf-8'),
                                          hashlib.sha256(data[start:end]).hexdigest(), len(data[:start].decode('utf-8'))))
            strong_scope = EditScope(tuple(replace(files[path], complete=False) for path in sorted({r.path for r in regions})),
                                     tuple(regions), (), ('certified_boolean_grammar',))
            plan = replace(plan, edit_scope=strong_scope)
            self.program.freeze_plan(plan, context)
            fillings = self.fill_holes(task, plan, snapshot, context)
            self.program.materialize(task, plan, fillings, snapshot, context)
            transaction = EditTransaction(tuple(SourceEdit('replace_region', f.hole_id, f.source_text) for f in fillings))
        else:
            plans = scoped_plans(contracts, scope, localization, snapshot, context)
            plan = self.select_minimal_scope(plans, contracts, snapshot, context)
            self.program.freeze_plan(plan, context)
            transaction = TransactionRenderer(self.model, self.response_tokens).render(task, plan, context)
        patch = self.program.compile(task, plan, transaction, snapshot, context)
        return SynthesisResult(plan, patch, plan.unresolved)

    def projection_plans(self, contracts: ContractSet, scope: EditScope, localization: LocalizationResult,
                         assessments: tuple, snapshot: RepositorySnapshot, context: RunContext) -> tuple[PatchPlan, ...]:
        """Compose disjoint finite constructions with companion edits under the shared candidate budget."""
        from boundary_repair.domain.repair import EditRegion
        base = scoped_localization_plan(contracts, scope, localization, snapshot, context)
        base = replace(base, read_scope=scope)
        required = {f'{claim.constraint_id}:{number}' for claim in contracts.must + contracts.frames
                    for number in range(len(claim.entry_cases))}
        options, identities = [], set()
        for assessment in assessments:
            site = assessment.boundary.sites[0]
            identity = (site, assessment.construction)
            if identity not in identities:
                identities.add(identity)
                options.append(assessment)

        def disjoint(left: object, right: object) -> bool:
            """Reject compositions whose exact source interventions overlap."""
            a, b = left.boundary.sites[0], right.boundary.sites[0]
            return a.path != b.path or a.end_byte <= b.start_byte or b.end_byte <= a.start_byte

        plans, seen, kinds = [base], set(), set()
        files = {file.path: file for file in scope.files}
        for seed in options:
            if context.budget.patch_candidates >= context.budget.limits.max_patch_candidates:
                break
            chosen, covered = [seed], set(seed.covered_obligations)
            while not required <= covered:
                context.budget.check_deadline()
                candidates = [(len(set(a.covered_obligations) - covered), -a.boundary.sites[0].node_count, -rank, a)
                              for rank, a in enumerate(options) if all(disjoint(a, b) for b in chosen)]
                if not candidates or max(candidates, key=lambda row: row[:3])[0] == 0:
                    break
                additional = max(candidates, key=lambda row: row[:3])[3]
                chosen.append(additional)
                covered.update(additional.covered_obligations)
            identity = tuple(sorted((a.boundary.sites[0].path, a.boundary.sites[0].start_byte,
                                     a.boundary.sites[0].end_byte, a.construction) for a in chosen))
            if identity in seen:
                continue
            seen.add(identity)
            new_kinds = {a.boundary.edit_kind for a in chosen} - kinds
            if context.budget.ideas + len(new_kinds) > context.budget.limits.max_ideas:
                continue
            context.budget.claim_ideas(len(new_kinds))
            kinds.update(new_kinds)
            context.budget.claim_candidates()
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
            holes, fillings, regions = [], [], []
            for number, assessment in enumerate(chosen):
                site = assessment.boundary.sites[0]
                hole_id = f'projection:{key}:{number}'
                data, start, end = source_slice(snapshot.root, site)
                holes.append(SyntaxHole(hole_id, site, 'finite-expression',
                                        tuple(f.feature_id for f in assessment.required_features)))
                fillings.append(HoleFilling(hole_id, assessment.construction))
                regions.append(EditRegion(hole_id, files[site.path].file_id, site.path, start, end, site.start_line,
                                          data[start:end].decode('utf-8'), hashlib.sha256(data[start:end]).hexdigest(),
                                          len(data[:start].decode('utf-8'))))
            strong = EditScope(tuple(replace(files[path], complete=False) for path in sorted({r.path for r in regions})),
                               tuple(regions), (), ('finite_projection_constructions_only',))
            plan = replace(base, plan_id='projection:' + key, boundary_ids=tuple(a.boundary.boundary_id for a in chosen),
                           edit_kind=chosen[0].boundary.edit_kind, holes=tuple(holes), edit_scope=strong,
                           generation_mode='certified_projection', fixed_fillings=tuple(fillings),
                           unresolved=('finite_entry_domain_only', 'entry_case_evidence_interpretation_not_verified',
                                       'JSX_construction_not_browser_visibility', 'candidate_pool_minimum_not_global_minimum'))
            checked = self.program.assess_projection_plan(plan, snapshot, context)
            plans.append(replace(plan, semantic_check=checked, effects=checked.effects))
        return tuple(plans)

    def select_projection_scope(self, plans: tuple[PatchPlan, ...], contracts: ContractSet,
                                context: RunContext) -> PatchPlan:
        """Rank joint-checked finite plans by semantic obligations before structural size."""
        ranked, comparisons = [], []
        for rank, plan in enumerate(plans):
            context.budget.check_deadline()
            checked = plan.semantic_check
            covered = set(checked.covered) if checked else set()
            must = tuple(c.constraint_id for c in contracts.must if c.constraint_id not in covered)
            frames = tuple(c.constraint_id for c in contracts.frames if c.constraint_id not in covered)
            extra = len({e.target.entity_id + ':' + e.target.property_name for e in checked.effects
                         if e.relation.value == 'effect:possible_extra_property'}) if checked else 1
            semantic = SemanticScopeCost(len(must), len(frames), len(frames), extra, 0,
                                         checked.ast_nodes if checked else 0)
            ranges = scope_ranges(plan.edit_scope)
            size = sum(end - start for _, start, end in ranges)
            legacy = ScopeCost(len(must), 0, len(must), 0, len(frames), int(checked is None),
                               sum(f.complete for f in plan.edit_scope.files) + len(plan.edit_scope.creation_roots), 0, size)
            targets = tuple(h.hole_id for h in plan.holes) or tuple(b.block_id for b in plan.edit_scope.blocks)
            comparisons.append(PlanAssessment(plan.plan_id, plan.boundary_ids, targets, ranges, legacy,
                                              must, (), frames, (), rank, semantic))
            if checked is None or (not checked.unresolved and not checked.violated and checked.baseline_mismatches
                                   and not must and not frames):
                ranked.append((semantic, rank, plan.plan_id, replace(plan, cost=legacy, semantic_cost=semantic)))
        winner = min(ranked, key=lambda row: row[:3])[3]
        comparisons.sort(key=lambda row: (row.semantic_cost, row.localization_rank, row.plan_id))
        return replace(winner, scope_comparison=tuple(comparisons),
                       selection_policy='semantic-scope-v1:U_req,U_frame,R_protected,S_extra,N_invented,N_AST')

    def enumerate_plans(self, contracts: ContractSet, localization: LocalizationResult,
                        snapshot: RepositorySnapshot, context: RunContext, query: str = '',
                        allow_empty: bool = False) -> tuple[PatchPlan, ...]:
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
            declared = replace(boundary, readable_features=assessment.required_features)
            holes = syntax_holes(declared, self.program.index(snapshot, context), snapshot, query,
                                 boundary.boundary_id, assessment.verdict == ExpressivityVerdict.FEASIBLE)
            if not holes:
                continue
            unresolved = tuple(c.constraint_id for c in contracts.must + contracts.frames)
            if assessment.verdict == ExpressivityVerdict.UNKNOWN:
                unresolved += ('local_semantics_unknown',)
            plan = PatchPlan('plan:' + boundary.boundary_id, (boundary.boundary_id,), boundary.edit_kind,
                             holes, contracts.must + contracts.frames, (), None, unresolved, contracts.may,
                             interpretation_groups=contracts.interpretation_groups, evidence_sources=contracts.sources)
            if allow_empty and not (assessment.certificate and len(holes) == 1
                                    and holes[0].expected_type == 'boolean-expression'
                                    and len(holes[0].allowed_symbols) <= 2 and boolean_cases(plan) is not None
                                    and contracts.extraction_status != 'unavailable'):
                continue
            if boundary.edit_kind not in kinds:
                if context.budget.ideas >= context.budget.limits.max_ideas:
                    continue
                context.budget.claim_ideas()
                kinds.add(boundary.edit_kind)
            context.budget.claim_candidates()
            plans.append(plan)
        if not plans and not allow_empty:
            raise NoAdmissiblePatch('no_plan_within_declared_interfaces')
        return tuple(plans)

    def select_minimal_scope(self, plans: tuple[PatchPlan, ...], contracts: ContractSet,
                             snapshot: RepositorySnapshot, context: RunContext) -> PatchPlan:
        """G2: minimize exposed permissions subject to intervention coverage, retaining semantic uncertainty."""
        if not plans:
            raise NoAdmissiblePatch('empty_plan_pool')
        scope = next((p.read_scope for p in plans if p.read_scope is not None), None)
        if scope is None:
            scope = self.program.source_scope(snapshot, context)
        must, unknown_must = constraint_anchors(contracts.must, scope, snapshot)
        frames, unknown_frames = constraint_anchors(contracts.frames, scope, snapshot)
        required_ranges = merged_ranges(tuple(site for sites in must.values() for site in sites))
        ranked, comparisons = [], []
        for plan in plans:
            context.budget.check_deadline()
            effects = self.program.effects(plan, snapshot, context)
            unknown_effects = int(not effects or any(e.coverage == Coverage.PARTIAL for e in effects))
            if plan.edit_scope is not None:
                ranges = scope_ranges(plan.edit_scope)
                restricted = scope_identity(plan.edit_scope) != scope_identity(scope)
                broad = sum(f.complete for f in plan.edit_scope.files) + len(plan.edit_scope.creation_roots)
                targets = tuple(b.block_id for b in plan.edit_scope.blocks) + tuple(
                    r.region_id for r in plan.edit_scope.regions if r.edit_mode == 'text')
            else:
                ranges = merged_ranges(tuple((h.site.path, *source_slice(snapshot.root, h.site)[1:]) for h in plan.holes))
                restricted, broad = True, 0
                targets = tuple(h.hole_id for h in plan.holes)
            uncovered = tuple(key for key, sites in must.items() if any(not contains_range(ranges, site) for site in sites))
            touched = tuple(key for key, sites in frames.items() if any(
                p == path and start < b and a < end for path, start, end in sites for p, a, b in ranges))
            size = sum(end - start for _, start, end in ranges)
            anchored = sum(max(0, min(end, b) - max(start, a))
                           for path, start, end in ranges for p, a, b in required_ranges if path == p)
            restricted_unknown = (len(unknown_must) + int(not contracts.must)) * int(restricted)
            cost = ScopeCost(len(uncovered), restricted_unknown, len(unknown_must), len(touched),
                             len(unknown_frames), unknown_effects, broad, size - anchored, size)
            guidance_order = {g.assessment.boundary.boundary_id: rank
                              for rank, g in enumerate(plan.boundary_guidance or ())}
            localization_rank = min((guidance_order.get(key, len(guidance_order)) for key in plan.boundary_ids),
                                    default=len(guidance_order))
            unresolved = tuple(dict.fromkeys(plan.unresolved
                + tuple('uncovered_requirement_anchor:' + key for key in uncovered)
                + tuple('unmapped_requirement:' + key for key in unknown_must)
                + tuple('unmapped_frame:' + key for key in unknown_frames)
                + (('partial_effect_summary',) if unknown_effects else ())))
            comparisons.append(PlanAssessment(plan.plan_id, plan.boundary_ids, targets, ranges, cost,
                                              uncovered, unknown_must, touched, unknown_frames, localization_rank))
            ranked.append((cost, localization_rank, plan.plan_id, replace(plan, effects=effects, cost=cost,
                                                                         unresolved=unresolved)))
        winner = min(ranked, key=lambda row: row[:3])[3]
        comparisons.sort(key=lambda row: (row.cost, row.localization_rank, row.plan_id))
        return replace(winner, scope_comparison=tuple(comparisons),
                       selection_policy='intervention-risk-v1:lexicographic_cost_then_localization_then_plan_id')

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
            if plan.edit_scope is not None and plan.generation_mode == 'certified':
                raise NoAdmissiblePatch('certified_boolean_generation_failed')
        from boundary_repair.algorithms.rendering import HoleRenderer
        return HoleRenderer(self.model, self.response_tokens, self.max_context_chars).render(task, plan, snapshot, context)
