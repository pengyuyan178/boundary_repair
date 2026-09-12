"""Real compiler, Unicode, hash, edit-boundary, and diff application tests."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, parser_module, snapshot, task
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.domain.repair import EditKind, HoleFilling, PatchPlan, SyntaxHole
from boundary_repair.domain.errors import ValidationError, NoAdmissiblePatch
from boundary_repair.kernel.files import source_slice, tree_digest


@unittest.skipUnless(parser_module(), 'install typescript for real AST tests')
class ProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = snapshot(self.root)
        self.program = ProgramAdapter(config(self.root))
        self.ctx = context()
        self.site = next(s for s in self.program.index(self.snapshot, self.ctx).locations if s.node_kind == 'BooleanReturn')
        self.plan = PatchPlan('p', ('b',), EditKind.REFINE_GUARD,
                             (SyntaxHole('h', self.site, 'boolean-expression', ('active', 'hidden')),), (), ())

    def test_unicode_bytes_not_utf16(self):
        data, start, end = source_slice(self.snapshot.root, self.site)
        self.assertEqual(data[start:end].decode(), 'active || hidden')
        self.assertGreater(start, self.snapshot.root.joinpath('ui.js').read_text().index('active || hidden'))

    def test_index_reuses_immutable_snapshot(self):
        self.assertIs(self.program.index(self.snapshot, self.ctx), self.program.index(self.snapshot, self.ctx))

    def test_actual_git_apply_and_runtime_behavior(self):
        artifact = self.program.materialize(task(), self.plan, (HoleFilling('h', '(active && !(hidden))'),), self.snapshot, self.ctx)
        candidate = self.root / 'candidate'
        candidate.mkdir()
        (candidate / 'ui.js').write_bytes((self.snapshot.root / 'ui.js').read_bytes())
        patch = self.root / 'candidate.patch'
        patch.write_bytes(artifact.unified_diff.encode('utf-8'))
        subprocess.run(['git', 'apply', '--check', str(patch)], cwd=candidate, check=True, capture_output=True)
        subprocess.run(['git', 'apply', str(patch)], cwd=candidate, check=True, capture_output=True)
        code = "const f=require(process.argv[1]).shouldShow;console.log(JSON.stringify([[true,false],[true,true],[false,false],[false,true]].map(v=>f(...v))));"
        result = subprocess.check_output(['node', '-e', code, str(candidate / 'ui.js')])
        self.assertEqual(json.loads(result), [True, False, False, False])
        self.assertEqual(hashlib.sha256(artifact.unified_diff.encode()).hexdigest(), artifact.sha256)

    def test_syntax_invalid_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (HoleFilling('h', 'active && )'),), self.snapshot, self.ctx)

    def test_boolean_replacement_cannot_inject_sibling_functions(self):
        replacement = 'active; } function injected() { sideEffect(); } function leftover(hidden) { return hidden'
        with self.assertRaisesRegex(ValidationError, 'local_syntax_scope_escaped'):
            self.program.materialize(task(), self.plan, (HoleFilling('h', replacement),), self.snapshot, self.ctx)

    def test_calls_cannot_escape_boolean_grammar(self):
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (HoleFilling('h', 'danger()'),), self.snapshot, self.ctx)

    def test_read_not_declared_is_rejected(self):
        plan = replace(self.plan, holes=(replace(self.plan.holes[0], allowed_symbols=('active',)),))
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), plan, (HoleFilling('h', 'hidden'),), self.snapshot, self.ctx)

    def test_outside_range_modified_snapshot_rejected(self):
        (self.snapshot.root / 'ui.js').write_text('// altered\n' + (self.snapshot.root / 'ui.js').read_text())
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (HoleFilling('h', 'false'),), self.snapshot, self.ctx)

    def test_noop_is_not_patch(self):
        with self.assertRaises(NoAdmissiblePatch):
            self.program.materialize(task(), self.plan, (HoleFilling('h', 'active || hidden'),), self.snapshot, self.ctx)

    def test_overlapping_holes_rejected(self):
        plan = replace(self.plan, holes=self.plan.holes + (replace(self.plan.holes[0], hole_id='h2'),))
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), plan, (HoleFilling('h', 'false'), HoleFilling('h2', 'true')), self.snapshot, self.ctx)

    def test_duplicate_or_missing_holes_rejected(self):
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (), self.snapshot, self.ctx)
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (HoleFilling('h','false'), HoleFilling('h','true')), self.snapshot, self.ctx)

    def test_unknown_function_is_not_claimed_pure(self):
        path = self.snapshot.root / 'async.js'
        path.write_text('async function getIt(x){ return await compute(x); }')
        updated = replace(self.snapshot, tree_sha256=tree_digest(self.snapshot.root))
        idx = self.program.index(updated, self.ctx)
        self.assertFalse(any(s.symbol == 'getIt' and s.node_kind == 'BooleanReturn' for s in idx.locations))

    def test_no_newline_diff_is_git_applicable(self):
        (self.snapshot.root / 'ui.js').write_text('function f(x){return x;}')
        updated = replace(self.snapshot, tree_sha256=tree_digest(self.snapshot.root))
        site = next(s for s in self.program.index(updated, self.ctx).locations if s.node_kind == 'BooleanReturn')
        plan = PatchPlan('p', ('b',), EditKind.REFINE_GUARD, (SyntaxHole('h', site, 'boolean-expression', ('x',)),), (), ())
        patch = self.program.materialize(task(), plan, (HoleFilling('h', '!x'),), updated, self.ctx)
        self.assertIn('\\ No newline at end of file', patch.unified_diff)
        path = self.root / 'nonewline.patch'
        path.write_bytes(patch.unified_diff.encode('utf-8'))
        subprocess.run(['git','apply','--check',str(path)], cwd=updated.root, capture_output=True, check=True)

from unittest.mock import Mock
from boundary_repair.domain.repair import RepairBoundary
from boundary_repair.domain.task import RepositorySnapshot
from boundary_repair.algorithms.rendering import HoleRenderer
from boundary_repair.kernel.retrieval import candidate_boundaries, context_excerpt, syntax_holes
from boundary_repair.ports import ModelResponse


@unittest.skipUnless(parser_module(), 'install typescript for real AST tests')
class LocalEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.program = ProgramAdapter(config(self.root))
        self.ctx = context()

    def setup_source(self, source, name='ui.js'):
        (self.root / name).write_bytes(source.encode('utf-8'))
        self.snapshot = RepositorySnapshot(self.root, 'a' * 40, tree_digest(self.root))
        return self.program.index(self.snapshot, self.ctx)

    def plan(self, site, expected=None):
        hole = SyntaxHole('h', site, expected or 'local:' + site.node_kind, ())
        return PatchPlan('p', ('b',), EditKind.FREEFORM, (hole,), (), ())

    def apply(self, artifact):
        with TemporaryDirectory() as directory:
            candidate = Path(directory)
            for path in self.root.iterdir():
                if path.is_file():
                    (candidate / path.name).write_bytes(path.read_bytes())
            result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '-'], input=artifact.unified_diff.encode('utf-8'),
                                    cwd=candidate, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            return {path.name: path.read_bytes() for path in candidate.iterdir() if path.is_file()}

    def test_only_comments_are_not_a_behavioral_patch(self):
        index = self.setup_source('function run(x) { log(x); keep(x); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Statement')
        old = source_slice(self.root, site)[0][site.start_byte:site.end_byte].decode()
        with self.assertRaisesRegex(NoAdmissiblePatch, 'no_executable_change'):
            self.program.materialize(task(), self.plan(site), (HoleFilling('h', '// fixed\n' + old),), self.snapshot, self.ctx)

    def test_old_whole_function_protocol_cannot_delete_body(self):
        index = self.setup_source('function run(x) { log(x); keep(x); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Function')
        with self.assertRaisesRegex(ValidationError, 'unbounded_hole_not_allowed'):
            self.program.materialize(task(), self.plan(site, 'source-fragment'), (HoleFilling('h', '// removed'),), self.snapshot, self.ctx)

    def test_omission_comment_and_implicit_deletion_are_rejected(self):
        index = self.setup_source('function run(x) { log(x); keep(x); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Statement')
        for text in ('// Keep the rest of the block the same', ''):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                self.program.materialize(task(), self.plan(site), (HoleFilling('h', text),), self.snapshot, self.ctx)

    def test_explicit_statement_deletion_preserves_siblings(self):
        source = 'function run(x) { log(x); keep(x); }\r\n'
        index = self.setup_source(source)
        site = next(s for s in index.locations if s.node_kind == 'Statement')
        artifact = self.program.materialize(task(), self.plan(site), (HoleFilling('h', '', 'delete'),), self.snapshot, self.ctx)
        self.assertEqual(self.apply(artifact)['ui.js'], source.replace('log(x);', '').encode())

    def test_large_explicit_statement_deletion_is_not_rejected_by_line_count(self):
        source = 'function run(x) {\nlog(\n' + '\n'.join('  x,' for _ in range(60)) + '\n);\nkeep(x);\n}\n'
        index = self.setup_source(source)
        site = next(s for s in index.locations if s.node_kind == 'Statement')
        artifact = self.program.materialize(task(), self.plan(site), (HoleFilling('h', '', 'delete'),), self.snapshot, self.ctx)
        self.assertIn(b'keep(x);', self.apply(artifact)['ui.js'])

    def test_expression_cannot_inject_sibling_statements(self):
        index = self.setup_source('function run(x) { if(x) ok(); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Condition')
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan(site), (HoleFilling('h', 'x){}; injected(); if(x'),), self.snapshot, self.ctx)

    def test_partial_model_edits_preserve_crlf_and_unselected_sites(self):
        source = '// 中文😀\r\nfunction run(x, y) { if(x) one(); if(y) two(); }\r\n'
        index = self.setup_source(source)
        sites = [s for s in index.locations if s.node_kind == 'Condition']
        holes = tuple(SyntaxHole(f'h{i}', site, 'local:Condition', ()) for i, site in enumerate(sites))
        plan = PatchPlan('p', ('b',), EditKind.REFINE_GUARD, holes, (), ())
        model = Mock()
        model.complete.return_value = ModelResponse(json.dumps({'edits': [
            {'hole_id': 'h0', 'operation': 'replace', 'new_source': '!x', 'condition': None}]}), 10, 'fixture')
        fillings = HoleRenderer(model).render(task(), plan, self.snapshot, self.ctx)
        artifact = self.program.materialize(task(), plan, fillings, self.snapshot, self.ctx)
        self.assertEqual(self.apply(artifact)['ui.js'], source.replace('if(x)', 'if(!x)').encode())
        model.complete.assert_called_once()
        self.assertEqual(model.complete.call_args.args[0].schema_name, 'fillings.v2')

    def test_consumer_guard_retains_original_fallback_and_other_attributes(self):
        source = 'const view = <Box title={label}>{renderText(item)}</Box>;\n'
        index = self.setup_source(source, 'ui.jsx')
        site = next(s for s in index.locations if s.node_kind == 'Consumer' and s.symbol == 'children')
        plan = self.plan(site, 'consumer-branch')
        model = Mock()
        model.complete.return_value = ModelResponse(json.dumps({'edits': [
            {'hole_id': 'h', 'operation': 'guard_consumer', 'new_source': 'renderRich(item)', 'condition': 'enabled'}]}), 10, 'fixture')
        fillings = HoleRenderer(model).render(task(), plan, self.snapshot, self.ctx)
        artifact = self.program.materialize(task(), plan, fillings, self.snapshot, self.ctx)
        expected = source.replace('renderText(item)', '(enabled) ? (renderRich(item)) : (renderText(item))')
        self.assertEqual(self.apply(artifact)['ui.jsx'], expected.encode())

    def test_unknown_duplicate_and_disallowed_edit_ids_are_rejected(self):
        index = self.setup_source('function run(x) { if(x) ok(); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Condition')
        valid = {'hole_id': 'h', 'operation': 'replace', 'new_source': '!x', 'condition': None}
        for edits in ([{**valid, 'hole_id': 'missing'}], [valid, valid], [{**valid, 'operation': 'delete'}]):
            model = Mock()
            model.complete.return_value = ModelResponse(json.dumps({'edits': edits}), 10, 'fixture')
            with self.assertRaises(ValidationError):
                HoleRenderer(model).render(task(), self.plan(site), self.snapshot, self.ctx)
            model.complete.assert_called_once()

    def test_file_boundary_is_decomposed_into_local_nonoverlapping_sites(self):
        index = self.setup_source('function run(x) { if(x) one(); else two(); }\n')
        file = next(s for s in index.locations if s.node_kind == 'File')
        boundary = RepairBoundary('b', (file,), EditKind.FREEFORM, (), ())
        holes = syntax_holes(boundary, index, self.snapshot, 'run x', 'b')
        self.assertTrue(holes)
        self.assertFalse(any(h.site.node_kind in {'File', 'Function', 'Assignment'} for h in holes))
        for left, right in zip(holes, holes[1:]):
            self.assertLessEqual(left.site.end_byte, right.site.start_byte)

    def test_consumer_fallback_is_checked_again_at_materialization(self):
        index = self.setup_source('const view = <div>{label}</div>;\n', 'ui.jsx')
        site = next(s for s in index.locations if s.node_kind == 'Consumer')
        with self.assertRaisesRegex(ValidationError, 'consumer_fallback_not_retained'):
            self.program.materialize(task(), self.plan(site, 'consumer-branch'),
                                     (HoleFilling('h', 'flag ? value : other', 'guard_consumer'),), self.snapshot, self.ctx)

    def test_delete_cannot_carry_executable_replacement(self):
        index = self.setup_source('function run(x) { log(x); keep(x); }\n')
        site = next(s for s in index.locations if s.node_kind == 'Statement')
        with self.assertRaisesRegex(ValidationError, 'invalid_delete_arguments'):
            self.program.materialize(task(), self.plan(site), (HoleFilling('h', 'other();', 'delete'),), self.snapshot, self.ctx)

    def test_nested_function_bodies_are_not_atomic_conditions(self):
        index = self.setup_source('if ((() => { sideEffect(); return flag; })()) ok();\n')
        self.assertFalse(any(s.node_kind == 'Condition' for s in index.locations))

    def test_jsx_text_change_is_not_mistaken_for_formatting(self):
        index = self.setup_source('const view = <div>{label}</div>;\n', 'ui.jsx')
        site = next(s for s in index.locations if s.node_kind == 'Consumer')
        filling = HoleFilling('h', 'true ? (<span> </span>) : (label)', 'guard_consumer')
        result = self.program.materialize(task(), self.plan(site, 'consumer-branch'), (filling,), self.snapshot, self.ctx)
        self.assertIn('<span> </span>', result.unified_diff)

    def test_context_window_reaches_late_source_without_stitching(self):
        text = '// unrelated\n' * 1000 + 'function importantBoundary() { return issueValue; }\n'
        excerpt, start = context_excerpt(text, 'importantBoundary issueValue', 200)
        self.assertIn('importantBoundary', excerpt)
        self.assertEqual(text[start:start + len(excerpt)], excerpt)


if __name__ == '__main__':
    unittest.main()
