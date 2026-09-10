"""生成实验编排；不导入评测器。顺序运行是明确的一期架构选择。"""
import platform
from dataclasses import dataclass
from pathlib import Path

from boundary_repair.adapters.storage import (
    BatchStore,
    file_sha256,
    prediction_row,
    source_fingerprint,
    write_json,
)
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import (
    BudgetExceeded,
    EvidenceConflict,
    ImplementationRequired,
    NoAdmissiblePatch,
    ValidationError, ConfigurationError, ExternalServiceError,
)
from boundary_repair.domain.runtime import BudgetLedger, RunContext, StageEvent
from boundary_repair.domain.task import TaskInput
from boundary_repair.pipeline import RepairPipeline
from boundary_repair.ports import WorkspacePort


@dataclass(frozen=True, slots=True)
class InferenceRecord:
    """生成状态；resolved 固定为 None，最终是否修复由独立评分记录给出。"""
    instance_id: str
    status: str
    component: str | None
    model_calls: int
    output_tokens: int
    resolved: None = None


@dataclass(frozen=True, slots=True)
class BatchReport:
    """部分执行也保留 selected/attempted 区别，不缩小后续 benchmark 分母。"""
    batch_path: Path
    status: str
    selected: int
    attempted: int
    generated: int
    unattempted: int
    resolved: None = None


def run_generation(
    config: ExperimentConfig, tasks: tuple[TaskInput, ...], batch_id: str,
    pipeline: RepairPipeline, workspace: WorkspacePort,
) -> BatchReport:
    """任务序列/已注入依赖 → 新建批次、逐题产物和未评分报告。

    每题同一 BudgetLedger；仅生成一个最终预测。未实现是工程阻断，记录后结束批次；
    剩余题标为未尝试，不记成 unresolved。普通运行异常用类型名记录，不回显凭证。
    不读取 .env；不评测、不回溯、不根据失败额外采样。调用方应先完成数据选择。
    """
    if not tasks:
        raise ValueError("No tasks selected")
    store = BatchStore.create(config.results_root, batch_id)
    write_json(store.root / "manifest.json", {
        "artifact_kind": "bounded_implementation",
        "execution_mode": config.integration.model_mode,
        "not_benchmark_evidence": config.integration.model_mode == "fixture", "config": config,
        "dataset_sha256": file_sha256(config.dataset),
        "dataset_source_sha256": (file_sha256(config.dataset_source)
                                  if config.dataset_source.is_file() else None),
        "method_source_sha256": source_fingerprint(Path(__file__).resolve().parents[1]),
        "python": platform.python_version(), "platform": platform.system(),
        "selected_instances": [task.instance_id for task in tasks],
        "evaluation_performed": False,
    })
    write_json(store.root / "summary.json", {"status": "running", "resolved": None})
    records: list[InferenceRecord] = []
    generated = 0
    aborted = False
    for task in tasks:
        case = store.create_case(task.instance_id)
        case.save("task.json", task)
        context = RunContext(batch_id, task.instance_id, config.seed, config.policy,
                             BudgetLedger(config.budget))
        prediction = None
        component = None
        try:
            case.emit(StageEvent("workspace", "started"))
            with workspace.open_base(task, context) as snapshot:
                if snapshot.base_commit != task.base_commit:
                    raise ValueError("base_commit mismatch")
                case.emit(StageEvent("workspace", "completed"))
                output = pipeline.repair(task, snapshot, context, case)
                patch = output.patch
                if patch.instance_id != task.instance_id or patch.base_commit != task.base_commit:
                    raise ValueError("patch identity mismatch")
                prediction = prediction_row(patch, config.model.label)
                case.save_patch(patch)
            status = "generated"
            generated += 1
        except ImplementationRequired as exc:
            status, component, aborted = "not_implemented", exc.component, True
        except BudgetExceeded:
            status = "budget_exceeded"
        except EvidenceConflict:
            status = "evidence_conflict"
        except NoAdmissiblePatch:
            status = "no_admissible_patch"
        except ValidationError as exc:
            status, component = "validation_error", str(exc)
        except ConfigurationError as exc:
            status, component, aborted = "configuration_error", str(exc), True
        except ExternalServiceError as exc:
            status, component = "external_service_error", str(exc)
        except Exception as exc:
            status, component = "infrastructure_error", type(exc).__name__
            prediction = None
        if status != "generated":
            prediction = None
        record = InferenceRecord(task.instance_id, status, component,
                                 context.budget.model_calls, context.budget.output_tokens)
        case.emit(StageEvent("generation", status))
        case.save_inference(record)
        write_json(case.root / "logs" / "status.json", {"status": status, "component": component})
        store.record(record, prediction)
        records.append(record)
        if aborted:
            break
    report = BatchReport(store.root, "blocked" if aborted else "generation_finished",
                         len(tasks), len(records), generated, len(tasks) - len(records))
    write_json(store.root / "summary.json", report)
    return report
