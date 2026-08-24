"""JSONL problem-dataset loading."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from tracejudge_hy3.exceptions import DatasetError
from tracejudge_hy3.schemas.problem import ProblemSpec


def load_problems(path: str | Path) -> list[ProblemSpec]:
    path = Path(path)
    if not path.exists():
        raise DatasetError(f"dataset file not found: {path}")

    problems: list[ProblemSpec] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            try:
                problem = ProblemSpec.model_validate(payload)
            except ValidationError as exc:
                # Pydantic's default error rendering includes ``input_value``.
                # Dataset rows may contain withheld benchmark answers, tests, or
                # credentials under an unexpected field, so never echo the
                # validation exception (or retain it as a traceback cause).
                issue_types = sorted(
                    {
                        str(issue.get("type", "validation_error"))
                        for issue in exc.errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        )
                    }
                )
                safe_types = ", ".join(issue_types[:5]) or "validation_error"
                raise DatasetError(
                    f"{path}:{lineno}: schema validation failed "
                    f"({exc.error_count()} issue(s); types: {safe_types})"
                ) from None
            if problem.problem_id in seen_ids:
                raise DatasetError(f"{path}:{lineno}: duplicate problem_id '{problem.problem_id}'")
            seen_ids.add(problem.problem_id)
            problems.append(problem)

    if not problems:
        raise DatasetError(f"dataset file is empty: {path}")
    return problems


def load_problem_by_id(path: str | Path, problem_id: str) -> ProblemSpec:
    problems = load_problems(path)
    for problem in problems:
        if problem.problem_id == problem_id:
            return problem
    raise DatasetError(f"problem_id '{problem_id}' not found in {path}")
