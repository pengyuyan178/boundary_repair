"""Scoped generation consumes localization with real parsing, logic and patch compilation."""
from dataclasses import replace
from itertools import product
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, task, SOURCE
from test_evidence_alignment import EntryModel
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.controls import PlainControls
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.rendering import EDIT_SYSTEM, TransactionRenderer, repair_guidance
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis, boundary_edit_targets, scoped_localization_plan
from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.repair import ExpressivityVerdict, LocalizationResult
from boundary_repair.kernel.files import tree_digest
from boundary_repair.ports import ModelResponse


def target_source(prompt, target):
    """Read the selected block or text region only from the model's actual visible request."""
    block = next((b for b in prompt['blocks'] if b['block_id'] == target), None)
    region_id = block['region_id'] if block else target
    region = next(r for r in prompt['regions'] if r['region_id'] == region_id)
    text = region['source']
    if block is None:
        return text
    offsets = []
    for position in (block['start_inclusive'], block['end_exclusive']):
        offsets.append(sum(len(line) + 1 for line in text.split('\n')[:position['line'] - region['start_line']])
                       + position['column'])
    return text[offsets[0]:offsets[1]]


class FocusModel:
    """A scripted generator follows the frozen focus, not a real model-quality measurement."""
    def __init__(self, before=None, invalid=False):
        self.requests = []
        self.before = before
        self.invalid = invalid

    def complete(self, request, ctx):
        self.requests.append(request)
        if request.schema_name != 'edits.v4':
            raise AssertionError('unexpected additional model call')
        ctx.budget.begin_model_call(request.max_output_tokens)
        ctx.budget.record_output_tokens(1)
        prompt = json.loads(request.prompt)
        if self.before:
            self.before(prompt, ctx)
        focus = prompt.get('repair_guidance', {}).get('primary_edit_targets', [])
        targets = focus or [b['block_id'] for b in prompt['blocks']] or [r['region_id'] for r in prompt['regions']]
        for target in targets:
            old = target_source(prompt, target)
            new = old.replace('active || hidden', 'active && !hidden').replace('a || b || c', 'a && b && c')
            if new != old:
                break
        else:
            raise AssertionError('synthetic focus contains no scripted replacement')
        is_block = any(b['block_id'] == target for b in prompt['blocks'])
        data = {'edits': [{'operation': 'replace_block' if is_block else 'replace_text', 'target': target,
                           'old_text': '' if is_block else old, 'new_text': new, 'destination': ''}]}
        return ModelResponse('not-json' if self.invalid else json.dumps(data), 1, 'synthetic-focus-model')


class ThreeInputEvidence:
    """Script the eight truth-table rows explicitly stated in the synthetic three-input issue."""
    def complete(self, request, ctx):
        if request.schema_name != 'evidence.v4':
            raise AssertionError('unexpected evidence retry')
        ctx.budget.begin_model_call(request.max_output_tokens)
        ctx.budget.record_output_tokens(1)
        prompt = json.loads(request.prompt)
        interface = next(i for i in prompt['observation_interfaces'] if i['site']['symbol'] == 'gate')
        refs = [{'span_id': next(r['span_id'] for r in prompt['evidence_catalog']['spans'] if r['kind'] == kind)}
                for kind in ('issue_text', 'base_code')]
        claim = {'statement': 'Return the conjunction of all three Boolean inputs.', 'evidence_refs': refs,
                 'targets': [], 'formalization': None,
                 'entry_cases': [{'interface_id': interface['interface_id'],
                                  'inputs': [{'parameter': name, 'value': value} for name, value in zip(('a', 'b', 'c'), row)],
                                  'expected': all(row)} for row in product((False, True), repeat=3)]}
        data = {'observations': [], 'requirement_groups': [{'alternatives': [{'all_of': [claim]}]}], 'frames': []}
        return ModelResponse(json.dumps(data), 1, 'synthetic-three-input-evidence')


@unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
class ScopedAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = snapshot(self.root)
        self.program = ProgramAdapter(config(self.root))
        self.logic = LogicAdapter()
        self.ctx = context()
        self.task = task()
        self.contracts = SpecificationRecovery(EntryModel(), self.program, self.logic).recover(
            self.task, self.snap, self.ctx)
        self.localized = ExpressivityLocalization(self.program, self.logic).locate(
            self.task, self.contracts, self.snap, self.ctx)
        self.scope = self.program.source_scope(self.snap, self.ctx)
        self.serial = 0

    def assessment(self, verdict):
        return next(a for a in self.localized.assessments if a.verdict == verdict)

    def generate(self, localized, model=None, contracts=None, plain=False):
        self.serial += 1
        ctx = replace(context(), run_id=f'scoped-check-{self.serial}')
        model = model or FocusModel()
        generator = PlainControls(model, self.program) if plain else ScopeSynthesis(model, self.program, self.logic)
        result = generator.synthesize(self.task, contracts or self.contracts, localized, self.snap, ctx)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(ctx.budget.model_calls, 1)
        self.assertEqual(result.patch.application_check, 'passed')
        self.assertEqual(result.plan.generation_mode, 'scoped')
        if not plain:
            self.assertEqual(ctx.budget.patch_candidates, len(result.plan.scope_comparison))
        return result, model.requests[0]

    def test_removing_second_layer_changes_real_scoped_request_and_frozen_plan(self):
        negative = self.assessment(ExpressivityVerdict.INEXPRESSIBLE)
        with_result, request = self.generate(LocalizationResult((negative,), ('analyzed_pool',)))
        without, empty_request = self.generate(LocalizationResult(()))
        self.assertNotEqual(request, empty_request)
        self.assertNotEqual(with_result.plan, without.plan)
        payload = json.loads(request.prompt)
        self.assertEqual(payload['repair_guidance']['decision_interfaces'][0]['role'], 'avoid_this_interface')
        self.assertIn('analyzed_pool', with_result.plan.unresolved)
        self.assertTrue({b.block_id for b in with_result.plan.edit_scope.blocks}
                        < {b.block_id for b in without.plan.edit_scope.blocks})
        self.assertEqual(len(without.plan.scope_comparison), 1)
        self.assertEqual(json.loads(empty_request.prompt)['repair_guidance']['decision_interfaces'], [])

    def test_unknown_ranking_selects_different_focus_and_changes_scripted_patch(self):
        (self.snap.root / 'other.js').write_bytes(SOURCE.encode('utf-8'))
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        unbound = replace(self.contracts, must=tuple(replace(c, entry_cases=()) for c in self.contracts.must),
                          frames=tuple(replace(c, entry_cases=()) for c in self.contracts.frames))
        located = ExpressivityLocalization(self.program, self.logic).locate(self.task, unbound, self.snap, self.ctx)
        candidates = [next(a for a in located.assessments
                           if a.boundary.sites[0].path == path and a.boundary.sites[0].node_kind == 'BooleanReturn')
                      for path in ('ui.js', 'other.js')]
        self.assertTrue(all(a.verdict == ExpressivityVerdict.UNKNOWN for a in candidates))
        left, left_request = self.generate(LocalizationResult(tuple(candidates)), contracts=unbound)
        right, right_request = self.generate(LocalizationResult(tuple(reversed(candidates))), contracts=unbound)
        self.assertEqual(left.plan.boundary_ids, (candidates[0].boundary.boundary_id,))
        self.assertEqual(right.plan.boundary_ids, (candidates[1].boundary.boundary_id,))
        self.assertEqual(json.loads(left_request.prompt)['regions'][0]['path'], 'ui.js')
        self.assertEqual(json.loads(right_request.prompt)['regions'][0]['path'], 'other.js')
        self.assertIn('diff --git a/ui.js b/ui.js', left.patch.unified_diff)
        self.assertIn('diff --git a/other.js b/other.js', right.patch.unified_diff)
        self.assertNotEqual(left.patch.unified_diff, right.patch.unified_diff)
        self.assertEqual(left.plan.edit_scope.files, right.plan.edit_scope.files)
        self.assertEqual((self.snap.root / 'ui.js').read_bytes(), SOURCE.encode('utf-8'))

    def test_feasible_three_input_interface_guides_one_model_generation(self):
        source = 'function gate(a, b, c) { return a || b || c; }\nmodule.exports = {gate};\n'
        (self.snap.root / 'ui.js').write_text(source, encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.task = replace(self.task, problem_statement='gate is a pure Boolean entry. Return true exactly when a, b and c are all true; otherwise return false.')
        contracts = SpecificationRecovery(ThreeInputEvidence(), self.program, self.logic).recover(self.task, self.snap, self.ctx)
        located = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        feasible = next(a for a in located.assessments if a.verdict == ExpressivityVerdict.FEASIBLE)
        self.assertEqual({f.feature_id for f in feasible.required_features}, {'a', 'b', 'c'})
        with patch('boundary_repair.algorithms.synthesis.synthesize_boolean', side_effect=AssertionError('not finite generation')):
            result, request = self.generate(located, contracts=contracts)
        self.assertEqual(result.plan.boundary_ids, (feasible.boundary.boundary_id,))
        guidance = json.loads(request.prompt)['repair_guidance']
        self.assertEqual(guidance['selection_status'], 'focus_selected')
        self.assertEqual({f['feature_id'] for f in guidance['decision_interfaces'][0]['sufficient_read_features']}, {'a', 'b', 'c'})
        self.assertIn('not a uniquely necessary set', request.system)
        self.assertTrue(result.plan.obligations[0].entry_cases)
        self.apply_and_execute(result, 'gate', 3, [False] * 7 + [True])

    def apply_and_execute(self, result, name, arity, expected):
        candidate = self.root / ('candidate-' + str(self.serial))
        candidate.mkdir()
        (candidate / 'ui.js').write_bytes((self.snap.root / 'ui.js').read_bytes())
        subprocess.run(['git', 'apply', '-'], input=result.patch.unified_diff.encode('utf-8'),
                       cwd=candidate, capture_output=True, check=True)
        rows = list(product((False, True), repeat=arity))
        script = 'const f=require(process.argv[1])[process.argv[2]]; console.log(JSON.stringify(JSON.parse(process.argv[3]).map(v=>f(...v))));'
        output = subprocess.check_output(['node', '-e', script, str(candidate / 'ui.js'), name, json.dumps(rows)])
        self.assertEqual(json.loads(output), expected)

    def test_negative_interface_does_not_forbid_broader_same_block_repair(self):
        negative = self.assessment(ExpressivityVerdict.INEXPRESSIBLE)
        self.assertFalse(negative.boundary.readable_features)
        result, request = self.generate(LocalizationResult((negative,)))
        guidance = json.loads(request.prompt)['repair_guidance']
        self.assertEqual(guidance['selection_status'], 'broader_interface_required')
        self.assertFalse(result.plan.boundary_ids)
        self.assertIn('NOT forbidden', request.system)
        self.assertEqual(result.plan.read_scope.files, self.scope.files)
        self.assertFalse(any(f.complete for f in result.plan.edit_scope.files))
        self.assertFalse(result.plan.edit_scope.creation_roots)
        self.assertTrue(set(result.plan.edit_scope.blocks) <= set(self.scope.blocks))
        self.assertTrue(boundary_edit_targets(negative.boundary, result.plan.edit_scope, self.snap))
        self.assertTrue(any(row.plan_id == 'scoped:transaction' for row in result.plan.scope_comparison))
        self.apply_and_execute(result, 'shouldShow', 2, [False, False, True, False])

    def test_negative_does_not_hide_unknown_alternative_at_same_source_site(self):
        negative = self.assessment(ExpressivityVerdict.INEXPRESSIBLE)
        full = self.assessment(ExpressivityVerdict.FEASIBLE)
        unknown = replace(full, verdict=ExpressivityVerdict.UNKNOWN, certificate=None, unresolved=('unsupported_summary',))
        result, request = self.generate(LocalizationResult((negative, unknown)))
        self.assertEqual(result.plan.boundary_ids, (unknown.boundary.boundary_id,))
        rows = json.loads(request.prompt)['repair_guidance']['decision_interfaces']
        self.assertEqual([r['role'] for r in rows], ['primary', 'avoid_this_interface'])
        self.assertEqual(rows[0]['edit_targets'], rows[1]['edit_targets'])

    def test_certificate_missing_downgrades_inexpressible_to_unknown(self):
        negative = replace(self.assessment(ExpressivityVerdict.INEXPRESSIBLE), certificate=None)
        result, request = self.generate(LocalizationResult((negative,)))
        guidance = json.loads(request.prompt)['repair_guidance']
        row = guidance['decision_interfaces'][0]
        self.assertEqual((row['verdict'], row['role']), ('unknown', 'primary'))
        self.assertTrue(any('missing_interface_certificate' in group['reasons']
                            and row['boundary_id'] in group['boundary_ids']
                            for group in guidance['unresolved_conditions']))
        self.assertIsNone(row['sufficient_read_features'])

    def test_unknown_reasons_are_consumed_without_becoming_negative_facts(self):
        full = self.assessment(ExpressivityVerdict.FEASIBLE)
        a = replace(full, verdict=ExpressivityVerdict.UNKNOWN, certificate=None, unresolved=('downstream_sound',))
        b = replace(a, unresolved=('witnesses_reachable',))
        left, lr = self.generate(LocalizationResult((a,)))
        right, rr = self.generate(LocalizationResult((b,)))
        self.assertNotEqual(lr.prompt, rr.prompt)
        self.assertEqual(left.plan.boundary_ids, right.plan.boundary_ids)
        for request in (lr, rr):
            row = json.loads(request.prompt)['repair_guidance']['decision_interfaces'][0]
            self.assertEqual(row['role'], 'primary')
            self.assertIsNone(row['limited_interface_certificate'])
            self.assertIsNone(row['sufficient_read_features'])

    def test_plain_control_remains_independent_of_second_layer_conclusions(self):
        negative = self.assessment(ExpressivityVerdict.INEXPRESSIBLE)
        left, lr = self.generate(LocalizationResult((negative,)), plain=True)
        right, rr = self.generate(LocalizationResult(()), plain=True)
        self.assertEqual(lr, rr)
        self.assertEqual(left.plan, right.plan)
        self.assertIsNone(left.plan.boundary_guidance)
        self.assertNotIn('repair_guidance', json.loads(lr.prompt))
        self.assertEqual(lr.system, EDIT_SYSTEM)

    def test_every_hard_and_soft_obligation_survives_guided_planning(self):
        extra = replace(self.contracts.must[0], constraint_id='unsupported-hard', entry_cases=())
        may = replace(extra, constraint_id='uncertain-alternative')
        contracts = replace(self.contracts, must=self.contracts.must + (extra,), may=(may,))
        localized = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        plan = scoped_localization_plan(contracts, self.scope, localized, self.snap, context())
        self.assertTrue(all(g.assessment.verdict == ExpressivityVerdict.UNKNOWN for g in plan.boundary_guidance))
        self.assertEqual(plan.obligations, contracts.must + contracts.frames)
        self.assertEqual(plan.soft_obligations, contracts.may)
        self.assertEqual(plan.evidence_sources, contracts.sources)
        self.assertEqual(plan.interpretation_groups, contracts.interpretation_groups)
        self.assertIsNone(plan.cost)
        self.assertIn('scope_minimality_unproved', plan.unresolved)
        self.assertIn('freeform_edits_not_certified_by_local_interfaces', plan.unresolved)

    def test_mapping_uses_smallest_covering_block_not_same_named_function(self):
        assessment = self.assessment(ExpressivityVerdict.FEASIBLE)
        site = assessment.boundary.sites[0]
        targets = boundary_edit_targets(assessment.boundary, self.scope, self.snap)
        covering = [b for b in self.scope.blocks if b.start_byte <= site.start_byte and site.end_byte <= b.end_byte]
        self.assertEqual(targets, (min(covering, key=lambda b: (b.end_byte - b.start_byte, b.block_id)).block_id,))
        repeated = replace(assessment.boundary, sites=(site, site))
        self.assertEqual(boundary_edit_targets(repeated, self.scope, self.snap), targets)

    def test_unmapped_multisite_boundary_cannot_be_selected_partially(self):
        (self.snap.root / 'outside.js').write_bytes(SOURCE.encode('utf-8'))
        base = self.assessment(ExpressivityVerdict.FEASIBLE)
        other = replace(base.boundary.sites[0], path='outside.js')
        boundary = replace(base.boundary, sites=base.boundary.sites + (other,))
        unknown = replace(base, boundary=boundary, verdict=ExpressivityVerdict.UNKNOWN, certificate=None)
        plan = scoped_localization_plan(self.contracts, self.scope, LocalizationResult((unknown,)), self.snap, context())
        self.assertFalse(plan.boundary_ids)
        self.assertFalse(plan.boundary_guidance[0].edit_targets)
        self.assertIn('unmapped_boundary:' + boundary.boundary_id, plan.unresolved)
        guidance = repair_guidance(plan)
        self.assertFalse(guidance['decision_interfaces'])
        self.assertEqual(guidance['deferred_interfaces'], [
            {'verdict': 'unknown', 'role': 'unmapped', 'boundary_ids': [boundary.boundary_id]}])

    def test_stale_boundary_hash_is_rejected_before_generation(self):
        original = self.assessment(ExpressivityVerdict.INEXPRESSIBLE)
        bad_site = replace(original.boundary.sites[0], content_sha256='0' * 64)
        bad = replace(original, boundary=replace(original.boundary, sites=(bad_site,)))
        model = FocusModel()
        with self.assertRaisesRegex(ValidationError, 'stale_source_hash'):
            self.generate(LocalizationResult((bad,)), model)
        self.assertFalse(model.requests)

    def test_plan_is_frozen_before_model_receives_its_focus(self):
        base = self.assessment(ExpressivityVerdict.FEASIBLE)
        unknown = replace(base, verdict=ExpressivityVerdict.UNKNOWN, certificate=None)
        def inspect(prompt, ctx):
            path = config(self.root).results_root / ctx.run_id / 'cases' / ctx.instance_id / 'trajectory/generation_plan.json'
            stored = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(stored['boundary_ids'], prompt['repair_guidance']['primary_boundary_ids'])
            self.assertTrue(stored['boundary_guidance'])
        self.generate(LocalizationResult((unknown,)), FocusModel(before=inspect))

    def test_bad_transaction_still_gets_only_one_generation_call(self):
        model = FocusModel(invalid=True)
        with self.assertRaises(ValidationError):
            self.generate(LocalizationResult(()), model)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual((self.snap.root / 'ui.js').read_bytes(), SOURCE.encode('utf-8'))

    def test_text_only_unknown_retains_text_operation_and_specific_reason(self):
        self.program = ProgramAdapter(config(self.root, parser=False))
        unbound = replace(self.contracts, witnesses=(), must=tuple(replace(c, entry_cases=()) for c in self.contracts.must),
                          frames=tuple(replace(c, entry_cases=()) for c in self.contracts.frames))
        located = ExpressivityLocalization(self.program, self.logic).locate(self.task, unbound, self.snap, self.ctx)
        result, request = self.generate(located, contracts=unbound)
        prompt = json.loads(request.prompt)
        self.assertEqual(prompt['blocks'], [])
        self.assertEqual(prompt['repair_guidance']['primary_edit_targets'], [prompt['regions'][0]['region_id']])
        self.assertTrue(all(row['verdict'] == 'unknown' for row in prompt['repair_guidance']['decision_interfaces']))
        self.assertEqual(result.patch.syntax_check, 'unknown')

    def test_empty_analysis_preserves_capabilities_and_does_not_invent_focus(self):
        plan = scoped_localization_plan(self.contracts, self.scope, LocalizationResult(()), self.snap, context())
        self.assertEqual(plan.edit_scope, self.scope)
        self.assertFalse(plan.boundary_ids)
        self.assertEqual(plan.boundary_guidance, ())
        self.assertEqual(repair_guidance(plan)['selection_status'], 'no_mapped_focus')

    def test_same_named_functions_in_one_file_get_distinct_scoped_targets(self):
        source = ('const first = function gate(active, hidden) { return active || hidden; };\n'
                  'const second = function gate(active, hidden) { return active || hidden; };\n')
        (self.snap.root / 'ui.js').write_text(source, encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        scope = self.program.source_scope(self.snap, self.ctx, self.task.problem_statement)
        located = ExpressivityLocalization(self.program, self.logic).locate(self.task, self.contracts, self.snap, self.ctx)
        by_site = {a.boundary.sites[0]: a for a in located.assessments
                   if a.boundary.sites[0].node_kind == 'BooleanReturn'}
        self.assertEqual(len(by_site), 2)
        targets = [boundary_edit_targets(a.boundary, scope, self.snap) for a in by_site.values()]
        self.assertTrue(all(targets))
        self.assertTrue(set(targets[0]).isdisjoint(targets[1]))

    def test_positive_read_features_change_scoped_guidance_without_becoming_permissions(self):
        full = self.assessment(ExpressivityVerdict.FEASIBLE)
        left = scoped_localization_plan(self.contracts, self.scope, LocalizationResult((full,)), self.snap, context())
        injected = replace(full, required_features=full.required_features[:1])
        right = scoped_localization_plan(self.contracts, self.scope, LocalizationResult((injected,)), self.snap, context())
        lm, rm = FocusModel(), FocusModel()
        TransactionRenderer(lm).render(self.task, left, context())
        TransactionRenderer(rm).render(self.task, right, context())
        self.assertNotEqual(lm.requests[0].prompt, rm.requests[0].prompt)
        self.assertEqual(lm.requests[0].output_schema, rm.requests[0].output_schema)
        self.assertEqual(left.edit_scope, right.edit_scope)
        self.assertIn('not a uniquely necessary set or an exclusive allowed-symbol list', lm.requests[0].system)

    def test_unretrieved_path_is_not_read_to_create_guidance(self):
        base = self.assessment(ExpressivityVerdict.FEASIBLE)
        boundary = replace(base.boundary, sites=(replace(base.boundary.sites[0], path='unretrieved.js'),))
        with patch('boundary_repair.algorithms.synthesis.source_slice', side_effect=AssertionError('out-of-scope read')):
            self.assertEqual(boundary_edit_targets(boundary, self.scope, self.snap), ())

    def test_legacy_line_span_is_resolved_before_mapping_to_byte_boundaries(self):
        import hashlib
        base = self.assessment(ExpressivityVerdict.FEASIBLE)
        site = base.boundary.sites[0]
        line = SOURCE.encode('utf-8').splitlines(keepends=True)[site.start_line - 1]
        legacy = replace(site, start_byte=None, end_byte=None, content_sha256=hashlib.sha256(line).hexdigest())
        targets = boundary_edit_targets(replace(base.boundary, sites=(legacy,)), self.scope, self.snap)
        self.assertTrue(targets)
        self.assertTrue(set(targets) <= {b.block_id for b in self.scope.blocks})

    def test_guided_planning_counts_one_plan_not_one_per_hint(self):
        ctx = context()
        scoped_localization_plan(self.contracts, self.scope, self.localized, self.snap, ctx)
        self.assertEqual(ctx.budget.ideas, 1)
        self.assertEqual(ctx.budget.patch_candidates, 1)
        self.assertEqual(ctx.budget.model_calls, 0)


if __name__ == '__main__':
    unittest.main()
