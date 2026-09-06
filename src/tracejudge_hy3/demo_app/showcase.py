"""Public, hash-checked showcase cases for the local demo UI.

The case browser intentionally reads only repository-owned Phase-3 public
counterfactual fixtures and public certificate claims.  It never opens the
private annotation packet, formal per-trace method outputs, or hidden
HumanEval+ execution material.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tracejudge_hy3.phase3.counterfactual import PUBLIC_COUNTERFACTUAL_SOURCE_SHA256
from tracejudge_hy3.phase3.public_evidence import PUBLIC_CERTIFICATE_CLAIMS_SHA256

PUBLIC_SOURCE_PATH = Path("data/phase3/public_counterfactuals_v1.json")
PUBLIC_CLAIMS_PATH = Path("data/phase3/public_certificate_claims_v1.json")
SHOWCASE_SVG_PATH = Path(
    "docs/releases/phase4/charts/contest_showcase_v1/04_reasoning_swap_detection.svg"
)

_CASE_KINDS = (
    "reasoning_swap",
    "boundary_deletion",
    "equivalent_implementation",
)


class ShowcaseSourceError(ValueError):
    """Raised when a tracked public showcase source is missing or has drifted."""


def _load_hash_bound_json(path: Path, expected_sha256: str, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ShowcaseSourceError(f"missing {label}")
    payload = path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ShowcaseSourceError(f"{label} SHA256 differs from the public commitment")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowcaseSourceError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise ShowcaseSourceError(f"{label} must be a JSON object")
    return value


def _safe_mean_materials(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source = _load_hash_bound_json(
        repo_root / PUBLIC_SOURCE_PATH,
        PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
        label="public counterfactual source",
    )
    claims = _load_hash_bound_json(
        repo_root / PUBLIC_CLAIMS_PATH,
        PUBLIC_CERTIFICATE_CLAIMS_SHA256,
        label="public certificate claims",
    )
    parents = source.get("parents")
    mutations = source.get("counterfactuals")
    claim_rows = claims.get("claims")
    if (
        not isinstance(parents, list)
        or not isinstance(mutations, list)
        or not isinstance(claim_rows, list)
    ):
        raise ShowcaseSourceError("public showcase sources have an unexpected schema")
    parent = next(
        (
            item
            for item in parents
            if isinstance(item, dict)
            and isinstance(item.get("fixture"), dict)
            and item["fixture"].get("problem_id") == "safe_mean"
        ),
        None,
    )
    if parent is None:
        raise ShowcaseSourceError("safe_mean public parent fixture is missing")
    return (
        parent,
        {
            str(item.get("mutation_kind")): item
            for item in mutations
            if isinstance(item, dict)
            and isinstance(item.get("solution_trace"), dict)
            and item["solution_trace"].get("problem_id") == "safe_mean"
        },
        {str(item.get("trace_id")): item for item in claim_rows},
    )


def _case_payload(
    *,
    kind: str,
    mutation: dict[str, Any],
    claim: dict[str, Any],
    fixture: dict[str, Any],
) -> dict[str, Any]:
    trace_id = mutation.get("trace_id")
    solution = mutation.get("solution_trace")
    if not isinstance(trace_id, str) or not isinstance(solution, dict):
        raise ShowcaseSourceError(f"{kind} public case is malformed")

    common = {
        "case_id": kind,
        "trace_id": trace_id,
        "mutation_kind": kind,
        "problem": {
            "problem_id": fixture.get("problem_id"),
            "title": fixture.get("title"),
            "requirement": fixture.get("requirement"),
            "function_signature": fixture.get("function_signature"),
            "requirements": fixture.get("requirements", []),
        },
        "mutation": {
            "sole_change": mutation.get("sole_change"),
            "expected_impact": mutation.get("expected_impact"),
        },
        "solution": {
            "requirement_understanding": solution.get("requirement_understanding"),
            "design_summary": solution.get("design_summary"),
            "implementation_steps": solution.get("implementation_steps", []),
            "code": solution.get("code"),
        },
        "source": "公开自建 Phase-3 Fixture（MIT）",
    }

    if kind == "reasoning_swap":
        return {
            **common,
            "title": "答案正确，但过程解释错误",
            "short_label": "伪正确 / reasoning swap",
            "summary": "代码与公开执行保持正确，但结构化解释声称空列表返回 1.0、非空列表取最大值。",
            "functional_correct": True,
            "process_correct": False,
            "execution": {
                "status": "pass",
                "summary": "代码与父 Fixture 字节一致，复用同代码公开执行证据；全部公开用例通过。",
                "selected_case": None,
            },
            "assessment": {
                "first_faulty_layer": claim.get("first_faulty_layer"),
                "first_faulty_step": claim.get("first_faulty_step"),
                "error_type": claim.get("error_type"),
                "rationale": claim.get("claim_summary"),
            },
            "certificate": {
                "verdict": claim.get("expected_verdict"),
                "evidence_mode": claim.get("evidence_mode"),
                "publicly_replayable": True,
            },
            "cohort_observation": {
                "scope": "同类 3 条公开反事实的聚合结果，不是该单条的独立性能估计",
                "test_only": "0/3",
                "judge_methods": "四种方法均为 3/3",
            },
        }

    if kind == "boundary_deletion":
        empty_case = next(
            (item for item in fixture.get("test_cases", []) if item.get("case_id") == "c1_empty"),
            None,
        )
        return {
            **common,
            "title": "边界删除：反例定位首错步骤",
            "short_label": "边界错误 / boundary deletion",
            "summary": "推理声称会处理空列表，但代码删除了空输入保护，公开挑战用例可复现异常。",
            "functional_correct": False,
            "process_correct": False,
            "execution": {
                "status": "fail",
                "summary": "非空输入行为保持不变；空列表公开挑战稳定触发 ZeroDivisionError。",
                "selected_case": {
                    "case_id": "c1_empty",
                    "args": empty_case.get("args") if isinstance(empty_case, dict) else [[]],
                    "expected": empty_case.get("expected") if isinstance(empty_case, dict) else 0.0,
                    "actual": "ZeroDivisionError",
                },
            },
            "assessment": {
                "first_faulty_layer": claim.get("first_faulty_layer"),
                "first_faulty_step": claim.get("first_faulty_step"),
                "error_type": claim.get("error_type"),
                "rationale": claim.get("claim_summary"),
            },
            "certificate": {
                "verdict": claim.get("expected_verdict"),
                "evidence_mode": claim.get("evidence_mode"),
                "publicly_replayable": True,
            },
            "cohort_observation": {
                "scope": "同类 3 条公开反事实的聚合结果",
                "test_only": "3/3",
                "judge_methods": "四种方法均为 3/3",
            },
        }

    return {
        **common,
        "title": "等价实现：防止把写法不同当成错误",
        "short_label": "等价实现 / negative control",
        "summary": "用显式累加循环替换 sum(nums)，公开行为和复杂度保持正确，是误报压力测试。",
        "functional_correct": True,
        "process_correct": True,
        "execution": {
            "status": "pass",
            "summary": "全部公开用例通过；循环累加与 sum(nums) 在该公开契约下语义等价。",
            "selected_case": None,
        },
        "assessment": {
            "first_faulty_layer": None,
            "first_faulty_step": None,
            "error_type": None,
            "rationale": "人工构造真值为过程正确；没有已证实的首错步骤。",
        },
        "certificate": {
            "verdict": claim.get("expected_verdict"),
            "evidence_mode": claim.get("evidence_mode"),
            "publicly_replayable": False,
            "boundary_note": "该工程 claim 只有未验证怀疑，不应升级为已证实错误。",
        },
        "cohort_observation": {
            "scope": "同类 3 条公开反事实的聚合正确判断数",
            "test_only": "3/3",
            "judge_methods": "四种方法均为 2/3（出现 1 条误报）",
        },
    }


def load_public_showcase(repo_root: str | Path) -> dict[str, Any]:
    """Return three curated public cases plus the conceptual 2×2 matrix."""

    root = Path(repo_root)
    parent, mutations_by_kind, claims_by_trace = _safe_mean_materials(root)
    fixture = parent["fixture"]
    cases: list[dict[str, Any]] = []
    for kind in _CASE_KINDS:
        mutation = mutations_by_kind.get(kind)
        if not isinstance(mutation, dict):
            raise ShowcaseSourceError(f"missing public {kind} mutation")
        claim = claims_by_trace.get(str(mutation.get("trace_id")))
        if not isinstance(claim, dict):
            raise ShowcaseSourceError(f"missing public {kind} certificate claim")
        cases.append(_case_payload(kind=kind, mutation=mutation, claim=claim, fixture=fixture))

    return {
        "ok": True,
        "source": {
            "counterfactual_bundle": PUBLIC_SOURCE_PATH.as_posix(),
            "counterfactual_sha256": PUBLIC_COUNTERFACTUAL_SOURCE_SHA256,
            "claims_bundle": PUBLIC_CLAIMS_PATH.as_posix(),
            "claims_sha256": PUBLIC_CERTIFICATE_CLAIMS_SHA256,
            "contains_private_material": False,
        },
        "matrix": [
            {
                "answer_correct": True,
                "process_correct": True,
                "label": "正常正确",
                "case_id": "equivalent_implementation",
            },
            {
                "answer_correct": True,
                "process_correct": False,
                "label": "重点：伪正确",
                "case_id": "reasoning_swap",
            },
            {
                "answer_correct": False,
                "process_correct": True,
                "label": "推理可能合理，但实现失败",
                "case_id": None,
            },
            {
                "answer_correct": False,
                "process_correct": False,
                "label": "常规失败",
                "case_id": "boundary_deletion",
            },
        ],
        "cases": cases,
        "disclaimer": (
            "三条案例来自公开自建 Fixture，用于解释机制与证据等级；同类聚合均只有 n=3，"
            "不构成总体性能或显著性结论。"
        ),
    }
