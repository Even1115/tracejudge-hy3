"""Typer CLI: doctor / baseline / demo / run / batch."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from tracejudge_hy3.baseline import (
    BaselineExperimentError,
    new_baseline_run_id,
    run_baseline_experiment,
)
from tracejudge_hy3.config import get_settings
from tracejudge_hy3.dataset.humanevalplus import (
    DATASET_SOURCE as HUMANEVALPLUS_DATASET_SOURCE,
)
from tracejudge_hy3.dataset.humanevalplus import (
    convert_humanevalplus,
    sample_humanevalplus,
    validate_problem_dataset,
)
from tracejudge_hy3.dataset.loader import load_problem_by_id, load_problems
from tracejudge_hy3.evalplus import (
    DockerLimits,
    EvalPlusDockerRunner,
    EvalPlusExperimentError,
    EvalPlusExportError,
    MockEvalPlusExecutor,
    new_evalplus_run_id,
    run_evalplus_experiment,
)
from tracejudge_hy3.exceptions import DatasetError, TraceJudgeError
from tracejudge_hy3.phase3 import (
    ANNOTATION_GUIDE_RELATIVE_PATH,
    ANNOTATION_PROTOCOL_RELATIVE_PATH,
    PHASE3_EVALUATION_RANDOM_SEED,
    PUBLIC_CERTIFICATE_CLAIMS_RELATIVE_PATH,
    PUBLIC_COUNTERFACTUAL_SOURCE_RELATIVE_PATH,
    Phase3AnnotationError,
    Phase3FreezeError,
    Phase3PublicEvidenceError,
    Phase3ReportError,
    Phase3RunnerError,
    Phase3StatisticsError,
    check_annotation_labels,
    execute_phase3_evaluation,
    execute_public_counterfactual_evidence,
    export_annotation_packet,
    freeze_annotation_labels,
    freeze_counterfactual_cohort,
    freeze_natural_cohort,
    generate_phase3_report,
    generate_phase3_statistics,
    generate_public_certificates,
    preflight_annotation_labels_freeze,
    preflight_annotation_packet,
    preflight_counterfactual_freeze,
    preflight_natural_cohort,
    preflight_paired_interface,
    preflight_phase3_evaluation,
    preflight_phase3_report,
    preflight_phase3_statistics,
    preflight_public_certificates,
    preflight_public_counterfactual_source,
    replay_public_certificate,
)
from tracejudge_hy3.phase4 import (
    P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
    P1_ADJUDICATION_DEFAULT_MANIFEST,
    P1_ADJUDICATION_DEFAULT_OUTPUT,
    P1_AGREEMENT_DEFAULT_MANIFEST,
    P1_AGREEMENT_DEFAULT_OUTPUT,
    P1_ARRANGEMENT_RELATIVE_PATH,
    P1_COORDINATOR_REFERENCE_DEFAULT_PATH,
    P1_DELIVERY_RECORD_DEFAULT_PATH,
    P1_DELIVERY_SCHEMA_RELATIVE_PATH,
    P1_FORMAL_LABELS_DEFAULT_MANIFEST,
    P1_FORMAL_LABELS_DEFAULT_OUTPUT,
    P1_FORMAL_PACKET_DEFAULT_DIR,
    P1_FORMAL_PACKET_DEFAULT_OUTPUT,
    P1_FORMAL_PACKET_ID,
    P1_FORMAL_PACKET_MANIFEST_SHA256,
    P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH,
    P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH,
    P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT,
    P1_PRACTICE_ADMISSION_DEFAULT_PATH,
    P1_PRACTICE_ID,
    P1_PRACTICE_SOURCE_RELATIVE_PATH,
    P1_PRIMARY_LABELS_DEFAULT_MANIFEST,
    P1_PROTOCOL_RELATIVE_PATH,
    P1InterRaterAgreementAnalysis,
    P1PostAdjudicationSensitivityError,
    Phase4P1AnnotationError,
    Phase4ReleaseError,
    Phase4ReproducibilityError,
    Phase4StabilityError,
    Phase4StabilitySensitivityError,
    complete_p1_consensus_adjudication,
    create_p1_delivery_record_template,
    execute_hy3_judge_stability,
    freeze_artifact_inventory,
    freeze_p1_formal_labels,
    freeze_p1_formal_subset,
    initialize_p1_adjudication,
    preflight_artifact_inventory,
    preflight_hy3_judge_stability,
    preflight_p1_adjudication,
    preflight_p1_agreement,
    preflight_p1_delivery_record,
    preflight_p1_formal_labels,
    preflight_p1_formal_packet,
    preflight_p1_formal_subset,
    preflight_p1_practice_bundle,
    prepare_public_charts,
    prepare_public_replay_receipt,
    publish_p1_agreement,
    publish_p1_post_adjudication_sensitivity,
    publish_stability_sensitivity_release,
    verify_artifact_inventory,
    verify_p1_adjudication,
    verify_p1_agreement,
    verify_p1_completed_adjudication,
    verify_p1_formal_labels,
    verify_p1_formal_packet,
    verify_p1_formal_subset,
    verify_p1_post_adjudication_sensitivity,
    verify_p1_practice_bundle,
    verify_public_charts,
    write_p1_formal_packet,
    write_p1_practice_admission,
    write_p1_practice_bundle,
    write_public_charts,
    write_public_replay_receipt,
)
from tracejudge_hy3.pipeline.runner import PipelineResult, run_pipeline, select_backend
from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.reporting.serializer import (
    pipeline_result_to_dict,
    save_result_json,
    timestamped_artifact_path,
)
from tracejudge_hy3.resources import data_path
from tracejudge_hy3.sandbox.base import SandboxBackend
from tracejudge_hy3.sandbox.docker_backend import DockerSandbox
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox
from tracejudge_hy3.schemas.problem import ProblemSpec

app = typer.Typer(
    add_completion=False, help="TraceJudge-Hy3: 需求-推理-代码-执行证据四层对齐评估系统 (v0.1)"
)
dataset_app = typer.Typer(
    add_completion=False,
    help="离线数据集转换、确定性抽样与 ProblemSpec 校验（不执行数据集代码）",
)
phase3_app = typer.Typer(
    add_completion=False,
    help="阶段三离线冻结、配对评估与公开证据工具（按研究门槛逐步开放）",
)
phase4_app = typer.Typer(
    add_completion=False,
    help="阶段四复现、公开发布与 P1 研究增强工具",
)
app.add_typer(dataset_app, name="dataset")
app.add_typer(phase3_app, name="phase3")
app.add_typer(phase4_app, name="phase4")
console = Console()

DEFAULT_DATASET = str(data_path("sample_problems.jsonl"))
DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE = str(data_path(PUBLIC_COUNTERFACTUAL_SOURCE_RELATIVE_PATH))
DEFAULT_PHASE3_CERTIFICATE_CLAIMS = str(data_path(PUBLIC_CERTIFICATE_CLAIMS_RELATIVE_PATH))
DEFAULT_PHASE3_ANNOTATION_PROTOCOL = str(data_path(ANNOTATION_PROTOCOL_RELATIVE_PATH))
DEFAULT_PHASE3_ANNOTATION_GUIDE = ANNOTATION_GUIDE_RELATIVE_PATH
DEFAULT_PHASE4_CERTIFICATE = (
    "artifacts/experiments/phase3-public-certificates/"
    "phase3_gate_d_public_certificates_v1/certificates/certificate_001.json"
)
DEFAULT_PHASE4_CERTIFICATE_MANIFEST = (
    "artifacts/experiments/phase3-public-certificates/"
    "phase3_gate_d_public_certificates_v1/manifest.json"
)
DEFAULT_PHASE4_COHORT_MANIFEST = (
    "artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json"
)
DEFAULT_PHASE4_NATURAL_MANIFEST = (
    "artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json"
)
DEFAULT_PHASE4_SOURCE_BUNDLE = f"data/{PUBLIC_COUNTERFACTUAL_SOURCE_RELATIVE_PATH}"
DEFAULT_PHASE4_STATISTICS_MANIFEST = (
    "artifacts/experiments/phase3-statistics/phase3_stats_primary_round1_v1/manifest.json"
)
DEFAULT_PHASE4_STATISTICS_REPORT = (
    "artifacts/experiments/phase3-statistics/phase3_stats_primary_round1_v1/report.json"
)
DEFAULT_PHASE4_STATISTICS_MANIFEST_SHA256 = (
    "7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8"
)
DEFAULT_PHASE4_STATISTICS_REPORT_SHA256 = (
    "972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10"
)
DEFAULT_PHASE4_P1_ARRANGEMENT = P1_ARRANGEMENT_RELATIVE_PATH
DEFAULT_PHASE4_P1_PROTOCOL = P1_PROTOCOL_RELATIVE_PATH
DEFAULT_PHASE4_P1_PRACTICE_SOURCE = P1_PRACTICE_SOURCE_RELATIVE_PATH
DEFAULT_PHASE4_P1_COORDINATOR_REFERENCE = P1_COORDINATOR_REFERENCE_DEFAULT_PATH
DEFAULT_PHASE4_P1_PRACTICE_OUTPUT = "docs/experiments/phase4_p1_practice"
DEFAULT_PHASE4_P1_DELIVERY_SCHEMA = P1_DELIVERY_SCHEMA_RELATIVE_PATH
DEFAULT_PHASE4_P1_DELIVERY_RECORD = P1_DELIVERY_RECORD_DEFAULT_PATH
DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST = P1_FORMAL_PRIVATE_MANIFEST_DEFAULT_PATH
DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT = P1_FORMAL_PUBLIC_COMMITMENT_DEFAULT_PATH
DEFAULT_PHASE4_P1_PRACTICE_ADMISSION = P1_PRACTICE_ADMISSION_DEFAULT_PATH
DEFAULT_PHASE4_P1_FORMAL_PACKET_OUTPUT = P1_FORMAL_PACKET_DEFAULT_OUTPUT
DEFAULT_PHASE4_P1_PHASE1_RUN = (
    "artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5"
)
DEFAULT_PHASE4_P1_PHASE2_RUN = (
    "artifacts/experiments/phase2-research-natural/phase2_20260827T081939637435Z_3c366f64fc19"
)
DEFAULT_PHASE4_P1_DATASET_MANIFEST = (
    "artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json"
)
DEFAULT_PHASE4_P1_EXECUTION_RUN = (
    "artifacts/experiments/phase3-public-evidence/phase3_cf_public_15_v1"
)
DEFAULT_PHASE4_STABILITY_OUTPUT = "artifacts/experiments/phase4-judge-stability"
DEFAULT_PHASE4_STABILITY_RUN = (
    f"{DEFAULT_PHASE4_STABILITY_OUTPUT}/phase4_stability_hy3_public4x5_v1"
)
DEFAULT_PHASE4_STABILITY_RELEASE_OUTPUT = "docs/releases/phase4"


def _reject_phase1_projection_execution(problems: list[ProblemSpec]) -> None:
    if any(problem.source == HUMANEVALPLUS_DATASET_SOURCE for problem in problems):
        raise DatasetError(
            "HumanEval+ 公共投影阶段一仅支持 baseline，不能使用 run/batch；"
            "请对已完成的阶段一产物使用独立的 `tracejudge evalplus`"
        )


def _make_provider(provider_name: str, case: str | None = None) -> LLMProvider:
    if provider_name == "mock":
        return MockProvider(case=case)
    if provider_name == "hy3":
        return Hy3OpenAIProvider()
    raise typer.BadParameter(f"unknown provider: {provider_name!r} (expected 'mock' or 'hy3')")


async def _run_and_close_provider(
    problem: ProblemSpec,
    provider: LLMProvider,
    backend: SandboxBackend,
) -> PipelineResult:
    try:
        return await run_pipeline(problem, provider, backend)
    finally:
        await provider.aclose()


async def _run_batch_in_one_loop(
    problems: list[ProblemSpec],
    provider: LLMProvider,
    backend: SandboxBackend,
) -> list[tuple[ProblemSpec, PipelineResult | None, TraceJudgeError | None]]:
    """Keep one event loop for an async provider client across the whole batch."""

    outcomes: list[tuple[ProblemSpec, PipelineResult | None, TraceJudgeError | None]] = []
    try:
        for problem in problems:
            try:
                result = await run_pipeline(problem, provider, backend)
            except TraceJudgeError as exc:
                outcomes.append((problem, None, exc))
            else:
                outcomes.append((problem, result, None))
    finally:
        await provider.aclose()
    return outcomes


@app.command()
def doctor() -> None:
    """环境检查：Python 版本、Hy3 配置、Docker 可用性、示例数据、输出目录可写性。不会打印真实 API Key。"""

    settings = get_settings()
    table = Table(title="TraceJudge-Hy3 环境检查")
    table.add_column("检查项")
    table.add_column("状态")
    table.add_column("说明")

    py_ver = sys.version.split()[0]
    py_ok = sys.version_info >= (3, 11)
    table.add_row("Python 版本", "OK" if py_ok else "FAIL", f"{py_ver}（需要 >= 3.11）")

    hy3_ok = settings.hy3_configured()
    table.add_row(
        "Hy3 环境变量",
        "OK" if hy3_ok else "未配置",
        "base_url/model/api_key 均已设置"
        if hy3_ok
        else "缺少 HY3_BASE_URL / HY3_API_KEY / HY3_MODEL 之一，--provider hy3 不可用",
    )

    docker_backend = DockerSandbox(image=settings.tracejudge_docker_image)
    docker_available, docker_reason = docker_backend.is_available()
    table.add_row(
        "Docker 可用性",
        "OK" if docker_available else "不可用",
        docker_reason or f"image={settings.tracejudge_docker_image}",
    )

    dataset_path = Path(DEFAULT_DATASET)
    dataset_ok = dataset_path.exists()
    dataset_note = "未找到文件"
    if dataset_ok:
        try:
            problems = load_problems(dataset_path)
            dataset_note = f"共 {len(problems)} 道题"
        except TraceJudgeError as exc:
            dataset_ok = False
            dataset_note = f"读取失败：{exc}"
    table.add_row("示例数据", "OK" if dataset_ok else "FAIL", dataset_note)

    artifact_dir = settings.artifact_path
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        probe = artifact_dir / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        artifact_ok, artifact_note = True, str(artifact_dir)
    except OSError as exc:
        artifact_ok, artifact_note = False, str(exc)
    table.add_row("输出目录可写", "OK" if artifact_ok else "FAIL", artifact_note)

    console.print(table)

    if not docker_available:
        console.print(
            "[yellow]提示：Docker 不可用时，真实 Hy3 生成代码默认无法执行，"
            "除非显式传入 --sandbox trusted-local --allow-unsafe-local-exec（不推荐）。"
            "`tracejudge demo --mock` 仍可在 TrustedLocalSandbox 下正常运行内置可信 Fixture。[/yellow]"
        )
    if not hy3_ok:
        console.print(
            "[yellow]提示：未检测到完整的 Hy3 环境变量配置，--provider hy3 暂不可用；"
            "可使用 --provider mock 运行完整链路。[/yellow]"
        )


@dataset_app.command("convert-humanevalplus")
def dataset_convert_humanevalplus(
    input_path: str = typer.Option(
        "artifacts/datasets/raw/humanevalplus/test.jsonl",
        "--input",
        help="固定 HumanEval+ Hugging Face JSONL 快照",
    ),
    revision: str = typer.Option(..., "--revision", help="固定的完整 Hugging Face commit SHA"),
    source_manifest: str = typer.Option(
        ...,
        "--manifest",
        help="记录官方 revision、许可证和原始文件 SHA256 的受控 manifest",
    ),
    output_dir: str = typer.Option(..., "--output-dir", help="原子发布的公共投影目录"),
) -> None:
    """将完整 HumanEval+ 快照转换为阶段一公开投影；不执行或复制答案/测试。"""

    try:
        result = convert_humanevalplus(
            input_path=input_path,
            revision=revision,
            source_manifest_path=source_manifest,
            output_dir=output_dir,
        )
    except (TraceJudgeError, OSError) as exc:
        console.print(f"[red]HumanEval+ 转换失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="HumanEval+ 阶段一公共投影")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("原始题目", str(result.record_count))
    table.add_row("公开投影 SHA256", result.dataset_sha256)
    table.add_row("Bundle manifest SHA256", result.manifest_sha256)
    table.add_row("执行数据集代码", "否")
    table.add_row("复制 canonical_solution/test", "否")
    console.print(table)
    console.print(f"[dim]dataset: {result.dataset_path}[/dim]")
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")


@dataset_app.command("sample")
def dataset_sample(
    dataset: str = typer.Option(..., "--dataset", help="完整的 ProblemSpec JSONL 公共投影"),
    source_manifest: str = typer.Option(
        ..., "--manifest", help="完整公共投影的 dataset_manifest.json"
    ),
    count: int = typer.Option(10, "--count", min=1, help="确定性抽样题数"),
    seed: int = typer.Option(20260824, "--seed", help="只与公开 problem_id 组合使用的固定种子"),
    output_dir: str = typer.Option(..., "--output-dir", help="原子发布的 dataset bundle 目录"),
    exclude_manifest: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--exclude-manifest",
        help="要排除的 v1 子集 manifest（可重复）；至少支持固定 Pilot manifest",
    ),
    selection_role: str = typer.Option(
        "pilot",
        "--selection-role",
        help="选择角色：pilot 或 research_natural",
    ),
) -> None:
    """仅依据公开 problem_id 生成确定性的 Pilot 或研究子集。"""

    try:
        result = sample_humanevalplus(
            dataset_path=dataset,
            source_manifest_path=source_manifest,
            count=count,
            seed=seed,
            output_dir=output_dir,
            exclude_manifests=exclude_manifest,
            selection_role=selection_role,
        )
    except (TraceJudgeError, OSError) as exc:
        console.print(f"[red]数据集抽样失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    title = (
        "HumanEval+ 正式自然研究子集"
        if selection_role == "research_natural"
        else "HumanEval+ 确定性 Pilot 子集"
    )
    table = Table(title=title)
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("题目数", str(len(result.selected_problem_ids)))
    table.add_row("公开投影 SHA256", result.dataset_sha256)
    table.add_row("Bundle manifest SHA256", result.manifest_sha256)
    table.add_row("problem_id", ", ".join(result.selected_problem_ids))
    console.print(table)
    console.print(f"[dim]dataset: {result.dataset_path}[/dim]")
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    if selection_role == "research_natural":
        console.print(
            "[yellow]该子集是阶段三正式自然研究 source cohort，"
            "不代表完整 HumanEval+ 功能分数或正式 benchmark 排名。[/yellow]"
        )
    else:
        console.print("[yellow]该子集仅用于生成与解析 Pilot，不代表 HumanEval+ 功能分数。[/yellow]")


@dataset_app.command("validate")
def dataset_validate(
    dataset: str = typer.Option(..., "--dataset", help="待校验的 ProblemSpec JSONL"),
) -> None:
    """离线校验 ProblemSpec JSONL；不调用 Provider、不执行候选或测试代码。"""

    try:
        result = validate_problem_dataset(dataset)
    except (TraceJudgeError, OSError) as exc:
        console.print(f"[red]数据集校验失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="ProblemSpec 数据集校验")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("题目数", str(result.problem_count))
    table.add_row("SHA256", result.dataset_sha256)
    table.add_row("来源", ", ".join(result.sources))
    table.add_row("难度标签", ", ".join(result.difficulties))
    table.add_row("执行代码/测试", "否")
    console.print(table)


@app.command()
def baseline(
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset", help="ProblemSpec JSONL 数据集"),
    dataset_manifest: str | None = typer.Option(
        None,
        "--dataset-manifest",
        help="可选：与数据集哈希绑定的公开 provenance manifest（HumanEval+ Pilot 必填）",
    ),
    provider: str = typer.Option("mock", "--provider", help="'mock' 或 'hy3'"),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase1",
        "--output-dir",
        help="运行目录的父目录；每次新运行会在其下创建唯一 run_id 子目录",
    ),
    resume_run_id: str | None = typer.Option(
        None,
        "--resume-run-id",
        help="续跑既有 run_id：跳过已成功题目，并重试此前失败或未完成题目",
    ),
) -> None:
    """阶段一：仅生成并原子保存基线解答，不执行候选代码或任何测试/评估。"""

    try:
        effective_run_id = resume_run_id or new_baseline_run_id()
        run_path = Path(output_dir).expanduser().resolve() / effective_run_id
        action = "续跑" if resume_run_id is not None else "新建"
        # Print recovery coordinates before the first model call so an
        # interrupted invocation can always be resumed without directory
        # discovery or log inspection.
        console.print(f"[cyan]阶段一 {action} run_id: {effective_run_id}[/cyan]")
        console.print(f"[dim]产物目录: {run_path}[/dim]")
        llm_provider = _make_provider(provider)
        result = asyncio.run(
            run_baseline_experiment(
                dataset_path=dataset,
                provider=llm_provider,
                output_dir=output_dir,
                run_id=effective_run_id,
                resume=resume_run_id is not None,
                dataset_manifest_path=dataset_manifest,
            )
        )
    except (BaselineExperimentError, TraceJudgeError, OSError) as exc:
        console.print(f"[red]基线生成失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    summary = result.summary
    parse_rate = summary.get("parse_success_rate")
    parse_rate_text = "N/A" if parse_rate is None else f"{parse_rate:.2%}"
    average_duration = summary.get("average_duration_seconds")
    average_duration_text = "N/A" if average_duration is None else f"{average_duration:.3f}s"

    table = Table(title=f"阶段一基线生成：{result.run_id}")
    table.add_column("统计")
    table.add_column("结果")
    table.add_row("题目总数", str(summary["total_problem_count"]))
    table.add_row("成功", str(summary["success_count"]))
    table.add_row("解析失败", str(summary["parse_error_count"]))
    table.add_row("Provider 失败", str(summary["provider_error_count"]))
    table.add_row("本次跳过", str(summary["skipped_count"]))
    table.add_row("解析成功率", parse_rate_text)
    table.add_row("平均耗时（跳过项除外）", average_duration_text)
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]responses: {result.responses_path}[/dim]")
    console.print(f"[dim]summary: {result.summary_path}[/dim]")

    if summary["failure_count"]:
        raise typer.Exit(code=1)


@app.command("evalplus")
def evalplus_command(
    baseline_run: str = typer.Option(
        ...,
        "--baseline-run",
        help="已完成的阶段一 HumanEval+ run 目录",
    ),
    dataset_manifest: str = typer.Option(
        ...,
        "--dataset-manifest",
        help="与阶段一绑定的 dataset_manifest.json",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase2",
        "--output-dir",
        help="仓库内且被 .gitignore 覆盖的阶段二运行目录父目录",
    ),
    executor: str = typer.Option(
        "docker",
        "--executor",
        help="'docker' 执行官方 EvalPlus，'mock' 仅验证产物链路且不执行候选代码",
    ),
    resume_run_id: str | None = typer.Option(
        None,
        "--resume-run-id",
        help="续跑既有 run_id；输入、provenance、镜像或限制变化时会拒绝",
    ),
    selection_policy: str = typer.Option(
        "all",
        "--selection-policy",
        help="'all' 要求每个数据集题目都有阶段一成功记录；"
        "'phase1-success-only' 仅导出成功解析的题目",
    ),
    min_success_count: int = typer.Option(
        30,
        "--min-success-count",
        min=1,
        help="--selection-policy 为 phase1-success-only 时要求的最少成功题数",
    ),
    parallel: int = typer.Option(
        2,
        "--parallel",
        min=1,
        max=16,
        help="宿主同时运行的单题容器数（官方容器内 parallel 固定为 1）",
    ),
    per_task_timeout: float = typer.Option(
        180.0,
        "--per-task-timeout",
        min=1.0,
        help="每题容器的外层超时秒数",
    ),
    batch_timeout: float = typer.Option(
        900.0,
        "--batch-timeout",
        min=1.0,
        help="整批调度超时秒数",
    ),
) -> None:
    """阶段二：从阶段一安全导出代码，使用隔离的官方 EvalPlus 执行器评测。"""

    if executor not in {"docker", "mock"}:
        raise typer.BadParameter("--executor 必须是 'docker' 或 'mock'")
    if selection_policy not in {"all", "phase1-success-only"}:
        raise typer.BadParameter("--selection-policy 必须是 'all' 或 'phase1-success-only'")
    if batch_timeout < per_task_timeout:
        raise typer.BadParameter("--batch-timeout 不能小于 --per-task-timeout")
    if resume_run_id is not None and not resume_run_id.strip():
        raise typer.BadParameter("--resume-run-id 不能为空")

    effective_run_id = resume_run_id or new_evalplus_run_id()
    run_path = Path(output_dir).expanduser().resolve() / effective_run_id
    action = "续跑" if resume_run_id is not None else "新建"
    console.print(f"[cyan]阶段二 {action} run_id: {effective_run_id}[/cyan]")
    console.print(f"[dim]产物目录: {run_path}[/dim]")

    selected_executor = (
        MockEvalPlusExecutor()
        if executor == "mock"
        else EvalPlusDockerRunner(limits=DockerLimits(per_task_timeout_seconds=per_task_timeout))
    )
    try:
        result = run_evalplus_experiment(
            baseline_run_dir=baseline_run,
            dataset_manifest_path=dataset_manifest,
            output_dir=output_dir,
            executor=selected_executor,
            run_id=effective_run_id,
            resume=resume_run_id is not None,
            max_workers=parallel,
            per_task_timeout_seconds=per_task_timeout,
            batch_timeout_seconds=batch_timeout,
            selection_policy=selection_policy,
            min_success_count=min_success_count,
        )
    except (EvalPlusExportError, EvalPlusExperimentError, OSError, ValueError) as exc:
        # EvalPlus failures may originate while handling candidate source or
        # evaluation-only data.  Never echo exception text to the terminal;
        # public artifacts and bounded allowlisted logs carry safe diagnostics.
        console.print(
            "[red]阶段二 EvalPlus 运行失败；为避免泄露候选或隐藏测试，未输出原始异常详情。[/red]"
        )
        raise typer.Exit(code=1) from exc

    summary = result.summary
    actual = int(summary["actual_execution_count"])
    base_rate = summary.get("base_pass_rate")
    plus_rate = summary.get("base_plus_pass_rate")
    average_duration = summary.get("average_duration_seconds")
    source_problem_count = int(summary.get("source_problem_count", summary["total_problem_count"]))
    exported_success_count = int(
        summary.get("exported_success_count", summary["total_problem_count"])
    )
    excluded_parse_error_count = int(summary.get("excluded_parse_error_count", 0))
    excluded_provider_error_count = int(summary.get("excluded_provider_error_count", 0))

    def rate(value: object) -> str:
        return "N/A" if value is None else f"{float(value):.2%}"

    duration = "N/A" if average_duration is None else f"{float(average_duration):.3f}s"

    table = Table(title=f"阶段二 EvalPlus：{result.run_id}")
    table.add_column("统计")
    table.add_column("结果")
    table.add_row("阶段一来源题数", str(source_problem_count))
    table.add_row("成功导出数", str(exported_success_count))
    table.add_row("解析错误排除数", str(excluded_parse_error_count))
    table.add_row("Provider 错误排除数", str(excluded_provider_error_count))
    table.add_row("阶段二结果题数", str(summary["total_problem_count"]))
    table.add_row("实际执行数", str(actual))
    table.add_row(
        "Base 通过",
        f"{summary['base_pass_count']}/{actual} ({rate(base_rate)})" if actual else "N/A",
    )
    table.add_row(
        "Base+Extra 通过",
        f"{summary['base_plus_pass_count']}/{actual} ({rate(plus_rate)})" if actual else "N/A",
    )
    table.add_row("Timeout", str(summary["timeout_count"]))
    table.add_row(
        "错误答案/候选异常（官方状态不可细分）",
        str(summary["wrong_answer_or_candidate_exception_count"]),
    )
    table.add_row("可单独观测的 execution error", "N/A（固定 EvalPlus raw schema 不提供）")
    table.add_row("基础设施错误", str(summary["infrastructure_error_count"]))
    table.add_row("平均逐题容器耗时", duration)
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]samples: {result.samples_path}[/dim]")
    console.print(f"[dim]safe results: {result.results_path}[/dim]")
    console.print(f"[dim]summary: {result.summary_path}[/dim]")
    console.print(
        f"[yellow]本次来源 {source_problem_count} 题，成功导出 {exported_success_count} 题并进入"
        "单样本阶段二结果；不是完整 HumanEval+ 成绩或正式 benchmark 排名。[/yellow]"
    )
    if executor == "mock":
        console.print("[yellow]Mock dry run 未执行任何候选代码或官方测试。[/yellow]")
    if summary["infrastructure_error_count"]:
        raise typer.Exit(code=1)


def _render_phase3_validation_failure(exc: BaseException) -> None:
    console.print("[red]阶段三校验失败；未输出 Provider raw、候选正文或隐藏评测内容。[/red]")
    safe_stage = exc.safe_stage if isinstance(exc, Phase3FreezeError) else "P3B_UNCLASSIFIED"
    console.print(f"[yellow]安全阶段码：{safe_stage}[/yellow]")


def _render_phase3_interface_failure(exc: BaseException) -> None:
    console.print(
        "[red]阶段三配对接口校验失败；未输出 Provider raw、候选正文或隐藏评测内容。[/red]"
    )
    safe_stage = exc.safe_stage if isinstance(exc, Phase3RunnerError) else "P3C_UNCLASSIFIED"
    console.print(f"[yellow]安全阶段码：{safe_stage}[/yellow]")


def _render_phase3_public_evidence_failure(exc: BaseException) -> None:
    console.print("[red]阶段三公开证书校验失败；未输出候选正文、隐藏评测内容或原始异常。[/red]")
    safe_stage = (
        exc.safe_stage if isinstance(exc, Phase3PublicEvidenceError) else "P3D_UNCLASSIFIED"
    )
    console.print(f"[yellow]安全阶段码：{safe_stage}[/yellow]")


def _render_phase3_annotation_failure(exc: BaseException) -> None:
    console.print("[red]阶段三盲法标注包校验失败；未输出方法预测、候选正文或隐藏评测内容。[/red]")
    safe_stage = getattr(exc, "safe_stage", "P3E_UNCLASSIFIED")
    console.print(f"[yellow]安全阶段码：{safe_stage}[/yellow]")


@phase3_app.command("preflight")
def phase3_preflight(
    phase1_run: str = typer.Option(
        ...,
        "--phase1-run",
        help="已完成的 45 题 research-natural 阶段一 run 目录",
    ),
    phase2_run: str = typer.Option(
        ...,
        "--phase2-run",
        help="已完成的 research-natural 阶段二官方执行 run 目录",
    ),
    dataset_manifest: str = typer.Option(
        ...,
        "--dataset-manifest",
        help="45 题 research-natural dataset_manifest.json",
    ),
    freeze_id: str = typer.Option(
        ...,
        "--freeze-id",
        help="拟使用的冻结 ID；已有同名目录时预检失败",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-freezes",
        "--output-dir",
        help="拟使用的阶段三冻结目录父路径；预检不会创建它",
    ),
) -> None:
    """Gate B：完成全链路只读预检，但不创建目录或 manifest。"""

    try:
        result = preflight_natural_cohort(
            phase1_run_dir=phase1_run,
            phase2_run_dir=phase2_run,
            dataset_manifest_path=dataset_manifest,
            output_dir=output_dir,
            freeze_id=freeze_id,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三自然轨迹只读预检：{result.freeze_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("阶段一来源", str(result.source_problem_count))
    table.add_row("拟冻结自然轨迹", str(result.natural_trace_count))
    table.add_row("解析失败（保留来源核算）", str(result.parse_error_count))
    table.add_row("Provider 失败（保留来源核算）", str(result.provider_error_count))
    table.add_row("阶段一 run", result.phase1_run_id)
    table.add_row("阶段二 run", result.phase2_run_id)
    table.add_row("创建目录 / manifest", "否")
    table.add_row("执行 Provider / Docker", "否")
    console.print(table)
    console.print("[yellow]预检通过不等于正式冻结；本命令没有写入研究产物。[/yellow]")


@phase3_app.command("freeze")
def phase3_freeze(
    phase1_run: str = typer.Option(
        ...,
        "--phase1-run",
        help="已完成的 45 题 research-natural 阶段一 run 目录",
    ),
    phase2_run: str = typer.Option(
        ...,
        "--phase2-run",
        help="已完成的 research-natural 阶段二官方执行 run 目录",
    ),
    dataset_manifest: str = typer.Option(
        ...,
        "--dataset-manifest",
        help="45 题 research-natural dataset_manifest.json",
    ),
    freeze_id: str = typer.Option(
        ...,
        "--freeze-id",
        help="显式冻结 ID；已有同名目录时拒绝覆盖",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-freezes",
        "--output-dir",
        help="只含白名单 manifest 的阶段三冻结目录父路径",
    ),
) -> None:
    """Gate B：只读校验阶段一/二并冻结全部 research-natural 成功轨迹。"""

    try:
        result = freeze_natural_cohort(
            phase1_run_dir=phase1_run,
            phase2_run_dir=phase2_run,
            dataset_manifest_path=dataset_manifest,
            output_dir=output_dir,
            freeze_id=freeze_id,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三自然轨迹冻结：{result.freeze_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("阶段一来源", str(result.source_problem_count))
    table.add_row("冻结自然轨迹", str(result.natural_trace_count))
    table.add_row("解析失败（保留来源核算）", str(result.parse_error_count))
    table.add_row("Provider 失败（保留来源核算）", str(result.provider_error_count))
    table.add_row("公开 manifest SHA256", result.manifest_sha256)
    table.add_row("执行 Provider / Docker", "否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(
        "[yellow]该 manifest 只冻结自然轨迹；未运行五种方法，也不构成阶段三研究结果。[/yellow]"
    )


@phase3_app.command("counterfactual-preflight")
def phase3_counterfactual_preflight(
    execution_run_id: str = typer.Option(
        ...,
        "--execution-run-id",
        help="拟使用的公开 Fixture 证据 run ID；已有同名目录时拒绝",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="仓库内置且 SHA256 精确白名单化的 15 条公开反事实源 bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-public-evidence",
        "--output-dir",
        help="拟使用的公开证据目录父路径；预检不会创建它",
    ),
) -> None:
    """Gate B：只读验证 15 条公开反事实源，不执行任何候选代码。"""

    try:
        result = preflight_public_counterfactual_source(
            source_bundle_path=source_bundle,
            output_dir=output_dir,
            execution_run_id=execution_run_id,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三公开反事实源只读预检：{result.bundle_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("父 Fixture", str(result.parent_count))
    table.add_row("拟冻结反事实", str(result.counterfactual_count))
    table.add_row("五类配额", "每类 3 条")
    table.add_row("拟执行独立代码主体", str(result.execution_subject_count))
    table.add_row(
        "预期通过 / 预期失败", f"{result.expected_pass_count} / {result.expected_fail_count}"
    )
    table.add_row("公开源 SHA256", result.source_bundle_sha256)
    table.add_row("拟用证据 run", result.execution_run_id)
    table.add_row("创建目录 / 产物", "否")
    table.add_row("执行候选 / Provider / Docker", "否 / 否 / 否")
    console.print(table)
    console.print("[yellow]预检通过不等于已取得功能证据；本命令没有执行代码或写入产物。[/yellow]")


@phase3_app.command("counterfactual-execute")
def phase3_counterfactual_execute(
    execution_run_id: str = typer.Option(
        ...,
        "--execution-run-id",
        help="公开 Fixture 证据 run ID；已有同名目录时拒绝覆盖",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="仓库内置且 SHA256 精确白名单化的 15 条公开反事实源 bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-public-evidence",
        "--output-dir",
        help="公开 Fixture 执行证据目录父路径",
    ),
    per_test_timeout_seconds: float = typer.Option(
        2.0,
        "--per-test-timeout-seconds",
        min=0.1,
        max=10.0,
        help="每个公开测试的父进程强制超时；仅允许 0.1–10 秒",
    ),
) -> None:
    """Gate B：只执行精确白名单化的公开自建 Fixture 代码。"""

    try:
        result = execute_public_counterfactual_evidence(
            source_bundle_path=source_bundle,
            output_dir=output_dir,
            execution_run_id=execution_run_id,
            per_test_timeout_seconds=per_test_timeout_seconds,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三公开 Fixture 独立证据：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("独立执行主体", str(result.result_count))
    table.add_row("实际通过", str(result.pass_count))
    table.add_row("实际失败", str(result.fail_count))
    table.add_row("超时", str(result.timeout_count))
    table.add_row("基础设施错误", str(result.infrastructure_error_count))
    table.add_row("与预期影响不一致", str(result.expectation_mismatch_count))
    table.add_row("公开 results SHA256", result.results_sha256)
    table.add_row("执行 Provider / Docker", "否 / 否")
    table.add_row("受限本地公开 Fixture 执行", "是")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]results: {result.results_path}[/dim]")
    if (
        result.timeout_count
        or result.infrastructure_error_count
        or result.expectation_mismatch_count
    ):
        console.print("[red]证据 bundle 已完整保留实际结果，但不满足冻结条件；不会自动重试。[/red]")
        raise typer.Exit(code=1)
    console.print("[yellow]该命令只取得公开 Fixture 功能证据；尚未冻结反事实 overlay。[/yellow]")


@phase3_app.command("counterfactual-freeze-preflight")
def phase3_counterfactual_freeze_preflight(
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="已冻结且只含自然轨迹的阶段三 manifest.json",
    ),
    execution_run: str = typer.Option(
        ...,
        "--execution-run",
        help="已完成的公开 Fixture 独立证据 run 目录",
    ),
    freeze_id: str = typer.Option(
        ...,
        "--freeze-id",
        help="拟使用的反事实 overlay 冻结 ID",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="与执行证据完全相同的公开反事实源 bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-freezes",
        "--output-dir",
        help="拟使用的阶段三冻结目录父路径；预检不会创建它",
    ),
) -> None:
    """Gate B：只读验证自然集、反事实源和逐行证据的完整绑定。"""

    try:
        result = preflight_counterfactual_freeze(
            natural_manifest_path=natural_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            output_dir=output_dir,
            freeze_id=freeze_id,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三反事实 overlay 只读预检：{result.freeze_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("自然轨迹", str(result.natural_trace_count))
    table.add_row("反事实轨迹", str(result.counterfactual_trace_count))
    table.add_row("配对研究轨迹合计", str(result.combined_trace_count))
    table.add_row("父 Fixture（不计入分母）", str(result.parent_count))
    table.add_row("自然 freeze", result.natural_freeze_id)
    table.add_row("公开证据 run", result.evidence_run_id)
    table.add_row("创建目录 / manifest", "否")
    table.add_row("执行候选 / Provider / Docker", "否 / 否 / 否")
    console.print(table)
    console.print("[yellow]预检通过不等于正式冻结；本命令没有写入研究产物。[/yellow]")


@phase3_app.command("counterfactual-freeze")
def phase3_counterfactual_freeze(
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="已冻结且只含自然轨迹的阶段三 manifest.json",
    ),
    execution_run: str = typer.Option(
        ...,
        "--execution-run",
        help="已完成的公开 Fixture 独立证据 run 目录",
    ),
    freeze_id: str = typer.Option(
        ...,
        "--freeze-id",
        help="反事实 overlay 冻结 ID；已有同名目录时拒绝覆盖",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="与执行证据完全相同的公开反事实源 bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-freezes",
        "--output-dir",
        help="只含白名单反事实 overlay manifest 的目录父路径",
    ),
) -> None:
    """Gate B：原子冻结自然集引用、15 条反事实和各自功能证据。"""

    try:
        result = freeze_counterfactual_cohort(
            natural_manifest_path=natural_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            output_dir=output_dir,
            freeze_id=freeze_id,
        )
    except (Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_validation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三反事实 overlay 冻结：{result.freeze_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("冻结自然轨迹引用", str(result.natural_trace_count))
    table.add_row("冻结反事实轨迹", str(result.counterfactual_trace_count))
    table.add_row("五方法配对轨迹合计", str(result.combined_trace_count))
    table.add_row("父 Fixture（不计入分母）", str(result.parent_count))
    table.add_row("公开 manifest SHA256", result.manifest_sha256)
    table.add_row("本命令执行候选 / Provider / Docker", "否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(
        "[yellow]该 overlay 只完成 Gate B 研究集冻结；尚未运行五种方法，不构成阶段三结果。[/yellow]"
    )


@phase3_app.command("paired-preflight")
def phase3_paired_preflight(
    cohort_manifest: str = typer.Option(
        ...,
        "--cohort-manifest",
        help="Gate B 已冻结的自然 + 反事实 overlay manifest.json",
    ),
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="overlay 精确引用的自然轨迹 manifest.json",
    ),
    provider: str = typer.Option(
        "mock",
        "--provider",
        help="拟冻结的 Judge Provider 公开名称；预检不会连接它",
    ),
    model: str = typer.Option(
        "deterministic-phase3-mock-v1",
        "--model",
        help="拟冻结的 Judge 模型名称；预检不会连接它",
    ),
    temperature: float = typer.Option(
        0.0,
        "--temperature",
        min=0.0,
        max=2.0,
        help="四个 LLM 方法共用的拟冻结 temperature",
    ),
    timeout_seconds: float = typer.Option(
        120.0,
        "--timeout-seconds",
        min=1.0,
        max=600.0,
        help="每次 Judge 调用的拟冻结超时",
    ),
) -> None:
    """Gate C：只读验证同一 cohort 的五方法配对计划。"""

    try:
        result = preflight_paired_interface(
            overlay_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            provider=provider,
            model=model,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except (Phase3RunnerError, OSError, ValueError) as exc:
        _render_phase3_interface_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三五方法配对只读预检：{result.freeze_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("自然轨迹", str(result.natural_trace_count))
    table.add_row("反事实轨迹", str(result.counterfactual_trace_count))
    table.add_row(
        "冻结轨迹 / 方法 / 配对",
        f"{result.trace_count} / {result.method_count} / {result.pair_count}",
    )
    table.add_row("Judge Provider / 模型", f"{result.provider} / {result.model}")
    table.add_row("方法规格 SHA256", result.method_specs_sha256)
    table.add_row("Prompt bundle SHA256", result.prompt_bundle_sha256)
    table.add_row("输出 schema SHA256", result.output_schema_sha256)
    table.add_row("创建目录 / 运行方法", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该命令只校验 Gate C 接口身份；不会读取方法输入正文，也不构成方法运行结果。[/yellow]"
    )


@phase3_app.command("certificate-preflight")
def phase3_certificate_preflight(
    run_id: str = typer.Option(
        ...,
        "--run-id",
        help="拟使用的 Gate D 公开证书工程 Fixture run ID",
    ),
    cohort_manifest: str = typer.Option(
        ...,
        "--cohort-manifest",
        help="Gate B 已冻结的自然 + 反事实 overlay manifest.json",
    ),
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="overlay 精确引用的自然轨迹 manifest.json",
    ),
    execution_run: str = typer.Option(
        ...,
        "--execution-run",
        help="Gate B 已完成的公开 Fixture 独立证据 run 目录",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="SHA256 精确白名单化的公开反事实源 bundle",
    ),
    claims_bundle: str = typer.Option(
        DEFAULT_PHASE3_CERTIFICATE_CLAIMS,
        "--claims-bundle",
        help="覆盖三等级证书的公开工程 claim bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-public-certificates",
        "--output-dir",
        help="拟使用的公开证书目录父路径；预检不会创建它",
    ),
) -> None:
    """Gate D：只读验证三等级公开证书输入和哈希，不执行代码。"""

    try:
        result = preflight_public_certificates(
            run_id=run_id,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            claims_bundle_path=claims_bundle,
            output_dir=output_dir,
        )
    except (Phase3PublicEvidenceError, Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_public_evidence_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三公开证书只读预检：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("公开工程证书", str(result.certificate_count))
    table.add_row(
        "confirmed / strong / unverified",
        f"{result.confirmed_bug_count} / {result.strongly_supported_count} / "
        f"{result.unverified_suspicion_count}",
    )
    table.add_row("公开证据 run", result.public_evidence_run_id)
    table.add_row("Claim bundle SHA256", result.claims_bundle_sha256)
    table.add_row("证书策略 SHA256", result.certificate_policy_sha256)
    table.add_row("证书 payloads SHA256", result.certificate_payloads_sha256)
    table.add_row("创建目录 / 写证书", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该预检只验证 Gate D 工程 Fixture；三等级覆盖不是五方法研究结果。[/yellow]"
    )


@phase3_app.command("certificate-generate")
def phase3_certificate_generate(
    run_id: str = typer.Option(..., "--run-id", help="Gate D 公开证书 run ID"),
    cohort_manifest: str = typer.Option(
        ...,
        "--cohort-manifest",
        help="Gate B 已冻结的自然 + 反事实 overlay manifest.json",
    ),
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="overlay 精确引用的自然轨迹 manifest.json",
    ),
    execution_run: str = typer.Option(
        ...,
        "--execution-run",
        help="Gate B 已完成的公开 Fixture 独立证据 run 目录",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="SHA256 精确白名单化的公开反事实源 bundle",
    ),
    claims_bundle: str = typer.Option(
        DEFAULT_PHASE3_CERTIFICATE_CLAIMS,
        "--claims-bundle",
        help="覆盖三等级证书的公开工程 claim bundle",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-public-certificates",
        "--output-dir",
        help="公开证书目录父路径；同名 run 拒绝覆盖",
    ),
) -> None:
    """Gate D：原子生成三等级公开脱敏工程证书，不执行候选代码。"""

    try:
        result = generate_public_certificates(
            run_id=run_id,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            claims_bundle_path=claims_bundle,
            output_dir=output_dir,
        )
    except (Phase3PublicEvidenceError, Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_public_evidence_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三公开工程证书：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("公开工程证书", str(result.certificate_count))
    table.add_row(
        "confirmed / strong / unverified",
        f"{result.confirmed_bug_count} / {result.strongly_supported_count} / "
        f"{result.unverified_suspicion_count}",
    )
    table.add_row("公开 manifest SHA256", result.manifest_sha256)
    table.add_row("证书 payloads SHA256", result.certificate_payloads_sha256)
    table.add_row("本命令执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    for certificate_path in result.certificate_paths:
        console.print(f"[dim]certificate: {certificate_path}[/dim]")
    console.print(
        "[yellow]这些是公开工程 Fixture 证书；confirmed 证书仍须用 phase3 replay 独立重放。[/yellow]"
    )


@phase3_app.command("replay")
def phase3_replay(
    certificate: str = typer.Option(
        ...,
        "--certificate",
        help="certificate-generate 产生的单个 confirmed_bug JSON 证书",
    ),
    cohort_manifest: str = typer.Option(
        ...,
        "--cohort-manifest",
        help="证书绑定的自然 + 反事实 overlay manifest.json",
    ),
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="overlay 精确引用的自然轨迹 manifest.json",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="证书绑定的 SHA256 精确白名单公开源 bundle",
    ),
    per_test_timeout_seconds: float = typer.Option(
        2.0,
        "--per-test-timeout-seconds",
        min=0.1,
        max=10.0,
        help="单个公开重放用例的父进程强制超时",
    ),
) -> None:
    """Gate D：只执行证书绑定的单个精确白名单公开 Fixture 反例。"""

    try:
        result = replay_public_certificate(
            certificate_path=certificate,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            source_bundle_path=source_bundle,
            per_test_timeout_seconds=per_test_timeout_seconds,
        )
    except (Phase3PublicEvidenceError, Phase3FreezeError, OSError, ValueError) as exc:
        _render_phase3_public_evidence_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三公开错误证书重放：{result.certificate_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("Trace / Problem", f"{result.trace_id} / {result.problem_id}")
    table.add_row("重现证书失败", "是" if result.reproduced_failure else "否")
    table.add_row("证据哈希一致", "是" if result.verified else "否")
    table.add_row("执行公开用例", str(result.executed_case_count))
    table.add_row("公开执行证据 SHA256", result.execution_evidence_sha256)
    table.add_row("执行 Provider / Docker / 网络", "否 / 否 / 否")
    table.add_row("受限本地精确白名单执行", "是")
    console.print(table)


@phase3_app.command("annotation-packet-preflight")
def phase3_annotation_packet_preflight(
    packet_id: str = typer.Option(..., "--packet-id", help="拟创建的盲法标注包 ID"),
    rater_id: str = typer.Option(..., "--rater-id", help="不含姓名或联系方式的稳定标注者 ID"),
    annotation_round: int = typer.Option(
        1,
        "--annotation-round",
        min=1,
        help="独立标注轮次；主标注通常为 1",
    ),
    blinded_to_other_raters: bool = typer.Option(
        True,
        "--blinded-to-other-raters/--not-blinded-to-other-raters",
        help="标注者是否看不到其他标注者标签",
    ),
    cohort_manifest: str = typer.Option(
        ...,
        "--cohort-manifest",
        help="Gate B 自然 + 反事实 overlay manifest.json",
    ),
    natural_manifest: str = typer.Option(
        ...,
        "--natural-manifest",
        help="overlay 精确引用的自然轨迹 manifest.json",
    ),
    phase1_run: str = typer.Option(
        ...,
        "--phase1-run",
        help="冻结自然轨迹对应的阶段一 run",
    ),
    phase2_run: str = typer.Option(
        ...,
        "--phase2-run",
        help="冻结自然轨迹对应的阶段二安全 run",
    ),
    dataset_manifest: str = typer.Option(
        ...,
        "--dataset-manifest",
        help="阶段一 research-natural 公开投影 manifest",
    ),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
        help="Gate B 精确冻结的公开反事实源",
    ),
    execution_run: str = typer.Option(
        ...,
        "--execution-run",
        help="Gate B 公开 Fixture 功能证据 run",
    ),
    protocol: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_PROTOCOL,
        "--protocol",
        help="Gate E 冻结标注协议 JSON",
    ),
    guide: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_GUIDE,
        "--guide",
        help="Gate E 冻结标注指南 Markdown",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-annotations",
        "--output-dir",
        help="拟写入的 Git-ignored 私有标注包根目录",
    ),
) -> None:
    """Gate E1：只读验证并哈希盲法标注包，不创建任何文件。"""

    try:
        result = preflight_annotation_packet(
            packet_id=packet_id,
            rater_id=rater_id,
            annotation_round=annotation_round,
            blinded_to_other_raters=blinded_to_other_raters,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            phase1_run_dir=phase1_run,
            phase2_run_dir=phase2_run,
            dataset_manifest_path=dataset_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            protocol_path=protocol,
            guide_path=guide,
            output_dir=output_dir,
        )
    except (
        Phase3AnnotationError,
        Phase3FreezeError,
        Phase3RunnerError,
        OSError,
        ValueError,
    ) as exc:
        _render_phase3_annotation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三盲法标注包只读预检：{result.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_item_count} / {result.counterfactual_item_count} / {result.item_count}",
    )
    table.add_row("标注者 / 轮次", f"{result.rater_id} / {result.annotation_round}")
    table.add_row("标注协议 SHA256", result.annotation_protocol_sha256)
    table.add_row("标注指南 SHA256", result.annotation_guide_sha256)
    table.add_row("材料 payloads SHA256", result.material_payloads_sha256)
    table.add_row("盲法 packet SHA256", result.packet_sha256)
    table.add_row("身份映射 SHA256", result.identity_map_sha256)
    table.add_row("标签模板 SHA256", result.labels_template_sha256)
    table.add_row("创建目录 / 写入标注包", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该命令会只读绑定候选正文以计算盲法包哈希，但不会打印正文、方法预测、"
        "反事实注错元数据或隐藏评测内容。[/yellow]"
    )


@phase3_app.command("annotation-packet-export")
def phase3_annotation_packet_export(
    packet_id: str = typer.Option(..., "--packet-id", help="盲法标注包 ID"),
    rater_id: str = typer.Option(..., "--rater-id", help="不含姓名或联系方式的稳定标注者 ID"),
    annotation_round: int = typer.Option(
        1,
        "--annotation-round",
        min=1,
        help="独立标注轮次；主标注通常为 1",
    ),
    blinded_to_other_raters: bool = typer.Option(
        True,
        "--blinded-to-other-raters/--not-blinded-to-other-raters",
        help="标注者是否看不到其他标注者标签",
    ),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    phase1_run: str = typer.Option(..., "--phase1-run"),
    phase2_run: str = typer.Option(..., "--phase2-run"),
    dataset_manifest: str = typer.Option(..., "--dataset-manifest"),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
    ),
    execution_run: str = typer.Option(..., "--execution-run"),
    protocol: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_PROTOCOL,
        "--protocol",
    ),
    guide: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_GUIDE,
        "--guide",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-annotations",
        "--output-dir",
    ),
) -> None:
    """Gate E1：原子写入私有盲法标注包、身份映射和未填写模板。"""

    try:
        result = export_annotation_packet(
            packet_id=packet_id,
            rater_id=rater_id,
            annotation_round=annotation_round,
            blinded_to_other_raters=blinded_to_other_raters,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            phase1_run_dir=phase1_run,
            phase2_run_dir=phase2_run,
            dataset_manifest_path=dataset_manifest,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            protocol_path=protocol,
            guide_path=guide,
            output_dir=output_dir,
        )
    except (
        Phase3AnnotationError,
        Phase3FreezeError,
        Phase3RunnerError,
        OSError,
        ValueError,
    ) as exc:
        _render_phase3_annotation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三私有盲法标注包：{result.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_item_count} / {result.counterfactual_item_count} / {result.item_count}",
    )
    table.add_row("标注者 / 轮次", f"{result.rater_id} / {result.annotation_round}")
    table.add_row("标注包 manifest SHA256", result.manifest_sha256)
    table.add_row("盲法 packet SHA256", result.packet_sha256)
    table.add_row("身份映射 SHA256", result.identity_map_sha256)
    table.add_row("标签模板 SHA256", result.labels_template_sha256)
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]packet: {result.packet_path}[/dim]")
    console.print(f"[dim]coordinator identity map: {result.identity_map_path}[/dim]")
    console.print(f"[dim]labels template: {result.labels_template_path}[/dim]")
    console.print(
        "[yellow]该目录为 Git-ignored 私有标注材料；identity_map 仅供协调者保管，"
        "不得交给独立标注者。模板尚未填写，不构成人工标签。[/yellow]"
    )


@phase3_app.command("annotation-labels-check")
def phase3_annotation_labels_check(
    packet_run: str = typer.Option(
        ...,
        "--packet-run",
        help="Gate E1 正式私有盲法标注包目录",
    ),
    packet_manifest_sha256: str = typer.Option(
        ...,
        "--packet-manifest-sha256",
        help="Gate E1 导出时记录的 manifest SHA256",
    ),
    labels: str = typer.Option(
        ...,
        "--labels",
        help="仅含 opaque item ID 的标注 working JSONL",
    ),
    protocol: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_PROTOCOL,
        "--protocol",
    ),
    guide: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_GUIDE,
        "--guide",
    ),
) -> None:
    """Gate E2：只读检查标注进度，不打开协调者 identity map。"""

    try:
        result = check_annotation_labels(
            packet_run_dir=packet_run,
            expected_packet_manifest_sha256=packet_manifest_sha256,
            completed_labels_path=labels,
            protocol_path=protocol,
            guide_path=guide,
        )
    except (Phase3AnnotationError, OSError, ValueError) as exc:
        _render_phase3_annotation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三盲法标注进度：{result.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("预期条目", str(result.expected_item_count))
    table.add_row("已完成 / 待标注", f"{result.completed_count} / {result.pending_count}")
    table.add_row(
        "无效 / 缺失 / 额外 / 顺序偏差",
        f"{result.invalid_count} / {result.missing_item_count} / "
        f"{result.extra_line_count} / {result.order_mismatch_count}",
    )
    table.add_row("Working labels SHA256", result.working_labels_sha256)
    table.add_row("可进入冻结预检", "是" if result.ready_to_freeze else "否")
    table.add_row("读取 identity map / 写入文件", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    if result.invalid_line_numbers:
        line_numbers = ", ".join(str(value) for value in result.invalid_line_numbers)
        console.print(f"[yellow]无效行号：{line_numbers}[/yellow]")
    console.print(
        "[yellow]本命令只报告完成度和结构问题；不读取身份映射，"
        "不计算人工标签分布或方法结果。[/yellow]"
    )


def _phase3_annotation_labels_freeze_preflight_impl(
    *,
    annotation_set_id: str,
    packet_run: str,
    packet_manifest_sha256: str,
    labels: str,
    cohort_manifest: str,
    natural_manifest: str,
    protocol: str,
    guide: str,
    output_dir: str,
):
    return preflight_annotation_labels_freeze(
        annotation_set_id=annotation_set_id,
        packet_run_dir=packet_run,
        expected_packet_manifest_sha256=packet_manifest_sha256,
        completed_labels_path=labels,
        cohort_manifest_path=cohort_manifest,
        natural_manifest_path=natural_manifest,
        protocol_path=protocol,
        guide_path=guide,
        output_dir=output_dir,
    )


@phase3_app.command("annotation-labels-freeze-preflight")
def phase3_annotation_labels_freeze_preflight(
    annotation_set_id: str = typer.Option(..., "--annotation-set-id"),
    packet_run: str = typer.Option(..., "--packet-run"),
    packet_manifest_sha256: str = typer.Option(..., "--packet-manifest-sha256"),
    labels: str = typer.Option(..., "--labels"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    protocol: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_PROTOCOL,
        "--protocol",
    ),
    guide: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_GUIDE,
        "--guide",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-labels",
        "--output-dir",
    ),
) -> None:
    """Gate E2：完整标签才能只读回连身份并核算冻结哈希。"""

    try:
        result = _phase3_annotation_labels_freeze_preflight_impl(
            annotation_set_id=annotation_set_id,
            packet_run=packet_run,
            packet_manifest_sha256=packet_manifest_sha256,
            labels=labels,
            cohort_manifest=cohort_manifest,
            natural_manifest=natural_manifest,
            protocol=protocol,
            guide=guide,
            output_dir=output_dir,
        )
    except (Phase3AnnotationError, Phase3RunnerError, OSError, ValueError) as exc:
        _render_phase3_annotation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三人工标签冻结只读预检：{result.annotation_set_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_trace_count} / {result.counterfactual_trace_count} / "
        f"{result.record_count}",
    )
    table.add_row("标注者 / 轮次", f"{result.rater_id} / {result.annotation_round}")
    table.add_row("标注协议 SHA256", result.annotation_protocol_sha256)
    table.add_row("源 packet manifest SHA256", result.source_packet_manifest_sha256)
    table.add_row("完成标签 SHA256", result.completed_labels_sha256)
    table.add_row("回连标注记录 SHA256", result.annotation_records_sha256)
    table.add_row("创建目录 / 写入冻结集", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该预检仅在 57 条全部完成后读取协调者 identity map；"
        "不打印身份、标签正负分布或理由。[/yellow]"
    )


@phase3_app.command("annotation-labels-freeze")
def phase3_annotation_labels_freeze(
    annotation_set_id: str = typer.Option(..., "--annotation-set-id"),
    packet_run: str = typer.Option(..., "--packet-run"),
    packet_manifest_sha256: str = typer.Option(..., "--packet-manifest-sha256"),
    labels: str = typer.Option(..., "--labels"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    protocol: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_PROTOCOL,
        "--protocol",
    ),
    guide: str = typer.Option(
        DEFAULT_PHASE3_ANNOTATION_GUIDE,
        "--guide",
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-labels",
        "--output-dir",
    ),
) -> None:
    """Gate E2：原子冻结一个完整的私有盲法人工标注集。"""

    try:
        result = freeze_annotation_labels(
            annotation_set_id=annotation_set_id,
            packet_run_dir=packet_run,
            expected_packet_manifest_sha256=packet_manifest_sha256,
            completed_labels_path=labels,
            cohort_manifest_path=cohort_manifest,
            natural_manifest_path=natural_manifest,
            protocol_path=protocol,
            guide_path=guide,
            output_dir=output_dir,
        )
    except (Phase3AnnotationError, Phase3RunnerError, OSError, ValueError) as exc:
        _render_phase3_annotation_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三私有人工标注冻结集：{result.annotation_set_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_trace_count} / {result.counterfactual_trace_count} / "
        f"{result.record_count}",
    )
    table.add_row("标注者 / 轮次", f"{result.rater_id} / {result.annotation_round}")
    table.add_row("私有 manifest SHA256", result.manifest_sha256)
    table.add_row("完成标签 SHA256", result.completed_labels_sha256)
    table.add_row("回连标注记录 SHA256", result.annotation_records_sha256)
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]completed labels: {result.completed_labels_path}[/dim]")
    console.print(f"[dim]annotation records: {result.annotation_records_path}[/dim]")
    console.print(
        "[yellow]该目录为 Git-ignored 私有人工标签；尚未执行五种方法，"
        "agreement_kind 仍为 not_computed。[/yellow]"
    )


def _phase3_evaluation_arguments(
    *,
    run_id: str,
    cohort_manifest: str,
    natural_manifest: str,
    annotation_set_manifest: str,
    annotation_set_manifest_sha256: str,
    phase1_run: str,
    phase2_run: str,
    dataset_manifest: str,
    source_bundle: str,
    execution_run: str,
    protocol: str,
    guide: str,
    provider: str,
    model: str,
    temperature: float,
    timeout_seconds: float,
    output_dir: str,
    resume: bool,
    allow_dirty: bool,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
        "annotation_set_manifest_path": annotation_set_manifest,
        "expected_annotation_set_manifest_sha256": annotation_set_manifest_sha256,
        "phase1_run_dir": phase1_run,
        "phase2_run_dir": phase2_run,
        "dataset_manifest_path": dataset_manifest,
        "source_bundle_path": source_bundle,
        "execution_run_dir": execution_run,
        "protocol_path": protocol,
        "guide_path": guide,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "output_dir": output_dir,
        "resume": resume,
        "allow_dirty": allow_dirty,
        "random_seed": PHASE3_EVALUATION_RANDOM_SEED,
        "settings": None,
        "privacy_canaries": (),
    }


@phase3_app.command("evaluate-preflight")
def phase3_evaluate_preflight(
    run_id: str = typer.Option(..., "--run-id"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    annotation_set_manifest: str = typer.Option(..., "--annotation-set-manifest"),
    annotation_set_manifest_sha256: str = typer.Option(
        ...,
        "--annotation-set-manifest-sha256",
    ),
    phase1_run: str = typer.Option(..., "--phase1-run"),
    phase2_run: str = typer.Option(..., "--phase2-run"),
    dataset_manifest: str = typer.Option(..., "--dataset-manifest"),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
    ),
    execution_run: str = typer.Option(..., "--execution-run"),
    protocol: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_PROTOCOL, "--protocol"),
    guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--guide"),
    provider: str = typer.Option("hy3", "--provider"),
    model: str = typer.Option(..., "--model"),
    temperature: float = typer.Option(0.0, "--temperature", min=0.0, max=2.0),
    timeout_seconds: float = typer.Option(
        120.0,
        "--timeout-seconds",
        min=1.0,
        max=600.0,
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-runs",
        "--output-dir",
    ),
    resume: bool = typer.Option(False, "--resume"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
) -> None:
    """Gate E3：只读绑定冻结标签、方法材料、Provider 与恢复身份。"""

    arguments = _phase3_evaluation_arguments(
        run_id=run_id,
        cohort_manifest=cohort_manifest,
        natural_manifest=natural_manifest,
        annotation_set_manifest=annotation_set_manifest,
        annotation_set_manifest_sha256=annotation_set_manifest_sha256,
        phase1_run=phase1_run,
        phase2_run=phase2_run,
        dataset_manifest=dataset_manifest,
        source_bundle=source_bundle,
        execution_run=execution_run,
        protocol=protocol,
        guide=guide,
        provider=provider,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir,
        resume=resume,
        allow_dirty=allow_dirty,
    )
    try:
        result = preflight_phase3_evaluation(**arguments)
    except (Phase3RunnerError, OSError, ValueError) as exc:
        _render_phase3_interface_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三五方法正式运行只读预检：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_trace_count} / {result.counterfactual_trace_count} / "
        f"{result.trace_count}",
    )
    table.add_row(
        "轨迹 / 方法 / 配对", f"{result.trace_count} / {result.method_count} / {result.pair_count}"
    )
    table.add_row(
        "Provider 配对 / 最大 Provider 调用",
        f"{result.provider_pair_count} / {result.maximum_provider_call_count}",
    )
    table.add_row("Judge Provider / 模型", f"{result.provider} / {result.model}")
    table.add_row("人工标签集", result.annotation_set_id)
    table.add_row("人工标签 manifest SHA256", result.annotation_set_manifest_sha256)
    table.add_row("方法材料 SHA256", result.material_payloads_sha256)
    table.add_row("方法规格 SHA256", result.method_specs_sha256)
    table.add_row("Provider 配置 SHA256", result.provider_config_sha256)
    table.add_row("Resume identity SHA256", result.resume_identity_sha256)
    table.add_row(
        "Git commit / 分支 / dirty",
        f"{result.git_commit} / {result.git_branch} / {result.git_dirty}",
    )
    table.add_row("创建目录 / 写入产物", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该预检只核算正式运行身份；不打印人工标签内容或分布，也不连接 Hy3。[/yellow]"
    )


@phase3_app.command("evaluate")
def phase3_evaluate(
    run_id: str = typer.Option(..., "--run-id"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    annotation_set_manifest: str = typer.Option(..., "--annotation-set-manifest"),
    annotation_set_manifest_sha256: str = typer.Option(
        ...,
        "--annotation-set-manifest-sha256",
    ),
    phase1_run: str = typer.Option(..., "--phase1-run"),
    phase2_run: str = typer.Option(..., "--phase2-run"),
    dataset_manifest: str = typer.Option(..., "--dataset-manifest"),
    source_bundle: str = typer.Option(
        DEFAULT_PHASE3_COUNTERFACTUAL_SOURCE,
        "--source-bundle",
    ),
    execution_run: str = typer.Option(..., "--execution-run"),
    protocol: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_PROTOCOL, "--protocol"),
    guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--guide"),
    provider: str = typer.Option("hy3", "--provider"),
    model: str = typer.Option(..., "--model"),
    temperature: float = typer.Option(0.0, "--temperature", min=0.0, max=2.0),
    timeout_seconds: float = typer.Option(
        120.0,
        "--timeout-seconds",
        min=1.0,
        max=600.0,
    ),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-runs",
        "--output-dir",
    ),
    resume: bool = typer.Option(False, "--resume"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty"),
    confirm_real_provider: bool = typer.Option(False, "--confirm-real-provider"),
) -> None:
    """Gate E3：显式确认后执行同一 57×5 冻结配对积。"""

    arguments = _phase3_evaluation_arguments(
        run_id=run_id,
        cohort_manifest=cohort_manifest,
        natural_manifest=natural_manifest,
        annotation_set_manifest=annotation_set_manifest,
        annotation_set_manifest_sha256=annotation_set_manifest_sha256,
        phase1_run=phase1_run,
        phase2_run=phase2_run,
        dataset_manifest=dataset_manifest,
        source_bundle=source_bundle,
        execution_run=execution_run,
        protocol=protocol,
        guide=guide,
        provider=provider,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir,
        resume=resume,
        allow_dirty=allow_dirty,
    )
    try:
        result = asyncio.run(
            execute_phase3_evaluation(
                confirm_real_provider=confirm_real_provider,
                **arguments,
            )
        )
    except (Phase3RunnerError, OSError, ValueError) as exc:
        _render_phase3_interface_failure(exc)
        raise typer.Exit(code=1) from exc

    run = result.run
    status_summary = ", ".join(
        f"{status.value}={count}" for status, count in run.status_counts.items() if count
    )
    table = Table(title=f"阶段三五方法正式配对运行：{run.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("完整配对", str(run.result_count))
    table.add_row("恢复复用", str(run.reused_count))
    table.add_row("结果状态", status_summary or "none")
    table.add_row("公开 results SHA256", run.results_sha256)
    table.add_row("公开 index SHA256", run.index_sha256)
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 是 / 否 / 是")
    console.print(table)
    console.print(f"[dim]manifest: {run.manifest_path}[/dim]")
    console.print(f"[dim]results: {run.results_path}[/dim]")
    console.print(f"[dim]index: {run.index_path}[/dim]")
    console.print("[yellow]该运行保留全部失败与 285 配对分母；尚未计算人工标签对比统计。[/yellow]")


def _phase3_statistics_arguments(
    *,
    statistics_id: str,
    paired_run: str,
    paired_run_manifest_sha256: str,
    results_sha256: str,
    index_sha256: str,
    cohort_manifest: str,
    natural_manifest: str,
    annotation_set_manifest: str,
    annotation_set_manifest_sha256: str,
    protocol: str,
    guide: str,
    output_dir: str,
    allow_dirty: bool,
) -> dict[str, object]:
    return {
        "statistics_id": statistics_id,
        "paired_run_dir": paired_run,
        "expected_paired_run_manifest_sha256": paired_run_manifest_sha256,
        "expected_results_sha256": results_sha256,
        "expected_index_sha256": index_sha256,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
        "annotation_set_manifest_path": annotation_set_manifest,
        "expected_annotation_set_manifest_sha256": annotation_set_manifest_sha256,
        "protocol_path": protocol,
        "guide_path": guide,
        "output_dir": output_dir,
        "allow_dirty": allow_dirty,
        "privacy_canaries": (),
    }


def _render_phase3_statistics_failure(exc: BaseException) -> None:
    console.print(
        "[red]阶段三配对统计校验失败；未输出逐轨迹标签、方法预测、Provider raw 或隐藏评测内容。[/red]"
    )
    console.print(f"[yellow]安全阶段码：{getattr(exc, 'safe_stage', 'P3E4_UNCLASSIFIED')}[/yellow]")


@phase3_app.command("statistics-preflight")
def phase3_statistics_preflight(
    statistics_id: str = typer.Option(..., "--statistics-id"),
    paired_run: str = typer.Option(..., "--paired-run"),
    paired_run_manifest_sha256: str = typer.Option(..., "--paired-run-manifest-sha256"),
    results_sha256: str = typer.Option(..., "--results-sha256"),
    index_sha256: str = typer.Option(..., "--index-sha256"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    annotation_set_manifest: str = typer.Option(..., "--annotation-set-manifest"),
    annotation_set_manifest_sha256: str = typer.Option(
        ...,
        "--annotation-set-manifest-sha256",
    ),
    protocol: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_PROTOCOL, "--protocol"),
    guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--guide"),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-statistics",
        "--output-dir",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate E4：只读校验并在内存计算冻结配对统计，不写研究产物。"""

    arguments = _phase3_statistics_arguments(
        statistics_id=statistics_id,
        paired_run=paired_run,
        paired_run_manifest_sha256=paired_run_manifest_sha256,
        results_sha256=results_sha256,
        index_sha256=index_sha256,
        cohort_manifest=cohort_manifest,
        natural_manifest=natural_manifest,
        annotation_set_manifest=annotation_set_manifest,
        annotation_set_manifest_sha256=annotation_set_manifest_sha256,
        protocol=protocol,
        guide=guide,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
    )
    try:
        result = preflight_phase3_statistics(**arguments)
    except (Phase3StatisticsError, OSError, ValueError) as exc:
        _render_phase3_statistics_failure(exc)
        raise typer.Exit(code=1) from exc

    status_summary = ", ".join(
        f"{status}={count}" for status, count in result.final_status_counts.items() if count
    )
    table = Table(title=f"阶段三 Gate E4 配对统计只读预检：{result.statistics_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_trace_count} / {result.counterfactual_trace_count} / {result.trace_count}",
    )
    table.add_row(
        "轨迹 / 方法 / 配对",
        f"{result.trace_count} / {result.method_count} / {result.pair_count}",
    )
    table.add_row("原始结果状态", status_summary or "none")
    table.add_row("配对 run manifest SHA256", result.paired_run_manifest_sha256)
    table.add_row("配对 results SHA256", result.paired_results_sha256)
    table.add_row("配对 index SHA256", result.paired_index_sha256)
    table.add_row("人工标签 manifest SHA256", result.annotation_set_manifest_sha256)
    table.add_row("统计实现 SHA256", result.statistics_implementation_sha256)
    table.add_row("拟生成 report SHA256", result.report_sha256)
    table.add_row(
        "Git commit / 分支 / dirty",
        f"{result.git_commit} / {result.git_branch} / {result.git_dirty}",
    )
    table.add_row("创建目录 / 写入统计", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该预检已在内存核算聚合统计，但不展示标签分布或方法结果，也不写入研究产物。[/yellow]"
    )


@phase3_app.command("statistics")
def phase3_statistics(
    statistics_id: str = typer.Option(..., "--statistics-id"),
    paired_run: str = typer.Option(..., "--paired-run"),
    paired_run_manifest_sha256: str = typer.Option(..., "--paired-run-manifest-sha256"),
    results_sha256: str = typer.Option(..., "--results-sha256"),
    index_sha256: str = typer.Option(..., "--index-sha256"),
    cohort_manifest: str = typer.Option(..., "--cohort-manifest"),
    natural_manifest: str = typer.Option(..., "--natural-manifest"),
    annotation_set_manifest: str = typer.Option(..., "--annotation-set-manifest"),
    annotation_set_manifest_sha256: str = typer.Option(
        ...,
        "--annotation-set-manifest-sha256",
    ),
    protocol: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_PROTOCOL, "--protocol"),
    guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--guide"),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-statistics",
        "--output-dir",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate E4：原子写入不含逐轨迹内容的冻结聚合统计。"""

    arguments = _phase3_statistics_arguments(
        statistics_id=statistics_id,
        paired_run=paired_run,
        paired_run_manifest_sha256=paired_run_manifest_sha256,
        results_sha256=results_sha256,
        index_sha256=index_sha256,
        cohort_manifest=cohort_manifest,
        natural_manifest=natural_manifest,
        annotation_set_manifest=annotation_set_manifest,
        annotation_set_manifest_sha256=annotation_set_manifest_sha256,
        protocol=protocol,
        guide=guide,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
    )
    try:
        result = generate_phase3_statistics(**arguments)
    except (Phase3StatisticsError, OSError, ValueError) as exc:
        _render_phase3_statistics_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三 Gate E4 冻结配对统计：{result.statistics_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.natural_trace_count} / {result.counterfactual_trace_count} / {result.trace_count}",
    )
    table.add_row("完整配对", str(result.pair_count))
    table.add_row("公开 report SHA256", result.report_sha256)
    table.add_row("公开 manifest SHA256", result.manifest_sha256)
    table.add_row("逐轨迹标签 / 方法预测 / Provider raw", "否 / 否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]report: {result.report_path}[/dim]")
    console.print(
        "[yellow]该产物是探索性聚合统计；尚未完成人工一致性，也不能把不显著解释为等效。[/yellow]"
    )


def _phase3_report_arguments(
    *,
    report_id: str,
    statistics_run: str,
    statistics_manifest_sha256: str,
    statistics_report_sha256: str,
    paired_run: str,
    certificate_run: str,
    certificate_manifest_sha256: str,
    confirmed_certificate: str,
    confirmed_certificate_sha256: str,
    replay_evidence_sha256: str,
    output_dir: str,
    allow_dirty: bool,
) -> dict[str, object]:
    return {
        "report_id": report_id,
        "statistics_run_dir": statistics_run,
        "expected_statistics_manifest_sha256": statistics_manifest_sha256,
        "expected_statistics_report_sha256": statistics_report_sha256,
        "paired_run_dir": paired_run,
        "certificate_run_dir": certificate_run,
        "expected_certificate_manifest_sha256": certificate_manifest_sha256,
        "confirmed_certificate_path": confirmed_certificate,
        "expected_confirmed_certificate_sha256": confirmed_certificate_sha256,
        "expected_replay_evidence_sha256": replay_evidence_sha256,
        "output_dir": output_dir,
        "allow_dirty": allow_dirty,
        "privacy_canaries": (),
    }


def _render_phase3_report_failure(exc: BaseException) -> None:
    console.print(
        "[red]阶段三脱敏报告校验失败；未输出逐轨迹标签、方法预测、Provider raw 或隐藏评测内容。[/red]"
    )
    console.print(f"[yellow]安全阶段码：{getattr(exc, 'safe_stage', 'P3F_UNCLASSIFIED')}[/yellow]")


@phase3_app.command("report-preflight")
def phase3_report_preflight(
    report_id: str = typer.Option(..., "--report-id"),
    statistics_run: str = typer.Option(..., "--statistics-run"),
    statistics_manifest_sha256: str = typer.Option(
        ...,
        "--statistics-manifest-sha256",
    ),
    statistics_report_sha256: str = typer.Option(..., "--statistics-report-sha256"),
    paired_run: str = typer.Option(..., "--paired-run"),
    certificate_run: str = typer.Option(..., "--certificate-run"),
    certificate_manifest_sha256: str = typer.Option(
        ...,
        "--certificate-manifest-sha256",
    ),
    confirmed_certificate: str = typer.Option(..., "--confirmed-certificate"),
    confirmed_certificate_sha256: str = typer.Option(
        ...,
        "--confirmed-certificate-sha256",
    ),
    replay_evidence_sha256: str = typer.Option(..., "--replay-evidence-sha256"),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-reports",
        "--output-dir",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate F：只读解读冻结统计并在内存生成脱敏报告，不写产物。"""

    arguments = _phase3_report_arguments(
        report_id=report_id,
        statistics_run=statistics_run,
        statistics_manifest_sha256=statistics_manifest_sha256,
        statistics_report_sha256=statistics_report_sha256,
        paired_run=paired_run,
        certificate_run=certificate_run,
        certificate_manifest_sha256=certificate_manifest_sha256,
        confirmed_certificate=confirmed_certificate,
        confirmed_certificate_sha256=confirmed_certificate_sha256,
        replay_evidence_sha256=replay_evidence_sha256,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
    )
    try:
        result = preflight_phase3_report(**arguments)
    except (Phase3ReportError, OSError, ValueError) as exc:
        _render_phase3_report_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三 Gate F 脱敏报告只读预检：{result.report_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "轨迹 / 方法 / 配对",
        f"{result.trace_count} / {result.method_count} / {result.pair_count}",
    )
    table.add_row(
        "原始结果状态",
        f"valid_judgment={result.valid_judgment_count}, provider_error={result.provider_error_count}",
    )
    table.add_row("总体置信等级", result.overall_confidence)
    table.add_row("统计谬误扫描", f"{result.fallacy_scan_coverage}/11")
    table.add_row("统计 report SHA256", result.statistics_report_sha256)
    table.add_row("报告实现 SHA256", result.report_implementation_sha256)
    table.add_row("拟生成 Markdown SHA256", result.markdown_sha256)
    table.add_row("拟生成 validation SHA256", result.validation_sha256)
    table.add_row(
        "Git commit / 分支 / dirty",
        f"{result.git_commit} / {result.git_branch} / {result.git_dirty}",
    )
    table.add_row("创建目录 / 写入报告", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]该预检只核验聚合统计、公开证书和脱敏边界；不展示方法结果，也不自动重放证书。[/yellow]"
    )


@phase3_app.command("report")
def phase3_report(
    report_id: str = typer.Option(..., "--report-id"),
    statistics_run: str = typer.Option(..., "--statistics-run"),
    statistics_manifest_sha256: str = typer.Option(
        ...,
        "--statistics-manifest-sha256",
    ),
    statistics_report_sha256: str = typer.Option(..., "--statistics-report-sha256"),
    paired_run: str = typer.Option(..., "--paired-run"),
    certificate_run: str = typer.Option(..., "--certificate-run"),
    certificate_manifest_sha256: str = typer.Option(
        ...,
        "--certificate-manifest-sha256",
    ),
    confirmed_certificate: str = typer.Option(..., "--confirmed-certificate"),
    confirmed_certificate_sha256: str = typer.Option(
        ...,
        "--confirmed-certificate-sha256",
    ),
    replay_evidence_sha256: str = typer.Option(..., "--replay-evidence-sha256"),
    output_dir: str = typer.Option(
        "artifacts/experiments/phase3-reports",
        "--output-dir",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate F：原子写入脱敏研究报告、验证记录和公开证书 Demo。"""

    arguments = _phase3_report_arguments(
        report_id=report_id,
        statistics_run=statistics_run,
        statistics_manifest_sha256=statistics_manifest_sha256,
        statistics_report_sha256=statistics_report_sha256,
        paired_run=paired_run,
        certificate_run=certificate_run,
        certificate_manifest_sha256=certificate_manifest_sha256,
        confirmed_certificate=confirmed_certificate,
        confirmed_certificate_sha256=confirmed_certificate_sha256,
        replay_evidence_sha256=replay_evidence_sha256,
        output_dir=output_dir,
        allow_dirty=allow_dirty,
    )
    try:
        result = generate_phase3_report(**arguments)
    except (Phase3ReportError, OSError, ValueError) as exc:
        _render_phase3_report_failure(exc)
        raise typer.Exit(code=1) from exc

    table = Table(title=f"阶段三 Gate F 脱敏研究报告：{result.report_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "轨迹 / 方法 / 配对", f"{result.trace_count} / {result.method_count} / {result.pair_count}"
    )
    table.add_row("总体置信等级", result.overall_confidence)
    table.add_row("统计谬误扫描", f"{result.fallacy_scan_coverage}/11")
    table.add_row("公开 Markdown SHA256", result.markdown_sha256)
    table.add_row("公开 validation SHA256", result.validation_sha256)
    table.add_row("公开 manifest SHA256", result.manifest_sha256)
    table.add_row("逐轨迹标签 / 方法预测 / Provider raw", "否 / 否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]report: {result.markdown_path}[/dim]")
    console.print(f"[dim]validation: {result.validation_path}[/dim]")
    console.print(f"[dim]certificate demo: {result.demo_certificate_path}[/dim]")
    console.print(f"[dim]replay command: {result.replay_command_path}[/dim]")
    console.print(
        "[yellow]该报告的验证状态为 ANALYZED、总体置信为 CAUTION；不显著不等于方法等效。[/yellow]"
    )


def _render_phase4_failure(exc: BaseException) -> None:
    console.print("[red]阶段四校验失败；未输出敏感正文。[/red]")
    console.print(f"[yellow]安全阶段码：{getattr(exc, 'safe_stage', 'P4B_UNCLASSIFIED')}[/yellow]")


@phase4_app.command("stability-preflight")
def phase4_stability_preflight(
    run_id: str = typer.Option(..., "--run-id", help="独立稳定性实验 run ID"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    execution_run: str = typer.Option(
        DEFAULT_PHASE4_P1_EXECUTION_RUN,
        "--execution-run",
        help="阶段三已冻结的公开 Fixture 功能证据 run",
    ),
    output_dir: str = typer.Option(DEFAULT_PHASE4_STABILITY_OUTPUT, "--output-dir"),
    repo_root: str = typer.Option(".", "--repo-root"),
    temperature: float = typer.Option(0.0, "--temperature", min=0.0),
    timeout_seconds: float = typer.Option(
        120.0,
        "--timeout-seconds",
        min=1.0,
        help="每次底层 Judge 请求的超时",
    ),
    resume: bool = typer.Option(False, "--resume", help="预检同 ID 的未完成稳定性 run"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """只读固化 4 个公开案例 × 5 次 Full TraceJudge 计划，不调用 Provider。"""

    try:
        result = preflight_hy3_judge_stability(
            run_id=run_id,
            source_bundle_path=source_bundle,
            execution_run_dir=execution_run,
            output_dir=output_dir,
            repo_root=repo_root,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            resume=resume,
            allow_dirty=allow_dirty,
        )
    except (Phase4StabilityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 Judge 稳定性只读预检：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "公开案例 / 每例重复 / 评审单元",
        f"{result.case_count} / {result.repetition_count} / {result.scheduled_evaluation_count}",
    )
    table.add_row(
        "名义 / 最大底层 Provider 请求",
        f"{result.nominal_provider_call_count} / {result.maximum_provider_call_count}",
    )
    table.add_row("Provider / 模型", f"{result.provider} / {result.model}")
    table.add_row("Protocol SHA256", result.protocol_sha256)
    table.add_row("公开证据 results SHA256", result.execution_results_sha256)
    table.add_row("方法输入集合 SHA256", result.material_payloads_sha256)
    table.add_row("Full Prompt SHA256", result.prompt_sha256)
    table.add_row("Git commit / dirty", f"{result.git_commit} / {result.git_dirty}")
    table.add_row("创建目录 / 调用 Provider", "否 / 否")
    console.print(table)
    console.print("[yellow]该计划是独立探索性附加实验，不得并入冻结的 57×5 主实验。[/yellow]")


@phase4_app.command("stability-run")
def phase4_stability_run(
    run_id: str = typer.Option(..., "--run-id", help="独立稳定性实验 run ID"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    execution_run: str = typer.Option(
        DEFAULT_PHASE4_P1_EXECUTION_RUN,
        "--execution-run",
        help="阶段三已冻结的公开 Fixture 功能证据 run",
    ),
    output_dir: str = typer.Option(DEFAULT_PHASE4_STABILITY_OUTPUT, "--output-dir"),
    repo_root: str = typer.Option(".", "--repo-root"),
    temperature: float = typer.Option(0.0, "--temperature", min=0.0),
    timeout_seconds: float = typer.Option(
        120.0,
        "--timeout-seconds",
        min=1.0,
        help="每次底层 Judge 请求的超时",
    ),
    resume: bool = typer.Option(False, "--resume", help="续跑同 ID 的未完成稳定性 run"),
    confirm_real_provider: bool = typer.Option(
        False,
        "--confirm-real-provider",
        help="确认本命令会调用真实 Hy3，并可能产生费用",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """执行或续跑 20 个独立评审单元，并生成 JSON/Markdown 稳定性报告。"""

    try:
        result = asyncio.run(
            execute_hy3_judge_stability(
                confirm_real_provider=confirm_real_provider,
                run_id=run_id,
                source_bundle_path=source_bundle,
                execution_run_dir=execution_run,
                output_dir=output_dir,
                repo_root=repo_root,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                resume=resume,
                allow_dirty=allow_dirty,
            )
        )
    except (Phase4StabilityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 Judge 稳定性实验：{result.run_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("有效判断 / 计划", f"{result.valid_judgment_count} / 20")
    table.add_row(
        "Provider / 解析失败", f"{result.provider_failure_count} / {result.parse_failure_count}"
    )
    table.add_row("实际底层 Provider 请求", str(result.observed_provider_call_count))
    table.add_row("results SHA256", result.results_sha256)
    table.add_row("report JSON SHA256", result.report_json_sha256)
    table.add_row("report Markdown SHA256", result.report_markdown_sha256)
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]report: {result.report_markdown_path}[/dim]")
    console.print(
        "[yellow]结果仅描述四个目的性选择公开案例的运行内稳定性；不得覆盖或合并主实验。[/yellow]"
    )


@phase4_app.command("stability-sensitivity-publish")
def phase4_stability_sensitivity_publish(
    run_dir: str = typer.Option(
        DEFAULT_PHASE4_STABILITY_RUN,
        "--run-dir",
        help="已完成且哈希有效的四案例稳定性 run",
    ),
    output_dir: str = typer.Option(
        DEFAULT_PHASE4_STABILITY_RELEASE_OUTPUT,
        "--output-dir",
        help="公开聚合报告与结果卡片目录",
    ),
) -> None:
    """离线发布原始结果卡片和 post-hoc 标识符规范化敏感性报告。"""

    try:
        result = publish_stability_sensitivity_release(
            run_dir=run_dir,
            output_dir=output_dir,
        )
    except (Phase4StabilitySensitivityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    fields = {item.field_name: item for item in result.analysis.overall_fields}
    step = fields["first_faulty_step"]
    table = Table(title="阶段四 Judge 稳定性结果卡片与敏感性报告")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("有效判断 / Provider / 解析失败", "20 / 0 / 0")
    table.add_row(
        "has_error / error_type 原始一致率",
        "100.0% / 100.0%",
    )
    table.add_row(
        "首错步骤：原始 / post-hoc 规范化",
        f"{step.raw.pairwise_agreement * 100:.1f}% / "
        f"{step.normalized.pairwise_agreement * 100:.1f}%",
    )
    table.add_row("新增 Provider / Docker / 网络调用", "0 / 0 / 0")
    table.add_row("报告 JSON SHA256", result.json_sha256)
    table.add_row("报告 Markdown SHA256", result.markdown_sha256)
    table.add_row("结果卡片 SVG SHA256", result.card_sha256)
    console.print(table)
    console.print(f"[dim]report: {result.markdown_path}[/dim]")
    console.print(f"[dim]card: {result.card_path}[/dim]")
    console.print(
        "[yellow]规范化 100% 是事后敏感性读数；预注册原始首错步骤一致率仍为 90%。[/yellow]"
    )


@phase4_app.command("artifact-preflight")
def phase4_artifact_preflight(
    inventory_id: str = typer.Option("phase4_artifact_inventory_v1", "--inventory-id"),
    digest_id: str = typer.Option("phase4_public_artifact_digest_v1", "--digest-id"),
    repo_root: str = typer.Option(".", "--repo-root"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
    allow_permission_warnings: bool = typer.Option(
        False,
        "--allow-permission-warnings",
        help="仅用于记录现状；正式封版前仍须消除所有私有权限警告",
    ),
) -> None:
    """Gate B：只读计算关键 Git-ignored 产物的哈希与权限清单。"""

    try:
        result = preflight_artifact_inventory(
            repo_root=repo_root,
            inventory_id=inventory_id,
            digest_id=digest_id,
            allow_dirty=allow_dirty,
            allow_permission_warnings=allow_permission_warnings,
        )
    except (Phase4ReproducibilityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四关键产物清单只读预检：{result.inventory.inventory_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("关键产物", str(result.inventory.artifact_count))
    table.add_row("公开哈希锚点", str(result.public_digest.public_anchor_count))
    table.add_row("权限警告", str(result.inventory.permission_warning_count))
    table.add_row("确定性 artifact-set SHA256", result.inventory.artifact_set_sha256)
    table.add_row("私有 manifest SHA256", result.private_manifest_sha256)
    table.add_row("解析或打印正文 / 写入文件", "否 / 否")
    table.add_row("执行候选 / Provider / Docker / 网络", "否 / 否 / 否 / 否")
    console.print(table)


@phase4_app.command("artifact-freeze")
def phase4_artifact_freeze(
    inventory_id: str = typer.Option("phase4_artifact_inventory_v1", "--inventory-id"),
    digest_id: str = typer.Option("phase4_public_artifact_digest_v1", "--digest-id"),
    repo_root: str = typer.Option(".", "--repo-root"),
    private_output_dir: str = typer.Option(
        "artifacts/experiments/phase4-reproducibility",
        "--private-output-dir",
    ),
    public_output_dir: str = typer.Option("docs/releases/phase4", "--public-output-dir"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
    allow_permission_warnings: bool = typer.Option(
        False,
        "--allow-permission-warnings",
        help="仅用于记录现状；正式封版前仍须消除所有私有权限警告",
    ),
) -> None:
    """Gate B：原子冻结私有完整清单与不含私有路径的公开摘要。"""

    try:
        result = freeze_artifact_inventory(
            repo_root=repo_root,
            inventory_id=inventory_id,
            digest_id=digest_id,
            private_output_dir=private_output_dir,
            public_output_dir=public_output_dir,
            allow_dirty=allow_dirty,
            allow_permission_warnings=allow_permission_warnings,
        )
    except (Phase4ReproducibilityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四关键产物清单：{result.inventory_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "关键产物 / 权限警告", f"{result.artifact_count} / {result.permission_warning_count}"
    )
    table.add_row("确定性 artifact-set SHA256", result.artifact_set_sha256)
    table.add_row("私有 manifest SHA256", result.private_manifest_sha256)
    table.add_row("公开 digest SHA256", result.public_digest_sha256)
    table.add_row("公开摘要含私有路径/正文", "否 / 否")
    console.print(table)
    console.print(f"[dim]private manifest: {result.private_manifest_path}[/dim]")
    console.print(f"[dim]public digest: {result.public_digest_path}[/dim]")


@phase4_app.command("artifact-verify")
def phase4_artifact_verify(
    manifest: str = typer.Option(..., "--manifest"),
    repo_root: str = typer.Option(".", "--repo-root"),
) -> None:
    """Gate B：验证原目录或恢复目录的文件大小、mode 与 SHA256。"""

    try:
        result = verify_artifact_inventory(repo_root=repo_root, manifest_path=manifest)
    except (Phase4ReproducibilityError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]恢复验证通过：{result.inventory_id}，{result.artifact_count} 个关键产物。[/green]"
    )


def _phase4_replay_arguments(
    *,
    receipt_id: str,
    certificate: str,
    certificate_manifest: str,
    cohort_manifest: str,
    natural_manifest: str,
    source_bundle: str,
    repo_root: str,
    allow_dirty: bool,
) -> dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "certificate_path": certificate,
        "certificate_manifest_path": certificate_manifest,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
        "source_bundle_path": source_bundle,
        "repo_root": repo_root,
        "allow_dirty": allow_dirty,
    }


@phase4_app.command("replay-receipt-preflight")
def phase4_replay_receipt_preflight(
    receipt_id: str = typer.Option("phase4_public_replay_receipt_v1", "--receipt-id"),
    certificate: str = typer.Option(DEFAULT_PHASE4_CERTIFICATE, "--certificate"),
    certificate_manifest: str = typer.Option(
        DEFAULT_PHASE4_CERTIFICATE_MANIFEST,
        "--certificate-manifest",
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    repo_root: str = typer.Option(".", "--repo-root"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate B：执行一个公开白名单用例，在内存生成 receipt，不写文件。"""

    try:
        receipt = prepare_public_replay_receipt(
            **_phase4_replay_arguments(
                receipt_id=receipt_id,
                certificate=certificate,
                certificate_manifest=certificate_manifest,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
                source_bundle=source_bundle,
                repo_root=repo_root,
                allow_dirty=allow_dirty,
            )
        )
    except (Phase4ReproducibilityError, Phase3PublicEvidenceError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四公开 replay receipt 只读预检：{receipt.receipt_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("重现失败 / 证据哈希一致", "是 / 是")
    table.add_row("公开执行证据 SHA256", receipt.execution_evidence_sha256)
    table.add_row("执行公开用例", str(receipt.safety.executed_public_case_count))
    table.add_row("写入文件", "否")
    table.add_row("Provider / Docker / 网络", "否 / 否 / 否")
    console.print(table)


@phase4_app.command("replay-receipt")
def phase4_replay_receipt(
    receipt_id: str = typer.Option("phase4_public_replay_receipt_v1", "--receipt-id"),
    certificate: str = typer.Option(DEFAULT_PHASE4_CERTIFICATE, "--certificate"),
    certificate_manifest: str = typer.Option(
        DEFAULT_PHASE4_CERTIFICATE_MANIFEST,
        "--certificate-manifest",
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    repo_root: str = typer.Option(".", "--repo-root"),
    output_dir: str = typer.Option("docs/releases/phase4", "--output-dir"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate B：执行一个公开白名单用例并原子持久化脱敏 receipt。"""

    try:
        result = write_public_replay_receipt(
            output_dir=output_dir,
            **_phase4_replay_arguments(
                receipt_id=receipt_id,
                certificate=certificate,
                certificate_manifest=certificate_manifest,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
                source_bundle=source_bundle,
                repo_root=repo_root,
                allow_dirty=allow_dirty,
            ),
        )
    except (Phase4ReproducibilityError, Phase3PublicEvidenceError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四公开 replay receipt：{result.receipt.receipt_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("重现失败 / 证据哈希一致", "是 / 是")
    table.add_row("receipt SHA256", result.receipt_sha256)
    table.add_row("Provider / Docker / 网络", "否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]receipt: {result.receipt_path}[/dim]")


def _phase4_chart_arguments(
    *,
    chart_bundle_id: str,
    statistics_manifest: str,
    statistics_report: str,
    statistics_manifest_sha256: str,
    statistics_report_sha256: str,
    repo_root: str,
    allow_dirty: bool,
) -> dict[str, object]:
    return {
        "chart_bundle_id": chart_bundle_id,
        "statistics_manifest_path": statistics_manifest,
        "statistics_report_path": statistics_report,
        "expected_statistics_manifest_sha256": statistics_manifest_sha256,
        "expected_statistics_report_sha256": statistics_report_sha256,
        "repo_root": repo_root,
        "allow_dirty": allow_dirty,
    }


@phase4_app.command("charts-preflight")
def phase4_charts_preflight(
    chart_bundle_id: str = typer.Option(
        "phase4_public_charts_v1",
        "--chart-bundle-id",
    ),
    statistics_manifest: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_MANIFEST,
        "--statistics-manifest",
    ),
    statistics_report: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_REPORT,
        "--statistics-report",
    ),
    statistics_manifest_sha256: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_MANIFEST_SHA256,
        "--statistics-manifest-sha256",
    ),
    statistics_report_sha256: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_REPORT_SHA256,
        "--statistics-report-sha256",
    ),
    repo_root: str = typer.Option(".", "--repo-root"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate E：只读校验聚合统计并在内存生成三个确定性 SVG。"""

    try:
        result = prepare_public_charts(
            **_phase4_chart_arguments(
                chart_bundle_id=chart_bundle_id,
                statistics_manifest=statistics_manifest,
                statistics_report=statistics_report,
                statistics_manifest_sha256=statistics_manifest_sha256,
                statistics_report_sha256=statistics_report_sha256,
                repo_root=repo_root,
                allow_dirty=allow_dirty,
            )
        )
    except (Phase4ReleaseError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四公开聚合图表只读预检：{result.manifest.chart_bundle_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "轨迹 / 方法 / 配对",
        f"{result.manifest.cohort.trace_count} / {result.manifest.cohort.method_count} / {result.manifest.cohort.pair_count}",
    )
    table.add_row("公开 SVG", str(len(result.manifest.figures)))
    table.add_row("图表 manifest SHA256", result.manifest_sha256)
    table.add_row("写入文件", "否")
    table.add_row("Provider / Docker / 网络", "否 / 否 / 否")
    console.print(table)
    console.print(
        "[yellow]图表仅含聚合数据；反事实单方法结果保持描述性，证据边界仍为 "
        "ANALYZED / CAUTION / CANNOT_VERIFY。[/yellow]"
    )


@phase4_app.command("charts-publish")
def phase4_charts_publish(
    chart_bundle_id: str = typer.Option(
        "phase4_public_charts_v1",
        "--chart-bundle-id",
    ),
    statistics_manifest: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_MANIFEST,
        "--statistics-manifest",
    ),
    statistics_report: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_REPORT,
        "--statistics-report",
    ),
    statistics_manifest_sha256: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_MANIFEST_SHA256,
        "--statistics-manifest-sha256",
    ),
    statistics_report_sha256: str = typer.Option(
        DEFAULT_PHASE4_STATISTICS_REPORT_SHA256,
        "--statistics-report-sha256",
    ),
    repo_root: str = typer.Option(".", "--repo-root"),
    output_dir: str = typer.Option(
        "docs/releases/phase4/charts",
        "--output-dir",
    ),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", hidden=True),
) -> None:
    """Gate E：原子发布三个 SVG 和一个聚合公开 manifest。"""

    try:
        result = write_public_charts(
            output_dir=output_dir,
            **_phase4_chart_arguments(
                chart_bundle_id=chart_bundle_id,
                statistics_manifest=statistics_manifest,
                statistics_report=statistics_report,
                statistics_manifest_sha256=statistics_manifest_sha256,
                statistics_report_sha256=statistics_report_sha256,
                repo_root=repo_root,
                allow_dirty=allow_dirty,
            ),
        )
    except (Phase4ReleaseError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四公开聚合图表：{result.chart_bundle_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("公开 SVG", str(len(result.figure_paths)))
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("Provider / Docker / 网络", "否 / 否 / 否")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")


@phase4_app.command("charts-verify")
def phase4_charts_verify(
    manifest: str = typer.Option(..., "--manifest"),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
) -> None:
    """Gate E：从公开 manifest 重绘并逐字节验证三个 SVG。"""

    try:
        result = verify_public_charts(
            manifest_path=manifest,
            expected_manifest_sha256=manifest_sha256,
        )
    except (Phase4ReleaseError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四公开聚合图表验证：{result.chart_bundle_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("确定性 SVG", str(result.figure_count))
    table.add_row("逐字节验证", "通过" if result.verified else "失败")
    table.add_row("Provider / Docker / 网络", "否 / 否 / 否")
    console.print(table)


def _phase4_p1_practice_arguments(
    *,
    arrangement: str,
    protocol: str,
    phase3_guide: str,
    source: str,
    coordinator_reference: str,
    cohort_manifest: str,
    natural_manifest: str,
    timeout_seconds: float,
) -> dict[str, object]:
    return {
        "arrangement_path": arrangement,
        "protocol_path": protocol,
        "phase3_guide_path": phase3_guide,
        "source_path": source,
        "coordinator_reference_path": coordinator_reference,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
        "timeout_seconds": timeout_seconds,
    }


@phase4_app.command("p1-practice-preflight")
def phase4_p1_practice_preflight(
    arrangement: str = typer.Option(DEFAULT_PHASE4_P1_ARRANGEMENT, "--arrangement"),
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    source: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_SOURCE, "--source"),
    coordinator_reference: str = typer.Option(
        DEFAULT_PHASE4_P1_COORDINATOR_REFERENCE,
        "--coordinator-reference",
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    timeout_seconds: float = typer.Option(2.0, "--timeout-seconds"),
) -> None:
    """Gate D/P1：只读构建 5 条 cohort 外公开 Fixture 练习包。"""

    try:
        result = preflight_p1_practice_bundle(
            **_phase4_p1_practice_arguments(
                arrangement=arrangement,
                protocol=protocol,
                phase3_guide=phase3_guide,
                source=source,
                coordinator_reference=coordinator_reference,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
                timeout_seconds=timeout_seconds,
            )
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 公开练习包只读预检：{result.manifest.practice_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "公开 Fixture / 执行用例",
        f"{result.manifest.item_count} / {result.manifest.executed_public_case_count}",
    )
    table.add_row("与阶段三 cohort 重合", str(result.manifest.cohort_overlap_count))
    table.add_row("练习 manifest SHA256", result.manifest_sha256)
    table.add_row("写入文件", "否")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    table.add_row(
        "伦理状态 / 正式数据收集",
        f"{result.manifest.ethics_status} / 禁止（待单次交付记录）",
    )
    console.print(table)


@phase4_app.command("p1-practice-publish")
def phase4_p1_practice_publish(
    arrangement: str = typer.Option(DEFAULT_PHASE4_P1_ARRANGEMENT, "--arrangement"),
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    source: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_SOURCE, "--source"),
    coordinator_reference: str = typer.Option(
        DEFAULT_PHASE4_P1_COORDINATOR_REFERENCE,
        "--coordinator-reference",
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    output_dir: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_OUTPUT, "--output-dir"),
    timeout_seconds: float = typer.Option(2.0, "--timeout-seconds"),
) -> None:
    """Gate D/P1：原子写入不可覆盖的公开 Fixture 练习包。"""

    try:
        result = write_p1_practice_bundle(
            output_dir=output_dir,
            **_phase4_p1_practice_arguments(
                arrangement=arrangement,
                protocol=protocol,
                phase3_guide=phase3_guide,
                source=source,
                coordinator_reference=coordinator_reference,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
                timeout_seconds=timeout_seconds,
            ),
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 公开练习包：{result.manifest.practice_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "公开 Fixture / 执行用例",
        f"{result.manifest.item_count} / {result.manifest.executed_public_case_count}",
    )
    table.add_row("与阶段三 cohort 重合", str(result.manifest.cohort_overlap_count))
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)
    console.print(f"[dim]manifest: {result.manifest_path}[/dim]")
    console.print(f"[dim]参与者文件: {result.participant_packet_path.parent}[/dim]")
    console.print(
        "[yellow]公开目录仅写入 participant/ 与 manifest；协调者参考继续保存在 "
        "Git-ignored 受限目录。伦理已批准，但单次交付记录仍待完成；本命令不授权发包。"
        "[/yellow]"
    )


@phase4_app.command("p1-practice-verify")
def phase4_p1_practice_verify(
    manifest: str = typer.Option(
        f"{DEFAULT_PHASE4_P1_PRACTICE_OUTPUT}/{P1_PRACTICE_ID}/manifest.json",
        "--manifest",
    ),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
    arrangement: str = typer.Option(DEFAULT_PHASE4_P1_ARRANGEMENT, "--arrangement"),
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    source: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_SOURCE, "--source"),
    coordinator_reference: str = typer.Option(
        DEFAULT_PHASE4_P1_COORDINATOR_REFERENCE,
        "--coordinator-reference",
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    timeout_seconds: float = typer.Option(2.0, "--timeout-seconds"),
) -> None:
    """Gate D/P1：重建并逐字节核验公开 Fixture 练习包。"""

    try:
        result = verify_p1_practice_bundle(
            manifest_path=manifest,
            expected_manifest_sha256=manifest_sha256,
            **_phase4_p1_practice_arguments(
                arrangement=arrangement,
                protocol=protocol,
                phase3_guide=phase3_guide,
                source=source,
                coordinator_reference=coordinator_reference,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
                timeout_seconds=timeout_seconds,
            ),
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 公开练习包验证：{result.practice_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "公开 Fixture / 执行用例", f"{result.item_count} / {result.executed_public_case_count}"
    )
    table.add_row("逐字节重建", "通过" if result.verified else "失败")
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-delivery-init")
def phase4_p1_delivery_init(
    schema: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_SCHEMA, "--schema"),
    record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--record"),
) -> None:
    """Gate D/P1：创建不可覆盖的 Git-ignored 私有单次交付记录模板。"""

    try:
        result = create_p1_delivery_record_template(schema_path=schema, record_path=record)
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 单次交付记录模板：{result.delivery_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("记录状态", result.record_status)
    table.add_row("缺失必填确认", str(result.missing_required_count))
    table.add_row("数据收集", "允许" if result.data_collection_allowed else "禁止")
    table.add_row("Schema SHA256", result.schema_sha256)
    table.add_row("记录 SHA256", result.record_sha256)
    table.add_row("写入范围", "仅 Git-ignored 私有模板；不发包")
    console.print(table)


@phase4_app.command("p1-delivery-preflight")
def phase4_p1_delivery_preflight(
    schema: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_SCHEMA, "--schema"),
    record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--record"),
) -> None:
    """Gate D/P1：只读检查单次交付记录和操作门。"""

    try:
        result = preflight_p1_delivery_record(schema_path=schema, record_path=record)
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 单次交付记录预检：{result.delivery_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("记录状态", result.record_status)
    table.add_row("缺失必填确认", str(result.missing_required_count))
    table.add_row("数据收集", "允许" if result.data_collection_allowed else "禁止")
    table.add_row("Schema SHA256", result.schema_sha256)
    table.add_row("记录 SHA256", result.record_sha256)
    table.add_row("读取内容回显", "无")
    console.print(table)


def _phase4_p1_formal_subset_arguments(
    *, protocol: str, cohort_manifest: str, natural_manifest: str
) -> dict[str, str]:
    return {
        "protocol_path": protocol,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
    }


@phase4_app.command("p1-formal-subset-preflight")
def phase4_p1_formal_subset_preflight(
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
) -> None:
    """Gate D/P1：只读重建正式 20 条子集，不读取标签或方法结果。"""

    try:
        result = preflight_p1_formal_subset(
            **_phase4_p1_formal_subset_arguments(
                protocol=protocol,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
            )
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式子集只读预检：{result.commitment.subset_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "自然 / 反事实 / 合计",
        f"{result.commitment.selected_natural_count} / "
        f"{result.commitment.selected_counterfactual_count} / "
        f"{result.commitment.selected_total_count}",
    )
    table.add_row(
        "反事实父题覆盖 / 单父题上限",
        f"{result.commitment.counterfactual_parent_count} / "
        f"{result.commitment.counterfactual_per_parent_maximum}",
    )
    table.add_row("私有 manifest SHA256", result.private_manifest_sha256)
    table.add_row("公开 commitment SHA256", result.public_commitment_sha256)
    table.add_row("写入文件", "否")
    table.add_row("标签 / 方法预测 / Provider 状态", "未读取 / 未读取 / 未读取")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-formal-subset-freeze")
def phase4_p1_formal_subset_freeze(
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    private_manifest: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST, "--private-manifest"
    ),
    public_commitment: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT, "--public-commitment"
    ),
) -> None:
    """Gate D/P1：不可覆盖地冻结私有 20 条身份与公开哈希承诺。"""

    try:
        result = freeze_p1_formal_subset(
            private_manifest_path=private_manifest,
            public_commitment_path=public_commitment,
            **_phase4_p1_formal_subset_arguments(
                protocol=protocol,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
            ),
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式子集冻结：{result.commitment.subset_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("自然 / 反事实 / 合计", "15 / 5 / 20")
    table.add_row("私有 manifest SHA256", result.private_manifest_sha256)
    table.add_row("公开 commitment SHA256", result.public_commitment_sha256)
    table.add_row("正式标注包 / 数据收集", "未创建 / 未开始")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)
    console.print(f"[dim]公开承诺: {result.public_commitment_path}[/dim]")


@phase4_app.command("p1-formal-subset-verify")
def phase4_p1_formal_subset_verify(
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    private_manifest: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST, "--private-manifest"
    ),
    public_commitment: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT, "--public-commitment"
    ),
    commitment_sha256: str | None = typer.Option(None, "--commitment-sha256"),
) -> None:
    """Gate D/P1：逐字节重建并验证私有正式子集和公开承诺。"""

    try:
        result = verify_p1_formal_subset(
            private_manifest_path=private_manifest,
            public_commitment_path=public_commitment,
            expected_public_commitment_sha256=commitment_sha256,
            **_phase4_p1_formal_subset_arguments(
                protocol=protocol,
                cohort_manifest=cohort_manifest,
                natural_manifest=natural_manifest,
            ),
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式子集验证：{result.subset_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("逐字节重建", "通过" if result.verified else "失败")
    table.add_row("正式子集条数", str(result.selected_total_count))
    table.add_row("私有 manifest SHA256", result.private_manifest_sha256)
    table.add_row("公开 commitment SHA256", result.public_commitment_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-practice-admission-freeze")
def phase4_p1_practice_admission_freeze(
    completed_labels: str = typer.Option(..., "--completed-labels"),
    returned_archive_sha256: str = typer.Option(..., "--returned-archive-sha256"),
    public_evidence_rationales_confirmed: bool = typer.Option(
        False,
        "--public-evidence-rationales-confirmed",
        help="协调者已确认 5 条 rationale 只使用参与者可见的公开证据",
    ),
    coordinator_written_authorization_confirmed: bool = typer.Option(
        False,
        "--coordinator-written-authorization-confirmed",
        help="已向标注者发出“准入正式 20 条”的书面通知",
    ),
    privacy_or_blinding_violation_count: int = typer.Option(
        0, "--privacy-or-blinding-violation-count", min=0
    ),
    output: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_ADMISSION, "--output"),
    arrangement: str = typer.Option(DEFAULT_PHASE4_P1_ARRANGEMENT, "--arrangement"),
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    source: str = typer.Option(DEFAULT_PHASE4_P1_PRACTICE_SOURCE, "--source"),
    coordinator_reference: str = typer.Option(
        DEFAULT_PHASE4_P1_COORDINATOR_REFERENCE, "--coordinator-reference"
    ),
) -> None:
    """Gate D/P1：根据 5 条练习结果固化私有准入决定。"""

    try:
        result = write_p1_practice_admission(
            completed_labels_path=completed_labels,
            returned_archive_sha256=returned_archive_sha256,
            public_evidence_only_rationales_confirmed=(public_evidence_rationales_confirmed),
            coordinator_written_authorization_confirmed=(
                coordinator_written_authorization_confirmed
            ),
            privacy_or_blinding_violation_count=privacy_or_blinding_violation_count,
            output_path=output,
            arrangement_path=arrangement,
            protocol_path=protocol,
            phase3_guide_path=phase3_guide,
            source_path=source,
            coordinator_reference_path=coordinator_reference,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 练习准入：{result.record.admission_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("Schema 有效", f"{result.record.schema_valid_count} / 5")
    table.add_row(
        "has_error / process_correct",
        f"{result.record.has_error_exact_agreement_count} / 5; "
        f"{result.record.process_correct_exact_agreement_count} / 5",
    )
    table.add_row(
        "首错层（错误条目）",
        f"{result.record.error_item_first_faulty_layer_exact_agreement_count} / 3",
    )
    table.add_row("隐私/盲法违规", str(result.record.privacy_or_blinding_violation_count))
    table.add_row("准入决定", "通过，准入正式 20 条")
    table.add_row("准入记录 SHA256", result.record_sha256)
    table.add_row("练习结果用途", "仅校准，排除于研究终点")
    console.print(table)
    console.print(f"[dim]私有准入记录: {result.record_path}[/dim]")


def _phase4_p1_formal_packet_arguments(
    *,
    protocol: str,
    phase3_guide: str,
    delivery_schema: str,
    delivery_record: str,
    practice_admission: str,
    private_manifest: str,
    public_commitment: str,
    cohort_manifest: str,
    natural_manifest: str,
    phase1_run: str,
    phase2_run: str,
    dataset_manifest: str,
    source_bundle: str,
    execution_run: str,
    output_dir: str,
) -> dict[str, str]:
    return {
        "protocol_path": protocol,
        "phase3_guide_path": phase3_guide,
        "delivery_schema_path": delivery_schema,
        "delivery_record_path": delivery_record,
        "practice_admission_path": practice_admission,
        "formal_subset_manifest_path": private_manifest,
        "formal_subset_commitment_path": public_commitment,
        "cohort_manifest_path": cohort_manifest,
        "natural_manifest_path": natural_manifest,
        "phase1_run_dir": phase1_run,
        "phase2_run_dir": phase2_run,
        "dataset_manifest_path": dataset_manifest,
        "source_bundle_path": source_bundle,
        "execution_run_dir": execution_run,
        "output_dir": output_dir,
    }


def _phase4_p1_formal_packet_options(
    *,
    protocol: str,
    phase3_guide: str,
    delivery_schema: str,
    delivery_record: str,
    practice_admission: str,
    private_manifest: str,
    public_commitment: str,
    cohort_manifest: str,
    natural_manifest: str,
    phase1_run: str,
    phase2_run: str,
    dataset_manifest: str,
    source_bundle: str,
    execution_run: str,
    output_dir: str,
) -> dict[str, str]:
    """Keep CLI wrappers explicit while sharing the exact argument mapping."""

    return _phase4_p1_formal_packet_arguments(
        protocol=protocol,
        phase3_guide=phase3_guide,
        delivery_schema=delivery_schema,
        delivery_record=delivery_record,
        practice_admission=practice_admission,
        private_manifest=private_manifest,
        public_commitment=public_commitment,
        cohort_manifest=cohort_manifest,
        natural_manifest=natural_manifest,
        phase1_run=phase1_run,
        phase2_run=phase2_run,
        dataset_manifest=dataset_manifest,
        source_bundle=source_bundle,
        execution_run=execution_run,
        output_dir=output_dir,
    )


@phase4_app.command("p1-formal-packet-preflight")
def phase4_p1_formal_packet_preflight(
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    delivery_schema: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_SCHEMA, "--delivery-schema"),
    delivery_record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--delivery-record"),
    practice_admission: str = typer.Option(
        DEFAULT_PHASE4_P1_PRACTICE_ADMISSION, "--practice-admission"
    ),
    private_manifest: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST, "--private-manifest"
    ),
    public_commitment: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT, "--public-commitment"
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    phase1_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE1_RUN, "--phase1-run"),
    phase2_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE2_RUN, "--phase2-run"),
    dataset_manifest: str = typer.Option(DEFAULT_PHASE4_P1_DATASET_MANIFEST, "--dataset-manifest"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    execution_run: str = typer.Option(DEFAULT_PHASE4_P1_EXECUTION_RUN, "--execution-run"),
    output_dir: str = typer.Option(DEFAULT_PHASE4_P1_FORMAL_PACKET_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：硬门禁后只读构建正式 20 条盲化包。"""

    try:
        result = preflight_p1_formal_packet(**_phase4_p1_formal_packet_options(**locals()))
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式包只读预检：{result.manifest.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("自然 / 反事实 / 合计", "15 / 5 / 20")
    table.add_row("参与者 packet SHA256", result.participant_packet_sha256)
    table.add_row("空白标签模板 SHA256", result.participant_labels_template_sha256)
    table.add_row("协调者身份映射 SHA256", result.coordinator_identity_map_sha256)
    table.add_row("写入文件", "否")
    table.add_row("主标签 / 方法预测 / Provider / 网络", "未读取 / 未读取 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-formal-packet-export")
def phase4_p1_formal_packet_export(
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    delivery_schema: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_SCHEMA, "--delivery-schema"),
    delivery_record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--delivery-record"),
    practice_admission: str = typer.Option(
        DEFAULT_PHASE4_P1_PRACTICE_ADMISSION, "--practice-admission"
    ),
    private_manifest: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST, "--private-manifest"
    ),
    public_commitment: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT, "--public-commitment"
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    phase1_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE1_RUN, "--phase1-run"),
    phase2_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE2_RUN, "--phase2-run"),
    dataset_manifest: str = typer.Option(DEFAULT_PHASE4_P1_DATASET_MANIFEST, "--dataset-manifest"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    execution_run: str = typer.Option(DEFAULT_PHASE4_P1_EXECUTION_RUN, "--execution-run"),
    output_dir: str = typer.Option(DEFAULT_PHASE4_P1_FORMAL_PACKET_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：门禁通过后原子导出私有正式盲化包。"""

    try:
        result = write_p1_formal_packet(**_phase4_p1_formal_packet_options(**locals()))
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式包：{result.manifest.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("自然 / 反事实 / 合计", "15 / 5 / 20")
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("参与者目录", str(result.participant_packet_path.parent))
    table.add_row("身份映射", "另存 coordinator/，不得发送")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-formal-packet-verify")
def phase4_p1_formal_packet_verify(
    manifest: str = typer.Option(
        f"{DEFAULT_PHASE4_P1_FORMAL_PACKET_OUTPUT}/{P1_FORMAL_PACKET_ID}/manifest.json",
        "--manifest",
    ),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
    protocol: str = typer.Option(DEFAULT_PHASE4_P1_PROTOCOL, "--protocol"),
    phase3_guide: str = typer.Option(DEFAULT_PHASE3_ANNOTATION_GUIDE, "--phase3-guide"),
    delivery_schema: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_SCHEMA, "--delivery-schema"),
    delivery_record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--delivery-record"),
    practice_admission: str = typer.Option(
        DEFAULT_PHASE4_P1_PRACTICE_ADMISSION, "--practice-admission"
    ),
    private_manifest: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PRIVATE_MANIFEST, "--private-manifest"
    ),
    public_commitment: str = typer.Option(
        DEFAULT_PHASE4_P1_FORMAL_PUBLIC_COMMITMENT, "--public-commitment"
    ),
    cohort_manifest: str = typer.Option(DEFAULT_PHASE4_COHORT_MANIFEST, "--cohort-manifest"),
    natural_manifest: str = typer.Option(DEFAULT_PHASE4_NATURAL_MANIFEST, "--natural-manifest"),
    phase1_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE1_RUN, "--phase1-run"),
    phase2_run: str = typer.Option(DEFAULT_PHASE4_P1_PHASE2_RUN, "--phase2-run"),
    dataset_manifest: str = typer.Option(DEFAULT_PHASE4_P1_DATASET_MANIFEST, "--dataset-manifest"),
    source_bundle: str = typer.Option(DEFAULT_PHASE4_SOURCE_BUNDLE, "--source-bundle"),
    execution_run: str = typer.Option(DEFAULT_PHASE4_P1_EXECUTION_RUN, "--execution-run"),
    output_dir: str = typer.Option(DEFAULT_PHASE4_P1_FORMAL_PACKET_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：重建并逐字节核验正式 20 条盲化包。"""

    options = locals().copy()
    options.pop("manifest")
    options.pop("manifest_sha256")
    try:
        result = verify_p1_formal_packet(
            manifest_path=manifest,
            expected_manifest_sha256=manifest_sha256,
            **_phase4_p1_formal_packet_options(**options),
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式包验证：{result.packet_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("逐字节重建", "通过" if result.verified else "失败")
    table.add_row("条数", str(result.item_count))
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


def _parse_timezone_aware_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise Phase4P1AnnotationError(
            f"{label} must be an ISO 8601 timestamp",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        ) from None
    if parsed.tzinfo is None:
        raise Phase4P1AnnotationError(
            f"{label} must include a UTC offset",
            safe_stage="P4D_P1_FORMAL_LABELS_RECEIPT",
        )
    return parsed


def _phase4_p1_formal_labels_options(
    *,
    completed_labels: str,
    returned_archive: str,
    received_at: str,
    archive_extraction_confirmed: bool,
    packet_dir: str,
    packet_manifest_sha256: str,
    delivery_record: str,
    output_dir: str,
) -> dict[str, object]:
    return {
        "completed_labels_path": completed_labels,
        "returned_archive_path": returned_archive,
        "received_at": _parse_timezone_aware_datetime(received_at, label="received_at"),
        "archive_extraction_binding_confirmed": archive_extraction_confirmed,
        "packet_dir": packet_dir,
        "expected_packet_manifest_sha256": packet_manifest_sha256,
        "delivery_record_path": delivery_record,
        "output_dir": output_dir,
    }


@phase4_app.command("p1-formal-labels-preflight")
def phase4_p1_formal_labels_preflight(
    completed_labels: str = typer.Option(..., "--completed-labels"),
    returned_archive: str = typer.Option(..., "--returned-archive"),
    received_at: str = typer.Option(..., "--received-at"),
    archive_extraction_confirmed: bool = typer.Option(
        False,
        "--archive-extraction-confirmed",
        help="协调者确认 completed labels 来自该回传归档",
    ),
    packet_dir: str = typer.Option(P1_FORMAL_PACKET_DEFAULT_DIR, "--packet-dir"),
    packet_manifest_sha256: str = typer.Option(
        P1_FORMAL_PACKET_MANIFEST_SHA256, "--packet-manifest-sha256"
    ),
    delivery_record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--delivery-record"),
    output_dir: str = typer.Option(P1_FORMAL_LABELS_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：只读校验正式 20 条回传、交付证据和盲化绑定。"""

    try:
        result = preflight_p1_formal_labels(**_phase4_p1_formal_labels_options(**locals()))
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式回传预检：{result.label_set_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("完成 / 预期", f"{result.completed_count} / {result.record_count}")
    table.add_row(
        "有错误 / 无错误", f"{result.has_error_true_count} / {result.has_error_false_count}"
    )
    table.add_row("按时收到", "是" if result.received_within_formal_deadline else "否")
    table.add_row("回传 archive SHA256", result.source_archive_sha256)
    table.add_row("完成标签 SHA256", result.source_completed_labels_sha256)
    table.add_row("可冻结", "是" if result.ready_to_freeze else "否")
    table.add_row("写入文件", "否")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-formal-labels-freeze")
def phase4_p1_formal_labels_freeze(
    completed_labels: str = typer.Option(..., "--completed-labels"),
    returned_archive: str = typer.Option(..., "--returned-archive"),
    received_at: str = typer.Option(..., "--received-at"),
    archive_extraction_confirmed: bool = typer.Option(
        False,
        "--archive-extraction-confirmed",
        help="协调者确认 completed labels 来自该回传归档",
    ),
    packet_dir: str = typer.Option(P1_FORMAL_PACKET_DEFAULT_DIR, "--packet-dir"),
    packet_manifest_sha256: str = typer.Option(
        P1_FORMAL_PACKET_MANIFEST_SHA256, "--packet-manifest-sha256"
    ),
    delivery_record: str = typer.Option(DEFAULT_PHASE4_P1_DELIVERY_RECORD, "--delivery-record"),
    output_dir: str = typer.Option(P1_FORMAL_LABELS_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：原子冻结正式回传、身份回连记录和原始归档。"""

    try:
        result = freeze_p1_formal_labels(**_phase4_p1_formal_labels_options(**locals()))
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式标签冻结：{result.label_set_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("正式标签", f"{result.completed_count} / {result.record_count}")
    table.add_row("按时收到", "是")
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("回传 archive SHA256", result.source_archive_sha256)
    table.add_row("完成标签 SHA256", result.completed_labels_sha256)
    table.add_row("私有冻结目录", str(result.run_dir))
    table.add_row("一致性统计", "尚未计算")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-formal-labels-verify")
def phase4_p1_formal_labels_verify(
    manifest: str = typer.Option(P1_FORMAL_LABELS_DEFAULT_MANIFEST, "--manifest"),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
) -> None:
    """Gate D/P1：验证已冻结正式回传的 schema、权限和逐文件哈希。"""

    try:
        result = verify_p1_formal_labels(
            manifest_path=manifest, expected_manifest_sha256=manifest_sha256
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 正式标签验证：{result.label_set_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("验证", "通过" if result.verified else "失败")
    table.add_row("条数", str(result.record_count))
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("回传 archive SHA256", result.source_archive_sha256)
    table.add_row("完成标签 SHA256", result.completed_labels_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


def _render_p1_agreement_summary(
    *,
    title: str,
    analysis: P1InterRaterAgreementAnalysis,
    analysis_sha256: str,
    report_sha256: str,
) -> None:
    has_error = next(item for item in analysis.binary_fields if item.field_name == "has_error")
    table = Table(title=title)
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("配对条目", str(analysis.item_count))
    table.add_row(
        "has_error 原始一致率",
        f"{has_error.raw_agreement.agreeing_count}/{has_error.raw_agreement.denominator} "
        f"({has_error.raw_agreement.estimate:.1%})",
    )
    table.add_row(
        "has_error Cohen's κ",
        (f"{has_error.cohen_kappa:.3f}" if has_error.cohen_kappa is not None else "N/A"),
    )
    table.add_row("原始标签已改写", "否")
    table.add_row("分歧清单已输出", "否")
    table.add_row("analysis SHA256", analysis_sha256)
    table.add_row("report SHA256", report_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-agreement-preflight")
def phase4_p1_agreement_preflight(
    primary_manifest: str = typer.Option(P1_PRIMARY_LABELS_DEFAULT_MANIFEST, "--primary-manifest"),
    secondary_manifest: str = typer.Option(
        P1_FORMAL_LABELS_DEFAULT_MANIFEST, "--secondary-manifest"
    ),
    output_dir: str = typer.Option(P1_AGREEMENT_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：只读复算 20 条两位标注者的聚合一致性。"""

    try:
        result = preflight_p1_agreement(
            primary_manifest_path=primary_manifest,
            secondary_manifest_path=secondary_manifest,
            output_dir=output_dir,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    _render_p1_agreement_summary(
        title="阶段四 P1 一致性分析预检（未写盘）",
        analysis=result.analysis,
        analysis_sha256=result.analysis_sha256,
        report_sha256=result.report_sha256,
    )


@phase4_app.command("p1-agreement-publish")
def phase4_p1_agreement_publish(
    primary_manifest: str = typer.Option(P1_PRIMARY_LABELS_DEFAULT_MANIFEST, "--primary-manifest"),
    secondary_manifest: str = typer.Option(
        P1_FORMAL_LABELS_DEFAULT_MANIFEST, "--secondary-manifest"
    ),
    output_dir: str = typer.Option(P1_AGREEMENT_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：原子冻结聚合一致性 JSON、报告和来源哈希。"""

    try:
        result = publish_p1_agreement(
            primary_manifest_path=primary_manifest,
            secondary_manifest_path=secondary_manifest,
            output_dir=output_dir,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    _render_p1_agreement_summary(
        title="阶段四 P1 一致性分析已冻结",
        analysis=result.analysis,
        analysis_sha256=result.analysis_sha256,
        report_sha256=result.report_sha256,
    )
    console.print(f"私有聚合目录：{result.run_dir}")
    console.print(f"manifest SHA256：{result.manifest_sha256}")


@phase4_app.command("p1-agreement-verify")
def phase4_p1_agreement_verify(
    manifest: str = typer.Option(P1_AGREEMENT_DEFAULT_MANIFEST, "--manifest"),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
    primary_manifest: str = typer.Option(P1_PRIMARY_LABELS_DEFAULT_MANIFEST, "--primary-manifest"),
    secondary_manifest: str = typer.Option(
        P1_FORMAL_LABELS_DEFAULT_MANIFEST, "--secondary-manifest"
    ),
) -> None:
    """Gate D/P1：逐哈希验证并从两份冻结标签确定性复算。"""

    try:
        result = verify_p1_agreement(
            manifest_path=manifest,
            expected_manifest_sha256=manifest_sha256,
            primary_manifest_path=primary_manifest,
            secondary_manifest_path=secondary_manifest,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 一致性验证：{result.analysis_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("验证", "通过" if result.verified else "失败")
    table.add_row("条数", str(result.item_count))
    table.add_row("manifest SHA256", result.manifest_sha256)
    table.add_row("analysis SHA256", result.analysis_sha256)
    table.add_row("report SHA256", result.report_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


def _render_p1_adjudication_summary(
    *,
    title: str,
    status: str,
    record_sha256: str,
    working_template_sha256: str,
    instructions_sha256: str,
) -> None:
    table = Table(title=title)
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("状态", status)
    table.add_row("完整记录分歧", "1 / 20")
    table.add_row("has_error 分歧", "0 / 20")
    table.add_row("已访问逐条原始标签", "否")
    table.add_row("已写入裁决结论", "否")
    table.add_row("pending record SHA256", record_sha256)
    table.add_row("working template SHA256", working_template_sha256)
    table.add_row("instructions SHA256", instructions_sha256)
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    console.print(table)


@phase4_app.command("p1-adjudication-preflight")
def phase4_p1_adjudication_preflight(
    agreement_manifest: str = typer.Option(P1_AGREEMENT_DEFAULT_MANIFEST, "--agreement-manifest"),
    output_dir: str = typer.Option(P1_ADJUDICATION_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：仅从聚合一致性包预检单条分歧待裁决记录。"""

    try:
        result = preflight_p1_adjudication(
            agreement_manifest_path=agreement_manifest,
            output_dir=output_dir,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    _render_p1_adjudication_summary(
        title="阶段四 P1 单条分歧裁决记录预检（未写盘）",
        status=result.record.status,
        record_sha256=result.record_sha256,
        working_template_sha256=result.working_template_sha256,
        instructions_sha256=result.instructions_sha256,
    )


@phase4_app.command("p1-adjudication-init")
def phase4_p1_adjudication_init(
    agreement_manifest: str = typer.Option(P1_AGREEMENT_DEFAULT_MANIFEST, "--agreement-manifest"),
    output_dir: str = typer.Option(P1_ADJUDICATION_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：初始化不可覆盖、未含逐条数据的待人工裁决记录。"""

    try:
        result = initialize_p1_adjudication(
            agreement_manifest_path=agreement_manifest,
            output_dir=output_dir,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    _render_p1_adjudication_summary(
        title="阶段四 P1 单条分歧待裁决记录已初始化",
        status=result.record.status,
        record_sha256=result.record_sha256,
        working_template_sha256=result.working_template_sha256,
        instructions_sha256=result.instructions_sha256,
    )
    console.print(f"私有目录：{result.run_dir}")
    console.print(f"manifest SHA256：{result.manifest_sha256}")


@phase4_app.command("p1-adjudication-verify")
def phase4_p1_adjudication_verify(
    manifest: str = typer.Option(P1_ADJUDICATION_DEFAULT_MANIFEST, "--manifest"),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
) -> None:
    """Gate D/P1：验证待裁决记录的私有权限、Schema 和逐文件哈希。"""

    try:
        result = verify_p1_adjudication(
            manifest_path=manifest, expected_manifest_sha256=manifest_sha256
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    _render_p1_adjudication_summary(
        title=f"阶段四 P1 单条分歧裁决记录验证：{result.bundle_id}",
        status=result.status,
        record_sha256=result.record_sha256,
        working_template_sha256=result.working_template_sha256,
        instructions_sha256=result.instructions_sha256,
    )


def _parse_boolean_decision(value: str, *, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise Phase4P1AnnotationError(
        f"{label} must be true or false",
        safe_stage="P4D_P1_ADJUDICATION_COMPLETE_INPUT",
    )


@phase4_app.command("p1-adjudication-consensus-complete")
def phase4_p1_adjudication_consensus_complete(
    annotation_item_id: str = typer.Option(..., "--annotation-item-id"),
    plan_code_aligned: str = typer.Option(..., "--plan-code-aligned"),
    first_faulty_layer: str = typer.Option(..., "--first-faulty-layer"),
    first_faulty_step: str = typer.Option(..., "--first-faulty-step"),
    error_type: str = typer.Option(..., "--error-type"),
    rationale: str = typer.Option(..., "--rationale"),
    started_at: str = typer.Option(..., "--started-at"),
    completed_at: str = typer.Option(..., "--completed-at"),
    both_confirmed: bool = typer.Option(False, "--both-confirmed"),
    method_blinding_confirmed: bool = typer.Option(False, "--method-blinding-confirmed"),
    pending_manifest: str = typer.Option(P1_ADJUDICATION_DEFAULT_MANIFEST, "--pending-manifest"),
    formal_packet_manifest: str = typer.Option(
        f"{P1_FORMAL_PACKET_DEFAULT_DIR}/manifest.json", "--formal-packet-manifest"
    ),
    output_dir: str = typer.Option(P1_ADJUDICATION_DEFAULT_OUTPUT, "--output-dir"),
) -> None:
    """Gate D/P1：追加冻结两位原始标注者记录在案的单条共识。"""

    try:
        result = complete_p1_consensus_adjudication(
            annotation_item_id=annotation_item_id,
            plan_code_aligned=_parse_boolean_decision(plan_code_aligned, label="plan_code_aligned"),
            first_faulty_layer=first_faulty_layer,
            first_faulty_step=first_faulty_step,
            error_type=error_type,
            decision_rationale=rationale,
            adjudication_started_at=_parse_timezone_aware_datetime(
                started_at, label="adjudication_started_at"
            ),
            adjudication_completed_at=_parse_timezone_aware_datetime(
                completed_at, label="adjudication_completed_at"
            ),
            both_original_raters_confirmed=both_confirmed,
            adjudicators_blinded_to_method_predictions=method_blinding_confirmed,
            pending_manifest_path=pending_manifest,
            formal_packet_manifest_path=formal_packet_manifest,
            output_dir=output_dir,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title="阶段四 P1 单条分歧人类共识已追加冻结")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("状态", result.record.status)
    table.add_row("盲化条目", result.record.annotation_item_id)
    table.add_row("裁决模式", result.record.adjudication_mode)
    table.add_row("双方确认 / 方法预测盲法", "是 / 是")
    table.add_row("原标签 / raw agreement 已覆盖", "否 / 否")
    table.add_row("decision SHA256", result.decision_sha256)
    table.add_row("report SHA256", result.report_sha256)
    console.print(table)
    console.print(f"私有目录：{result.run_dir}")
    console.print(f"manifest SHA256：{result.manifest_sha256}")


@phase4_app.command("p1-adjudication-completed-verify")
def phase4_p1_adjudication_completed_verify(
    manifest: str = typer.Option(P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST, "--manifest"),
    manifest_sha256: str | None = typer.Option(None, "--manifest-sha256"),
) -> None:
    """Gate D/P1：验证追加完成的共识记录及其来源绑定。"""

    try:
        result = verify_p1_completed_adjudication(
            manifest_path=manifest,
            expected_manifest_sha256=manifest_sha256,
        )
    except (Phase4P1AnnotationError, OSError, ValueError) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 完成态裁决验证：{result.bundle_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("状态", result.status)
    table.add_row("盲化条目", result.annotation_item_id)
    table.add_row("decision SHA256", result.decision_sha256)
    table.add_row("report SHA256", result.report_sha256)
    table.add_row("验证", "通过" if result.verified else "失败")
    console.print(table)


@phase4_app.command("p1-adjudication-sensitivity-publish")
def phase4_p1_adjudication_sensitivity_publish(
    agreement_manifest: str = typer.Option(P1_AGREEMENT_DEFAULT_MANIFEST, "--agreement-manifest"),
    completed_adjudication_manifest: str = typer.Option(
        P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
        "--completed-adjudication-manifest",
    ),
    output_dir: str = typer.Option(
        P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT,
        "--output-dir",
    ),
) -> None:
    """Gate D/P1：发布不读取逐条原标签的裁决后影响上界报告。"""

    try:
        result = publish_p1_post_adjudication_sensitivity(
            agreement_manifest_path=agreement_manifest,
            completed_adjudication_manifest_path=completed_adjudication_manifest,
            output_dir=output_dir,
        )
    except (
        P1PostAdjudicationSensitivityError,
        Phase4P1AnnotationError,
        OSError,
        ValueError,
    ) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    binary = {item.field_name: item for item in result.analysis.raw_binary_fields}
    table = Table(title="阶段四 P1 裁决后敏感性分析")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row(
        "原始完整记录一致率",
        f"{result.analysis.raw_full_record_exact_agreement.agreeing_count}/20（保持不变）",
    )
    table.add_row(
        "原始 has_error 一致率",
        f"{binary['has_error'].raw_agreement.agreeing_count}/20（保持不变）",
    )
    table.add_row("分歧解决", "1 / 1；不是新的 20/20 一致率")
    table.add_row("固定 20 条最大影响", "计划/定位类指标 ≤ 1 条，即 5.0 pp")
    table.add_row("固定 6 条定位分母最大影响", "≤ 1 条，即 16.7 pp")
    table.add_row("精确方法分数变化", "未计算；未读取方法预测或逐条原标签")
    table.add_row("Provider / Docker / 网络", "0 / 0 / 0")
    table.add_row("JSON SHA256", result.json_sha256)
    table.add_row("Markdown SHA256", result.markdown_sha256)
    console.print(table)
    console.print(f"[dim]report: {result.markdown_path}[/dim]")


@phase4_app.command("p1-adjudication-sensitivity-verify")
def phase4_p1_adjudication_sensitivity_verify(
    agreement_manifest: str = typer.Option(P1_AGREEMENT_DEFAULT_MANIFEST, "--agreement-manifest"),
    completed_adjudication_manifest: str = typer.Option(
        P1_ADJUDICATION_COMPLETED_DEFAULT_MANIFEST,
        "--completed-adjudication-manifest",
    ),
    output_dir: str = typer.Option(
        P1_POST_ADJUDICATION_SENSITIVITY_DEFAULT_OUTPUT,
        "--output-dir",
    ),
    json_sha256: str | None = typer.Option(None, "--json-sha256"),
    markdown_sha256: str | None = typer.Option(None, "--markdown-sha256"),
) -> None:
    """Gate D/P1：从两个固定聚合来源重建并验证敏感性报告。"""

    try:
        result = verify_p1_post_adjudication_sensitivity(
            agreement_manifest_path=agreement_manifest,
            completed_adjudication_manifest_path=completed_adjudication_manifest,
            output_dir=output_dir,
            expected_json_sha256=json_sha256,
            expected_markdown_sha256=markdown_sha256,
        )
    except (
        P1PostAdjudicationSensitivityError,
        Phase4P1AnnotationError,
        OSError,
        ValueError,
    ) as exc:
        _render_phase4_failure(exc)
        raise typer.Exit(code=1) from exc
    table = Table(title=f"阶段四 P1 裁决后敏感性验证：{result.analysis_id}")
    table.add_column("项目")
    table.add_column("结果")
    table.add_row("JSON SHA256", result.json_sha256)
    table.add_row("Markdown SHA256", result.markdown_sha256)
    table.add_row("确定性重建", "通过" if result.verified else "失败")
    table.add_row("逐条原标签 / 方法预测访问", "0 / 0")
    console.print(table)


def _render_result(result: PipelineResult) -> None:
    problem = result.problem
    solution = result.solution
    assessment = result.process_assessment

    console.print(
        Panel(
            f"[bold]{problem.title}[/bold]\n{problem.requirement}",
            title=f"题目 {problem.problem_id}",
        )
    )

    steps_text = "\n".join(
        f"[{s.step_id}] {s.content} (关联需求: {', '.join(s.related_requirements) or '无'})"
        for s in solution.implementation_steps
    )
    console.print(
        Panel(
            f"[bold]需求理解[/bold]\n{solution.requirement_understanding}\n\n"
            f"[bold]设计摘要[/bold]\n{solution.design_summary}\n\n"
            f"[bold]实现步骤[/bold]\n{steps_text}\n\n"
            f"[bold]复杂度声明[/bold] 时间: {solution.declared_time_complexity} / 空间: {solution.declared_space_complexity}",
            title="解题说明 (SolutionTrace)",
        )
    )
    console.print(Syntax(solution.code, "python", theme="ansi_dark", line_numbers=True))

    se = result.static_evidence
    static_table = Table(title="静态证据 (AST)")
    static_table.add_column("字段")
    static_table.add_column("值")
    static_table.add_row("if 数量", str(se.if_count))
    static_table.add_row(
        "循环数量 / 最大嵌套深度", f"{se.loop_count} / {se.max_loop_nesting_depth}"
    )
    static_table.add_row("使用的数据结构", ", ".join(se.data_structures_used) or "无")
    static_table.add_row("空输入判断分支", "存在" if se.has_empty_input_check else "未发现")
    static_table.add_row("可疑硬编码", "是" if se.suspicious_hardcoding else "否")
    console.print(static_table)

    test_table = Table(title="测试执行结果")
    test_table.add_column("用例")
    test_table.add_column("类别")
    test_table.add_column("结果")
    test_table.add_column("期望")
    test_table.add_column("实际/异常")
    for r in result.execution_result.results:
        outcome = "PASS" if r.passed else "FAIL"
        actual = str(r.exception_type) if r.exception_type else str(r.actual_output)
        test_table.add_row(r.case_id, r.category, outcome, str(r.expected_output), actual)
    console.print(test_table)
    if result.execution_result.runtime_status != "completed":
        console.print(
            f"[red]沙盒未正常完成：{result.execution_result.runtime_status} - {result.execution_result.setup_error}[/red]"
        )

    process_text = (
        f"functional_correct: {assessment.functional_correct}\n"
        f"reasoning_correct: {assessment.reasoning_correct}\n"
        f"plan_code_aligned: {assessment.plan_code_aligned}\n"
        f"process_correct: {assessment.process_correct}\n"
        f"first_faulty_layer: {assessment.first_faulty_layer}\n"
        f"first_faulty_step: {assessment.first_faulty_step}\n"
        f"violated_requirement: {assessment.violated_requirement}\n"
        f"code_span: {assessment.code_span}\n"
        f"error_type: {assessment.error_type.value if assessment.error_type else None}\n"
        f"confidence: {assessment.confidence}\n\n"
        f"说明: {assessment.explanation}"
    )
    console.print(Panel(process_text, title="过程评估结论"))

    if result.counterexample:
        ce = result.counterexample
        console.print(
            Panel(
                f"来源: {ce.source} (minimized={ce.minimized})\n"
                f"args={ce.args} kwargs={ce.kwargs}\n"
                f"expected={ce.expected!r}\n"
                f"candidate_output={ce.candidate_output!r} candidate_exception={ce.candidate_exception}\n"
                f"reference_output={ce.reference_output!r} reference_exception={ce.reference_exception}",
                title="反例 (Counterexample)",
                style="red",
            )
        )

    if result.error_certificate:
        cert = result.error_certificate
        console.print(
            Panel(
                f"verdict: [bold]{cert.verdict}[/bold]\n"
                f"error_type: {cert.error_type.value if cert.error_type else None}\n"
                f"first_faulty_layer: {cert.first_faulty_layer}\n"
                f"first_faulty_step: {cert.first_faulty_step}\n"
                f"violated_requirement: {cert.violated_requirement}\n"
                f"supporting_evidence:\n- " + "\n- ".join(cert.supporting_evidence),
                title="错误证书 (ErrorCertificate)",
                style="bold red" if cert.verdict == "confirmed_bug" else "yellow",
            )
        )
    else:
        console.print(
            Panel(
                "未发现需要生成错误证书的疑似问题。",
                title="错误证书 (ErrorCertificate)",
                style="green",
            )
        )


def _safe_demo_output_path(output: str | None, *, case: str) -> Path:
    if output is None:
        if Path("artifacts").is_symlink():
            raise typer.BadParameter("Demo 输出目录不能是符号链接")
        return timestamped_artifact_path(Path("artifacts"), f"demo_{case}")
    if "\\" in output:
        raise typer.BadParameter("--output 必须是 artifacts/ 下的规范仓库相对路径")
    raw_parts = output.split("/")
    relative = PurePosixPath(output)
    if (
        relative.is_absolute()
        or not raw_parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or relative.parts[0] != "artifacts"
        or relative.suffix != ".json"
    ):
        raise typer.BadParameter("--output 必须是 artifacts/ 下的规范仓库相对 JSON 路径")
    path = Path(*relative.parts)
    for parent in (path.parent, *path.parents):
        if parent == Path("."):
            break
        if parent.exists() and parent.is_symlink():
            raise typer.BadParameter("Demo 输出路径不能经过符号链接")
    return path


@app.command()
def demo(
    mock: bool = typer.Option(
        True, "--mock/--no-mock", help="v0.1 仅支持 Mock Demo；--no-mock 会报错退出"
    ),
    case: str = typer.Option("faulty", "--case", help="safe_mean 演示场景：'correct' 或 'faulty'"),
    output: str | None = typer.Option(
        None,
        "--output",
        help="可选的 artifacts/ 下规范仓库相对 JSON 路径；默认使用 UTC 时间戳",
    ),
    per_test_timeout_seconds: float = typer.Option(
        5.0,
        "--per-test-timeout-seconds",
        min=0.1,
        max=10.0,
        help="公开 Mock Fixture 单用例父进程超时；不会读取 .env",
    ),
) -> None:
    """运行内置 safe_mean 场景的完整链路演示（默认 faulty：可见测试通过但隐藏/挑战测试暴露空输入缺陷）。"""

    if not mock:
        console.print(
            "[red]v0.1 的 demo 命令只支持 --mock。若要用真实 Hy3，请使用 "
            "`tracejudge run --provider hy3 --problem-id <id>`。[/red]"
        )
        raise typer.Exit(code=1)
    if case not in ("correct", "faulty"):
        raise typer.BadParameter("--case 必须是 'correct' 或 'faulty'")

    out_path = _safe_demo_output_path(output, case=case)
    if out_path.exists() or out_path.is_symlink():
        raise typer.BadParameter("Demo 输出已存在，拒绝覆盖")
    try:
        problem = load_problem_by_id(DEFAULT_DATASET, "safe_mean")
    except TraceJudgeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    provider = MockProvider(case=case)
    backend = TrustedLocalSandbox(per_test_timeout_seconds=per_test_timeout_seconds)

    result = asyncio.run(run_pipeline(problem, provider, backend))
    _render_result(result)

    save_result_json(result, out_path)
    console.print(f"\n[dim]完整结果 JSON 已保存到 {out_path.as_posix()}[/dim]")


@app.command()
def run(
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset"),
    problem_id: str = typer.Option(..., "--problem-id"),
    provider: str = typer.Option("mock", "--provider", help="'mock' 或 'hy3'"),
    sandbox: str | None = typer.Option(
        None,
        "--sandbox",
        help="'docker' 或 'trusted-local'；未指定时使用 TRACEJUDGE_SANDBOX",
    ),
    allow_unsafe_local_exec: bool = typer.Option(
        False, "--allow-unsafe-local-exec", help="允许非 mock provider 使用本地子进程执行（不推荐）"
    ),
    output: str | None = typer.Option(None, "--output"),
) -> None:
    """对数据集中的单个题目运行完整流水线。"""

    settings = get_settings()
    try:
        problem = load_problem_by_id(dataset, problem_id)
        _reject_phase1_projection_execution([problem])
        backend = select_backend(
            provider_name=provider,
            sandbox_choice=sandbox or settings.tracejudge_sandbox,
            allow_unsafe_local_exec=allow_unsafe_local_exec,
            settings=settings,
        )
        llm_provider = _make_provider(provider)
        result = asyncio.run(_run_and_close_provider(problem, llm_provider, backend))
    except TraceJudgeError as exc:
        console.print(f"[red]运行失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    _render_result(result)

    out_path = (
        Path(output)
        if output
        else timestamped_artifact_path(settings.artifact_path, f"run_{problem_id}")
    )
    save_result_json(result, out_path)
    console.print(f"\n[dim]完整结果 JSON 已保存到 {out_path}[/dim]")


@app.command()
def batch(
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset"),
    provider: str = typer.Option("mock", "--provider", help="'mock' 或 'hy3'"),
    sandbox: str | None = typer.Option(
        None,
        "--sandbox",
        help="'docker' 或 'trusted-local'；未指定时使用 TRACEJUDGE_SANDBOX",
    ),
    allow_unsafe_local_exec: bool = typer.Option(False, "--allow-unsafe-local-exec"),
    limit: int | None = typer.Option(None, "--limit"),
    output: str = typer.Option("artifacts/batch_results.jsonl", "--output"),
) -> None:
    """对数据集中的多个题目批量运行完整流水线，输出 JSONL。"""

    settings = get_settings()
    try:
        problems = load_problems(dataset)
        _reject_phase1_projection_execution(problems)
    except TraceJudgeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if limit is not None:
        problems = problems[:limit]

    try:
        backend = select_backend(
            provider_name=provider,
            sandbox_choice=sandbox or settings.tracejudge_sandbox,
            allow_unsafe_local_exec=allow_unsafe_local_exec,
            settings=settings,
        )
        llm_provider = _make_provider(provider)
    except TraceJudgeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    outcomes = asyncio.run(_run_batch_in_one_loop(problems, llm_provider, backend))

    succeeded = 0
    with out_path.open("w", encoding="utf-8") as f:
        for problem, result, error in outcomes:
            console.print(f"[bold]评估中：{problem.problem_id}[/bold]")
            if error is not None:
                console.print(f"  [red]失败：{error}[/red]")
                continue
            assert result is not None
            f.write(json.dumps(pipeline_result_to_dict(result), ensure_ascii=False) + "\n")
            succeeded += 1
            verdict = (
                result.error_certificate.verdict if result.error_certificate else "no_certificate"
            )
            console.print(
                f"  functional_correct={result.process_assessment.functional_correct}, "
                f"process_correct={result.process_assessment.process_correct}, verdict={verdict}"
            )

    console.print(f"\n共评估 {succeeded}/{len(problems)} 道题，结果已写入 {out_path}")
    if problems and succeeded == 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
