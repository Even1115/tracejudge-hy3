"""Integrity tests for the constructed counterfactual capability bundle.

Static checks only: the bundle is validated against the frozen annotation
packet and its gold labels are re-validated against the mutated traces. The
mutated counterfactual code is NEVER executed anywhere in this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracejudge_hy3.process_eval_v2.contracts import PilotLabel
from tracejudge_hy3.process_eval_v2.materials import canonical, digest
from tracejudge_hy3.process_eval_v2.preflight import by_id, read_rows
from tracejudge_hy3.schemas.solution import SolutionTrace

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "data/process-eval/mbpp_process_counterfactuals_v1.json"
PACKET_DIR = REPO_ROOT / "artifacts/process-annotations/mbpp120-process-v1"

pytestmark = pytest.mark.skipif(
    not BUNDLE_PATH.exists() or not PACKET_DIR.exists(),
    reason="counterfactual bundle or source packet not present",
)


@pytest.fixture(scope="module")
def bundle():
    return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def packet_items():
    return by_id(read_rows((PACKET_DIR / "development/annotator_a/items.jsonl").read_bytes()))


def test_bundle_honesty_markers(bundle):
    assert bundle["schema"] == "tracejudge-process-counterfactuals-v1"
    assert bundle["constructed"] is True
    assert "never executed" in bundle["execution_policy"]
    assert "excluded" in bundle["statistics_policy"]
    for cf in bundle["counterfactuals"]:
        assert cf["execution_status"] == "not_executed_constructed"
        assert cf["functional_evidence"] == "unavailable_never_executed"


def test_parents_hash_match_frozen_packet(bundle, packet_items):
    assert bundle["source_packet"]["sha256"] == digest((PACKET_DIR / "manifest.json").read_bytes())
    assert len(bundle["parents"]) == 3
    for parent in bundle["parents"]:
        material = packet_items[parent["item_id"]]
        assert parent["packet_item_sha256"] == digest(canonical(material))
        assert parent["problem"] == material["problem"]
        assert parent["solution_trace"] == material["solution_trace"]


def test_sole_change_is_exact(bundle, packet_items):
    for cf in bundle["counterfactuals"]:
        parent = packet_items[cf["parent_item_id"]]
        assert cf["problem"] == parent["problem"]
        mutated = cf["solution_trace"]
        original = parent["solution_trace"]
        if cf["mutation_kind"] == "step_condition_inversion":
            # Steps and code change together; everything else is untouched.
            for key in mutated:
                if key not in ("implementation_steps", "code"):
                    assert mutated[key] == original[key]
            changed = [
                (a, b)
                for a, b in zip(
                    original["implementation_steps"],
                    mutated["implementation_steps"],
                    strict=True,
                )
                if a != b
            ]
            assert len(changed) == 1 and changed[0][0]["step_id"] == "S2"
        else:
            # Only the code string changes.
            for key in mutated:
                if key != "code":
                    assert mutated[key] == original[key]
        assert mutated["code"] != original["code"]


def test_gold_labels_validate_against_mutated_traces(bundle):
    for cf in bundle["counterfactuals"]:
        label = PilotLabel.model_validate(cf["gold_label"])
        assert label.process_correct is False
        assert label.localization_status == "supported"
        solution = SolutionTrace.model_validate(
            {"problem_id": cf["trace_id"], **cf["solution_trace"]}
        )
        label.first_faulty_location.validate_against(solution)
        steps = {step.step_id for step in solution.implementation_steps}
        assert label.first_faulty_step in steps


def test_capability_coverage_and_semantics(bundle):
    by_kind = {cf["mutation_kind"]: cf for cf in bundle["counterfactuals"]}
    assert set(by_kind) == {
        "code_pairing_offset",
        "step_condition_inversion",
        "code_boundary_tightening",
    }
    labels = {kind: PilotLabel.model_validate(cf["gold_label"]) for kind, cf in by_kind.items()}
    # Plan-code mismatch capability: plan correct, code deviates.
    for kind in ("code_pairing_offset", "code_boundary_tightening"):
        assert labels[kind].reasoning_correct is True
        assert labels[kind].plan_code_aligned is False
        assert labels[kind].first_faulty_layer == "alignment"
        assert labels[kind].error_type == "A01_PLAN_CODE_MISMATCH"
    # Step-level reasoning fault with code following the faulty step.
    step = labels["step_condition_inversion"]
    assert step.reasoning_correct is False
    assert step.plan_code_aligned is True
    assert step.first_faulty_layer == "reasoning"
    assert step.first_faulty_step == "S2"
    assert step.error_type == "R01_REQUIREMENT_MISREAD"
    # The mutations really are in the mutated code strings (static check only).
    assert "zip(tup, tup[2:])" in by_kind["code_pairing_offset"]["solution_trace"]["code"]
    assert "x % 2 == 0" in by_kind["step_condition_inversion"]["solution_trace"]["code"]
    assert "len(word) > 4" in by_kind["code_boundary_tightening"]["solution_trace"]["code"]


def test_counterfactual_ids_never_collide_with_real_items(bundle, packet_items):
    real_ids = set(packet_items)
    for cf in bundle["counterfactuals"]:
        assert cf["trace_id"].startswith("counterfactual:")
        assert cf["trace_id"] not in real_ids
        assert cf["gold_label"]["item_id"] == cf["trace_id"]
