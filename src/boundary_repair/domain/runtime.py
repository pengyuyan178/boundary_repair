"""每题共享预算与单向运行上下文。共享计量；适配器设置请求/进程超时，DNS 阻塞仍受操作系统限制。"""
from dataclasses import dataclass, field
from time import monotonic

from boundary_repair.domain.errors import BudgetExceeded


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """用户给定的是任务总预算；不是每个模块各自拥有一份。"""
    timeout_seconds: int = 3600
    max_model_calls: int = 100
    max_output_tokens: int = 150000
    max_patch_candidates: int = 20
    max_ideas: int = 5


@dataclass(frozen=True, slots=True)
class SearchPolicy:
    """多候选仅来自同一原始上下文；不使用评分/修复后截图做自适应迭代。"""
    require_witness: bool = True
    prefer_greedy_patch: bool = True
    allow_multiple_sampling: bool = True
    temperature: float = 1.0
    final_submissions: int = 1


@dataclass(slots=True)
class BudgetLedger:
    """单线程每题计量；并发版本需单独提供原子预留，不能共享此实例跨题。"""
    limits: BudgetLimits
    model_calls: int = 0
    output_tokens: int = 0
    patch_candidates: int = 0
    ideas: int = 0
    started_at: float = field(default_factory=monotonic)

    def remaining_seconds(self) -> float:
        """返回剩余墙钟秒数；外部请求/子进程必须使用不大于此值的超时。"""
        return max(0.0, self.limits.timeout_seconds - (monotonic() - self.started_at))

    def check_deadline(self) -> None:
        """输入当前 ledger；超时即抛 BudgetExceeded，不重试或扩展预算。"""
        if self.remaining_seconds() <= 0:
            raise BudgetExceeded("task_timeout")

    def begin_model_call(self, requested_output_tokens: int) -> int:
        """调用前计数并返回可用输出上限；失败请求也计次，不允许隐式 SDK 重试。

        后续适配器必须将返回值送给服务端 max-output 参数，并在返回后记实际 usage。
        此函数不是后台硬超时器，也不能阻止不遵守接口的服务端超额生成。
        """
        self.check_deadline()
        if requested_output_tokens <= 0:
            raise ValueError("requested_output_tokens must be positive")
        remaining = self.limits.max_output_tokens - self.output_tokens
        if self.model_calls >= self.limits.max_model_calls or remaining <= 0:
            raise BudgetExceeded("model_budget")
        self.model_calls += 1
        return min(requested_output_tokens, remaining)

    def record_output_tokens(self, actual_tokens: int) -> None:
        """累加服务端实际输出 token；超额立即终止，并保留实际消费而非截断数字。"""
        if actual_tokens < 0:
            raise ValueError("actual_tokens must be nonnegative")
        self.output_tokens += actual_tokens
        if self.output_tokens > self.limits.max_output_tokens:
            raise BudgetExceeded("output_token_budget")

    def claim_candidates(self, count: int = 1) -> None:
        """预留候选补丁数；方案枚举器调用，评分器不得借此触发新候选。"""
        self.check_deadline()
        if count < 0:
            raise ValueError("count must be nonnegative")
        if self.patch_candidates + count > self.limits.max_patch_candidates:
            raise BudgetExceeded("patch_candidate_budget")
        self.patch_candidates += count

    def claim_ideas(self, count: int = 1) -> None:
        """预留不同修复方案数；同一题三个模块共享该计数。"""
        self.check_deadline()
        if count < 0:
            raise ValueError("count must be nonnegative")
        if self.ideas + count > self.limits.max_ideas:
            raise BudgetExceeded("idea_budget")
        self.ideas += count


@dataclass(frozen=True, slots=True)
class RunContext:
    """依赖显式传递；没有全局会话、评分器、gold 数据或隐式模型记忆。"""
    run_id: str
    instance_id: str
    seed: int
    policy: SearchPolicy
    budget: BudgetLedger


@dataclass(frozen=True, slots=True)
class StageEvent:
    """简短可审计轨迹，不要求模型私有推理链；引用另存的中间产物。"""
    stage: str
    status: str
    artifact_name: str | None = None
