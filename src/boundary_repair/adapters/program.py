"""Parse-only program adapter, sound limited Boolean entry models, and hash-bound patch emission."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

from boundary_repair.adapters.frontend import collect_sources, parse_sources
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import Effect, HoleFilling, LocalRepairModel, PatchArtifact, PatchPlan, ProofAssumptions, RepairBoundary
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import Coverage, ObservationKey, SolverStatus, Term, Witness
from boundary_repair.domain.task import ProgramIndex, RepositorySnapshot, SourceSpan, TaskInput
from boundary_repair.kernel.files import allowed_source, safe_path, source_slice, tree_digest
from boundary_repair.kernel.terms import evaluate, literal, literal_assignments, symbol, term_from_json


@dataclass(slots=True)
class ProgramAdapter:
    """Cache only one immutable snapshot; never execute target imports, callbacks or build scripts."""
    config: ExperimentConfig
    _key: tuple[str, str] | None = field(default=None, init=False, repr=False)
    _data: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _index: ProgramIndex | None = field(default=None, init=False, repr=False)

    def index(self, snapshot: RepositorySnapshot, context: RunContext) -> ProgramIndex:
        """Index production sources with exact spans; include explicit partial file fallbacks."""
        context.budget.check_deadline()
        key = (str(snapshot.root.resolve()), snapshot.tree_sha256)
        if key == self._key and self._index is not None:
            return self._index
        files, truncated = collect_sources(snapshot, self.config)
        parsed = parse_sources(files, self.config, context)
        locations, names, interfaces = [], set(), []
        diagnostics = list(truncated)
        for file in files:
            raw = file['source'].encode('utf-8')
            if raw:
                locations.append(SourceSpan(file['path'], 1, max(1, len(raw.splitlines())),
                                            hashlib.sha256(raw).hexdigest(), 0, len(raw), 'File'))
        for row in parsed['files']:
            names.update(row.get('identifiers', []))
            diagnostics.extend(f"{row['path']}:{d}" for d in row.get('unsupported', []))
            locations.extend(SourceSpan(**item) for item in row.get('locations', []))
            interfaces.extend((SourceSpan(**item['site']), tuple(item['params'])) for item in row.get('functions', []))
        self._data = parsed
        self._key = key
        self._index = ProgramIndex(tuple(sorted(names)), tuple(locations), tuple(diagnostics), tuple(interfaces))
        return self._index

    def function_summary(self, site: SourceSpan, snapshot: RepositorySnapshot,
                         context: RunContext) -> dict[str, Any] | None:
        """Return a summary only for the exact parser-certified Boolean-return byte range."""
        self.index(snapshot, context)
        for file in self._data['files']:
            for function in file.get('functions', []):
                if SourceSpan(**function['site']) == site:
                    return function
        return None

    def summarize(self, boundary: RepairBoundary, witnesses: tuple[Witness, ...],
                  snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel:
        """A4: construct z_b and identity continuation for pure Boolean entry functions only.

        The proof universe is direct function-entry invocations with supplied Boolean arguments,
        not reachability from the application UI. Unsupported closures, mutation, async code,
        incomplete arguments or other output projections retain PARTIAL/UNKNOWN.
        """
        unsupported = LocalRepairModel(boundary, (), (), (), (), ProofAssumptions(False, False, False, False), Coverage.PARTIAL)
        if len(boundary.sites) != 1:
            return unsupported
        function = self.function_summary(boundary.sites[0], snapshot, context)
        if function is None:
            return unsupported
        params = tuple(function['params'])
        reads = tuple(feature.feature_id for feature in boundary.readable_features)
        if any(name not in params for name in reads):
            return unsupported
        actual, inputs, outputs, constraints = [], [], [], []
        complete = True
        for witness in witnesses:
            if len(witness.observations) != 1:
                complete = False
                continue
            target = witness.observations[0]
            if target.entity_id != function['name'] or target.property_name != 'return':
                continue
            values = literal_assignments(target.context)
            if values is None or set(values) != set(params) or any(type(value) is not bool for value in values.values()):
                complete = False
                continue
            index = len(actual)
            actual.append(replace(witness, reachability=SolverStatus.SAT))
            inputs.append(Term('and', tuple(Term('eq', (symbol(name), literal(values[name]))) for name in reads)))
            outputs.append(symbol(f'local_out:{index}'))
            original = evaluate(term_from_json(function['expression']), values)
            constraints.append(Term('eq', (symbol(f'base_out:{index}'), literal(original))))
        for i, left in enumerate(inputs):
            for j in range(i):
                if left == inputs[j]:
                    constraints.append(Term('eq', (outputs[i], outputs[j])))
        certified = bool(actual) and complete
        return LocalRepairModel(boundary, tuple(actual), tuple(inputs), tuple(outputs), tuple(constraints),
                                ProofAssumptions(True, True, certified, True),
                                Coverage.COMPLETE if certified else Coverage.PARTIAL)

    def effects(self, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext) -> tuple[Effect, ...]:
        """A5: return a local property effect where justified, otherwise explicit unknown effects.

        Identity-return edits have a precise local return projection, not a proof of whole-program
        noninterference. Consumer edits and object sharing currently use conservative partial
        effects. Empty impact is never inferred from unsupported analysis.
        """
        result = []
        for hole in plan.holes:
            source_slice(snapshot.root, hole.site)
            function = self.function_summary(hole.site, snapshot, context)
            if function is not None:
                result.append(Effect(ObservationKey(function['name'], 'return', literal(True)),
                                     symbol('effect:may_change'), Coverage.COMPLETE))
            else:
                result.append(Effect(ObservationKey(hole.site.symbol or 'file:' + hole.site.path, '*', literal(True)),
                                     symbol('effect:unknown'), Coverage.PARTIAL))
        if not result:
            result.append(Effect(ObservationKey('*', '*', literal(True)), symbol('effect:unknown'), Coverage.PARTIAL))
        return tuple(result)

    def materialize(self, task: TaskInput, plan: PatchPlan, fillings: tuple[HoleFilling, ...],
                    snapshot: RepositorySnapshot, context: RunContext) -> PatchArtifact:
        """A6: verify exact hashes, nonoverlap, UTF-8 and syntax; build diff without touching base.

        The patched buffers are reparsed but never executed. Tests, secret paths and generated
        copies are rejected by the edit policy. No-newline markers preserve valid Git patches.
        """
        context.budget.check_deadline()
        if snapshot.base_commit != task.base_commit:
            raise ValidationError('base_commit_mismatch')
        if tree_digest(snapshot.root) != snapshot.tree_sha256:
            raise ValidationError('snapshot_changed_since_export')
        fills = {f.hole_id: f.source_text for f in fillings}
        if len(fills) != len(fillings) or set(fills) != {h.hole_id for h in plan.holes}:
            raise ValidationError('hole_ids_must_match_exactly')
        if len({h.hole_id for h in plan.holes}) != len(plan.holes):
            raise ValidationError('duplicate_plan_hole')
        originals: dict[str, bytes] = {}
        edits: dict[str, list[tuple[int, int, bytes]]] = {}
        for hole in plan.holes:
            if not allowed_source(hole.site.path, for_edit=True):
                raise ValidationError('forbidden_edit_path')
            original, start, end = source_slice(snapshot.root, hole.site)
            text = fills[hole.hole_id]
            if not isinstance(text, str) or '\x00' in text or len(text.encode()) > self.config.integration.max_file_bytes:
                raise ValidationError('invalid_hole_text')
            originals[hole.site.path] = original
            edits.setdefault(hole.site.path, []).append((start, end, text.encode('utf-8')))
        patched: dict[str, bytes] = {}
        for path, changes in edits.items():
            ordered = sorted(changes)
            for (start, end, _), (next_start, _, _) in zip(ordered, ordered[1:]):
                if end > next_start or start == end == next_start:
                    raise ValidationError('overlapping_holes')
            content = originals[path]
            for start, end, replacement in reversed(ordered):
                content = content[:start] + replacement + content[end:]
            content.decode('utf-8')
            patched[path] = content
        if not any(patched[path] != originals[path] for path in patched):
            raise NoAdmissiblePatch('no_op_patch')
        parsed = parse_sources([{'path': p, 'source': data.decode('utf-8')} for p, data in patched.items()], self.config, context)
        for file in parsed['files']:
            if any(item.startswith('syntax:') for item in file.get('unsupported', [])):
                raise ValidationError('generated_syntax_invalid')
        # For certified Boolean holes, reparse proves that the new expression cannot read outside
        # the declared interface or introduce calls, closures, side effects, or fresh parameters.
        for hole in plan.holes:
            if hole.expected_type != 'boolean-expression':
                continue
            matches = [f for row in parsed['files'] if row['path'] == hole.site.path
                       for f in row.get('functions', []) if f['name'] == hole.site.symbol]
            if len(matches) != 1:
                raise ValidationError('boolean_grammar_escaped')
            from boundary_repair.kernel.terms import symbols
            emitted = term_from_json(matches[0]['expression'])
            if set(symbols(emitted)) - set(hole.allowed_symbols):
                raise ValidationError('undeclared_boolean_read')
            original_summary = self.function_summary(hole.site, snapshot, context)
            if original_summary is None:
                raise ValidationError('original_boolean_summary_missing')
            original_term = term_from_json(original_summary['expression'])
            for obligation in plan.obligations:
                for target in obligation.targets:
                    if target.entity_id != hole.site.symbol or target.property_name != 'return':
                        continue
                    values = literal_assignments(target.context)
                    if values is None:
                        raise ValidationError('boolean_obligation_context_unsupported')
                    environment = dict(values)
                    environment['return'] = evaluate(emitted, values)
                    environment['base:return'] = evaluate(original_term, values)
                    if evaluate(obligation.relation, environment) is not True:
                        raise ValidationError('generated_boolean_obligation_violated')
        patches = []
        for path in sorted(patched):
            if patched[path] == originals[path]:
                continue
            before = originals[path].decode('utf-8').splitlines(keepends=True)
            after = patched[path].decode('utf-8').splitlines(keepends=True)
            lines = list(difflib.unified_diff(before, after, fromfile='a/' + path, tofile='b/' + path, lineterm='\n'))
            normalized = []
            for line in lines:
                normalized.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
            # Git quotes paths with whitespace, quotes or non-ASCII using JSON-compatible quoting.
            left, right = 'a/' + path, 'b/' + path
            quote = lambda value: json.dumps(value, ensure_ascii=False) if any(c.isspace() or c in '"\\' for c in value) else value
            normalized[0] = '--- ' + quote(left) + '\n'
            normalized[1] = '+++ ' + quote(right) + '\n'
            patches.append(f'diff --git {quote(left)} {quote(right)}\n' + ''.join(normalized))
        diff = ''.join(patches)
        return PatchArtifact(task.instance_id, task.base_commit, plan.plan_id, diff, hashlib.sha256(diff.encode('utf-8')).hexdigest())
