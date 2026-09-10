"""创新二、三的中间表示：接口能力、证明条件、作用域计划与单个最终补丁。"""
from dataclasses import dataclass
from enum import StrEnum

from boundary_repair.domain.specification import (
    BehaviorConstraint,
    Coverage,
    ObservationKey,
    Term,
    Witness,
)
from boundary_repair.domain.task import SourceSpan


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


@dataclass(frozen=True, slots=True)
class LocalizationResult:
    """按策略排序的边界集合；UNKNOWN 保留为候选，不能静默删去。"""
    assessments: tuple[BoundaryAssessment, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyntaxHole:
    """待补齐的语法空洞；限定位置、预期类型和可使用符号，避免自由改周边。"""
    hole_id: str
    site: SourceSpan
    expected_type: str
    allowed_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Effect:
    """可能/已证实的属性级影响；可能触及 FRAME 不等于已证明违反它。"""
    target: ObservationKey
    relation: Term
    coverage: Coverage


@dataclass(frozen=True, slots=True, order=True)
class ScopeCost:
    """词典序成本；未知义务与风险在 AST 大小之前，不靠缩短补丁掩盖风险。"""
    unresolved_requirements: int
    unresolved_frames: int
    protected_risk: int
    extra_scope: int
    invented_constants: int
    ast_size: int


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


@dataclass(frozen=True, slots=True)
class HoleFilling:
    """模型/有限语法求解器提供的局部语法片段；后端还须类型与作用域校验。"""
    hole_id: str
    source_text: str


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    """唯一提交补丁；SHA256 绑定内容，base_commit 绑定原程序版本。"""
    instance_id: str
    base_commit: str
    plan_id: str
    unified_diff: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """生成成功不等于 resolved；返回计划、补丁和尚未证明的义务。"""
    plan: PatchPlan
    patch: PatchArtifact
    unresolved: tuple[str, ...] = ()
