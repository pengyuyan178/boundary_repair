"""Audit completed frozen run metadata without reading benchmark answers or test output."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator


def load(path: Path) -> dict:
    """Read one explicitly named JSON metadata artifact."""
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path: Path) -> str:
    """Hash artifact bytes without interpreting benchmark or evaluation content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    """Check frozen hashes and report stage counts from immutable generation records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.batch.resolve()
    frozen = load(root / 'generation_frozen.json')
    for name in ('predictions', 'results', 'manifest'):
        path = root / (name + ('.json' if name == 'manifest' else '.jsonl'))
        assert digest(path) == frozen[name + '_sha256'], name
    records = [json.loads(line) for line in (root / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    cases = []
    all_models, reasons = Counter(), Counter()
    input_tokens = output_tokens = 0
    for record in records:
        case = root / 'cases' / record['instance_id']
        trajectory = case / 'trajectory'
        calls = []
        for path in sorted(trajectory.glob('*.request.json')):
            request = load(path)
            response_path = path.with_name(path.name.replace('.request.json', '.response.json'))
            response = load(response_path) if response_path.exists() else None
            call = {'request': path.name, 'schema_name': request['schema_name'], 'requested_model': request['model'],
                    'seed': request['seed'], 'prompt_chars': len(request['prompt']), 'response_available': response is not None}
            if response is not None:
                all_models[str(response.get('model'))] += 1
                usage = response.get('usage') or {}
                input_tokens += usage.get('prompt_tokens', 0)
                output_tokens += usage.get('completion_tokens', 0)
                call.update(model=response.get('model'), usage=usage, choices=[])
                schema = request['response_format'].get('json_schema', {}).get('schema')
                for choice in response.get('choices') or []:
                    reason = choice.get('finish_reason')
                    reasons[str(reason)] += 1
                    text = choice.get('text')
                    entry = {'finish_reason': reason}
                    try:
                        payload = json.loads(text) if isinstance(text, str) else None
                    except json.JSONDecodeError:
                        payload = None
                    entry['json_object'] = isinstance(payload, dict)
                    if isinstance(payload, dict) and schema is not None:
                        errors = list(Draft202012Validator(schema).iter_errors(payload))
                        entry['schema_passed'] = not errors
                        entry['schema_errors'] = [{'path': list(error.absolute_path), 'validator': error.validator}
                                                  for error in errors[:20]]
                    if isinstance(payload, dict) and request['schema_name'] == 'edits.v3':
                        edits = payload.get('edits', [])
                        entry['operation_counts'] = dict(Counter(item.get('operation') for item in edits if isinstance(item, dict)))
                    call['choices'].append(entry)
            calls.append(call)
        patch = case / 'patch' / 'final.patch'
        plan_path = trajectory / 'generation_plan.json'
        contract_path = trajectory / 'contracts.json'
        plan = load(plan_path) if plan_path.exists() else {}
        contracts = load(contract_path) if contract_path.exists() else {}
        media_path = case / 'assets' / 'availability.json'
        assets = load(media_path).get('assets', []) if media_path.exists() else []
        item = {'inference': record, 'calls': calls, 'plan_present': bool(plan),
                'generation_mode': plan.get('generation_mode'),
                'contract_diagnostics': contracts.get('diagnostics', []),
                'extraction_status': contracts.get('extraction_status'),
                'asset_status_counts': dict(Counter(asset['status'] for asset in assets)),
                'patch_exists': patch.exists(), 'patch_bytes': patch.stat().st_size if patch.exists() else 0,
                'patch_sha256': digest(patch) if patch.exists() else None}
        scope = plan.get('edit_scope') or {}
        item['scope_files'] = [file['path'] for file in scope.get('files', [])]
        item['scope_diagnostics'] = scope.get('diagnostics', [])
        if patch.exists():
            item['changed_files'] = [line for line in patch.read_text(encoding='utf-8').splitlines() if line.startswith('diff --git ')]
        cases.append(item)
    report = {'frozen_hashes_verified': True, 'selected': len(frozen['selected']), 'attempted': len(records),
              'generated': frozen['generated'], 'final_summary': load(root / 'final_summary.json'),
              'actual_model_counts': dict(all_models), 'finish_reason_counts': dict(reasons),
              'prompt_tokens': input_tokens, 'completion_tokens': output_tokens,
              'generation_mode_counts': dict(Counter(row.get('generation_mode') for row in records)),
              'cases': cases, 'benchmark_answers_read': False, 'official_test_output_read': False,
              'post_generation_audit_only': True, 'regeneration_performed': False}
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key not in {'cases', 'final_summary'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
