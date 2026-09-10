"""仅架构/基础设施测试。所有程序、规格和 patch 均为合成样本，不是修复算法成绩。"""
from __future__ import annotations

import ast
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
import itertools
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from boundary_repair.adapters.dataset import load_tasks, project_task, select_tasks
from boundary_repair.adapters.integrations import DockerWorkspaceAdapter, FrozenModelAdapter
from boundary_repair.adapters.storage import (
    BatchStore, file_sha256, json_value, prediction_row, safe_component, write_json,
)
from boundary_repair.algorithms.controls import PlainControls
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.bootstrap import build_pipeline
from boundary_repair.cli import main
from boundary_repair.config import ModuleSelection, load_config
from boundary_repair.domain.errors import (
    BudgetExceeded, ConfigurationError, DatasetFormatError, ImplementationRequired,
)
from boundary_repair.domain.repair import EditKind, PatchArtifact, PatchPlan, SynthesisResult
from boundary_repair.domain.runtime import BudgetLedger, BudgetLimits, RunContext, SearchPolicy
from boundary_repair.domain.specification import (
    ContractSet, Coverage, InterpretationSpace, SolverStatus,
)
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.experiments.runner import run_generation
from boundary_repair.pipeline import RepairPipeline


def raw_task(instance_id: str = "example__repo-1") -> dict[str, object]:
    """创建包含故意敏感评测字段的合成行，用于验证生成侧投影，而非复现真实问题。"""
    return {
        "instance_id": instance_id, "repo": "example/repo", "base_commit": "a" * 40,
        "problem_statement": "SYNTHETIC: visible label differs from the requested text.",
        "image_assets": {"problem_statement": ["https://example.invalid/issue.png"],
                         "patch": ["GOLD_IMAGE_MUST_NOT_LEAK"],
                         "test_patch": ["TEST_IMAGE_MUST_NOT_LEAK"]},
        "patch": "GOLD_MUST_NOT_LEAK", "test_patch": "TEST_MUST_NOT_LEAK",
        "FAIL_TO_PASS": ["PRIVATE_TEST"], "hints_text": "HINT_MUST_NOT_LEAK",
    }


def contracts() -> ContractSet:
    """创建明确 UNKNOWN/PARTIAL 的合成规格，只供调用传递测试。"""
    return ContractSet((), (), (), (), InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.PARTIAL))


def context() -> RunContext:
    """创建不调用模型的单题上下文，验证同一对象在三个阶段间传递。"""
    return RunContext("synthetic", "example__repo-1", 42, SearchPolicy(), BudgetLedger(BudgetLimits()))


def output() -> SynthesisResult:
    """构造带正确哈希的合成 diff；不是算法求出的补丁，也不进行 benchmark 评分。"""
    diff = "diff --git a/example.txt b/example.txt\n--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-old\n+new\n"
    plan = PatchPlan("synthetic-only", (), EditKind.FREEFORM, (), (), ())
    patch_data = PatchArtifact("example__repo-1", "a" * 40, plan.plan_id, diff,
                               hashlib.sha256(diff.encode()).hexdigest())
    return SynthesisResult(plan, patch_data)


class ScaffoldTests(unittest.TestCase):
    """本机架构验收；不要求 API、Docker、Node 或第三方测试框架。"""

    def setUp(self) -> None:
        """每个测试独立临时项目，不写用户目录、不读取真实 .env。"""
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "dataset" / "dev").mkdir(parents=True)
        self.dataset = self.root / "dataset" / "dev" / "SWE-bench_Multimodal.json"
        self.dataset.write_text(json.dumps([raw_task()]), encoding="utf-8")
        (self.root / "dataset" / "SOURCE.txt").write_text("synthetic revision", encoding="utf-8")
        self.config = load_config(PACKAGE_ROOT / "configs" / "local.json", self.root)

    def test_domain_input_has_no_gold_fields(self) -> None:
        """TaskInput 的结构不提供任何 gold 或评测信息入口。"""
        names = {field.name for field in fields(TaskInput)}
        self.assertEqual(names, {"instance_id", "repo", "base_commit", "problem_statement", "assets"})

    def test_task_is_frozen(self) -> None:
        """中间数据对象不能被下游原地改写。"""
        task = project_task(raw_task())
        with self.assertRaises(FrozenInstanceError):
            setattr(task, "repo", "changed")

    def test_allowlist_excludes_all_answer_fields(self) -> None:
        """原始行含答案，但生成侧序列化不包含它们。"""
        text = json.dumps(json_value(project_task(raw_task())))
        for marker in ("GOLD", "TEST_MUST", "TEST_IMAGE", "PRIVATE_TEST", "HINT"):
            self.assertNotIn(marker, text)
        self.assertIn("issue.png", text)

    def test_string_image_assets_supported(self) -> None:
        """官方历史导出的字符串型 image_assets 可以读取。"""
        row = raw_task()
        row["image_assets"] = json.dumps(row["image_assets"])
        self.assertEqual(len(project_task(row).assets), 1)

    def test_image_assets_are_deduplicated(self) -> None:
        """同一原始附件只保留一次，顺序稳定。"""
        row = raw_task()
        row["image_assets"] = {"problem_statement": ["x", "x", "y"]}
        self.assertEqual([a.uri for a in project_task(row).assets], ["x", "y"])

    def test_bad_asset_shape_rejected(self) -> None:
        """未知附件形状不被默默当作没有图片。"""
        row = raw_task()
        row["image_assets"] = {"problem_statement": {"url": "x"}}
        with self.assertRaises(DatasetFormatError):
            project_task(row)

    def test_json_array_loaded(self) -> None:
        """数组格式数据导入后只得到 TaskInput。"""
        self.assertEqual(len(load_tasks(self.dataset)), 1)

    def test_instance_map_loaded(self) -> None:
        """instance_id 为键的映射可以读取，且需要键与行匹配。"""
        self.dataset.write_text(json.dumps({"example__repo-1": raw_task()}), encoding="utf-8")
        self.assertEqual(load_tasks(self.dataset)[0].instance_id, "example__repo-1")

    def test_jsonl_loaded(self) -> None:
        """明确 .jsonl 扩展名使用逐行解析。"""
        path = self.root / "sample.jsonl"
        path.write_text(json.dumps(raw_task()) + "\n", encoding="utf-8")
        self.assertEqual(len(load_tasks(path)), 1)

    def test_duplicate_instances_rejected(self) -> None:
        """重复题目不能无声覆盖预测或放大样本数。"""
        self.dataset.write_text(json.dumps([raw_task(), raw_task()]), encoding="utf-8")
        with self.assertRaises(DatasetFormatError):
            load_tasks(self.dataset)

    def test_unknown_dataset_wrapper_rejected(self) -> None:
        """未知 wrapper 不能被猜测成任务映射。"""
        self.dataset.write_text(json.dumps({"data": [raw_task()]}), encoding="utf-8")
        with self.assertRaises(DatasetFormatError):
            load_tasks(self.dataset)

    def test_missing_selected_instance_rejected(self) -> None:
        """显式选择不存在的案例应报错而不是空跑。"""
        with self.assertRaises(DatasetFormatError):
            select_tasks(load_tasks(self.dataset), instance_ids=("absent",))

    def test_config_resolves_relative_paths(self) -> None:
        """项目路径不依赖 .git，其他路径相对项目根。"""
        self.assertEqual(self.config.dataset, self.dataset)
        self.assertEqual(self.config.env_file, self.root / "code" / ".env")
        self.assertEqual(self.config.results_root, self.root / "result" / "method" / "boundary_repair")

    def test_user_budget_values_preserved(self) -> None:
        """读取用户给出的预算和采样语义，额外明确每题唯一提交。"""
        self.assertEqual(self.config.budget, BudgetLimits(3600, 100, 150000, 20, 5))
        self.assertEqual(self.config.seed, 42)
        self.assertEqual(self.config.policy.temperature, 1.0)
        self.assertTrue(self.config.policy.allow_multiple_sampling)
        self.assertEqual(self.config.policy.final_submissions, 1)

    def test_unknown_config_key_rejected(self) -> None:
        """拼错的配置字段不能被静默忽略。"""
        data = json.loads((PACKAGE_ROOT / "configs" / "local.json").read_text(encoding="utf-8"))
        data["max_models_call_typo"] = 3
        path = self.root / "bad.json"
        write_json(path, data)
        with self.assertRaises(ConfigurationError):
            load_config(path, self.root)

    def test_invalid_boolean_budget_rejected(self) -> None:
        """bool 不能当作整数预算通过校验。"""
        data = json.loads((PACKAGE_ROOT / "configs" / "local.json").read_text(encoding="utf-8"))
        data["budget"]["max_model_calls"] = True
        path = self.root / "bad.json"
        write_json(path, data)
        with self.assertRaises(ConfigurationError):
            load_config(path, self.root)

    def test_all_eight_ablations_are_wired(self) -> None:
        """三开关均替换为同形对照模块，而非绕过流程或共用创新结论。"""
        for s, l, g in itertools.product((False, True), repeat=3):
            pipeline = build_pipeline(replace(self.config, modules=ModuleSelection(s, l, g)))
            self.assertIsInstance(pipeline.specification, SpecificationRecovery if s else PlainControls)
            self.assertIsInstance(pipeline.localization, ExpressivityLocalization if l else PlainControls)
            self.assertIsInstance(pipeline.synthesis, ScopeSynthesis if g else PlainControls)

    def test_pipeline_calls_exactly_three_stages(self) -> None:
        """用 mocks 验证真实 pipeline 的数据传递和顺序，不实现任何假修复算法。"""
        parent, spec, locator, synth, trace = Mock(), Mock(), Mock(), Mock(), Mock()
        parent.attach_mock(spec, "spec")
        parent.attach_mock(locator, "locator")
        parent.attach_mock(synth, "synth")
        spec.recover.return_value = contracts()
        locator.locate.return_value = "SYNTHETIC_LOCALIZATION"
        synth.synthesize.return_value = output()
        task, run = project_task(raw_task()), context()
        snap = RepositorySnapshot(self.root, "a" * 40, "synthetic")
        result = RepairPipeline(spec, locator, synth).repair(task, snap, run, trace)
        self.assertEqual(result, output())
        self.assertEqual([call[0] for call in parent.mock_calls],
                         ["spec.recover", "locator.locate", "synth.synthesize"])
        locator.locate.assert_called_once_with(task, spec.recover.return_value, snap, run)
        synth.synthesize.assert_called_once_with(task, spec.recover.return_value,
                                                locator.locate.return_value, snap, run)

    def test_pipeline_does_not_continue_after_placeholder(self) -> None:
        """未实现不伪作空规格，也不继续进入定位。"""
        spec, locator, synth = Mock(), Mock(), Mock()
        spec.recover.side_effect = ImplementationRequired("synthetic")
        with self.assertRaises(ImplementationRequired):
            RepairPipeline(spec, locator, synth).repair(
                project_task(raw_task()), RepositorySnapshot(self.root, "a" * 40, "x"), context(), Mock())
        locator.locate.assert_not_called()
        synth.synthesize.assert_not_called()

    def test_specification_rejects_malformed_response(self) -> None:
        """实现后验证坏模型输出不能冒充规格，而不是继续要求占位异常。"""
        from boundary_repair.domain.errors import ValidationError
        from boundary_repair.domain.task import ProgramIndex
        program, model = Mock(), Mock()
        program.index.return_value = ProgramIndex((), ())
        model.complete.return_value.text = "not JSON"
        with self.assertRaises(ValidationError):
            SpecificationRecovery(model, program, Mock()).recover(
                project_task(raw_task()), RepositorySnapshot(self.root, "a" * 40, "x"), context())

    def test_model_adapter_does_not_fake_response(self) -> None:
        """未配置真实 API 时抛配置错误，不能隐式回落到离线 fixture。"""
        with self.assertRaises(ConfigurationError):
            FrozenModelAdapter(self.config).complete(Mock(), context())

    def test_budget_model_calls_and_output_accounted(self) -> None:
        """输出总预算共享；第二次调用仅能使用剩余 token。"""
        ledger = BudgetLedger(BudgetLimits(max_model_calls=2, max_output_tokens=10))
        self.assertEqual(ledger.begin_model_call(8), 8)
        ledger.record_output_tokens(8)
        self.assertEqual(ledger.begin_model_call(8), 2)
        with self.assertRaises(BudgetExceeded):
            ledger.begin_model_call(1)

    def test_budget_overrun_not_hidden(self) -> None:
        """服务端超额输出保留真实计数，然后终止。"""
        ledger = BudgetLedger(BudgetLimits(max_output_tokens=3))
        with self.assertRaises(BudgetExceeded):
            ledger.record_output_tokens(4)
        self.assertEqual(ledger.output_tokens, 4)

    def test_candidate_and_idea_limits(self) -> None:
        """计划/候选计数不会突破显式上限。"""
        ledger = BudgetLedger(BudgetLimits(max_patch_candidates=1, max_ideas=1))
        ledger.claim_candidates()
        ledger.claim_ideas()
        with self.assertRaises(BudgetExceeded):
            ledger.claim_candidates()
        with self.assertRaises(BudgetExceeded):
            ledger.claim_ideas()

    def test_deadline_is_explicit(self) -> None:
        """墙钟过期禁止新工作；不声称能够替代容器硬超时。"""
        ledger = BudgetLedger(BudgetLimits(timeout_seconds=1), started_at=-100000)
        with self.assertRaises(BudgetExceeded):
            ledger.check_deadline()

    def test_storage_rejects_unsafe_names(self) -> None:
        """案例目录不能穿越，Windows 保留名不落盘。"""
        for value in ("../escape", "/tmp", "C:\\tmp", "CON", "NUL.txt", "a..b", "bad."):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                safe_component(value)
        self.assertEqual(safe_component("chartjs__Chart.js-9095"), "chartjs__Chart.js-9095")

    def test_batch_never_overwrites_existing_directory(self) -> None:
        """同名批次明确失败，原有结果不被清空。"""
        store = BatchStore.create(self.root / "results", "batch")
        sentinel = store.root / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            BatchStore.create(self.root / "results", "batch")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_atomic_json_and_sha256(self) -> None:
        """中文 UTF-8 JSON 可重读，哈希与内容一致。"""
        path = self.root / "nested" / "artifact.json"
        write_json(path, {"结果": [1, 2]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"结果": [1, 2]})
        self.assertEqual(file_sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(list(path.parent.glob(".writing-*")))

    def test_prediction_uses_official_three_fields(self) -> None:
        """预测行只包含官方生成字段，不能混入 resolved 或 gold。"""
        row = prediction_row(output().patch, "synthetic")
        self.assertEqual(set(row), {"instance_id", "model_name_or_path", "model_patch"})

    def test_patch_hash_mismatch_rejected(self) -> None:
        """错误哈希不能导出为正式预测。"""
        with self.assertRaises(ValueError):
            prediction_row(replace(output().patch, sha256="wrong"), "synthetic")

    def test_generation_configuration_failure_is_not_scored(self) -> None:
        """缺少镜像映射会中止并保存配置错误，剩余题不计为 unresolved。"""
        tasks = (project_task(raw_task()), project_task(raw_task("example__repo-2")))
        report = run_generation(self.config, tasks, "blocked", build_pipeline(self.config),
                                DockerWorkspaceAdapter(self.config))
        self.assertEqual((report.status, report.attempted, report.unattempted), ("blocked", 1, 1))
        self.assertIsNone(report.resolved)
        self.assertEqual((report.batch_path / "predictions.jsonl").read_text(), "")
        row = json.loads((report.batch_path / "results.jsonl").read_text())
        self.assertEqual(row["status"], "configuration_error")
        self.assertIsNone(row["resolved"])

    def test_injected_generation_writes_artifact_layout(self) -> None:
        """使用 mock 生成器仅检验真实实验存储；此测试不宣称修复成功。"""
        pipeline, workspace = Mock(), Mock()
        pipeline.repair.return_value = output()
        workspace.open_base.return_value = nullcontext(RepositorySnapshot(self.root, "a" * 40, "synthetic"))
        report = run_generation(self.config, (project_task(raw_task()),), "mock-storage", pipeline, workspace)
        self.assertEqual(report.generated, 1)
        self.assertIsNone(report.resolved)
        case = report.batch_path / "cases" / "example__repo-1"
        self.assertEqual({p.name for p in case.iterdir()},
                         {"patch", "input_context", "trajectory", "logs", "result_data"})
        self.assertTrue((case / "patch" / "final.patch").is_file())

    def test_cli_plan_never_reads_env_or_runs_model(self) -> None:
        """plan 只解析配置，项目内敏感文件不会被读取。"""
        original = Path.read_text
        def guarded_read(path: Path, *args: object, **kwargs: object) -> str:
            """拦截任何 .env 读取；其他读取仍使用真实文件系统。"""
            if path.name == ".env":
                raise AssertionError(".env should not be read")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded_read), patch("builtins.print"):
            code = main(["plan", "--config", str(PACKAGE_ROOT / "configs" / "local.json"),
                         "--project-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertFalse(self.config.results_root.exists())

    def test_core_layers_do_not_import_adapters_or_evaluation(self) -> None:
        """依赖方向受测试约束，防止以后把平台细节或评分反馈塞入算法。"""
        package = PACKAGE_ROOT / "src" / "boundary_repair"
        paths = [*list((package / "domain").glob("*.py")),
                 *list((package / "algorithms").glob("*.py")), package / "pipeline.py", package / "ports.py"]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    self.assertFalse(name.startswith(("boundary_repair.adapters", "boundary_repair.experiments",
                                                      "boundary_repair.config")), f"{path}: {name}")

    def test_all_functions_documented_and_annotated(self) -> None:
        """所有源码函数都必须有注释、返回注解和非 self/cls 参数类型。"""
        for path in (PACKAGE_ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    label = f"{path.name}:{node.name}"
                    self.assertIsNotNone(ast.get_docstring(node), label)
                    self.assertIsNotNone(node.returns, label)
                    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                        if argument.arg not in {"self", "cls"}:
                            self.assertIsNotNone(argument.annotation, label)

    def test_source_parses_as_python_311(self) -> None:
        """用 3.11 语法级解析检查最低版本语法；不是实机 3.11 运行验证。"""
        for path in (PACKAGE_ROOT / "src").rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))


    def test_missing_model_reference_is_configuration_error(self) -> None:
        """模型引用缺失应得到字段错误，而非运行时构造 TypeError。"""
        data = json.loads((PACKAGE_ROOT / "configs" / "local.json").read_text(encoding="utf-8"))
        del data["model"]["name_env"]
        path = self.root / "bad.json"
        write_json(path, data)
        with self.assertRaises(ConfigurationError):
            load_config(path, self.root)

    def test_specification_internal_steps_are_connected(self) -> None:
        """替换叶子仅检验 S1→S2→S3→S4 的真实编排，不实现规格算法。"""
        evidence, bindings, space = Mock(), (), Mock()
        task, run = project_task(raw_task()), context()
        snapshot = RepositorySnapshot(self.root, "a" * 40, "synthetic")
        service = SpecificationRecovery(Mock(), Mock(), Mock())
        with patch.object(SpecificationRecovery, "extract_evidence", return_value=evidence) as s1, \
             patch.object(SpecificationRecovery, "bind_entities", return_value=bindings) as s2, \
             patch.object(SpecificationRecovery, "build_interpretation_space", return_value=space) as s3, \
             patch.object(SpecificationRecovery, "derive_contracts", return_value=contracts()) as s4:
            result = service.recover(task, snapshot, run)
        s1.assert_called_once_with(task, snapshot, run)
        s2.assert_called_once_with(evidence, snapshot, run)
        s3.assert_called_once_with(evidence, bindings, run)
        s4.assert_called_once_with(evidence, bindings, space, run)
        self.assertEqual(result, contracts())

    def test_localization_internal_steps_are_connected(self) -> None:
        """定位按固定候选建模、判定并排序，不需要也不获取任何补丁评分。"""
        boundary, local_model, assessment, expected = Mock(), Mock(), Mock(), Mock()
        task, run, specification = project_task(raw_task()), context(), contracts()
        snapshot = RepositorySnapshot(self.root, "a" * 40, "synthetic")
        service = ExpressivityLocalization(Mock(), Mock())
        with patch.object(ExpressivityLocalization, "enumerate_boundaries", return_value=(boundary,)), \
             patch.object(ExpressivityLocalization, "build_local_model", return_value=local_model) as l2, \
             patch.object(ExpressivityLocalization, "assess_expressivity", return_value=assessment) as l3, \
             patch.object(ExpressivityLocalization, "rank_boundaries", return_value=expected) as l4:
            result = service.locate(task, specification, snapshot, run)
        l2.assert_called_once_with(boundary, specification, snapshot, run)
        l3.assert_called_once_with(local_model, specification, run)
        l4.assert_called_once_with((assessment,), specification, run)
        self.assertIs(result, expected)

    def test_synthesis_preserves_unresolved_obligations(self) -> None:
        """作用域未决义务必须传播到 SynthesisResult，不能因 diff 生成而消失。"""
        plan = replace(output().plan, unresolved=("SYNTHETIC_UNPROVEN_FRAME",))
        task, run, specification = project_task(raw_task()), context(), contracts()
        snapshot, localization = RepositorySnapshot(self.root, "a" * 40, "synthetic"), Mock()
        program = Mock()
        program.materialize.return_value = output().patch
        service = ScopeSynthesis(Mock(), program, Mock())
        with patch.object(ScopeSynthesis, "enumerate_plans", return_value=(plan,)), \
             patch.object(ScopeSynthesis, "select_minimal_scope", return_value=plan), \
             patch.object(ScopeSynthesis, "fill_holes", return_value=()):
            result = service.synthesize(task, specification, localization, snapshot, run)
        program.materialize.assert_called_once_with(task, plan, (), snapshot, run)
        self.assertEqual(result.unresolved, ("SYNTHETIC_UNPROVEN_FRAME",))


if __name__ == "__main__":
    unittest.main()
