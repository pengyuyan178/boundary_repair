"""唯一推理主链；仅依赖领域和窄接口，不导入适配器、配置解析或评测模块。"""
from dataclasses import dataclass

from boundary_repair.domain.repair import SynthesisResult
from boundary_repair.domain.runtime import RunContext, StageEvent
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.ports import LocalizationPort, SpecificationPort, SynthesisPort, TracePort


@dataclass(frozen=True, slots=True)
class RepairPipeline:
    """三个模块可独立替换；结构上没有 evaluator 参数或反馈入口。"""
    specification: SpecificationPort
    localization: LocalizationPort
    synthesis: SynthesisPort

    def repair(
        self, task: TaskInput, snapshot: RepositorySnapshot,
        context: RunContext, trace: TracePort,
    ) -> SynthesisResult:
        """顺序调用三个阶段，逐阶段保存中间产物；错误直接交给实验层分类。

        输入必须已完成生成侧 allowlist 投影；输出仅表示 patch 已生成。
        trajectory 记录公开阶段状态和产物引用，不索取模型私有推理链。
        """
        context.budget.check_deadline()
        trace.emit(StageEvent("specification", "started"))
        contracts = self.specification.recover(task, snapshot, context)
        trace.save("contracts.json", contracts)
        trace.emit(StageEvent("specification", "completed", "contracts.json"))

        context.budget.check_deadline()
        trace.emit(StageEvent("expressivity", "started"))
        localization = self.localization.locate(task, contracts, snapshot, context)
        trace.save("localization.json", localization)
        trace.emit(StageEvent("expressivity", "completed", "localization.json"))

        context.budget.check_deadline()
        trace.emit(StageEvent("synthesis", "started"))
        result = self.synthesis.synthesize(task, contracts, localization, snapshot, context)
        trace.save("synthesis.json", result)
        trace.emit(StageEvent("synthesis", "completed", "synthesis.json"))
        return result
