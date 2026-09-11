"""Pure file policy and hash-bound source slices shared by retrieval and patch application."""
import hashlib
from pathlib import Path, PurePosixPath

from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.task import SourceSpan

EXCLUDED = {'.git', '.hg', '.svn', 'node_modules', 'vendor', 'coverage', '.cache',
            '__pycache__', 'dist', 'build', 'fixtures', '__snapshots__'}
TEST_PARTS = {'test', 'tests', '__tests__', 'testing', 'e2e', 'cypress'}
SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.css', '.scss', '.json', '.html', '.vue', '.svelte', '.r', '.R'}


def allowed_source(relative: str, for_edit: bool = False) -> bool:
    """Filter dependencies, secrets, tests and generated outputs; edit policy is fail-closed."""
    path = PurePosixPath(relative)
    parts = path.parts
    if (not parts or path.is_absolute() or '\\' in relative or ':' in relative
            or any(p in {'', '.', '..'} for p in relative.split('/'))):
        return False
    if any(p in EXCLUDED or p in TEST_PARTS or p.startswith('.env') for p in parts):
        return False
    name = parts[-1]
    if any(x in name for x in ('.min.', '.bundle.', '.test.', '.spec.')):
        return False
    if (name.lower() in {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'evaluation.json',
                         '.npmrc', '.pypirc', '.netrc', 'credentials', 'id_rsa', 'id_ed25519'}
            or any(p.lower() in {'.ssh', '.aws', '.gnupg'} for p in parts)
            or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.keystore'}):
        return False
    return True


def safe_path(root: Path, relative: str) -> Path:
    """Resolve only normalized relative paths with no symlinks anywhere in their ancestry."""
    path = PurePosixPath(relative)
    if path.is_absolute() or '\\' in relative or ':' in relative or not path.parts or any(p in {'.', '..'} for p in path.parts):
        raise ValidationError('unsafe_source_path')
    current = root.resolve()
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError('symlink_source_rejected')
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValidationError('source_path_escape')
    return resolved


def source_slice(root: Path, span: SourceSpan) -> tuple[bytes, int, int]:
    """Verify a byte-precise or legacy line span against the original content SHA256."""
    path = safe_path(root, span.path)
    data = path.read_bytes()
    if (span.start_byte is None) != (span.end_byte is None):
        raise ValidationError('incomplete_byte_span')
    if span.start_byte is not None:
        start, end = span.start_byte, span.end_byte
    else:
        lines = data.splitlines(keepends=True)
        if not 1 <= span.start_line <= span.end_line <= len(lines):
            raise ValidationError('invalid_line_span')
        start, end = sum(map(len, lines[:span.start_line-1])), sum(map(len, lines[:span.end_line]))
    if not 0 <= start <= end <= len(data):
        raise ValidationError('invalid_byte_span')
    segment = data[start:end]
    if hashlib.sha256(segment).hexdigest() != span.content_sha256:
        raise ValidationError('stale_source_hash')
    segment.decode('utf-8')
    return data, start, end


def tree_digest(root: Path) -> str:
    """Hash every regular file in a sanitized snapshot; refuse links rather than following them."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValidationError('snapshot_symlink')
        if path.is_file():
            name = path.relative_to(root).as_posix().encode()
            content_hash = hashlib.sha256(path.read_bytes()).digest()
            digest.update(len(name).to_bytes(8, 'big') + name + content_hash)
    return digest.hexdigest()
