"""Language-neutral source retrieval and atomic, exact-base patch compilation."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory

from boundary_repair.adapters.process import run_process
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import EditBlock, EditRegion, EditScope, EditTransaction, FileState, PatchArtifact, PatchPlan
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.files import EXCLUDED, TEST_PARTS, allowed_source, safe_path, tree_digest
from boundary_repair.kernel.retrieval import (
    context_excerpt,
    location_score,
    scope_snippets as scope_snippets,
    source_priority,
)


JS_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
COMMENT_SUFFIXES = JS_SUFFIXES | {'.css', '.scss', '.html', '.htm'}


def decode_source(data: bytes) -> tuple[str, str] | None:
    """Decode supported text losslessly, preserving BOM and original newline bytes."""
    encoding = 'utf-16-le' if data.startswith(b'\xff\xfe') else 'utf-16-be' if data.startswith(b'\xfe\xff') else 'utf-8'
    if encoding == 'utf-8' and b'\0' in data:
        return None
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None
    return (text, encoding) if text.encode(encoding) == data else None


def _bom(data: bytes) -> bytes:
    """Return a retained Unicode BOM so complete-range replacements cannot turn UTF-16 into binary."""
    for marker in (b'\xff\xfe', b'\xfe\xff', b'\xef\xbb\xbf'):
        if data.startswith(marker):
            return marker
    return b''


def encode_edit_text(text: str, encoding: str, original: bytes, start: int, end: int) -> bytes:
    """Encode a replacement and retain a consumed original BOM without adding one on insertion."""
    encoded = text.encode(encoding)
    marker = _bom(original)
    return marker + encoded if start == 0 < end and marker and not encoded.startswith(marker) else encoded


def omission_placeholder(text: str, original: str) -> bool:
    """Reject newly introduced standalone omission markers, not ordinary ellipsis examples."""
    comments = re.compile(
        r'(?ms)^[ \t]*(?P<comment>//[^\r\n]*|#[^\r\n]*|/\*.*?\*/|<!--.*?-->)[ \t]*(?:\r?\n|$)'
    )
    directive = re.compile(
        r'\b(?:keep|leave|rest|remaining)\b.*\b(?:unchanged|same)\b|其余.*(?:不变|省略)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in comments.finditer(text):
        comment = match.group('comment')
        if comment in original:
            continue
        if comment.startswith('//'):
            payload = comment[2:]
        elif comment.startswith('#'):
            payload = comment[1:]
        elif comment.startswith('/*'):
            payload = comment[2:-2]
        else:
            payload = comment[4:-3]
        payload = re.sub(r'(?m)^[ \t]*\*[ \t]?', '', payload).strip()
        if payload in {'...', '…'} or directive.search(payload):
            return True
    return False


def _git_mode(mode: int) -> int:
    """Normalize permissions to Git's regular-file mode using the owner execute bit."""
    return 0o755 if mode & 0o100 else 0o644


def _without_comments(text: str, suffix: str) -> str:
    """Remove source comments outside quotes while preserving all literal string bytes."""
    result: list[str] = []
    index, quote = 0, ''
    markers: list[tuple[str, str | None]] = [('/*', '*/')]
    if suffix in JS_SUFFIXES:
        markers.append(('//', None))
    if suffix in {'.html', '.htm'}:
        markers.append(('<!--', '-->'))
    while index < len(text):
        char = text[index]
        if quote:
            result.append(char)
            if char == '\\' and index + 1 < len(text):
                index += 1
                result.append(text[index])
            elif char == quote:
                quote = ''
            index += 1
            continue
        if char in {'\'', '"'}:
            quote = char
            result.append(char)
            index += 1
            continue
        marker = next(((left, right) for left, right in markers if text.startswith(left, index)), None)
        if marker:
            if marker[1] is None:
                close = text.find('\n', index + len(marker[0]))
                if close < 0:
                    break
                index = close
                continue
            close = text.find(marker[1], index + len(marker[0]))
            if close < 0:
                return text
            index = close + len(marker[1])
            continue
        result.append(char)
        index += 1
    return ''.join(result)


def comment_only_change(before: str, after: str, suffix: str) -> bool:
    """Recognize only comment edits, never comment-looking literal strings, in CSS and HTML."""
    return (before != after and _without_comments(before, suffix).rstrip('\r\n')
            == _without_comments(after, suffix).rstrip('\r\n'))


def comment_only_source(text: str, suffix: str) -> bool:
    """Reject a non-delete patch whose complete final file contains only whitespace and comments."""
    return suffix in COMMENT_SUFFIXES and not _without_comments(text, suffix).strip()

def line_window(text: str, query: str, maximum: int) -> tuple[str, int]:
    """Select a contiguous complete-line window without inventing missing source."""
    if len(text) <= maximum:
        return text, 0
    excerpt, start = context_excerpt(text, query, maximum)
    left = text.rfind('\n', 0, start) + 1
    right = text.rfind('\n', left, start + len(excerpt)) + 1
    if right <= left or right - left > maximum * 2:
        return '', 0
    return text[left:right], left


@dataclass(frozen=True, slots=True)
class SourceRepository:
    """Retrieve bounded text before optional semantic analysis; never execute repository code."""
    config: ExperimentConfig

    def retrieve(self, snapshot: RepositorySnapshot, query: str, context: RunContext) -> EditScope:
        """Rank production text files and expose hash-bound, line-aligned editing regions."""
        ranked = []
        diagnostics = []
        count = 0
        for directory, children, names in os.walk(snapshot.root, followlinks=False):
            children[:] = sorted(d for d in children if d not in EXCLUDED | TEST_PARTS
                                 and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                context.budget.check_deadline()
                path = Path(directory) / name
                relative = path.relative_to(snapshot.root).as_posix()
                if path.is_symlink() or not allowed_source(relative):
                    continue
                count += 1
                if count > self.config.integration.max_files:
                    diagnostics.append('catalog_file_limit')
                    break
                if path.stat().st_size > self.config.integration.max_file_bytes:
                    diagnostics.append('oversize_text:' + relative)
                    continue
                data = path.read_bytes()
                decoded = decode_source(data)
                if decoded is None:
                    diagnostics.append('unsupported_encoding_or_binary:' + relative)
                    continue
                text, encoding = decoded
                score = location_score(query, relative, '', text)
                ranked.append((source_priority(query, relative), score, relative, text, encoding,
                               hashlib.sha256(data).hexdigest(), path.stat().st_mode & 0o777))
                if len(ranked) > self.config.integration.context_files * 2:
                    ranked = sorted(ranked, key=lambda row: (row[0], -row[1], row[2]))[
                        :self.config.integration.context_files
                    ]
            if count > self.config.integration.max_files:
                break
        selected = sorted(ranked, key=lambda row: (row[0], -row[1], row[2]))[
            :self.config.integration.context_files
        ]
        states, regions = [], []
        remaining = self.config.integration.max_context_chars
        for ordinal, (_, _, path, text, encoding, digest, mode) in enumerate(selected):
            allowance = max(1, remaining // (len(selected) - ordinal))
            excerpt, start = line_window(text, query, allowance)
            if not excerpt and text:
                diagnostics.append('no_complete_line_in_context:' + path)
                continue
            before, body = text[:start].encode(encoding), excerpt.encode(encoding)
            file_id, region_id = f'F{ordinal:03d}', f'R{ordinal:03d}'
            size = len(text.encode(encoding))
            complete = start == 0 and len(excerpt) == len(text)
            states.append(FileState(file_id, path, digest, size, mode, encoding, complete))
            regions.append(EditRegion(region_id, file_id, path, len(before), len(before) + len(body),
                                      text[:start].count('\n') + 1, excerpt, hashlib.sha256(body).hexdigest(), start))
            remaining -= len(excerpt)
            if not complete:
                diagnostics.append('partial_source_context:' + path)
        parents = tuple(sorted({str(PurePosixPath(f.path).parent) for f in states}))
        return EditScope(tuple(states), tuple(regions), parents, tuple(diagnostics))


def bind_edit_blocks(snapshot: RepositorySnapshot, scope: EditScope, analysis: dict,
                     query: str, maximum: int = 128) -> EditScope:
    """Bind bounded syntax units inside retrieved windows; unsupported windows retain exact-text editing."""
    files = {file.file_id: file for file in scope.files}
    rows = {row['path']: row for row in analysis['files']}
    originals = validate_scope(snapshot, scope)
    regions, blocks = [], []
    diagnostics = list(scope.diagnostics)
    unit_kinds = {'FunctionDeclaration', 'MethodDeclaration', 'Constructor',
                  'GetAccessor', 'SetAccessor'}
    for ordinal, region in enumerate(scope.regions):
        file = files[region.file_id]
        row = rows.get(region.path, {})
        candidates = []
        if row.get('syntax_status') == 'passed':
            source = originals[file.path].decode(file.encoding)
            utf8 = source.encode('utf-8')
            for item in row.get('edit_blocks', ()):
                left, right = item['start_byte'], item['end_byte']
                if not 0 <= left < right <= len(utf8):
                    raise ValidationError('invalid_parser_block_range')
                text = utf8[left:right].decode('utf-8')
                if hashlib.sha256(utf8[left:right]).hexdigest() != item['content_sha256']:
                    raise ValidationError('stale_parser_block_hash')
                start = left if file.encoding == 'utf-8' else len(utf8[:left].decode('utf-8').encode(file.encoding))
                end = right if file.encoding == 'utf-8' else start + len(text.encode(file.encoding))
                if not region.start_byte <= start < end <= region.end_byte:
                    continue
                score = location_score(query, file.path, item['symbol'], text)
                candidates.append((item['node_kind'] not in unit_kinds, -score, end - start,
                                   start, end, item['node_kind'], item['symbol']))
        allowance = max(0, (maximum - len(blocks)) // (len(scope.regions) - ordinal))
        selected = sorted(candidates)[:allowance]
        if len(candidates) > len(selected):
            diagnostics.append('edit_block_limit:' + file.path)
        for _, _, _, start, end, kind, symbol in sorted(selected, key=lambda item: (item[3], item[4])):
            blocks.append(EditBlock(f'B{len(blocks):03d}', region.region_id, start, end,
                                    hashlib.sha256(originals[file.path][start:end]).hexdigest(), kind, symbol))
        regions.append(replace(region, edit_mode='syntax' if selected else 'text'))
        if not selected:
            diagnostics.append('text_edit_fallback:' + file.path)
    return replace(scope, regions=tuple(regions), blocks=tuple(blocks), diagnostics=tuple(diagnostics))


def validate_scope(snapshot: RepositorySnapshot, scope: EditScope) -> dict[str, bytes]:
    """Recheck every declared original file and region before interpreting any transaction."""
    if len({f.file_id for f in scope.files}) != len(scope.files) or len({f.path for f in scope.files}) != len(scope.files):
        raise ValidationError('duplicate_scope_file')
    if len({r.region_id for r in scope.regions}) != len(scope.regions):
        raise ValidationError('duplicate_scope_region')
    originals: dict[str, bytes] = {}
    files = {file.file_id: file for file in scope.files}
    for file in scope.files:
        if not allowed_source(file.path, for_edit=True):
            raise ValidationError('forbidden_scope_file')
        data = safe_path(snapshot.root, file.path).read_bytes()
        if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256:
            raise ValidationError('stale_source_hash')
        decoded = decode_source(data)
        if decoded is None or decoded[1] != file.encoding:
            raise ValidationError('scope_encoding_mismatch')
        originals[file.path] = data
    spans: dict[str, list[tuple[int, int]]] = {}
    for region in scope.regions:
        file = files.get(region.file_id)
        if (file is None or file.path != region.path or region.edit_mode not in {'text', 'syntax'}
                or type(region.start_char) is not int
                or region.start_char < 0 or not 0 <= region.start_byte <= region.end_byte <= file.size):
            raise ValidationError('invalid_scope_region')
        data = originals[file.path]
        raw = data[region.start_byte:region.end_byte]
        try:
            prefix = data[:region.start_byte].decode(file.encoding)
        except UnicodeDecodeError as exc:
            raise ValidationError('scope_encoding_mismatch') from exc
        if (raw != region.source.encode(file.encoding) or hashlib.sha256(raw).hexdigest() != region.sha256
                or len(prefix) != region.start_char):
            raise ValidationError('stale_region_hash')
        if prefix.count('\n') + 1 != region.start_line:
            raise ValidationError('scope_line_mismatch')
        if file.complete and not any(candidate.file_id == file.file_id and candidate.start_byte == 0
                                     and candidate.end_byte == file.size for candidate in scope.regions):
            raise ValidationError('incomplete_file_authority')
        for left, right in spans.setdefault(file.path, []):
            if region.start_byte < right and left < region.end_byte:
                raise ValidationError('overlapping_scope_regions')
        spans[file.path].append((region.start_byte, region.end_byte))
    regions = {region.region_id: region for region in scope.regions}
    if (len({block.block_id for block in scope.blocks}) != len(scope.blocks)
            or {block.block_id for block in scope.blocks} & (regions.keys() | files.keys())):
        raise ValidationError('duplicate_edit_block')
    for block in scope.blocks:
        region = regions.get(block.region_id)
        if (region is None or region.edit_mode != 'syntax'
                or not region.start_byte <= block.start_byte < block.end_byte <= region.end_byte):
            raise ValidationError('edit_block_outside_region')
        data = originals[region.path]
        if hashlib.sha256(data[block.start_byte:block.end_byte]).hexdigest() != block.sha256:
            raise ValidationError('stale_edit_block_hash')
    return originals


def transaction_contents(snapshot: RepositorySnapshot, scope: EditScope, transaction: EditTransaction,
                         max_file_bytes: int = 512000) -> tuple[dict[str, bytes], dict[str, bytes | None]]:
    """Validate one atomic transaction and return bounded final bytes without touching the base tree."""
    if type(max_file_bytes) is not int or max_file_bytes < 1:
        raise ValidationError('invalid_max_file_bytes')
    originals = validate_scope(snapshot, scope)
    if any(len(data) > max_file_bytes for data in originals.values()):
        raise ValidationError('source_file_too_large')
    regions = {region.region_id: region for region in scope.regions}
    blocks = {block.block_id: block for block in scope.blocks}
    files = {file.file_id: file for file in scope.files}
    patches: dict[str, list[tuple[int, int, bytes]]] = {}
    exclusive: dict[str, None] = {}
    destinations: dict[str, bytes] = {}
    if not transaction.edits:
        raise NoAdmissiblePatch('empty_edit_transaction')
    for edit in transaction.edits:
        if not all(isinstance(value, str) for value in (edit.operation, edit.target, edit.new_text, edit.destination, edit.old_text)):
            raise ValidationError('invalid_edit_text')
        if '\x00' in edit.new_text or '\x00' in edit.old_text:
            raise ValidationError('invalid_edit_text')
        if edit.operation != 'replace_text' and edit.old_text:
            raise ValidationError('unexpected_search_text')
        if edit.operation in {'replace_block', 'insert_before', 'insert_after', 'replace_text'}:
            if edit.first_line is not None or edit.last_line is not None or edit.destination:
                raise ValidationError('unexpected_edit_coordinates')
            if edit.operation == 'replace_text':
                block = blocks.get(edit.target)
                if block is not None:
                    region = regions[block.region_id]
                    start, end = block.start_byte, block.end_byte
                else:
                    region = regions.get(edit.target)
                    if region is None:
                        raise ValidationError('unknown_search_target')
                    if region.edit_mode != 'text':
                        raise ValidationError('text_edit_not_declared')
                    start, end = region.start_byte, region.end_byte
                file = files[region.file_id]
                search_source = originals[file.path][start:end].decode(file.encoding)
                if not edit.old_text and search_source:
                    raise ValidationError('empty_search_text')
                offset = search_source.find(edit.old_text)
                if offset < 0:
                    raise ValidationError('search_text_not_found')
                if edit.old_text and search_source.find(edit.old_text, offset + 1) >= 0:
                    raise ValidationError('ambiguous_search_text')
                start += len(search_source[:offset].encode(file.encoding))
                end = start + len(edit.old_text.encode(file.encoding))
            else:
                block = blocks.get(edit.target)
                if block is None:
                    raise ValidationError('unknown_edit_block')
                region = regions[block.region_id]
                file = files[region.file_id]
                start, end = block.start_byte, block.end_byte
                if edit.operation == 'insert_before':
                    end = start
                elif edit.operation == 'insert_after':
                    start = end
            old = originals[file.path][start:end].decode(file.encoding)
            newline = '\r\n' if '\r\n' in region.source else '\n'
            text = edit.new_text.replace('\r\n', '\n').replace('\n', newline)
            if edit.operation != 'replace_text' and text and old.endswith('\n') and not text.endswith('\n'):
                text += newline
            if omission_placeholder(text, old):
                raise ValidationError('omission_placeholder')
            patches.setdefault(file.path, []).append((start, end, encode_edit_text(
                text, file.encoding, originals[file.path], start, end)))
        elif edit.operation in {'replace_region', 'replace_lines', 'insert_at'}:
            if edit.destination:
                raise ValidationError('unexpected_edit_destination')
            region = regions.get(edit.target)
            if region is None:
                raise ValidationError('unknown_edit_region')
            if region.edit_mode != 'text':
                raise ValidationError('text_edit_not_declared')
            file = files[region.file_id]
            lines = region.source.splitlines(keepends=True)
            first, last = edit.first_line, edit.last_line
            start, end = region.start_byte, region.end_byte
            if edit.operation == 'replace_region':
                if first is not None or last is not None:
                    raise ValidationError('unexpected_line_range')
            elif edit.operation == 'replace_lines':
                if (type(first) is not int or type(last) is not int
                        or not region.start_line <= first <= last < region.start_line + len(lines)):
                    raise ValidationError('edit_line_range_outside_scope')
                start += len(''.join(lines[:first - region.start_line]).encode(file.encoding))
                end = region.start_byte + len(''.join(lines[:last - region.start_line + 1]).encode(file.encoding))
            else:
                if (type(first) is not int or last is not None
                        or not region.start_line <= first <= region.start_line + len(lines)):
                    raise ValidationError('insertion_outside_scope')
                start += len(''.join(lines[:first - region.start_line]).encode(file.encoding))
                end = start
            old = originals[file.path][start:end].decode(file.encoding)
            text = edit.new_text
            if edit.operation in {'replace_lines', 'insert_at'}:
                newline = '\r\n' if '\r\n' in region.source else '\n'
                text = text.replace('\r\n', '\n').replace('\n', newline)
                if edit.operation == 'insert_at' and start == region.end_byte and region.source and not region.source.endswith('\n'):
                    text = newline + text
                if text and (old.endswith('\n') or edit.operation == 'insert_at') and not text.endswith('\n'):
                    text += newline
            if omission_placeholder(text, old):
                raise ValidationError('omission_placeholder')
            patches.setdefault(file.path, []).append((start, end, encode_edit_text(
                text, file.encoding, originals[file.path], start, end)))
        elif edit.operation in {'delete_file', 'rename_file', 'create_file'}:
            if edit.first_line is not None or edit.last_line is not None:
                raise ValidationError('unexpected_line_range')
            if edit.operation != 'create_file':
                file = files.get(edit.target)
                if file is None or not file.complete or file.path in exclusive or edit.new_text:
                    raise ValidationError('invalid_file_operation')
                exclusive[file.path] = None
            if edit.operation == 'delete_file':
                if edit.destination:
                    raise ValidationError('unexpected_edit_destination')
                continue
            destination = edit.target if edit.operation == 'create_file' else edit.destination
            if edit.operation == 'create_file' and edit.destination:
                raise ValidationError('unexpected_edit_destination')
            path = safe_path(snapshot.root, destination)
            parent = str(PurePosixPath(destination).parent)
            if not allowed_source(destination, for_edit=True) or parent not in scope.creation_roots:
                raise ValidationError('creation_outside_scope')
            if path.exists() or destination in destinations:
                raise ValidationError('destination_exists')
            text = edit.new_text
            if omission_placeholder(text, ''):
                raise ValidationError('omission_placeholder')
            destinations[destination] = text.encode('utf-8') if edit.operation == 'create_file' else originals[file.path]
        else:
            raise ValidationError('unknown_edit_operation')
    if patches.keys() & exclusive.keys():
        raise ValidationError('conflicting_file_operations')
    updated: dict[str, bytes | None] = {**exclusive, **destinations}
    for path, changes in patches.items():
        ordered = sorted(changes)
        for (left, right, _), (next_left, _, _) in zip(ordered, ordered[1:]):
            if right > next_left or left == right == next_left:
                raise ValidationError('overlapping_transaction_edits')
        data = originals[path]
        for left, right, replacement in reversed(ordered):
            data = data[:left] + replacement + data[right:]
        updated[path] = data
    updated = {path: data for path, data in updated.items() if data != originals.get(path)}
    if not updated:
        raise NoAdmissiblePatch('no_op_patch')
    for path, data in updated.items():
        if data is None:
            continue
        decoded = decode_source(data)
        if len(data) > max_file_bytes or decoded is None:
            raise ValidationError('generated_text_encoding_or_size_invalid')
        if comment_only_source(decoded[0], PurePosixPath(path).suffix.lower()):
            raise NoAdmissiblePatch('comment_only_patch')
    return originals, updated


@dataclass(frozen=True, slots=True)
class PatchCompiler:
    """Compile authorized edits into an applicable Git diff without running target code."""
    config: ExperimentConfig

    def compile(self, task: TaskInput, plan: PatchPlan, transaction: EditTransaction,
                snapshot: RepositorySnapshot, context: RunContext) -> PatchArtifact:
        """Check all edits, emit one diff, and prove application recreates final bytes from pristine."""
        context.budget.check_deadline()
        if snapshot.base_commit != task.base_commit:
            raise ValidationError('base_commit_mismatch')
        scope = plan.edit_scope
        if scope is None:
            raise ValidationError('missing_edit_scope')
        if tree_digest(snapshot.root) != snapshot.tree_sha256:
            raise ValidationError('snapshot_changed')
        originals, updated = transaction_contents(snapshot, scope, transaction,
                                                  self.config.integration.max_file_bytes)
        syntax, diagnostics = self.check_syntax(originals, updated, context)
        with TemporaryDirectory(prefix='boundary-patch-') as temporary:
            root = Path(temporary)
            working, pristine = root / 'working', root / 'pristine'
            working.mkdir()
            pristine.mkdir()
            states = {file.path: file for file in scope.files}
            state_ids = {file.file_id: file for file in scope.files}
            renamed_modes = {edit.destination: state_ids[edit.target].mode for edit in transaction.edits
                             if edit.operation == 'rename_file'}
            created = {edit.target for edit in transaction.edits if edit.operation == 'create_file'}
            expected_modes = {path: _git_mode(state.mode) for path, state in states.items()}
            expected_modes.update({path: _git_mode(mode) for path, mode in renamed_modes.items()})
            expected_modes.update({path: 0o644 for path in created})
            for path, data in originals.items():
                for directory in (working, pristine):
                    target = safe_path(directory, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(expected_modes[path])

            def git(*args: str) -> bytes:
                """Run only the system Git binary with repository conversions and prompts disabled."""
                result = run_process(['git', '-c', 'core.autocrlf=false', '-c', 'core.quotePath=false', *args],
                                     cwd=working, timeout=min(30, context.budget.remaining_seconds()))
                if result.returncode:
                    raise ValidationError('patch_git_operation_failed')
                return result.stdout

            git('init', '-q')
            pristine_init = run_process(['git', '-c', 'core.autocrlf=false', 'init', '-q'], cwd=pristine,
                                        timeout=min(30, context.budget.remaining_seconds()))
            if pristine_init.returncode:
                raise ValidationError('patch_git_operation_failed')
            attributes_text = '* -text -eol -filter -ident -working-tree-encoding\n'
            for directory in (working, pristine):
                (directory / '.git' / 'info' / 'attributes').write_text(attributes_text, encoding='utf-8')
            git('add', '-f', '--all', '--', '.')
            for path, data in updated.items():
                target = safe_path(working, path)
                if data is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    if path not in originals:
                        target.chmod(expected_modes[path])
            created_paths = sorted(set(updated) - set(originals))
            if created_paths:
                git('add', '-f', '-N', '--', *created_paths)
            diff_bytes = git('diff', '--binary', '--no-ext-diff', '--no-textconv', '--no-renames')
            try:
                diff = diff_bytes.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ValidationError('generated_patch_not_utf8') from exc
            if not diff.strip():
                raise NoAdmissiblePatch('empty_git_diff')
            for arguments, failure in ((['git', '-c', 'core.autocrlf=false', 'apply', '--check', '-'],
                                       'generated_patch_not_applicable'),
                                      (['git', '-c', 'core.autocrlf=false', 'apply', '-'],
                                       'generated_patch_apply_failed')):
                applied = run_process(arguments, cwd=pristine, input_data=diff_bytes,
                                      timeout=min(30, context.budget.remaining_seconds()))
                if applied.returncode:
                    raise ValidationError(failure)
            expected = {**originals, **updated}
            if os.name == 'nt':
                diagnostics.append('patch_mode_unverified_windows')
            for path, data in expected.items():
                target = safe_path(pristine, path)
                if data is None:
                    if target.exists():
                        raise ValidationError('generated_patch_bytes_mismatch')
                    continue
                if not target.is_file() or target.read_bytes() != data:
                    raise ValidationError('generated_patch_bytes_mismatch')
                if os.name != 'nt' and _git_mode(target.stat().st_mode) != expected_modes[path]:
                    raise ValidationError('generated_patch_mode_mismatch')
        if tree_digest(snapshot.root) != snapshot.tree_sha256:
            raise ValidationError('snapshot_changed')
        return PatchArtifact(task.instance_id, task.base_commit, plan.plan_id, diff,
                             hashlib.sha256(diff_bytes).hexdigest(), 'passed', syntax,
                             plan.generation_mode, tuple(diagnostics))

    def check_syntax(self, originals: dict[str, bytes], updated: dict[str, bytes | None],
                     context: RunContext) -> tuple[str, list[str]]:
        """Reject new supported syntax errors; parser limits and non-JS syntax stay explicitly unknown."""
        from boundary_repair.adapters.frontend import analyze_sources

        diagnostics: list[str] = []
        checked, unchanged = 0, 0
        for path, data in updated.items():
            if data is None:
                continue
            decoded = decode_source(data)
            if decoded is None:
                raise ValidationError('generated_text_encoding_invalid')
            text, _ = decoded
            suffix = PurePosixPath(path).suffix.lower()
            before_decoded = decode_source(originals.get(path, b''))
            before = before_decoded[0] if before_decoded is not None else ''
            if suffix in JS_SUFFIXES:
                base_rows = analyze_sources([{'path': path, 'source': before}], self.config, context)['files']
                after_rows = analyze_sources([{'path': path, 'source': text}], self.config, context)['files']
                if len(base_rows) != 1 or len(after_rows) != 1:
                    diagnostics.append('syntax_unknown:' + path)
                    continue
                base, after = base_rows[0], after_rows[0]
                base_errors = tuple(base.get('unsupported', ()))
                after_errors = tuple(after.get('unsupported', ()))
                base_complete = base.get('analysis_status') == 'complete' and not base_errors
                after_complete = after.get('analysis_status') == 'complete' and not after_errors
                base_syntax_valid = base.get('syntax_status') == 'passed' or base_complete
                if base_syntax_valid and any(error.startswith('syntax:') for error in after_errors):
                    from boundary_repair.adapters.storage import safe_component, write_json
                    destination = (self.config.results_root / safe_component(context.run_id) / 'cases'
                                   / safe_component(context.instance_id) / 'trajectory' / 'rejected_syntax.json')
                    write_json(destination, {'status': 'rejected', 'path': path,
                        'original_sha256': hashlib.sha256(originals.get(path, b'')).hexdigest(),
                        'generated_sha256': hashlib.sha256(data).hexdigest(),
                        'diagnostics': after.get('syntax_diagnostics', []), 'unsupported': after_errors})
                    raise ValidationError('generated_syntax_invalid')
                if not base_complete or not after_complete:
                    diagnostics.append('syntax_unknown:' + path)
                    continue
                checked += 1
                if base.get('semantic_hash') and base['semantic_hash'] == after.get('semantic_hash'):
                    unchanged += 1
            elif suffix == '.json':
                if before_decoded is None:
                    diagnostics.append('syntax_unknown:' + path)
                    continue
                try:
                    json.loads(before.lstrip('﻿'))
                except ValueError:
                    diagnostics.append('syntax_unknown:' + path)
                    continue
                try:
                    json.loads(text.lstrip('﻿'))
                except ValueError as exc:
                    raise ValidationError('generated_json_invalid') from exc
            elif suffix in {'.css', '.scss', '.html', '.htm'} and comment_only_change(before, text, suffix):
                checked += 1
                unchanged += 1
            else:
                diagnostics.append('syntax_unknown:' + path)
        if len(updated) == checked == unchanged and checked:
            raise NoAdmissiblePatch('no_executable_change')
        return ('unknown' if diagnostics else 'passed'), diagnostics
