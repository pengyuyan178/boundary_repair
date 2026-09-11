"""Audit frozen generation inputs without model calls, target execution or evaluation artifacts."""
import argparse
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
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
    parser.add_argument('--batch', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--message', default='Record verified layer implementation')
    args = parser.parse_args()
    if args.checkpoint is not None:
        print(json.dumps(checkpoint(args.checkpoint, args.message)))
        return
    if args.batch is None or args.output is None:
        parser.error('--batch and --output are required for context audits')
    result = audit_context(args.batch)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=True))


def checkpoint(snapshot: Path, message: str) -> dict:
    """Record a frozen snapshot on a separate Git branch without changing HEAD or the user's index."""
    root = Path(__file__).resolve().parents[1]
    reference = 'refs/heads/codex/layer-repair-20260912'
    state = json.loads((snapshot / 'source_state.json').read_text(encoding='utf-8'))
    previous = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '--quiet', reference],
                              capture_output=True, text=True)
    parent = previous.stdout.strip() if previous.returncode == 0 else state['git_head']
    index = (snapshot / 'checkpoint.index').resolve()
    if index.exists() or (snapshot / 'git_checkpoint.json').exists():
        raise ValueError('checkpoint_already_exists')
    env = {**os.environ, 'GIT_INDEX_FILE': str(index), 'GIT_AUTHOR_NAME': 'Codex',
           'GIT_AUTHOR_EMAIL': 'codex@local', 'GIT_COMMITTER_NAME': 'Codex', 'GIT_COMMITTER_EMAIL': 'codex@local'}
    def git(*args, data=None):
        return subprocess.check_output(['git', '-C', str(root), *args], input=data, env=env).decode().strip()
    git('read-tree', parent)
    entries = []
    with tarfile.open(snapshot / 'source.tar.gz') as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name.startswith('node_modules/'):
                continue
            blob = git('hash-object', '-w', '--stdin', data=archive.extractfile(member).read())
            mode = '100755' if member.mode & 0o111 else '100644'
            entries.append(f'{mode} {blob}\t{member.name}\n')
    git('update-index', '--index-info', data=''.join(entries).encode())
    tree = git('write-tree')
    commit = git('commit-tree', tree, '-p', parent, data=(message + '\n').encode())
    git('update-ref', reference, commit, parent if previous.returncode == 0 else '0' * 40)
    result = {'branch': reference, 'commit': commit, 'parent': parent,
              'source_archive_sha256': state['archive_sha256'], 'worktree_and_main_index_unchanged': True}
    (snapshot / 'git_checkpoint.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    main()
