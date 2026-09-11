from pathlib import Path

import pytest

from tracejudge_hy3.dataset.loader import load_problem_by_id
from tracejudge_hy3.exceptions import ProviderResponseError, UnsafeExecutionError
from tracejudge_hy3.pipeline.evaluate_solution import (
    BoundExecutionEvidence,
    EvaluationPolicy,
    evaluate_solution,
    model_sha256,
)
from tracejudge_hy3.pipeline.runner import run_pipeline
from tracejudge_hy3.providers.mock import MockProvider
from tracejudge_hy3.sandbox.trusted_local import TrustedLocalSandbox

DATASET = Path(__file__).resolve().parents[1] / "data/sample_problems.jsonl"


async def test_shared_entry_rejects_unbound_judge_location(frozen, monkeypatch):
    from tracejudge_hy3.schemas.evaluation import ProcessAssessment
    from tracejudge_hy3.schemas.location import FaultLocation

    problem, solution, provider = frozen

    async def bad_location(*args, **kwargs):
        return ProcessAssessment(
            functional_correct=None,
            reasoning_correct=False,
            process_correct=False,
            first_faulty_layer="reasoning",
            error_type="P01_ALGORITHM_ERROR",
            explanation="Invalid quote.",
            first_faulty_location=FaultLocation(
                source_field="design_summary", quote="ABSENT_QUOTE_CANARY"
            ),
        )

    monkeypatch.setattr(provider, "evaluate_process", bad_location)
    with pytest.raises(ProviderResponseError, match="does not bind"):
        await evaluate_solution(problem, solution, provider, TrustedLocalSandbox())


@pytest.fixture
async def frozen():
    problem = load_problem_by_id(DATASET, "safe_mean")
    provider = MockProvider(case="correct")
    solution = await provider.generate_solution(problem)
    return problem, solution, provider


async def test_frozen_evaluation_never_generates_and_matches_product(frozen, monkeypatch):
    problem, solution, provider = frozen
    backend = TrustedLocalSandbox()
    product = await run_pipeline(problem, provider, backend)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Solver must not run")

    monkeypatch.setattr(provider, "generate_solution", forbidden)
    before = model_sha256(solution)
    result = await evaluate_solution(problem, solution, provider, backend)
    assert result.process_assessment == product.process_assessment
    assert result.solution_sha256 == before == model_sha256(solution)
    assert result.evaluator_version == "shared-evaluator-v3"


async def test_cached_evaluation_can_disable_all_external_work(frozen, monkeypatch):
    problem, solution, provider = frozen
    backend = TrustedLocalSandbox()
    original = await evaluate_solution(problem, solution, provider, backend)
    evidence = BoundExecutionEvidence(
        problem_sha256=model_sha256(problem),
        solution_sha256=model_sha256(solution),
        execution=original.execution_result,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("no sandbox or API work expected")

    monkeypatch.setattr(backend, "run", forbidden)
    monkeypatch.setattr(backend, "is_available", forbidden)
    result = await evaluate_solution(
        problem,
        solution,
        None,
        backend,
        execution_evidence=evidence,
        policy=EvaluationPolicy(use_judge=False, search_counterexamples=False),
    )
    assert result.execution_reused
    assert result.process_assessment.functional_correct is True
    assert result.process_assessment.process_correct is None
    assert result.llm_assessment is None

    changed = solution.model_copy(update={"code": solution.code + "\n# changed"})
    with pytest.raises(ProviderResponseError, match="does not match"):
        await evaluate_solution(
            problem,
            changed,
            None,
            backend,
            execution_evidence=evidence,
            policy=EvaluationPolicy(use_judge=False, search_counterexamples=False),
        )
    evidence.execution.results.pop()
    with pytest.raises(ProviderResponseError, match="coverage"):
        await evaluate_solution(
            problem,
            solution,
            None,
            backend,
            execution_evidence=evidence,
            policy=EvaluationPolicy(use_judge=False, search_counterexamples=False),
        )


async def test_shared_entry_preserves_untrusted_code_guard(frozen):
    problem, solution, provider = frozen
    changed = solution.model_copy(update={"code": "def safe_mean(nums): return 1"})
    with pytest.raises(UnsafeExecutionError):
        await evaluate_solution(problem, changed, provider, TrustedLocalSandbox())


async def test_provider_cannot_mutate_frozen_input_or_execution(frozen, monkeypatch):
    problem, solution, provider = frozen
    original = model_sha256(solution)
    original_problem = model_sha256(problem)

    async def mutate(problem, solution, static, execution):
        solution.code = "def changed(): pass"
        problem.title = "changed"
        execution.results.clear()
        return None

    monkeypatch.setattr(provider, "evaluate_process", mutate)
    result = await evaluate_solution(problem, solution, provider, TrustedLocalSandbox())
    assert model_sha256(solution) == original == result.solution_sha256
    assert model_sha256(problem) == original_problem
    assert result.execution_result.results
    assert result.process_assessment.functional_correct is True
