"""Open-world partial specification: quoted evidence -> alternatives -> model-relative MUST/MAY."""
from dataclasses import dataclass
import json

from boundary_repair.domain.errors import EvidenceConflict
from boundary_repair.domain.runtime import RunContext
from boundary_repair.domain.specification import (
    BehaviorConstraint, ClaimKind, ContractSet, Coverage, EntityBinding, EvidenceBundle,
    InterpretationSpace, SolverStatus, Term, Witness,
)
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.evidence import EVIDENCE_SCHEMA, EVIDENCE_SYSTEM, parse_evidence
from boundary_repair.kernel.retrieval import lexical_score, snippets
from boundary_repair.kernel.terms import literal, symbol
from boundary_repair.ports import LogicPort, ModelPort, ModelRequest, ProgramPort


@dataclass(frozen=True, slots=True)
class SpecificationRecovery:
    """One evidence call; logical reasoning quantifies only the explicitly recorded interpretation theory."""
    model: ModelPort
    program: ProgramPort
    logic: LogicPort
    response_tokens: int = 8000
    context_files: int = 8
    max_context_chars: int = 100000

    def recover(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> ContractSet:
        """Run S1-S4 exactly once without evaluator access, repair execution, or result feedback."""
        context.budget.check_deadline()
        evidence = self.extract_evidence(task, snapshot, context)
        bindings = self.bind_entities(evidence, snapshot, context)
        space = self.build_interpretation_space(evidence, bindings, context)
        return self.derive_contracts(evidence, bindings, space, context)

    def extract_evidence(self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext) -> EvidenceBundle:
        """S1: send original evidence and base snippets; reject nonexistent quotes and invented sources."""
        index = self.program.index(snapshot, context)
        code = snippets(index, snapshot, task.problem_statement, self.context_files, self.max_context_chars)
        prompt = json.dumps({'issue': task.problem_statement, 'assets': [a.source_id for a in task.assets],
                             'base_code': code, 'output_example': EVIDENCE_SCHEMA}, ensure_ascii=False)
        response = self.model.complete(ModelRequest(EVIDENCE_SYSTEM, prompt, task.assets,
                                                   'evidence.v1', self.response_tokens), context)
        return parse_evidence(response.text, task, code)

    def bind_entities(self, evidence: EvidenceBundle, snapshot: RepositorySnapshot,
                      context: RunContext) -> tuple[EntityBinding, ...]:
        """S2: retain all exact symbol bindings; lexical alternatives are explicitly PARTIAL.

        Exact bindings exhaust only names in the indexed snapshot, not all possible visual-to-code
        meanings. An index truncation propagates PARTIAL and cannot establish global coverage.
        """
        index = self.program.index(snapshot, context)
        entities = sorted({target.entity_id for claim in evidence.claims for target in claim.targets})
        bindings = []
        for entity in entities:
            exact = tuple(span for span in index.locations if span.symbol == entity)
            candidates = exact or tuple(span for span in index.locations
                                        if lexical_score(entity, span.path + ' ' + span.symbol) > 0)[:12]
            complete = bool(exact) and not index.unsupported_constructs
            choices = tuple(symbol(f'bind:{entity}:{i}') for i in range(len(candidates)))
            bindings.append(EntityBinding(entity, candidates, choices,
                                          Coverage.COMPLETE if complete else Coverage.PARTIAL))
        return tuple(bindings)

    def build_interpretation_space(self, evidence: EvidenceBundle, bindings: tuple[EntityBinding, ...],
                                   context: RunContext) -> InterpretationSpace:
        """S3: construct separate acceptance variables and mutually exclusive interpretation choices.

        Current observations never constrain desired output values. Alternatives are encoded,
        not sampled. COMPLETE refers only to this finite acceptance theory; it is not a claim
        that the model extracted all real-world interpretations or bound every repository entity.
        """
        grouped = {name for group in evidence.choice_groups for name in group}
        assumptions = tuple(symbol('accept:' + c.claim_id) for c in evidence.claims if c.claim_id not in grouped)
        choices = []
        for group in evidence.choice_groups:
            variables = tuple(symbol('accept:' + name) for name in group)
            choices.append(Term('or', variables))
            choices.extend(Term('not', (Term('and', (a, b)),))
                           for i, a in enumerate(variables) for b in variables[i+1:])
        # Binding is kept independently: it does not invent an exhaustive semantic correspondence.
        result = self.logic.check(assumptions + tuple(choices), context)
        if result.status == SolverStatus.UNSAT:
            raise EvidenceConflict('inconsistent_acceptance_theory')
        return InterpretationSpace(assumptions, tuple(choices), result.status,
                                   Coverage.COMPLETE if result.status == SolverStatus.SAT else Coverage.PARTIAL)

    def derive_contracts(self, evidence: EvidenceBundle, bindings: tuple[EntityBinding, ...],
                         space: InterpretationSpace, context: RunContext) -> ContractSet:
        """S4: classify accepted requirements by entailment; uncertain frames remain MAY.

        Witnesses contain cited input contexts only. Reachability remains UNKNOWN until a
        supported code adapter establishes entry-local reachability. No LLM confidence can
        promote it. MUST certifies acceptance under this theory, not correctness of perception.
        """
        if space.consistency == SolverStatus.UNSAT:
            raise EvidenceConflict('vacuous_entailment_rejected')
        must, may, frames, witnesses = [], [], [], []
        diagnostics = ['MUST_is_relative_to_normalized_acceptance_theory_not_a_perception_proof']
        for claim in evidence.claims:
            if claim.kind == ClaimKind.OBSERVATION:
                continue
            acceptance = symbol('accept:' + claim.claim_id)
            negated = self.logic.check(space.assumptions + space.choices + (Term('not', (acceptance,)),), context)
            positive = self.logic.check(space.assumptions + space.choices + (acceptance,), context)
            constraint = BehaviorConstraint(claim.claim_id, claim.kind, claim.targets, claim.statement, claim.source_ids)
            certain = (space.consistency == SolverStatus.SAT and space.coverage == Coverage.COMPLETE
                       and negated.status == SolverStatus.UNSAT)
            if certain:
                (frames if claim.kind == ClaimKind.FRAME else must).append(constraint)
                for number, target in enumerate(claim.targets):
                    witnesses.append(Witness(f'{claim.claim_id}:{number}', (target,), (target.context,),
                                             SolverStatus.UNKNOWN, claim.source_ids))
            elif positive.status != SolverStatus.UNSAT:
                may.append(constraint)
            else:
                diagnostics.append('rejected_interpretation:' + claim.claim_id)
        if any(b.coverage == Coverage.PARTIAL for b in bindings):
            diagnostics.append('entity_bindings_partial')
        if context.policy.require_witness and not witnesses:
            diagnostics.append('no_cited_witness_no_hard_pruning')
        return ContractSet(tuple(must), tuple(may), tuple(frames), tuple(witnesses), space, tuple(diagnostics))
