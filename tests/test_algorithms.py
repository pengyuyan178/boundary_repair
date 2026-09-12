"""Actual finite algorithms and real compiler parsing, not method-call mocks."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, evidence, anchored_evidence, parser_module, snapshot, task, write_fixture
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.model import FrozenModelAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.errors import EvidenceConflict, ValidationError
from boundary_repair.domain.specification import Coverage, InterpretationSpace, SolverStatus, Term
from boundary_repair.domain.repair import ExpressivityVerdict
from boundary_repair.kernel.evidence import EVIDENCE_SCHEMA, parse_evidence, evidence_spans, evidence_request, parse_anchored_evidence
from boundary_repair.domain.task import IssueAsset
from boundary_repair.kernel.terms import literal, symbol
from boundary_repair.kernel.files import source_slice


class EvidenceTests(unittest.TestCase):
    def test_code_span_offsets_refer_to_full_original_file(self):
        original = '// context\r\n' * 20 + 'const enabled = true;\r\n'
        start = original.index('const enabled')
        code = ({'path': 'ui.js', 'source': original[start:], 'start_char': start},)
        span = next(row for row in evidence_spans(task(), code) if row['kind'] == 'base_code')
        self.assertEqual(span['start'], start)
        self.assertEqual(original[span['start']:span['end']], span['text'])

    def test_v2_requires_description_context_and_canonical_term_fields(self):
        for field in ('description', 'context', 'extra_term', 'missing_literal'):
            data = anchored_evidence()
            claim = data['claims'][0]
            if field == 'description':
                del claim['description']
            elif field == 'context':
                del claim['targets'][0]['context']
            elif field == 'extra_term':
                claim['relation']['value'] = None
            else:
                del claim['relation']['args'][1]['value']
            with self.subTest(field=field), self.assertRaises(ValidationError):
                parse_anchored_evidence(json.dumps(data), task(), ())

    def test_anchor_preserves_urls_crlf_unicode_and_duplicate_text(self):
        original = 'URL https://example.invalid/a%20b\\r\\n中文😀\r\nrepeat\nrepeat\r\n'
        t = replace(task(), problem_statement=original)
        spans = evidence_spans(t, ())
        self.assertEqual(''.join(row['text'] for row in spans), original)
        self.assertEqual(len({row['span_id'] for row in spans}), len(spans))
        for row in spans:
            self.assertEqual(original[row['start']:row['end']], row['text'])
        data = anchored_evidence()
        data['sources'] = [{'source_id': f's{i}', 'kind': 'issue_text', 'span_id': spans[0]['span_id']} for i in range(4)]
        bundle = parse_anchored_evidence(json.dumps(data), t, ())
        self.assertEqual(bundle.sources[0].locator, spans[0]['text'])
        self.assertIn('%20', bundle.sources[0].locator)
        self.assertIn('\r\n', bundle.sources[0].locator)

    def test_spans_bound_long_lines_without_dropping_characters(self):
        original = '中😀' * 3000 + '\r\n'
        spans = evidence_spans(replace(task(), problem_statement=original), ())
        self.assertTrue(all(len(row['text']) <= 2000 for row in spans))
        self.assertEqual(''.join(row['text'] for row in spans), original)

    def test_anchored_evidence_rejects_unknown_id_and_stitched_reference(self):
        for reference in ('T-invented', ['T0000_00000000', 'T0000_00000086']):
            data = anchored_evidence()
            data['sources'][0]['span_id'] = reference
            with self.assertRaises(ValidationError):
                parse_anchored_evidence(json.dumps(data), task(), ())

    def test_anchored_evidence_rejects_kind_substitution(self):
        data = anchored_evidence()
        data['sources'][0]['kind'] = 'base_code'
        with self.assertRaisesRegex(ValidationError, 'unknown_evidence_span'):
            parse_anchored_evidence(json.dumps(data), task(), ())

    def test_anchor_protocol_does_not_accept_legacy_quote_field(self):
        with self.assertRaises(ValidationError):
            parse_anchored_evidence(json.dumps(evidence()), task(), ())

    def test_choice_errors_are_rejected_without_rewriting(self):
        for groups in ([['c0']], [['c0', 'c0']], [['c0', 'missing']], [['c0', 'c1'], ['c1', 'c2']]):
            data = anchored_evidence()
            data['choice_groups'] = groups
            with self.assertRaisesRegex(ValidationError, 'invalid_or_overlapping_choice_group'):
                parse_anchored_evidence(json.dumps(data), task(), ())
            self.assertEqual(data['choice_groups'], groups)

    def test_nested_term_array_has_actionable_path(self):
        data = anchored_evidence()
        data['claims'][0]['relation']['args'][1] = [{'op': 'literal', 'value': True}]
        with self.assertRaisesRegex(ValidationError, r'invalid_term_shape:claims\[0\].relation.args\[1\]'):
            parse_anchored_evidence(json.dumps(data), task(), ())

    def test_recursive_output_contract_is_attached(self):
        request = evidence_request(task(), (), 1000)
        self.assertEqual(request.schema_name, 'evidence.v2')
        self.assertFalse(request.output_schema['additionalProperties'])
        term = request.output_schema['$defs']['term']['anyOf']
        self.assertTrue(any(row['properties'].get('args', {}).get('items') == {'$ref': '#/$defs/term'} for row in term))
        self.assertIn('evidence_spans', json.loads(request.prompt))

    def test_v2_image_and_code_spans_still_require_real_sources(self):
        t = replace(task(), assets=(IssueAsset('https://example.invalid/x.png', 'image-0'),))
        code = ({'path': 'ui.js', 'source': 'return enabled;\r\n'},)
        span = next(row for row in evidence_spans(t, code) if row['kind'] == 'base_code')
        data = anchored_evidence()
        data['sources'].extend([{'source_id': 'code', 'kind': 'base_code', 'span_id': span['span_id']},
                                {'source_id': 'image', 'kind': 'issue_image', 'asset_id': 'image-0', 'bbox': [0, 0, 1, 1]}])
        bundle = parse_anchored_evidence(json.dumps(data), t, code)
        self.assertEqual(bundle.sources[-2].locator, 'ui.js#quote=return enabled;\r\n')
        data['sources'][-1]['bbox'] = [0, 0, 100, 100]
        with self.assertRaisesRegex(ValidationError, 'invalid_image_anchor'):
            parse_anchored_evidence(json.dumps(data), t, code)

    def test_prompt_example_has_valid_claim_references(self):
        example_task = replace(task(), problem_statement=EVIDENCE_SCHEMA['sources'][0]['locator'])
        result = parse_evidence(json.dumps(EVIDENCE_SCHEMA), example_task, ())
        self.assertEqual(len(result.claims), 1)

    def test_all_three_source_formats_and_strict_image_anchors(self):
        data = evidence()
        data['sources'].extend([
            {'source_id': 'visual', 'kind': 'issue_image',
             'locator': 'issue-image-0#bbox=0.1,0.2,0.8,0.9'},
            {'source_id': 'code', 'kind': 'base_code', 'locator': 'ui.js#quote=return active;'},
        ])
        image_task = replace(task(), assets=(IssueAsset('https://example.invalid/image.png', 'issue-image-0'),))
        code = ({'path': 'ui.js', 'source': 'function visible(active) { return active; }'},)
        result = parse_evidence(json.dumps(data), image_task, code)
        self.assertEqual(len(result.sources), 6)
        for locator in ('invented#bbox=0,0,1,1', 'issue-image-0#bbox=0,0,100,100',
                        'issue-image-0#bbox=0.8,0.2,0.1,0.9', 'issue-image-0'):
            with self.subTest(locator=locator):
                data['sources'][-2]['locator'] = locator
                with self.assertRaisesRegex(ValidationError, 'invalid_image_anchor'):
                    parse_evidence(json.dumps(data), image_task, code)

    def test_nonexistent_quote_rejected(self):
        data = evidence()
        data['sources'][0]['locator'] = 'not present anywhere'
        with self.assertRaises(ValidationError):
            parse_evidence(json.dumps(data), task(), ())

    def test_unknown_source_rejected(self):
        data = evidence()
        data['claims'][0]['source_ids'] = ['invented']
        with self.assertRaises(ValidationError):
            parse_evidence(json.dumps(data), task(), ())

    def test_frame_not_created_from_observation(self):
        data = evidence()
        data['claims'][2]['kind'] = 'observation'
        bundle = parse_evidence(json.dumps(data), task(), ())
        service = SpecificationRecovery(None, None, LogicAdapter())
        space = service.build_interpretation_space(bundle, (), context())
        result = service.derive_contracts(bundle, (), space, context())
        self.assertEqual(len(result.frames), 0)
        self.assertEqual(len(result.must), 3)

    def test_alternatives_remain_may(self):
        data = evidence()
        data['choice_groups'] = [['c0', 'c1']]
        bundle = parse_evidence(json.dumps(data), task(), ())
        service = SpecificationRecovery(None, None, LogicAdapter())
        space = service.build_interpretation_space(bundle, (), context())
        result = service.derive_contracts(bundle, (), space, context())
        self.assertEqual({c.constraint_id for c in result.may}, {'c0', 'c1'})
        self.assertEqual({c.constraint_id for c in result.must}, {'c3'})

    def test_inconsistent_theory_not_vacuously_true(self):
        bundle = parse_evidence(json.dumps(evidence()), task(), ())
        service = SpecificationRecovery(None, None, LogicAdapter())
        space = InterpretationSpace((), (), SolverStatus.UNSAT, Coverage.COMPLETE)
        with self.assertRaises(EvidenceConflict):
            service.derive_contracts(bundle, (), space, context())

    def test_model_cannot_assert_proof_flags(self):
        data = evidence()
        data['claims'][0]['reachable'] = True
        with self.assertRaises(ValidationError):
            parse_evidence(json.dumps(data), task(), ())

    def test_ambiguous_frame_is_not_protection(self):
        data = evidence()
        data['choice_groups'] = [['c1', 'c2']]
        bundle = parse_evidence(json.dumps(data), task(), ())
        service = SpecificationRecovery(None, None, LogicAdapter())
        space = service.build_interpretation_space(bundle, (), context())
        result = service.derive_contracts(bundle, (), space, context())
        self.assertFalse(result.frames)
        self.assertTrue(any(c.constraint_id == 'c2' for c in result.may))


@unittest.skipUnless(parser_module(), 'install pinned typescript or set BOUNDARY_TEST_TYPESCRIPT')
class AlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = snapshot(self.root)
        self.config = config(self.root)
        self.config = replace(self.config, integration=replace(self.config.integration, model_mode='fixture',
                                                              fixture_file=write_fixture(self.root)))
        self.program = ProgramAdapter(self.config)
        self.model = FrozenModelAdapter(self.config)
        self.logic = LogicAdapter()
        self.ctx = context()
        self.spec = SpecificationRecovery(self.model, self.program, self.logic)

    def test_extract_evidence_real_normalization(self):
        result = self.spec.extract_evidence(task(), self.snapshot, self.ctx)
        self.assertEqual(len(result.claims), 4)
        self.assertEqual(self.ctx.budget.model_calls, 1)

    def test_bind_entities_real_source_ranges(self):
        bundle = parse_evidence(json.dumps(evidence()), task(), ())
        result = self.spec.bind_entities(bundle, self.snapshot, self.ctx)
        self.assertEqual(result[0].entity_id, 'shouldShow')
        self.assertGreaterEqual(len(result[0].candidates), 2)
        for span in result[0].candidates:
            source_slice(self.snapshot.root, span)

    def test_must_may_frame_and_witness(self):
        result = self.spec.recover(task(), self.snapshot, self.ctx)
        self.assertEqual((len(result.must), len(result.may), len(result.frames)), (3, 0, 1))
        self.assertTrue(all(w.reachability == SolverStatus.UNKNOWN for w in result.witnesses))

    def test_constant_interface_is_inexpressible_parameters_are_feasible(self):
        contracts = self.spec.recover(task(), self.snapshot, self.ctx)
        result = ExpressivityLocalization(self.program, self.logic).locate(task(), contracts, self.snapshot, self.ctx)
        self.assertEqual(result.assessments[0].verdict, ExpressivityVerdict.FEASIBLE)
        self.assertEqual({f.feature_id for f in result.assessments[0].required_features}, {'active', 'hidden'})
        rejected = [a for a in result.assessments if a.verdict == ExpressivityVerdict.INEXPRESSIBLE]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0].boundary.readable_features)
        self.assertIn('entry-boolean:', rejected[0].certificate)

    def test_missing_witness_cannot_certify_exclusion(self):
        contracts = self.spec.recover(task(), self.snapshot, self.ctx)
        empty = replace(contracts, witnesses=())
        result = ExpressivityLocalization(self.program, self.logic).locate(task(), empty, self.snapshot, self.ctx)
        self.assertTrue(all(a.verdict == ExpressivityVerdict.UNKNOWN for a in result.assessments))

    def test_synthesis_computes_patch_without_second_model_call(self):
        contracts = self.spec.recover(task(), self.snapshot, self.ctx)
        located = ExpressivityLocalization(self.program, self.logic).locate(task(), contracts, self.snapshot, self.ctx)
        result = ScopeSynthesis(self.model, self.program, self.logic).synthesize(task(), contracts, located, self.snapshot, self.ctx)
        self.assertIn('diff --git a/ui.js b/ui.js', result.patch.unified_diff)
        self.assertIn('hidden', result.patch.unified_diff)
        self.assertEqual(self.ctx.budget.model_calls, 1)
        self.assertEqual(result.unresolved, result.plan.unresolved)
        self.assertIn('active || hidden', (self.snapshot.root / 'ui.js').read_text())

    def test_conflicting_may_is_not_promoted_to_hard_obligation(self):
        contracts = self.spec.recover(task(), self.snapshot, self.ctx)
        original = contracts.must[0]
        contrary = replace(original, constraint_id='uncertain_contrary',
                           relation=Term('eq', (symbol('return'), literal(False))))
        contracts = replace(contracts, may=(contrary,))
        located = ExpressivityLocalization(self.program, self.logic).locate(task(), contracts, self.snapshot, self.ctx)
        result = ScopeSynthesis(self.model, self.program, self.logic).synthesize(task(), contracts, located, self.snapshot, self.ctx)
        self.assertNotIn(contrary, result.plan.obligations)
        self.assertIn(contrary, result.plan.soft_obligations)
        self.assertTrue(result.patch.unified_diff)
        self.assertEqual(self.ctx.budget.model_calls, 1)

    def test_rank_is_permutation_invariant(self):
        contracts = self.spec.recover(task(), self.snapshot, self.ctx)
        service = ExpressivityLocalization(self.program, self.logic)
        result = service.locate(task(), contracts, self.snapshot, self.ctx)
        other = service.rank_boundaries(tuple(reversed(result.assessments)), contracts, self.ctx)
        self.assertEqual(result, other)

if __name__ == '__main__':
    unittest.main()
