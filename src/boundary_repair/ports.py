"""外部能力与可替换模块的窄接口；实现类无需继承，也没有自动插件扫描。"""
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from boundary_repair.domain.repair import (
    Effect,
    HoleFilling,
    LocalRepairModel,
    LocalizationResult,
    PatchArtifact,
    PatchPlan,
    RepairBoundary,
    SynthesisResult,
)
from boundary_repair.domain.runtime import RunContext, StageEvent
from boundary_repair.domain.specification import ContractSet, SolverAnswer, Term, Witness
from boundary_repair.domain.task import IssueAsset, ProgramIndex, RepositorySnapshot, TaskInput


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """冻结模型请求；schema_name 指向显式输出契约，不允许执行返回文本。"""
    system: str
    prompt: str
    assets: tuple[IssueAsset, ...]
    schema_name: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """原始响应与实际 usage；解析和语义验证属于调用它的算法模块。"""
    text: str
    output_tokens: int
    request_id: str
    asset_sha256: tuple[str, ...] = ()
    model_name: str = ""


class ModelPort(Protocol):
    """唯一模型出入口；后续 provider 适配器负责预算、脱敏与固定模型版本。"""

    def complete(self, request: ModelRequest, context: RunContext) -> ModelResponse:
        """输入原始证据请求；输出响应，调用前计次、调用后记 usage，禁止隐式重试。"""
        ...


class LogicPort(Protocol):
    """求解后端只处理已声明逻辑；超时/不支持都返回 UNKNOWN 而非猜答案。"""

    def check(self, assertions: tuple[Term, ...], context: RunContext) -> SolverAnswer:
        """检查断言合取的可满足性并返回证书；必须验证算子、sort 和预算。"""
        ...


class ProgramPort(Protocol):
    """JS/TS/JSX 等语义前端；不在此层决定论文的规格、排序或修复策略。"""

    def index(self, snapshot: RepositorySnapshot, context: RunContext) -> ProgramIndex:
        """索引修复前符号与 AST 位置；不解析 node_modules、压缩副本或 Git 历史。"""
        ...

    def summarize(
        self, boundary: RepairBoundary, witnesses: tuple[Witness, ...],
        snapshot: RepositorySnapshot, context: RunContext,
    ) -> LocalRepairModel:
        """构造读取接口、固定下游摘要和可达性前提；不支持的语法标为 PARTIAL。"""
        ...

    def effects(
        self, plan: PatchPlan, snapshot: RepositorySnapshot, context: RunContext,
    ) -> tuple[Effect, ...]:
        """估计实体-属性-上下文级影响与别名传播；可能影响不等于确定违反。"""
        ...

    def materialize(
        self, task: TaskInput, plan: PatchPlan, fillings: tuple[HoleFilling, ...],
        snapshot: RepositorySnapshot, context: RunContext,
    ) -> PatchArtifact:
        """核验源码哈希后在隔离副本应用 AST 编辑、生成唯一 diff；不运行评分测试。"""
        ...


class WorkspacePort(Protocol):
    """只向生成器提供 base 工作树；不得挂载 gold、测试答案或完整项目根。"""

    def open_base(
        self, task: TaskInput, context: RunContext,
    ) -> AbstractContextManager[RepositorySnapshot]:
        """创建并最终清理隔离工作区；固定 commit/镜像，不能信任未校验的仓库状态。"""
        ...


class SpecificationPort(Protocol):
    """创新一或明确声明的普通规格对照实现。"""

    def recover(
        self, task: TaskInput, snapshot: RepositorySnapshot, context: RunContext,
    ) -> ContractSet:
        """issue/附件/base code → 带来源的 MUST、MAY、FRAME 与见证。"""
        ...


class LocalizationPort(Protocol):
    """创新二或明确声明的普通相关性定位对照实现。"""

    def locate(
        self, task: TaskInput, contracts: ContractSet,
        snapshot: RepositorySnapshot, context: RunContext,
    ) -> LocalizationResult:
        """规格/base code → 可表达、不可表达与未知的修复接口集合。"""
        ...


class SynthesisPort(Protocol):
    """创新三或明确声明的普通单向生成对照实现。"""

    def synthesize(
        self, task: TaskInput, contracts: ContractSet, localization: LocalizationResult,
        snapshot: RepositorySnapshot, context: RunContext,
    ) -> SynthesisResult:
        """行为义务/候选接口 → 一个作用域计划与最终补丁；不能消费评分结果。"""
        ...


StageArtifact = TaskInput | ContractSet | LocalizationResult | SynthesisResult


class TracePort(Protocol):
    """生成阶段的只写产物接口；没有读取旧失败轨迹的能力。"""

    def emit(self, event: StageEvent) -> None:
        """写入阶段名与状态，不自动保存模型私有推理或敏感环境。"""
        ...

    def save(self, name: str, artifact: StageArtifact) -> None:
        """以固定文件名持久化类型化中间产物；路径限制由存储适配器负责。"""
        ...
