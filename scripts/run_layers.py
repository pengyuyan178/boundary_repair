"""Prepare answer-free inputs, generate in isolated workers, and evaluate frozen patches separately."""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path('/home/ubuntu/anaconda3/envs/pyy/paper/newGUIRepair')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from boundary_repair.adapters.dataset import load_generation_tasks
from boundary_repair.adapters.storage import BatchStore, file_sha256, json_value, safe_component, source_fingerprint, write_json
from boundary_repair.adapters.workspace import DockerWorkspaceAdapter
from boundary_repair.config import load_config
from boundary_repair.domain.errors import ConfigurationError, ValidationError
from boundary_repair.domain.runtime import BudgetLedger, RunContext
from boundary_repair.domain.task import RepositorySnapshot
from boundary_repair.kernel.files import allowed_source, tree_digest

PROTOCOL = 'isolated-generation-v1'
DATASET_HASHES = {'dev': 'bbc645dcf2557348c749f95f0bcbd2d3064409f2e089432d2d1f24f91a07c87b',
                  'test': '03653423b955194e857012e3273e6aa57c05ddfd51429657b19b073b2aa4c0b0'}


def verify_source(method, expected):
    """Compare frozen method files with the transfer manifest."""
    changed = [name for name, digest in expected.items() if file_sha256(method / name) != digest]
    if changed:
        raise ValidationError('frozen_method_source_changed')
    return {'source_file_count': len(expected), 'changed_source_files': changed}


def prepare_inputs(dataset, destination, split, modules):
    """Evaluation-side export of task fields, issue assets and image identities for a complete split."""
    from boundary_repair.adapters.dataset import load_tasks
    task_map = {t.instance_id: t for t in load_tasks(dataset)}
    selected = random.Random(42).sample(sorted(task_map), len(task_map))
    raw = json.loads(dataset.read_text(encoding='utf-8'))
    rows = raw if isinstance(raw, dict) else {r['instance_id']: r for r in raw}
    metadata = {iid: {key: rows[iid][key] for key in ('instance_id', 'repo', 'base_commit', 'image')}
                for iid in selected}
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / 'tasks.json', [task_map[iid] for iid in selected])
    manifest = {'protocol': PROTOCOL, 'split': split, 'seed': 42, 'modules': modules,
                'population': len(selected), 'selected_instances': selected,
                'tasks_sha256': file_sha256(destination / 'tasks.json'),
                'evaluation_dataset_sha256': file_sha256(dataset), 'selected_metadata': metadata,
                'producer_role': 'evaluation', 'prepared_at': datetime.now(timezone.utc).isoformat()}
    write_json(destination / 'input_manifest.json', manifest)
    return manifest


def read_inputs(directory):
    """Verify the prepared task identity and content before any generator is started."""
    manifest = json.loads((directory / 'input_manifest.json').read_text(encoding='utf-8'))
    if manifest['protocol'] != PROTOCOL or file_sha256(directory / 'tasks.json') != manifest['tasks_sha256']:
        raise ValidationError('prepared_inputs_changed')
    tasks = load_generation_tasks(directory / 'tasks.json')
    if [t.instance_id for t in tasks] != manifest['selected_instances']:
        raise ValidationError('prepared_selection_changed')
    for task in tasks:
        safe_component(task.instance_id)
        entry = manifest['selected_metadata'][task.instance_id]
        if set(entry) != {'instance_id', 'repo', 'base_commit', 'image'} or any(
                entry[k] != getattr(task, k) for k in ('instance_id', 'repo', 'base_commit')):
            raise ValidationError('prepared_image_identity_mismatch')
    return tasks, manifest


@contextmanager
def leased_image(official):
    """Lease a task image and remove only aliases introduced by this run."""
    sys.path.insert(0, str(ROOT.parent / 'TraceRepair/releases/isolation_20260907'))
    from causal_trace_repair.validation import ensure_image, image_present, local_ref, release_image
    local_was_present = image_present(local_ref(official))
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
    """Export sanitized base source through the existing image lease adapter."""
    def __init__(self, config, metadata):
        self.config, self.metadata = config, metadata
        self.manifest = {'instances': {}, 'evaluation': {'max_workers': 1, 'timeout_seconds': 3600}}
        self.original = DockerWorkspaceAdapter(config)
        write_json(config.image_manifest, self.manifest)

    @contextmanager
    def open_base(self, task, context):
        """Bind the task to its inspected image digest and immutable base commit."""
        official = self.metadata[task.instance_id]['image']
        print(json.dumps({'instance_id': task.instance_id, 'phase': 'loading_image'}), flush=True)
        with leased_image(official):
            details = json.loads(subprocess.check_output(['docker', 'image', 'inspect', official], text=True))[0]
            pinned = details['RepoDigests'][0]
            verified = json.loads(subprocess.check_output(['docker', 'image', 'inspect', pinned], text=True))[0]
            if details['Id'] != verified['Id'] or official not in details['RepoTags']:
                raise ValidationError('task_image_identity_mismatch')
            self.manifest['instances'][task.instance_id] = {
                'repo': task.repo, 'base_commit': task.base_commit, 'image': pinned,
                'repository_path': details['Config']['WorkingDir'], 'harness_image': official,
            }
            write_json(self.config.image_manifest, self.manifest)
            with self.original.open_base(task, context) as snapshot:
                yield snapshot


def worker_settings(template):
    """Create container paths without host datasets, harness paths or credential files."""
    settings = json.loads(json.dumps(template))
    settings.update(project_root='/work', dataset='/input/tasks.json', dataset_source='/input/source.json',
                    results_root='/output', env_file='/input/no-credentials', image_manifest=None,
                    harness_python=None, harness_revision=None, node_candidates=['/usr/bin/node'])
    settings['integration'].update(parser_module='/opt/typescript', fixture_file=None, repository_manifest=None)
    return settings


def worker_command(image, method, inputs, source, output, name, operation, batch_id, env_names=()):
    """Construct a clean non-root worker with exactly five read-only inputs and one writable output."""
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise ConfigurationError('generator_requires_nonroot_host_user')
    mounts = [(method / 'src', '/opt/method/src', True),
              (method / 'scripts/run_layers.py', '/opt/method/run_layers.py', True),
              (method / 'node_modules/typescript', '/opt/typescript', True),
              (inputs, '/input', True), (source, '/base', True), (output, '/output', False)]
    args = ['docker', 'create', '--name', name, '--pull=never', '--read-only', '--user', f'{uid}:{gid}',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=6g', '--cpus=2',
            '--network=' + ('bridge' if operation == 'generate' else 'none'),
            '--tmpfs=/tmp:rw,nosuid,nodev,size=2g,mode=1777',
            '--tmpfs=/work:rw,nosuid,nodev,size=64m,mode=1777', '--workdir=/work',
            '--env=PYTHONPATH=/opt/method/src', '--env=PYTHONHASHSEED=42',
            '--env=BOUNDARY_GENERATION_ROLE=isolated_worker']
    for path, target, readonly in mounts:
        args += ['--mount', f'type=bind,src={path.resolve()},dst={target}' + (',readonly' if readonly else '')]
    for key in env_names:
        args += ['--env', key]
    return args + ['--entrypoint=python', image, '/opt/method/run_layers.py', '--mode=worker',
                   '--operation=' + operation, '--batch-id=' + safe_component(batch_id)]


def container_audit(details, image, expected_mounts):
    """Check actual Docker isolation and log selected settings without environment secrets."""
    host, config = details['HostConfig'], details['Config']
    mounts = {m['Destination']: {'source': m['Source'], 'writable': m['RW']}
              for m in details['Mounts'] if m['Type'] != 'tmpfs'}
    expected = {target: {'source': str(path.resolve()), 'writable': target == '/output'}
                for target, path in expected_mounts.items()}
    if (details['Image'] != image or mounts != expected or host['Privileged'] or not host['ReadonlyRootfs']
            or host['NetworkMode'] == 'host' or host['PidMode'] == 'host' or host['IpcMode'] == 'host'
            or config['User'].split(':')[0] in {'', '0', 'root'} or 'ALL' not in host['CapDrop']
            or not any(s.startswith('no-new-privileges') for s in host['SecurityOpt'])):
        raise ValidationError('generator_container_isolation_mismatch')
    return {'protocol': PROTOCOL, 'image_id': image, 'user': config['User'], 'mounts': mounts,
            'read_only_root': True, 'privileged': False, 'cap_drop': host['CapDrop'],
            'security_opt': host['SecurityOpt'], 'network': host['NetworkMode'],
            'raw_dataset_mounted': False, 'docker_socket_mounted': False}


def launch_worker(image, method, inputs, source, output, log, operation, batch_id, environment):
    """Inspect an isolated worker before execution and remove it after recording its exit status."""
    name = 'boundary-generate-' + uuid4().hex
    command = worker_command(image, method, inputs, source, output, name, operation, batch_id, environment)
    subprocess.run(command, check=True, capture_output=True, env={**os.environ, **environment})
    try:
        details = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
        audit = container_audit(details, image, {'/opt/method/src': method / 'src',
            '/opt/method/run_layers.py': method / 'scripts/run_layers.py',
            '/opt/typescript': method / 'node_modules/typescript', '/input': inputs,
            '/base': source, '/output': output})
        write_json(log.with_suffix('.isolation.json'), audit)
        with log.open('wb') as stream:
            subprocess.run(['docker', 'start', '-a', name], stdout=stream, stderr=subprocess.STDOUT)
        state = json.loads(subprocess.check_output(['docker', 'inspect', '--format={{json .State}}', name], text=True))
        audit['exit_code'], audit['oom_killed'] = state['ExitCode'], state['OOMKilled']
        write_json(log.with_suffix('.isolation.json'), audit)
        if state['ExitCode'] or state['OOMKilled']:
            raise ValidationError('isolated_worker_failed_see_case_log')
        return audit
    finally:
        subprocess.run(['docker', 'rm', '-f', name], check=True, capture_output=True, timeout=30)


class MountedWorkspace:
    """Expose only a checked read-only production source tree inside the worker."""
    @contextmanager
    def open_base(self, task, context):
        """Verify source identity without access to a repository image or Docker daemon."""
        manifest = json.loads(Path('/input/source.json').read_text(encoding='utf-8'))
        root = Path('/base')
        if (task.base_commit != manifest['base_commit'] or task.instance_id != manifest['instance_id']
                or tree_digest(root) != manifest['tree_sha256']):
            raise ValidationError('mounted_source_identity_mismatch')
        for path in root.rglob('*'):
            if path.is_symlink() or (path.is_file() and not allowed_source(path.relative_to(root).as_posix())):
                raise ValidationError('unfiltered_generator_source')
        yield RepositorySnapshot(root, task.base_commit, manifest['tree_sha256'])


def worker(operation, batch_id):
    """Execute generation or a zero-model isolation probe using only container inputs."""
    from boundary_repair.experiments.runner import require_isolated_generation, run_generation
    from boundary_repair.bootstrap import build_pipeline
    config = load_config(Path('/input/runtime.json'))
    require_isolated_generation(config)
    tasks = load_generation_tasks(config.dataset)
    if len(tasks) != 1:
        raise ValidationError('one_task_per_worker_required')
    task = tasks[0]
    random.seed(config.seed)
    context = RunContext(batch_id, task.instance_id, config.seed, config.policy, BudgetLedger(config.budget))
    if operation == 'generate':
        report = run_generation(config, tasks, batch_id, build_pipeline(config), MountedWorkspace())
        return 0 if report.status == 'generation_finished' else 3
    with MountedWorkspace().open_base(task, context) as snapshot:
        denied_paths = [ROOT / 'dataset/dev/SWE-bench_Multimodal.json',
                        ROOT / 'dataset/test/SWE-bench_Multimodal.json',
                        Path('/proc/1/root') / str(ROOT).lstrip('/') / 'dataset/dev/SWE-bench_Multimodal.json',
                        Path('/var/run/docker.sock'), Path('/testbed/.git/HEAD')]
        read_denied = {str(path): subprocess.run(
            [sys.executable, '-c', 'import os,sys; os.close(os.open(sys.argv[1], os.O_RDONLY))', str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0 for path in denied_paths}
        if not all(read_denied.values()):
            raise ValidationError('answer_file_read_succeeded_in_worker')
        result = {'operation': operation, 'not_benchmark_evidence': True, 'instance_id': task.instance_id,
                  'source_sha256': snapshot.tree_sha256, 'real_model_calls': 0,
                  'file_open_denied': read_denied,
                  'base_is_read_only': not os.access(snapshot.root, os.W_OK),
                  'uid': os.getuid(), 'docker_socket_accessible': os.access('/var/run/docker.sock', os.R_OK),
                  'host_dataset_accessible': os.access(ROOT / 'dataset', os.R_OK),
                  'host_evaluation_accessible': os.access(ROOT / 'result', os.R_OK),
                  'benchmark_image_testbed_accessible': os.access('/testbed', os.R_OK)}
        if any(v for k, v in result.items() if k.endswith('_accessible')):
            raise ValidationError('answer_side_path_accessible_in_worker')
        if operation == 'census':
            from boundary_repair.adapters.program import ProgramAdapter
            program = ProgramAdapter(config)
            interfaces, index = program.observation_interfaces(snapshot, context), program.index(snapshot, context)
            result.update(interfaces=len(interfaces), boolean_entries=sum(i.kind == 'boolean_entry' for i in interfaces),
                          local_projections=sum(i.kind == 'local_projection' for i in interfaces),
                          local_edit_interfaces=len(index.local_interfaces))
        write_json(Path('/output') / (operation + '.json'), result)
    return 0


def generate(control, run, method, template, operation, image):
    """Coordinate isolated per-case generation without opening the original dataset or any scores."""
    tasks, prepared = read_inputs(control / 'inputs')
    template.update(dataset=str(control / 'inputs/tasks.json'), dataset_source=str(control / 'inputs/input_manifest.json'),
                    image_manifest=str(control / 'image_manifest.json'))
    template['integration']['parser_module'] = str(method / 'node_modules/typescript')
    write_json(control / 'runtime_generation.json', template)
    config = load_config(control / 'runtime_generation.json')
    environment = {}
    if operation == 'generate':
        from boundary_repair.adapters.model import read_model_environment
        environment = read_model_environment(config)
        if environment[config.model.name_env] != 'gpt-4.1':
            raise ConfigurationError('model_differs_from_frozen_gpt41_protocol')
    image_id = subprocess.check_output(['docker', 'image', 'inspect', '--format={{.Id}}', image], text=True).strip()
    store = BatchStore.create(run.parent, run.name)
    workspace = PreparedWorkspace(config, prepared['selected_metadata'])
    write_json(run / 'selection.json', prepared)
    write_json(run / 'manifest.json', {'protocol': PROTOCOL, 'execution_mode': config.integration.model_mode,
        'not_benchmark_evidence': operation != 'generate', 'dataset_sha256': prepared['tasks_sha256'],
        'evaluation_dataset_sha256': prepared['evaluation_dataset_sha256'],
        'prepared_inputs_sha256': file_sha256(control / 'inputs/input_manifest.json'),
        'selected_instances': prepared['selected_instances'], 'seed': config.seed, 'modules': config.modules,
        'method_source_sha256': source_fingerprint(method / 'src/boundary_repair'),
        'generator_image_id': image_id, 'evaluation_performed': False})
    for task in tasks:
        context = RunContext(run.name, task.instance_id, config.seed, config.policy, BudgetLedger(config.budget))
        log = run / 'worker_logs' / (task.instance_id + '.log')
        log.parent.mkdir(exist_ok=True)
        print(json.dumps({'instance_id': task.instance_id, 'phase': operation}), flush=True)
        with workspace.open_base(task, context) as snapshot, TemporaryDirectory(prefix='boundary-worker-') as temporary:
            root = Path(temporary)
            inputs = root / 'input'
            output = run / 'worker_outputs' / task.instance_id
            inputs.mkdir()
            output.mkdir(parents=True, exist_ok=False)
            write_json(inputs / 'tasks.json', [task])
            write_json(inputs / 'runtime.json', worker_settings(template))
            write_json(inputs / 'source.json', {'instance_id': task.instance_id, 'base_commit': task.base_commit,
                                               'tree_sha256': snapshot.tree_sha256})
            audit = launch_worker(image_id, method, inputs, snapshot.root, output, log, operation, run.name, environment)
            if operation != 'generate':
                case = store.create_case(task.instance_id)
                shutil.copy2(output / (operation + '.json'), case.root / 'result_data' / (operation + '.json'))
                continue
            produced = output / run.name
            case_root = run / 'cases' / task.instance_id
            shutil.move(str(produced / 'cases' / task.instance_id), str(case_root))
            write_json(case_root / 'logs/generator_isolation.json', audit)
            shutil.copy2(produced / 'manifest.json', case_root / 'result_data/worker_manifest.json')
            records = [json.loads(line) for line in (produced / 'results.jsonl').read_text().splitlines()]
            predictions = [json.loads(line) for line in (produced / 'predictions.jsonl').read_text().splitlines()]
            if len(records) != 1 or records[0]['instance_id'] != task.instance_id or len(predictions) > 1:
                raise ValidationError('worker_output_identity_mismatch')
            if predictions and predictions[0]['instance_id'] != task.instance_id:
                raise ValidationError('worker_prediction_identity_mismatch')
            store.record(records[0], predictions[0] if predictions else None)
    if operation != 'generate':
        write_json(run / 'summary.json', {'operation': operation, 'selected': len(tasks), 'real_model_calls': 0,
                                         'not_benchmark_evidence': True})
        return 0
    records = [json.loads(line) for line in (run / 'results.jsonl').read_text().splitlines()]
    frozen = {'protocol': PROTOCOL, 'frozen_at': datetime.now(timezone.utc).isoformat(), 'seed': 42,
        'selected': prepared['selected_instances'], 'attempted': len(records),
        'generated': sum(r['status'] == 'generated' for r in records),
        'predictions_sha256': file_sha256(run / 'predictions.jsonl'), 'results_sha256': file_sha256(run / 'results.jsonl'),
        'manifest_sha256': file_sha256(run / 'manifest.json'), 'image_manifest_sha256': file_sha256(config.image_manifest),
        'generation_status_counts': dict(Counter(r['status'] for r in records)),
        'model_calls': sum(r['model_calls'] for r in records), 'output_tokens': sum(r['output_tokens'] for r in records),
        'evaluation_performed': False}
    write_json(run / 'generation_frozen.json', frozen)
    write_json(run / 'summary.json', frozen)
    print(json.dumps(frozen), flush=True)
    return 0


def verify_freeze(run, control):
    """Require a complete unchanged batch before the separate evaluation process reads answers."""
    frozen = json.loads((run / 'generation_frozen.json').read_text(encoding='utf-8'))
    if frozen['protocol'] != PROTOCOL or frozen['attempted'] != len(frozen['selected']):
        raise ValidationError('complete_isolated_generation_required')
    for filename, key in (('predictions.jsonl', 'predictions_sha256'), ('results.jsonl', 'results_sha256'),
                          ('manifest.json', 'manifest_sha256')):
        if file_sha256(run / filename) != frozen[key]:
            raise ValidationError('frozen_generation_artifact_changed')
    if file_sha256(control / 'image_manifest.json') != frozen['image_manifest_sha256']:
        raise ValidationError('frozen_image_manifest_changed')
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    if file_sha256(control / 'inputs/input_manifest.json') != manifest['prepared_inputs_sha256']:
        raise ValidationError('frozen_prepared_inputs_changed')
    read_inputs(control / 'inputs')
    return frozen


def evaluate(control, run, template):
    """Read answers exclusively in the scoring invocation after validating the immutable generation batch."""
    from boundary_repair.experiments.evaluation import EvaluationRequest, OfficialDockerEvaluator
    frozen = verify_freeze(run, control)
    _, prepared = read_inputs(control / 'inputs')
    dataset = ROOT / 'dataset' / prepared['split'] / 'SWE-bench_Multimodal.json'
    if file_sha256(dataset) != prepared['evaluation_dataset_sha256']:
        raise ValidationError('evaluation_dataset_changed')
    template.update(dataset=str(dataset), image_manifest=str(control / 'image_manifest.json'))
    write_json(control / 'runtime_evaluation.json', template)
    config = load_config(control / 'runtime_evaluation.json')
    reports = []
    for line in (run / 'predictions.jsonl').read_text().splitlines():
        iid = json.loads(line)['instance_id']
        subset = run / 'evaluation_inputs' / (iid + '.jsonl')
        subset.parent.mkdir(parents=True, exist_ok=True)
        with subset.open('x', encoding='utf-8') as stream:
            stream.write(line + '\n')
        with leased_image(prepared['selected_metadata'][iid]['image']):
            report = OfficialDockerEvaluator().evaluate(EvaluationRequest(dataset, subset, run, 'official_' + iid,
                config.harness_python, config.harness_revision, config.image_manifest))
            reports.append(json_value(report))
            write_json(run / 'evaluation_progress.json', {'reports': reports})
        verify_freeze(run, control)
    summary = {**frozen, 'selected': len(frozen['selected']), 'unattempted': 0,
        **{key: sum(r[key] for r in reports) for key in ('submitted', 'graded', 'resolved', 'infrastructure_errors')},
        'evaluation_performed': bool(reports), 'reports': reports, 'completed_at': datetime.now(timezone.utc).isoformat()}
    summary['resolved_over_selected'] = summary['resolved'] / summary['selected']
    write_json(run / 'final_summary.json', summary)
    print(json.dumps(summary), flush=True)
    return 0


def main(argv=None):
    """Dispatch exactly one role per process; generation never starts evaluation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', type=Path)
    parser.add_argument('--mode', choices=('prepare', 'generate', 'evaluate', 'census', 'probe', 'worker'), required=True)
    parser.add_argument('--split', choices=('dev', 'test'), default='dev')
    parser.add_argument('--modules', choices=tuple(f'{i:03b}' for i in range(8)), default='111')
    parser.add_argument('--generator-image', default='boundary-repair-generator:20260912')
    parser.add_argument('--operation', choices=('generate', 'census', 'probe'), default='generate')
    parser.add_argument('--batch-id')
    args = parser.parse_args(argv)
    if args.mode == 'worker':
        return worker(args.operation, safe_component(args.batch_id))
    if args.launch is None:
        parser.error('--launch is required for host roles')
    control = args.launch.resolve()
    run = ROOT / 'result/method/boundary_repair' / safe_component(control.name.removesuffix('_launch'))
    method = control / 'method_snapshot'
    expected = json.loads((control / 'source_files.sha256.json').read_text(encoding='utf-8'))
    write_json(control / ('source_integrity_' + args.mode + '_before.json'), verify_source(method, expected))
    if args.mode == 'prepare':
        dataset = ROOT / 'dataset' / args.split / 'SWE-bench_Multimodal.json'
        if file_sha256(dataset) != DATASET_HASHES[args.split]:
            raise ValidationError('benchmark_dataset_identity_changed')
        prepared = prepare_inputs(dataset, control / 'inputs', args.split, args.modules)
        if prepared['population'] != (100 if args.split == 'dev' else 480):
            raise ValidationError('complete_benchmark_split_required')
        return 0
    _, prepared = read_inputs(control / 'inputs')
    template = json.loads((control / 'runtime_template.json').read_text(encoding='utf-8'))
    template['modules'] = dict(zip(('partial_specification', 'expressivity_localization', 'scope_synthesis'),
                                  (bit == '1' for bit in prepared['modules'])))
    if template['seed'] != prepared['seed']:
        raise ValidationError('prepared_seed_mismatch')
    if args.mode == 'evaluate':
        return evaluate(control, run, template)
    result = generate(control, run, method, template, args.mode, args.generator_image)
    write_json(control / ('source_integrity_' + args.mode + '_after.json'), verify_source(method, expected))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
