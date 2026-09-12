"""Source-grounded mechanism checks with explicitly scripted evidence and no provider access."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, task
from test_scoped_alignment import target_source
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.controls import PlainControls
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.repair import ExpressivityVerdict
from boundary_repair.kernel.evidence import evidence_request_v6, parse_evidence_v6, parse_evidence_v4
from boundary_repair.kernel.files import tree_digest
from boundary_repair.kernel.retrieval import scope_snippets
from boundary_repair.ports import ModelResponse


SOURCE = '''function card(active, hidden) {
  const enabled = active || hidden;
  return <div title={active ? "shown" : "hidden"}>
    {enabled ? <strong /> : null}
  </div>;
}
'''
ISSUE = ('For card, construct strong exactly when active=true and hidden=false. '
         'Preserve title: shown when active=true and hidden otherwise. '
         'Retain the unsupported remote behavior described separately.')


class CatalogueModel:
    """Encode the fixture issue's declared truth table, then script a single scoped transaction."""
    def __init__(self, partial=False, transform=None, bad_frame=False, bad_header=None):
        self.partial, self.transform, self.bad_frame = partial, transform, bad_frame
        self.requests = []
        self.bad_header = bad_header

    def complete(self, request, ctx):
        self.requests.append(request)
        ctx.budget.begin_model_call(request.max_output_tokens)
        ctx.budget.record_output_tokens(1)
        payload = json.loads(request.prompt)
        if request.schema_name == 'evidence.v6':
            interfaces = {i['interface_id']: i for i in payload['observation_interfaces']}
            refs = [{'span_id': next(r['span_id'] for r in payload['evidence_catalog']['spans'] if r['kind'] == kind)}
                    for kind in ('issue_text', 'base_code')]
            def claim(property_name, frame=False):
                cases = []
                for scenario in payload['scenario_catalog']:
                    interface = interfaces[scenario['interface_id']]
                    if interface['property_name'] != property_name:
                        continue
                    values = scenario['inputs']
                    expected = ('shown' if values['active'] else 'hidden') if frame else bool(values['active'] and not values['hidden'])
                    cases.append({'scenario_id': scenario['scenario_id'], 'expected': expected})
                return {'statement': 'Preserve title.' if frame else 'Strong follows active and not hidden.',
                        'formalization': None, 'targets': [], 'evidence_refs': refs,
                        'binding_alternatives': [{'cases': cases}]}
            data = {'observations': [], 'requirement_groups': [{'alternatives': [{'all_of': [claim('jsx.children.contains:strong')]}]}],
                    'frames': [claim('jsx.attribute:title', True)]}
            if self.partial:
                data['requirement_groups'].append({'alternatives': [{'all_of': [{
                    'statement': 'Retain unsupported remote behavior.', 'formalization': None, 'targets': [],
                    'evidence_refs': refs[:1], 'binding_alternatives': []}]}]})
            if self.transform:
                self.transform(data, payload)
        else:
            if request.schema_name != 'edits.v5':
                raise AssertionError('unexpected model call')
            block = next(b for b in payload['blocks'] if b['node_kind'] == 'FunctionDeclaration')
            old = target_source(payload, block['block_id'])
            new = old.replace('active || hidden', 'active && !hidden')
            if self.bad_frame:
                new = new.replace('"shown"', '"wrong"')
            if self.bad_header == 'rename':
                new = new.replace('function card(', 'function another(')
            if self.bad_header == 'parameters':
                new = new.replace('function card(active, hidden)', 'function card(hidden, active)')
            data = {'edits': [{'operation': 'replace_block', 'target': block['block_id'], 'old_text': '',
                               'new_text': new, 'destination': ''}]}
        return ModelResponse(json.dumps(data), 1, 'scripted-mechanism-fixture')


@unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
class MechanismActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        snap = snapshot(self.root)
        (snap.root / 'ui.js').write_text(SOURCE, encoding='utf-8')
        self.snap = replace(snap, tree_sha256=tree_digest(snap.root))
        self.task = replace(task(), problem_statement=ISSUE)
        self.program = ProgramAdapter(config(self.root))
        self.logic = LogicAdapter()
        self.ctx = context()

    def recover(self, model=None):
        model = model or CatalogueModel()
        contracts = SpecificationRecovery(model, self.program, self.logic).recover(self.task, self.snap, self.ctx)
        return model, contracts

    def request(self):
        code = scope_snippets(self.program.source_scope(self.snap, self.ctx, self.task.problem_statement))
        interfaces = self.program.observation_interfaces(self.snap, self.ctx)
        scenarios = self.program.observation_scenarios(self.snap, self.ctx)
        request = evidence_request_v6(self.task, code, 8000, interfaces, scenarios)
        return request, code, interfaces, scenarios

    def test_catalogue_contains_inputs_but_never_desired_outputs(self):
        request, _, _, scenarios = self.request()
        rows = json.loads(request.prompt)['scenario_catalog']
        self.assertTrue(rows)
        self.assertTrue(all('expected' not in row and 'output' not in row for row in rows))
        self.assertEqual(scenarios, self.program.observation_scenarios(self.snap, self.ctx))
        self.assertTrue(all(row['provenance'] == 'declared_entry_source_literal_domain' for row in rows))

    def test_new_protocol_binds_without_model_authored_inputs(self):
        model, contracts = self.recover()
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(contracts.extraction_status, 'complete', contracts.diagnostics)
        self.assertEqual(len(contracts.witnesses), 8)
        self.assertTrue(any(binding.entity_id.startswith('claim:') for binding in contracts.bindings))
        self.assertTrue(all(c.binding_status == 'program_bound' for c in contracts.must + contracts.frames))

    def test_numeric_scenarios_follow_the_declared_javascript_number_sort(self):
        (self.snap.root / 'ui.js').write_text(
            'function label(width) { if (width < 2) return "small"; return "wide"; }', encoding='utf-8')
        snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        scenarios = self.program.observation_scenarios(snap, self.ctx)
        self.assertTrue(scenarios)
        self.assertTrue(all(type(dict(s.inputs)['width']) is float for s in scenarios))
        self.assertTrue(all(dict(s.inputs)['width'] == 2.0 for s in scenarios))

    def test_response_and_schema_agree_on_program_context_ids(self):
        from jsonschema import Draft202012Validator
        request, code, interfaces, scenarios = self.request()
        raw = CatalogueModel().complete(request, context()).text
        Draft202012Validator(request.output_schema).validate(json.loads(raw))
        self.assertTrue(parse_evidence_v6(raw, self.task, code, interfaces, scenarios).claims)
        data = json.loads(raw)
        data['frames'][0]['binding_alternatives'][0]['cases'][0]['scenario_id'] = 'invented'
        with self.assertRaises(ValidationError):
            parse_evidence_v6(json.dumps(data), self.task, code, interfaces, scenarios)

    def test_missing_code_provenance_cannot_gain_a_binding(self):
        def remove(data, payload):
            for claim in [data['requirement_groups'][0]['alternatives'][0]['all_of'][0], data['frames'][0]]:
                claim['evidence_refs'] = claim['evidence_refs'][:1]
        _, contracts = self.recover(CatalogueModel(transform=remove))
        self.assertEqual(contracts.extraction_status, 'unavailable')

    def test_empty_binding_alternative_prevents_unconditional_witnesses(self):
        def uncertain(data, payload):
            data['frames'] = []
            data['requirement_groups'][0]['alternatives'][0]['all_of'][0]['binding_alternatives'].append({'cases': []})
        _, contracts = self.recover(CatalogueModel(transform=uncertain))
        self.assertEqual(len(contracts.must), 1)
        self.assertEqual(contracts.must[0].binding_status, 'ambiguous')
        self.assertFalse(contracts.witnesses)
        self.assertTrue(contracts.must[0].binding_alternatives)

    def test_shared_requirement_is_must_across_distinct_interpretations(self):
        def equivalent(data, payload):
            group = data['requirement_groups'][0]
            group['alternatives'].append(deepcopy(group['alternatives'][0]))
        _, contracts = self.recover(CatalogueModel(transform=equivalent))
        self.assertEqual(len(contracts.must), 1)
        self.assertEqual(len(contracts.witnesses), 8)

    def test_partial_requirement_does_not_erase_local_certificates(self):
        _, contracts = self.recover(CatalogueModel(partial=True))
        result = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        proved = [a for a in result.assessments if a.verdict == ExpressivityVerdict.FEASIBLE]
        self.assertTrue(proved)
        self.assertTrue(all(a.construction and a.proof and a.covered_obligations for a in proved))
        self.assertTrue(any(not c.entry_cases for c in contracts.must))

    def test_partial_plan_enforces_properties_with_one_generation(self):
        model, contracts = self.recover(CatalogueModel(partial=True))
        localization = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        result = ScopeSynthesis(model, self.program, self.logic).synthesize(self.task, contracts, localization, self.snap, self.ctx)
        self.assertEqual(result.plan.generation_mode, 'guided_partial')
        self.assertTrue(result.plan.enforced_obligations)
        self.assertEqual([r.schema_name for r in model.requests], ['evidence.v6', 'edits.v5'])
        payload = json.loads(model.requests[-1].prompt)
        self.assertEqual(set(payload['bound_property_checks']['obligation_ids']), set(result.plan.enforced_obligations))
        self.assertTrue(any(not c.entry_cases for c in result.plan.obligations))
        self.assertEqual(result.patch.application_check, 'passed')
        self.assertEqual((self.snap.root / 'ui.js').read_text(encoding='utf-8'), SOURCE)

    def test_final_transaction_cannot_break_a_protected_property(self):
        model, contracts = self.recover(CatalogueModel(partial=True, bad_frame=True))
        localization = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        with self.assertRaisesRegex(ValidationError, 'bound_property_transaction_not_verified'):
            ScopeSynthesis(model, self.program, self.logic).synthesize(self.task, contracts, localization, self.snap, self.ctx)
        self.assertEqual(len(model.requests), 2)

    def test_scope_synthesis_operates_when_localization_is_disabled(self):
        model, contracts = self.recover()
        localization = PlainControls(model, self.program).locate(self.task, contracts, self.snap, self.ctx)
        self.assertTrue(all(a.verdict == ExpressivityVerdict.UNKNOWN and not a.certificate for a in localization.assessments))
        result = ScopeSynthesis(model, self.program, self.logic).synthesize(self.task, contracts, localization, self.snap, self.ctx)
        self.assertEqual(result.plan.generation_mode, 'certified_projection')
        self.assertIsNotNone(result.plan.semantic_cost)
        self.assertEqual(len(model.requests), 1)

    def assert_header_change_rejected(self, change):
        model, contracts = self.recover(CatalogueModel(partial=True, bad_header=change))
        localization = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        with self.assertRaisesRegex(ValidationError, 'bound_property_transaction_not_verified'):
            ScopeSynthesis(model, self.program, self.logic).synthesize(self.task, contracts, localization, self.snap, self.ctx)
        self.assertEqual(len(model.requests), 2)

    def test_renamed_consumer_invalidates_the_original_binding(self):
        self.assert_header_change_rejected('rename')

    def test_changed_argument_order_invalidates_the_original_binding(self):
        self.assert_header_change_rejected('parameters')

    def test_direct_specification_control_preserves_shared_binding_inputs(self):
        model = CatalogueModel()
        contracts = PlainControls(model, self.program).recover(self.task, self.snap, self.ctx)
        self.assertEqual(contracts.specification_policy, 'first_sourced_interpretation')
        self.assertTrue(contracts.must and contracts.frames and contracts.witnesses)
        self.assertEqual(contracts.theory.consistency.value, 'unknown')
        localization = ExpressivityLocalization(self.program, self.logic).locate(self.task, contracts, self.snap, self.ctx)
        self.assertTrue(any(a.certificate for a in localization.assessments))

    def test_irrelevant_inert_declaration_does_not_destroy_local_summary(self):
        source = SOURCE.replace('const enabled', 'let unused = {value: 3};\n  const enabled')
        (self.snap.root / 'ui.js').write_text(source, encoding='utf-8')
        snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.program.source_scope(snap, self.ctx, ISSUE)
        self.assertTrue(self.program.observation_interfaces(snap, self.ctx))

    def test_possible_effects_remain_unknown(self):
        (self.snap.root / 'ui.js').write_text(SOURCE.replace('const enabled', 'mutateState();\n  const enabled'), encoding='utf-8')
        snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.program.source_scope(snap, self.ctx, ISSUE)
        self.assertFalse(self.program.observation_interfaces(snap, self.ctx))
        self.assertTrue(any('local_model_effect_or_call' in x for x in self.program.index(snap, self.ctx).unsupported_constructs))

    def test_short_circuit_jsx_uses_explicit_boolean_guard(self):
        (self.snap.root / 'ui.js').write_text(SOURCE.replace('enabled ? <strong /> : null', 'enabled && <strong />'), encoding='utf-8')
        snap = replace(self.snap, tree_sha256=tree_digest(self.snap.root))
        self.program.source_scope(snap, self.ctx, ISSUE)
        self.assertTrue(any(i.property_name == 'jsx.children.contains:strong' for i in self.program.observation_interfaces(snap, self.ctx)))

    def test_old_unbound_response_is_not_silently_enriched(self):
        _, code, interfaces, _ = self.request()
        raw = {'observations': [], 'frames': [], 'requirement_groups': [{'alternatives': [{'all_of': [{
            'statement': 'Retain unsupported remote behavior.', 'formalization': None, 'targets': [],
            'evidence_refs': [{'span_id': 'issue:0001'}], 'entry_cases': []}]}]}]}
        from boundary_repair.kernel.evidence import evidence_catalog_v3
        raw['requirement_groups'][0]['alternatives'][0]['all_of'][0]['evidence_refs'][0]['span_id'] = evidence_catalog_v3(self.task, code)['spans'][0]['span_id']
        evidence = parse_evidence_v4(json.dumps(raw), self.task, code, interfaces)
        self.assertFalse(evidence.claims[0].entry_cases or evidence.claims[0].binding_alternatives)
