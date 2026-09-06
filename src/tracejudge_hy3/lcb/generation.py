"""Public-only standard-I/O generation; reuse Hy3's bounded retry/repair policy."""

from __future__ import annotations

import json
from dataclasses import dataclass

from tracejudge_hy3.benchmark.contracts import BenchmarkTask, TaskInterface, canonical_sha256
from tracejudge_hy3.providers.base import SolutionGeneration
from tracejudge_hy3.providers.hy3_openai import Hy3OpenAIProvider
from tracejudge_hy3.schemas.solution import SolutionTrace

STDIO_SYSTEM_PROMPT = """你是一名严谨的 Python 竞赛程序开发者，完成标准输入输出任务。
只输出符合附带 JSON Schema 的 JSON 对象，不要在 JSON 外输出 Markdown 或解释。
code 字段是完整、可独立运行的 Python 程序：从 stdin 读取输入，将答案写入 stdout。
允许 print/sys.stdout.write；不要求函数签名，不输出交互提示或调试文字。
题面内的 Markdown 答案格式指示仅描述代码内容，最终响应格式以本系统的 JSON Schema 为准。
requirement_understanding 和 design_summary 简述公开需求与算法；implementation_steps
提供可供审阅的实现计划，至少一步；没有公开 requirement_id 时 related_requirements 用 []。
这些字段是用户可审阅的说明，不要求隐藏的思维过程。
problem_id 必须与公开任务 ID 一致。不要访问题面之外的测试或针对样例硬编码。
"""
STDIO_PROMPT_VERSION = "lcb-stdio-solution-trace-v1"


def prompt_bundle_hash() -> str:
    return canonical_sha256(
        {
            "version": STDIO_PROMPT_VERSION,
            "system": STDIO_SYSTEM_PROMPT,
            "schema": SolutionTrace.model_json_schema(),
            "user_fields": ["problem_id", "prompt", "requirements"],
        }
    )


@dataclass(frozen=True)
class _PublicStdioContext:
    task: BenchmarkTask

    @property
    def problem_id(self):
        return self.task.identity.task_id

    @property
    def requirements(self):
        return self.task.requirements


class StdioHy3Provider(Hy3OpenAIProvider):
    def public_generation_config(self) -> dict:
        return {**super().public_generation_config(), "stdio_prompt_sha256": prompt_bundle_hash()}

    def _solver_messages(self, problem: _PublicStdioContext) -> list[dict[str, str]]:
        task = problem.task
        return [
            {
                "role": "system",
                "content": STDIO_SYSTEM_PROMPT
                + "\nJSON Schema:\n"
                + json.dumps(SolutionTrace.model_json_schema(), ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "problem_id": task.identity.task_id,
                        "prompt": task.prompt,
                        "requirements": [r.model_dump(mode="json") for r in task.requirements],
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    async def generate_task(self, task: BenchmarkTask) -> SolutionGeneration:
        if task.identity.interface is not TaskInterface.STANDARD_IO:
            raise ValueError("stdio provider requires a standard-I/O task")
        return await super().generate_solution_with_details(_PublicStdioContext(task))
