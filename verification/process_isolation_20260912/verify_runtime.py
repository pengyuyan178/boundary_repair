"""Verify public split exports, actual isolated workers and archived grading logs without model calls."""
from collections import Counter
import json
from pathlib import Path
import sys

CHECK = Path(sys.argv[1]).resolve()
METHOD = CHECK / 'method'
sys.path.insert(0, str(METHOD / 'src'))
sys.path.insert(0, str(METHOD / 'scripts'))
import run_layers as layers
from boundary_repair.adapters.storage import write_json
from boundary_repair.config import load_config
from boundary_repair.domain.runtime import BudgetLedger, RunContext
from boundary_repair.experiments.evaluation import evaluation_integrity


def main():
    """Run full input checks and a real zero-model worker against immutable base source."""
    exports = {}
    for split, count in (('dev', 100), ('test', 480)):
        dataset = layers.ROOT / 'dataset' / split / 'SWE-bench_Multimodal.json'
        assert layers.file_sha256(dataset) == layers.DATASET_HASHES[split]
        directory = CHECK / 'exports' / split
        if not directory.exists():
            layers.prepare_inputs(dataset, directory, split, '111')
        tasks, manifest = layers.read_inputs(directory)
        assert len(tasks) == count
        exports[split] = {'tasks': len(tasks), 'task_sha256': manifest['tasks_sha256'],
                          'evaluation_dataset_sha256': manifest['evaluation_dataset_sha256'],
                          'exact_field_allowlist_passed': True}
    tasks, manifest = layers.read_inputs(CHECK / 'exports/dev')
    previous = layers.ROOT / 'result/method/boundary_repair/dev_all_gpt41_seed42_layers_20260912_03_launch'
    template = json.loads((previous / 'runtime_template.json').read_text())
    template.update(dataset=str(CHECK/'exports/dev/tasks.json'),
                    dataset_source=str(CHECK/'exports/dev/input_manifest.json'),
                    image_manifest=str(CHECK/'probe_images.json'))
    template['integration']['parser_module'] = str(METHOD/'node_modules/typescript')
    write_json(CHECK/'probe_runtime.json', template)
    config = load_config(CHECK/'probe_runtime.json')
    task = tasks[0]
    context = RunContext('isolation-check', task.instance_id, 42, config.policy, BudgetLedger(config.budget))
    workspace = layers.PreparedWorkspace(config, manifest['selected_metadata'])
    probes = {}
    image = layers.subprocess.check_output(['docker', 'image', 'inspect',
        'boundary-repair-generator:20260912', '--format={{.Id}}'], text=True).strip()
    with workspace.open_base(task, context) as source:
        for operation in ('probe', 'census'):
            directory = CHECK/'probes'/operation
            inputs, output = directory/'input', directory/'output'
            inputs.mkdir(parents=True, exist_ok=False)
            output.mkdir()
            write_json(inputs/'tasks.json', [task])
            write_json(inputs/'runtime.json', layers.worker_settings(template))
            write_json(inputs/'source.json', {'instance_id': task.instance_id,
                'base_commit': task.base_commit, 'tree_sha256': source.tree_sha256})
            audit = layers.launch_worker(image, METHOD, inputs, source.root, output,
                directory/'worker.log', operation, 'isolation-check', {})
            result = json.loads((output/(operation+'.json')).read_text())
            assert result['real_model_calls'] == 0 and result['base_is_read_only']
            assert all(result['file_open_denied'].values())
            probes[operation] = {'isolation': audit, 'result': result}
            print(json.dumps({'operation': operation, 'exit_code': audit['exit_code']}), flush=True)
    old_run = previous.with_name(previous.name.removesuffix('_launch'))
    replay = []
    for report in sorted((old_run/'evaluation').rglob('report.json')):
        for iid, payload in json.loads(report.read_text()).items():
            if isinstance(payload, dict) and type(payload.get('resolved')) is bool:
                result = evaluation_integrity(report.parent, iid, payload)
                replay.append({'instance_id': iid, 'official_resolved': payload['resolved'], **result})
    write_json(CHECK/'archived_grading_integrity.json', replay)
    result = {'not_benchmark_evidence': True, 'real_model_calls': 0, 'exports': exports,
              'probes': probes, 'archived_reports_checked': len(replay),
              'incomplete_reports': sum(not row['complete'] for row in replay),
              'retained_passes': [row['instance_id'] for row in replay if row['complete'] and row['official_resolved']],
              'incomplete_repositories': dict(Counter(row['instance_id'].rsplit('-', 1)[0]
                  for row in replay if not row['complete']))}
    write_json(CHECK/'runtime_verification.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'probes'}), flush=True)


if __name__ == '__main__':
    main()
