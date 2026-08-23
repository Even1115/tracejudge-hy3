# 数据格式说明 (v0.1)

## `data/sample_problems.jsonl`

每行一个 JSON 对象，对应 `tracejudge_hy3.schemas.problem.ProblemSpec`：

```jsonc
{
  "problem_id": "safe_mean",
  "title": "安全平均值 safe_mean",
  "requirement": "自然语言需求描述",
  "function_signature": "def safe_mean(nums: list[float]) -> float:",
  "requirements": [
    {"requirement_id": "R1", "content": "...", "verification_hint": "safe_mean([]) == 0.0"}
  ],
  "visible_test_cases": [
    {"case_id": "v1", "args": [[1, 2, 3]], "kwargs": {}, "expected": 2.0, "category": "visible", "related_requirements": ["R2"]}
  ],
  "hidden_test_cases": ["..."],
  "challenge_test_cases": ["..."],
  "reference_code": "def safe_mean(nums):\n    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)\n",
  "difficulty": "easy",
  "source": "self_constructed_mvp_fixture",
  "tags": ["boundary", "arithmetic"]
}
```

要点：

- `args`/`kwargs` 是结构化 JSON 值，**从不使用可执行的表达式字符串**（测试运行器不调用 `eval()`）。
- `expected` 通常是普通 JSON 值。有一个特殊约定：`{"raises": "<ExceptionClassName>"}` 表示该用例期望抛出指定类型的异常（见 `clamp` 题的 `R4` 非法区间测试）。测试运行器在比较时会识别这种结构。
- `visible_test_cases` 会连同题目一起提供给 Solver；`hidden_test_cases` 和 `challenge_test_cases` 不会出现在 Prompt 中，只用于执行阶段。
- `source` 字段目前统一为 `self_constructed_mvp_fixture`，如接入真实 HumanEval/MBPP 数据，应替换为对应来源标记并保留原始许可证信息（v0.1 尚未实现该接入，见 `IMPLEMENTATION_STATUS.md`）。

## `data/mock_responses/*.json`

`tracejudge_hy3.schemas.solution.SolutionTrace` 格式的确定性 Mock 解答，被 `providers/mock.py` 按 `problem_id`（以及 `safe_mean` 的 `correct`/`faulty` 变体）加载。字段与 `SolutionTrace` schema 一一对应。

## `data/demo_annotations.jsonl`

为内置 Mock Fixture 提供的少量人工标注 Ground Truth，字段前缀 `human_*`，用于 `tests/test_metrics.py` 验证 `reporting/metrics.py` 中的指标函数行为是否正确。**这不是正式的人工标注研究数据**，样本量极小，仅覆盖 4 条内置 Fixture 记录。

## 流水线输出 JSON（`artifacts/*.json`）

由 `reporting/serializer.py:pipeline_result_to_dict()` 生成，顶层字段：`problem` / `solution` / `static_evidence` / `execution_result` / `llm_assessment` / `process_assessment` / `counterexample` / `error_certificate`，均为对应 Pydantic 模型的 `model_dump(mode="json")`。`batch` 命令将多条这样的记录以 JSONL 形式写入同一个文件。
