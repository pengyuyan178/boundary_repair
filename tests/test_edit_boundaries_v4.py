"""Synthetic program-owned boundaries and exact-text transactions; no benchmark inputs."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, task
from boundary_repair.adapters.frontend import analyze_sources
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.adapters.repository import SourceRepository, PatchCompiler, bind_edit_blocks, transaction_contents
from boundary_repair.algorithms.rendering import TransactionRenderer, edit_transaction_schema, parse_transaction
from boundary_repair.domain.errors import ExternalServiceError, NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import EditKind, EditTransaction, PatchPlan, SourceEdit
from boundary_repair.domain.task import RepositorySnapshot
from boundary_repair.kernel.files import tree_digest


METHOD = '  render(value) {\n    if (value) { return {label: "red"}; }\n    return {label: "none"};\n  }\n'
SOURCE = '// 中文 🐈\nclass View {\n' + METHOD + '  next() { return "untouched"; }\n}\n'


class EditBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = context()

    def prepare(self, files, parser=True, **settings):
        root = self.root / 'source'
        root.mkdir()
        for path, content in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)
        self.snap = RepositorySnapshot(root, 'a' * 40, tree_digest(root))
        cfg = config(self.root, parser=parser)
        self.cfg = replace(cfg, integration=replace(cfg.integration, **settings))
        self.program = ProgramAdapter(self.cfg)
        self.scope = self.program.source_scope(self.snap, self.ctx, 'render label blue')
        return self.scope

    def block(self, kind='MethodDeclaration', symbol='render'):
        return next(b for b in self.scope.blocks if b.node_kind == kind and b.symbol == symbol)

    def apply(self, *edits):
        return transaction_contents(self.snap, self.scope, EditTransaction(tuple(edits)))[1]

    def compile(self, *edits):
        plan = PatchPlan('boundaries', (), EditKind.FREEFORM, (), (), (), edit_scope=self.scope)
        return self.program.compile(task(), plan, EditTransaction(tuple(edits)), self.snap, self.ctx)

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_complete_method_replacement_preserves_closing_brace_and_neighbor(self):
        self.prepare({'view.js': SOURCE})
        block = self.block()
        original = SOURCE.encode()[block.start_byte:block.end_byte].decode()
        self.assertEqual(original, METHOD)
        edit = SourceEdit('replace_block', block.block_id, METHOD.replace('red', 'blue').rstrip('\n'))
        updated = self.apply(edit)['view.js']
        self.assertEqual(updated.decode(), SOURCE.replace('red', 'blue'))
        artifact = self.compile(edit)
        self.assertEqual((artifact.application_check, artifact.syntax_check), ('passed', 'passed'))
        subprocess.run(['node', '--check'], input=updated, capture_output=True, check=True)
        self.assertEqual((self.snap.root / 'view.js').read_bytes(), SOURCE.encode())

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_unicode_crlf_utf16_bom_and_no_final_newline(self):
        for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding), TemporaryDirectory() as raw:
                root = Path(raw)
                original = ('﻿' + SOURCE.rstrip('\n')).replace('\n', '\r\n').encode(encoding)
                (root / 'view.js').write_bytes(original)
                snap = RepositorySnapshot(root, 'a' * 40, tree_digest(root))
                cfg, ctx = config(self.root), context()
                program = ProgramAdapter(cfg)
                scope = program.source_scope(snap, ctx, 'render')
                block = next(b for b in scope.blocks if b.symbol == 'render' and b.node_kind == 'MethodDeclaration')
                edit = SourceEdit('replace_block', block.block_id, METHOD.replace('red', 'blue'))
                updated = transaction_contents(snap, scope, EditTransaction((edit,)))[1]['view.js']
                self.assertEqual(updated, original.decode(encoding).replace('red', 'blue').encode(encoding))
                plan = PatchPlan('encoded', (), EditKind.FREEFORM, (), (), (), edit_scope=scope)
                self.assertEqual(program.compile(task(), plan, EditTransaction((edit,)), snap, ctx).application_check, 'passed')

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_inline_method_keeps_object_comma_and_neighbor(self):
        source = 'const obj = {render() { return "red"; }, next() { return 2; }};'
        self.prepare({'view.js': source})
        block = self.block()
        self.assertEqual(source.encode()[block.start_byte:block.end_byte], b'render() { return "red"; }')
        edit = SourceEdit('replace_block', block.block_id, 'render() { return "blue"; }')
        self.assertEqual(self.apply(edit)['view.js'].decode(), source.replace('red', 'blue'))
        self.assertEqual(self.compile(edit).syntax_check, 'passed')

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_block_catalog_contains_declarations_and_statements(self):
        self.prepare({'view.ts': 'import x from "x";\nfunction run() { return x; }\n'
                      'class C { constructor() {} get value() { return 1; } set value(x) {} }\n'})
        kinds = {b.node_kind for b in self.scope.blocks}
        self.assertTrue({'ImportDeclaration', 'FunctionDeclaration', 'ClassDeclaration', 'Constructor',
                         'GetAccessor', 'SetAccessor', 'ReturnStatement'} <= kinds)

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_insert_before_and_after_use_program_anchors(self):
        self.prepare({'view.js': SOURCE})
        block = self.block()
        edits = (SourceEdit('insert_before', block.block_id, '  first() { return 1; }\n'),
                 SourceEdit('insert_after', block.block_id, '  last() { return 2; }\n'))
        expected = SOURCE.replace(METHOD, edits[0].new_text + METHOD + edits[1].new_text)
        self.assertEqual(self.apply(*edits)['view.js'].decode(), expected)
        self.assertEqual(self.compile(*edits).syntax_check, 'passed')

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_overlapping_parent_child_and_duplicate_insertion_are_rejected(self):
        self.prepare({'view.js': SOURCE})
        parent, child = self.block('ClassDeclaration', 'View'), self.block()
        cases = ((SourceEdit('replace_block', parent.block_id, 'class View {}'),
                  SourceEdit('replace_block', child.block_id, METHOD.replace('red', 'blue'))),
                 (SourceEdit('insert_before', child.block_id, 'x'), SourceEdit('insert_before', child.block_id, 'y')))
        for edits in cases:
            with self.subTest(edits=edits), self.assertRaisesRegex(ValidationError, 'overlapping_transaction_edits'):
                self.apply(*edits)
        self.assertEqual(tree_digest(self.snap.root), self.snap.tree_sha256)

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_no_text_or_coordinate_escape_from_syntax_scope(self):
        self.prepare({'view.js': SOURCE})
        region, block = self.scope.regions[0], self.block()
        for edit, error in ((SourceEdit('replace_text', region.region_id, 'blue', old_text='red'), 'text_edit_not_declared'),
                            (SourceEdit('replace_block', 'unknown', METHOD), 'unknown_edit_block'),
                            (SourceEdit('replace_block', block.block_id, METHOD, first_line=1), 'unexpected_edit_coordinates')):
            with self.subTest(error=error), self.assertRaisesRegex(ValidationError, error):
                self.apply(edit)

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_block_hash_and_original_file_are_rechecked(self):
        self.prepare({'view.js': SOURCE})
        self.scope = replace(self.scope, blocks=tuple(replace(b, sha256='0' * 64) for b in self.scope.blocks))
        with self.assertRaisesRegex(ValidationError, 'stale_edit_block_hash'):
            self.apply(SourceEdit('replace_block', self.block().block_id, METHOD))
        (self.snap.root / 'view.js').write_text(SOURCE + '// changed', encoding='utf-8')
        with self.assertRaisesRegex(ValidationError, 'stale_source_hash'):
            self.apply(SourceEdit('replace_block', self.block().block_id, METHOD))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_partial_window_excludes_enclosing_nodes(self):
        self.prepare({'view.js': SOURCE})
        region = self.scope.regions[0]
        start = SOURCE.index('    if')
        text = '    if (value) { return {label: "red"}; }\n'
        start_byte = len(SOURCE[:start].encode())
        partial = replace(region, source=text, start_byte=start_byte, end_byte=start_byte + len(text.encode()),
                          start_char=start, start_line=SOURCE[:start].count('\n') + 1,
                          sha256=hashlib.sha256(text.encode()).hexdigest(), edit_mode='text')
        raw_scope = replace(self.scope, regions=(partial,), blocks=(),
                            files=tuple(replace(f, complete=False) for f in self.scope.files))
        bound = bind_edit_blocks(self.snap, raw_scope, self.program._data, 'render')
        self.assertTrue(bound.blocks)
        self.assertTrue(all(partial.start_byte <= b.start_byte < b.end_byte <= partial.end_byte for b in bound.blocks))
        self.assertFalse(any(b.node_kind in {'MethodDeclaration', 'ClassDeclaration'} for b in bound.blocks))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_catalog_is_bounded_deterministic_and_parser_cached(self):
        self.prepare({'view.js': '\n'.join(f'function f{i}() {{ return {i}; }}' for i in range(180))})
        self.assertEqual(len(self.scope.blocks), 128)
        duplicate = ProgramAdapter(self.cfg).source_scope(self.snap, context(), 'render label blue')
        self.assertEqual(self.scope, duplicate)
        with patch('boundary_repair.adapters.program.analyze_sources', side_effect=AssertionError('reparsed')):
            self.program.index(self.snap, self.ctx)
            self.assertIs(self.program.source_scope(self.snap, self.ctx), self.scope)
        self.assertTrue(any(d.startswith('edit_block_limit:') for d in self.scope.diagnostics))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_unknown_language_and_existing_syntax_error_use_text(self):
        self.prepare({'view.vue': '<template>red</template>', 'style.css': '.x { color: red; }', 'broken.js': 'const = red;'})
        self.assertFalse(self.scope.blocks)
        self.assertTrue(all(r.edit_mode == 'text' for r in self.scope.regions))
        edits = [SourceEdit('replace_text', r.region_id, 'blue', old_text='red') for r in self.scope.regions]
        self.assertEqual(len(self.apply(*edits)), 3)
        self.assertEqual(self.compile(*edits).syntax_check, 'unknown')

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_optional_parser_failure_falls_back_before_generation(self):
        with patch('boundary_repair.adapters.frontend._parse_selected_source', side_effect=ExternalServiceError('unavailable')):
            self.prepare({'view.js': SOURCE})
        self.assertFalse(self.scope.blocks)
        self.assertEqual(self.scope.regions[0].edit_mode, 'text')
        updated = self.apply(SourceEdit('replace_text', self.scope.regions[0].region_id, 'blue', old_text='red'))
        self.assertEqual(updated['view.js'].decode(), SOURCE.replace('red', 'blue'))

    def test_exact_search_is_unique_and_preserves_other_bytes(self):
        self.prepare({'style.css': '/* 中文 🐈 */\r\n.x { color: red; }\r\n.y { color: green; }'}, parser=False)
        edit = SourceEdit('replace_text', self.scope.regions[0].region_id, 'blue', old_text='red')
        before = (self.snap.root / 'style.css').read_bytes()
        self.assertEqual(self.apply(edit)['style.css'], before.replace(b'red', b'blue'))
        self.assertEqual(self.compile(edit).application_check, 'passed')

    def test_missing_ambiguous_overlapping_and_empty_search_are_rejected(self):
        self.prepare({'style.css': '.x { label: aaaa; color: red; }'}, parser=False)
        for old, error in (('blue', 'search_text_not_found'), ('aa', 'ambiguous_search_text'), ('', 'empty_search_text')):
            with self.subTest(old=old), self.assertRaisesRegex(ValidationError, error):
                self.apply(SourceEdit('replace_text', self.scope.regions[0].region_id, 'new', old_text=old))

    def test_text_insert_delete_and_no_op(self):
        self.prepare({'style.css': '.x { color: red; margin: 0; }'}, parser=False)
        target = self.scope.regions[0].region_id
        self.assertIn(b'color: red; padding: 0;', self.apply(SourceEdit('replace_text', target,
                      'color: red; padding: 0;', old_text='color: red;'))['style.css'])
        self.assertNotIn(b'margin', self.apply(SourceEdit('replace_text', target, '', old_text='margin: 0;'))['style.css'])
        with self.assertRaises(NoAdmissiblePatch):
            self.apply(SourceEdit('replace_text', target, 'red', old_text='red'))

    def test_empty_file_accepts_only_empty_original_match(self):
        self.prepare({'style.css': ''}, parser=False)
        edit = SourceEdit('replace_text', self.scope.regions[0].region_id, '.x { color: blue; }', old_text='')
        self.assertEqual(self.apply(edit)['style.css'], edit.new_text.encode())
        self.assertEqual(self.compile(edit).application_check, 'passed')

    def test_multifile_atomic_edit_creation_rename_and_delete(self):
        self.prepare({'a.css': '.a { color: red; }', 'b.css': '.b {}', 'c.css': '.c {}'}, parser=False)
        files = {f.path: f for f in self.scope.files}
        region = next(r for r in self.scope.regions if r.path == 'a.css')
        edits = (SourceEdit('replace_text', region.region_id, 'blue', old_text='red'),
                 SourceEdit('rename_file', files['b.css'].file_id, destination='moved.css'),
                 SourceEdit('delete_file', files['c.css'].file_id),
                 SourceEdit('create_file', 'new.css', '.new {}'))
        self.assertEqual(set(self.apply(*edits)), {'a.css', 'b.css', 'c.css', 'moved.css', 'new.css'})
        self.assertEqual(self.compile(*edits).application_check, 'passed')
        with self.assertRaisesRegex(ValidationError, 'search_text_not_found'):
            self.apply(*edits, SourceEdit('replace_text', region.region_id, 'x', old_text='absent'))
        self.assertEqual(tree_digest(self.snap.root), self.snap.tree_sha256)

    def test_comment_append_and_final_newline_is_not_a_patch(self):
        self.prepare({'style.css': '.x { color: red; }\n'}, parser=False)
        region = self.scope.regions[0]
        with self.assertRaisesRegex(NoAdmissiblePatch, 'no_executable_change'):
            self.compile(SourceEdit('replace_text', region.region_id, region.source + '/* explanatory */', old_text=region.source))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_live_schema_and_prompt_have_ids_not_edit_coordinates(self):
        from jsonschema import Draft202012Validator
        self.prepare({'view.js': SOURCE})
        block = self.block()
        good = {'operation': 'replace_block', 'target': block.block_id, 'new_text': METHOD.replace('red', 'blue'),
                'old_text': '', 'destination': ''}
        validator = Draft202012Validator(edit_transaction_schema(self.scope))
        validator.validate({'edits': [good]})
        for bad in ({**good, 'first_line': 3}, {**good, 'last_line': 6}, {**good, 'operation': 'replace_lines'},
                    {**good, 'operation': 'replace_region'}, {**good, 'target': 'B999'}):
            self.assertFalse(validator.is_valid({'edits': [bad]}))
        model = Mock()
        model.complete.return_value.text = json.dumps({'edits': [good]})
        plan = PatchPlan('live', (), EditKind.FREEFORM, (), (), (), edit_scope=self.scope)
        self.program.freeze_plan(plan, self.ctx)
        transaction = TransactionRenderer(model).render(task(), plan, self.ctx)
        artifact = self.program.compile(task(), plan, transaction, self.snap, self.ctx)
        self.assertEqual(model.complete.call_count, 1)
        request = model.complete.call_args.args[0]
        self.assertEqual(request.schema_name, 'edits.v4')
        prompt = json.loads(request.prompt)
        self.assertTrue(all('source' not in b and 'new_text' not in b for b in prompt['blocks']))
        self.assertEqual(prompt['regions'][0]['source'], SOURCE)
        self.assertEqual(artifact.application_check, 'passed')
        for op in ('replace_lines', 'insert_at', 'replace_region'):
            with self.assertRaises(ValidationError):
                parse_transaction(json.dumps({'edits': [{**good, 'operation': op}]}))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_unicode_line_separator_does_not_desynchronize_block_positions(self):
        source = SOURCE.replace('"red"', '`red green`')
        self.prepare({'view.js': source})
        model = Mock()
        model.complete.return_value.text = json.dumps({'edits': [{'operation': 'replace_block',
            'target': self.block().block_id, 'new_text': METHOD.replace('red', 'blue'),
            'old_text': '', 'destination': ''}]})
        plan = PatchPlan('positions', (), EditKind.FREEFORM, (), (), (), edit_scope=self.scope)
        TransactionRenderer(model).render(task(), plan, self.ctx)
        prompt = json.loads(model.complete.call_args.args[0].prompt)
        visible = prompt['regions'][0]
        self.assertEqual(visible['source'], source)
        lines = [line + '\n' for line in visible['source'].split('\n')[:-1]]
        block = next(b for b in prompt['blocks'] if b['block_id'] == self.block().block_id)
        displayed = ''.join(lines[block['start_inclusive']['line'] - visible['start_line']:
                                  block['end_exclusive']['line'] - visible['start_line']])
        self.assertEqual(displayed, METHOD.replace('"red"', '`red green`'))

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_invalid_generated_syntax_is_logged_without_retry_or_final_patch(self):
        self.prepare({'view.js': SOURCE})
        model = Mock()
        model.complete.return_value.text = json.dumps({'edits': [{'operation': 'replace_block',
            'target': self.block().block_id, 'new_text': METHOD + '  }\n', 'old_text': '', 'destination': ''}]})
        plan = PatchPlan('invalid', (), EditKind.FREEFORM, (), (), (), edit_scope=self.scope)
        transaction = TransactionRenderer(model).render(task(), plan, self.ctx)
        with self.assertRaisesRegex(ValidationError, 'generated_syntax_invalid'):
            self.program.compile(task(), plan, transaction, self.snap, self.ctx)
        case = self.cfg.results_root / self.ctx.run_id / 'cases' / self.ctx.instance_id
        rejection = json.loads((case / 'trajectory/rejected_syntax.json').read_text(encoding='utf-8'))
        self.assertEqual(rejection['path'], 'view.js')
        self.assertTrue(all(d['line'] > 0 and d['message'] for d in rejection['diagnostics']))
        self.assertTrue(rejection['diagnostics'])
        self.assertFalse((case / 'patch/final.patch').exists())
        self.assertEqual(model.complete.call_count, 1)

    @unittest.skipUnless(parser_module(), 'requires pinned TypeScript')
    def test_metadata_limit_does_not_disable_new_syntax_error_rejection(self):
        self.prepare({'view.js': SOURCE})
        parsed = analyze_sources([{'path': 'view.js', 'source': SOURCE}], self.cfg, self.ctx)
        base = dict(parsed['files'][0], analysis_status='partial', unsupported=['analysis_location_limit'])
        self.assertEqual(base['syntax_status'], 'passed')
        bad = analyze_sources([{'path': 'view.js', 'source': SOURCE + '}'}], self.cfg, self.ctx)
        with patch('boundary_repair.adapters.frontend.analyze_sources', side_effect=[{'files': [base]}, bad]):
            with self.assertRaisesRegex(ValidationError, 'generated_syntax_invalid'):
                PatchCompiler(self.cfg).check_syntax({'view.js': SOURCE.encode()}, {'view.js': (SOURCE + '}').encode()}, self.ctx)


if __name__ == '__main__':
    unittest.main()
