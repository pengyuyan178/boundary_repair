"""Attachment availability and GIF model-view tests without external services."""

import base64
import hashlib
import io
import json
import sys
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helpers import config, context, task

from boundary_repair.adapters.model import (
    FrozenModelAdapter,
    prepare_asset_view,
    prepare_task_assets,
)
from boundary_repair.domain.errors import (
    BudgetExceeded,
    ConfigurationError,
    ExternalServiceError,
    ValidationError,
)
from boundary_repair.domain.repair import EditKind, PatchArtifact, PatchPlan, SynthesisResult
from boundary_repair.domain.specification import (
    ContractSet,
    Coverage,
    InterpretationSpace,
    SolverStatus,
)
from boundary_repair.domain.task import IssueAsset, RepositorySnapshot, TaskInput
from boundary_repair.experiments.runner import run_generation
from boundary_repair.ports import ModelRequest

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is required for GIF view tests")
class AssetAvailabilityV3Tests(unittest.TestCase):
    def gif_bytes(self) -> bytes:
        first = Image.new("RGBA", (3, 2), (255, 0, 0, 255))
        second = Image.new("RGBA", (3, 2), (0, 0, 255, 255))
        output = io.BytesIO()
        first.save(output, format="GIF", save_all=True, append_images=[second], duration=10, loop=0)
        return output.getvalue()

    def data_uri(self, mime: str, data: bytes) -> tuple[str, str]:
        return f"data:{mime};base64,{base64.b64encode(data).decode()}", hashlib.sha256(
            data
        ).hexdigest()

    def runner_config(self, root: Path):
        conf = config(root, parser=False)
        conf.dataset.parent.mkdir(parents=True)
        conf.dataset.write_text("[]", encoding="utf-8")
        conf.dataset_source.parent.mkdir(parents=True, exist_ok=True)
        conf.dataset_source.write_text("synthetic", encoding="utf-8")
        return conf

    def synthesis_output(
        self, instance_id: str, base_commit: str, generation_mode: str
    ) -> SynthesisResult:
        diff = "diff --git a/ui.js b/ui.js\n--- a/ui.js\n+++ b/ui.js\n@@ -1 +1 @@\n-old\n+new\n"
        plan = PatchPlan("plan", (), EditKind.FREEFORM, (), (), (), generation_mode=generation_mode)
        patch = PatchArtifact(
            instance_id,
            base_commit,
            plan.plan_id,
            diff,
            hashlib.sha256(diff.encode()).hexdigest(),
            application_check="passed",
            syntax_check="unknown",
            generation_mode=generation_mode,
        )
        return SynthesisResult(plan, patch)

    def test_gif_view_keeps_original_hash_and_marks_partial_coverage(self):
        data = self.gif_bytes()
        asset = IssueAsset("https://images.example.com/animated.gif", "issue-image-0")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf, ctx = config(root, parser=False), context()
            with patch(
                "boundary_repair.adapters.model._download_asset",
                return_value=self.data_uri("image/gif", data),
            ):
                uri, digest = prepare_asset_view(asset, conf, ctx)
            key = hashlib.sha256(asset.uri.encode()).hexdigest()
            assets = conf.results_root / ctx.run_id / "cases" / ctx.instance_id / "assets"
            self.assertTrue(uri.startswith("data:image/png;base64,"))
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual((assets / (key + ".bin")).read_bytes(), data)
            metadata = json.loads((assets / (key + ".view.json")).read_text(encoding="utf-8"))
            self.assertEqual(metadata["original_sha256"], digest)
            self.assertEqual(
                (metadata["frame"], metadata["total_frames"], metadata["coverage"]),
                (0, 2, "partial"),
            )
            view = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
            self.assertEqual(view.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 255))

    def test_no_assets_writes_empty_availability_without_mutating_task(self):
        original = task()
        with TemporaryDirectory() as temporary:
            conf, ctx = config(Path(temporary), parser=False), context()
            prepared = prepare_task_assets(original, conf, ctx)
            availability = (
                conf.results_root
                / ctx.run_id
                / "cases"
                / ctx.instance_id
                / "assets"
                / "availability.json"
            )
            self.assertIsNot(prepared, original)
            self.assertEqual(prepared.assets, ())
            self.assertEqual(original.assets, ())
            self.assertEqual(json.loads(availability.read_text(encoding="utf-8")), {"assets": []})

    def test_partial_failure_filters_task_and_model_does_not_transfer_failed_asset_again(self):
        good = IssueAsset("https://images.example.com/good.png", "issue-image-0")
        failed = IssueAsset("https://images.example.com/missing.png", "issue-image-1")
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue", (good, failed))
        png = b"\x89PNG\r\n\x1a\nsynthetic"
        transfers: list[str] = []

        def download(asset, _config, _context):
            transfers.append(asset.source_id)
            if asset is failed:
                raise ExternalServiceError("asset_http_status:404")
            return self.data_uri("image/png", png)

        with TemporaryDirectory() as temporary:
            conf, ctx = config(Path(temporary), parser=False), context()
            with (
                patch("boundary_repair.adapters.model._download_asset", side_effect=download),
                patch(
                    "boundary_repair.adapters.model.read_model_environment",
                    return_value={
                        conf.model.name_env: "test-model",
                        conf.model.endpoint_env: "http://local",
                        conf.model.api_key_env: "test-key",
                    },
                ),
                patch(
                    "boundary_repair.adapters.model.chat_endpoint",
                    return_value=("http", "local", 80, "/chat/completions"),
                ),
                patch("boundary_repair.adapters.model.http.client.HTTPConnection") as transport,
            ):
                prepared = prepare_task_assets(original, conf, ctx)
                response = transport.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps(
                    {
                        "id": "response",
                        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                        "usage": {"completion_tokens": 1},
                    }
                ).encode()
                FrozenModelAdapter(conf).complete(
                    ModelRequest("system", "prompt", prepared.assets, "test.v1", 4), ctx
                )
            availability = (
                conf.results_root
                / ctx.run_id
                / "cases"
                / ctx.instance_id
                / "assets"
                / "availability.json"
            )
            self.assertEqual(prepared.assets, (good,))
            self.assertEqual(original.assets, (good, failed))
            self.assertEqual(transfers, ["issue-image-0", "issue-image-1"])
            self.assertEqual(
                json.loads(availability.read_text(encoding="utf-8"))["assets"],
                [
                    {
                        "source_id": "issue-image-0",
                        "status": "available",
                        "code": None,
                        "original_sha256": hashlib.sha256(png).hexdigest(),
                    },
                    {
                        "source_id": "issue-image-1",
                        "status": "unavailable",
                        "code": "asset_http_status:404",
                    },
                ],
            )

    def test_budget_exhaustion_propagates_from_prefetch(self):
        asset = IssueAsset("https://images.example.com/a.png", "issue-image-0")
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue", (asset,))
        with TemporaryDirectory() as temporary:
            conf, ctx = config(Path(temporary), parser=False), context()
            with patch(
                "boundary_repair.adapters.model.prepare_asset_view",
                side_effect=BudgetExceeded("task_timeout"),
            ):
                with self.assertRaises(BudgetExceeded):
                    prepare_task_assets(original, conf, ctx)

    def test_gif_partial_notice_is_sent_to_model(self):
        data = self.gif_bytes()
        asset = IssueAsset("https://images.example.com/animated.gif", "issue-image-0")
        request = ModelRequest("system", "prompt", (asset,), "test.v1", 4)
        with TemporaryDirectory() as temporary:
            conf, ctx = config(Path(temporary), parser=False), context()
            with (
                patch(
                    "boundary_repair.adapters.model._download_asset",
                    return_value=self.data_uri("image/gif", data),
                ),
                patch(
                    "boundary_repair.adapters.model.read_model_environment",
                    return_value={
                        conf.model.name_env: "test-model",
                        conf.model.endpoint_env: "http://local",
                        conf.model.api_key_env: "test-key",
                    },
                ),
                patch(
                    "boundary_repair.adapters.model.chat_endpoint",
                    return_value=("http", "local", 80, "/chat/completions"),
                ),
                patch("boundary_repair.adapters.model.http.client.HTTPConnection") as transport,
            ):
                response = transport.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps(
                    {
                        "id": "response",
                        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                        "usage": {"completion_tokens": 1},
                    }
                ).encode()
                result = FrozenModelAdapter(conf).complete(request, ctx)
                content = json.loads(transport.return_value.request.call_args.kwargs["body"])[
                    "messages"
                ][1]["content"]
            self.assertEqual(result.asset_sha256, (hashlib.sha256(data).hexdigest(),))
            self.assertIn(
                {"type": "text", "text": "GIF view uses frame 0 of 2; coverage: partial."}, content
            )
            self.assertTrue(
                any(
                    item.get("type") == "image_url"
                    and item["image_url"]["url"].startswith("data:image/png")
                    for item in content
                )
            )

    def test_runner_keeps_original_task_and_records_contract_coverage(self):
        asset = IssueAsset("https://images.example.com/a.png", "issue-image-0")
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue", (asset,))
        filtered = replace(original, assets=())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = self.runner_config(root)
            pipeline, workspace = Mock(), Mock()
            snapshot = RepositorySnapshot(root, original.base_commit, "synthetic")
            workspace.open_base.return_value = nullcontext(snapshot)

            def prefetch(saved_task, saved_config, run):
                task_path = (
                    saved_config.results_root
                    / run.run_id
                    / "cases"
                    / run.instance_id
                    / "input_context"
                    / "task.json"
                )
                self.assertEqual(
                    json.loads(task_path.read_text(encoding="utf-8"))["assets"][0]["uri"], asset.uri
                )
                self.assertIs(saved_task, original)
                return filtered

            def repair(prepared_task, _snapshot, _run, trace):
                trace.save(
                    "contracts.json",
                    ContractSet(
                        (),
                        (),
                        (),
                        (),
                        InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.COMPLETE),
                    ),
                )
                return self.synthesis_output(original.instance_id, original.base_commit, "scoped")

            pipeline.repair.side_effect = repair
            with patch(
                "boundary_repair.adapters.model.prepare_task_assets", side_effect=prefetch
            ) as prepare:
                report = run_generation(conf, (original,), "assets-runner", pipeline, workspace)
            self.assertEqual(report.generated, 1)
            prepare.assert_called_once()
            self.assertIs(pipeline.repair.call_args.args[0], filtered)
            case = report.batch_path / "cases" / original.instance_id
            self.assertEqual(
                json.loads((case / "input_context" / "task.json").read_text(encoding="utf-8"))[
                    "assets"
                ],
                [{"uri": asset.uri, "source_id": asset.source_id, "media_type": None}],
            )
            record = json.loads(
                (case / "result_data" / "inference.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["status"], "generated")
            self.assertEqual(record["generation_mode"], "scoped")
            self.assertEqual(record["application_check"], "passed")
            self.assertEqual(record["syntax_check"], "unknown")
            self.assertEqual(record["contract_coverage"], "complete")
            self.assertEqual(record["extraction_status"], "partial")
            self.assertEqual(record["semantic_coverage"], "partial")

    def test_runner_maps_mode_to_coverage_when_contracts_are_unavailable(self):
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = self.runner_config(root)
            pipeline, workspace = Mock(), Mock()
            workspace.open_base.return_value = nullcontext(
                RepositorySnapshot(root, original.base_commit, "synthetic")
            )
            pipeline.repair.return_value = self.synthesis_output(
                original.instance_id, original.base_commit, "raw_evidence"
            )
            report = run_generation(conf, (original,), "mode-fallback", pipeline, workspace)
            row = json.loads((report.batch_path / "results.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["status"], "generated")
            self.assertEqual(row["semantic_coverage"], "partial")

    def test_runner_records_complete_semantic_coverage_only_for_certified_mode(self):
        """Certified generation is the sole mode that can report complete local source coverage."""
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = self.runner_config(root)
            pipeline, workspace = Mock(), Mock()
            workspace.open_base.return_value = nullcontext(
                RepositorySnapshot(root, original.base_commit, "synthetic")
            )
            pipeline.repair.return_value = self.synthesis_output(
                original.instance_id, original.base_commit, "certified"
            )
            report = run_generation(conf, (original,), "certified-mode", pipeline, workspace)
            record = json.loads((report.batch_path / "results.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["generation_mode"], "certified")
            self.assertEqual(record["semantic_coverage"], "complete")
            self.assertIsNone(record["contract_coverage"])
            self.assertIsNone(record["extraction_status"])

    def test_runner_retains_frozen_plan_metadata_after_compilation_failure(self):
        """A frozen plan and contracts remain auditable when patch validation later fails."""
        failure_checks = {
            "generated_patch_not_applicable": ("failed", "unknown"),
            "generated_syntax_invalid": ("not_run", "failed"),
        }
        for component, expected_checks in failure_checks.items():
            with self.subTest(component=component), TemporaryDirectory() as temporary:
                root = Path(temporary)
                conf = self.runner_config(root)
                original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue")
                pipeline, workspace = Mock(), Mock()
                workspace.open_base.return_value = nullcontext(
                    RepositorySnapshot(root, original.base_commit, "synthetic")
                )

                def repair(_task, _snapshot, _run, trace):
                    trace.save(
                        "contracts.json",
                        ContractSet(
                            (),
                            (),
                            (),
                            (),
                            InterpretationSpace((), (), SolverStatus.UNKNOWN, Coverage.COMPLETE),
                            extraction_status="partial",
                        ),
                    )
                    trace.save(
                        "generation_plan.json",
                        PatchPlan(
                            "frozen-plan",
                            (),
                            EditKind.FREEFORM,
                            (),
                            (),
                            (),
                            generation_mode="scoped",
                        ),
                    )
                    raise ValidationError(component)

                pipeline.repair.side_effect = repair
                report = run_generation(
                    conf, (original,), "frozen-" + component, pipeline, workspace
                )
                record = json.loads(
                    (report.batch_path / "results.jsonl").read_text(encoding="utf-8")
                )
                self.assertEqual(report.generated, 0)
                self.assertEqual(record["status"], "validation_error")
                self.assertEqual(record["component"], component)
                self.assertEqual(record["generation_mode"], "scoped")
                self.assertEqual(record["contract_coverage"], "complete")
                self.assertEqual(record["extraction_status"], "partial")
                self.assertEqual(record["semantic_coverage"], "partial")
                self.assertEqual(
                    (record["application_check"], record["syntax_check"]), expected_checks
                )

    def test_runner_records_partial_coverage_for_failed_certified_plan(self):
        """A frozen certified plan remains diagnostic only when materialization does not finish."""
        original = TaskInput("demo__ui-1", "demo/ui", "a" * 40, "issue")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = self.runner_config(root)
            pipeline, workspace = Mock(), Mock()
            workspace.open_base.return_value = nullcontext(
                RepositorySnapshot(root, original.base_commit, "synthetic")
            )

            def repair(_task, _snapshot, _run, trace):
                trace.save(
                    "generation_plan.json",
                    PatchPlan(
                        "certified-plan",
                        (),
                        EditKind.REFINE_GUARD,
                        (),
                        (),
                        (),
                        generation_mode="certified",
                    ),
                )
                raise ValidationError("generated_patch_not_applicable")

            pipeline.repair.side_effect = repair
            report = run_generation(conf, (original,), "failed-certified", pipeline, workspace)
            record = json.loads((report.batch_path / "results.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(report.generated, 0)
            self.assertEqual(record["generation_mode"], "certified")
            self.assertEqual(record["semantic_coverage"], "partial")
            self.assertEqual(record["application_check"], "failed")
            self.assertEqual(record["syntax_check"], "unknown")

    def test_version_three_protocols_require_an_output_schema(self):
        from boundary_repair.adapters.model import response_format

        with TemporaryDirectory() as temporary:
            conf = config(Path(temporary), parser=False)
            for schema_name in ("evidence.v3", "evidence.v4", "edits.v3", "edits.v4"):
                with self.subTest(schema_name=schema_name):
                    with self.assertRaisesRegex(ConfigurationError, "output_schema_missing"):
                        response_format(ModelRequest("system", "prompt", (), schema_name, 4), conf)


if __name__ == "__main__":
    unittest.main()
