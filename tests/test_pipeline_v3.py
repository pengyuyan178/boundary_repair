"""Synthetic v3 pipeline coverage without providers, repositories, or benchmark scoring."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helpers import catalogue_response, config, context

from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.domain.repair import ExpressivityVerdict
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.experiments.runner import run_generation
from boundary_repair.kernel.files import tree_digest
from boundary_repair.pipeline import RepairPipeline
from boundary_repair.ports import ModelResponse


class TraceDouble:
    """Capture pipeline artefacts while retaining the production stage order."""

    def __init__(self) -> None:
        self.events = []
        self.artifacts = {}

    def emit(self, event) -> None:
        """Record an emitted stage event."""
        self.events.append(event)

    def save(self, name, artifact) -> None:
        """Record a saved typed intermediate artefact."""
        self.artifacts[name] = artifact


class V3ModelDouble:
    """Return one scripted evidence response and at most one transaction response."""

    def __init__(
        self, evidence: str = "valid", transaction: str = "replace", before_edit=None
    ) -> None:
        self.evidence = evidence
        self.transaction = transaction
        self.before_edit = before_edit
        self.requests = []

    @property
    def edit_calls(self) -> int:
        """Count only the one-shot generation calls."""
        return sum(request.schema_name == "edits.v5" for request in self.requests)

    def complete(self, request, run_context) -> ModelResponse:
        """Provide deterministic v3 JSON and account for each synthetic model response."""
        self.requests.append(request)
        run_context.budget.begin_model_call(request.max_output_tokens)
        if request.schema_name == "evidence.v6":
            text = "not-json" if self.evidence == "invalid" else json.dumps(catalogue_response(self._evidence(request), json.loads(request.prompt)))
        elif request.schema_name == "edits.v5":
            if self.before_edit is not None:
                self.before_edit(request)
            text = json.dumps(self._transaction(request))
        else:
            raise AssertionError(f"unexpected schema: {request.schema_name}")
        run_context.budget.record_output_tokens(1)
        return ModelResponse(text, 1, f"synthetic-{len(self.requests)}")

    @staticmethod
    def _evidence(request) -> dict[str, object]:
        """Cite an exact issue span while retaining a deliberately null formalization."""
        span_id = json.loads(request.prompt)["evidence_catalog"]["spans"][0]["span_id"]
        claim = {
            "statement": "Use the requested blue theme styling.",
            "evidence_refs": [{"span_id": span_id}],
            "targets": [],
            "formalization": None,
            "entry_cases": [],
        }
        return {
            "observations": [],
            "requirement_groups": [{"alternatives": [{"all_of": [claim]}]}],
            "frames": [],
        }

    def _transaction(self, request) -> dict[str, object]:
        """Return one bounded transaction selected by the test scenario."""
        if self.transaction == "empty":
            return {"edits": []}
        if self.transaction == "illegal":
            return {
                "edits": [
                    {
                        "operation": "replace_text",
                        "target": "unknown-region",
                        "new_text": "x",
                        "old_text": "old",
                        "destination": "",
                    }
                ]
            }
        regions = json.loads(request.prompt)["regions"]
        if self.transaction == "comment":
            region = regions[0]
            return {
                "edits": [
                    {
                        "operation": "replace_text",
                        "target": region["region_id"],
                "new_text": region["source"]
                        + "/* visual explanation only */",
                "old_text": region["source"],
                        "destination": "",
                    }
                ]
            }
        edits = []
        for region in regions:
            for line in region["source"].splitlines(keepends=True):
                if "red" in line:
                    edits.append(
                        {
                            "operation": "replace_text",
                            "target": region["region_id"],
                        "new_text": line.replace("red", "blue"),
                        "old_text": line,
                            "destination": "",
                        }
                    )
        if not edits:
            raise AssertionError("synthetic source did not expose a replaceable line")
        return {"edits": edits}


class PipelineV3Tests(unittest.TestCase):
    """Exercise v3 fallbacks and atomic transaction behaviour with source-only fixtures."""

    issue = "Use a blue CSS theme and matching JavaScript theme value in the interface."

    def setUp(self) -> None:
        """Allocate a self-contained project root and text-only parser configuration."""
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = config(self.root, parser=False)

    def make_snapshot(self, files: dict[str, str] | None = None) -> RepositorySnapshot:
        """Write a two-file UI fixture and return its immutable base snapshot identity."""
        source = self.root / "source"
        source.mkdir()
        for relative, contents in (
            files
            or {
                "theme.css": ".banner {\n  color: red;\n}\n",
                "theme.js": "export const themeColor = 'red';\n",
            }
        ).items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        return RepositorySnapshot(source, "a" * 40, tree_digest(source))

    def task(self) -> TaskInput:
        """Return a task whose commit matches every synthetic snapshot."""
        return TaskInput("demo__ui-1", "demo/ui", "a" * 40, self.issue)

    def pipeline(self, model: V3ModelDouble) -> RepairPipeline:
        """Build the real v3 stages around deterministic test-only provider responses."""
        program = ProgramAdapter(self.config)
        logic = LogicAdapter()
        return RepairPipeline(
            SpecificationRecovery(model, program, logic),
            ExpressivityLocalization(program, logic),
            ScopeSynthesis(model, program, logic),
        )

    def run_pipeline(self, model: V3ModelDouble):
        """Run the production pipeline against a fresh UI snapshot and inspect its trace."""
        trace = TraceDouble()
        result = self.pipeline(model).repair(self.task(), self.make_snapshot(), context(), trace)
        return result, trace

    def prepare_runner_inputs(self) -> None:
        """Create runner manifest inputs required by the existing batch store."""
        self.config.dataset.parent.mkdir(parents=True, exist_ok=True)
        self.config.dataset.write_text("[]", encoding="utf-8")
        self.config.dataset_source.parent.mkdir(parents=True, exist_ok=True)
        self.config.dataset_source.write_text("synthetic", encoding="utf-8")

    def test_null_formalization_scopes_multifile_css_transaction(self) -> None:
        """Natural-language CSS evidence reaches one scoped, applicable multi-file transaction."""
        model = V3ModelDouble()
        result, trace = self.run_pipeline(model)
        self.assertEqual(result.plan.generation_mode, "scoped")
        self.assertEqual(result.patch.application_check, "passed")
        self.assertIn("a/theme.css", result.patch.unified_diff)
        self.assertIn("a/theme.js", result.patch.unified_diff)
        self.assertEqual(trace.artifacts["contracts.json"].extraction_status, "partial")
        self.assertEqual(model.edit_calls, 1)
        self.assertEqual(
            [request.schema_name for request in model.requests], ["evidence.v6", "edits.v5"]
        )

    def test_invalid_evidence_uses_raw_evidence_once(self) -> None:
        """Invalid evidence becomes unavailable without a second evidence request."""
        model = V3ModelDouble(evidence="invalid")
        result, trace = self.run_pipeline(model)
        self.assertEqual(trace.artifacts["contracts.json"].extraction_status, "unavailable")
        self.assertEqual(result.plan.generation_mode, "raw_evidence")
        self.assertEqual(result.patch.application_check, "passed")
        self.assertEqual(
            [request.schema_name for request in model.requests], ["evidence.v6", "edits.v5"]
        )
        self.assertEqual(model.edit_calls, 1)

    def test_text_only_parser_unknown_still_generates_once(self) -> None:
        """Text-only parsing retains UNKNOWN boundaries and allows one scoped generation."""
        model = V3ModelDouble()
        result, trace = self.run_pipeline(model)
        localization = trace.artifacts["localization.json"]
        self.assertTrue(localization.assessments)
        self.assertTrue(
            all(item.verdict == ExpressivityVerdict.UNKNOWN for item in localization.assessments)
        )
        self.assertEqual(result.plan.generation_mode, "scoped")
        self.assertEqual(model.edit_calls, 1)

    def test_illegal_transaction_fails_case_without_second_generation(self) -> None:
        """An invalid operation rejects the case without a corrective model request."""
        self.prepare_runner_inputs()
        snapshot = self.make_snapshot()
        model = V3ModelDouble(transaction="illegal")
        workspace = Mock()
        workspace.open_base.return_value = nullcontext(snapshot)
        fixture_config = replace(
            self.config, integration=replace(self.config.integration, model_mode="fixture")
        )
        report = run_generation(
            fixture_config, (self.task(),), "illegal-transaction", self.pipeline(model), workspace
        )
        record = json.loads((report.batch_path / "results.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((report.generated, record["status"]), (0, "validation_error"))
        self.assertEqual(record["generation_mode"], "scoped")
        self.assertEqual(record["extraction_status"], "partial")
        self.assertIn(record["contract_coverage"], {"complete", "partial"})
        self.assertEqual(record["semantic_coverage"], "partial")
        self.assertEqual(model.edit_calls, 1)
        self.assertFalse(
            (
                report.batch_path / "cases" / self.task().instance_id / "patch" / "final.patch"
            ).exists()
        )

    def test_generation_plan_is_persisted_before_transaction_request(self) -> None:
        """The frozen plan exists before the model receives editable regions."""
        checked = []

        def verify_plan(_request) -> None:
            """Verify the plan artifact is written before the one generation request."""
            path = (
                self.config.results_root
                / "synthetic"
                / "cases"
                / "demo__ui-1"
                / "trajectory"
                / "generation_plan.json"
            )
            self.assertTrue(path.is_file())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["generation_mode"], "scoped"
            )
            checked.append(path)

        model = V3ModelDouble(before_edit=verify_plan)
        result, _ = self.run_pipeline(model)
        self.assertEqual(result.patch.application_check, "passed")
        self.assertEqual(len(checked), 1)
        self.assertEqual(model.edit_calls, 1)

    def test_empty_or_comment_only_transactions_never_emit_patch(self) -> None:
        """Empty and omission-style comments leave no patch and never cause another generation."""
        for transaction in ("empty", "comment"):
            with self.subTest(transaction=transaction):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    config_value = config(root, parser=False)
                    config_value.dataset.parent.mkdir(parents=True, exist_ok=True)
                    config_value.dataset.write_text("[]", encoding="utf-8")
                    config_value.dataset_source.parent.mkdir(parents=True, exist_ok=True)
                    config_value.dataset_source.write_text("synthetic", encoding="utf-8")
                    source = root / "source"
                    source.mkdir()
                    (source / "theme.css").write_text(".banner { color: red; }\n", encoding="utf-8")
                    snapshot = RepositorySnapshot(source, "a" * 40, tree_digest(source))
                    task_value = TaskInput("demo__ui-1", "demo/ui", "a" * 40, self.issue)
                    model = V3ModelDouble(transaction=transaction)
                    program = ProgramAdapter(config_value)
                    logic = LogicAdapter()
                    pipeline = RepairPipeline(
                        SpecificationRecovery(model, program, logic),
                        ExpressivityLocalization(program, logic),
                        ScopeSynthesis(model, program, logic),
                    )
                    workspace = Mock()
                    workspace.open_base.return_value = nullcontext(snapshot)
                    fixture_config = replace(
                        config_value,
                        integration=replace(config_value.integration, model_mode="fixture"),
                    )
                    report = run_generation(
                        fixture_config, (task_value,), f"no-{transaction}", pipeline, workspace
                    )
                    record = json.loads(
                        (report.batch_path / "results.jsonl").read_text(encoding="utf-8")
                    )
                    self.assertEqual(report.generated, 0)
                    self.assertIn(record["status"], {"validation_error", "no_admissible_patch"})
                    self.assertEqual(model.edit_calls, 1)
                    self.assertFalse(
                        (
                            report.batch_path
                            / "cases"
                            / task_value.instance_id
                            / "patch"
                            / "final.patch"
                        ).exists()
                    )


if __name__ == "__main__":
    unittest.main()
