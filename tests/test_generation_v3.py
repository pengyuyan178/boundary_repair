"""Generation-mode and transaction-schema checks independent of model repair quality."""
from dataclasses import replace
from itertools import product
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, structured_evidence, aligned_evidence, task
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.rendering import edit_transaction_schema, parse_transaction, transaction_plan
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.kernel.boolean import synthesize_boolean
from boundary_repair.kernel.evidence import parse_evidence_v3
from boundary_repair.kernel.terms import evaluate


class GenerationV3Tests(unittest.TestCase):
    def test_certified_grammar_covers_every_two_variable_truth_table(self):
        inputs = [dict(zip(('a', 'b'), values)) for values in product((False, True), repeat=2)]
        for outputs in product((False, True), repeat=4):
            with self.subTest(outputs=outputs):
                expression = synthesize_boolean(('a', 'b'), tuple(zip(inputs, outputs)), context())
                self.assertIsNotNone(expression)
                self.assertEqual(tuple(evaluate(expression.term, values) for values in inputs), outputs)

    def test_transaction_parser_rejects_partial_or_extra_fields(self):
        good = {'operation': 'replace_text', 'target': 'R000', 'old_text': 'old\n',
                'new_text': 'new\n', 'destination': ''}
        self.assertEqual(len(parse_transaction(json.dumps({'edits': [good]})).edits), 1)
        for bad in ({**good, 'first_line': True}, {**good, 'extra': 'discard this'}, {**good, 'operation': 'run_shell'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                parse_transaction(json.dumps({'edits': [good, bad]}))

    def test_schema_accepts_the_same_closed_transaction(self):
        from jsonschema import Draft202012Validator
        schema = edit_transaction_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate({'edits': [{'operation': 'insert_after', 'target': 'B000',
            'new_text': 'const enabled = true;\n', 'old_text': '', 'destination': ''}]})

    def test_live_schema_binds_ids_lines_and_operation_arguments(self):
        from jsonschema import Draft202012Validator
        from boundary_repair.algorithms.rendering import TransactionRenderer
        with TemporaryDirectory() as raw:
            root = Path(raw)
            snap, ctx = snapshot(root), context()
            program = ProgramAdapter(config(root, parser=False))
            scope = program.source_scope(snap, ctx, task().problem_statement)
            region = scope.regions[0]
            good = {'operation': 'replace_text', 'target': region.region_id,
                    'old_text': region.source, 'new_text': 'const enabled = true;\n', 'destination': ''}
            model = Mock()
            model.complete.return_value.text = json.dumps({'edits': [good]})
            from boundary_repair.domain.repair import EditKind, PatchPlan
            plan = PatchPlan('schema', (), EditKind.FREEFORM, (), (), (), edit_scope=scope)
            TransactionRenderer(model).render(task(), plan, ctx)
            request = model.complete.call_args.args[0]
            schema = request.output_schema
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            validator.validate({'edits': [good]})
            for bad in ({**good, 'target': region.path}, {**good, 'target': region.file_id},
                        {**good, 'first_line': None}, {**good, 'last_line': region.start_line},
                        {**good, 'operation': 'insert_at'}, {**good, 'operation': 'replace_lines'},
                        {**good, 'operation': 'replace_block'}, {**good, 'destination': 'other.js'}):
                with self.subTest(bad=bad):
                    self.assertFalse(validator.is_valid({'edits': [bad]}))
            self.assertEqual(request.schema_name, 'edits.v5')
            self.assertEqual(model.complete.call_count, 1)

    def test_scope_schema_limits_complete_file_operations_and_creation_paths(self):
        from jsonschema import Draft202012Validator
        with TemporaryDirectory() as raw:
            root = Path(raw)
            snap, ctx = snapshot(root), context()
            scope = ProgramAdapter(config(root, parser=False)).source_scope(snap, ctx, '')
            validator = Draft202012Validator(edit_transaction_schema(scope))
            create = {'operation': 'create_file', 'target': 'new.js', 'new_text': 'export const x=1;',
                      'old_text': '', 'destination': ''}
            validator.validate({'edits': [create]})
            for target in ('elsewhere/new.js', 'elsewhere\\new.js'):
                self.assertFalse(validator.is_valid({'edits': [{**create, 'target': target}]}))
            delete = {**create, 'operation': 'delete_file', 'target': scope.files[0].file_id, 'new_text': ''}
            validator.validate({'edits': [delete]})
            partial = replace(scope, files=tuple(replace(file, complete=False) for file in scope.files))
            self.assertFalse(Draft202012Validator(edit_transaction_schema(partial)).is_valid({'edits': [delete]}))
            self.assertFalse(validator.is_valid({'edits': [{**delete, 'new_text': 'discarded?'}]}))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_certified_failure_never_calls_a_second_generator(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            snap, ctx = snapshot(root), context()
            program, logic, model = ProgramAdapter(config(root)), LogicAdapter(), Mock()
            model.complete.side_effect = lambda request, ctx: Mock(
                text=json.dumps(aligned_evidence(json.loads(request.prompt))))
            contracts = SpecificationRecovery(model, program, logic).recover(task(), snap, ctx)
            localized = ExpressivityLocalization(program, logic).locate(task(), contracts, snap, ctx)
            with patch('boundary_repair.algorithms.synthesis.synthesize_boolean', return_value=None):
                with self.assertRaisesRegex(NoAdmissiblePatch, 'certified_boolean_generation_failed'):
                    ScopeSynthesis(model, program, logic).synthesize(task(), contracts, localized, snap, ctx)
            self.assertEqual(model.complete.call_count, 1)
            frozen = config(root).results_root / ctx.run_id / 'cases' / ctx.instance_id / 'trajectory/generation_plan.json'
            self.assertEqual(json.loads(frozen.read_text(encoding='utf-8'))['generation_mode'], 'certified')

    def test_groups_and_citations_survive_generic_plan(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            snap, ctx = snapshot(root), context()
            program, logic = ProgramAdapter(config(root, parser=False)), LogicAdapter()
            evidence = parse_evidence_v3(json.dumps(structured_evidence()), task(), ())
            recovery = SpecificationRecovery(Mock(), program, logic)
            space = recovery.build_interpretation_space(evidence, (), ctx)
            contracts = recovery.derive_contracts(evidence, (), space, ctx)
            scope = program.source_scope(snap, ctx, task().problem_statement)
            plan = transaction_plan(contracts, scope, ctx, 'test')
            self.assertEqual(plan.interpretation_groups, evidence.interpretation_groups)
            self.assertEqual(plan.evidence_sources, evidence.sources)
            raw_plan = transaction_plan(replace(contracts, extraction_status='unavailable'), scope, ctx, 'raw')
            self.assertEqual(raw_plan.generation_mode, 'raw_evidence')


if __name__ == '__main__':
    unittest.main()
