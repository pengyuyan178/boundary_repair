"""创新一的稳定中间表示；逻辑项只是语法树，不是已验证的语义。"""
from dataclasses import dataclass
from enum import StrEnum

from boundary_repair.domain.task import SourceRef, SourceSpan

Scalar = str | int | float | bool | None


class SolverStatus(StrEnum):
    """求解状态；UNKNOWN 不允许被转换成 UNSAT 或证明成功。"""
    SAT = "sat"
    UNSAT = "unsat"
    UNKNOWN = "unknown"


class Coverage(StrEnum):
    """相对于声明的有限/抽象模型的覆盖度，不是全程序正确性。"""
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class Term:
    """类型化接口中的逻辑 AST。op 如 symbol/literal/and/eq；禁止 eval 字符串。

    后续 LogicAdapter 必须校验运算符白名单、参数个数和 sort；value 仅供叶子使用。
    具体程序表达式不能直接作为已可信的逻辑事实，必须经语义摘要转换。
    """
    op: str
    args: tuple["Term", ...] = ()
    value: Scalar = None


class ClaimKind(StrEnum):
    """当前观察与规范性需求分开；未提及的区域不能自动成为 FRAME。"""
    OBSERVATION = "observation"
    REQUIREMENT = "requirement"
    FRAME = "frame"


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    """模型提出的证据候选；仍须校验证据位置与观察/规范分类。"""
    claim_id: str
    kind: ClaimKind
    statement: Term
    source_ids: tuple[str, ...]
    targets: tuple["ObservationKey", ...] = ()
    description: str = ""
    entry_cases: tuple["EntryCase", ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """原始证据及候选命题；没有完整正确截图的假设。"""
    sources: tuple[SourceRef, ...]
    claims: tuple[EvidenceClaim, ...]
    choice_groups: tuple[tuple[str, ...], ...] = ()
    interpretation_groups: tuple[tuple[tuple[str, ...], ...], ...] = ()


@dataclass(frozen=True, slots=True)
class EntityBinding:
    """视觉/文本实体到修复前程序位置的候选绑定；允许多义，不强行选唯一答案。"""
    entity_id: str
    candidates: tuple[SourceSpan, ...]
    alternatives: tuple[Term, ...]
    coverage: Coverage


@dataclass(frozen=True, slots=True)
class InterpretationSpace:
    """证据理论 Ψ；截断/不完整搜索不能支持全称 MUST。"""
    assumptions: tuple[Term, ...]
    choices: tuple[Term, ...]
    consistency: SolverStatus
    coverage: Coverage


@dataclass(frozen=True, slots=True)
class ObservationKey:
    """保持范围的最小单位：(实体, 观察属性, 适用上下文)，不是整个组件。"""
    entity_id: str
    property_name: str
    context: Term


@dataclass(frozen=True, slots=True)
class ObservationInterface:
    """Program-owned, snapshot-bound direct Boolean entry observation."""
    interface_id: str
    site: SourceSpan
    parameters: tuple[str, ...]
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class EntryCase:
    """An evidence-proposed entry case, separate from the original behavioral target."""
    interface: ObservationInterface
    inputs: tuple[tuple[str, bool], ...]
    expected: bool

    @property
    def target(self) -> ObservationKey:
        """Compile the identity return projection in declared parameter order."""
        context = Term('and', tuple(Term('eq', (Term('symbol', value=name), Term('literal', value=value)))
                                    for name, value in self.inputs))
        return ObservationKey(self.interface.site.symbol, 'return', context)

    @property
    def relation(self) -> Term:
        """Compile a Boolean output equality without interpreting natural-language literals."""
        return Term('eq', (Term('symbol', value='return'), Term('literal', value=self.expected)))


@dataclass(frozen=True, slots=True)
class Witness:
    """原始或经证明可达的情境；不能把臆造执行情境当作剪枝证明。"""
    witness_id: str
    observations: tuple[ObservationKey, ...]
    assumptions: tuple[Term, ...]
    reachability: SolverStatus
    source_ids: tuple[str, ...]
    interface: ObservationInterface | None = None


@dataclass(frozen=True, slots=True)
class BehaviorConstraint:
    """局部行为规格；role 不预先规定必须采用何种源码实现机制。"""
    constraint_id: str
    kind: ClaimKind
    targets: tuple[ObservationKey, ...]
    relation: Term
    source_ids: tuple[str, ...]
    description: str = ''
    entry_cases: tuple[EntryCase, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractSet:
    """MUST/MAY 与有依据的保持义务；MUST 仅相对于明确记录的解释理论。"""
    must: tuple[BehaviorConstraint, ...]
    may: tuple[BehaviorConstraint, ...]
    frames: tuple[BehaviorConstraint, ...]
    witnesses: tuple[Witness, ...]
    theory: InterpretationSpace
    diagnostics: tuple[str, ...] = ()
    extraction_status: str = 'partial'
    interpretation_groups: tuple[tuple[tuple[str, ...], ...], ...] = ()
    sources: tuple[SourceRef, ...] = ()


@dataclass(frozen=True, slots=True)
class SolverAnswer:
    """后端答案及可审计证书标识；诊断只能表达模型内结论。"""
    status: SolverStatus
    certificate: str | None = None
    diagnostics: tuple[str, ...] = ()
    assignment: tuple[tuple[str, Scalar], ...] = ()
