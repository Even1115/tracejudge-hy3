"""Typer CLI: doctor / baseline / demo / run / batch."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

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
app.add_typer(dataset_app, name="dataset")
console = Console()

DEFAULT_DATASET = str(data_path("sample_problems.jsonl"))


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


@app.command()
def demo(
    mock: bool = typer.Option(
        True, "--mock/--no-mock", help="v0.1 仅支持 Mock Demo；--no-mock 会报错退出"
    ),
    case: str = typer.Option("faulty", "--case", help="safe_mean 演示场景：'correct' 或 'faulty'"),
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

    settings = get_settings()
    try:
        problem = load_problem_by_id(DEFAULT_DATASET, "safe_mean")
    except TraceJudgeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    provider = MockProvider(case=case)
    backend = TrustedLocalSandbox(per_test_timeout_seconds=settings.tracejudge_test_timeout_seconds)

    result = asyncio.run(run_pipeline(problem, provider, backend))
    _render_result(result)

    settings.artifact_path.mkdir(parents=True, exist_ok=True)
    out_path = timestamped_artifact_path(settings.artifact_path, f"demo_{case}")
    save_result_json(result, out_path)
    console.print(f"\n[dim]完整结果 JSON 已保存到 {out_path}[/dim]")


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
