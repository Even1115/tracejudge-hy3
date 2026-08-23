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
- Solver Prompt 使用显式白名单：`problem_id`、`requirement`、`function_signature`、每个公开需求条款的 `requirement_id`/`content`，以及可见测试的 `args`/`kwargs`/`expected`。`verification_hint`、`reference_code`、`hidden_test_cases`、`challenge_test_cases`、其期望值和人工标注都不会出现在 Solver Prompt 中。现有完整评估链路可在之后的执行阶段使用隐藏/challenge 数据，但 `baseline` 命令到生成与解析即停止，不运行任何测试。
- `source` 字段目前统一为 `self_constructed_mvp_fixture`。对这份数据运行阶段一时，manifest 的 `experiment_label` 为 `self_constructed_mvp_fixture_pilot`；这是自建工程 Fixture 小规模试运行，不是 HumanEval+、MBPP+ 或正式 benchmark。如接入真实公开数据，应替换为对应来源标记并保留原始许可证信息（v0.1 尚未实现该接入，见 `IMPLEMENTATION_STATUS.md`）。

## `data/mock_responses/*.json`

`tracejudge_hy3.schemas.solution.SolutionTrace` 格式的确定性 Mock 解答，被 `providers/mock.py` 按 `problem_id`（以及 `safe_mean` 的 `correct`/`faulty` 变体）加载。字段与 `SolutionTrace` schema 一一对应。

## `data/demo_annotations.jsonl`

为内置 Mock Fixture 提供的少量人工标注 Ground Truth，字段前缀 `human_*`，用于 `tests/test_metrics.py` 验证 `reporting/metrics.py` 中的指标函数行为是否正确。**这不是正式的人工标注研究数据**，样本量极小，仅覆盖 4 条内置 Fixture 记录。

## 阶段一基线产物（`artifacts/experiments/phase1/<run_id>/`）

`tracejudge baseline` 每次新运行自动生成唯一 `run_id`，并创建：

```text
<output-dir>/<run_id>/
├── manifest.json
├── responses.jsonl
└── summary.json
```

三个文件都以 UTF-8 编码。JSON/JSONL 写入先落到同目录临时文件，`flush`/`fsync` 后再原子替换目标；因此进程中断不会在 `responses.jsonl` 末尾留下半条 JSON。

### `manifest.json`

manifest 是运行级别的非敏感复现信息，结构如下（值仅为示意）：

```jsonc
{
  "schema_version": 1,
  "phase": "phase1_baseline_generation",
  "experiment_label": "self_constructed_mvp_fixture_pilot",
  "run_id": "phase1_<timestamp>_<random>",
  "created_at": "<UTC timestamp>",
  "status": "completed",
  "completed_at": "<UTC timestamp>",
  "dataset": {
    "path": "<absolute dataset path>",
    "sha256": "<sha256 of exact input bytes>",
    "problem_count": 3,
    "sources": {"self_constructed_mvp_fixture": 3},
    "difficulties": {"easy": 2, "medium": 1},
    "visible_tests": {
      "total_count": 6,
      "per_problem": {"<problem_id>": {"count": 2, "case_ids": ["v1", "v2"]}}
    }
  },
  "git": {
    "available": true,
    "commit": "<commit>",
    "branch": "<branch>",
    "dirty": false,
    "working_tree_sha256": null
  },
  "environment": {
    "project": {"name": "tracejudge-hy3", "version": "<version>"},
    "python": {"version": "<version>", "implementation": "CPython", "executable": "<path>"},
    "direct_dependencies": {"openai": "<version>", "pydantic": "<version>", "...": "..."}
  },
  "provider_config": {
    "provider": "hy3",
    "model": "<model>",
    "reasoning_effort": "high",
    "reasoning_effort_enabled": true,
    "timeout_seconds": 120.0,
    "max_retries": 2,
    "endpoint_sha256": "<sha256; endpoint itself is not stored>"
  },
  "invocations": [
    {
      "invocation_id": "<id>",
      "started_at": "<UTC timestamp>",
      "resume": false,
      "status": "completed",
      "completed_at": "<UTC timestamp>",
      "git": {"...": "invocation-time snapshot"},
      "environment": {"...": "invocation-time snapshot"}
    }
  ]
}
```

`status` 在处理中为 `running`，批次正常处理完后原子更新为 `completed` 并写入 `completed_at`。`provider_config` 是 Provider 显式返回的白名单，不会通过枚举 Provider 内部属性来采集配置。Hy3 先剔除 endpoint 的 userinfo/query/fragment，再记录规范化 endpoint SHA256，不记录 endpoint 原文。敏感键会被过滤，常见凭据形式会被脱敏；API Key、Authorization Header、完整请求头、Cookie 和密码不得写入产物。续跑会在 `invocations` 中追加带当时 Git/环境快照的 `resume: true` 记录，将遗留的 `running` invocation 标记为 `interrupted` 并写入 `interrupted_at`，但不改变初始 `run_id`。工作树不干净时，`working_tree_sha256` 只保存变更内容的指纹，不保存 diff 或未跟踪文件内容；本 run 自身产物目录从该指纹中排除。

### `responses.jsonl`

该文件是追加语义的事件日志，每行一个 JSON 对象：

```jsonc
{
  "run_id": "<run_id>",
  "invocation_id": "<invocation_id>",
  "problem_id": "safe_mean",
  "provider": "hy3",
  "model": "<model>",
  "status": "success",
  "parse_status": "parsed",
  "started_at": "<UTC timestamp>",
  "ended_at": "<UTC timestamp>",
  "duration_seconds": 1.234567,
  "attempt_count": 1,
  "retry_count": 0,
  "raw_output_attempt": 1,
  "parse_attempted": true,
  "raw_output": "<provider text after artifact-safety redaction>",
  "solution_trace": {
    "problem_id": "safe_mean",
    "requirement_understanding": "<user-facing explanation>",
    "design_summary": "<auditable design summary>",
    "edge_cases_considered": ["..."],
    "implementation_steps": [
      {
        "step_id": "S1",
        "content": "<auditable implementation step>",
        "related_requirements": ["R1"],
        "expected_code_behavior": "<expected behavior>"
      }
    ],
    "declared_time_complexity": "O(n)",
    "declared_space_complexity": "O(1)",
    "code": "def safe_mean(...):\n    ...\n"
  },
  "error_type": null,
  "error": null
}
```

`raw_output` 是 Provider 的原始文本（已精确移除已配置密钥，并对 JSON/文本中常见凭据形式做产物安全脱敏），`solution_trace` 是经 JSON Schema 和题目上下文引用校验后的结构，两者分开保存。解析失败摘要只保留错误类型、位置和消息，不包含 Pydantic `input_value`；Hy3 修复轮也只回传脱敏后的旧输出。`solution_trace` 中的解题说明、设计摘要和实现步骤是面向用户的可审查摘要，不是、也不要求模型私有思维链。

`status` 只有四种：

| 状态 | 含义 |
|---|---|
| `success` | Provider 调用完成，原始输出已成功解析和校验为 `SolutionTrace` |
| `parse_error` | 在有限尝试内仍无法将输出解析/校验为正确 `SolutionTrace` |
| `provider_error` | 认证、连接、服务端、超时或 Provider 内部异常；同一批次会继续处理后续题目 |
| `skipped` | 续跑时该 `problem_id` 历史上已有 `success`，本次不再调用 Provider |

`parse_status` 独立标记解析结果：`success` 对应 `parsed`，`parse_error` 对应 `failed`，从未得到可解析文本的 `provider_error` 与 `skipped` 对应 `not_attempted`。若早期尝试已对一份原始输出解析失败，但后续尝试又发生超时/服务错误，最终 `status` 是 `provider_error`、`parse_status` 仍是 `failed`，`raw_output_attempt` 指明保存的 `raw_output` 来自第几次调用。`error_type` 是便于汇总的顶层错误类型；`error` 为 `null` 或 `{"type": "<exception class>", "message": "<redacted message>"}`。失败记录不会保留过期的 `solution_trace`；如 Provider 已返回文本，安全脱敏后仍可在 `raw_output` 中保留它。所有持久化字符串中无法表示为 UTF-8 标量值的孤立 surrogate 会统一替换为 `U+FFFD`，以确保单题异常不中止整批持久化且续跑 ID 语义一致。

使用 `--resume-run-id <run_id>` 续跑时，数据集 SHA256、Provider 公开配置、TraceJudge Git commit/工作树指纹以及 Python/直接依赖环境必须与 manifest 一致。历史上任意一次成功的 `problem_id` 追加 `skipped`，未成功题目重新调用 Provider。

### `summary.json`

汇总是实验生成阶段的可观测事实，包括：

- `total_problem_count` / `dataset_problem_count`：本数据集题目总数；
- `experiment_label`：与 manifest 一致的数据/实验性质标记；
- `final_outcome_counts` 及 `success_count` / `parse_error_count` / `provider_error_count` / `failure_count` / `pending_count`：每题最后一条非 `skipped` 事件构成的最终结果，其中 `failure = parse_error + provider_error`；
- `parse_attempted_count` / `parse_success_count` / `parse_failure_count` 以及 `parse_success_rate`：`parsed / (parsed + failed)`；若没有解析尝试则为 `null`，从未取得文本的 Provider 失败不进入分母；
- `average_duration_seconds`：每题最终非 `skipped` 事件耗时的算术平均；
- `record_count` / `record_status_counts`：包含续跑事件在内的全部日志记录数/状态分布；
- `invocation` / `skipped_count`：最近一次调用的时间、状态分布和跳过数；
- `metrics_scope: "generation_and_parsing_only"`：明确统计边界。

summary **不包含**功能正确率、测试通过率、错误检测率、四层评估结果、反例指标、人工标注对比或任何阶段二及以后的指标。

## 流水线输出 JSON（`artifacts/*.json`）

这是 `run` / `batch` / `demo` 现有完整评估链路的格式，**与上述阶段一基线产物不同**。它由 `reporting/serializer.py:pipeline_result_to_dict()` 生成，顶层字段：`problem` / `solution` / `static_evidence` / `execution_result` / `llm_assessment` / `process_assessment` / `counterexample` / `error_certificate`，均为对应 Pydantic 模型的 `model_dump(mode="json")`。`batch` 命令将多条这样的记录以 JSONL 形式写入同一个文件。
