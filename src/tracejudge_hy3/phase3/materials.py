"""Gate-E loading of exact frozen trace bodies without hidden evaluation data.

The loader revalidates the same phase-one/two and public-Fixture bundles used by
Gate B, then reconstructs only the five-method allowlisted inputs.  It never
opens EvalPlus ``samples.jsonl`` or ``evalplus_raw_results.json``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .cohort import _load_phase1_freeze_input, _load_phase2_freeze_input
from .contracts import (
    CounterfactualCohortManifest,
    CounterfactualTrace,
    FrozenCohortManifest,
    NaturalTrace,
    PublicFixtureExecutionResult,
    PublicFixtureFunctionalEvidenceRef,
)
from .counterfactual import (
    _load_execution_evidence,
    _load_source_bundle,
    _public_problem_payload,
)
from .privacy import canonical_sha256
from .runner import (
    LoadedPairedCohort,
    Phase3RunnerError,
    Phase3TraceMaterial,
    PublicDynamicEvidenceInput,
    _read_manifest,
    load_paired_cohort,
    validate_materials,
)

_DYNAMIC_POLICY_VERSION = "phase3_public_dynamic_evidence_v1"


@dataclass(frozen=True, slots=True)
class LoadedPhase3Materials:
    cohort: LoadedPairedCohort
    materials: Mapping[str, Phase3TraceMaterial]
    material_payloads_sha256: str
    natural_dynamic_unavailable_count: int
    public_dynamic_evidence_count: int


def _natural_dynamic_evidence(functional_evidence_sha256: str) -> PublicDynamicEvidenceInput:
    payload = {
        "policy_version": _DYNAMIC_POLICY_VERSION,
        "availability": "not_available",
        "reason": "no_frozen_public_oracle_for_natural_trace",
        "attempted_public_case_count": 0,
        "functional_evidence_sha256": functional_evidence_sha256,
    }
    return PublicDynamicEvidenceInput(
        status="available",
        evidence_sha256=canonical_sha256(payload),
        payload=payload,
    )


def _public_execution_payload(
    result: PublicFixtureExecutionResult,
    *,
    execution_evidence_sha256: str,
) -> dict[str, Any]:
    """Remove mutation expectations while retaining public reproducible evidence."""

    return {
        "policy_version": _DYNAMIC_POLICY_VERSION,
        "availability": "available",
        "source": "frozen_public_fixture_execution",
        "problem_id": result.problem_id,
        "public_fixture_id": result.public_fixture_id,
        "public_fixture_sha256": result.public_fixture_sha256,
        "code_sha256": result.code_sha256,
        "replay_spec_sha256": result.replay_spec_sha256,
        "execution_evidence_sha256": execution_evidence_sha256,
        "execution_status": result.execution_status,
        "case_count": result.case_count,
        "pass_count": result.pass_count,
        "fail_count": result.fail_count,
        "timeout_count": result.timeout_count,
        "case_results": [item.model_dump(mode="json") for item in result.case_results],
    }


def _load_manifests(
    *,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
) -> tuple[LoadedPairedCohort, FrozenCohortManifest, CounterfactualCohortManifest]:
    cohort = load_paired_cohort(
        overlay_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    _natural_payload, natural_value = _read_manifest(
        Path(natural_manifest_path), label="natural manifest"
    )
    _overlay_payload, overlay_value = _read_manifest(
        Path(cohort_manifest_path), label="overlay manifest"
    )
    try:
        natural = FrozenCohortManifest.model_validate(natural_value)
        overlay = CounterfactualCohortManifest.model_validate(overlay_value)
    except ValidationError:
        raise Phase3RunnerError(
            "phase-three material manifests failed schema validation",
            safe_stage="P3E_MATERIAL",
        ) from None
    return cohort, natural, overlay


def load_phase3_materials(
    *,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    privacy_canaries: Sequence[str | bytes] = (),
) -> LoadedPhase3Materials:
    """Reconstruct and hash-bind all 57 allowlisted method/annotation materials."""

    cohort, natural, overlay = _load_manifests(
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
    )
    phase1 = _load_phase1_freeze_input(
        Path(phase1_run_dir),
        Path(dataset_manifest_path),
        privacy_canaries=privacy_canaries,
    )
    phase2 = _load_phase2_freeze_input(Path(phase2_run_dir), phase1)
    if (
        phase1.bundle != natural.phase1
        or phase1.dataset != natural.dataset
        or phase2.bundle != natural.phase2
    ):
        raise Phase3RunnerError(
            "natural material inputs differ from the frozen manifest",
            safe_stage="P3E_MATERIAL",
        )

    seeds_by_trace_id = {f"natural:{seed.problem_id}": seed for seed in phase1.natural_seeds}
    materials: dict[str, Phase3TraceMaterial] = {}
    for trace in natural.traces:
        if not isinstance(trace, NaturalTrace) or trace.trace_id not in seeds_by_trace_id:
            raise Phase3RunnerError(
                "natural material set differs from frozen traces",
                safe_stage="P3E_MATERIAL",
            )
        seed = seeds_by_trace_id[trace.trace_id]
        evidence = phase2.evidence_by_problem.get(trace.problem_id)
        if evidence is None or evidence != trace.functional_evidence:
            raise Phase3RunnerError(
                "natural functional evidence differs from frozen trace",
                safe_stage="P3E_MATERIAL",
            )
        materials[trace.trace_id] = Phase3TraceMaterial(
            trace_id=trace.trace_id,
            public_problem=seed.public_problem,
            solution_trace=seed.solution_trace,
            functional_evidence=evidence,
            public_dynamic_evidence=_natural_dynamic_evidence(evidence.functional_evidence_sha256),
        )

    source = _load_source_bundle(
        source_bundle_path,
        expected_source_sha256=overlay.source.source_bundle_sha256,
        privacy_canaries=privacy_canaries,
    )
    evidence_bundle = _load_execution_evidence(
        execution_run_dir,
        prepared_source=source,
        privacy_canaries=privacy_canaries,
    )
    if (
        source.bundle.bundle_id != overlay.source.bundle_id
        or source.source_sha256 != overlay.source.source_bundle_sha256
        or evidence_bundle.identity != overlay.execution
    ):
        raise Phase3RunnerError(
            "public counterfactual material inputs differ from the frozen overlay",
            safe_stage="P3E_MATERIAL",
        )

    parents_by_id = {item.parent_trace_id: item for item in source.bundle.parents}
    variants_by_id = {item.trace_id: item for item in source.bundle.counterfactuals}
    for trace in overlay.counterfactuals:
        if not isinstance(trace, CounterfactualTrace):
            raise Phase3RunnerError(
                "overlay contains a non-counterfactual trace",
                safe_stage="P3E_MATERIAL",
            )
        variant = variants_by_id.get(trace.trace_id)
        if variant is None or variant.parent_trace_id not in parents_by_id:
            raise Phase3RunnerError(
                "counterfactual source no longer covers the frozen overlay",
                safe_stage="P3E_MATERIAL",
            )
        parent = parents_by_id[variant.parent_trace_id]
        functional = trace.functional_evidence
        if not isinstance(functional, PublicFixtureFunctionalEvidenceRef):
            raise Phase3RunnerError(
                "counterfactual lacks public Fixture evidence",
                safe_stage="P3E_MATERIAL",
            )
        result = evidence_bundle.results_by_subject.get(functional.execution_subject_id)
        result_sha = evidence_bundle.result_sha256_by_subject.get(functional.execution_subject_id)
        if result is None or result_sha is None:
            raise Phase3RunnerError(
                "counterfactual public execution row is unavailable",
                safe_stage="P3E_MATERIAL",
            )
        dynamic_payload = _public_execution_payload(
            result,
            execution_evidence_sha256=result_sha,
        )
        materials[trace.trace_id] = Phase3TraceMaterial(
            trace_id=trace.trace_id,
            public_problem=_public_problem_payload(parent.fixture),
            solution_trace=variant.solution_trace,
            functional_evidence=functional,
            public_dynamic_evidence=PublicDynamicEvidenceInput(
                status="available",
                evidence_sha256=canonical_sha256(dynamic_payload),
                payload=dynamic_payload,
            ),
        )

    validate_materials(cohort, materials, privacy_canaries=privacy_canaries)
    ordered_payloads = [
        materials[trace_id].model_dump(mode="json") for trace_id in cohort.ordered_trace_ids
    ]
    return LoadedPhase3Materials(
        cohort=cohort,
        materials=materials,
        material_payloads_sha256=canonical_sha256(ordered_payloads),
        natural_dynamic_unavailable_count=cohort.natural_trace_count,
        public_dynamic_evidence_count=cohort.counterfactual_trace_count,
    )


__all__ = ["LoadedPhase3Materials", "load_phase3_materials"]
