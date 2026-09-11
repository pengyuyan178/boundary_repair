"""Run complete frozen splits through the existing Docker image lease and evaluation workflow."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path('/home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair')
PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--launch', type=Path, required=True)
PARSER.add_argument('--mode', choices=('census', 'generate'), default='generate')
PARSER.add_argument('--split', choices=('dev', 'test'), default='dev')
PARSER.add_argument('--modules', choices=tuple(f'{i:03b}' for i in range(8)), default='111')
PARSER.add_argument('--instance')
ARGS = PARSER.parse_args()
CONTROL = ARGS.launch.resolve()
BATCH_ID = CONTROL.name.removesuffix('_launch')
RUN = ROOT / 'result/method/boundary_repair' / BATCH_ID
METHOD = CONTROL / 'method_snapshot'
RUNTIME = ROOT.parent / 'TraceRepair/releases/isolation_20260907'
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(METHOD / 'src'))

from causal_trace_repair.validation import ensure_image, image_present, local_ref, release_image
from boundary_repair.adapters.dataset import load_tasks
from boundary_repair.adapters.storage import file_sha256, json_value, write_json
from boundary_repair.adapters.workspace import DockerWorkspaceAdapter
from boundary_repair.bootstrap import build_pipeline
from boundary_repair.config import load_config
from boundary_repair.experiments.runner import run_generation


def verify_source(expected):
    """Compare the complete frozen method snapshot with its transfer manifest."""
    changed = [name for name, digest in expected.items() if file_sha256(METHOD / name) != digest]
    assert not changed, changed
    return {'source_file_count': len(expected), 'changed_source_files': changed}


@contextmanager
def leased_image(official):
    """Lease a task image and remove only aliases introduced by this run."""
    mirrored = local_ref(official)
    local_was_present = image_present(mirrored)
    lease = ensure_image(official)
    try:
        yield lease
    finally:
        if not lease.already_present and local_was_present:
            result = subprocess.run(['docker', 'rmi', lease.official], capture_output=True, text=True, timeout=300)
            print(json.dumps({'phase': 'release_created_alias', 'image': lease.official,
                              'returncode': result.returncode}), flush=True)
        else:
            release_image(lease)


class PreparedWorkspace:
    """Prepare each selected Docker image without changing the repair pipeline."""
    def __init__(self, config, metadata):
        self.config, self.metadata = config, metadata
        self.manifest = {'instances': {}, 'evaluation': {'max_workers': 1, 'timeout_seconds': 3600}}
        self.original = DockerWorkspaceAdapter(config)
        write_json(config.image_manifest, self.manifest)

    @contextmanager
    def open_base(self, task, context):
        """Record inspected image identity and export only the immutable base commit."""
        official = self.metadata[task.instance_id]['image']
        print(json.dumps({'instance_id': task.instance_id, 'phase': 'loading_image'}), flush=True)
        with leased_image(official) as lease:
            details = json.loads(subprocess.check_output(['docker', 'image', 'inspect', official], text=True))[0]
            assert official in details['RepoTags'] and details['RepoDigests']
            pinned = details['RepoDigests'][0]
            verified = json.loads(subprocess.check_output(['docker', 'image', 'inspect', pinned], text=True))[0]
            assert details['Id'] == verified['Id']
            self.manifest['instances'][task.instance_id] = {
                'repo': task.repo, 'base_commit': task.base_commit, 'image': pinned,
                'repository_path': details['Config']['WorkingDir'], 'harness_image': official,
            }
            write_json(self.config.image_manifest, self.manifest)
            write_json(RUN / 'cases' / task.instance_id / 'logs/image.json', {
                'official': lease.official, 'local': lease.local, 'already_present': lease.already_present,
                'image_id': details['Id'], 'digest': pinned,
            })
            print(json.dumps({'instance_id': task.instance_id, 'phase': 'generating'}), flush=True)
            with self.original.open_base(task, context) as snapshot:
                yield snapshot
        print(json.dumps({'instance_id': task.instance_id, 'phase': 'image_released'}), flush=True)


def grade_frozen_predictions(config, metadata, frozen):
    """Grade immutable nonempty patches after every generation attempt has ended."""
    from boundary_repair.experiments.evaluation import EvaluationRequest, OfficialDockerEvaluator
    predictions = RUN / 'predictions.jsonl'
    assert file_sha256(predictions) == frozen['predictions_sha256']
    reports = []
    for line in predictions.read_text().splitlines():
        row = json.loads(line)
        iid = row['instance_id']
        subset = RUN / 'evaluation_inputs' / (iid + '.jsonl')
        subset.parent.mkdir(parents=True, exist_ok=True)
        subset.write_text(line + '\n', encoding='utf-8')
        print(json.dumps({'instance_id': iid, 'phase': 'grading_image_load'}), flush=True)
        with leased_image(metadata[iid]['image']):
            print(json.dumps({'instance_id': iid, 'phase': 'official_grading'}), flush=True)
            report = OfficialDockerEvaluator().evaluate(EvaluationRequest(
                config.dataset, subset, RUN, 'official_' + iid,
                config.harness_python, config.harness_revision, config.image_manifest,
            ))
            reports.append(json_value(report))
            write_json(RUN / 'evaluation_progress.json', {'reports': reports})
        assert file_sha256(predictions) == frozen['predictions_sha256']
    summary = {
        'selected': len(frozen['selected']), 'attempted': frozen['attempted'],
        'unattempted': len(frozen['selected']) - frozen['attempted'],
        'generated': frozen['generated'], 'submitted': sum(r['submitted'] for r in reports),
        'graded': sum(r['graded'] for r in reports), 'resolved': sum(r['resolved'] for r in reports),
        'infrastructure_errors': sum(r['infrastructure_errors'] for r in reports),
        'predictions_sha256': file_sha256(predictions), 'reports': reports,
        'generation_status_counts': frozen['generation_status_counts'],
        'model_calls': frozen['model_calls'], 'output_tokens': frozen['output_tokens'],
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    summary['resolved_over_selected'] = summary['resolved'] / summary['selected'] if reports else None
    summary['evaluation_performed'] = bool(reports)
    summary['status'] = 'blocked' if summary['unattempted'] else 'completed'
    write_json(RUN / 'final_summary.json', summary)
    return summary


def main():
    """Freeze seed-42 selection, run one-way generation, then score without regeneration."""
    assert ARGS.instance or not RUN.exists()
    expected = json.loads((CONTROL / 'source_files.sha256.json').read_text())
    write_json(CONTROL / 'source_integrity_before.json', verify_source(expected))
    dataset = ROOT / 'dataset' / ARGS.split / 'SWE-bench_Multimodal.json'
    hashes = {'dev': 'bbc645dcf2557348c749f95f0bcbd2d3064409f2e089432d2d1f24f91a07c87b',
              'test': '03653423b955194e857012e3273e6aa57c05ddfd51429657b19b073b2aa4c0b0'}
    assert file_sha256(dataset) == hashes[ARGS.split]
    task_map = {t.instance_id: t for t in load_tasks(dataset)}
    population = sorted(task_map)
    assert len(population) == (100 if ARGS.split == 'dev' else 480)
    selected = random.Random(42).sample(population, len(population))
    if ARGS.instance:
        assert ARGS.instance in selected and ARGS.mode == 'census'
        selected = [ARGS.instance]
    raw = json.loads(dataset.read_text())
    metadata = {iid: {key: raw[iid][key] for key in ('instance_id', 'repo', 'base_commit', 'image')} for iid in selected}
    del raw
    selection = {
        'split': ARGS.split, 'population': len(population), 'sample_size': len(selected), 'seed': 42,
        'sampling': 'random.Random(42).sample(sorted(instance_ids), len(instance_ids))', 'exclusions': [],
        'selected_instances': selected, 'repositories': dict(Counter(r['repo'] for r in metadata.values())),
        'dataset_sha256': file_sha256(dataset), 'selected_metadata': list(metadata.values()),
        'purpose': 'code_coverage_only' if ARGS.mode == 'census' else 'frozen_complete_split',
        'modules': ARGS.modules,
    }
    if not ARGS.instance:
        write_json(CONTROL / 'selection.json', selection)
    settings = json.loads((CONTROL / 'runtime_template.json').read_text())
    settings['image_manifest'] = str(CONTROL / 'image_manifest.json')
    settings['integration']['parser_module'] = str(METHOD / 'node_modules/typescript')
    settings['dataset'] = str(dataset)
    settings['modules'] = dict(zip(('partial_specification', 'expressivity_localization', 'scope_synthesis'),
                                  (bit == '1' for bit in ARGS.modules)))
    if ARGS.instance:
        settings['image_manifest'] = str(RUN / 'cases' / ARGS.instance / 'logs/image_manifest.json')
    write_json(CONTROL / 'runtime.json', settings)
    config = load_config(CONTROL / 'runtime.json')
    if ARGS.mode == 'census':
        return census(config, task_map, metadata, selected, expected)
    from boundary_repair.adapters.model import read_model_environment
    values = read_model_environment(config)
    assert values[config.model.name_env] == 'gpt-4.1', 'Configured model differs from frozen GPT-4.1 protocol'
    del values
    tasks = tuple(task_map[iid] for iid in selected)
    random.seed(42)
    print(json.dumps({'batch': BATCH_ID, 'selected_instances': selected}), flush=True)
    report = run_generation(config, tasks, BATCH_ID, build_pipeline(config), PreparedWorkspace(config, metadata))
    write_json(RUN / 'selection.json', selection)
    write_json(RUN / 'source_integrity_after.json', verify_source(expected))
    records = [json.loads(line) for line in (RUN / 'results.jsonl').read_text().splitlines()]
    frozen = {
        'frozen_at': datetime.now(timezone.utc).isoformat(), 'seed': 42, 'selected': selected,
        'attempted': report.attempted, 'generated': report.generated,
        'method_source_sha256': json.loads((RUN / 'manifest.json').read_text())['method_source_sha256'],
        'predictions_sha256': file_sha256(RUN / 'predictions.jsonl'),
        'results_sha256': file_sha256(RUN / 'results.jsonl'), 'manifest_sha256': file_sha256(RUN / 'manifest.json'),
        'image_manifest_sha256': file_sha256(config.image_manifest),
        'generation_status_counts': dict(Counter(r['status'] for r in records)),
        'model_calls': sum(r['model_calls'] for r in records),
        'output_tokens': sum(r['output_tokens'] for r in records), 'evaluation_performed': False,
    }
    write_json(RUN / 'generation_frozen.json', frozen)
    write_json(CONTROL / 'completion.json', {'generation': report, 'frozen': frozen})
    print(json.dumps(json_value({'generation': report, 'frozen': frozen})), flush=True)
    summary = grade_frozen_predictions(config, metadata, frozen)
    write_json(RUN / 'source_integrity_after_evaluation.json', verify_source(expected))
    write_json(CONTROL / 'finished.json', summary)
    print(json.dumps(summary), flush=True)
    return 0 if report.attempted == len(population) else 3


def census(config, task_map, metadata, selected, expected):
    """Audit every selected base with no model calls, keeping failed cases in the coverage denominator."""
    from boundary_repair.adapters.program import ProgramAdapter
    from boundary_repair.domain.runtime import RunContext, BudgetLedger
    if ARGS.instance:
        task = task_map[ARGS.instance]
        ctx = RunContext(BATCH_ID, task.instance_id, 42, config.policy, BudgetLedger(config.budget))
        write_json(RUN / 'cases' / task.instance_id / 'input_context/task.json', task)
        with PreparedWorkspace(config, metadata).open_base(task, ctx) as snapshot:
            program = ProgramAdapter(config)
            scope = program.source_scope(snapshot, ctx, task.problem_statement)
            interfaces = program.observation_interfaces(snapshot, ctx)
            index = program.index(snapshot, ctx)
            write_json(RUN / 'cases' / task.instance_id / 'input_context/source_scope.json', scope)
            write_json(RUN / 'cases' / task.instance_id / 'trajectory/observation_catalog.json', interfaces)
            result = {'instance_id': task.instance_id, 'status': 'audited', 'interfaces': len(interfaces),
                'boolean_entries': sum(i.kind == 'boolean_entry' for i in interfaces),
                'local_projections': sum(i.kind == 'local_projection' for i in interfaces),
                'local_edit_interfaces': len(index.local_interfaces), 'source_files': len(scope.files),
                'source_chars': sum(len(r.source) for r in scope.regions), 'snapshot_sha256': snapshot.tree_sha256,
                'diagnostics': index.unsupported_constructs, 'model_calls': ctx.budget.model_calls}
            assert ctx.budget.model_calls == 0
            write_json(RUN / 'cases' / task.instance_id / 'result_data/census.json', result)
        return 0
    results = []
    for iid in selected:
        case = RUN / 'cases' / iid
        (case / 'logs').mkdir(parents=True, exist_ok=True)
        with (case / 'logs/census.log').open('w') as log:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--launch', str(CONTROL),
                '--mode', 'census', '--split', ARGS.split, '--instance', iid], stdout=log, stderr=subprocess.STDOUT)
        result_path = case / 'result_data/census.json'
        result = json.loads(result_path.read_text()) if process.returncode == 0 and result_path.exists() else {
            'instance_id': iid, 'status': 'infrastructure_error', 'exit_code': process.returncode}
        results.append(result)
        summary = {'mode': 'code_coverage_only', 'selected': len(selected), 'attempted': len(results),
            'audited': sum(r['status'] == 'audited' for r in results),
            'cases_with_interfaces': sum(r.get('interfaces', 0) > 0 for r in results),
            'cases_with_local_projections': sum(r.get('local_projections', 0) > 0 for r in results),
            'interfaces': sum(r.get('interfaces', 0) for r in results), 'real_model_calls': 0,
            'official_score': None, 'results': results}
        write_json(RUN / 'census_summary.json', summary)
        print(json.dumps({key: value for key, value in summary.items() if key != 'results'}), flush=True)
    write_json(CONTROL / 'source_integrity_after.json', verify_source(expected))
    return 0 if all(r['status'] == 'audited' for r in results) else 3


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_json(CONTROL / 'failed.json', {'status': 'launcher_failed', 'error_type': type(exc).__name__,
                                            'at': datetime.now(timezone.utc).isoformat()})
        print(json.dumps({'status': 'launcher_failed', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(2)
