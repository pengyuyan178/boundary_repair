"""Stage and replay historical evidence using verified base files, without patch generation or scoring."""
import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def digest(data):
    """Hash an immutable evidence or source byte sequence."""
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    """Write one new audit artifact without overwriting a previous run."""
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def stage(args):
    """Read only sanitized tasks, pre-generation scopes and archived evidence responses."""
    from boundary_repair.adapters.dataset import load_generation_tasks
    from boundary_repair.adapters.storage import safe_component
    from boundary_repair.kernel.files import allowed_source, safe_path
    from boundary_repair.kernel.codec import plain
    tasks = load_generation_tasks(args.prepared / 'tasks.json')
    args.destination.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in tasks:
        iid = safe_component(task.instance_id)
        case = args.batch / 'cases' / iid
        trajectory = case / 'trajectory'
        row = {'instance_id': iid, 'status': 'no_archived_evidence_response'}
        responses = sorted(trajectory.glob('*evidence*.response.json'))
        if not responses:
            rows.append(row)
            continue
        response_file = responses[0]
        request_file = response_file.with_name(response_file.name.replace('.response.json', '.request.json'))
        plan_file = trajectory / 'generation_plan.json'
        if not request_file.is_file() or not plan_file.is_file():
            row['status'] = 'missing_frozen_evidence_context'
            rows.append(row)
            continue
        request = json.loads(request_file.read_text())
        payload = json.loads(request['prompt'])
        original_plan = json.loads(plan_file.read_text())
        scope = original_plan.get('read_scope') or original_plan['edit_scope']
        target = args.destination / iid
        source_root = target / 'source'
        source_root.mkdir(parents=True)
        missing, files = [], []
        for file in scope['files']:
            if not allowed_source(file['path']):
                missing.append({'path': file['path'], 'reason': 'excluded_source'})
                continue
            regions = sorted((r for r in scope['regions'] if r['file_id'] == file['file_id']), key=lambda r: r['start_byte'])
            parts, cursor = [], 0
            for region in regions:
                if region['start_byte'] != cursor:
                    break
                parts.append(region['source'].encode(file['encoding']))
                cursor = region['end_byte']
            source = b''.join(parts)
            origin = 'frozen_pre_generation_source'
            if cursor != file['size'] or digest(source) != file['sha256']:
                cache = args.cache / iid
                provenance = cache / 'provenance.json'
                if not provenance.is_file() or not (cache / 'repo').is_dir():
                    missing.append({'path': file['path'], 'reason': 'partial_source_and_no_cache'})
                    continue
                if json.loads(provenance.read_text()).get('base_commit') != task.base_commit:
                    missing.append({'path': file['path'], 'reason': 'cache_base_mismatch'})
                    continue
                exported = subprocess.run(['git', '-C', str(cache / 'repo'), 'show', task.base_commit + ':' + file['path']],
                                          capture_output=True)
                if exported.returncode or digest(exported.stdout) != file['sha256']:
                    missing.append({'path': file['path'], 'reason': 'base_file_hash_mismatch'})
                    continue
                source, origin = exported.stdout, 'existing_cache_exact_base_commit'
            destination = safe_path(source_root, file['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source)
            files.append({'path': file['path'], 'sha256': digest(source), 'origin': origin})
        row.update(status='ready' if not missing else 'incomplete_verified_source', files=files, missing=missing)
        write(target / 'tasks.json', [plain(task)])
        response = json.loads(response_file.read_text())
        choice = response['choices'][0]
        evidence = choice['text'] if 'text' in choice else choice['message']['content']
        old_contracts = json.loads((trajectory / 'contracts.json').read_text())
        old_localization = json.loads((trajectory / 'localization.json').read_text())
        old_hard = old_contracts.get('must', []) + old_contracts.get('frames', [])
        metadata = {'scope': scope, 'evidence_catalog': payload['evidence_catalog'],
            'observation_interfaces': payload.get('observation_interfaces', []), 'evidence': evidence,
            'schema': request['schema_name'], 'request_sha256': digest(request_file.read_bytes()),
            'response_sha256': digest(response_file.read_bytes()), 'scope_sha256': digest(plan_file.read_bytes()),
            'before': {'interfaces': len(payload.get('observation_interfaces', [])),
                'bound_obligations': sum(bool(c.get('entry_cases')) for c in old_hard),
                'verdicts': dict(Counter(a['verdict'] for a in old_localization.get('assessments', []))),
                'generation_mode': original_plan['generation_mode']}}
        write(target / 'evidence.json', metadata)
        rows.append(row)
        print(json.dumps({'instance_id': iid, 'stage': row['status']}, ensure_ascii=True), flush=True)
    write(args.destination / 'manifest.json', {'mode': 'historical_evidence_only', 'seed': 42,
        'population': len(rows), 'cases': rows, 'provider_calls': 0, 'new_patches': 0,
        'forbidden_inputs': ['gold_patch', 'test_patch', 'generated_patch', 'evaluation_results']})


class ForbiddenModel:
    """Prevent an offline audit from requesting evidence or generating a patch."""
    def complete(self, request, context):
        raise AssertionError('offline_replay_model_call_forbidden')


def analyze_case(args):
    """Reparse the exact archived evidence and compare deterministic mechanism decisions."""
    from audit_layers import restore
    from boundary_repair.adapters.dataset import load_generation_tasks
    from boundary_repair.adapters.logic import LogicAdapter
    from boundary_repair.adapters.program import ProgramAdapter
    from boundary_repair.algorithms.expressivity import ExpressivityLocalization
    from boundary_repair.algorithms.specification import SpecificationRecovery
    from boundary_repair.algorithms.synthesis import ScopeSynthesis, scoped_plans, scope_ranges
    from boundary_repair.config import load_config
    from boundary_repair.domain.repair import EditScope
    from boundary_repair.domain.runtime import BudgetLedger, RunContext
    from boundary_repair.domain.specification import ObservationInterface
    from boundary_repair.domain.task import RepositorySnapshot
    from boundary_repair.kernel.codec import plain
    from boundary_repair.kernel.evidence import evidence_catalog_v3, parse_evidence_v4
    from boundary_repair.kernel.files import tree_digest
    from boundary_repair.kernel.retrieval import scope_snippets
    metadata = json.loads((args.case / 'evidence.json').read_text())
    task = load_generation_tasks(args.case / 'tasks.json')[0]
    image_ids = {item['image_id'] for item in metadata['evidence_catalog']['images']}
    assert image_ids <= {asset.source_id for asset in task.assets}
    task = replace(task, assets=tuple(asset for asset in task.assets if asset.source_id in image_ids))
    original_scope = restore(EditScope, metadata['scope'])
    grouped = {}
    for span in metadata['evidence_catalog']['spans']:
        if span['kind'] == 'base_code':
            grouped.setdefault(int(span['span_id'].split(':')[1]), []).append(span)
    states = {file.path: file for file in original_scope.files}
    code = []
    for number, spans in sorted(grouped.items()):
        spans.sort(key=lambda span: span['start'])
        path, start, end = spans[0]['path'], spans[0]['start'], spans[-1]['end']
        source = (args.case / 'source' / path).read_bytes().decode(states[path].encoding)
        supplied = ''.join(span['text'] for span in spans)
        assert supplied == source[start:end]
        assert all(a['end'] == b['start'] for a, b in zip(spans, spans[1:]))
        code.append({'path': path, 'source': supplied, 'start_char': start,
            'start_byte': len(source[:start].encode(states[path].encoding)),
            'start_line': source[:start].count('\n') + 1, 'truncated': start != 0 or end != len(source)})
    code = tuple(code)
    if evidence_catalog_v3(task, code) != metadata['evidence_catalog']:
        write(args.output, {'instance_id': task.instance_id, 'status': 'historical_catalogue_mismatch'})
        return
    old_interfaces = tuple(restore(ObservationInterface, i) for i in metadata['observation_interfaces'])
    print(json.dumps({'phase': 'archived_evidence_validation'}), flush=True)
    evidence = parse_evidence_v4(metadata['evidence'], task, code, old_interfaces)
    print(json.dumps({'phase': 'mechanism_analysis'}), flush=True)
    config = load_config(args.method / 'configs/local.json', project_root=args.method)
    config = replace(config, node_candidates=('/usr/bin/node',), results_root=args.output.parent,
        integration=replace(config.integration, parser_module=args.method / 'node_modules/typescript'))
    ctx = RunContext('historical-mechanism-replay', task.instance_id, 42, config.policy, BudgetLedger(config.budget))
    snapshot = RepositorySnapshot(args.case / 'source', task.base_commit, tree_digest(args.case / 'source'))
    program, logic = ProgramAdapter(config), LogicAdapter()
    service = SpecificationRecovery(ForbiddenModel(), program, logic)
    program.source_scope(snapshot, ctx, task.problem_statement)
    bindings = service.bind_entities(evidence, snapshot, ctx)
    theory = service.build_interpretation_space(evidence, bindings, ctx)
    contracts = service.derive_contracts(evidence, bindings, theory, ctx)
    localization = ExpressivityLocalization(program, logic, config.integration.max_boundaries).locate(task, contracts, snapshot, ctx)
    synthesizer = ScopeSynthesis(ForbiddenModel(), program, logic)
    scope = program.source_scope(snapshot, ctx)
    projected = tuple(a for a in localization.assessments if a.certificate and a.construction and a.proof)
    if projected and contracts.must:
        plans = synthesizer.projection_plans(contracts, scope, localization, projected, snapshot, ctx)
        plan = synthesizer.select_projection_scope(plans, contracts, ctx)
    else:
        plans = scoped_plans(contracts, scope, localization, snapshot, ctx)
        plan = synthesizer.select_minimal_scope(plans, contracts, snapshot, ctx)
    after = {'interfaces': len(program.observation_interfaces(snapshot, ctx)),
        'available_scenarios': len(program.observation_scenarios(snapshot, ctx)),
        'bound_obligations': sum(bool(c.entry_cases) for c in contracts.must + contracts.frames),
        'verdicts': dict(Counter(a.verdict.value for a in localization.assessments)),
        'planned_mode': plan.generation_mode, 'scope_narrowed': scope_ranges(plan.edit_scope) != scope_ranges(scope),
        'enforced_obligations': list(plan.enforced_obligations), 'constraints': len(contracts.must + contracts.frames),
        'unknown_reasons': dict(Counter(reason for a in localization.assessments for reason in a.unresolved)),
        'diagnostics': list(contracts.diagnostics)}
    write(args.output, {'instance_id': task.instance_id, 'status': 'replayed', 'before': metadata['before'], 'after': after,
        'selection_changed': bool(after['scope_narrowed'] or after['enforced_obligations']),
        'source_sha256': snapshot.tree_sha256, 'request_sha256': metadata['request_sha256'],
        'response_sha256': metadata['response_sha256'], 'model_calls': ctx.budget.model_calls,
        'new_patch_count': 0, 'input_scope': 'verified_historical_read_files_not_entire_repository',
        'catalogue_supply_is_not_a_verified_new_model_association': True,
        'contracts': plain(contracts), 'localization': plain(localization), 'plan': plain(plan)})


def analyze(args):
    """Inventory every historical case and isolate replay failures through child process exit status."""
    manifest = json.loads((args.inputs / 'manifest.json').read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for item in manifest['cases']:
        iid = item['instance_id']
        if item['status'] != 'ready':
            rows.append({'instance_id': iid, 'status': item['status'], 'missing': item.get('missing', [])})
            continue
        target = args.output / (iid + '.json')
        command = [sys.executable, str(Path(__file__).resolve()), '--method', str(args.method), '--case',
                   str(args.inputs / iid), '--output', str(target)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            (args.output / (iid + '.stderr.log')).write_text(result.stderr)
            phases = [json.loads(line)['phase'] for line in result.stdout.splitlines()]
            phase = phases[-1] if phases else 'source_catalogue_validation'
            reason = result.stderr.splitlines()[-1] if result.stderr else 'missing_error_diagnostic'
            rejected = (phase == 'archived_evidence_validation'
                        and reason.startswith('boundary_repair.domain.errors.ValidationError:'))
            rows.append({'instance_id': iid, 'status': 'archived_evidence_rejected' if rejected else 'replay_error',
                         'phase': phase, 'reason': reason, 'exit_code': result.returncode})
        else:
            record = json.loads(target.read_text())
            rows.append({k: v for k, v in record.items() if k not in {'contracts', 'localization', 'plan'}})
        print(json.dumps({'instance_id': iid, 'status': rows[-1]['status']}, ensure_ascii=True), flush=True)
    write(args.output / 'report.json', {'mode': 'historical_evidence_replay', 'seed': 42,
        'population': len(rows), 'statuses': dict(Counter(r['status'] for r in rows)), 'cases': rows,
        'provider_calls': 0, 'new_patch_count': 0, 'benchmark_runs': 0})


def main():
    """Dispatch trusted staging or isolated offline analysis."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--batch', type=Path)
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--cache', type=Path, default=Path('/data/zy/tracerepair_base_inputs'))
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--case', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sys.path[:0] = [str(args.method / 'src'), str(args.method / 'scripts')]
    if args.batch is not None:
        stage(args)
    elif args.case is not None:
        analyze_case(args)
    else:
        analyze(args)


if __name__ == '__main__':
    main()
