"""Typer CLI: doctor / demo / run / batch."""

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

from tracejudge_hy3.config import get_settings
from tracejudge_hy3.dataset.loader import load_problem_by_id, load_problems
from tracejudge_hy3.exceptions import TraceJudgeError
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
console = Console()

DEFAULT_DATASET = str(data_path("sample_problems.jsonl"))


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
