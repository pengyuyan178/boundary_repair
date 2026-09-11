"""v3 repository compiler tests for frozen scope, encoding, Git application, and transactions."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import boundary_repair.adapters.repository as repository_adapter
from boundary_repair.adapters.repository import (
    PatchCompiler,
    SourceRepository,
    comment_only_change,
    decode_source,
    omission_placeholder,
    scope_snippets,
    transaction_contents,
)
from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import (
    EditKind,
    EditRegion,
    EditScope,
    EditTransaction,
    FileState,
    PatchPlan,
    SourceEdit,
)
from boundary_repair.domain.task import RepositorySnapshot
from boundary_repair.kernel.files import tree_digest
from helpers import config, context, task


GIT_AVAILABLE = shutil.which('git') is not None


def full_scope(root: Path, paths: list[str]) -> EditScope:
    """Construct an immutable normal, whole-file scope from exact fixture bytes."""
    files: list[FileState] = []
    regions: list[EditRegion] = []
    for ordinal, path in enumerate(paths):
        data = (root / path).read_bytes()
        decoded = decode_source(data)
        if decoded is None:
            raise AssertionError('fixture must be supported text')
        text, encoding = decoded
        file_id = f'F{ordinal:03d}'
        files.append(FileState(file_id, path, hashlib.sha256(data).hexdigest(), len(data),
                               (root / path).stat().st_mode & 0o777, encoding, True))
        regions.append(EditRegion(f'R{ordinal:03d}', file_id, path, 0, len(data), 1, text,
                                  hashlib.sha256(data).hexdigest(), 0))
    parents = tuple(sorted({str(Path(path).parent).replace('\\', '/') for path in paths}))
    return EditScope(tuple(files), tuple(regions), parents)


def partial_scope(root: Path, path: str, source: str) -> EditScope:
    """Construct a strong-mode scope whose region is a non-line-aligned Boolean expression."""
    data = (root / path).read_bytes()
    decoded = decode_source(data)
    if decoded is None:
        raise AssertionError('fixture must be supported text')
    text, encoding = decoded
    start_char = text.index(source)
    start_byte = len(text[:start_char].encode(encoding))
    end_byte = start_byte + len(source.encode(encoding))
    file = FileState('F000', path, hashlib.sha256(data).hexdigest(), len(data),
                     (root / path).stat().st_mode & 0o777, encoding, False)
    region = EditRegion('R000', file.file_id, path, start_byte, end_byte,
                        text[:start_char].count('\n') + 1, source,
                        hashlib.sha256(data[start_byte:end_byte]).hexdigest(), start_char)
    return EditScope((file,), (region,), (str(Path(path).parent).replace('\\', '/'),))


def plan(scope: EditScope) -> PatchPlan:
    """Build the smallest unrestricted plan carrying a frozen transaction scope."""
    return PatchPlan('repository-v3', (), EditKind.FREEFORM, (), (), (), edit_scope=scope)


def snapshot(root: Path) -> RepositorySnapshot:
    """Bind a fixture directory to the synthetic base commit used by test tasks."""
    return RepositorySnapshot(root, 'a' * 40, tree_digest(root))


def apply_and_read(root: Path, originals: dict[str, bytes], modes: dict[str, int], diff: str) -> Path:
    """Apply an artifact to independent pristine fixture bytes with conversions disabled."""
    applied = root / 'applied'
    applied.mkdir()
    for path, data in originals.items():
        target = applied / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if modes[path] & 0o111 else 0o644)
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'init', '-q'], cwd=applied,
                   check=True, capture_output=True)
    (applied / '.git' / 'info' / 'attributes').write_text(
        '* -text -eol -filter -ident -working-tree-encoding\n', encoding='utf-8')
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '--check', '-'], cwd=applied,
                   input=diff.encode('utf-8'), check=True, capture_output=True)
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '-'], cwd=applied,
                   input=diff.encode('utf-8'), check=True, capture_output=True)
    return applied


@unittest.skipUnless(GIT_AVAILABLE, 'Git is required to prove patch application')
class RepositoryV3Tests(unittest.TestCase):
    """Exercise only local synthetic source files and the trusted system Git binary."""

    def setUp(self) -> None:
        """Create a fresh production-like source tree and parser-free local configuration."""
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()
        self.settings = config(self.root, parser=False)

    def tearDown(self) -> None:
        """Discard the synthetic source tree after each transaction test."""
        self.temporary.cleanup()

    def compile(self, scope: EditScope, edits: tuple[SourceEdit, ...], *, settings=None):
        """Compile one atomic fixture transaction against a hash-bound snapshot."""
        return PatchCompiler(settings or self.settings).compile(
            task(), plan(scope), EditTransaction(edits), snapshot(self.root), context())

    def test_retrieval_exposes_real_start_character_and_kernel_reexport(self) -> None:
        """Normal retrieval trims only full lines and reports its nonzero Unicode character offset."""
        (self.root / 'view.css').write_text('zero\none\ntwo\nneedle\nlast\n', encoding='utf-8')
        settings = replace(self.settings, integration=replace(self.settings.integration, max_context_chars=6))
        scope = SourceRepository(settings).retrieve(snapshot(self.root), 'needle', context())
        self.assertEqual(len(scope.regions), 1)
        region = scope.regions[0]
        self.assertGreater(region.start_char, 0)
        full_text = decode_source((self.root / 'view.css').read_bytes())[0]
        self.assertEqual(full_text[region.start_char:region.start_char + len(region.source)], region.source)
        self.assertEqual(scope_snippets(scope)[0]['start_char'], region.start_char)
        self.assertTrue(region.source.endswith('\n'))

    def test_retrieval_prioritizes_source_unless_the_issue_names_supporting_file(self) -> None:
        """High-overlap templates lose to source, while literal documentation and configuration paths win."""
        repeated = ('card color visual regression bug report template guidance ' * 30).strip()
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'card.css').write_text('.card { color: red; }\n', encoding='utf-8')
        (self.root / 'docs').mkdir()
        (self.root / 'docs' / 'repair-guide.md').write_text(repeated, encoding='utf-8')
        (self.root / 'configs').mkdir()
        (self.root / 'configs' / 'theme.json').write_text('{"theme": "red"}\n', encoding='utf-8')
        (self.root / 'examples').mkdir()
        (self.root / 'examples' / 'issue-report.md').write_text(repeated, encoding='utf-8')
        issue_templates = self.root / '.github' / 'ISSUE_TEMPLATE'
        issue_templates.mkdir(parents=True)
        (issue_templates / 'visual.md').write_text(repeated, encoding='utf-8')
        (self.root / 'README.md').write_text(repeated, encoding='utf-8')
        frozen = snapshot(self.root)
        settings = replace(self.settings, integration=replace(self.settings.integration, context_files=1))

        default_scope = SourceRepository(settings).retrieve(
            frozen, 'card color visual regression bug report template guidance', context())
        self.assertEqual(default_scope.files[0].path, 'src/card.css')

        document_scope = SourceRepository(settings).retrieve(
            frozen, 'Revise docs/repair-guide.md for the card color guidance.', context())
        self.assertEqual(document_scope.files[0].path, 'docs/repair-guide.md')

        configuration_scope = SourceRepository(settings).retrieve(
            frozen, 'Set configs/theme.json to the requested visual theme.', context())
        self.assertEqual(configuration_scope.files[0].path, 'configs/theme.json')

    def test_settings_components_and_configuration_remain_production_candidates(self) -> None:
        """Directory names for runtime settings do not imply documentation or reporting metadata."""
        from boundary_repair.kernel.retrieval import source_priority
        for path in ('src/settings/theme.jsx', 'src/config/load.js', 'configs/theme.json'):
            self.assertEqual(source_priority('visual theme is incorrect', path), 0)
        self.assertGreater(source_priority('visual theme is incorrect', '.github/ISSUE_TEMPLATE/visual.md'), 0)

    def test_strong_partial_region_accepts_replace_region_without_line_authority(self) -> None:
        """A Boolean-expression strong scope may replace a non-line region while file deletion stays barred."""
        path = 'logic.js'
        (self.root / path).write_text('function visible() { return true; }\n', encoding='utf-8')
        scope = partial_scope(self.root, path, 'true')
        originals, updated = transaction_contents(
            snapshot(self.root), scope, EditTransaction((SourceEdit('replace_region', 'R000', 'false'),)))
        original = (self.root / path).read_bytes()
        self.assertEqual(updated[path], original.replace(b'true', b'false'))
        with self.assertRaisesRegex(ValidationError, 'invalid_file_operation'):
            transaction_contents(snapshot(self.root), scope, EditTransaction((SourceEdit('delete_file', 'F000'),)))
        self.assertIn(path, originals)

    def test_crlf_line_replacement_and_no_final_newline_round_trip(self) -> None:
        """Line editing preserves CRLF while a no-final-newline CSS patch applies to exact final bytes."""
        (self.root / 'lines.css').write_bytes(b'a{}\r\nb{}\r\n')
        scope = full_scope(self.root, ['lines.css'])
        _, updated = transaction_contents(
            snapshot(self.root), scope,
            EditTransaction((SourceEdit('replace_lines', 'R000', 'c{}', 2, 2),)))
        self.assertEqual(updated['lines.css'], b'a{}\r\nc{}\r\n')

        (self.root / 'append.css').write_bytes(b'a{}')
        append_scope = full_scope(self.root, ['append.css'])
        _, appended = transaction_contents(
            snapshot(self.root), append_scope,
            EditTransaction((SourceEdit('insert_at', 'R000', 'b{}', 2),)))
        self.assertEqual(appended['append.css'], b'a{}\nb{}\n')

        (self.root / 'theme.css').write_bytes(b'body{color:red}')
        scope = full_scope(self.root, ['lines.css', 'theme.css'])
        artifact = self.compile(scope, (SourceEdit('replace_region', 'R001', 'body{color:blue}'),))
        self.assertIn('No newline at end of file', artifact.unified_diff)
        originals = {'lines.css': b'a{}\r\nb{}\r\n', 'theme.css': b'body{color:red}'}
        applied = apply_and_read(self.root.parent, originals, {'lines.css': 0o644, 'theme.css': 0o644}, artifact.unified_diff)
        self.assertEqual((applied / 'theme.css').read_bytes(), b'body{color:blue}')
        self.assertEqual((applied / 'lines.css').read_bytes(), originals['lines.css'])

    def test_utf16_binary_patch_applies_exactly_despite_target_attributes(self) -> None:
        """UTF-16 replacement survives binary diff and apply with hostile target attributes present."""
        source = 'const label = "before";\r\n'
        original = b'\xff\xfe' + source.encode('utf-16-le')
        (self.root / 'script.ts').write_bytes(original)
        (self.root / '.gitattributes').write_text(
            '*.ts text working-tree-encoding=UTF-16LE filter=missing-filter\n', encoding='utf-8')
        scope = full_scope(self.root, ['.gitattributes', 'script.ts'])
        artifact = self.compile(scope, (SourceEdit('replace_region', 'R001', 'const label = "after";\r\n'),))
        expected = b'\xff\xfe' + 'const label = "after";\r\n'.encode('utf-16-le')
        originals = {'.gitattributes': (self.root / '.gitattributes').read_bytes(), 'script.ts': original}
        applied = apply_and_read(self.root.parent, originals,
                                 {'.gitattributes': 0o644, 'script.ts': 0o644}, artifact.unified_diff)
        self.assertEqual((applied / 'script.ts').read_bytes(), expected)
        self.assertEqual((applied / '.gitattributes').read_bytes(), originals['.gitattributes'])

    def test_create_delete_rename_is_atomic_and_preserves_required_modes(self) -> None:
        """One multi-file transaction creates 644 files, removes files, and keeps executable rename mode."""
        (self.root / 'remove.css').write_bytes(b'x{}\n')
        (self.root / 'old.js').write_bytes(b'export const value = true;\n')
        (self.root / 'old.js').chmod(0o755)
        scope = full_scope(self.root, ['remove.css', 'old.js'])
        artifact = self.compile(scope, (
            SourceEdit('create_file', 'new.css', 'body{}\n'),
            SourceEdit('delete_file', 'F000'),
            SourceEdit('rename_file', 'F001', destination='moved.js'),
        ))
        originals = {'remove.css': b'x{}\n', 'old.js': b'export const value = true;\n'}
        applied = apply_and_read(self.root.parent, originals, {'remove.css': 0o644, 'old.js': 0o755},
                                 artifact.unified_diff)
        self.assertFalse((applied / 'remove.css').exists())
        self.assertFalse((applied / 'old.js').exists())
        self.assertEqual((applied / 'new.css').read_bytes(), b'body{}\n')
        self.assertEqual((applied / 'moved.js').read_bytes(), originals['old.js'])
        if os.name == 'nt':
            self.assertIn('patch_mode_unverified_windows', artifact.diagnostics)
            self.assertIn('new file mode 100644', artifact.unified_diff)
        else:
            self.assertEqual((applied / 'new.css').stat().st_mode & 0o100, 0)
            self.assertEqual((applied / 'moved.js').stat().st_mode & 0o100, 0o100)

    @unittest.skipIf(os.name == 'nt', 'POSIX Git mode verification is unavailable on Windows')
    def test_git_mode_verification_normalizes_umask_created_files(self) -> None:
        """A real Git apply under umask 0002 accepts 664/775 as Git 644/755 modes."""
        (self.root / 'old.js').write_bytes(b'export const value = true;\n')
        (self.root / 'old.js').chmod(0o755)
        scope = full_scope(self.root, ['old.js'])
        original_umask = os.umask(0o002)
        try:
            artifact = self.compile(scope, (
                SourceEdit('create_file', 'new.css', 'body{}\n'),
                SourceEdit('rename_file', 'F000', destination='moved.js'),
            ))
            applied = apply_and_read(
                self.root.parent,
                {'old.js': b'export const value = true;\n'},
                {'old.js': 0o755},
                artifact.unified_diff,
            )
        finally:
            os.umask(original_umask)
        self.assertEqual((applied / 'new.css').stat().st_mode & 0o777, 0o664)
        self.assertEqual((applied / 'moved.js').stat().st_mode & 0o777, 0o775)

    @unittest.skipIf(os.name == 'nt', 'POSIX Git mode verification is unavailable on Windows')
    def test_compiler_rejects_patch_that_changes_executable_bit(self) -> None:
        """An emitted Git mode change cannot silently turn an authorized executable non-executable."""
        path = 'script.js'
        (self.root / path).write_bytes(b'export const value = true;\n')
        (self.root / path).chmod(0o755)
        scope = full_scope(self.root, [path])
        real_run_process = repository_adapter.run_process
        diff_mode_changed = False

        def emit_executable_mode_change(arguments, *, cwd, input_data=None, timeout):
            """Simulate an unexpected executable-bit diff before Git emits the patch."""
            nonlocal diff_mode_changed
            if 'diff' in arguments:
                (Path(cwd) / path).chmod(0o644)
                diff_mode_changed = True
            return real_run_process(arguments, cwd=cwd, input_data=input_data, timeout=timeout)

        with patch.object(repository_adapter, 'run_process', side_effect=emit_executable_mode_change):
            with self.assertRaisesRegex(ValidationError, 'generated_patch_mode_mismatch'):
                self.compile(scope, (SourceEdit('replace_region', 'R000', 'export const value = false;\n'),))
        self.assertTrue(diff_mode_changed)

    def test_bounds_scope_hash_overlap_and_placeholder_are_rejected(self) -> None:
        """Reject final oversize files, unknown capability targets, stale hashes, overlaps, and omitted source."""
        (self.root / 'style.css').write_bytes(b'a{}\n')
        scope = full_scope(self.root, ['style.css'])
        with self.assertRaisesRegex(ValidationError, 'generated_text_encoding_or_size_invalid'):
            transaction_contents(snapshot(self.root), scope,
                                 EditTransaction((SourceEdit('replace_region', 'R000', 'a{' + 'x' * 20 + '}'),)), 10)
        with self.assertRaisesRegex(ValidationError, 'generated_text_encoding_or_size_invalid'):
            transaction_contents(snapshot(self.root), scope,
                                 EditTransaction((SourceEdit('create_file', 'new.css', 'x' * 20),)), 10)
        with self.assertRaisesRegex(ValidationError, 'unknown_edit_region'):
            transaction_contents(snapshot(self.root), scope, EditTransaction((SourceEdit('replace_region', 'missing', 'b{}'),)))
        with self.assertRaisesRegex(ValidationError, 'overlapping_transaction_edits'):
            transaction_contents(snapshot(self.root), scope, EditTransaction((
                SourceEdit('replace_region', 'R000', 'b{}'), SourceEdit('replace_region', 'R000', 'c{}'),
            )))
        with self.assertRaisesRegex(ValidationError, 'omission_placeholder'):
            transaction_contents(snapshot(self.root), scope,
                                 EditTransaction((SourceEdit('replace_region', 'R000', '/* keep rest unchanged */'),)))
        with self.assertRaisesRegex(NoAdmissiblePatch, 'comment_only_patch'):
            transaction_contents(snapshot(self.root), scope,
                                 EditTransaction((SourceEdit('replace_region', 'R000', '/* note */'),)))
        (self.root / 'style.css').write_bytes(b'b{}\n')
        with self.assertRaisesRegex(ValidationError, 'stale_source_hash'):
            transaction_contents(snapshot(self.root), scope, EditTransaction((SourceEdit('replace_region', 'R000', 'c{}'),)))

    def test_omission_placeholder_requires_an_explicit_placeholder_comment(self) -> None:
        """Ellipses inside ordinary explanatory examples remain admissible source text."""
        markdown_example = '// Handle **...\\[** as a Markdown example\nconst matcher = true;\n'
        self.assertFalse(omission_placeholder(markdown_example, ''))
        self.assertTrue(omission_placeholder('// ...\n', ''))
        self.assertTrue(omission_placeholder('/* ... */\n', ''))
        self.assertTrue(omission_placeholder('/* keep the rest unchanged */\n', ''))
        self.assertTrue(omission_placeholder('/* 其余不变 */\n', ''))

    def test_base_commit_and_comment_classification_remain_conservative(self) -> None:
        """A mismatched base is rejected and CSS/HTML strings are never mistaken for removable comments."""
        (self.root / 'page.html').write_text('<p title="/* literal */">x</p>', encoding='utf-8')
        scope = full_scope(self.root, ['page.html'])
        with self.assertRaisesRegex(ValidationError, 'base_commit_mismatch'):
            PatchCompiler(self.settings).compile(
                task('b' * 40), plan(scope), EditTransaction((SourceEdit('replace_region', 'R000', '<p>x</p>'),)),
                snapshot(self.root), context())
        self.assertTrue(comment_only_change('a{}', '/* note */a{}', '.css'))
        self.assertFalse(comment_only_change('a{content:"x"}', 'a{content:"/* note */"}', '.css'))
        self.assertFalse(comment_only_change('<p title="x">x</p>', '<p title="<!-- note -->">x</p>', '.html'))

    def test_parser_partial_or_unavailable_is_syntax_unknown_not_passed(self) -> None:
        """Any optional parser status or unsupported diagnostic prevents a false passed syntax certificate."""
        compiler = PatchCompiler(self.settings)
        row = {'path': 'logic.js', 'locations': [], 'functions': [], 'identifiers': [],
               'semantic_hash': None, 'consumer_guards': [],
               'unsupported': ['analysis_execution_failed'], 'analysis_status': 'unavailable'}
        with patch('boundary_repair.adapters.frontend.analyze_sources', return_value={'files': [row]}):
            status, diagnostics = compiler.check_syntax(
                {'logic.js': b'const value = true;\n'}, {'logic.js': b'const value = false;\n'}, context())
        self.assertEqual(status, 'unknown')
        self.assertEqual(diagnostics, ['syntax_unknown:logic.js'])
        with self.assertRaisesRegex(NoAdmissiblePatch, 'no_executable_change'):
            compiler.check_syntax({'page.css': b'a{}'}, {'page.css': b'/* note */a{}'}, context())


if __name__ == '__main__':
    unittest.main()
