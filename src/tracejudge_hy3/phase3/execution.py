"""Gate-E3 formal paired-evaluation preparation and explicit Hy3 execution.

The preflight reconstructs the exact allowlisted method materials, verifies the
immutable annotation set, freezes the public Provider configuration, and binds
the Git/Python environment without creating an output directory or connecting
to a Provider.  The execution entry point is separate and requires an explicit
real-Provider confirmation.
"""

from __future__ import annotations

import hashlib
import platform
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openai
from pydantic import ValidationError

from tracejudge_hy3.baseline.runner import _dependency_versions, _git_metadata
from tracejudge_hy3.config import Settings, get_settings
from tracejudge_hy3.prompts.phase3 import prompt_bundle_sha256
from tracejudge_hy3.redaction import redact_sensitive_text

from .annotations import ANNOTATION_GUIDE_SHA256, ANNOTATION_PROTOCOL_SHA256
from .contracts import AnnotationSetManifest, MethodId, Phase3ResumeIdentity, Phase3RunManifest
from .materials import LoadedPhase3Materials, load_phase3_materials
from .privacy import assert_public_payload_safe, canonical_sha256
from .runner import (
    Phase3ExecutionBindings,
    Phase3ProviderCallError,
    Phase3RunnerError,
    Phase3RunResult,
    ProviderCallResult,
    _read_manifest,
    ast_implementation_sha256,
    build_method_specs,
    implementation_sha256,
    method_specs_sha256,
    output_schema_sha256,
    provider_config_sha256,
    public_evidence_policy_sha256,
    run_paired_evaluation,
)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_MAX_PRIVATE_LABEL_BYTES = 64 * 1024 * 1024
PHASE3_EVALUATION_RANDOM_SEED = 20260828


@dataclass(frozen=True, slots=True)
class LoadedAnnotationSetBinding:
    manifest: AnnotationSetManifest
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class Phase3EvaluationPreflight:
    run_id: str
    resume: bool
    annotation_set_id: str
    natural_trace_count: int
    counterfactual_trace_count: int
    trace_count: int
    method_count: int
    pair_count: int
    provider_pair_count: int
    maximum_provider_call_count: int
    method_specs_sha256: str
    prompt_bundle_sha256: str
    output_schema_sha256: str
    material_payloads_sha256: str
    provider_config_sha256: str
    annotation_set_manifest_sha256: str
    completed_labels_sha256: str
    annotation_records_sha256: str
    resume_identity_sha256: str
    provider: str
    model: str
    git_commit: str
    git_branch: str
    git_dirty: bool


@dataclass(frozen=True, slots=True)
class Phase3EvaluationResult:
    preflight: Phase3EvaluationPreflight
    run: Phase3RunResult


@dataclass(frozen=True, slots=True)
class _PreparedPhase3Evaluation:
    loaded: LoadedPhase3Materials
    specs: tuple[Any, ...]
    bindings: Phase3ExecutionBindings
    identity: Phase3ResumeIdentity
    provider_configuration: Mapping[str, Any]
    settings: Settings
    preflight: Phase3EvaluationPreflight


def _read_private_payload(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase3RunnerError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    try:
        file_mode = stat.S_IMODE(path.stat().st_mode)
        if file_mode & 0o077:
            raise Phase3RunnerError(
                f"{label} permissions are too broad",
                safe_stage="P3E_ANNOTATION_BINDING",
            )
        if path.stat().st_size > _MAX_PRIVATE_LABEL_BYTES:
            raise Phase3RunnerError(
                f"{label} exceeds the size limit",
                safe_stage="P3E_ANNOTATION_BINDING",
            )
        payload = path.read_bytes()
    except OSError:
        raise Phase3RunnerError(
            f"cannot read {label}",
            safe_stage="P3E_ANNOTATION_BINDING",
        ) from None
    if len(payload) > _MAX_PRIVATE_LABEL_BYTES:
        raise Phase3RunnerError(
            f"{label} exceeds the size limit",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    return payload


def _read_public_identity_payload(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise Phase3RunnerError(
            f"{label} must be a regular non-symlink file",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    try:
        if path.stat().st_size > _MAX_PRIVATE_LABEL_BYTES:
            raise Phase3RunnerError(
                f"{label} exceeds the size limit",
                safe_stage="P3E_ANNOTATION_BINDING",
            )
        payload = path.read_bytes()
    except OSError:
        raise Phase3RunnerError(
            f"cannot read {label}",
            safe_stage="P3E_ANNOTATION_BINDING",
        ) from None
    return payload


def load_annotation_set_binding(
    *,
    manifest_path: str | Path,
    expected_manifest_sha256: str,
    frozen_manifest_sha256: str,
    ordered_trace_ids: Sequence[str],
    natural_trace_count: int,
    counterfactual_trace_count: int,
    protocol_path: str | Path,
    guide_path: str | Path,
) -> LoadedAnnotationSetBinding:
    """Verify the private label set by hashes without parsing label contents."""

    if not _SHA256_PATTERN.fullmatch(expected_manifest_sha256):
        raise Phase3RunnerError(
            "annotation manifest SHA256 is invalid",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    path = Path(manifest_path)
    payload, value = _read_manifest(path, label="annotation-set manifest")
    actual_manifest_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise Phase3RunnerError(
            "annotation-set manifest hash differs",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    try:
        manifest = AnnotationSetManifest.model_validate(value)
    except ValidationError:
        raise Phase3RunnerError(
            "annotation-set manifest failed schema validation",
            safe_stage="P3E_ANNOTATION_BINDING",
        ) from None

    root = path.parent
    try:
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise Phase3RunnerError(
                "annotation-set directory is not private",
                safe_stage="P3E_ANNOTATION_BINDING",
            )
    except OSError:
        raise Phase3RunnerError(
            "cannot inspect annotation-set directory",
            safe_stage="P3E_ANNOTATION_BINDING",
        ) from None

    completed_payload = _read_private_payload(
        root / "completed_labels.jsonl", label="completed labels"
    )
    annotation_payload = _read_private_payload(
        root / "annotations.jsonl", label="annotation records"
    )
    if hashlib.sha256(completed_payload).hexdigest() != manifest.completed_labels_sha256:
        raise Phase3RunnerError(
            "completed-label payload hash differs",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    if hashlib.sha256(annotation_payload).hexdigest() != manifest.annotation_records_sha256:
        raise Phase3RunnerError(
            "annotation-record payload hash differs",
            safe_stage="P3E_ANNOTATION_BINDING",
        )

    protocol_payload = _read_public_identity_payload(
        Path(protocol_path), label="annotation protocol"
    )
    guide_payload = _read_public_identity_payload(Path(guide_path), label="annotation guide")
    protocol_sha256 = hashlib.sha256(protocol_payload).hexdigest()
    guide_sha256 = hashlib.sha256(guide_payload).hexdigest()
    if (
        protocol_sha256 != ANNOTATION_PROTOCOL_SHA256
        or protocol_sha256 != manifest.annotation_protocol_sha256
        or guide_sha256 != ANNOTATION_GUIDE_SHA256
        or guide_sha256 != manifest.annotation_guide_sha256
    ):
        raise Phase3RunnerError(
            "annotation protocol or guide identity differs",
            safe_stage="P3E_ANNOTATION_BINDING",
        )

    if (
        manifest.frozen_cohort_manifest_sha256 != frozen_manifest_sha256
        or manifest.ordered_trace_ids != tuple(ordered_trace_ids)
        or manifest.record_count != len(ordered_trace_ids)
        or manifest.natural_trace_count != natural_trace_count
        or manifest.counterfactual_trace_count != counterfactual_trace_count
    ):
        raise Phase3RunnerError(
            "annotation set differs from the frozen paired cohort",
            safe_stage="P3E_ANNOTATION_BINDING",
        )
    return LoadedAnnotationSetBinding(
        manifest=manifest,
        manifest_sha256=actual_manifest_sha256,
    )


def _endpoint_fingerprint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
        netloc = f"{hostname}:{port}" if port is not None else hostname
        path = parsed.path.rstrip("/") or "/"
        canonical = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except (TypeError, ValueError):
        canonical = "unparseable-endpoint"
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _hy3_public_configuration(settings: Settings, *, model: str) -> dict[str, Any]:
    if not settings.hy3_configured():
        raise Phase3RunnerError(
            "Hy3 is not configured",
            safe_stage="P3E_PROVIDER_CONFIG",
        )
    if settings.hy3_model != model:
        raise Phase3RunnerError(
            "configured Hy3 model differs from the requested model",
            safe_stage="P3E_PROVIDER_CONFIG",
        )
    assert settings.hy3_base_url is not None
    assert settings.hy3_api_key is not None
    for value in (model, settings.hy3_reasoning_effort):
        if redact_sensitive_text(value, known_secrets=(settings.hy3_api_key,)) != value:
            raise Phase3RunnerError(
                "public Provider configuration contains sensitive text",
                safe_stage="P3E_PROVIDER_CONFIG",
            )
    try:
        openai_version = metadata.version("openai")
    except metadata.PackageNotFoundError:
        openai_version = "unavailable"
    configuration = {
        "provider": "hy3",
        "model": model,
        "reasoning_effort": (
            settings.hy3_reasoning_effort if settings.hy3_enable_reasoning_effort else None
        ),
        "reasoning_effort_enabled": settings.hy3_enable_reasoning_effort,
        "endpoint_sha256": _endpoint_fingerprint(settings.hy3_base_url),
        "transport_max_retries": 0,
        "openai_version": openai_version,
    }
    assert_public_payload_safe(configuration)
    return configuration


class Phase3Hy3JudgeProvider:
    """Single-call Hy3 adapter; the paired runner exclusively owns JSON repair."""

    name = "hy3"

    def __init__(self, settings: Settings, *, model: str) -> None:
        self.model = model
        self._settings = settings
        self._configuration = _hy3_public_configuration(settings, model=model)
        assert settings.hy3_base_url is not None
        assert settings.hy3_api_key is not None
        self._client = openai.AsyncOpenAI(
            base_url=settings.hy3_base_url,
            api_key=settings.hy3_api_key,
            max_retries=0,
        )
        self._fatal_diagnostic: str | None = None

    def public_configuration(self) -> Mapping[str, Any]:
        return dict(self._configuration)

    async def complete(
        self,
        *,
        method_id: MethodId,
        messages: tuple[dict[str, str], ...],
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderCallResult:
        del method_id
        if self._fatal_diagnostic is not None:
            raise Phase3ProviderCallError(self._fatal_diagnostic)
        extra_body: dict[str, Any] = {}
        if self._settings.hy3_enable_reasoning_effort:
            extra_body["reasoning_effort"] = self._settings.hy3_reasoning_effort
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=list(messages),  # type: ignore[arg-type]
                temperature=temperature,
                timeout=timeout_seconds,
                extra_body=extra_body or None,
            )
        except openai.AuthenticationError:
            self._fatal_diagnostic = "provider_auth_error"
            raise Phase3ProviderCallError(self._fatal_diagnostic) from None
        except (openai.APITimeoutError, TimeoutError):
            raise Phase3ProviderCallError("provider_timeout") from None
        except openai.RateLimitError:
            raise Phase3ProviderCallError("provider_rate_limit") from None
        except openai.APIConnectionError:
            raise Phase3ProviderCallError("provider_connection_error") from None
        except openai.APIStatusError:
            raise Phase3ProviderCallError("provider_api_status_error") from None

        if not response.choices:
            raise Phase3ProviderCallError("provider_empty_response")
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise Phase3ProviderCallError("provider_empty_response")
        assert self._settings.hy3_api_key is not None
        raw_text = redact_sensitive_text(
            content,
            known_secrets=(self._settings.hy3_api_key,),
        )
        usage = response.usage

        def token_count(name: str) -> int | None:
            value = getattr(usage, name, None) if usage is not None else None
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        return ProviderCallResult(
            raw_text=raw_text,
            prompt_tokens=token_count("prompt_tokens"),
            completion_tokens=token_count("completion_tokens"),
        )

    async def aclose(self) -> None:
        await self._client.close()


def _validate_output_target(*, run_id: str, output_dir: str | Path, resume: bool) -> None:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise Phase3RunnerError(
            "run_id is not a safe directory identifier",
            safe_stage="P3E_OUTPUT",
        )
    root = Path(output_dir)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Phase3RunnerError("phase-three output root is unsafe", safe_stage="P3E_OUTPUT")
    run_dir = root / run_id
    if not resume:
        if run_dir.exists() or run_dir.is_symlink():
            raise Phase3RunnerError(
                "phase-three run directory already exists",
                safe_stage="P3E_OUTPUT",
            )
        return
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise Phase3RunnerError(
            "resume run directory is missing or unsafe",
            safe_stage="P3E_RESUME",
        )
    _payload, value = _read_manifest(run_dir / "manifest.json", label="phase-three run manifest")
    try:
        manifest = Phase3RunManifest.model_validate(value)
    except ValidationError:
        raise Phase3RunnerError(
            "phase-three resume manifest failed schema validation",
            safe_stage="P3E_RESUME",
        ) from None
    if manifest.run_id != run_id or manifest.status != "running":
        raise Phase3RunnerError(
            "only an incomplete matching run may be resumed",
            safe_stage="P3E_RESUME",
        )


def _build_resume_identity(
    *,
    loaded: LoadedPhase3Materials,
    specs: Sequence[Any],
    bindings: Phase3ExecutionBindings,
    annotation_protocol_sha256: str,
    output_dir: str | Path,
    allow_dirty: bool,
    random_seed: int,
) -> Phase3ResumeIdentity:
    git = _git_metadata(Path.cwd(), excluded_paths=(Path(output_dir),))
    commit = git.get("commit")
    branch = git.get("branch")
    dirty = git.get("dirty")
    fingerprint = git.get("working_tree_sha256")
    if (
        git.get("available") is not True
        or not isinstance(commit, str)
        or not _GIT_COMMIT_PATTERN.fullmatch(commit)
        or not isinstance(branch, str)
        or not branch.strip()
        or not isinstance(dirty, bool)
    ):
        raise Phase3RunnerError(
            "Git execution identity is unavailable",
            safe_stage="P3E_GIT_IDENTITY",
        )
    if dirty and not allow_dirty:
        raise Phase3RunnerError(
            "formal paired evaluation requires a clean worktree",
            safe_stage="P3E_GIT_DIRTY",
        )
    if dirty and (not isinstance(fingerprint, str) or not _SHA256_PATTERN.fullmatch(fingerprint)):
        raise Phase3RunnerError(
            "dirty worktree fingerprint is unavailable",
            safe_stage="P3E_GIT_IDENTITY",
        )
    return Phase3ResumeIdentity(
        frozen_manifest_sha256=loaded.cohort.overlay_manifest_sha256,
        natural_manifest_sha256=loaded.cohort.natural_manifest_sha256,
        ordered_trace_ids_sha256=canonical_sha256(loaded.cohort.ordered_trace_ids),
        material_payloads_sha256=loaded.material_payloads_sha256,
        method_specs_sha256=method_specs_sha256(specs),
        prompt_bundle_sha256=prompt_bundle_sha256(),
        output_schema_sha256=output_schema_sha256(),
        implementation_sha256=implementation_sha256(),
        provider_config_sha256=bindings.provider_config_sha256,
        annotation_set_manifest_sha256=bindings.annotation_set_manifest_sha256,
        completed_labels_sha256=bindings.completed_labels_sha256,
        annotation_records_sha256=bindings.annotation_records_sha256,
        git_commit=commit,
        git_branch=branch,
        git_dirty=dirty,
        git_worktree_fingerprint=fingerprint if dirty else None,
        python_version=platform.python_version(),
        direct_dependencies_sha256=canonical_sha256(_dependency_versions()),
        ast_implementation_sha256=ast_implementation_sha256(),
        public_evidence_policy_sha256=public_evidence_policy_sha256(),
        annotation_protocol_sha256=annotation_protocol_sha256,
        random_seed=random_seed,
    )


def _prepare_phase3_evaluation(
    *,
    run_id: str,
    cohort_manifest_path: str | Path,
    natural_manifest_path: str | Path,
    annotation_set_manifest_path: str | Path,
    expected_annotation_set_manifest_sha256: str,
    phase1_run_dir: str | Path,
    phase2_run_dir: str | Path,
    dataset_manifest_path: str | Path,
    source_bundle_path: str | Path,
    execution_run_dir: str | Path,
    protocol_path: str | Path,
    guide_path: str | Path,
    provider: str,
    model: str,
    temperature: float,
    timeout_seconds: float,
    output_dir: str | Path,
    resume: bool,
    allow_dirty: bool,
    random_seed: int,
    settings: Settings | None,
    privacy_canaries: Sequence[str | bytes],
) -> _PreparedPhase3Evaluation:
    if provider != "hy3":
        raise Phase3RunnerError(
            "formal paired evaluation supports only the explicit Hy3 provider",
            safe_stage="P3E_PROVIDER_CONFIG",
        )
    if random_seed != PHASE3_EVALUATION_RANDOM_SEED:
        raise Phase3RunnerError(
            "phase-three evaluation random seed differs from the frozen value",
            safe_stage="P3E_EXECUTION_IDENTITY",
        )
    _validate_output_target(run_id=run_id, output_dir=output_dir, resume=resume)
    loaded = load_phase3_materials(
        cohort_manifest_path=cohort_manifest_path,
        natural_manifest_path=natural_manifest_path,
        phase1_run_dir=phase1_run_dir,
        phase2_run_dir=phase2_run_dir,
        dataset_manifest_path=dataset_manifest_path,
        source_bundle_path=source_bundle_path,
        execution_run_dir=execution_run_dir,
        privacy_canaries=privacy_canaries,
    )
    annotation = load_annotation_set_binding(
        manifest_path=annotation_set_manifest_path,
        expected_manifest_sha256=expected_annotation_set_manifest_sha256,
        frozen_manifest_sha256=loaded.cohort.overlay_manifest_sha256,
        ordered_trace_ids=loaded.cohort.ordered_trace_ids,
        natural_trace_count=loaded.cohort.natural_trace_count,
        counterfactual_trace_count=loaded.cohort.counterfactual_trace_count,
        protocol_path=protocol_path,
        guide_path=guide_path,
    )
    configured = settings or get_settings()
    provider_configuration = _hy3_public_configuration(configured, model=model)
    provider_sha256 = canonical_sha256(provider_configuration)
    specs = build_method_specs(
        provider=provider,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )
    bindings = Phase3ExecutionBindings(
        natural_manifest_sha256=loaded.cohort.natural_manifest_sha256,
        material_payloads_sha256=loaded.material_payloads_sha256,
        provider_config_sha256=provider_sha256,
        annotation_set_manifest_sha256=annotation.manifest_sha256,
        completed_labels_sha256=annotation.manifest.completed_labels_sha256,
        annotation_records_sha256=annotation.manifest.annotation_records_sha256,
    )
    identity = _build_resume_identity(
        loaded=loaded,
        specs=specs,
        bindings=bindings,
        annotation_protocol_sha256=annotation.manifest.annotation_protocol_sha256,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
        random_seed=random_seed,
    )
    trace_count = len(loaded.cohort.ordered_trace_ids)
    provider_pair_count = trace_count * sum(spec.uses_llm for spec in specs)
    preflight = Phase3EvaluationPreflight(
        run_id=run_id,
        resume=resume,
        annotation_set_id=annotation.manifest.annotation_set_id,
        natural_trace_count=loaded.cohort.natural_trace_count,
        counterfactual_trace_count=loaded.cohort.counterfactual_trace_count,
        trace_count=trace_count,
        method_count=len(specs),
        pair_count=trace_count * len(specs),
        provider_pair_count=provider_pair_count,
        maximum_provider_call_count=provider_pair_count * 2,
        method_specs_sha256=method_specs_sha256(specs),
        prompt_bundle_sha256=prompt_bundle_sha256(),
        output_schema_sha256=output_schema_sha256(),
        material_payloads_sha256=loaded.material_payloads_sha256,
        provider_config_sha256=provider_sha256,
        annotation_set_manifest_sha256=annotation.manifest_sha256,
        completed_labels_sha256=annotation.manifest.completed_labels_sha256,
        annotation_records_sha256=annotation.manifest.annotation_records_sha256,
        resume_identity_sha256=canonical_sha256(identity),
        provider=provider,
        model=model,
        git_commit=identity.git_commit,
        git_branch=identity.git_branch,
        git_dirty=identity.git_dirty,
    )
    return _PreparedPhase3Evaluation(
        loaded=loaded,
        specs=specs,
        bindings=bindings,
        identity=identity,
        provider_configuration=provider_configuration,
        settings=configured,
        preflight=preflight,
    )


def preflight_phase3_evaluation(**kwargs: Any) -> Phase3EvaluationPreflight:
    """Validate the complete Gate-E3 identity without writing or calling Hy3."""

    return _prepare_phase3_evaluation(**kwargs).preflight


async def execute_phase3_evaluation(
    *,
    confirm_real_provider: bool,
    **kwargs: Any,
) -> Phase3EvaluationResult:
    """Execute the exact five-method product only after explicit confirmation."""

    if not confirm_real_provider:
        raise Phase3RunnerError(
            "real Provider execution requires explicit confirmation",
            safe_stage="P3E_REAL_PROVIDER_CONFIRMATION",
        )
    prepared = _prepare_phase3_evaluation(**kwargs)
    judge = Phase3Hy3JudgeProvider(prepared.settings, model=prepared.preflight.model)
    if provider_config_sha256(judge) != prepared.bindings.provider_config_sha256:
        await judge.aclose()
        raise Phase3RunnerError(
            "runtime Provider configuration differs from preflight",
            safe_stage="P3E_PROVIDER_IDENTITY",
        )
    try:
        run = await run_paired_evaluation(
            run_id=prepared.preflight.run_id,
            cohort=prepared.loaded.cohort,
            materials=prepared.loaded.materials,
            method_specs=prepared.specs,
            provider=judge,
            resume_identity=prepared.identity,
            execution_bindings=prepared.bindings,
            output_dir=kwargs["output_dir"],
            resume=kwargs["resume"],
            privacy_canaries=kwargs.get("privacy_canaries", ()),
        )
    finally:
        await judge.aclose()
    return Phase3EvaluationResult(preflight=prepared.preflight, run=run)


__all__ = [
    "LoadedAnnotationSetBinding",
    "PHASE3_EVALUATION_RANDOM_SEED",
    "Phase3EvaluationPreflight",
    "Phase3EvaluationResult",
    "Phase3Hy3JudgeProvider",
    "execute_phase3_evaluation",
    "load_annotation_set_binding",
    "preflight_phase3_evaluation",
]
