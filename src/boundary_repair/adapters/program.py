"""Parse-only program adapter, sound limited Boolean entry models, and hash-bound patch emission."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field, replace
import difflib
import hashlib
import json
import re
from typing import Any

from boundary_repair.adapters.frontend import analyze_sources, parse_sources
from boundary_repair.adapters.repository import SourceRepository, PatchCompiler, bind_edit_blocks
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import NoAdmissiblePatch, ValidationError
from boundary_repair.domain.repair import EditScope, EditTransaction, Effect, HoleFilling, LocalRepairModel, PatchArtifact, PatchPlan, ProofAssumptions, RepairBoundary
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import Coverage, ObservationInterface, ObservationKey, SolverStatus, Term, Witness
from boundary_repair.domain.task import ProgramIndex, RepositorySnapshot, SourceSpan, TaskInput
from boundary_repair.kernel.files import allowed_source, source_slice, tree_digest
from boundary_repair.kernel.terms import evaluate, literal, literal_assignments, symbol, term_from_json, typed_key, walk, symbols
from boundary_repair.kernel.boolean import program_term, program_sort, program_evaluate


@dataclass(slots=True)
class ProgramAdapter:
    """Cache only one immutable snapshot; never execute target imports, callbacks or build scripts."""
    config: ExperimentConfig
    _key: tuple[str, ...] | None = field(default=None, init=False, repr=False)
    _data: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _index: ProgramIndex | None = field(default=None, init=False, repr=False)
    _scope: EditScope | None = field(default=None, init=False, repr=False)

    def source_scope(self, snapshot: RepositorySnapshot, context: RunContext, query: str = '') -> EditScope:
        """Freeze task-local retrieval once, independently of semantic parser availability."""
        key = (str(snapshot.root.resolve()), snapshot.tree_sha256, context.run_id, context.instance_id)
        if key != self._key:
            from boundary_repair.kernel.files import safe_path
            scope = SourceRepository(self.config).retrieve(snapshot, query, context)
            sources = [{'path': file.path, 'source': safe_path(snapshot.root, file.path).read_bytes().decode(file.encoding)}
                       for file in scope.files]
            parsed = analyze_sources(sources, self.config, context)
            self._scope = bind_edit_blocks(snapshot, scope, parsed, query)
            self._key, self._index, self._data = key, None, parsed
        return self._scope

    def freeze_plan(self, plan: PatchPlan, context: RunContext) -> None:
        """Persist capabilities and generation mode before any code-generation operation."""
        from boundary_repair.adapters.storage import safe_component, write_json
        destination = (self.config.results_root / safe_component(context.run_id) / 'cases'
                       / safe_component(context.instance_id) / 'trajectory' / 'generation_plan.json')
        if destination.exists():
            raise ValidationError('generation_plan_already_frozen')
        write_json(destination, plan)

    def compile(self, task: TaskInput, plan: PatchPlan, transaction: EditTransaction,
                snapshot: RepositorySnapshot, context: RunContext) -> PatchArtifact:
        """Delegate language-neutral transaction validation to the shared patch compiler."""
        return PatchCompiler(self.config).compile(task, plan, transaction, snapshot, context)

    def index(self, snapshot: RepositorySnapshot, context: RunContext) -> ProgramIndex:
        """Analyze only retrieved UTF-8 files; text retrieval never waits for whole-repo parsing."""
        from boundary_repair.kernel.files import safe_path
        scope = self.source_scope(snapshot, context)
        if self._index is not None:
            return self._index
        locations = []
        diagnostics = list(scope.diagnostics)
        supported_paths = set()
        for file in scope.files:
            if file.encoding != 'utf-8':
                diagnostics.append('semantic_encoding_unsupported:' + file.path)
                continue
            supported_paths.add(file.path)
            raw = safe_path(snapshot.root, file.path).read_bytes()
            if raw:
                locations.append(SourceSpan(file.path, 1, max(1, len(raw.splitlines())),
                                            file.sha256, 0, len(raw), 'File'))
        parsed = self._data
        names, interfaces, local_interfaces = set(), [], []
        for row in parsed['files']:
            if row['path'] not in supported_paths:
                continue
            names.update(row.get('identifiers', []))
            diagnostics.extend(f"{row['path']}:{d}" for d in row.get('unsupported', []))
            locations.extend(SourceSpan(**item) for item in row.get('locations', []))
            interfaces.extend((SourceSpan(**item['site']), tuple(item['params'])) for item in row.get('functions', []))
            for model in row.get('local_models', []):
                owner = SourceSpan(**model['owner'])
                if any(r.path == owner.path and r.start_byte <= owner.start_byte
                       and owner.end_byte <= r.end_byte for r in scope.regions):
                    local_interfaces.extend((SourceSpan(**item['site']), tuple(x['name'] for x in model['inputs']),
                                             'consumer' if any(o['property_name'].startswith('jsx.')
                                                               for o in model['observations']) else 'condition')
                                            for item in model['edits'])
        self._data = parsed
        self._index = ProgramIndex(tuple(sorted(names)), tuple(locations), tuple(diagnostics), tuple(interfaces),
                                   tuple(local_interfaces))
        return self._index

    def observation_interfaces(self, snapshot: RepositorySnapshot,
                               context: RunContext) -> tuple[ObservationInterface, ...]:
        """Publish only visible pure Boolean entries, with path, parameters and snapshot identity."""
        index = self.index(snapshot, context)
        result = tuple(item for site, parameters in index.read_interfaces
                       if (item := self._observation_interface(site, parameters, snapshot, context)) is not None)
        scope = self.source_scope(snapshot, context)
        for row in self._data['files']:
            for model in row.get('local_models', []):
                owner = SourceSpan(**model['owner'])
                if not any(r.path == owner.path and r.start_byte <= owner.start_byte
                           and owner.end_byte <= r.end_byte for r in scope.regions):
                    continue
                key = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
                for number, observation in enumerate(model['observations']):
                    identity = hashlib.sha256(f'{snapshot.tree_sha256}:{key}:{number}'.encode()).hexdigest()[:24]
                    result += (ObservationInterface('projection:' + identity, SourceSpan(**observation['site']),
                        tuple(item['name'] for item in model['inputs']), snapshot.tree_sha256, 'local_projection',
                        observation['property_name'], tuple((item['name'], item['sort']) for item in model['inputs']),
                        observation['output_sort'], owner, f'{key}:{number}', tuple(model['premises'])),)
        return tuple(sorted(result, key=lambda item: (item.site.path, item.site.start_byte)))

    def _observation_interface(self, site: SourceSpan, parameters: tuple[str, ...],
                               snapshot: RepositorySnapshot, context: RunContext) -> ObservationInterface | None:
        """Bind one observed return to its complete visible owner and immutable source identity."""
        index = self.index(snapshot, context)
        scope = self.source_scope(snapshot, context)
        owners = tuple(owner for owner in index.locations if owner.node_kind == 'Function'
                       and owner.path == site.path and owner.start_byte <= site.start_byte
                       and site.end_byte <= owner.end_byte)
        if not owners:
            return None
        owner = min(owners, key=lambda span: span.end_byte - span.start_byte)
        if not any(region.path == owner.path and region.start_byte <= owner.start_byte
                   and owner.end_byte <= region.end_byte for region in scope.regions):
            return None
        source_slice(snapshot.root, owner)
        identity = json.dumps((snapshot.tree_sha256, asdict(owner), asdict(site), parameters),
                              ensure_ascii=False, sort_keys=True).encode('utf-8')
        interface_id = 'entry:' + hashlib.sha256(identity).hexdigest()[:24]
        return ObservationInterface(interface_id, site, parameters, snapshot.tree_sha256)

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
            return self.summarize_projection(boundary, witnesses, snapshot, context)
        params = tuple(function['params'])
        reads = tuple(feature.feature_id for feature in boundary.readable_features)
        if any(name not in params for name in reads):
            return unsupported
        declared = self._observation_interface(boundary.sites[0], params, snapshot, context)
        actual, inputs, outputs, constraints = [], [], [], []
        complete = True
        for witness in witnesses:
            binding = witness.interface
            if binding is None or binding != declared:
                continue
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

    def projection_model(self, site: SourceSpan, snapshot: RepositorySnapshot,
                         context: RunContext) -> tuple[dict, dict, str] | None:
        """Locate a source-bound edit and its fixed downstream projections in the trusted parser result."""
        self.index(snapshot, context)
        for file in self._data['files']:
            for model in file.get('local_models', []):
                for edit in model['edits']:
                    if SourceSpan(**edit['site']) == site:
                        key = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
                        return model, edit, key
        return None

    def summarize_projection(self, boundary: RepairBoundary, witnesses: tuple[Witness, ...],
                             snapshot: RepositorySnapshot, context: RunContext) -> LocalRepairModel:
        """Pull sourced scalar and JSX obligations through a fixed, pure local continuation."""
        unavailable = LocalRepairModel(boundary, (), (), (), (), ProofAssumptions(False, False, False, False),
                                      Coverage.PARTIAL, proof_scope='finite_source_projection')
        record = self.projection_model(boundary.sites[0], snapshot, context)
        if record is None:
            return unavailable
        model, edit, key = record
        sorts = {item['name']: item['sort'] for item in model['inputs']}
        reads = tuple(feature.feature_id for feature in boundary.readable_features)
        if not set(reads) <= set(sorts):
            return unavailable
        directory = {item.interface_id: item for item in self.observation_interfaces(snapshot, context)}
        relevant = tuple(w for w in witnesses if w.interface is not None
                         and w.interface.summary_key.startswith(key + ':'))
        terms = [program_term(edit['expression'])]
        for continuation in edit['continuations']:
            terms.extend(program_term(raw) for raw in [continuation['expression'], *continuation['conditions']])
        declared = {**sorts, '$edit': edit['output_sort']}
        if any(program_sort(term, declared) is None for term in terms):
            return replace(unavailable, diagnostics=('unsupported_projection_algebra_or_rule',))
        literal_values = [program_evaluate(node, {}) for term in terms for node in walk(term) if node.op == 'literal']
        literal_values += [value for witness in relevant for value in witness.expected_values]
        literals = tuple(dict((typed_key(value), value) for value in literal_values).values())
        input_values = [value for witness in relevant for target in witness.observations
                        for value in (literal_assignments(target.context) or {}).values()]
        domain_values = tuple(dict((typed_key(value), value) for value in literals + tuple(input_values)).values())
        domain = (False, True) if edit['output_sort'] == 'boolean' else tuple(
            value for value in domain_values if program_sort(literal(value), {}) == edit['output_sort'])
        if not domain:
            return replace(unavailable, diagnostics=('no_sourced_local_output_domain',))
        actual, inputs, outputs, constraints, allowed, diagnostics = [], [], [], [], [], []
        complete = True
        has_regex = any(node.op == 'regex_test' for term in terms for node in walk(term))
        for witness in relevant:
            interface = witness.interface
            values = literal_assignments(witness.observations[0].context) if len(witness.observations) == 1 else None
            if (directory.get(interface.interface_id) != interface or not witness.source_ids
                    or len(witness.expected_values) != 1 or values is None or set(values) != set(sorts)
                    or any(program_sort(literal(values[name]), {}) != sort for name, sort in sorts.items())):
                complete = False
                diagnostics.append('invalid_projection_witness:' + witness.witness_id)
                continue
            if has_regex and any(isinstance(v, str) and not v.isascii() for v in values.values()):
                complete = False
                diagnostics.append('regular_rule_non_ascii_entry:' + witness.witness_id)
                continue
            number = int(interface.summary_key.rsplit(':', 1)[1])
            observation = model['observations'][number]
            continuation = next(item for item in edit['continuations'] if item['observation'] == number)
            expression = program_term(continuation['expression'])
            conditions = tuple(program_term(raw) for raw in continuation['conditions'])
            base_conditions = tuple(program_term(raw) for raw in observation['conditions'])
            if observation['projection'] != 'presence' and not all(program_evaluate(g, values) for g in base_conditions):
                complete = False
                diagnostics.append('unreachable_scalar_projection:' + witness.witness_id)
                continue
            admissible = []
            for value in domain:
                environment = {**values, '$edit': value}
                reached = all(program_evaluate(g, environment) for g in conditions)
                if not reached and observation['projection'] != 'presence':
                    continue
                projected = bool(reached and program_evaluate(expression, environment)) if observation['projection'] == 'presence' else program_evaluate(expression, environment)
                if typed_key(projected) == typed_key(witness.expected_values[0]):
                    admissible.append(value)
            index = len(actual)
            actual.append(replace(witness, reachability=SolverStatus.SAT))
            inputs.append(Term('and', tuple(Term('eq', (symbol(name), literal(values[name]))) for name in reads)))
            output = symbol(f'local_out:{index}')
            outputs.append(output)
            allowed.append(tuple(admissible))
            constraints.append(Term('domain', (output,) + tuple(literal(value) for value in admissible))
                               if admissible else literal(False))
        for index, left in enumerate(inputs):
            values = literal_assignments(left)
            for previous in range(index):
                other = literal_assignments(inputs[previous])
                if all(typed_key(values[name]) == typed_key(other[name]) for name in reads):
                    constraints.append(Term('eq', (outputs[index], outputs[previous])))
        proved = bool(actual) and complete
        atoms = tuple(term for term in terms if '$edit' not in symbols(term))
        return LocalRepairModel(boundary, tuple(actual), tuple(inputs), tuple(outputs), tuple(constraints),
            ProofAssumptions(True, True, proved, True), Coverage.COMPLETE if proved else Coverage.PARTIAL,
            tuple(diagnostics), tuple(allowed), tuple(w.witness_id for w in actual), 'finite_source_projection',
            literals, atoms, edit['output_sort'], tuple(domain))

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
        if not plan.holes and plan.edit_scope is not None:
            for path in sorted({r.path for r in plan.edit_scope.regions}):
                result.append(Effect(ObservationKey('file:' + path, '*', literal(True)),
                                     symbol('effect:unknown_transitive'), Coverage.PARTIAL))
            if plan.edit_scope.creation_roots:
                result.append(Effect(ObservationKey('*', '*', literal(True)),
                                     symbol('effect:unknown_new_file'), Coverage.PARTIAL))
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
        operations = {f.hole_id: f.operation for f in fillings}
        self.index(snapshot, context)
        base_files = {row['path']: row for row in self._data['files']}
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
            old_text = original[start:end].decode('utf-8')
            if text != old_text:
                from boundary_repair.domain.repair import edit_operations
                if operations[hole.hole_id] not in edit_operations(hole.expected_type):
                    raise ValidationError('edit_operation_not_declared')
                if hole.expected_type == 'source-fragment' or hole.site.node_kind in {'File', 'Function', 'Assignment'}:
                    raise ValidationError('unbounded_hole_not_allowed')
                if operations[hole.hole_id] == 'delete' and text != '':
                    raise ValidationError('invalid_delete_arguments')
                if not text.strip() and not (operations[hole.hole_id] == 'delete' and hole.expected_type == 'local:Statement'):
                    raise ValidationError('implicit_deletion_not_allowed')
                omission = r'(?im)^\s*(?://|/\*|\*)[^\n]*(?:\b(?:keep|leave|rest|remaining)\b[^\n]*\b(?:unchanged|same)\b|\.\.\.|其余.*(?:不变|省略))'
                if any(match not in old_text for match in re.findall(omission, text)):
                    raise ValidationError('omission_placeholder')
                if hole.expected_type == 'local:TextLine':
                    ending = '\r\n' if old_text.endswith('\r\n') else '\n' if old_text.endswith('\n') else ''
                    text = text.rstrip('\r\n')
                    if '\n' in text or '\r' in text:
                        raise ValidationError('text_line_scope_escaped')
                    text += ending
                    fills[hole.hole_id] = text
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
        parsed_files = {row['path']: row for row in parsed['files']}
        known_changes = [p for p in patched if patched[p] != originals[p]]
        if known_changes and all(base_files[p].get('semantic_hash') is not None
                                 and base_files[p]['semantic_hash'] == parsed_files[p].get('semantic_hash')
                                 for p in known_changes):
            raise NoAdmissiblePatch('no_executable_change')
        for hole in plan.holes:
            data, start, end = source_slice(snapshot.root, hole.site)
            replacement = fills[hole.hole_id].encode('utf-8')
            if replacement == data[start:end]:
                continue
            if operations[hole.hole_id] == 'delete':
                continue
            if hole.expected_type == 'local:TextLine':
                continue
            offset = sum(len(value) - (right - left) for left, right, value in edits[hole.site.path]
                         if right <= start and left < start)
            matches = [site for site in parsed_files[hole.site.path].get('locations', [])
                       if site['start_byte'] == start + offset
                       and site['end_byte'] == start + offset + len(replacement)
                       and site['node_kind'] == hole.site.node_kind]
            if not matches:
                raise ValidationError('local_syntax_scope_escaped')
            if operations[hole.hole_id] == 'guard_consumer':
                guards = parsed_files[hole.site.path].get('consumer_guards', [])
                if not any(guard['start_byte'] == start + offset
                           and guard['end_byte'] == start + offset + len(replacement)
                           and guard['fallback'] == data[start:end].decode('utf-8') for guard in guards):
                    raise ValidationError('consumer_fallback_not_retained')
        # For certified Boolean holes, reparse proves that the new expression cannot read outside
        # the declared interface or introduce calls, closures, side effects, or fresh parameters.
        for hole in plan.holes:
            if hole.expected_type != 'boolean-expression':
                continue
            start, end = hole.site.start_byte, hole.site.end_byte
            offset = sum(len(value) - (right - left) for left, right, value in edits[hole.site.path]
                         if right <= start and left < start)
            matches = [f for row in parsed['files'] if row['path'] == hole.site.path
                       for f in row.get('functions', [])
                       if f['site']['start_byte'] == start + offset
                       and f['site']['end_byte'] == start + offset + len(fills[hole.hole_id].encode('utf-8'))]
            if len(matches) != 1:
                raise ValidationError('boolean_grammar_escaped')
            from boundary_repair.kernel.terms import symbols
            emitted = term_from_json(matches[0]['expression'])
            if set(symbols(emitted)) - set(hole.allowed_symbols):
                raise ValidationError('undeclared_boolean_read')
            original_summary = self.function_summary(hole.site, snapshot, context)
            if original_summary is None:
                raise ValidationError('original_boolean_summary_missing')
            for obligation in plan.obligations:
                if not obligation.entry_cases:
                    raise ValidationError('boolean_obligation_unbound')
                for case in obligation.entry_cases:
                    if case.interface.site != hole.site or case.interface.snapshot_sha256 != snapshot.tree_sha256:
                        raise ValidationError('boolean_obligation_interface_mismatch')
                    if evaluate(emitted, dict(case.inputs)) is not case.expected:
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
