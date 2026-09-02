"""Phase-four artifact inventory, restore verification, and public replay receipts."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from tracejudge_hy3.phase3.privacy import assert_public_payload_safe, canonical_sha256
from tracejudge_hy3.phase3.replay import (
    PublicCertificateReplayResult,
    replay_public_certificate,
)

from .contracts import (
    ArtifactInventoryEntry,
    Phase4ArtifactInventory,
    Phase4GitIdentity,
    Phase4PublicArtifactDigest,
    Phase4PublicReplayReceipt,
    Phase4ReplayRuntime,
    Phase4ReplaySafety,
    PublicArtifactAnchor,
)


class Phase4ReproducibilityError(ValueError):
    """A safe, classified Gate-B reproducibility failure."""

    def __init__(self, message: str, *, safe_stage: str = "P4B_UNCLASSIFIED") -> None:
        super().__init__(message)
        self.safe_stage = safe_stage


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    artifact_id: str
    relative_path: str
    privacy_class: Literal["private_restricted", "deidentified_aggregate", "public_fixture"]
    public_anchor: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactInventoryPreflight:
    inventory: Phase4ArtifactInventory
    public_digest: Phase4PublicArtifactDigest
    private_manifest_payload: bytes
    public_digest_payload: bytes
    private_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactInventoryResult:
    inventory_id: str
    artifact_count: int
    permission_warning_count: int
    artifact_set_sha256: str
    private_manifest_path: Path
    private_manifest_sha256: str
    public_digest_path: Path
    public_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactVerificationResult:
    inventory_id: str
    artifact_count: int
    verified: bool


@dataclass(frozen=True, slots=True)
class ReplayReceiptResult:
    receipt: Phase4PublicReplayReceipt
    receipt_path: Path
    receipt_sha256: str


PHASE4_CRITICAL_ARTIFACTS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec(
        "humanevalplus_raw_snapshot",
        "artifacts/datasets/raw/humanevalplus/test.jsonl",
        "private_restricted",
    ),
    ArtifactSpec(
        "research_dataset_manifest",
        "artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json",
        "deidentified_aggregate",
    ),
    ArtifactSpec(
        "research_dataset_problems",
        "artifacts/datasets/processed/humanevalplus-research-natural-45/problems.jsonl",
        "deidentified_aggregate",
    ),
    ArtifactSpec(
        "phase1_manifest",
        "artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5/manifest.json",
        "private_restricted",
    ),
    ArtifactSpec(
        "phase1_responses",
        "artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5/responses.jsonl",
        "private_restricted",
    ),
    ArtifactSpec(
        "phase1_summary",
        "artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5/summary.json",
        "private_restricted",
    ),
    *tuple(
        ArtifactSpec(
            f"phase2_{name.replace('.', '_')}",
            "artifacts/experiments/phase2-research-natural/"
            "phase2_20260827T081939637435Z_3c366f64fc19/"
            f"{name}",
            "private_restricted",
        )
        for name in (
            "manifest.json",
            "summary.json",
            "results.jsonl",
            "execution.log",
            "samples.jsonl",
            "evalplus_raw_results.json",
        )
    ),
    ArtifactSpec(
        "phase3_natural_manifest",
        "artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json",
        "deidentified_aggregate",
        True,
    ),
    ArtifactSpec(
        "phase3_counterfactual_evidence_manifest",
        "artifacts/experiments/phase3-public-evidence/phase3_cf_public_15_v1/manifest.json",
        "public_fixture",
    ),
    ArtifactSpec(
        "phase3_counterfactual_evidence_results",
        "artifacts/experiments/phase3-public-evidence/phase3_cf_public_15_v1/results.jsonl",
        "public_fixture",
        True,
    ),
    ArtifactSpec(
        "phase3_cohort_manifest",
        "artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json",
        "deidentified_aggregate",
        True,
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_certificate_{index:03d}",
            "artifacts/experiments/phase3-public-certificates/"
            "phase3_gate_d_public_certificates_v1/certificates/"
            f"certificate_{index:03d}.json",
            "public_fixture",
            index == 1,
        )
        for index in range(1, 4)
    ),
    ArtifactSpec(
        "phase3_certificate_manifest",
        "artifacts/experiments/phase3-public-certificates/"
        "phase3_gate_d_public_certificates_v1/manifest.json",
        "public_fixture",
        True,
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_annotation_{name.replace('.', '_')}",
            f"artifacts/experiments/phase3-annotations/phase3_annotation_primary_round1_v1/{name}",
            "private_restricted",
        )
        for name in (
            "manifest.json",
            "packet.jsonl",
            "identity_map.jsonl",
            "labels_template.jsonl",
            "labels_primary_round1_working.jsonl",
        )
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_annotation_work_batch_{index:02d}",
            "artifacts/experiments/phase3-annotations/"
            "phase3_annotation_primary_round1_v1/_work/results/"
            f"batch_{index}.jsonl",
            "private_restricted",
        )
        for index in range(1, 7)
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_annotation_work_item_{index:03d}",
            "artifacts/experiments/phase3-annotations/"
            "phase3_annotation_primary_round1_v1/_work/items/"
            f"item_{index:03d}.json",
            "private_restricted",
        )
        for index in range(1, 58)
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_labels_{name.replace('.', '_')}",
            f"artifacts/experiments/phase3-labels/phase3_labels_primary_round1_v1/{name}",
            "private_restricted",
        )
        for name in ("manifest.json", "completed_labels.jsonl", "annotations.jsonl")
    ),
    ArtifactSpec(
        "phase3_run_manifest",
        "artifacts/experiments/phase3-runs/phase3_hy3_57x5_v1/manifest.json",
        "deidentified_aggregate",
        True,
    ),
    ArtifactSpec(
        "phase3_run_results",
        "artifacts/experiments/phase3-runs/phase3_hy3_57x5_v1/results.jsonl",
        "private_restricted",
        True,
    ),
    ArtifactSpec(
        "phase3_run_index",
        "artifacts/experiments/phase3-runs/phase3_hy3_57x5_v1/index.json",
        "private_restricted",
        True,
    ),
    ArtifactSpec(
        "phase3_invocation_results",
        "artifacts/experiments/phase3-runs/phase3_hy3_57x5_v1/invocations/"
        "invocation_001_8c7f73fd2350/results.jsonl",
        "private_restricted",
    ),
    ArtifactSpec(
        "phase3_provider_raw",
        "artifacts/experiments/phase3-runs/phase3_hy3_57x5_v1/invocations/"
        "invocation_001_8c7f73fd2350/provider_raw.jsonl",
        "private_restricted",
    ),
    ArtifactSpec(
        "phase3_statistics_manifest",
        "artifacts/experiments/phase3-statistics/phase3_stats_primary_round1_v1/manifest.json",
        "deidentified_aggregate",
        True,
    ),
    ArtifactSpec(
        "phase3_statistics_report",
        "artifacts/experiments/phase3-statistics/phase3_stats_primary_round1_v1/report.json",
        "deidentified_aggregate",
        True,
    ),
    *tuple(
        ArtifactSpec(
            f"phase3_report_{artifact_id}",
            f"artifacts/experiments/phase3-reports/phase3_report_primary_round1_v1/{name}",
            "deidentified_aggregate" if name != "demo_certificate.json" else "public_fixture",
            public_anchor,
        )
        for artifact_id, name, public_anchor in (
            ("manifest", "manifest.json", True),
            ("markdown", "phase3_research_report.md", True),
            ("validation", "validation.json", True),
            ("demo_certificate", "demo_certificate.json", False),
            ("replay_command", "replay_command.txt", False),
        )
    ),
)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise Phase4ReproducibilityError(
            "artifact path must be a normalized repository-relative path",
            safe_stage="P4B_PATH",
        )
    return path


def _resolve_regular_file(root: Path, relative_path: str) -> Path:
    relative = _safe_relative_path(relative_path)
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise Phase4ReproducibilityError(
            "required artifact is missing, non-regular, or a symlink",
            safe_stage="P4B_ARTIFACT",
        )
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise Phase4ReproducibilityError(
            "artifact resolves outside the repository root",
            safe_stage="P4B_PATH",
        ) from None
    return resolved


def _git_identity(repo_root: Path) -> Phase4GitIdentity:
    def run(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            raise Phase4ReproducibilityError(
                "Git identity is unavailable",
                safe_stage="P4B_GIT",
            )
        return completed.stdout.strip()

    return Phase4GitIdentity(
        commit=run("rev-parse", "HEAD"),
        branch=run("branch", "--show-current"),
        dirty=bool(run("status", "--porcelain", "--untracked-files=normal")),
    )


def _direct_dependencies_sha256() -> str:
    versions: dict[str, str] = {}
    for package in ("openai", "pydantic", "pydantic-settings", "rich", "typer"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return canonical_sha256(versions)


def _artifact_set_sha256(
    *,
    inventory_id: str,
    source_git: Phase4GitIdentity,
    artifacts: Sequence[ArtifactInventoryEntry],
) -> str:
    """Hash inventory identity and entries without the wall-clock capture time."""

    return canonical_sha256(
        {
            "schema_version": 1,
            "kind": "tracejudge_phase4_artifact_set",
            "inventory_id": inventory_id,
            "source_git": source_git.model_dump(mode="json"),
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
    )


def preflight_artifact_inventory(
    *,
    repo_root: str | Path,
    inventory_id: str,
    digest_id: str = "phase4_public_artifact_digest_v1",
    artifact_specs: Sequence[ArtifactSpec] = PHASE4_CRITICAL_ARTIFACTS,
    created_at: datetime | None = None,
    git_identity: Phase4GitIdentity | None = None,
    allow_dirty: bool = False,
    allow_permission_warnings: bool = False,
) -> ArtifactInventoryPreflight:
    """Hash exact allowlisted files without parsing or publishing their content."""

    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise Phase4ReproducibilityError(
            "repository root is unavailable or unsafe",
            safe_stage="P4B_PATH",
        )
    identity = git_identity or _git_identity(root)
    if identity.dirty and not allow_dirty:
        raise Phase4ReproducibilityError(
            "Git worktree is dirty; formal inventory requires a clean commit",
            safe_stage="P4B_GIT_DIRTY",
        )
    if not artifact_specs:
        raise Phase4ReproducibilityError(
            "artifact allowlist is empty",
            safe_stage="P4B_ARTIFACT",
        )
    entries: list[ArtifactInventoryEntry] = []
    for spec in artifact_specs:
        path = _resolve_regular_file(root, spec.relative_path)
        file_stat = path.stat()
        mode = stat.S_IMODE(file_stat.st_mode)
        permission_warning = spec.privacy_class == "private_restricted" and bool(mode & 0o077)
        entries.append(
            ArtifactInventoryEntry(
                artifact_id=spec.artifact_id,
                relative_path=spec.relative_path,
                privacy_class=spec.privacy_class,
                size_bytes=file_stat.st_size,
                mode_octal=f"0{mode:03o}",
                sha256=_sha256_file(path),
                permission_warning=permission_warning,
            )
        )
    warning_count = sum(item.permission_warning for item in entries)
    if warning_count and not allow_permission_warnings:
        raise Phase4ReproducibilityError(
            "private artifacts have group/other permission bits; hardening is required",
            safe_stage="P4B_PERMISSIONS",
        )
    artifact_set_sha = _artifact_set_sha256(
        inventory_id=inventory_id,
        source_git=identity,
        artifacts=entries,
    )
    created = created_at or datetime.now(UTC)
    inventory = Phase4ArtifactInventory(
        inventory_id=inventory_id,
        created_at=created,
        source_git=identity,
        artifact_set_sha256=artifact_set_sha,
        artifact_count=len(entries),
        permission_warning_count=warning_count,
        artifacts=tuple(entries),
    )
    private_payload = _pretty_json(inventory.model_dump(mode="json"))
    private_sha = _sha256(private_payload)
    entries_by_id = {item.artifact_id: item for item in entries}
    anchors = tuple(
        PublicArtifactAnchor(
            artifact_id=spec.artifact_id,
            sha256=entries_by_id[spec.artifact_id].sha256,
            size_bytes=entries_by_id[spec.artifact_id].size_bytes,
        )
        for spec in artifact_specs
        if spec.public_anchor
    )
    public_digest = Phase4PublicArtifactDigest(
        digest_id=digest_id,
        inventory_id=inventory_id,
        created_at=created,
        source_git=identity,
        artifact_set_sha256=artifact_set_sha,
        private_inventory_sha256=private_sha,
        private_artifact_count=len(entries),
        permission_warning_count=warning_count,
        public_anchor_count=len(anchors),
        public_anchors=anchors,
        privacy_review_status=("permission_hardening_required" if warning_count else "passed"),
    )
    assert_public_payload_safe(public_digest)
    public_payload = _pretty_json(public_digest.model_dump(mode="json"))
    return ArtifactInventoryPreflight(
        inventory=inventory,
        public_digest=public_digest,
        private_manifest_payload=private_payload,
        public_digest_payload=public_payload,
        private_manifest_sha256=private_sha,
    )


def _safe_output_root(value: str | Path) -> Path:
    root = Path(os.path.abspath(Path(value).expanduser()))
    for candidate in (root, *root.parents):
        if candidate.is_symlink():
            raise Phase4ReproducibilityError(
                "output root cannot traverse a symbolic link",
                safe_stage="P4B_OUTPUT",
            )
    if root.exists() and not root.is_dir():
        raise Phase4ReproducibilityError(
            "output root is unsafe",
            safe_stage="P4B_OUTPUT",
        )
    return root


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise Phase4ReproducibilityError(
            "output file already exists",
            safe_stage="P4B_OUTPUT",
        )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except OSError:
        raise Phase4ReproducibilityError(
            "cannot atomically publish output",
            safe_stage="P4B_OUTPUT",
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def freeze_artifact_inventory(
    *,
    private_output_dir: str | Path,
    public_output_dir: str | Path,
    **preflight_kwargs: Any,
) -> ArtifactInventoryResult:
    """Publish a private full inventory and a de-identified public digest."""

    prepared = preflight_artifact_inventory(**preflight_kwargs)
    private_root = _safe_output_root(private_output_dir)
    public_root = _safe_output_root(public_output_dir)
    private_run_dir = private_root / prepared.inventory.inventory_id
    private_manifest = private_run_dir / "manifest.json"
    public_digest = public_root / f"{prepared.public_digest.digest_id}.json"
    if private_run_dir.exists() or private_run_dir.is_symlink() or public_digest.exists():
        raise Phase4ReproducibilityError(
            "inventory output already exists",
            safe_stage="P4B_OUTPUT",
        )
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_root.chmod(0o700)
    private_run_dir.mkdir(parents=True, mode=0o700)
    private_run_dir.chmod(0o700)
    _write_new_file(private_manifest, prepared.private_manifest_payload, mode=0o600)
    _write_new_file(public_digest, prepared.public_digest_payload, mode=0o644)
    return ArtifactInventoryResult(
        inventory_id=prepared.inventory.inventory_id,
        artifact_count=prepared.inventory.artifact_count,
        permission_warning_count=prepared.inventory.permission_warning_count,
        artifact_set_sha256=prepared.inventory.artifact_set_sha256,
        private_manifest_path=private_manifest,
        private_manifest_sha256=prepared.private_manifest_sha256,
        public_digest_path=public_digest,
        public_digest_sha256=_sha256(prepared.public_digest_payload),
    )


def verify_artifact_inventory(
    *,
    repo_root: str | Path,
    manifest_path: str | Path,
) -> ArtifactVerificationResult:
    """Verify hashes, sizes, and exact modes after backup restoration."""

    root = Path(repo_root).expanduser().resolve()
    manifest_file = Path(os.path.abspath(Path(manifest_path).expanduser()))
    if any(candidate.is_symlink() for candidate in (manifest_file, *manifest_file.parents)):
        raise Phase4ReproducibilityError(
            "inventory manifest cannot traverse a symbolic link",
            safe_stage="P4B_VERIFY",
        )
    if not manifest_file.is_file():
        raise Phase4ReproducibilityError(
            "inventory manifest is missing or unsafe",
            safe_stage="P4B_VERIFY",
        )
    try:
        inventory = Phase4ArtifactInventory.model_validate_json(manifest_file.read_bytes())
    except ValueError:
        raise Phase4ReproducibilityError(
            "inventory manifest failed contract validation",
            safe_stage="P4B_VERIFY",
        ) from None
    expected_artifact_set_sha = _artifact_set_sha256(
        inventory_id=inventory.inventory_id,
        source_git=inventory.source_git,
        artifacts=inventory.artifacts,
    )
    if inventory.artifact_set_sha256 != expected_artifact_set_sha:
        raise Phase4ReproducibilityError(
            "inventory artifact-set digest does not match its entries",
            safe_stage="P4B_VERIFY",
        )
    for entry in inventory.artifacts:
        path = _resolve_regular_file(root, entry.relative_path)
        file_stat = path.stat()
        mode = f"0{stat.S_IMODE(file_stat.st_mode):03o}"
        if (
            file_stat.st_size != entry.size_bytes
            or mode != entry.mode_octal
            or _sha256_file(path) != entry.sha256
        ):
            raise Phase4ReproducibilityError(
                "restored artifact differs from the frozen inventory",
                safe_stage="P4B_VERIFY_MISMATCH",
            )
    return ArtifactVerificationResult(
        inventory_id=inventory.inventory_id,
        artifact_count=inventory.artifact_count,
        verified=True,
    )


def _replay_command(
    *,
    certificate_path: str | Path,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
) -> str:
    return " ".join(
        (
            "tracejudge phase3 replay",
            f"--certificate {Path(certificate_path).as_posix()}",
            f"--cohort-manifest {Path(cohort_manifest_path).as_posix()}",
            f"--natural-manifest {Path(natural_manifest_path).as_posix()}",
            f"--source-bundle {Path(source_bundle_path).as_posix()}",
        )
    )


def prepare_public_replay_receipt(
    *,
    receipt_id: str,
    certificate_path: str | Path,
    certificate_manifest_path: str | Path,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    source_bundle_path: str | Path,
    repo_root: str | Path,
    replay_started_at: datetime | None = None,
    git_identity: Phase4GitIdentity | None = None,
    allow_dirty: bool = False,
    replay: Callable[..., PublicCertificateReplayResult] = replay_public_certificate,
) -> Phase4PublicReplayReceipt:
    """Execute one exact public replay and build a content-free public receipt."""

    root = Path(repo_root).expanduser().resolve()
    identity = git_identity or _git_identity(root)
    if identity.dirty and not allow_dirty:
        raise Phase4ReproducibilityError(
            "Git worktree is dirty; formal replay receipt requires a clean commit",
            safe_stage="P4B_GIT_DIRTY",
        )
    paths = {
        "certificate": _resolve_regular_file(root, str(certificate_path)),
        "certificate_manifest": _resolve_regular_file(root, str(certificate_manifest_path)),
        "cohort_manifest": _resolve_regular_file(root, str(cohort_manifest_path)),
        "natural_manifest": _resolve_regular_file(root, str(natural_manifest_path)),
        "source_bundle": _resolve_regular_file(root, str(source_bundle_path)),
    }
    started = replay_started_at or datetime.now(UTC)
    result = replay(
        certificate_path=paths["certificate"],
        cohort_manifest_path=paths["cohort_manifest"],
        natural_manifest_path=paths["natural_manifest"],
        source_bundle_path=paths["source_bundle"],
    )
    completed = datetime.now(UTC)
    if not result.verified or not result.reproduced_failure or result.executed_case_count != 1:
        raise Phase4ReproducibilityError(
            "public replay did not produce a verified single-case failure",
            safe_stage="P4B_REPLAY",
        )
    try:
        implementation_source = inspect.getsourcefile(replay)
    except TypeError:
        implementation_source = None
    implementation_path = Path(implementation_source or "")
    if not implementation_path.is_file():
        raise Phase4ReproducibilityError(
            "replay implementation identity is unavailable",
            safe_stage="P4B_REPLAY",
        )
    receipt = Phase4PublicReplayReceipt(
        receipt_id=receipt_id,
        replay_started_at=started,
        replay_completed_at=completed,
        source_git=identity,
        certificate_id=result.certificate_id,
        trace_id=result.trace_id,
        problem_id=result.problem_id,
        certificate_sha256=_sha256_file(paths["certificate"]),
        certificate_manifest_sha256=_sha256_file(paths["certificate_manifest"]),
        cohort_manifest_sha256=_sha256_file(paths["cohort_manifest"]),
        natural_manifest_sha256=_sha256_file(paths["natural_manifest"]),
        public_source_sha256=_sha256_file(paths["source_bundle"]),
        execution_evidence_sha256=result.execution_evidence_sha256,
        replay_command=_replay_command(
            certificate_path=certificate_path,
            cohort_manifest_path=cohort_manifest_path,
            natural_manifest_path=natural_manifest_path,
            source_bundle_path=source_bundle_path,
        ),
        runtime=Phase4ReplayRuntime(
            python_version=platform.python_version(),
            sandbox_backend=result.sandbox_backend,
            replay_implementation_sha256=_sha256_file(implementation_path),
            direct_dependencies_sha256=_direct_dependencies_sha256(),
        ),
        safety=Phase4ReplaySafety(),
    )
    assert_public_payload_safe(receipt)
    return receipt


def write_public_replay_receipt(
    *,
    output_dir: str | Path,
    **receipt_kwargs: Any,
) -> ReplayReceiptResult:
    """Execute one public replay and atomically persist its de-identified receipt."""

    output_root = _safe_output_root(output_dir)
    receipt_id = receipt_kwargs.get("receipt_id")
    if (
        not isinstance(receipt_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", receipt_id) is None
    ):
        raise Phase4ReproducibilityError(
            "receipt ID is invalid",
            safe_stage="P4B_OUTPUT",
        )
    path = output_root / f"{receipt_id}.json"
    if path.exists() or path.is_symlink():
        raise Phase4ReproducibilityError(
            "output file already exists",
            safe_stage="P4B_OUTPUT",
        )
    receipt = prepare_public_replay_receipt(**receipt_kwargs)
    payload = _pretty_json(receipt.model_dump(mode="json"))
    _write_new_file(path, payload, mode=0o644)
    return ReplayReceiptResult(
        receipt=receipt,
        receipt_path=path,
        receipt_sha256=_sha256(payload),
    )
