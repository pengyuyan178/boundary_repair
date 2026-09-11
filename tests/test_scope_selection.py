"""Pre-generation scope decisions on synthetic sources, using real parser and permission compiler."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, task
from test_scoped_alignment import FocusModel
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.adapters.repository import transaction_contents, validate_scope
from boundary_repair.algorithms.rendering import TransactionRenderer, edit_transaction_schema
from boundary_repair.algorithms.synthesis import (
    ScopeSynthesis, boundary_edit_targets, constraint_anchors, merged_ranges,
    restricted_scope, scope_identity, scope_ranges, scoped_plans,
)
from boundary_repair.domain.errors import BudgetExceeded, ValidationError
from boundary_repair.domain.repair import (
    BoundaryAssessment, EditKind, EditTransaction, ExpressivityVerdict, LocalizationResult,
    RepairBoundary, SourceEdit,
)
from boundary_repair.domain.runtime import BudgetLedger, BudgetLimits
from boundary_repair.domain.specification import (
    BehaviorConstraint, ClaimKind, ContractSet, Coverage, EntryCase, InterpretationSpace, SolverStatus,
)
from boundary_repair.kernel.files import tree_digest
from boundary_repair.kernel.terms import literal


@unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
class ScopeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = snapshot(self.root)
        (self.snap.root / 'small.js').write_text('function guard(x) { return x; }\n', encoding='utf-8')
        (self.snap.root / 'large.js').write_text(
            'function companion(longParameterName) { return !longParameterName; }\n', encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.ctx, self.task = context(), task()
        self.program = ProgramAdapter(config(self.root))
        self.logic = LogicAdapter()
        self.scope = self.program.source_scope(self.snap, self.ctx, self.task.problem_statement)
        self.interfaces = {i.site.path: i for i in self.program.observation_interfaces(self.snap, self.ctx)}
        self.must = self.claim('need', 'ui.js')
        self.contracts = ContractSet((self.must,), (), (), (),
                                    InterpretationSpace((), (), SolverStatus.SAT, Coverage.COMPLETE))
        self.boundary = self.boundary_for('ui.js')
        self.loc = LocalizationResult((BoundaryAssessment(self.boundary, ExpressivityVerdict.UNKNOWN,
                                                         (), None, ('synthetic_unproved',)),))
        self.model = FocusModel()
        self.synthesis = ScopeSynthesis(self.model, self.program, self.logic)

    def claim(self, name, path, frame=False):
        interface = self.interfaces[path]
        case = EntryCase(interface, tuple((p, False) for p in interface.parameters), False)
        return BehaviorConstraint(name, ClaimKind.FRAME if frame else ClaimKind.REQUIREMENT,
                                  (case.target,), case.relation, ('synthetic_issue',),
                                  'Explicit synthetic direct-entry case.', (case,))

    def boundary_for(self, path):
        return RepairBoundary('boundary:' + path, (self.interfaces[path].site,), EditKind.REFINE_GUARD, (), ())

    def targets(self, *paths):
        return tuple(t for path in paths for t in boundary_edit_targets(self.boundary_for(path), self.scope, self.snap))

    def pool(self, contracts=None, loc=None, ctx=None):
        return scoped_plans(contracts or self.contracts, self.scope, loc or self.loc, self.snap, ctx or context())

    def select(self, plans, contracts=None):
        return self.synthesis.select_minimal_scope(plans, contracts or self.contracts, self.snap, self.ctx)

    def multi_localization(self, *groups):
        assessments = []
        for index, paths in enumerate(groups):
            boundary = replace(self.boundary, boundary_id=f'multi:{index}',
                               sites=tuple(self.interfaces[path].site for path in paths))
            assessments.append(replace(self.loc.assessments[0], boundary=boundary))
        return LocalizationResult(tuple(assessments))

    def companions(self):
        base = self.pool()[0]
        return (replace(base, plan_id='with-small', edit_scope=restricted_scope(self.scope, self.targets('ui.js', 'small.js'))),
                replace(base, plan_id='with-large', edit_scope=restricted_scope(self.scope, self.targets('ui.js', 'large.js'))))

    def test_enumerates_distinct_permissions_and_counts_every_plan(self):
        ctx = context()
        plans = self.pool(ctx=ctx)
        self.assertGreaterEqual(len(plans), 3)
        self.assertEqual(len({scope_identity(p.edit_scope) for p in plans}), len(plans))
        self.assertEqual(ctx.budget.patch_candidates, len(plans))
        self.assertEqual(ctx.budget.ideas, 1)
        self.assertEqual(ctx.budget.model_calls, 0)
        for plan in plans:
            validate_scope(self.snap, plan.edit_scope)
            self.assertEqual(plan.read_scope, plans[0].read_scope)
            self.assertEqual(plan.obligations, self.contracts.must)

    def test_cost_selects_nonfirst_candidate_and_not_input_order(self):
        plans = self.pool()
        selected = self.select(plans)
        self.assertNotEqual(selected.plan_id, plans[0].plan_id)
        self.assertEqual(self.select(tuple(reversed(plans))).plan_id, selected.plan_id)
        self.assertEqual(selected.cost.uncovered_requirements, 0)
        self.assertEqual(selected.cost.unknown_effects, 1)
        self.assertEqual(len(selected.scope_comparison), len(plans))
        self.assertEqual(selected.scope_comparison[0].plan_id, selected.plan_id)
        self.assertIn('need', selected.unresolved)
        self.assertIn('transitive_effects_unknown', selected.unresolved)

    def test_shorter_scope_loses_if_it_misses_a_required_site(self):
        other = self.claim('other-need', 'large.js')
        contracts = replace(self.contracts, must=(self.must, other))
        plans = self.pool(contracts, loc=self.multi_localization(('ui.js',), ('large.js',)))
        chosen = self.select(plans, contracts)
        self.assertEqual({r.path for r in chosen.edit_scope.regions}, {'ui.js', 'large.js'})
        incomplete = [a for a in chosen.scope_comparison if a.uncovered_requirements]
        self.assertTrue(incomplete)
        self.assertTrue(any(a.cost.edit_bytes < chosen.cost.edit_bytes for a in incomplete))
        self.assertEqual(chosen.cost.uncovered_requirements, 0)

    def test_all_sites_in_one_claim_are_covered_by_joint_candidate(self):
        combined = replace(self.must, entry_cases=self.must.entry_cases + self.claim('another', 'large.js').entry_cases)
        contracts = replace(self.contracts, must=(combined,))
        plans = self.pool(contracts, loc=self.multi_localization(('ui.js',), ('large.js',)))
        chosen = self.select(plans, contracts)
        self.assertEqual(chosen.cost.uncovered_requirements, 0)
        self.assertEqual({r.path for r in chosen.edit_scope.regions}, {'ui.js', 'large.js'})
        self.assertEqual(chosen.obligations[0].entry_cases, combined.entry_cases)

    def test_frame_risk_beats_shorter_edit_range_and_changes_selection(self):
        plans = self.companions()
        no_frame = self.select(plans)
        self.assertEqual(no_frame.plan_id, 'with-small')
        protect_small = replace(self.contracts, frames=(self.claim('keep-small', 'small.js', True),))
        protect_large = replace(self.contracts, frames=(self.claim('keep-large', 'large.js', True),))
        with_small_frame = self.select(plans, protect_small)
        with_large_frame = self.select(tuple(reversed(plans)), protect_large)
        self.assertEqual(with_small_frame.plan_id, 'with-large')
        self.assertEqual(with_large_frame.plan_id, 'with-small')
        self.assertGreater(with_small_frame.cost.edit_bytes, no_frame.cost.edit_bytes)
        loser = next(r for r in with_small_frame.scope_comparison if r.plan_id == 'with-small')
        self.assertEqual(loser.touched_frames, ('keep-small',))
        self.assertGreater(loser.cost.touched_frames, with_small_frame.cost.touched_frames)
        self.assertTrue(any(e.coverage == Coverage.PARTIAL for e in with_small_frame.effects))

    def test_real_enumerator_preserves_companions_and_frame_risk_changes_winner(self):
        loc = self.multi_localization(('ui.js', 'small.js'), ('ui.js', 'large.js'))
        plans = self.pool(loc=loc)
        for plan in plans:
            self.assertGreaterEqual(len({r.path for r in plan.edit_scope.regions}), 2)
        no_frame = self.select(plans)
        self.assertEqual({r.path for r in no_frame.edit_scope.regions}, {'ui.js', 'small.js'})
        contracts = replace(self.contracts, frames=(self.claim('keep-small', 'small.js', True),))
        protected = self.select(self.pool(contracts, loc=loc), contracts)
        self.assertEqual({r.path for r in protected.edit_scope.regions}, {'ui.js', 'large.js'})
        self.assertGreater(protected.cost.edit_bytes, no_frame.cost.edit_bytes)
        self.assertNotEqual(protected.plan_id, no_frame.plan_id)
        self.assertEqual(protected.cost.touched_frames, 0)
        self.assertTrue(any(row.touched_frames for row in protected.scope_comparison))
        self.assertEqual(protected.obligations, contracts.must + contracts.frames)
        result = self.synthesis.synthesize(self.task, contracts, loc, self.snap, self.ctx)
        self.assertEqual(result.plan.plan_id, protected.plan_id)
        self.assertEqual(result.patch.application_check, 'passed')
        self.assertEqual(len(self.model.requests), 1)
        payload = json.loads(self.model.requests[0].prompt)
        self.assertEqual({f['path'] for f in payload['files']}, {'ui.js', 'large.js'})
        self.assertEqual(payload['obligations'][-1]['constraint_id'], 'keep-small')

    def test_no_mapping_does_not_invent_an_unlocalized_narrow_plan(self):
        chosen = self.select(self.pool(loc=LocalizationResult(())))
        self.assertEqual(len(chosen.scope_comparison), 1)
        self.assertEqual(scope_identity(chosen.edit_scope), scope_identity(self.scope))

    def test_unknown_effect_summary_is_not_zero_risk_and_can_change_choice(self):
        plans = self.companions()
        effects = {p.plan_id: self.program.effects(p, self.snap, self.ctx) for p in plans}
        effects['with-large'] = tuple(replace(e, coverage=Coverage.COMPLETE) for e in effects['with-large'])
        with patch.object(ProgramAdapter, 'effects', side_effect=lambda p, *_: effects[p.plan_id]):
            chosen = self.select(plans)
        self.assertEqual(chosen.plan_id, 'with-large')
        unknown = next(r for r in chosen.scope_comparison if r.plan_id == 'with-small')
        self.assertEqual(unknown.cost.unknown_effects, 1)
        with patch.object(ProgramAdapter, 'effects', return_value=()):
            self.assertEqual(self.select(plans).cost.unknown_effects, 1)

    def test_unbound_requirement_retains_broad_scope_without_erasing_obligations(self):
        extra = replace(self.must, constraint_id='visual-unbound', entry_cases=())
        contracts = replace(self.contracts, must=(self.must, extra), may=(replace(extra, constraint_id='may'),),
                            frames=(replace(extra, constraint_id='frame', kind=ClaimKind.FRAME),))
        chosen = self.select(self.pool(contracts), contracts)
        self.assertEqual(chosen.plan_id, 'scoped:transaction')
        self.assertEqual(scope_identity(chosen.edit_scope), scope_identity(self.scope))
        self.assertEqual(chosen.cost.unmapped_requirements, 1)
        self.assertEqual(chosen.cost.unmapped_frames, 1)
        self.assertEqual(chosen.obligations, contracts.must + contracts.frames)
        self.assertEqual(chosen.soft_obligations, contracts.may)
        self.assertIn('unmapped_requirement:visual-unbound', chosen.unresolved)
        self.assertTrue(all(r.cost.restricted_unknown_requirements > 0 for r in chosen.scope_comparison[1:]))

    def test_no_hard_requirements_does_not_invent_a_narrow_target(self):
        contracts = replace(self.contracts, must=(), may=(self.must,))
        chosen = self.select(self.pool(contracts), contracts)
        self.assertEqual(chosen.plan_id, 'scoped:transaction')
        self.assertFalse(chosen.cost.uncovered_requirements)
        self.assertTrue(all(r.cost.restricted_unknown_requirements for r in chosen.scope_comparison[1:]))

    def test_soft_requirements_do_not_expand_the_write_scope(self):
        contracts = replace(self.contracts, may=(self.claim('soft', 'large.js'),))
        chosen = self.select(self.pool(contracts), contracts)
        self.assertEqual({r.path for r in chosen.edit_scope.regions}, {'ui.js'})
        self.assertEqual(chosen.soft_obligations, contracts.may)

    def test_selected_permissions_and_relevant_read_context_reach_real_request(self):
        chosen = self.select(self.pool())
        transaction = TransactionRenderer(self.model).render(self.task, chosen, self.ctx)
        request = self.model.requests[0]
        payload = json.loads(request.prompt)
        self.assertEqual({r['path'] for r in payload['regions']}, {'ui.js'})
        self.assertTrue(payload['context_manifest']['omitted_exploration_region_ids'])
        readonly = [r for r in payload['regions'] if r['path'] != 'ui.js']
        self.assertTrue(all(r['edit_mode'] == 'read_only' for r in readonly))
        self.assertTrue(all(not f['complete'] for f in payload['read_only_files']))
        self.assertEqual({b['block_id'] for b in payload['blocks']}, {b.block_id for b in chosen.edit_scope.blocks})
        self.assertEqual(payload['scope_selection']['plan_id'], chosen.plan_id)
        self.assertEqual(request.output_schema, edit_transaction_schema(chosen.edit_scope))
        self.assertNotEqual(request.output_schema, edit_transaction_schema(self.scope))
        artifact = self.program.compile(self.task, chosen, transaction, self.snap, self.ctx)
        self.assertEqual(artifact.application_check, 'passed')
        self.assertIn('active && !hidden', artifact.unified_diff)
        self.assertEqual(len(self.model.requests), 1)

    def test_changed_risk_changes_actual_generation_schema(self):
        plans = self.companions()
        schemas = []
        for path in ('small.js', 'large.js'):
            contracts = replace(self.contracts, frames=(self.claim('keep', path, True),))
            chosen = self.select(plans, contracts)
            model = FocusModel()
            TransactionRenderer(model).render(self.task, chosen, context())
            schemas.append(model.requests[0].output_schema)
        self.assertNotEqual(*schemas)

    def test_compiler_rejects_all_wider_write_channels(self):
        chosen = self.select(self.pool())
        region = chosen.edit_scope.regions[0]
        outside = self.targets('large.js')[0]
        edits = (SourceEdit('replace_block', outside, 'return false;'),
                 SourceEdit('replace_text', region.region_id, 'changed', old_text=region.source),
                 SourceEdit('replace_region', region.region_id, 'changed'),
                 SourceEdit('replace_lines', region.region_id, 'changed', region.start_line, region.start_line),
                 SourceEdit('insert_at', region.region_id, 'changed', region.start_line),
                 SourceEdit('delete_file', chosen.edit_scope.files[0].file_id),
                 SourceEdit('rename_file', chosen.edit_scope.files[0].file_id, destination='renamed.js'),
                 SourceEdit('create_file', 'new.js', 'const value = true;'))
        before = tree_digest(self.snap.root)
        for edit in edits:
            with self.subTest(operation=edit.operation), self.assertRaises(ValidationError):
                transaction_contents(self.snap, chosen.edit_scope, EditTransaction((edit,)))
        self.assertEqual(tree_digest(self.snap.root), before)

    def test_valid_edit_and_outside_edit_are_rejected_atomically(self):
        chosen = self.select(self.pool())
        valid = TransactionRenderer(self.model).render(self.task, chosen, self.ctx)
        invalid = SourceEdit('replace_block', self.targets('large.js')[0], 'return false;')
        with self.assertRaisesRegex(ValidationError, 'unknown_edit_block'):
            transaction_contents(self.snap, chosen.edit_scope, replace(valid, edits=valid.edits + (invalid,)))
        self.assertEqual(tree_digest(self.snap.root), self.snap.tree_sha256)

    def test_negative_interface_still_has_a_broader_freeform_candidate(self):
        negative = replace(self.loc.assessments[0], verdict=ExpressivityVerdict.INEXPRESSIBLE,
                           certificate='synthetic-limited-certificate')
        chosen = self.select(self.pool(loc=LocalizationResult((negative,))))
        self.assertTrue(boundary_edit_targets(self.boundary, chosen.edit_scope, self.snap))
        self.assertFalse(chosen.boundary_ids)
        self.assertIn('freeform_edits_not_certified_by_local_interfaces', chosen.unresolved)

    def test_budget_one_selects_only_full_candidate_without_retry(self):
        ctx = replace(context(), budget=BudgetLedger(BudgetLimits(max_patch_candidates=1)))
        plans = self.pool(ctx=ctx)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].plan_id, 'scoped:transaction')
        self.assertIn('scope_candidate_budget_reached', plans[0].unresolved)
        self.assertEqual(ctx.budget.patch_candidates, 1)
        self.assertEqual(ctx.budget.model_calls, 0)

    def test_budget_two_reserves_joint_requirement_scope_before_local_candidates(self):
        contracts = replace(self.contracts, must=(self.must, self.claim('other', 'large.js')))
        ctx = replace(context(), budget=BudgetLedger(BudgetLimits(max_patch_candidates=2)))
        plans = self.pool(contracts, loc=self.multi_localization(('ui.js',), ('large.js',)), ctx=ctx)
        chosen = self.select(plans, contracts)
        self.assertEqual(chosen.cost.uncovered_requirements, 0)
        self.assertEqual({r.path for r in chosen.edit_scope.regions}, {'ui.js', 'large.js'})
        self.assertEqual(ctx.budget.patch_candidates, 2)

    def test_exhausted_budget_fails_before_model(self):
        ctx = replace(context(), budget=BudgetLedger(BudgetLimits(max_patch_candidates=0)))
        with self.assertRaises(BudgetExceeded):
            self.synthesis.synthesize(self.task, self.contracts, self.loc, self.snap, ctx)
        self.assertFalse(self.model.requests)

    def test_comparison_is_frozen_before_the_only_model_call(self):
        def before(payload, ctx):
            path = config(self.root).results_root / ctx.run_id / 'cases' / ctx.instance_id / 'trajectory/generation_plan.json'
            stored = json.loads(path.read_text(encoding='utf-8'))
            self.assertGreater(len(stored['scope_comparison']), 1)
            self.assertEqual(stored['scope_comparison'][0]['plan_id'], stored['plan_id'])
            self.assertEqual(stored['cost'], payload['scope_selection']['cost'])
            self.assertEqual(stored['selection_policy'], payload['scope_selection']['policy'])
        model = FocusModel(before=before)
        result = ScopeSynthesis(model, self.program, self.logic).synthesize(
            self.task, self.contracts, self.loc, self.snap, self.ctx)
        self.assertEqual(result.patch.application_check, 'passed')
        self.assertEqual(self.ctx.budget.model_calls, 1)
        self.assertEqual(self.ctx.budget.patch_candidates, len(result.plan.scope_comparison))

    def test_stale_interface_is_rejected_even_when_boundary_is_unknown(self):
        case = self.must.entry_cases[0]
        stale = replace(case, interface=replace(case.interface, snapshot_sha256='0' * 64))
        contracts = replace(self.contracts, must=(replace(self.must, entry_cases=(stale,)),))
        with self.assertRaisesRegex(ValidationError, 'stale_observation_interface'):
            self.synthesis.synthesize(self.task, contracts, self.loc, self.snap, self.ctx)
        self.assertFalse(self.model.requests)

    def test_unretrieved_anchor_is_unknown_without_reading_its_file(self):
        case = self.must.entry_cases[0]
        interface = replace(case.interface, site=replace(case.interface.site, path='not-retrieved.js'))
        claim = replace(self.must, entry_cases=(replace(case, interface=interface),))
        with patch('boundary_repair.algorithms.synthesis.source_slice', side_effect=AssertionError('unretrieved read')):
            anchors, unknown = constraint_anchors((claim,), self.scope, self.snap)
        self.assertEqual(anchors, {'need': ()})
        self.assertEqual(unknown, ('need',))

    def test_text_only_scope_keeps_read_context_but_not_other_text_permissions(self):
        self.program = ProgramAdapter(config(self.root, parser=False))
        self.synthesis = ScopeSynthesis(self.model, self.program, self.logic)
        self.scope = self.program.source_scope(self.snap, self.ctx, self.task.problem_statement)
        chosen = self.select(self.pool())
        self.assertFalse(chosen.edit_scope.blocks)
        self.assertEqual({r.path for r in chosen.edit_scope.regions}, {'ui.js'})
        self.assertTrue(all(r.edit_mode == 'text' for r in chosen.edit_scope.regions))
        transaction = TransactionRenderer(self.model).render(self.task, chosen, self.ctx)
        self.assertEqual(transaction.edits[0].operation, 'replace_text')
        self.assertEqual(self.program.compile(self.task, chosen, transaction, self.snap, self.ctx).application_check, 'passed')
        outside = next(r for r in self.scope.regions if r.path == 'large.js')
        with self.assertRaisesRegex(ValidationError, 'unknown_edit_region'):
            transaction_contents(self.snap, chosen.edit_scope, EditTransaction((
                SourceEdit('replace_text', outside.region_id, 'new', old_text=outside.source),)))

    def test_free_text_target_names_do_not_override_exact_frame_binding(self):
        frame = replace(self.claim('same-name', 'large.js', True), targets=self.must.targets)
        contracts = replace(self.contracts, frames=(frame,))
        chosen = self.select(self.pool(contracts), contracts)
        self.assertEqual(chosen.cost.touched_frames, 0)
        self.assertEqual(chosen.cost.unmapped_frames, 0)
        self.assertIn('transitive_effects_unknown', chosen.unresolved)

    def test_partially_mapped_hard_claim_keeps_broad_permissions(self):
        case = self.must.entry_cases[0]
        outside = replace(case, interface=replace(case.interface, site=replace(case.interface.site, path='unseen.js')))
        contracts = replace(self.contracts, must=(replace(self.must, entry_cases=(case, outside)),))
        chosen = self.select(self.pool(contracts), contracts)
        self.assertEqual(chosen.plan_id, 'scoped:transaction')
        self.assertEqual(chosen.cost.unmapped_requirements, 1)
        self.assertIn('unmapped_requirement:need', chosen.unresolved)

    def test_duplicate_interfaces_do_not_spend_extra_plan_budget(self):
        baseline_ctx, duplicate_ctx = context(), context()
        first = self.pool(ctx=baseline_ctx)
        duplicate = replace(self.loc.assessments[0], boundary=replace(self.boundary, boundary_id='same-site'))
        second = self.pool(loc=LocalizationResult(self.loc.assessments + (duplicate,)), ctx=duplicate_ctx)
        self.assertEqual({scope_identity(p.edit_scope) for p in first}, {scope_identity(p.edit_scope) for p in second})
        self.assertEqual(baseline_ctx.budget.patch_candidates, duplicate_ctx.budget.patch_candidates)
        self.assertEqual(duplicate_ctx.budget.ideas, 1)

    def test_nested_permissions_are_measured_as_union_not_sum(self):
        ranges = scope_ranges(self.scope)
        expected = sum(f.size for f in self.scope.files)
        self.assertEqual(sum(b - a for _, a, b in ranges), expected)
        self.assertEqual(merged_ranges((('a', 0, 10), ('a', 3, 7), ('a', 10, 20), ('b', 0, 2))),
                         (('a', 0, 20), ('b', 0, 2)))

    def test_equal_cost_ties_use_stable_identity_not_pool_order(self):
        base = self.pool()[1]
        a, b = replace(base, plan_id='stable-a'), replace(base, plan_id='stable-b')
        self.assertEqual(self.select((a, b)).plan_id, 'stable-a')
        self.assertEqual(self.select((b, a)).plan_id, 'stable-a')


if __name__ == '__main__':
    unittest.main()
