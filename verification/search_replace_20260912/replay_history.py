"""Replay archived rejected edits against original source without model calls or evaluation."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


CASES = (
    'markedjs__marked-1435', 'diegomura__react-pdf-433', 'chartjs__Chart.js-8710',
    'markedjs__marked-2483', 'Automattic__wp-calypso-26286', 'Automattic__wp-calypso-21648',
)


def collect(batch: Path, output: Path, base_inputs: Path) -> None:
    """Export only archived source, frozen edit authority and rejected model output."""
    output.mkdir(parents=True, exist_ok=False)
    for instance_id in CASES:
        case = batch / 'cases' / instance_id
        trajectory = case / 'trajectory'
        request = json.loads((trajectory / '002_edits.v4.request.json').read_text())
        response = json.loads((trajectory / '002_edits.v4.response.json').read_text())
        plan = json.loads((trajectory / 'generation_plan.json').read_text())
        scope = plan['edit_scope']
        edits = json.loads(response['choices'][0]['text'])['edits']
        blocks = {b['block_id']: b for b in scope['blocks']}
        target_regions = {blocks[e['target']]['region_id'] for e in edits}
        regions = [r for r in scope['regions'] if r['region_id'] in target_regions]
        files = [f for f in scope['files'] if f['file_id'] in {r['file_id'] for r in regions}]
        task = json.loads((case / 'input_context/task.json').read_text())
        source_root = output / instance_id / 'source'
        source_root.mkdir(parents=True)
        for file in files:
            region = next(r for r in regions if r['file_id'] == file['file_id'])
            if region['start_byte'] == 0 and region['end_byte'] == file['size']:
                source = region['source'].encode(file['encoding'])
            else:
                cache = base_inputs / instance_id
                provenance = json.loads((cache / 'provenance.json').read_text())
                assert provenance['base_commit'] == task['base_commit']
                source = subprocess.run(['git', '-C', str(cache / 'repo'), 'show',
                                         task['base_commit'] + ':' + file['path']],
                                        capture_output=True, check=True).stdout
            assert hashlib.sha256(source).hexdigest() == file['sha256']
            path = source_root / file['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source)
        filtered = dict(scope, files=files, regions=regions,
                        blocks=[b for b in scope['blocks'] if b['region_id'] in target_regions], creation_roots=[])
        data = {'instance_id': instance_id, 'base_commit': task['base_commit'], 'scope': filtered,
                'edits': edits, 'request_sha256': hashlib.sha256((trajectory / '002_edits.v4.request.json').read_bytes()).hexdigest(),
                'response_sha256': hashlib.sha256((trajectory / '002_edits.v4.response.json').read_bytes()).hexdigest(),
                'original_protocol': request['schema_name']}
        (source_root.parent / 'input.json').write_text(json.dumps(data, indent=2) + '\n')
        print('collected ' + instance_id, flush=True)


def replay(inputs: Path, method: Path) -> None:
    """Confirm old and mechanically re-encoded edits still fail complete-file syntax checks."""
    sys.path[:0] = [str(method / 'src'), str(method / 'tests')]
    from helpers import config, context
    from boundary_repair.adapters.repository import PatchCompiler, transaction_contents
    from boundary_repair.domain.errors import ValidationError
    from boundary_repair.domain.repair import EditBlock, EditRegion, EditScope, EditTransaction, FileState, SourceEdit
    from boundary_repair.domain.task import RepositorySnapshot
    from boundary_repair.kernel.files import tree_digest
    results = []
    check = unittest.TestCase()
    for instance_id in CASES:
        case = inputs / instance_id
        data = json.loads((case / 'input.json').read_text())
        scope = EditScope(tuple(FileState(**f) for f in data['scope']['files']),
                          tuple(EditRegion(**r) for r in data['scope']['regions']), (),
                          blocks=tuple(EditBlock(**b) for b in data['scope']['blocks']))
        snapshot = RepositorySnapshot(case / 'source', data['base_commit'], tree_digest(case / 'source'))
        transaction = EditTransaction(tuple(SourceEdit(**edit) for edit in data['edits']))
        blocks = {b.block_id: b for b in scope.blocks}
        regions = {r.region_id: r for r in scope.regions}
        files = {f.file_id: f for f in scope.files}
        searches = []
        for edit in transaction.edits:
            block = blocks[edit.target]
            region = regions[block.region_id]
            file = files[region.file_id]
            source = (snapshot.root / file.path).read_bytes()[block.start_byte:block.end_byte]
            searches.append(replace(edit, operation='replace_text', old_text=source.decode(file.encoding)))
        with TemporaryDirectory() as raw:
            cfg = config(Path(raw))
            for label, candidate in (('original', transaction), ('search_replace_same_output', EditTransaction(tuple(searches)))):
                originals, updated = transaction_contents(snapshot, scope, candidate)
                check.assertEqual(tree_digest(snapshot.root), snapshot.tree_sha256)
                with check.assertRaisesRegex(ValidationError, 'generated_syntax_invalid'):
                    PatchCompiler(cfg).check_syntax(originals, updated, context())
                results.append({'instance_id': instance_id, 'operation': label,
                                'application': 'matched', 'syntax': 'rejected',
                                'source_unchanged': True, 'model_calls': 0})
    print(json.dumps({'replays': results, 'model_calls': 0, 'benchmark_runs': 0,
                      'note': 'Historical rejection replay only; no corrected patch or repair-rate claim.'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('collect', 'replay'))
    parser.add_argument('--batch', type=Path)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--base-inputs', type=Path)
    parser.add_argument('--method', type=Path, default=Path('/method'))
    args = parser.parse_args()
    if args.mode == 'collect':
        collect(args.batch, args.inputs, args.base_inputs)
    else:
        replay(args.inputs, args.method)
