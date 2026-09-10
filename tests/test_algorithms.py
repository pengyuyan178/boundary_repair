"""Actual finite algorithms and real compiler parsing, not method-call mocks."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, evidence, parser_module, snapshot, task, write_fixture
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.model import FrozenModelAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.errors import EvidenceConflict, ValidationError
from boundary_repair.domain.specification import Coverage, InterpretationSpace, SolverStatus, Term
from boundary_repair.domain.repair import ExpressivityVerdict
from boundary_repair.kernel.evidence import parse_evidence
from boundary_repair.kernel.terms import literal, symbol
from boundary_repair.kernel.files import source_slice


class EvidenceTests(unittest.TestCase):
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
