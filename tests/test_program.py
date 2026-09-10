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
        patch.write_text(artifact.unified_diff, encoding='utf-8')
        subprocess.run(['git', 'apply', '--check', str(patch)], cwd=candidate, check=True, capture_output=True)
        subprocess.run(['git', 'apply', str(patch)], cwd=candidate, check=True, capture_output=True)
        code = "const f=require(process.argv[1]).shouldShow;console.log(JSON.stringify([[true,false],[true,true],[false,false],[false,true]].map(v=>f(...v))));"
        result = subprocess.check_output(['node', '-e', code, str(candidate / 'ui.js')])
        self.assertEqual(json.loads(result), [True, False, False, False])
        self.assertEqual(hashlib.sha256(artifact.unified_diff.encode()).hexdigest(), artifact.sha256)

    def test_syntax_invalid_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.program.materialize(task(), self.plan, (HoleFilling('h', 'active && )'),), self.snapshot, self.ctx)

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
        path.write_text(patch.unified_diff)
        subprocess.run(['git','apply','--check',str(path)], cwd=updated.root, capture_output=True, check=True)

if __name__ == '__main__':
    unittest.main()
