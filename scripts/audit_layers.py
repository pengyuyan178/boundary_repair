"""Audit frozen generation inputs without model calls, target execution or evaluation artifacts."""
import argparse
from dataclasses import fields, is_dataclass
from enum import Enum
import json
from pathlib import Path
import sys
from types import UnionType
from typing import get_args, get_origin, get_type_hints

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from boundary_repair.algorithms.rendering import TransactionRenderer, edit_transaction_schema
from boundary_repair.domain.repair import PatchPlan
from boundary_repair.domain.runtime import RunContext, SearchPolicy, BudgetLedger, BudgetLimits
from boundary_repair.domain.task import TaskInput
from boundary_repair.ports import ModelResponse


def restore(annotation, value):
    """Restore archived dataclasses using the existing decision-handoff replay representation."""
    if value is None:
        return None
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return annotation(**{f.name: restore(hints[f.name], value[f.name]) for f in fields(annotation) if f.name in value})
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        structured = [t for t in args if is_dataclass(t) or get_origin(t) is tuple]
        return restore(structured[0], value) if structured else value
    if origin is tuple:
        return tuple(restore(args[0] if len(args) == 2 and args[1] is Ellipsis else args[i], v)
                     for i, v in enumerate(value))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    return value


class CaptureModel:
    """Capture a request locally; the placeholder response is never compiled or submitted."""
    def complete(self, request, context):
        self.request = request
        return ModelResponse(json.dumps({'edits': [{'operation': 'replace_block', 'target': 'audit-only',
            'new_text': 'audit-only', 'old_text': '', 'destination': ''}]}), 0, 'offline-audit')


def audit_context(batch: Path) -> dict:
    """Compare actual archived inputs with the new projection and verify source and obligations."""
    rows = []
    for path in sorted((batch / 'cases').glob('*/trajectory/*edits.v4.request.json')):
        case = path.parents[1]
        task = restore(TaskInput, json.loads((case / 'input_context/task.json').read_text(encoding='utf-8')))
        plan = restore(PatchPlan, json.loads((case / 'trajectory/generation_plan.json').read_text(encoding='utf-8')))
        request = json.loads(path.read_text(encoding='utf-8'))
        original = json.loads(request['prompt'])
        model = CaptureModel()
        context = RunContext('audit', task.instance_id, 42, SearchPolicy(), BudgetLedger(BudgetLimits()))
        TransactionRenderer(model, request['max_completion_tokens']).render(task, plan, context)
        actual = json.loads(model.request.prompt)
        for key in ('obligations', 'soft_hypotheses', 'interpretation_groups', 'evidence_sources'):
            assert original[key] == actual[key], (task.instance_id, key)
        assert model.request.output_schema == request['response_format']['json_schema']['schema']
        assert model.request.output_schema == edit_transaction_schema(plan.edit_scope)
        regions = {r['region_id']: r for r in actual['regions']}
        for region in plan.edit_scope.regions:
            assert regions[region.region_id]['source'] == region.source
        previous = {r['region_id']: r.get('source', ''.join(line['text'] for line in r.get('lines', [])))
                    for r in original['regions']}
        assert all(previous[rid] == region['source'] for rid, region in regions.items())
        assert context.budget.model_calls == 0
        rows.append({'instance_id': task.instance_id, 'before_chars': len(request['prompt']),
            'after_chars': len(model.request.prompt), 'reduction_percent': round(
                100 * (1 - len(model.request.prompt) / len(request['prompt'])), 2),
            'source_and_permissions_verified': True, 'obligations_verified': True,
            'context_manifest': actual['context_manifest'],
            'component_chars': {key: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
                                for key, value in actual.items()}})
    return {'mode': 'offline_context_replay', 'real_model_calls': 0, 'cases': rows,
            'benchmark_score': None, 'token_counts_are_not_estimated_from_chars': True}


def main():
    """Save an auditable context comparison in a new output file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit_context(args.batch)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
