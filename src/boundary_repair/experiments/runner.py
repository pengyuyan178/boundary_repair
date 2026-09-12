"""生成实验编排；不导入评测器。顺序运行是明确的一期架构选择。"""
import os
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
    ConfigurationError,
    EvidenceConflict,
    ExternalServiceError,
    ImplementationRequired,
    NoAdmissiblePatch,
    ValidationError,
)
from boundary_repair.domain.runtime import BudgetLedger, RunContext, StageEvent
from boundary_repair.domain.task import TaskInput
from boundary_repair.kernel.codec import strict_json
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
    generation_mode: str | None = None
    contract_coverage: str | None = None
    extraction_status: str | None = None
    application_check: str = "not_run"
    syntax_check: str = "unknown"
    semantic_coverage: str | None = None
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


def _contract_metadata(case_root: Path) -> tuple[str | None, str | None]:
    """Read theory coverage and extraction status without treating either as source semantics."""
    try:
        path = case_root / "trajectory" / "contracts.json"
        contracts = strict_json(path.read_text(encoding="utf-8"))
        theory = contracts.get("theory")
        coverage = theory.get("coverage") if isinstance(theory, dict) else None
        extraction_status = contracts.get("extraction_status")
    except (OSError, UnicodeDecodeError, ValidationError):
        return None, None
    return (
        coverage if coverage in {"complete", "partial"} else None,
        extraction_status
        if extraction_status in {"complete", "partial", "unavailable"}
        else None,
    )


def _frozen_generation_mode(case_root: Path) -> str | None:
    """Read the mode frozen before a transaction request when later compilation fails."""
    try:
        path = case_root / "trajectory" / "generation_plan.json"
        plan = strict_json(path.read_text(encoding="utf-8"))
        mode = plan.get("generation_mode")
    except (OSError, UnicodeDecodeError, ValidationError):
        return None
    return mode if mode in {"certified", "certified_projection", "scoped", "raw_evidence"} else None


def _semantic_coverage(generation_mode: str | None, status: str) -> str | None:
    """Report complete local source semantics only after a certified patch is generated."""
    if generation_mode == "certified":
        return "complete" if status == "generated" else "partial"
    if generation_mode == "certified_projection":
        return "finite_entry_complete" if status == "generated" else "finite_entry_partial"
    if generation_mode in {"scoped", "raw_evidence"}:
        return "partial"
    return None


def _failure_checks(component: str) -> tuple[str, str]:
    """Mark explicit compiler failures; unsupported analysis remains unknown."""
    application = {
        "generated_patch_not_applicable",
        "generated_patch_apply_failed",
        "generated_patch_bytes_mismatch",
        "generated_patch_mode_mismatch",
    }
    syntax = {"generated_syntax_invalid", "generated_json_invalid"}
    return (
        "failed" if component in application else "not_run",
        "failed" if component in syntax else "unknown",
    )


def require_isolated_generation(config: ExperimentConfig) -> None:
    """Require the isolated worker entry point for server-side patch generation."""
    if config.target != "server":
        return
    if (os.environ.get("BOUNDARY_GENERATION_ROLE") != "isolated_worker"
            or not Path("/.dockerenv").is_file()
            or os.getuid() == 0
            or config.project_root != Path("/work")
            or config.dataset != Path("/input/tasks.json")
            or config.results_root != Path("/output")
            or config.harness_python is not None
            or config.image_manifest is not None
            or Path("/var/run/docker.sock").exists()):
        raise ConfigurationError("server_generation_requires_isolated_worker")


def run_generation(
    config: ExperimentConfig, tasks: tuple[TaskInput, ...], batch_id: str,
    pipeline: RepairPipeline, workspace: WorkspacePort,
) -> BatchReport:
    """任务序列/已注入依赖 → 新建批次、逐题产物和未评分报告。

    每题同一 BudgetLedger；仅生成一个最终预测。未实现是工程阻断，记录后结束批次；
    剩余题标为未尝试，不记成 unresolved。普通运行异常用类型名记录，不回显凭证。
    不读取 .env；不评测、不回溯、不根据失败额外采样。调用方应先完成数据选择。
    """
    require_isolated_generation(config)
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
        generation_mode = None
        contract_coverage = None
        extraction_status = None
        application_check = "not_run"
        syntax_check = "unknown"
        semantic_coverage = None
        try:
            case.emit(StageEvent("workspace", "started"))
            with workspace.open_base(task, context) as snapshot:
                if snapshot.base_commit != task.base_commit:
                    raise ValueError("base_commit mismatch")
                case.emit(StageEvent("workspace", "completed"))
                prepared_task = task
                if config.integration.model_mode == "http":
                    from boundary_repair.adapters.model import prepare_task_assets

                    case.emit(StageEvent("assets", "started"))
                    prepared_task = prepare_task_assets(task, config, context)
                    case.emit(StageEvent("assets", "completed"))
                output = pipeline.repair(prepared_task, snapshot, context, case)
                patch = output.patch
                if patch.instance_id != task.instance_id or patch.base_commit != task.base_commit:
                    raise ValueError("patch identity mismatch")
                generation_mode = patch.generation_mode
                application_check = patch.application_check
                syntax_check = patch.syntax_check
                contract_coverage, extraction_status = _contract_metadata(case.root)
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
        except NoAdmissiblePatch as exc:
            status, component = "no_admissible_patch", str(exc)
        except ValidationError as exc:
            status, component = "validation_error", str(exc)
            application_check, syntax_check = _failure_checks(component)
        except ConfigurationError as exc:
            status, component, aborted = "configuration_error", str(exc), True
        except ExternalServiceError as exc:
            status, component = "external_service_error", str(exc)
        except Exception as exc:
            status, component = "infrastructure_error", type(exc).__name__
            prediction = None
        if generation_mode is None:
            generation_mode = _frozen_generation_mode(case.root)
        if contract_coverage is None and extraction_status is None:
            contract_coverage, extraction_status = _contract_metadata(case.root)
        semantic_coverage = _semantic_coverage(generation_mode, status)
        if status != "generated":
            prediction = None
        record = InferenceRecord(
            task.instance_id, status, component, context.budget.model_calls,
            context.budget.output_tokens, generation_mode, contract_coverage,
            extraction_status, application_check, syntax_check, semantic_coverage,
        )
        case.emit(StageEvent("generation", status))
        case.save_inference(record)
        write_json(case.root / "logs" / "status.json", {
            "status": status, "component": component, "generation_mode": generation_mode,
            "contract_coverage": contract_coverage, "extraction_status": extraction_status,
            "application_check": application_check, "syntax_check": syntax_check,
            "semantic_coverage": semantic_coverage,
        })
        store.record(record, prediction)
        records.append(record)
        if aborted:
            break
    report = BatchReport(store.root, "blocked" if aborted else "generation_finished",
                         len(tasks), len(records), generated, len(tasks) - len(records))
    write_json(store.root / "summary.json", report)
    return report
