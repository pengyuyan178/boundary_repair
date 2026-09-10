"""CLI 只路由用例；plan/doctor 不读密钥、不启动容器、不查询 Git、不联网。"""
import argparse
import json
import platform
import shutil
import sys
from collections import Counter
from pathlib import Path

from boundary_repair.adapters.dataset import load_tasks, select_tasks
from boundary_repair.adapters.storage import json_value
from boundary_repair.bootstrap import build_pipeline, build_workspace
from boundary_repair.config import load_config
from boundary_repair.domain.errors import BoundaryRepairError, ImplementationRequired
from boundary_repair.experiments.evaluation import OfficialDockerEvaluator, run_evaluation
from boundary_repair.experiments.runner import run_generation


STAGES = (
    "specification: extract_evidence -> bind_entities -> "
    "build_interpretation_space -> derive_contracts",
    "expressivity: enumerate_boundaries -> build_local_model -> "
    "assess_expressivity -> rank_boundaries",
    "synthesis: enumerate_plans -> select_minimal_scope -> fill_holes -> program.materialize",
)


def build_parser() -> argparse.ArgumentParser:
    """定义五个显式命令；不提供会隐式循环修复/评分的 run-all 或自动重试参数。"""
    parser = argparse.ArgumentParser(prog="boundary-repair", description="BoundaryRepair bounded implementation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "doctor", "inspect", "generate", "evaluate"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--project-root", type=Path)
        if name in {"inspect", "generate"}:
            command.add_argument("--repo")
            command.add_argument("--instance-id", action="append", default=[])
            command.add_argument("--limit", type=int)
        if name == "generate":
            command.add_argument("--batch", required=True)
        if name == "evaluate":
            command.add_argument("--batch-directory", type=Path, required=True)
            command.add_argument("--evaluation-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行 → 用例；0 为该命令完成，2 为输入/环境错误，3 为配置或兼容实现阻断。

    plan/inspect 成功不代表 benchmark 成功。generate 的配置阻断返回 3，部分任务失败返回 4；不会伪造 patch 或评分。
    """
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config, args.project_root)
        if args.command == "plan":
            output: object = {"mode": "bounded_implementation", "config": config, "stages": STAGES,
                              "evaluation": "separate command; no feedback", "network_calls": 0}
        elif args.command == "doctor":
            output = {
                "mode": "bounded_implementation", "python": platform.python_version(),
                "platform": platform.system(), "project_exists": config.project_root.is_dir(),
                "Agents_md_exists": (config.project_root / "Agents.md").is_file(),
                "dataset_exists": config.dataset.is_file(),
                "source_exists": config.dataset_source.is_file(),
                "env_exists_not_read": config.env_file.is_file(),
                "docker_executable": shutil.which("docker"),
                "docker_daemon_checked": False, "git_checked": False,
                "algorithms_ready": "bounded_semantics", "external_adapters_ready": "requires_configuration",
                "parser_module_exists": bool(config.integration.parser_module and config.integration.parser_module.exists()),
                "model_mode": config.integration.model_mode, "workspace_mode": config.integration.workspace_mode,
            }
        elif args.command in {"inspect", "generate"}:
            tasks = select_tasks(load_tasks(config.dataset), repo=args.repo,
                                 instance_ids=tuple(args.instance_id), limit=args.limit)
            if args.command == "inspect":
                output = {"selected": len(tasks),
                          "repositories": dict(Counter(t.repo for t in tasks)),
                          "issue_asset_count": sum(len(t.assets) for t in tasks),
                          "gold_fields_exposed": False, "revision_inferred_from_count": False}
            else:
                output = run_generation(config, tasks, args.batch,
                                        build_pipeline(config), build_workspace(config))
                print(json.dumps(json_value(output), ensure_ascii=False, indent=2))
                if output.status == "blocked":
                    return 3
                return 0 if output.generated == output.selected else 4
        else:
            output = run_evaluation(config, args.batch_directory, args.evaluation_id,
                                    OfficialDockerEvaluator())
        print(json.dumps(json_value(output), ensure_ascii=False, indent=2))
        return 0
    except ImplementationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (BoundaryRepairError, OSError, ValueError, TypeError) as exc:
        # 此入口不读取密钥；只展示已知校验消息，外部模型异常不得在适配器中携带密钥。
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
