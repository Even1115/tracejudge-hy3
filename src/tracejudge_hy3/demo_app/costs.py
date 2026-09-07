"""Per-demo cost journal, role timing, and published method comparisons."""

from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracejudge_hy3.providers.base import LLMProvider
from tracejudge_hy3.providers.telemetry import request_observer

REPORT_PATH = Path("docs/releases/phase4/phase3_research_report_public_v1.md")


class DemoCosts:
    def __init__(self, artifact_dir: Path, mode: str):
        self.sample_id = uuid.uuid4().hex
        self.path = artifact_dir / "demo_costs" / f"{self.sample_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self.requests: list[dict[str, Any]] = []
        self.operations: list[dict[str, Any]] = []
        self.mode = mode
        self.persistence_ok = True
        self.write(
            {
                "event": "start",
                "schema_version": 1,
                "sample_id": self.sample_id,
                "mode": mode,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )

    def write(self, event: dict[str, Any]) -> None:
        # Preserve metrics in memory if the journal becomes unwritable; never retry
        # a paid request because writing its observation failed.
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        except OSError:
            self.persistence_ok = False

    @contextmanager
    def operation(self, role: str):
        operation_id = len(self.operations) + 1
        operation = {
            "role": role,
            "operation_id": operation_id,
            "wall_seconds": 0.0,
            "status": "running",
        }
        self.operations.append(operation)
        attempt = 0

        def record(data: dict[str, Any]) -> None:
            nonlocal attempt
            attempt += 1
            row = {
                **data,
                "request_id": len(self.requests) + 1,
                "role": role,
                "operation_id": operation_id,
                "attempt": attempt,
                "is_retry": attempt > 1,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            self.requests.append(row)
            self.write({"event": "request", **row})

        token = request_observer.set(record)
        started = time.perf_counter()
        try:
            yield
            operation["status"] = "completed"
        except BaseException:
            operation["status"] = "failed"
            raise
        finally:
            request_observer.reset(token)
            operation["wall_seconds"] = round(time.perf_counter() - started, 6)
            self.write({"event": "operation", **operation})

    def summary(self, total_seconds: float, replay_seconds: float) -> dict[str, Any]:
        roles = {}
        for role in ("solver", "judge"):
            rows = [row for row in self.requests if row["role"] == role]
            operations = [row for row in self.operations if row["role"] == role]
            aggregate: dict[str, Any] = {
                "calls": len(rows),
                "retries": sum(row["is_retry"] for row in rows),
                "failed_requests": sum(row["status"] == "request_error" for row in rows),
                "request_seconds": round(sum(row["request_seconds"] for row in rows), 6),
                "wall_seconds": round(sum(row["wall_seconds"] for row in operations), 6),
                "operations": len(operations),
                "failed_operations": sum(row["status"] == "failed" for row in operations),
            }
            for field in ("input_tokens", "output_tokens"):
                values = [row[field] for row in rows if row[field] is not None]
                aggregate[field] = sum(values) if len(values) == len(rows) else None
                aggregate[f"known_{field}"] = sum(values)
                aggregate[f"{field}_coverage"] = len(values)
            roles[role] = aggregate
        return {
            "schema_version": 1,
            "sample_id": self.sample_id,
            "mode": self.mode,
            "total_seconds": round(total_seconds, 6),
            "evaluation_seconds": round(max(0, total_seconds - roles["solver"]["wall_seconds"]), 6),
            "replay_seconds": round(replay_seconds, 6),
            "roles": roles,
            "requests": list(self.requests),
            "operations": list(self.operations),
            "persistence_ok": self.persistence_ok,
            "scope": "本次 Demo；总耗时覆盖流水线与证书重放，不含启动检查和产物写入。评测总耗时排除 Solver，包含测试、Judge、反例与重放。",
            "token_scope": "API 返回的 usage；未返回记为缺失。重试含同一阶段的再次请求与 JSON 修复，调用次数包含首次与重试。",
        }


class MeteredProvider(LLMProvider):
    """Time roles without changing the wrapped provider's prompts or retry policy."""

    def __init__(self, provider: LLMProvider, costs: DemoCosts):
        self.provider = provider
        self.costs = costs
        self.name = provider.name

    async def generate_solution(self, problem):
        with self.costs.operation("solver"):
            return await self.provider.generate_solution(problem)

    async def evaluate_process(self, problem, solution, static_evidence, execution_result):
        with self.costs.operation("judge"):
            return await self.provider.evaluate_process(
                problem, solution, static_evidence, execution_result
            )

    def is_trusted_local_solution(self, problem, solution):
        return self.provider.is_trusted_local_solution(problem, solution)

    def public_generation_config(self):
        return self.provider.public_generation_config()

    async def aclose(self):
        await self.provider.aclose()


def load_method_comparison(repo_root: str | Path) -> dict[str, Any]:
    path = Path(repo_root) / REPORT_PATH
    if path.is_symlink():
        raise ValueError("published report must be a regular file")
    text = path.read_text(encoding="utf-8")
    rows = []
    cost_pattern = re.compile(
        r"^\| ([^|]+) \| (\d+)/(\d+) \| (\d+) \| (\d+) \| (\d+) "
        r"\| (\d+) / (\d+) rows \| (\d+) / (\d+) rows \| ([\d.]+) \| ([^|]+) \|$",
        re.M,
    )
    for match in cost_pattern.finditer(text):
        name = match[1].strip()
        effect = re.search(rf"^\| {re.escape(name)} \| (\d+)/(\d+)（([\d.]+)%）", text, re.M)
        if effect is None or int(effect[2]) != int(match[3]):
            raise ValueError("published method effect/cost cohorts disagree")
        rows.append(
            {
                "method": name,
                "correct": int(effect[1]),
                "total": int(effect[2]),
                "accuracy": float(effect[3]) / 100,
                "valid": int(match[2]),
                "provider_failures": int(match[4]),
                "calls": int(match[5]),
                "json_repairs": int(match[6]),
                "known_input_tokens": int(match[7]),
                "input_coverage": int(match[8]),
                "known_output_tokens": int(match[9]),
                "output_coverage": int(match[10]),
                "duration_sum_seconds": float(match[11]),
                "money_status": match[12].strip(),
                "solver_cost": None,
            }
        )
    if len(rows) != 5 or len({row["method"] for row in rows}) != 5:
        raise ValueError("expected five published method rows")
    return {
        "rows": rows,
        "source": REPORT_PATH.as_posix(),
        "cohort": "phase3_hy3_57x5_v1",
        "scope": "冻结的 57 条样本 × 5 方法；Judge 评测开销，不含共享 Solver 生成。Token 是已知用量之和，覆盖行数同时展示；耗时是逐配对 duration 累计，非并行实验墙钟时间。Test-only 复用已有测试结果，0 不代表执行测试免费。",
        "boundary": "探索性结果，不支持普遍优越性结论。金额未由 Provider 返回，不根据 token 推算。历史 Solver 开销未在此公开报告中单列。",
    }
