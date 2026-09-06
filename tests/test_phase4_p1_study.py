from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import tracejudge_hy3.phase4.p1_study as study_module
from tracejudge_hy3.cli import app
from tracejudge_hy3.phase4 import (
    P1_DELIVERY_SCHEMA_SHA256,
    P1FormalSubsetCommitment,
    P1SingleDeliveryRecord,
    Phase4P1AnnotationError,
    create_p1_delivery_record_template,
    delivery_record_schema_payload,
    freeze_p1_formal_subset,
    preflight_p1_delivery_record,
    preflight_p1_formal_subset,
    verify_p1_formal_subset,
)

REPO_ROOT = Path(__file__).parents[1]
PROTOCOL = REPO_ROOT / "data/phase4/p1_second_annotator_protocol_v1.json"
DELIVERY_SCHEMA = REPO_ROOT / "data/phase4/p1_single_delivery_record_schema_v1.json"
FORMAL_COMMITMENT = (
    REPO_ROOT
    / "docs/experiments/phase4_p1_formal_subset/phase4_p1_formal_subset_v1/commitment.json"
)
FORMAL_COMMITMENT_SHA256 = "b5090ad78715857455852e3450fa606f4963ca726a3df91a1b6603d372c491a2"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _trace(index: int, *, kind: str, parent: str | None = None) -> SimpleNamespace:
    trace_id = f"{kind}:fixture_{index:03d}:v1"
    return SimpleNamespace(
        trace_id=trace_id,
        trace_kind=kind,
        problem_id=f"fixture_{index:03d}",
        public_problem_sha256=_digest(f"problem:{trace_id}"),
        solution_trace_sha256=_digest(f"solution:{trace_id}"),
        structured_explanation_sha256=_digest(f"explanation:{trace_id}"),
        code_sha256=_digest(f"code:{trace_id}"),
        functional_evidence=SimpleNamespace(
            functional_evidence_sha256=_digest(f"evidence:{trace_id}")
        ),
        parent_trace_id=parent,
    )


def _synthetic_cohort() -> tuple[SimpleNamespace, SimpleNamespace]:
    natural = SimpleNamespace(traces=tuple(_trace(index, kind="natural") for index in range(1, 43)))
    parents = ("public-parent:a:v1", "public-parent:b:v1", "public-parent:c:v1")
    counterfactuals = tuple(
        _trace(index, kind="counterfactual", parent=parents[(index - 1) % 3])
        for index in range(1, 16)
    )
    cohort = SimpleNamespace(counterfactuals=counterfactuals)
    return cohort, natural


def _patch_synthetic_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    cohort, natural = _synthetic_cohort()
    monkeypatch.setattr(
        study_module,
        "_load_phase3_manifests",
        lambda **_kwargs: (cohort, natural),
    )
    return cohort, natural


def test_delivery_schema_is_deterministic_and_contains_no_delivery_values() -> None:
    payload = DELIVERY_SCHEMA.read_bytes()
    assert payload == delivery_record_schema_payload()
    assert hashlib.sha256(payload).hexdigest() == P1_DELIVERY_SCHEMA_SHA256
    schema = json.loads(payload)
    assert schema["type"] == "object"
    assert "file_delivery_channel" in payload.decode()
    assert "coordinator_contact" in payload.decode()
    assert "may_contain_private_contact_details" in payload.decode()
    assert "contains_direct_identity" not in payload.decode()
    assert "@" not in payload.decode()


def test_delivery_template_is_private_no_overwrite_and_fail_closed(tmp_path: Path) -> None:
    record_path = tmp_path / "private" / "delivery_record.json"
    result = create_p1_delivery_record_template(
        schema_path=DELIVERY_SCHEMA,
        record_path=record_path,
    )
    assert result.record_status == "pending_completion"
    assert result.missing_required_count == 16
    assert result.data_collection_allowed is False
    assert stat.S_IMODE(record_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600

    repeated = preflight_p1_delivery_record(
        schema_path=DELIVERY_SCHEMA,
        record_path=record_path,
    )
    assert repeated == result
    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        create_p1_delivery_record_template(
            schema_path=DELIVERY_SCHEMA,
            record_path=record_path,
        )

    record_path.parent.chmod(0o755)
    with pytest.raises(Phase4P1AnnotationError, match="parent permissions are too broad"):
        preflight_p1_delivery_record(
            schema_path=DELIVERY_SCHEMA,
            record_path=record_path,
        )


def test_delivery_template_rejects_symlink_parent(tmp_path: Path) -> None:
    actual_private = tmp_path / "actual-private"
    actual_private.mkdir(mode=0o700)
    linked_private = tmp_path / "linked-private"
    linked_private.symlink_to(actual_private, target_is_directory=True)
    with pytest.raises(Phase4P1AnnotationError, match="parent is unsafe"):
        create_p1_delivery_record_template(
            schema_path=DELIVERY_SCHEMA,
            record_path=linked_private / "delivery_record.json",
        )


def test_delivery_record_cannot_claim_readiness_with_missing_fields() -> None:
    with pytest.raises(ValidationError, match="ready delivery record"):
        P1SingleDeliveryRecord(
            schema_sha256=P1_DELIVERY_SCHEMA_SHA256,
            record_status="ready_for_practice_delivery",
            data_collection_allowed=True,
        )


def test_checked_in_formal_commitment_is_frozen_and_deidentified() -> None:
    payload = FORMAL_COMMITMENT.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == FORMAL_COMMITMENT_SHA256
    commitment = P1FormalSubsetCommitment.model_validate(json.loads(payload))
    assert commitment.selected_total_count == 20
    assert commitment.counterfactual_parent_count == 3
    assert commitment.counterfactual_per_parent_maximum == 2
    assert commitment.contains_selected_trace_ids is False
    assert commitment.contains_problem_ids is False
    assert commitment.contains_private_paths is False
    assert commitment.formal_packet_created is False
    assert commitment.formal_data_collected is False


def test_formal_subset_preflight_is_deterministic_balanced_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort, natural = _patch_synthetic_cohort(monkeypatch)
    all_trace_ids = tuple(item.trace_id for item in (*natural.traces, *cohort.counterfactuals))
    first = preflight_p1_formal_subset(
        protocol_path=PROTOCOL,
        privacy_canaries=all_trace_ids,
    )
    second = preflight_p1_formal_subset(
        protocol_path=PROTOCOL,
        privacy_canaries=all_trace_ids,
    )
    assert first == second
    assert first.commitment.selected_natural_count == 15
    assert first.commitment.selected_counterfactual_count == 5
    assert first.commitment.selected_total_count == 20
    assert first.commitment.counterfactual_parent_count == 3
    assert max(first.manifest.counterfactual_parent_counts.values()) == 2
    assert len(first.manifest.records) == 20
    assert first.commitment.formal_packet_created is False
    assert first.commitment.formal_data_collected is False

    public_value = first.commitment.model_dump(mode="json")
    assert "records" not in public_value
    assert "counterfactual_parent_counts" not in public_value
    assert "private_manifest_path" not in public_value
    assert all(trace_id not in json.dumps(public_value) for trace_id in all_trace_ids)


def test_formal_subset_freeze_is_no_overwrite_private_and_byte_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_synthetic_cohort(monkeypatch)
    private_path = tmp_path / "private" / "manifest.json"
    public_path = tmp_path / "public" / "commitment.json"
    result = freeze_p1_formal_subset(
        protocol_path=PROTOCOL,
        private_manifest_path=private_path,
        public_commitment_path=public_path,
    )
    assert stat.S_IMODE(private_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o644

    verified = verify_p1_formal_subset(
        protocol_path=PROTOCOL,
        private_manifest_path=private_path,
        public_commitment_path=public_path,
        expected_public_commitment_sha256=result.public_commitment_sha256,
    )
    assert verified.verified is True
    assert verified.selected_total_count == 20
    with pytest.raises(Phase4P1AnnotationError, match="already exists"):
        freeze_p1_formal_subset(
            protocol_path=PROTOCOL,
            private_manifest_path=private_path,
            public_commitment_path=public_path,
        )

    public_path.write_bytes(public_path.read_bytes() + b" ")
    with pytest.raises(Phase4P1AnnotationError, match="deterministic regeneration"):
        verify_p1_formal_subset(
            protocol_path=PROTOCOL,
            private_manifest_path=private_path,
            public_commitment_path=public_path,
        )


def test_cli_formal_subset_preflight_never_discloses_selected_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort, natural = _patch_synthetic_cohort(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["phase4", "p1-formal-subset-preflight", "--protocol", str(PROTOCOL)],
    )
    assert result.exit_code == 0
    assert "15 / 5 / 20" in result.stdout
    assert "未读取 / 未读取 / 未读取" in result.stdout
    assert all(
        item.trace_id not in result.stdout for item in (*natural.traces, *cohort.counterfactuals)
    )
