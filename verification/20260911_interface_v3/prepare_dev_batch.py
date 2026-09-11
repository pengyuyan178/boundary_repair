"""Prepare a frozen five-task regression by reusing the audited v2 batch orchestration."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main() -> None:
    """Copy only explicit source and launch files; never read or transfer credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-launch', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--local-verification', type=Path, required=True)
    parser.add_argument('--docker-verification', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.snapshot / 'source_files.sha256.json').read_text(encoding='utf-8'))
    for path in (args.local_verification, args.docker_verification):
        report = json.loads(path.read_text(encoding='utf-8'))
        assert report['status'] == 'passed'
        assert all(item['exit_code'] == 0 for item in report['checks'])
        assert all(manifest[name] == digest for name, digest in report['source_sha256'].items())
    state = json.loads((args.snapshot / 'source_state.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((args.snapshot / 'source.tar.gz').read_bytes()).hexdigest() == state['archive_sha256']
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    script = (args.previous_launch / 'run_batch.py').read_text(encoding='utf-8')
    assert script.startswith('"""Run five frozen dev tasks with unchanged v2 code')
    script = script.replace('with unchanged v2 code', 'with frozen v3 code', 1)
    (output / 'run_batch.py').write_text(script, encoding='utf-8')
    shutil.copyfile(args.previous_launch / 'runtime_template.json', output / 'runtime_template.json')
    for name in ('source.tar.gz', 'source_files.sha256.json', 'source_state.json'):
        shutil.copyfile(args.snapshot / name, output / name)
    shutil.copyfile(args.local_verification, output / 'local_verification.json')
    shutil.copyfile(args.docker_verification, output / 'docker_verification.json')
    provenance = {
        'orchestration_origin': args.previous_launch.name,
        'orchestration_change': 'version label only; task order, model, budget, image lifecycle and independent grading unchanged',
        'source_archive_sha256': state['archive_sha256'],
        'source_file_count': len(manifest),
        'credential_files_transferred': False,
        'regeneration_from_grading': False,
        'launch_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(output.iterdir()) if p.is_file()},
    }
    (output / 'preparation.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), 'source_files': len(manifest)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
