"""Run a synthetic end-to-end repair; only extracted evidence is recorded, never a gold patch."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from boundary_repair.adapters.dataset import load_tasks
from boundary_repair.adapters.storage import write_json
from boundary_repair.bootstrap import build_pipeline, build_workspace
from boundary_repair.config import load_config
from boundary_repair.experiments.runner import run_generation


def command(arguments: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run trusted demonstration commands with a deadline; never operate on the user's repository."""
    result = subprocess.run(arguments, cwd=cwd, env=env, check=True, capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    return result.stdout.strip()


def behavior(node: str, repository: Path) -> list[bool]:
    """Independently observe the demo function after generation; results never enter the pipeline."""
    javascript = ('const f=require(process.argv[1]).shouldShow;'
                  'console.log(JSON.stringify([[true,false],[true,true],'
                  '[false,false],[false,true]].map(v=>f(...v))))')
    return json.loads(command([node, '-e', javascript, str(repository / 'ui.js')], repository))


def main(argv: list[str] | None = None) -> int:
    """Create a fresh temporary project, generate a real diff, apply it, and retain verification artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parser-module', type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error('--output must not already exist; no existing files will be overwritten')
    node, git = shutil.which('node'), shutil.which('git')
    if not node or not git:
        parser.error('Node.js and Git must be on PATH')
    module = args.parser_module or (ROOT / 'node_modules' / 'typescript')
    if not (module / 'package.json').is_file():
        parser.error('run npm ci --ignore-scripts, or pass a trusted TypeScript package directory')
    output.mkdir(parents=True)
    original = output / 'original'
    original.mkdir()
    example = ROOT / 'examples' / 'offline'
    shutil.copyfile(example / 'ui.js', original / 'ui.js')
    environment = os.environ.copy()
    environment.update({'GIT_AUTHOR_NAME': 'BoundaryRepair synthetic demo',
                        'GIT_AUTHOR_EMAIL': 'demo@example.invalid',
                        'GIT_COMMITTER_NAME': 'BoundaryRepair synthetic demo',
                        'GIT_COMMITTER_EMAIL': 'demo@example.invalid'})
    hooks = output / 'empty-hooks'
    hooks.mkdir()
    for arguments in ([git, 'init', '-q'], [git, 'add', 'ui.js'],
                      [git, '-c', 'core.hooksPath=' + str(hooks), 'commit', '-qm', 'synthetic base']):
        command(arguments, original, environment)
    commit = command([git, 'rev-parse', 'HEAD'], original)
    dataset = output / 'dataset' / 'dev' / 'SWE-bench_Multimodal.json'
    dataset.parent.mkdir(parents=True)
    write_json(dataset, [{'instance_id': 'demo__ui-1', 'repo': 'demo/ui', 'base_commit': commit,
                          'problem_statement': (example / 'issue.txt').read_text(encoding='utf-8')}])
    (output / 'dataset' / 'SOURCE.txt').write_text('SYNTHETIC EXAMPLE; NOT SWE-BENCH\n', encoding='utf-8')
    fixture = output / 'evidence.json'
    shutil.copyfile(example / 'evidence.json', fixture)
    repositories = output / 'repositories.json'
    write_json(repositories, {'repositories': {'demo/ui': {'path': str(original)}}})
    config = load_config(ROOT / 'configs' / 'local.json', output)
    config = replace(config, integration=replace(
        config.integration, workspace_mode='git_archive', model_mode='fixture', fixture_file=fixture,
        repository_manifest=repositories, parser_module=module.resolve()),
        model=replace(config.model, label='boundary_repair/synthetic_fixture'))
    report = run_generation(config, load_tasks(dataset), 'offline-smoke',
                            build_pipeline(config), build_workspace(config))
    if report.generated != 1:
        raise RuntimeError(f'Generation failed; inspect {report.batch_path}')
    before = behavior(node, original)
    application = output / 'applied-copy'
    shutil.copytree(original, application)
    patch = report.batch_path / 'cases' / 'demo__ui-1' / 'patch' / 'final.patch'
    command([git, 'apply', '--check', str(patch)], application)
    command([git, 'apply', str(patch)], application)
    after = behavior(node, application)
    expected = [True, False, False, False]
    if after != expected or before == expected:
        raise AssertionError('The generated patch did not fix the synthetic behavior')
    if (original / 'ui.js').read_bytes() != (example / 'ui.js').read_bytes():
        raise AssertionError('Original repository unexpectedly changed')
    record = json.loads((report.batch_path / 'results.jsonl').read_text(encoding='utf-8'))
    summary = {'status': 'passed', 'benchmark': False, 'model_mode': 'fixture',
               'fixture_contains_final_patch': False, 'before': before, 'after': after,
               'expected': expected, 'model_calls': record['model_calls'],
               'git_apply_check': True, 'original_unchanged': True,
               'batch_directory': str(report.batch_path), 'formal_evaluation_run': False}
    write_json(output / 'smoke_report.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
