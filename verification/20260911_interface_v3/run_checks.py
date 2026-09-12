"""Reproducible v3 checks over synthetic inputs, without benchmark or provider access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main() -> int:
    """Check schema, complete tests, and real synthetic patch application in a fresh directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join((str(root / 'src'), environment_path()))}
    sources = sorted(p for directory in ('src', 'tests', 'scripts', 'examples')
                     for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    mask = os.umask(0)
    os.umask(mask)
    record = {'benchmark': False, 'real_model_requests': 0, 'formal_evaluation': False,
              'seed': 42, 'python': sys.version, 'umask': oct(mask), 'source_sha256': hashes, 'checks': []}
    commands = [
        ('node', ['node', '--version']),
        ('typescript', ['node', '-e', 'console.log(require(process.argv[1]).version)', str(root / 'node_modules/typescript')]),
        ('pillow', [sys.executable, '-c', 'import PIL; print(PIL.__version__)']),
        ('tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']),
        ('smoke', [sys.executable, 'scripts/smoke.py', '--output', str(output / 'smoke')]),
    ]
    for name, command in commands:
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True,
                                text=True, encoding='utf-8', timeout=600)
        log = result.stdout + result.stderr
        (output / (name + '.log')).write_text(log, encoding='utf-8')
        check = {'name': name, 'command': command, 'exit_code': result.returncode}
        if name == 'tests':
            match = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
            skipped = re.findall(
                r"^(\w+) \(([^)\r\n]+)\)(?:\r?\n[^\r\n]*?)? \.\.\. skipped '([^']+)'$",
                log, re.MULTILINE,
            )
            permitted = {'test_git_mode_verification_normalizes_umask_created_files',
                         'test_compiler_rejects_patch_that_changes_executable_bit'} if os.name == 'nt' else set()
            unexpected = [name for name, qualified, reason in skipped
                          if name not in permitted or not qualified.startswith('test_repository_v3.RepositoryV3Tests.')
                          or reason != 'POSIX Git mode verification is unavailable on Windows']
            summary_skips = re.search(r'OK \(skipped=(\d+)\)', log)
            skip_count = int(summary_skips[1]) if summary_skips else 0
            check.update(tests=int(match[1]) if match else None,
                         passed=int(match[1]) - skip_count if match and result.returncode == 0 else None,
                         seconds=float(match[2]) if match else None,
                         skipped=bool(skip_count), skipped_tests=[name for name, _, _ in skipped],
                         unexpected_skips=unexpected, skipped_count=skip_count,
                         skip_accounting_complete=skip_count == len(skipped))
        record['checks'].append(check)
        record['status'] = 'running' if result.returncode == 0 else 'failed'
        (output / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name + ': ' + str(check), flush=True)
        if result.returncode:
            print(log)
            return result.returncode
        if name == 'typescript' and result.stdout.strip() != '5.8.3':
            raise RuntimeError('The parser must be TypeScript 5.8.3')
        if name == 'tests' and (not check['tests'] or check['unexpected_skips'] or not check['skip_accounting_complete']):
            raise RuntimeError('Parser tests must execute; only two named POSIX mode tests may skip on Windows')
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != hashes[p.relative_to(root).as_posix()] for p in sources):
        raise RuntimeError('Source changed during verification')
    record['status'] = 'passed'
    (output / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


def environment_path() -> str:
    """Retain an explicitly supplied isolated dependency path inside Docker."""
    return os.environ.get('PYTHONPATH', '')


if __name__ == '__main__':
    raise SystemExit(main())
