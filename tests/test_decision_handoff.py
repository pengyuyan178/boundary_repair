"""Deterministic decision handoffs on synthetic evidence, with real scope and request checks."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, task, SOURCE
from test_evidence_alignment import EntryModel
from test_scoped_alignment import FocusModel
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.rendering import (
    EDIT_SYSTEM, TransactionRenderer, edit_transaction_schema, generation_handoff, repair_guidance, transaction_plan,
)
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis, scoped_localization_plan
from boundary_repair.domain.repair import ExpressivityVerdict, LocalizationResult
from boundary_repair.kernel.codec import plain
from boundary_repair.kernel.files import tree_digest


@unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
class DecisionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = snapshot(self.root)
        self.program = ProgramAdapter(config(self.root))
        self.logic = LogicAdapter()
        self.task = task()
        ctx = context()
        self.contracts = SpecificationRecovery(EntryModel(), self.program, self.logic).recover(self.task, self.snap, ctx)
        self.loc = ExpressivityLocalization(self.program, self.logic).locate(self.task, self.contracts, self.snap, ctx)
        self.full = next(a for a in self.loc.assessments if a.verdict == ExpressivityVerdict.FEASIBLE)
        self.unknown = replace(self.full, verdict=ExpressivityVerdict.UNKNOWN, certificate=None,
                               unresolved=('pure_deterministic_unproved', 'entry_case_evidence_interpretation_not_verified'))
        self.scope = self.program.source_scope(self.snap, ctx, self.task.problem_statement)

    def plan(self, assessments=None, contracts=None):
        return scoped_localization_plan(contracts or self.contracts, self.scope,
                                        LocalizationResult(tuple(assessments or (self.unknown,))),
                                        self.snap, context())

    def pool(self, count=40):
        return tuple(replace(self.unknown, boundary=replace(self.unknown.boundary, boundary_id=f'boundary:{i}'))
                     for i in range(count))

    def test_only_selected_unknown_interface_is_expanded(self):
        plan = self.plan(self.pool())
        before = plain(plan)
        payload = generation_handoff(plan)
        guidance = payload['repair_guidance']
        self.assertEqual(guidance['handoff_version'], 'decision.v1')
        self.assertEqual(len(guidance['decision_interfaces']), 1)
        self.assertEqual(guidance['decision_interfaces'][0]['boundary_id'], 'boundary:0')
        self.assertEqual(guidance['deferred_interfaces'], [
            {'verdict': 'unknown', 'role': 'alternative', 'boundary_ids': [f'boundary:{i}' for i in range(1, 40)]}])
        self.assertEqual(plain(plan), before)
        self.assertEqual(len(plan.boundary_guidance), 40)
        self.assertNotIn('ranked_interfaces', guidance)
        self.assertNotIn('boundary:boundary:0:pure_deterministic_unproved', payload['unresolved'])
        self.assertIn('boundary:boundary:0:pure_deterministic_unproved', plan.unresolved)

    def test_shared_and_unique_conditions_preserve_exact_applicability(self):
        pool = tuple(replace(a, unresolved=a.unresolved + (('odd_scope',) if i % 2 else ())
                             + ((f'only:{i}',) if i == 7 else ())) for i, a in enumerate(self.pool()))
        guidance = repair_guidance(self.plan(pool))
        actual, reasons = set(), []
        for group in guidance['unresolved_conditions']:
            reasons.extend(group['reasons'])
            actual.update((bid, reason) for bid in group['boundary_ids'] for reason in group['reasons'])
        expected = {(a.boundary.boundary_id, reason) for a in pool for reason in a.unresolved}
        self.assertEqual(actual, expected)
        self.assertEqual(len(reasons), len(set(reasons)))
        self.assertEqual(len(guidance['unresolved_conditions']), 3)
        self.assertTrue(all('unresolved' not in row for row in guidance['decision_interfaces']))

    def test_only_exact_projected_diagnostics_are_removed(self):
        plan = self.plan()
        expanded = 'boundary:' + self.unknown.boundary.boundary_id + ':' + self.unknown.unresolved[0]
        retained = ('boundary:unrelated:same_reason', expanded + ':extra', 'unmapped_requirement:hard', 'global_unknown')
        plan = replace(plan, unresolved=plan.unresolved + retained)
        payload = generation_handoff(plan)
        self.assertTrue(set(retained) <= set(payload['unresolved']))
        self.assertNotIn(expanded, payload['unresolved'])
        self.assertIn('freeform_edits_not_certified_by_local_interfaces', payload['unresolved'])
        self.assertIn('scope_minimality_unproved', payload['unresolved'])

    def test_hard_id_cannot_be_erased_by_a_diagnostic_name_collision(self):
        name = 'boundary:' + self.unknown.boundary.boundary_id + ':' + self.unknown.unresolved[0]
        hard = replace(self.contracts.must[0], constraint_id=name, entry_cases=())
        plan = self.plan(contracts=replace(self.contracts, must=(hard,)))
        payload = generation_handoff(plan)
        self.assertIn(name, payload['unresolved'])
        self.assertEqual(payload['obligations'][0], plain(hard))

    def test_all_contracts_sources_and_interpretations_survive(self):
        unbound = replace(self.contracts.must[0], constraint_id='unbound', description='保留未绑定的视觉要求', entry_cases=())
        may = (replace(unbound, constraint_id='alternative-a'), replace(unbound, constraint_id='alternative-b'))
        contracts = replace(self.contracts, must=self.contracts.must + (unbound,), may=may,
                            interpretation_groups=((('alternative-a',), ('alternative-b',)),))
        plan = self.plan(contracts=contracts)
        payload = generation_handoff(plan)
        self.assertEqual(payload['obligations'], plain(contracts.must + contracts.frames))
        self.assertEqual(payload['soft_hypotheses'], plain(may))
        self.assertEqual(payload['interpretation_groups'], plain(contracts.interpretation_groups))
        self.assertEqual(payload['evidence_sources'], plain(contracts.sources))
        self.assertIn('unbound', payload['unresolved'])
        self.assertTrue(payload['obligations'][0]['entry_cases'])

    def test_multisite_selected_interface_keeps_all_companion_targets(self):
        (self.snap.root / 'companion.js').write_bytes(SOURCE.encode('utf-8'))
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.scope = self.program.source_scope(self.snap, context(), self.task.problem_statement)
        other = next(i for i in self.program.observation_interfaces(self.snap, context()) if i.site.path == 'companion.js')
        pair = replace(self.unknown, boundary=replace(self.unknown.boundary,
                       sites=self.unknown.boundary.sites + (other.site,)))
        plan = self.plan((pair,))
        guidance = repair_guidance(plan)
        row = guidance['decision_interfaces'][0]
        self.assertEqual(row['analyzed_sites'], plain(pair.boundary.sites))
        self.assertEqual(set(guidance['primary_edit_targets']), set(plan.boundary_guidance[0].edit_targets))
        self.assertEqual(len(guidance['primary_edit_targets']), 2)

    def test_both_certificate_polarities_retain_the_exact_interface(self):
        negative = next(a for a in self.loc.assessments if a.verdict == ExpressivityVerdict.INEXPRESSIBLE)
        plan = self.plan((self.unknown, negative))
        guidance = repair_guidance(plan)
        self.assertEqual([r['role'] for r in guidance['decision_interfaces']], ['primary', 'avoid_this_interface'])
        row = guidance['decision_interfaces'][1]
        self.assertEqual(row['limited_interface_certificate'], negative.certificate)
        self.assertEqual(row['analyzed_sites'], plain(negative.boundary.sites))
        self.assertEqual(row['analyzed_read_features'], plain(negative.boundary.readable_features))
        positive = repair_guidance(self.plan((self.full,)))['decision_interfaces'][0]
        self.assertEqual(positive['sufficient_read_features'], plain(self.full.required_features))
        self.assertEqual(positive['limited_interface_certificate'], self.full.certificate)

    def test_certified_nonprimary_interface_is_not_lost(self):
        other = replace(self.full, boundary=replace(self.full.boundary, boundary_id='certified:other'))
        guidance = repair_guidance(self.plan((self.full, other)))
        self.assertEqual([r['role'] for r in guidance['decision_interfaces']], ['primary', 'alternative'])
        self.assertEqual(guidance['decision_interfaces'][1]['limited_interface_certificate'], other.certificate)

    def test_unmapped_certificate_cannot_forbid_the_selected_scope(self):
        negative = next(a for a in self.loc.assessments if a.verdict == ExpressivityVerdict.INEXPRESSIBLE)
        negative = replace(negative, boundary=replace(negative.boundary,
                           sites=(replace(negative.boundary.sites[0], path='not-retrieved.js'),)))
        plan = self.plan((negative,))
        guidance = repair_guidance(plan)
        self.assertFalse(guidance['decision_interfaces'])
        self.assertEqual(guidance['selection_status'], 'no_mapped_focus')
        self.assertEqual(guidance['deferred_interfaces'][0]['verdict'], 'inexpressible')
        self.assertEqual(guidance['deferred_interfaces'][0]['role'], 'unmapped')
        self.assertEqual(plan.edit_scope, self.scope)

    def test_real_request_uses_handoff_after_full_audit_is_frozen(self):
        def before(payload, ctx):
            path = config(self.root).results_root / ctx.run_id / 'cases' / ctx.instance_id / payload['repair_guidance']['audit_ref']
            stored = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(stored['boundary_guidance']), 40)
            self.assertGreater(len(stored['scope_comparison']), 1)
            self.assertEqual(stored['boundary_ids'], payload['repair_guidance']['primary_boundary_ids'])
            self.assertEqual(stored['cost'], payload['scope_selection']['cost'])
            self.assertGreater(len(stored['unresolved']), len(payload['unresolved']))
        model, ctx = FocusModel(before=before), context()
        result = ScopeSynthesis(model, self.program, self.logic).synthesize(
            self.task, self.contracts, LocalizationResult(self.pool()), self.snap, ctx)
        payload = json.loads(model.requests[0].prompt)
        for key, value in generation_handoff(result.plan).items():
            self.assertEqual(payload[key], plain(value))
        self.assertEqual(model.requests[0].output_schema, edit_transaction_schema(result.plan.edit_scope))
        self.assertEqual(ctx.budget.model_calls, 1)
        self.assertEqual(result.patch.application_check, 'passed')
        self.assertEqual((self.snap.root / 'ui.js').read_bytes(), SOURCE.encode('utf-8'))

    def test_raw_evidence_keeps_original_input_and_unavailability(self):
        contracts = replace(self.contracts, must=(), frames=(), may=(), extraction_status='unavailable',
                            diagnostics=('evidence_extraction_unavailable:validation',))
        plan = self.plan(contracts=contracts)
        model = FocusModel()
        TransactionRenderer(model).render(self.task, plan, context())
        payload = json.loads(model.requests[0].prompt)
        self.assertEqual(payload['generation_mode'], 'raw_evidence')
        self.assertIn(self.task.problem_statement, json.dumps(payload['original_evidence'], ensure_ascii=False).replace('\\n', '\n'))
        self.assertIn('evidence_extraction_unavailable:validation', payload['unresolved'])
        self.assertEqual(len(model.requests), 1)

    def test_projection_does_not_truncate_source_or_change_permissions(self):
        plan = self.plan(self.pool())
        model = FocusModel()
        TransactionRenderer(model).render(self.task, plan, context())
        payload = json.loads(model.requests[0].prompt)
        for region in plan.edit_scope.regions:
            visible = next(r for r in payload['regions'] if r['region_id'] == region.region_id)
            self.assertEqual(''.join(line['text'] for line in visible['lines']), region.source)
        self.assertEqual(payload['files'], plain(plan.edit_scope.files))
        self.assertEqual(payload['creation_roots'], list(plan.edit_scope.creation_roots))
        self.assertEqual({b['block_id'] for b in payload['blocks']}, {b.block_id for b in plan.edit_scope.blocks})
        self.assertEqual(model.requests[0].output_schema, edit_transaction_schema(plan.edit_scope))

    def test_plain_control_payload_and_system_remain_unchanged(self):
        plan = transaction_plan(self.contracts, self.scope, context(), 'plain')
        model = FocusModel()
        TransactionRenderer(model).render(self.task, plan, context())
        payload = json.loads(model.requests[0].prompt)
        self.assertNotIn('repair_guidance', payload)
        self.assertNotIn('scope_selection', payload)
        self.assertEqual(payload['unresolved'], list(plan.unresolved))
        self.assertEqual(model.requests[0].system, EDIT_SYSTEM)
        self.assertEqual(list(payload), ['original_evidence', 'generation_mode', 'files', 'regions', 'blocks',
                                        'creation_roots', 'obligations', 'soft_hypotheses', 'interpretation_groups',
                                        'evidence_sources', 'unresolved', 'scope_diagnostics'])

    def test_projection_is_deterministic_without_spending_budget(self):
        plan, ctx = self.plan(self.pool()), context()
        before = plain(ctx.budget)
        self.assertEqual(json.dumps(generation_handoff(plan)), json.dumps(generation_handoff(plan)))
        self.assertEqual(plain(ctx.budget), before)


if __name__ == '__main__':
    unittest.main()
