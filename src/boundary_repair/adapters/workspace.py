"""Immutable base archives: explicit local Git exports for tests, digest-pinned Docker for server runs."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import tarfile
from tempfile import TemporaryDirectory
from typing import Iterator, Any
from uuid import uuid4

from boundary_repair.adapters.process import run_process
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.codec import strict_json
from boundary_repair.kernel.files import allowed_source, safe_path, tree_digest


def validate_commit(commit: str) -> str:
    """Allow only full hexadecimal Git object ids; never options, branch names or revision expressions."""
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', commit):
        raise ValidationError('full_base_commit_required')
    return commit.lower()


def read_instance_manifest(path: Path | None, task: TaskInput) -> dict[str, Any]:
    """Bind one task to an explicit repo/base/image entry, not a guessed Docker name."""
    if path is None or not path.is_file():
        raise ConfigurationError('image_manifest_missing')
    data = strict_json(path.read_text(encoding='utf-8'))
    item = data.get('instances', {}).get(task.instance_id)
    if not isinstance(item, dict) or item.get('repo') != task.repo or item.get('base_commit') != task.base_commit:
        raise ConfigurationError('image_manifest_task_mismatch')
    image = item.get('image')
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}', image):
        raise ConfigurationError('image_digest_required')
    repo_path = item.get('repository_path', '/testbed')
    if not isinstance(repo_path, str) or not repo_path.startswith('/') or '..' in PurePosixPath(repo_path).parts:
        raise ConfigurationError('invalid_container_repository_path')
    return item


def extract_sources(archive: Path, destination: Path, max_bytes: int) -> None:
    """Extract only allowed regular production files; reject tar traversal and source symlinks.

    Filtering is deliberate: dependencies, tests, history and secrets never enter generator
    snapshots. Duplicate members and total extracted bytes are checked before each write.
    """
    total, seen = 0, set()
    with tarfile.open(archive, mode='r:*') as stream:
        for item in stream:
            raw = item.name.rstrip('/')
            if not raw:
                continue
            path = PurePosixPath(raw)
            if path.is_absolute() or '..' in path.parts or '\\' in raw or ':' in raw:
                raise ValidationError('unsafe_archive_member')
            if item.isdir() or not allowed_source(raw):
                continue
            if not item.isfile():
                raise ValidationError('source_archive_link_or_device')
            if raw in seen:
                raise ValidationError('duplicate_archive_member')
            total += item.size
            if total > max_bytes:
                raise ValidationError('workspace_size_limit')
            seen.add(raw)
            output = safe_path(destination, raw)
            output.parent.mkdir(parents=True, exist_ok=True)
            source = stream.extractfile(item)
            if source is None:
                raise ValidationError('archive_member_unreadable')
            with source, output.open('xb') as target:
                remaining = item.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValidationError('truncated_archive_member')
                    target.write(chunk)
                    remaining -= len(chunk)
            output.chmod(0o755 if item.mode & 0o111 else 0o644)
    if not seen:
        raise ValidationError('empty_production_snapshot')


@dataclass(frozen=True, slots=True)
class GitArchiveWorkspaceAdapter:
    """Explicit local development mode. It is not represented as a Docker-isolated benchmark run."""
    config: ExperimentConfig

    @contextmanager
    def open_base(self, task: TaskInput, context: RunContext) -> Iterator[RepositorySnapshot]:
        """Verify an exact commit and export a sanitized tree; never checkout or mutate the source repo."""
        commit = validate_commit(task.base_commit)
        manifest = self.config.integration.repository_manifest
        if manifest is None or not manifest.is_file():
            raise ConfigurationError('repository_manifest_missing')
        data = strict_json(manifest.read_text(encoding='utf-8'))
        entry = data.get('repositories', {}).get(task.repo)
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ConfigurationError('repository_manifest_entry_missing')
        raw = Path(entry['path']).expanduser()
        repo = (raw if raw.is_absolute() else manifest.parent / raw).resolve()
        if not repo.is_dir():
            raise ConfigurationError('repository_directory_missing')
        prefix = ['git']
        if entry.get('trust_directory') is True:
            prefix += ['-c', 'safe.directory=' + str(repo)]
        prefix += ['-C', str(repo)]
        verified = run_process(prefix + ['rev-parse', '--verify', commit + '^{commit}'],
                               timeout=min(30, context.budget.remaining_seconds()))
        if verified.returncode or verified.stdout.decode().strip().lower() != commit:
            raise ConfigurationError('base_commit_not_available_or_directory_untrusted')
        with TemporaryDirectory(prefix='boundary-base-') as temporary:
            root = Path(temporary)
            archive, snapshot = root / 'base.tar', root / 'source'
            snapshot.mkdir()
            result = run_process(prefix + ['archive', '--format=tar', '--output=' + str(archive), commit],
                                 timeout=min(120, context.budget.remaining_seconds()))
            if result.returncode:
                raise ExternalServiceError('git_archive_failed')
            extract_sources(archive, snapshot, self.config.integration.max_workspace_bytes)
            context.budget.check_deadline()
            yield RepositorySnapshot(snapshot, task.base_commit, tree_digest(snapshot))


@dataclass(frozen=True, slots=True)
class DockerWorkspaceAdapter:
    """Run only trusted Git inside a pinned container; expose a sanitized base archive to analysis."""
    config: ExperimentConfig

    @contextmanager
    def open_base(self, task: TaskInput, context: RunContext) -> Iterator[RepositorySnapshot]:
        """A7: export the exact commit in a restricted container and remove the container on all paths.

        No API keys, .env, raw dataset, baseline directory or Docker socket are mounted. The
        analysis host parses text only; it never runs target source. Repository history remains
        inaccessible to the model because only production files from git archive are extracted.
        """
        commit = validate_commit(task.base_commit)
        entry = read_instance_manifest(self.config.image_manifest, task)
        image, repo = entry['image'], entry.get('repository_path', '/testbed')
        inspected = run_process(['docker', 'image', 'inspect', image], timeout=min(30, context.budget.remaining_seconds()))
        if inspected.returncode:
            raise ConfigurationError('pinned_docker_image_not_present')
        name = 'boundary-base-' + uuid4().hex
        with TemporaryDirectory(prefix='boundary-docker-') as temporary:
            root = Path(temporary)
            archive, snapshot = root / 'base.tar', root / 'source'
            snapshot.mkdir()
            args = ['docker', 'run', '--name', name, '--pull=never', '--network=none', '--read-only',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=2g', '--cpus=2',
                    '--entrypoint=git', image, '-c', 'safe.directory=' + repo, '-C', repo,
                    'archive', '--format=tar', commit]
            try:
                result = run_process(args, timeout=min(180, context.budget.remaining_seconds()),
                                     output_file=archive, max_output=self.config.integration.max_workspace_bytes)
                if result.returncode:
                    raise ExternalServiceError('docker_base_archive_failed')
                extract_sources(archive, snapshot, self.config.integration.max_workspace_bytes)
                context.budget.check_deadline()
                yield RepositorySnapshot(snapshot, task.base_commit, tree_digest(snapshot))
            finally:
                # Cleanup gets its own small grace period even after the task budget is exhausted.
                try:
                    run_process(['docker', 'rm', '-f', name], timeout=15)
                except (OSError, ExternalServiceError):
                    pass
