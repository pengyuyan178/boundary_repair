"""Build a source-only, credential-free snapshot for isolated verification and a frozen dev batch."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def main() -> None:
    """Archive explicit code/test/dependency roots without outputs, credentials, or benchmark answers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    roots = ['src', 'tests', 'configs', 'scripts', 'examples', 'docs', 'node_modules/typescript']
    paths = [p for name in roots for p in (root / name).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('.env')
             and p.suffix not in {'.pyc', '.pyo'}]
    paths += [root / name for name in ('pyproject.toml', 'package.json', 'package-lock.json', 'README.md')]
    paths += [Path(__file__).resolve(), *(Path(__file__).with_name(name) for name in
               ('run_checks.py', 'run_docker.sh', 'prepare_dev_batch.py', 'run_dev.sh', 'audit_run.py'))]
    paths = sorted(set(paths))
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
        for path in paths:
            if path.is_symlink():
                raise ValueError('Snapshot may contain only regular explicit source files')
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    state = {'files': len(manifest), 'archive_sha256': hashlib.sha256((output / 'source.tar.gz').read_bytes()).hexdigest(),
             'git_head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
             'git_status': subprocess.check_output(['git', '-C', str(root), 'status', '--short'], text=True),
             'benchmark_answers_included': False, 'credentials_included': False}
    for name, value in (('source_files.sha256.json', manifest), ('source_state.json', state)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
