"""Unit tests for the program-owned evidence.v3 response protocol."""

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helpers import context, task

from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.domain.errors import BudgetExceeded, ValidationError
from boundary_repair.domain.specification import SolverStatus, Term
from boundary_repair.domain.task import IssueAsset
from boundary_repair.kernel.evidence import (
    EVIDENCE_V3_SYSTEM,
    V3_ISSUE_CHARS,
    evidence_catalog_v3,
    evidence_request_v3,
    parse_evidence_v3,
)
from boundary_repair.kernel.terms import symbol


class EvidenceV3Tests(unittest.TestCase):
    def setUp(self):
        self.task = replace(
            task(),
            problem_statement="标题：保持可见\r\n当 enabled 为真时显示中文文本。\r\n",
            assets=(IssueAsset("https://example.invalid/visual.png", "image-0"),),
        )
        self.code = (
            {"path": "src/界面.js", "source": "// 上文\r\nreturn enabled;\r\n", "start_char": 11},
        )
        catalog = evidence_catalog_v3(self.task, self.code)
        self.issue_span = next(
            row["span_id"] for row in catalog["spans"] if row["kind"] == "issue_text"
        )
        self.code_span = next(
            row["span_id"] for row in catalog["spans"] if row["kind"] == "base_code"
        )

    @staticmethod
    def _term_true():
        return {"op": "literal", "value": True}

    def _claim(self, reference, statement="Display the requested text.", formalization=None):
        return {
            "statement": statement,
            "evidence_refs": [reference],
            "targets": [
                {
                    "entity_id": "VisiblePanel",
                    "property_name": "visibility",
                    "context": self._term_true(),
                }
            ],
            "formalization": formalization,
        }

    def _response(self, observations=None, groups=None, frames=None):
        return json.dumps(
            {
                "observations": observations or [],
                "requirement_groups": groups or [],
                "frames": frames or [],
            },
            ensure_ascii=False,
        )

    def test_request_has_closed_recursive_schema_without_model_owned_ids(self):
        request = evidence_request_v3(self.task, self.code, 1200)
        self.assertEqual(request.schema_name, "evidence.v3")
        self.assertFalse(request.output_schema["additionalProperties"])
        self.assertIn("evidence_catalog", json.loads(request.prompt))
        self.assertIn("full original issue remains in task.json", EVIDENCE_V3_SYSTEM)
        serialized = json.dumps(request.output_schema, sort_keys=True)
        self.assertNotIn("source_id", serialized)
        self.assertNotIn("claim_id", serialized)
        self.assertNotIn("asset_id", serialized)

        seen = set()

        def assert_closed(value):
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    return
                seen.add(marker)
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                    self.assertEqual(set(value["properties"]), set(value["required"]))
                for child in value.values():
                    assert_closed(child)
            elif isinstance(value, list):
                for child in value:
                    assert_closed(child)

        assert_closed(request.output_schema)
        variants = request.output_schema["$defs"]["term"]["anyOf"]
        self.assertTrue(
            any(
                row["properties"].get("args", {}).get("items") == {"$ref": "#/$defs/term"}
                for row in variants
            )
        )

    def test_catalog_preserves_unicode_crlf_and_character_offsets(self):
        catalog = evidence_catalog_v3(self.task, self.code)
        issue_spans = [row for row in catalog["spans"] if row["kind"] == "issue_text"]
        code_spans = [row for row in catalog["spans"] if row["kind"] == "base_code"]
        self.assertEqual("".join(row["text"] for row in issue_spans), self.task.problem_statement)
        self.assertEqual("".join(row["text"] for row in code_spans), self.code[0]["source"])
        self.assertTrue(
            all(
                self.task.problem_statement[row["start"] : row["end"]] == row["text"]
                for row in issue_spans
            )
        )
        self.assertTrue(all(row["start"] >= self.code[0]["start_char"] for row in code_spans))
        self.assertTrue(all(row["end"] - row["start"] <= 1600 for row in catalog["spans"]))

    def test_long_issue_catalog_is_fixed_head_tail_partial(self):
        long_issue = "甲" * (V3_ISSUE_CHARS + 9)
        catalog = evidence_catalog_v3(replace(self.task, problem_statement=long_issue), self.code)
        coverage = catalog["issue_coverage"]
        spans = [row for row in catalog["spans"] if row["kind"] == "issue_text"]
        self.assertEqual(coverage["status"], "PARTIAL")
        self.assertEqual(coverage["provided_chars"], V3_ISSUE_CHARS)
        self.assertEqual(coverage["full_text_location"], "task.json.problem_statement")
        self.assertEqual(
            "".join(row["text"] for row in spans),
            long_issue[: V3_ISSUE_CHARS // 2] + long_issue[-V3_ISSUE_CHARS // 2 :],
        )

    def test_ids_are_program_generated_and_duplicate_model_source_ids_are_rejected(self):
        response = self._response(
            groups=[
                {
                    "alternatives": [
                        {
                            "all_of": [
                                self._claim({"span_id": self.issue_span}),
                            ]
                        }
                    ]
                }
            ]
        )
        bundle = parse_evidence_v3(response, self.task, self.code)
        self.assertEqual([source.source_id for source in bundle.sources], ["evidence-0001"])
        self.assertEqual(
            bundle.claims[0].claim_id, "requirement-group-0001-alternative-0001-claim-0001"
        )
        data = json.loads(response)
        claim = data["requirement_groups"][0]["alternatives"][0]["all_of"][0]
        claim["evidence_refs"][0]["source_id"] = "s1"
        with self.assertRaises(ValidationError):
            parse_evidence_v3(json.dumps(data), self.task, self.code)
        data = json.loads(response)
        data["requirement_groups"][0]["alternatives"][0]["all_of"][0]["claim_id"] = "c1"
        with self.assertRaises(ValidationError):
            parse_evidence_v3(json.dumps(data), self.task, self.code)

    def test_unknown_id_and_code_only_requirement_are_rejected_without_pruning(self):
        data = json.loads(
            self._response(
                groups=[
                    {
                        "alternatives": [
                            {
                                "all_of": [
                                    self._claim({"span_id": self.issue_span}),
                                ]
                            }
                        ]
                    }
                ]
            )
        )
        refs = data["requirement_groups"][0]["alternatives"][0]["all_of"][0]["evidence_refs"]
        refs.append({"span_id": "issue:invented"})
        with self.assertRaisesRegex(ValidationError, "unknown_evidence_span"):
            parse_evidence_v3(json.dumps(data), self.task, self.code)
        code_only = self._response(
            groups=[
                {
                    "alternatives": [
                        {
                            "all_of": [
                                self._claim({"span_id": self.code_span}),
                            ]
                        }
                    ]
                }
            ]
        )
        with self.assertRaisesRegex(ValidationError, "normative_claim_requires_issue_evidence"):
            parse_evidence_v3(code_only, self.task, self.code)

    def test_image_bbox_is_verified_and_program_referenced(self):
        response = self._response(
            frames=[
                self._claim(
                    {"image_id": "image-0", "bbox": [0.1, 0.2, 0.8, 0.9]},
                    statement="Keep the visible image state.",
                )
            ]
        )
        bundle = parse_evidence_v3(response, self.task, self.code)
        self.assertEqual(bundle.sources[0].source_id, "evidence-0001")
        self.assertEqual(bundle.sources[0].locator, "image-0#bbox=0.1,0.2,0.8,0.9")
        invalid = json.loads(response)
        invalid["frames"][0]["evidence_refs"][0]["bbox"] = [0, 0, 1.2, 1]
        with self.assertRaisesRegex(ValidationError, "invalid_image_anchor"):
            parse_evidence_v3(json.dumps(invalid), self.task, self.code)

    def test_nested_alternatives_keep_conjunctions_and_null_formalizations(self):
        response = self._response(
            groups=[
                {
                    "alternatives": [
                        {
                            "all_of": [
                                self._claim({"span_id": self.issue_span}, "First requirement."),
                                self._claim({"span_id": self.issue_span}, "Second requirement."),
                            ]
                        },
                        {
                            "all_of": [
                                self._claim(
                                    {"span_id": self.issue_span}, "Alternative requirement."
                                ),
                            ]
                        },
                    ]
                }
            ]
        )
        bundle = parse_evidence_v3(response, self.task, self.code)
        first = "requirement-group-0001-alternative-0001-claim-0001"
        second = "requirement-group-0001-alternative-0001-claim-0002"
        alternative = "requirement-group-0001-alternative-0002-claim-0001"
        self.assertEqual(bundle.interpretation_groups, (((first, second), (alternative,)),))
        self.assertEqual(bundle.choice_groups, ())
        self.assertEqual(bundle.claims[0].description, "First requirement.")
        self.assertEqual(bundle.claims[0].statement.op, "symbol")
        self.assertEqual(bundle.claims[1].statement.op, "symbol")
        self.assertNotEqual(bundle.claims[0].statement.value, bundle.claims[1].statement.value)
        self.assertTrue(str(bundle.claims[0].statement.value).startswith("uninterpreted:"))

    def test_malformed_terms_and_extra_fields_are_rejected(self):
        formalization = self._term_true()
        for _ in range(26):
            formalization = {"op": "not", "args": [formalization]}
        data = json.loads(
            self._response(
                groups=[
                    {
                        "alternatives": [
                            {
                                "all_of": [
                                    self._claim(
                                        {"span_id": self.issue_span}, formalization=formalization
                                    ),
                                ]
                            }
                        ]
                    }
                ]
            )
        )
        with self.assertRaises(ValidationError):
            parse_evidence_v3(json.dumps(data), self.task, self.code)
        data = json.loads(
            self._response(
                groups=[
                    {
                        "alternatives": [
                            {
                                "all_of": [
                                    self._claim(
                                        {"span_id": self.issue_span},
                                        formalization=self._term_true(),
                                    ),
                                ]
                            }
                        ]
                    }
                ]
            )
        )
        claim = data["requirement_groups"][0]["alternatives"][0]["all_of"][0]
        claim["formalization"]["extra"] = True
        with self.assertRaises(ValidationError):
            parse_evidence_v3(json.dumps(data), self.task, self.code)
        data = json.loads(
            self._response(
                groups=[
                    {
                        "alternatives": [
                            {
                                "all_of": [
                                    self._claim({"span_id": self.issue_span}),
                                ]
                            }
                        ]
                    }
                ]
            )
        )
        data["frames"] = []
        data["unexpected"] = []
        with self.assertRaises(ValidationError):
            parse_evidence_v3(json.dumps(data), self.task, self.code)


class SpecificationRecoveryV3Tests(unittest.TestCase):
    def _bundle(self, formalization=None):
        current_task = replace(task(), problem_statement="Choose one supported presentation.\n")
        span_id = next(row["span_id"] for row in evidence_catalog_v3(current_task, ())["spans"])

        def claim(statement):
            return {
                "statement": statement,
                "evidence_refs": [{"span_id": span_id}],
                "targets": [
                    {
                        "entity_id": "Panel",
                        "property_name": "visible",
                        "context": {"op": "literal", "value": True},
                    }
                ],
                "formalization": formalization,
            }

        response = json.dumps(
            {
                "observations": [],
                "requirement_groups": [
                    {
                        "alternatives": [
                            {"all_of": [claim("First requirement."), claim("Second requirement.")]},
                            {"all_of": [claim("Alternative requirement.")]},
                        ]
                    }
                ],
                "frames": [],
            }
        )
        return current_task, parse_evidence_v3(response, current_task, ())

    def test_nested_theory_selectors_bind_each_conjunct_and_remain_exclusive(self):
        current_task, bundle = self._bundle(formalization={"op": "literal", "value": True})
        logic = LogicAdapter()
        service = SpecificationRecovery(None, None, logic)
        run_context = context()
        space = service.build_interpretation_space(bundle, (), run_context)
        first = "requirement-group-0001-alternative-0001-claim-0001"
        second = "requirement-group-0001-alternative-0001-claim-0002"
        other = "requirement-group-0001-alternative-0002-claim-0001"
        select_first = symbol("select:interpretation:0001:alternative:0001")
        select_other = symbol("select:interpretation:0001:alternative:0002")
        accepted_first = symbol("accept:" + first)
        accepted_second = symbol("accept:" + second)
        accepted_other = symbol("accept:" + other)
        theory = space.assumptions + space.choices
        self.assertEqual(
            logic.check(theory + (select_first, Term("not", (select_other,))), run_context).status,
            SolverStatus.SAT,
        )
        self.assertEqual(
            logic.check(theory + (select_first, select_other), run_context).status,
            SolverStatus.UNSAT,
        )
        self.assertEqual(
            logic.check(
                theory + (select_first, Term("not", (accepted_first,))), run_context
            ).status,
            SolverStatus.UNSAT,
        )
        self.assertEqual(
            logic.check(
                theory + (select_first, Term("not", (accepted_second,))), run_context
            ).status,
            SolverStatus.UNSAT,
        )
        self.assertEqual(
            logic.check(theory + (select_first, accepted_other), run_context).status,
            SolverStatus.UNSAT,
        )
        contracts = service.derive_contracts(bundle, (), space, run_context)
        constraints = contracts.must + contracts.may + contracts.frames
        self.assertEqual(contracts.interpretation_groups, bundle.interpretation_groups)
        self.assertEqual(contracts.sources, bundle.sources)
        self.assertEqual(
            {constraint.description for constraint in constraints},
            {
                "First requirement.",
                "Second requirement.",
                "Alternative requirement.",
            },
        )

    def test_invalid_response_becomes_unavailable_without_a_second_request(self):
        current_task = task()

        class InvalidResponseRecovery(SpecificationRecovery):
            def extract_evidence(self, task_input, snapshot, run_context):
                return self._parse_evidence_response("{invalid", task_input, ())

        result = InvalidResponseRecovery(None, None, LogicAdapter()).recover(
            current_task,
            None,
            context(),
        )
        self.assertEqual(result.extraction_status, "unavailable")
        self.assertEqual(result.theory.coverage.value, "partial")
        self.assertEqual(result.theory.consistency, SolverStatus.UNKNOWN)
        self.assertFalse(result.must + result.may + result.frames + result.witnesses)
        self.assertEqual(result.diagnostics, ("evidence_extraction_unavailable:validation",))

    def test_valid_nonformal_extraction_is_partial_and_budget_errors_propagate(self):
        current_task, bundle = self._bundle()

        class ValidRecovery(SpecificationRecovery):
            def extract_evidence(self, task_input, snapshot, run_context):
                return bundle

            def bind_entities(self, evidence, snapshot, run_context):
                return ()

        result = ValidRecovery(None, None, LogicAdapter()).recover(current_task, None, context())
        self.assertEqual(result.extraction_status, "partial")

        class BudgetFailureRecovery(SpecificationRecovery):
            def extract_evidence(self, task_input, snapshot, run_context):
                raise BudgetExceeded("synthetic")

        with self.assertRaises(BudgetExceeded):
            BudgetFailureRecovery(None, None, LogicAdapter()).recover(current_task, None, context())

        class WorkspaceFailureRecovery(SpecificationRecovery):
            def extract_evidence(self, task_input, snapshot, run_context):
                raise ValidationError("stale_workspace_scope")

        with self.assertRaises(ValidationError):
            WorkspaceFailureRecovery(None, None, LogicAdapter()).recover(
                current_task,
                None,
                context(),
            )


if __name__ == "__main__":
    unittest.main()
