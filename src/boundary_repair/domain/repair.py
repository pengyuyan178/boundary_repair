"""创新二、三的中间表示：接口能力、证明条件、作用域计划与单个最终补丁。"""
from dataclasses import dataclass
from enum import StrEnum

from boundary_repair.domain.specification import (
    BehaviorConstraint,
    Coverage,
    ObservationKey,
    Term,
    Witness,
    Scalar,
)
from boundary_repair.domain.task import SourceRef, SourceSpan


class EditKind(StrEnum):
    """实现原语而非额外创新点；FREEFORM 仅供预先声明的弱约束对照路径。"""
    REFINE_GUARD = "refine_guard"
    SPLIT_CONSUMER = "split_consumer"
    MERGE_UNIT = "merge_unit"
    REBIND_IDENTITY = "rebind_identity"
    FREEFORM = "freeform"


@dataclass(frozen=True, slots=True)
class Feature:
    """修复接口可读信息；角色特征必须有来源，不能只拟合截图偶然值。"""
    feature_id: str
    expression: Term
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairBoundary:
    """候选 = 源码位置集合 + 修改语法 + 读取接口，不只是一个行号。"""
    boundary_id: str
    sites: tuple[SourceSpan, ...]
    edit_kind: EditKind
    readable_features: tuple[Feature, ...]
    output_symbols: tuple[str, ...]
    relevance: int = 0


@dataclass(frozen=True, slots=True)
class ProofAssumptions:
    """不可表达证明的必要前提；任何一项未证实时只能给 UNKNOWN。"""
    reads_complete: bool
    pure_deterministic: bool
    witnesses_reachable: bool
    downstream_sound: bool


@dataclass(frozen=True, slots=True)
class LocalRepairModel:
    """局部可表达模型 Φ_b；目标须经固定下游反推，不能直接冒充局部输出。"""
    boundary: RepairBoundary
    witnesses: tuple[Witness, ...]
    input_terms: tuple[Term, ...]
    output_terms: tuple[Term, ...]
    constraints: tuple[Term, ...]
    assumptions: ProofAssumptions
    coverage: Coverage
    diagnostics: tuple[str, ...] = ()
    allowed_outputs: tuple[tuple[Scalar, ...], ...] = ()
    covered_obligations: tuple[str, ...] = ()
    proof_scope: str = 'direct_boolean_entry'
    grammar_literals: tuple[Scalar, ...] = ()
    grammar_atoms: tuple[Term, ...] = ()
    output_sort: str = 'boolean'
    output_domain: tuple[Scalar, ...] = ()


@dataclass(frozen=True, slots=True)
class InterfaceCertificate:
    """Replayable finite witness evidence and the precise source/grammar domain of an interface proof."""
    witnesses: tuple[str, ...]
    inputs: tuple[Term, ...]
    allowed_outputs: tuple[tuple[Scalar, ...], ...]
    output_domain: tuple[Scalar, ...]
    assumptions: ProofAssumptions
    snapshot_sha256: tuple[str, ...]
    summary_keys: tuple[str, ...]
    max_grammar_nodes: int = 9


class ExpressivityVerdict(StrEnum):
    """FEASIBLE 仅表示声明模型/语法内存在解，不表示通过真实评测。"""
    FEASIBLE = "feasible"
    INEXPRESSIBLE = "inexpressible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BoundaryAssessment:
    """定位产物；证书只排除声明的修复接口，不能排除整个文件的一切修法。"""
    boundary: RepairBoundary
    verdict: ExpressivityVerdict
    required_features: tuple[Feature, ...]
    certificate: str | None
    unresolved: tuple[str, ...] = ()
    covered_obligations: tuple[str, ...] = ()
    construction: str | None = None
    proof_scope: str = 'direct_boolean_entry'
    proof: InterfaceCertificate | None = None


@dataclass(frozen=True, slots=True)
class LocalizationResult:
    """按策略排序的边界集合；UNKNOWN 保留为候选，不能静默删去。"""
    assessments: tuple[BoundaryAssessment, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundaryGuidance:
    """A checked mapping from an analyzed interface to broader, program-owned edit targets."""
    assessment: BoundaryAssessment
    edit_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyntaxHole:
    """待补齐的语法空洞；限定位置、预期类型和可使用符号，避免自由改周边。"""
    hole_id: str
    site: SourceSpan
    expected_type: str
    allowed_symbols: tuple[str, ...]


def edit_operations(expected_type: str) -> list[str]:
    """Return the declared edit grammar for a local syntax site."""
    if expected_type == 'consumer-branch':
        return ['guard_consumer']
    return ['replace', 'delete'] if expected_type == 'local:Statement' else ['replace']


@dataclass(frozen=True, slots=True)
class Effect:
    """可能/已证实的属性级影响；可能触及 FRAME 不等于已证明违反它。"""
    target: ObservationKey
    relation: Term
    coverage: Coverage


@dataclass(frozen=True, slots=True, order=True)
class ScopeCost:
    """Pre-generation intervention coverage and permission risk, not discharged semantic obligations."""
    uncovered_requirements: int
    restricted_unknown_requirements: int
    unmapped_requirements: int
    touched_frames: int
    unmapped_frames: int
    unknown_effects: int
    broad_operations: int
    extra_edit_bytes: int
    edit_bytes: int


@dataclass(frozen=True, slots=True)
class PlanAssessment:
    """Auditable alternatives scored before one selected plan is sent to generation."""
    plan_id: str
    boundary_ids: tuple[str, ...]
    edit_targets: tuple[str, ...]
    write_ranges: tuple[tuple[str, int, int], ...]
    cost: ScopeCost
    uncovered_requirements: tuple[str, ...]
    unmapped_requirements: tuple[str, ...]
    touched_frames: tuple[str, ...]
    unmapped_frames: tuple[str, ...]
    localization_rank: int


@dataclass(frozen=True, slots=True)
class PatchPlan:
    """一个语义修改可涉及多个位置；保留默认分支、别名隔离和配套声明义务。"""
    plan_id: str
    boundary_ids: tuple[str, ...]
    edit_kind: EditKind
    holes: tuple[SyntaxHole, ...]
    obligations: tuple[BehaviorConstraint, ...]
    effects: tuple[Effect, ...]
    cost: ScopeCost | None = None
    unresolved: tuple[str, ...] = ()
    soft_obligations: tuple[BehaviorConstraint, ...] = ()
    edit_scope: "EditScope | None" = None
    generation_mode: str = 'certified'
    interpretation_groups: tuple[tuple[tuple[str, ...], ...], ...] = ()
    evidence_sources: tuple[SourceRef, ...] = ()
    boundary_guidance: tuple[BoundaryGuidance, ...] | None = None
    read_scope: "EditScope | None" = None
    scope_comparison: tuple[PlanAssessment, ...] = ()
    selection_policy: str = ''


@dataclass(frozen=True, slots=True)
class HoleFilling:
    """模型/有限语法求解器提供的局部语法片段；后端还须类型与作用域校验。"""
    hole_id: str
    source_text: str
    operation: str = 'replace'


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    """唯一提交补丁；SHA256 绑定内容，base_commit 绑定原程序版本。"""
    instance_id: str
    base_commit: str
    plan_id: str
    unified_diff: str
    sha256: str
    application_check: str = 'not_run'
    syntax_check: str = 'unknown'
    generation_mode: str = 'certified'
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FileState:
    """Immutable file identity and encoding for a declared editing scope."""
    file_id: str
    path: str
    sha256: str
    size: int
    mode: int
    encoding: str
    complete: bool


@dataclass(frozen=True, slots=True)
class EditRegion:
    """A displayed contiguous source range, bound to original bytes and line numbers."""
    region_id: str
    file_id: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    source: str
    sha256: str
    start_char: int = 0
    edit_mode: str = 'text'


@dataclass(frozen=True, slots=True)
class EditBlock:
    """A complete syntax unit inside a displayed region, bound to original encoded bytes."""
    block_id: str
    region_id: str
    start_byte: int
    end_byte: int
    sha256: str
    node_kind: str
    symbol: str = ''


@dataclass(frozen=True, slots=True)
class EditScope:
    """Frozen multi-file write capabilities; read coverage is independent of semantic coverage."""
    files: tuple[FileState, ...]
    regions: tuple[EditRegion, ...]
    creation_roots: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    blocks: tuple[EditBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceEdit:
    """One declared operation on an original file or source range."""
    operation: str
    target: str
    new_text: str = ''
    first_line: int | None = None
    last_line: int | None = None
    destination: str = ''
    old_text: str = ''


@dataclass(frozen=True, slots=True)
class EditTransaction:
    """All edits are expressed against one base and are applied atomically."""
    edits: tuple[SourceEdit, ...]


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """生成成功不等于 resolved；返回计划、补丁和尚未证明的义务。"""
    plan: PatchPlan
    patch: PatchArtifact
    unresolved: tuple[str, ...] = ()
