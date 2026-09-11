"""Cross-layer evidence binding tests using real TypeScript parsing and finite logic."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import aligned_evidence, config, context, parser_module, snapshot, task, SOURCE
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.repair import ExpressivityVerdict, HoleFilling
from boundary_repair.domain.specification import SolverStatus
from boundary_repair.kernel.evidence import evidence_request_v4, parse_evidence_v4, parse_evidence_v3
from boundary_repair.kernel.files import tree_digest
from boundary_repair.kernel.retrieval import scope_snippets
from boundary_repair.ports import ModelResponse


def claims(data):
    return data['observations'] + [claim for group in data['requirement_groups']
                                  for alternative in group['alternatives']
                                  for claim in alternative['all_of']] + data['frames']


class EntryModel:
    """Keep natural targets unaligned while supplying independently typed direct-entry examples."""
    def __init__(self, transform=None):
        self.transform = transform
        self.requests = []
        self.response = None

    def complete(self, request, ctx):
        self.requests.append(request)
        if request.schema_name != 'evidence.v4':
            raise AssertionError('unexpected second model request')
        ctx.budget.begin_model_call(request.max_output_tokens)
        prompt = json.loads(request.prompt)
        data = aligned_evidence(prompt)
        for claim in claims(data):
            claim['targets'] = [{'entity_id': 'reported_gate_behavior', 'property_name': 'required_behavior',
                                 'context': {'op': 'literal', 'value': 'the direct calls described in the issue'}}]
            claim['formalization'] = {'op': 'literal', 'value': claim['statement']}
        if self.transform:
            self.transform(data, prompt)
        self.response = data
        ctx.budget.record_output_tokens(1)
        return ModelResponse(json.dumps(data), 1, 'synthetic-entry-evidence')


@unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
class EvidenceAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = snapshot(self.root)
        self.program = ProgramAdapter(config(self.root))
        self.ctx = context()
        self.logic = LogicAdapter()
        self.localizer = ExpressivityLocalization(self.program, self.logic)

    def recover(self, transform=None):
        model = EntryModel(transform)
        contracts = SpecificationRecovery(model, self.program, self.logic).recover(task(), self.snap, self.ctx)
        return contracts, model

    def locate(self, contracts):
        return self.localizer.locate(task(), contracts, self.snap, self.ctx)

    def assert_unknown(self, contracts):
        result = self.locate(contracts)
        self.assertTrue(result.assessments)
        self.assertTrue(all(item.verdict == ExpressivityVerdict.UNKNOWN and item.certificate is None
                            for item in result.assessments))
        return result

    def test_natural_targets_remain_intact_but_bound_cases_change_decisions(self):
        contracts, model = self.recover()
        self.assertEqual(contracts.extraction_status, 'complete')
        self.assertEqual(len(contracts.witnesses), 4)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].schema_name, 'evidence.v4')
        for claim in contracts.must + contracts.frames:
            self.assertEqual(claim.targets[0].property_name, 'required_behavior')
            self.assertEqual(claim.targets[0].entity_id, 'reported_gate_behavior')
            self.assertEqual(claim.relation.value, claim.description)
            self.assertTrue(claim.entry_cases)
            self.assertEqual(len(claim.source_ids), 2)
        self.assertTrue(all(w.reachability == SolverStatus.UNKNOWN and w.interface for w in contracts.witnesses))
        result = self.locate(contracts)
        self.assertEqual(sum(a.verdict == ExpressivityVerdict.FEASIBLE for a in result.assessments), 1)
        self.assertEqual(sum(a.verdict == ExpressivityVerdict.INEXPRESSIBLE for a in result.assessments), 1)
        self.assertIn('entry_case_evidence_interpretation_not_verified', result.assessments[0].unresolved)
        unbound = replace(contracts, must=tuple(replace(c, entry_cases=()) for c in contracts.must),
                          frames=tuple(replace(c, entry_cases=()) for c in contracts.frames))
        self.assert_unknown(unbound)

    def test_string_formalization_without_bindings_is_partial_not_complete(self):
        def remove(data, prompt):
            for claim in claims(data):
                claim['entry_cases'] = []
        contracts, model = self.recover(remove)
        self.assertEqual(contracts.extraction_status, 'partial')
        self.assertEqual((len(contracts.must), len(contracts.frames)), (3, 1))
        self.assertFalse(contracts.witnesses)
        self.assertEqual(len(model.requests), 1)
        self.assert_unknown(contracts)

    def test_null_formalization_can_have_a_supported_separate_entry_case(self):
        def clear(data, prompt):
            for claim in claims(data):
                claim['formalization'] = None
        contracts, _ = self.recover(clear)
        self.assertEqual(self.locate(contracts).assessments[0].verdict, ExpressivityVerdict.FEASIBLE)
        self.assertTrue(all(c.relation.value.startswith('uninterpreted:') for c in contracts.must))

    def test_unbound_hard_requirement_is_retained_and_blocks_certification(self):
        def add(data, prompt):
            unsupported = deepcopy(claims(data)[0])
            unsupported.update(statement='Also keep the Canvas rendering unchanged.', entry_cases=[])
            data['requirement_groups'][0]['alternatives'][0]['all_of'].append(unsupported)
        contracts, _ = self.recover(add)
        self.assertEqual(len(contracts.must), 4)
        result = self.assert_unknown(contracts)
        self.assertTrue(any(reason.startswith('unbound_hard_constraint:')
                            for reason in result.assessments[0].unresolved))

    def test_missing_one_case_is_not_covered_by_another_case_of_the_same_claim(self):
        def combine(data, prompt):
            first, second = claims(data)[:2]
            first['entry_cases'].extend(second['entry_cases'])
            del data['requirement_groups'][1]
        contracts, _ = self.recover(combine)
        removed = contracts.witnesses[1]
        contracts = replace(contracts, witnesses=tuple(w for w in contracts.witnesses if w != removed))
        result = self.assert_unknown(contracts)
        self.assertTrue(any('uncovered_entry_case:' + removed.witness_id in a.unresolved for a in result.assessments))

    def test_may_cases_do_not_create_witnesses_or_exclusions(self):
        contracts, _ = self.recover()
        hard = contracts.must[0]
        contrary = replace(hard, constraint_id='uncertain',
                           entry_cases=(replace(hard.entry_cases[0], expected=False),))
        baseline = self.locate(contracts)
        self.assertEqual(self.locate(replace(contracts, may=(contrary,))), baseline)

    def test_interpretation_alternatives_keep_bound_claims_soft(self):
        def alternative(data, prompt):
            group = data['requirement_groups'][0]
            other = deepcopy(group['alternatives'][0])
            other['all_of'][0]['entry_cases'][0]['expected'] = False
            group['alternatives'].append(other)
        contracts, _ = self.recover(alternative)
        self.assertEqual(len(contracts.may), 2)
        may_ids = {c.constraint_id for c in contracts.may}
        self.assertFalse(any(w.witness_id.rsplit(':', 1)[0] in may_ids for w in contracts.witnesses))

    def test_same_named_functions_in_different_files_do_not_share_constraints(self):
        (self.snap.root / 'other.js').write_bytes(SOURCE.encode('utf-8'))
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        contracts, _ = self.recover()
        bound = contracts.must[0].entry_cases[0].interface
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        self.assertEqual(len({item.interface_id for item in directory}), 2)
        for assessment in self.locate(contracts).assessments:
            if assessment.boundary.sites[0] != bound.site:
                self.assertEqual(assessment.verdict, ExpressivityVerdict.UNKNOWN)

    def test_snapshot_change_cannot_reuse_an_old_binding(self):
        contracts, _ = self.recover()
        (self.snap.root / 'unrelated.css').write_text('div { color: blue; }', encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.assert_unknown(contracts)

    def test_changed_function_header_rejects_cached_semantics(self):
        contracts, _ = self.recover()
        file = self.snap.root / 'ui.js'
        file.write_bytes(file.read_bytes().replace(b'shouldShow', b'shouldHide'))
        with self.assertRaisesRegex(ValidationError, 'stale_source_hash'):
            self.locate(contracts)

    def test_legacy_aligned_text_does_not_silently_gain_a_program_binding(self):
        from helpers import structured_evidence
        bundle = parse_evidence_v3(json.dumps(structured_evidence()), task(), ())
        spec = SpecificationRecovery(None, self.program, self.logic)
        space = spec.build_interpretation_space(bundle, (), self.ctx)
        contracts = spec.derive_contracts(bundle, (), space, self.ctx)
        self.assertFalse(contracts.witnesses)
        self.assert_unknown(contracts)

    def test_synthesis_and_validation_consume_the_same_bound_cases(self):
        contracts, model = self.recover()
        result = ScopeSynthesis(model, self.program, self.logic).synthesize(
            task(), contracts, self.locate(contracts), self.snap, self.ctx)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result.plan.generation_mode, 'certified')
        with self.assertRaisesRegex(ValidationError, 'generated_boolean_obligation_violated'):
            self.program.materialize(task(), result.plan,
                                     (HoleFilling(result.plan.holes[0].hole_id, 'true'),), self.snap, self.ctx)
        candidate = self.root / 'candidate'
        candidate.mkdir()
        (candidate / 'ui.js').write_bytes((self.snap.root / 'ui.js').read_bytes())
        subprocess.run(['git', 'apply', '-'], input=result.patch.unified_diff.encode('utf-8'),
                       cwd=candidate, check=True, capture_output=True)
        code = 'const f=require(process.argv[1]).shouldShow; console.log(JSON.stringify([[true,false],[true,true],[false,false],[false,true]].map(v=>f(...v))));'
        output = subprocess.check_output(['node', '-e', code, str(candidate / 'ui.js')])
        self.assertEqual(json.loads(output), [True, False, False, False])
        self.assertEqual((self.snap.root / 'ui.js').read_bytes(), SOURCE.encode('utf-8'))

    def test_strict_schema_and_local_validation_reject_bad_entry_cases(self):
        import jsonschema
        contracts, model = self.recover()
        request = model.requests[0]
        schema = request.output_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(model.response, schema)
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        code = scope_snippets(self.program.source_scope(self.snap, self.ctx))
        modifications = {
            'unknown_id': lambda case: case.update(interface_id='entry:invented'),
            'missing_input': lambda case: case['inputs'].pop(),
            'extra_input': lambda case: case['inputs'].append({'parameter': 'invented', 'value': True}),
            'duplicate_input': lambda case: case['inputs'].append(case['inputs'][0]),
            'numeric_input': lambda case: case['inputs'][0].update(value=1),
            'string_input': lambda case: case['inputs'][0].update(value='true'),
            'null_input': lambda case: case['inputs'][0].update(value=None),
            'numeric_output': lambda case: case.update(expected=1),
            'proof_flag': lambda case: case.update(reachable=True),
            'fake_projection': lambda case: case.update(property_name='visibility'),
        }
        for name, modify in modifications.items():
            with self.subTest(name=name):
                data = deepcopy(model.response)
                modify(claims(data)[0]['entry_cases'][0])
                with self.assertRaises(ValidationError):
                    parse_evidence_v4(json.dumps(data), task(), code, directory)

    def test_invalid_binding_does_not_trigger_a_second_evidence_request(self):
        def invalid(data, prompt):
            claims(data)[0]['entry_cases'][0]['interface_id'] = 'not-declared'
        contracts, model = self.recover(invalid)
        self.assertEqual(contracts.extraction_status, 'unavailable')
        self.assertEqual(len(model.requests), 1)
        self.assert_unknown(contracts)

    def test_code_and_issue_evidence_are_both_required_for_entry_cases(self):
        contracts, model = self.recover()
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        code = scope_snippets(self.program.source_scope(self.snap, self.ctx))
        for index in (0, 1):
            with self.subTest(keep_reference=index):
                data = deepcopy(model.response)
                claim = claims(data)[0]
                claim['evidence_refs'] = [claim['evidence_refs'][index]]
                with self.assertRaises(ValidationError):
                    parse_evidence_v4(json.dumps(data), task(), code, directory)

    def test_observations_cannot_supply_desired_output_cases(self):
        contracts, model = self.recover()
        data = deepcopy(model.response)
        data['observations'].append(deepcopy(claims(data)[0]))
        with self.assertRaisesRegex(ValidationError, 'observations_cannot_constrain'):
            parse_evidence_v4(json.dumps(data), task(), scope_snippets(self.program.source_scope(self.snap, self.ctx)),
                              self.program.observation_interfaces(self.snap, self.ctx))

    def test_input_order_is_canonical_not_a_false_distinction(self):
        def duplicate(data, prompt):
            first = claims(data)[0]
            other = deepcopy(first['entry_cases'][0])
            other['inputs'].reverse()
            other['expected'] = not other['expected']
            first['entry_cases'].append(other)
        contracts, _ = self.recover(duplicate)
        verdicts = [a.verdict for a in self.locate(contracts).assessments
                    if a.boundary.sites[0].node_kind == 'BooleanReturn']
        self.assertEqual(verdicts, [ExpressivityVerdict.INEXPRESSIBLE] * 2)

    def test_all_two_input_truth_tables_have_correct_interface_verdicts(self):
        from itertools import product
        inputs = tuple(product((False, True), repeat=2))
        for outputs in product((False, True), repeat=4):
            with self.subTest(outputs=outputs):
                self.ctx = context()
                table = dict(zip(inputs, outputs))
                def assign(data, prompt):
                    for claim in claims(data):
                        case = claim['entry_cases'][0]
                        values = {item['parameter']: item['value'] for item in case['inputs']}
                        case['expected'] = table[(values['active'], values['hidden'])]
                contracts, _ = self.recover(assign)
                assessments = [a for a in self.locate(contracts).assessments
                               if a.boundary.sites[0].node_kind == 'BooleanReturn']
                full = next(a for a in assessments if len(a.boundary.readable_features) == 2)
                constant = next(a for a in assessments if not a.boundary.readable_features)
                self.assertEqual(full.verdict, ExpressivityVerdict.FEASIBLE)
                self.assertEqual(constant.verdict, ExpressivityVerdict.FEASIBLE if len(set(outputs)) == 1
                                 else ExpressivityVerdict.INEXPRESSIBLE)
                required = {name for position, name in enumerate(('active', 'hidden'))
                            if any(table[row] != table[tuple(not value if index == position else value
                                                           for index, value in enumerate(row))]
                                   for row in inputs)}
                self.assertEqual({feature.feature_id for feature in full.required_features}, required)

    def test_same_named_functions_in_one_file_keep_exact_emission_identity(self):
        source = ('const first = function gate(active, hidden) { return active || hidden; };\n'
                  'const second = function gate(active, hidden) { return active || hidden; };\n')
        (self.snap.root / 'ui.js').write_text(source, encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        scope = self.program.source_scope(self.snap, self.ctx)
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        self.assertEqual(len(directory), 2)
        self.assertEqual(len({item.interface_id for item in directory}), 2)
        from boundary_repair.domain.repair import PatchPlan, SyntaxHole, EditKind
        site = directory[1].site
        plan = PatchPlan('same-name', (), EditKind.REFINE_GUARD,
                         (SyntaxHole('h', site, 'boolean-expression', ('active', 'hidden')),), (), ())
        patch = self.program.materialize(task(), plan, (HoleFilling('h', 'active && !hidden'),), self.snap, self.ctx)
        self.assertIn('+const second = function gate', patch.unified_diff)
        self.assertNotIn('-const first', patch.unified_diff)

    def test_invisible_function_header_is_not_an_available_observation(self):
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        self.assertEqual(len(directory), 1)
        site = directory[0].site
        scope = self.program.source_scope(self.snap, self.ctx)
        region = scope.regions[0]
        self.program._scope = replace(scope, regions=(replace(region, start_byte=site.start_byte),))
        self.assertEqual(self.program.observation_interfaces(self.snap, self.ctx), ())

    def test_declared_non_boolean_types_do_not_get_a_boolean_entry_interface(self):
        (self.snap.root / 'ui.js').unlink()
        source = ('function stringInput(x: string) { return x; }\n'
                  'function numberInput(x: number) { return !x; }\n'
                  'function stringOutput(x: boolean): string { return x; }\n'
                  'function booleanInput(x: boolean): boolean { return !x; }\n')
        (self.snap.root / 'typed.ts').write_text(source, encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        directory = self.program.observation_interfaces(self.snap, self.ctx)
        self.assertEqual([item.site.symbol for item in directory], ['booleanInput'])

    def test_unsupported_program_and_text_parser_have_no_observation_interfaces(self):
        unsupported = 'async function render(x) { return x; }\nfunction read(x) { return window.hidden || x; }\nfunction numeric(x) { return x + 1; }\n'
        (self.snap.root / 'ui.js').write_text(unsupported, encoding='utf-8')
        self.snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.assertEqual(self.program.observation_interfaces(self.snap, self.ctx), ())
        text_program = ProgramAdapter(config(self.root, parser=False))
        self.assertEqual(text_program.observation_interfaces(self.snap, self.ctx), ())
        request = evidence_request_v4(task(), (), 8000, ())
        self.assertEqual(request.output_schema['properties']['frames']['items']['properties']['entry_cases']['maxItems'], 0)


if __name__ == '__main__':
    unittest.main()
