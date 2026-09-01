"""Strict public/private contracts for phase-four reproducibility artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096),
]
OctalMode = Annotated[str, StringConstraints(pattern=r"^0[0-7]{3}$")]


class Phase4Contract(BaseModel):
    """Base contract with fail-closed parsing and immutable values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Phase4GitIdentity(Phase4Contract):
    commit: GitObjectId
    branch: str = Field(min_length=1, max_length=255)
    dirty: bool


class ArtifactInventoryEntry(Phase4Contract):
    artifact_id: Identifier
    relative_path: RelativePath
    privacy_class: Literal["private_restricted", "deidentified_aggregate", "public_fixture"]
    size_bytes: int = Field(ge=0)
    mode_octal: OctalMode
    sha256: Sha256
    permission_warning: bool = False

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("artifact path must be normalized and repository-relative")
        return value


class Phase4ArtifactInventory(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_private_artifact_inventory"] = (
        "tracejudge_phase4_private_artifact_inventory"
    )
    inventory_id: Identifier
    created_at: datetime
    source_git: Phase4GitIdentity
    artifact_set_sha256: Sha256
    artifact_count: int = Field(ge=1)
    permission_warning_count: int = Field(ge=0)
    artifacts: tuple[ArtifactInventoryEntry, ...]
    paths_are_repo_relative: Literal[True] = True
    artifact_content_parsed: Literal[False] = False
    includes_credentials: Literal[False] = False

    @model_validator(mode="after")
    def validate_accounting(self) -> Phase4ArtifactInventory:
        if self.artifact_count != len(self.artifacts):
            raise ValueError("artifact count differs from inventory entries")
        if self.permission_warning_count != sum(item.permission_warning for item in self.artifacts):
            raise ValueError("permission warning count differs from inventory entries")
        artifact_ids = [item.artifact_id for item in self.artifacts]
        relative_paths = [item.relative_path for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact IDs must be unique")
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("artifact paths must be unique")
        return self


class PublicArtifactAnchor(Phase4Contract):
    artifact_id: Identifier
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class Phase4PublicArtifactDigest(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_public_artifact_digest"] = (
        "tracejudge_phase4_public_artifact_digest"
    )
    digest_id: Identifier
    inventory_id: Identifier
    created_at: datetime
    source_git: Phase4GitIdentity
    artifact_set_sha256: Sha256
    private_inventory_sha256: Sha256
    private_artifact_count: int = Field(ge=1)
    permission_warning_count: int = Field(ge=0)
    public_anchor_count: int = Field(ge=1)
    public_anchors: tuple[PublicArtifactAnchor, ...]
    contains_absolute_paths: Literal[False] = False
    contains_private_relative_paths: Literal[False] = False
    contains_artifact_content: Literal[False] = False
    contains_annotation_records: Literal[False] = False
    contains_per_trace_predictions: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False
    privacy_review_status: Literal["passed", "permission_hardening_required"]

    @model_validator(mode="after")
    def validate_accounting(self) -> Phase4PublicArtifactDigest:
        if self.public_anchor_count != len(self.public_anchors):
            raise ValueError("public anchor count differs from digest entries")
        artifact_ids = [item.artifact_id for item in self.public_anchors]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("public artifact anchor IDs must be unique")
        expected_status = (
            "permission_hardening_required" if self.permission_warning_count else "passed"
        )
        if self.privacy_review_status != expected_status:
            raise ValueError("privacy review status differs from permission warnings")
        return self


class Phase4ReplaySafety(Phase4Contract):
    provider_call_count: Literal[0] = 0
    docker_call_count: Literal[0] = 0
    network_call_count: Literal[0] = 0
    executed_public_case_count: Literal[1] = 1
    exact_public_allowlist: Literal[True] = True
    contains_candidate_source: Literal[False] = False
    contains_counterexample_inputs: Literal[False] = False
    contains_provider_raw: Literal[False] = False
    contains_hidden_evaluation_content: Literal[False] = False
    contains_per_trace_predictions: Literal[False] = False


class Phase4ReplayRuntime(Phase4Contract):
    python_version: str = Field(min_length=1, max_length=64)
    sandbox_backend: Literal["trusted-local"] = "trusted-local"
    replay_implementation_sha256: Sha256
    direct_dependencies_sha256: Sha256


class Phase4PublicReplayReceipt(Phase4Contract):
    schema_version: Literal[1] = 1
    kind: Literal["tracejudge_phase4_public_replay_receipt"] = (
        "tracejudge_phase4_public_replay_receipt"
    )
    receipt_id: Identifier
    replay_started_at: datetime
    replay_completed_at: datetime
    source_git: Phase4GitIdentity
    certificate_id: Identifier
    trace_id: Identifier
    problem_id: Identifier
    certificate_sha256: Sha256
    certificate_manifest_sha256: Sha256
    cohort_manifest_sha256: Sha256
    natural_manifest_sha256: Sha256
    public_source_sha256: Sha256
    execution_evidence_sha256: Sha256
    reproduced_failure: Literal[True] = True
    evidence_hash_verified: Literal[True] = True
    replay_command: str = Field(min_length=1, max_length=4096)
    runtime: Phase4ReplayRuntime
    safety: Phase4ReplaySafety

    @model_validator(mode="after")
    def validate_timing(self) -> Phase4PublicReplayReceipt:
        if self.replay_completed_at < self.replay_started_at:
            raise ValueError("replay completion precedes replay start")
        return self
