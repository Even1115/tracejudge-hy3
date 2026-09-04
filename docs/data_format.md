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
- 内置样例的 `source` 为 `self_constructed_mvp_fixture`，阶段一 `experiment_label` 为 `self_constructed_mvp_fixture_pilot`。HumanEval+ 阶段一公开投影使用 `source: "evalplus_humanevalplus"`，其来源、revision、许可证和选择信息由 bundle manifest 绑定；两者都不能被表述为正式 benchmark 分数。

## HumanEval+ 阶段一公开投影 bundle

仓库提交 [`data/manifests/evalplus_humanevalplus_d32357cf.json`](../data/manifests/evalplus_humanevalplus_d32357cf.json) 作为受控来源 manifest。原始固定 revision 快照必须由用户放在 `artifacts/datasets/raw/humanevalplus/`；`artifacts/` 被 Git 忽略，因此原始 `canonical_solution`、官方 `test` 以及转换/实验产物都不会提交到仓库。转换器会核对来源 manifest 中每个原始文件的大小和 SHA256、完整 40 位 revision、Apache-2.0 许可证、`test` split 和 `HumanEval/0` 至 `HumanEval/163` 的完整题号集合。

`tracejudge dataset convert-humanevalplus` 只读取以下公开字段来构造 `ProblemSpec`：

| `ProblemSpec` 字段 | HumanEval+ 公开来源/固定值 |
|---|---|
| `problem_id` | `task_id` |
| `title` | `task_id` 与 `entry_point` 组成的可审查标题 |
| `requirement` | 原始公开 `prompt` |
| `function_signature` | 从公开 `prompt` 中解析并规范化的 `entry_point` 签名 |
| `requirements` | `R1`，内容为公开函数 docstring，`verification_hint: null` |
| `visible_test_cases` / `hidden_test_cases` / `challenge_test_cases` | 均为 `[]` |
| `reference_code` | `# EVALPLUS_REFERENCE_CODE_WITHHELD_FROM_PHASE1\n` 固定 sentinel |
| `difficulty` | `unknown`，不根据答案或测试猜测难度 |
| `source` | `evalplus_humanevalplus` |

`canonical_solution` 和官方 `test` 只用于验证原始行具备固定 schema；其正文不进入上述任何字段，也不复制到转换 bundle、抽样 bundle、Solver Prompt 或基线产物。原始快照整体哈希与 withheld 字段名称会进入 provenance，但不保存这些字段的内容。完整转换结果为：

```text
artifacts/datasets/processed/humanevalplus-full/
├── problems.jsonl
└── dataset_manifest.json
```

目录以临时目录加一次原子替换完整发布；相同内容重跑幂等，现有目录内容不同时拒绝覆盖。`dataset_manifest.json` 记录：

- 数据集 ID、固定 revision、split、许可证和适配器名称/版本；
- 受控来源 manifest SHA256，以及原始快照聚合 SHA256、原始 JSONL SHA256 和记录数；
- 公开 `problems.jsonl` SHA256、记录数、按顺序题号 SHA256；
- 选择算法、题数、按顺序的公开 `problem_id` 和 withheld 字段名称；
- `metrics_scope: "generation_and_parsing_only"`。

`tracejudge dataset sample --count 10 --seed 20260824` 只用 `sha256(seed\0problem_id)` 对公开题号排序取前 10 个，再按 HumanEval 数值题号顺序输出。当前固定题号为：

```text
HumanEval/8, HumanEval/26, HumanEval/41, HumanEval/51, HumanEval/70,
HumanEval/81, HumanEval/95, HumanEval/96, HumanEval/105, HumanEval/120
```

`--selection-role research_natural --count 45 --seed 20260825 --exclude-manifest <pilot_manifest>` 会生成 schema v2 的研究子集 manifest：在 164 题中排除指定 Pilot manifest 的题号后，按同一算法取前 45 题；v2 manifest 额外记录 `selection_role`、`excluded_manifests`（含每个被排除 manifest 的 SHA256、角色与题号）、`excluded_problem_ids` 及其 SHA256、被排除 manifest 列表哈希。

Pilot bundle 同样只有 `problems.jsonl` 和 `dataset_manifest.json`；后者（v1）额外绑定父 manifest SHA256、固定 seed、选择算法和题号列表，`experiment_label` 固定为 `humanevalplus_10_public_prompt_generation_pilot`。研究子集 bundle（v2）的 `experiment_label` 固定为 `humanevalplus_45_public_prompt_generation_research_natural`。`tracejudge dataset validate` 只离线验证 `ProblemSpec` JSONL 的 schema/重复 ID 等通用约束，不调用 Provider，也不执行任何代码或测试。完整的可复现命令见 README 的“HumanEval+ 固定 10 题阶段一 Pilot”和“HumanEval+ 45 题自然研究子集”。

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
  "schema_version": 2,
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
    "max_parse_repairs": 1,
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

顶层 `schema_version` 是 manifest、responses 和 summary 整个阶段一 artifact bundle 的统一版本。新 writer 固定创建 v2；已有 v1 产物保持原字节只读，阶段二 exporter 仍按 v1 严格验证，但新 writer 会拒绝 resume v1，避免同一 run 静默混写。`status` 在处理中为 `running`，批次正常处理完后原子更新为 `completed` 并写入 `completed_at`。`provider_config` 是 Provider 显式返回的白名单，不会通过枚举 Provider 内部属性来采集配置。Hy3 先剔除 endpoint 的 userinfo/query/fragment，再记录规范化 endpoint SHA256，不记录 endpoint 原文。敏感键会被过滤，常见凭据形式会被脱敏；API Key、Authorization Header、完整请求头、Cookie 和密码不得写入产物。传入 HumanEval+ `--dataset-manifest` 时，`dataset.provenance` 只保存经白名单校验的身份信息：manifest SHA256、revision、许可证、适配器、原始快照/公开投影哈希、题号顺序与确定性选择参数、withheld 字段名和 `generation_and_parsing_only` 边界；不会原样复制任意 manifest 字段或私有测试内容。续跑会在 `invocations` 中追加带当时 Git/环境快照的 `resume: true` 记录，将遗留的 `running` invocation 标记为 `interrupted` 并写入 `interrupted_at`，但不改变初始 `run_id`。工作树不干净时，`working_tree_sha256` 只保存变更内容的指纹，不保存 diff 或未跟踪文件内容；本 run 自身产物目录从该指纹中排除。

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
  "attempt_outcomes": ["success"],
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

`attempt_outcomes` 是按实际调用顺序保存的脱敏枚举序列，元素只允许 `success` / `parse_error` / `provider_error`。它不保存错误详情、原始输出、请求或凭据；`attempt_count` 必须等于其长度，成功必须是最后一次 outcome。`retry_count = attempt_count - 1` 只是“首次之后的全部额外调用数”：`provider_error → success` 是普通 Provider 重试，不是 JSON repair；只有某次 `parse_error` 后确实又发起下一次调用，才发生 repair。`HY3_MAX_PARSE_REPAIRS` 是解析失败触发的修复调用硬上限（与普通 Provider 重试分开），修复预算耗尽后即使仍有 `HY3_MAX_RETRIES` 剩余也不会再追加修复 Prompt，最终状态直接为 `parse_error`。最后一次为 `parse_error` 且没有后续调用时，不会虚构 repair。`skipped` 的 `attempt_count` / `retry_count` 为 0，`attempt_outcomes` 为空。

`parse_status` 独立标记解析结果：`success` 对应 `parsed`，`parse_error` 对应 `failed`，从未得到可解析文本的 `provider_error` 与 `skipped` 对应 `not_attempted`。若早期尝试已对一份原始输出解析失败，但后续尝试又发生超时/服务错误，最终 `status` 是 `provider_error`、`parse_status` 仍是 `failed`，`raw_output_attempt` 指明保存的 `raw_output` 来自第几次调用。`error_type` 是便于汇总的顶层错误类型；`error` 为 `null` 或 `{"type": "<exception class>", "message": "<redacted message>"}`。失败记录不会保留过期的 `solution_trace`；如 Provider 已返回文本，安全脱敏后仍可在 `raw_output` 中保留它。所有持久化字符串中无法表示为 UTF-8 标量值的孤立 surrogate 会统一替换为 `U+FFFD`，以确保单题异常不中止整批持久化且续跑 ID 语义一致。

使用 `--resume-run-id <run_id>` 续跑时，数据集 SHA256、Provider 公开配置、TraceJudge Git commit/工作树指纹以及 Python/直接依赖环境必须与 manifest 一致。若初始运行传入数据集 manifest，续跑还必须传入同一份 manifest，并精确匹配已记录的 provenance（含 manifest SHA256）和 `experiment_label`；更换 revision、选择 seed/题号、适配器或重写 manifest 都会拒绝续跑。历史上任意一次成功的 `problem_id` 追加 `skipped`，未成功题目重新调用 Provider。

### `summary.json`

汇总是实验生成阶段的可观测事实，包括：

- `total_problem_count` / `dataset_problem_count`：本数据集题目总数；
- `experiment_label`：与 manifest 一致的数据/实验性质标记；
- `final_outcome_counts` 及 `success_count` / `parse_error_count` / `provider_error_count` / `failure_count` / `pending_count`：每题最后一条非 `skipped` 事件构成的最终结果，其中 `failure = parse_error + provider_error`；
- `parse_attempted_count` / `parse_success_count` / `parse_failure_count` 以及 `parse_success_rate`：`parsed / (parsed + failed)`；若没有解析尝试则为 `null`，从未取得文本的 Provider 失败不进入分母；
- `first_attempt_parse_success_count`：第一次实际调用的 outcome 即为 `success` 的题目数；
- `parse_failure_encountered_count`：最终有效调用序列中至少出现过一次 `parse_error` 的题目数；
- `repair_attempted_count`：至少一次 `parse_error` 后确实还有下一次实际调用的题目数；
- `repair_success_count`：此前出现过 `parse_error` 且最终状态为 `success` 的题目数；普通 `provider_error → success` 不计入；
- `terminal_parse_error_count`：最终阶段一状态为 `parse_error` 的题目数；
- `average_attempt_count` / `average_retry_count`：每题最终有效非 `skipped` 记录的调用数/额外调用数算术平均；没有最终记录时为 `null`；
- `average_duration_seconds`：每题最终非 `skipped` 事件耗时的算术平均；
- `record_count` / `record_status_counts`：包含续跑事件在内的全部日志记录数/状态分布；
- `invocation` / `skipped_count`：最近一次调用的时间、状态分布和跳过数；
- `metrics_scope: "generation_and_parsing_only"`：明确统计边界。

summary **不包含**功能正确率、测试通过率、错误检测率、四层评估结果、反例指标、人工标注对比或任何阶段二及以后的指标。

以上题目级指标都从每个 `problem_id` 最后一条有效非 `skipped` 记录重建；resume 追加的 `skipped` 事件不会覆盖历史成功记录或重复增加解析/repair 计数。旧 v1 summary 不含这七个 v2 指标，阶段二 exporter 不会根据旧 `retry_count` 猜测是否发生过 repair。

## 阶段二 EvalPlus 产物（`artifacts/experiments/phase2/<run_id>/`）

`tracejudge evalplus` 只接受已完成的阶段一 run，并默认要求数据集中每道题都有唯一历史 `success`（`--selection-policy all`）。若使用 `--selection-policy phase1-success-only --min-success-count N`，则只导出阶段一成功题目，且要求成功数不少于 `N`；导出的题号顺序仍遵循数据集 manifest。在创建运行目录前，exporter 会验证阶段一的 manifest/summary/responses 字节哈希、题号、状态、Provider/模型、Git commit、数据 revision 和公开投影 provenance。合法续跑可以在 responses 中含有后续 `skipped` 事件。

```text
<output-dir>/<run_id>/
├── manifest.json
├── samples.jsonl
├── evalplus_raw_results.json
├── results.jsonl
├── summary.json
└── execution.log
```

运行目录权限固定为 `0700`，六个文件均为 `0600`、UTF-8，并使用同目录临时文件 + `fsync` + 原子替换。阶段二拒绝仓库外位置和仓库内未被 `git check-ignore` 覆盖的位置；正常目录位于被 Git 忽略的 `artifacts/` 下。

### `manifest.json`

manifest 的 `phase` 固定为 `phase2_evalplus_execution`。10 题 Pilot 全部导出时的 `experiment_label` 保持为 `humanevalplus_10_evalplus_execution_pilot`；45 题自然研究子集全部导出时为 `humanevalplus_45_evalplus_execution_research_natural`，只导出 N 题时为 `humanevalplus_{N}_of_45_evalplus_execution_research_natural`。标签中的数量始终与真正进入阶段二的 samples 相符。其允许字段包括：

- `phase1_source`：阶段一 run ID、manifest/summary/responses SHA256、Git commit/分支/工作树状态、Provider 和模型；
- `dataset`：Hugging Face 数据 revision、许可证、受控 manifest/原始快照/公开投影/题号顺序 SHA256、选择算法/种子/题号、`selection_role`（v2 研究子集为 `research_natural`）以及被排除 manifest 列表（v2）；
- `input`：samples SHA256、记录数、有序题号、每题代码 SHA256，按同一顺序记录的 `problem_id` / 公开 prompt SHA256 / entry point，以及 `phase1_export_selection`；后者精确记录 `selection_policy`、`min_success_count`、`source_problem_count`、`exported_success_count`、`excluded_parse_error_count` 和 `excluded_provider_error_count`；
- `executor`：镜像内 EvalPlus package `0.4.0.dev2`、源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`、官方镜像 RepoDigest、linux/amd64、Python 3.11.10、HumanEval+ release v0.1.10、官方参数、资源/隔离/超时策略；
- `executor_runtime`：运行前镜像 ID/平台检查、镜像内 `git -C /evalplus rev-parse HEAD` 的实际 commit、Python 最终导入的 `evalplus/evaluate.py` 精确字节 SHA256、EvalPlus 数据集官方 MD5、已加载 164 题 native corpus 的确定性 canonical SHA256 及算法、题数与实际导出题目的公开身份核对数；
- `execution_config`：宿主单题容器并发数、容器内官方 parallel=1、单题/整批调度超时、固定 5 秒 batch cleanup grace 和官方时限参数；
- `resume_fingerprint`：以上来源、候选字节、执行器/数据身份、参数和实现 SHA256 的组合指纹；
- `git` / `environment` / `invocations` / `preflight` / `output` 及与 Pilot/研究 cohort 相符的限制。

Hugging Face revision 和 EvalPlus 代码/release 是两套独立 provenance，manifest 不将它们混成一项。

### `samples.jsonl` 与 `evalplus_raw_results.json`

`samples.jsonl` 每题恰好两个字段，顺序与数据集 manifest 一致：

```json
{"task_id":"HumanEval/8","solution":"<exact solution_trace.code>"}
```

该序列化器只做严格 UTF-8/JSON 编码，不对阶段一已脱敏的代码再次替换文本，避免静默改写候选语义。

`evalplus_raw_results.json` 是 TraceJudge 逐题调用固定官方镜像内 EvalPlus package `0.4.0.dev2`（源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`）后的版本化 raw bundle。其内部官方文档仍保留 `date` / `hash` / `eval`，以及候选 `solution`、`base_status`、`plus_status`、`base_fail_tests`、`plus_fail_tests`。这是 evaluation-only 私有文件；不得打印、提交、加入普通日志或传递给后续模型。

容器传输期间只把两个预创建的宿主临时文件精确挂载为 raw/control 目标，不挂载输出目录；容器 PID 1 以状态 0 完全退出并清理后，宿主才把经身份/大小/哈希校验的 raw 复制为运行目录中的新 `0600` inode。临时文件模式不是最终 artifact 权限。

### `results.jsonl`

每题脱敏记录的核心字段为：

```jsonc
{
  "schema_version": 1,
  "run_id": "<phase2 run id>",
  "problem_id": "HumanEval/8",
  "base_status": "pass",
  "plus_status": "fail",
  "base_fail_test_count": 0,
  "plus_fail_test_count": 1,
  "failure_count_scope": "recorded_by_evalplus_test_details",
  "passed_base": true,
  "passed_plus": false,
  "error_type": "wrong_answer_or_candidate_exception",
  "infrastructure_status": "ok",
  "solution_sha256": "<sha256>",
  "official_override_hash": "<single-task private override md5>",
  "duration_seconds": 1.234,
  "started_at": "<UTC timestamp>",
  "ended_at": "<UTC timestamp>",
  "source_response": {
    "phase1_run_id": "<id>",
    "problem_id": "HumanEval/8",
    "invocation_id": "<id>",
    "response_line_number": 1,
    "response_record_sha256": "<sha256>",
    "code_sha256": "<sha256>"
  }
}
```

`passed_plus` 表示 Base 和 Extra 都通过，不是只看 `plus_status`。`*_fail_test_count` 只是官方 `--test-details` 在当次执行中已记录的失败数；尤其 timeout 时不保证它等于全部理论失败测试数。具体失败输入不进入该文件。

该固定 EvalPlus commit 的官方 raw 状态只有 `pass` / `fail` / `timeout`；其中 `fail` 不能可靠区分 wrong answer、语法/入口错误或候选运行异常，所以统一使用 `wrong_answer_or_candidate_exception`。基础设施错误的 base/plus status 为 `null`、`infrastructure_status: "error"`，不会被计为代码失败。Mock dry run 使用 `infrastructure_status: "mocked"` / `error_type: "mock_not_executed"`，同样不表示功能结果。

### `summary.json` 与 `execution.log`

summary 从脱敏逐题记录重建，主要包含阶段一来源题数、成功导出数、解析/Provider 排除数、`pipeline_coverage_rate`、阶段二结果数、实际执行数、Base 通过数/率、Base+Extra 通过数/率、timeout、`wrong_answer_or_candidate_exception`、基础设施错误、已观测失败数和平均逐题容器耗时。通过率分母是 `actual_execution_count`；Pipeline Coverage 分母是 `source_problem_count`，两者不混用。基础设施失败不进功能通过率分母。批次截止另以 `batch_timeout_count`、`batch_deadline_not_started_count` 和 `container_cleanup_failed_count` 区分已启动超时、尚未启动及清理失败/未确认的题；续跑以 `resume_skipped_count` 和本次 invocation 的结果/基础设施计数说明复用边界。`execution_error_count` 为 `null`，并用 `not_available_in_pinned_evalplus_raw_schema` 说明无法从官方状态精确细分。

`execution.log` 只是最多 64 KiB 的基础设施事件 JSONL；不保存 Docker/EvalPlus stdout/stderr 原文或失败输入，仅允许时间、题号、耗时、安全错误类别、输出字节数/SHA256、退出码和清理状态等白名单字段。

## 阶段三 Gate A/B 契约与自然/反事实冻结产物

阶段三契约位于 `tracejudge_hy3.phase3.contracts`，完整研究协议见 [`docs/experiments/phase3_protocol.md`](experiments/phase3_protocol.md)。Gate A 定义 schema 与离线隐私/哈希校验；Gate B 的 `tracejudge phase3 preflight` 完成全链路只读校验和内存 manifest 构造但不写文件，`tracejudge phase3 freeze` 在相同校验通过后原子生成一个只含白名单字段的自然轨迹 manifest。失败输出只含固定安全阶段码，不回显内部异常内容。

### `FrozenCohortManifest`

冻结 manifest 的核心字段包括：

- `phase1` / `phase2`：上游 run ID 及安全 bundle 文件 SHA256；
- `source_accounting` / `source_outcomes`：保留 45 条来源的 `success`、`parse_error`、`provider_error` 完整核算；
- `selection_rule`：在方法预测和人工标签前冻结的纳入、备用和停止规则；
- `traces` / `ordered_trace_ids`：顺序必须完全一致；
- `paired_method_ids`：按固定顺序声明 Test-only、Direct LLM Judge、四层结构化 Judge、四层 + AST、完整 TraceJudge；Prompt、模型和方法参数到 Gate C 的运行计划再冻结；
- `privacy_policy_version`：公共 writer 使用的白名单/阻断策略版本。

自然轨迹通过 `Phase1ResponseReference` 绑定阶段一精确 JSONL 行和 `code_sha256`，通过 `Phase2FunctionalEvidenceRef` 绑定阶段二脱敏结果行及同一代码哈希。反事实通过 `CounterfactualMutation` 绑定 parent、唯一修改和前后哈希；代码变化时禁止 `reuse_same_code`，必须取得独立 EvalPlus 或公开 Fixture 证据。

Gate B 自然冻结目录为：

```text
artifacts/experiments/phase3-freezes/<freeze_id>/
└── manifest.json
```

目录权限为 `0700`、文件为 `0600`，同名目录存在时拒绝覆盖。冻结器会验证阶段一、阶段二和公开数据集的完整身份，但阶段二代码只打开 `manifest.json`、`summary.json`、脱敏 `results.jsonl` 与白名单 `execution.log`；不会打开 `samples.jsonl` 或 `evalplus_raw_results.json`。输出不含题面正文、候选代码正文、结构化说明正文或 Provider raw，只保存公开数据身份、顺序、状态、逐行引用和 SHA256。

### 公开反事实源、独立证据与 overlay

公开反事实源为 `data/phase3/public_counterfactuals_v1.json`，`kind` 固定为 `tracejudge_phase3_public_counterfactual_source`，精确文件 SHA256 固定为 `a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df`。它包含 3 个不计入研究分母的公开父 Fixture，以及按 `reasoning_swap`、`code_defect`、`boundary_deletion`、`shortcut`、`equivalent_implementation` type-major 顺序排列的 15 条反事实。每类恰有 3 条；同一父题每类最多一条。reasoning 变体必须保持代码字节完全相同且只改变结构化说明；其余四类必须保持说明完全相同、只改变代码，并为每个变体使用唯一代码字节。

`phase3 counterfactual-execute` 不接受任意 source：只有上述精确 SHA256 可进入 `TrustedLocalSandbox`。执行主体是 3 个父代码和 12 个改码变体；3 个 reasoning 变体不重复执行，而是复用父版本的同代码证据。执行 bundle 为：

```text
artifacts/experiments/phase3-public-evidence/<execution_run_id>/
├── manifest.json
└── results.jsonl
```

`PublicFixtureExecutionManifest` 固定 source bundle、15 个执行主体顺序、每个主体的 `code_sha256` / `public_fixture_sha256` / `replay_spec_sha256`、预期状态及 `results.jsonl` 哈希。每个 `PublicFixtureExecutionResult` 保存公开 case 的 expected/actual/异常类型、通过标记和关联需求，但不保存代码正文、stdout/stderr、异常消息或耗时；冻结校验会根据源 Fixture 重新核对 case 顺序、expected、关联需求和 pass 语义。timeout、基础设施错误、任一预期影响不一致或记录篡改都会阻断 overlay 冻结；执行产物本身仍保留真实失败，不自动重试。

`CounterfactualCohortManifest` 是不可变 overlay，而不是改写已冻结自然 manifest。它包含：

- `natural_cohort`：自然 freeze ID、精确 manifest SHA256、自然轨迹数、原顺序及顺序哈希；
- `source` / `execution`：公开源和独立执行 bundle 身份；
- `parents`：只含父 Fixture 的题面/说明/代码哈希及父证据引用，不进入配对分母；
- `counterfactuals`：15 条 `CounterfactualTrace`，逐条绑定 parent、唯一修改、预期影响、前后哈希和自身功能证据；
- `paired_ordered_trace_ids`：自然顺序在前、反事实 type-major 顺序在后，并带独立哈希；
- `paired_method_ids`：固定五种方法顺序。

正式 overlay 已将 42 条自然轨迹与 15 条反事实冻结为 57 条配对研究轨迹；自然和反事实结果仍必须分开汇总。该 evidence run/overlay 只构成 Gate B 功能证据与研究输入，不能表述为五方法已运行或研究假设已验证。

### 方法结果与配对索引

单个方法事件使用 `MethodOutcome`，终态严格区分：

```text
valid_judgment
provider_error
parse_error
ast_error
public_execution_timeout
infrastructure_error
skipped
reused
```

只有 `valid_judgment` 可以带结构化 `judgment`。`reused` 必须引用旧结果行 SHA256；Provider/Judge、AST、公开执行和基础设施错误不得相互伪装。最终 `PairedEvaluationIndex` 必须按 trace-major 顺序列出“全部冻结轨迹 × 五种方法”的每一对，即使该对的状态是失败，也不能漏行。

Gate C 的 `MethodOutcome.usage` 另行保存 `prompt_tokens`、`completion_tokens`、`reported_cost_microusd` 和 `cost_status`。Provider 不报价时 `cost_status=unavailable` 且成本为 `null`，不得把未知成本写成零；Test-only 与 resume 复用行为 `not_applicable`。四个 LLM 方法只接受完整严格 JSON，不从围栏或前后文中抽取局部对象；第二次 Provider 调用只能是首次 schema 失败后的唯一脱敏修复，Provider 错误不自动重试。

### 公开错误证书

`Phase3ErrorCertificate` 使用三个等级：

- `confirmed_bug` 必须有公开反例与 replay 命令；
- `strongly_supported` 必须有可公开复算的静态证据，且没有可执行反例；
- `unverified_suspicion` 不得携带可复现证据或 replay 命令。

Gate D 的 `PublicCertificateClaimsBundle` 是明确标记为 `self_constructed_phase3_gate_d_engineering_fixture` 的公开工程输入，固定 SHA256 为 `3b1df5e5a1e43c1b91e626c8656495a03d332bd4a5231550eb88c8928b93bb5f`。三个 claim 分别要求公开执行证据、可复算 AST/对齐规则或仅 Judge claim；生产器依据证据强度得到三个等级，不把 `judge_claim_only` 升级为可复现错误。

`Phase3PublicCertificateManifest` 绑定自然/overlay manifest、公开反事实源、Gate B 公开证据 manifest/results、claim bundle、证书策略、证书顺序、逐证书文件 SHA256 和三等级原始数量。证书文件不保存候选代码、参考实现、stdout/stderr、异常消息、官方测试或隐藏输入。`confirmed_bug` 的执行证据哈希由公开输入、预期/实际结果、异常类型、代码/公开源/replay spec 哈希共同计算；`phase3 replay` 从精确白名单源恢复代码并重新计算同一哈希，而不是执行证书提供的代码。

### Gate E1 盲法标注包

`AnnotationPacketManifest` 绑定 cohort、标注协议/指南、57 条材料组合哈希、固定随机种子、标注者/轮次、opaque item 顺序及三个 JSONL 哈希。`packet.jsonl` 包含标注者可见材料；`identity_map.jsonl` 将 item 连回真实 `trace_id`，仅由协调者保管；`labels_template.jsonl` 只含 `pending` 空字段。三者按精确字节 SHA256 绑定，全部位于 Git-ignored 私有目录。未填写模板不是人工标签，不得进入统计。

`BlindedAnnotationTask` 只保留 opaque `annotation_item_id`、题号与三个来源哈希、公开题面、结构化说明/候选代码、脱敏功能证据和公开动态证据可用性。方法预测、Provider raw、其他标注者标签、反事实修改/预期影响/预期状态、官方隐藏输入和 EvalPlus raw 都在白名单之外。

### Gate E2 人工标签冻结

working JSONL 的每行保留 packet 给定的 `annotation_item_id`、协议/标注者/轮次/盲法元数据和八个标签字段。`status=pending` 时标签字段必须为 `null`；`status=completed` 时 `process_correct`、`has_error`、`reasoning_correct`、`plan_code_aligned` 必须为布尔值，`rationale` 必须非空。`process_correct` 必须与 `has_error` 互为补集；无错误时推理/对齐为真且故障字段全为 `null`，有错误时至少给出首错层级、错误类型和理由。行数、顺序、item ID 和所有元数据必须与 packet 精确一致。

`AnnotationSetManifest` schema v2 仅代表已完成的私有冻结集。它绑定协议/指南/cohort、源 packet manifest/packet/identity map/template、用户 working 文件原始字节、规范化 `completed_labels.jsonl`、trace-major `annotations.jsonl`、标注者/轮次和自然/反事实数量。首轮主标注的 `agreement_kind=not_computed`；不得把单人一轮标记为 inter-rater 或 intra-rater 一致性。

正式 `phase3_labels_primary_round1_v1` 已冻结 57 条。其 manifest、`completed_labels.jsonl`、`annotations.jsonl` 精确 SHA256 分别为 `fbf89aa950318392e49d01a5235461c4ce6ae94acb55842b963bb54048eac0a3`、`17b4e1b43fd2161aff7a0b3d63a7f5f31a89992db9fe75823697d2ac4c32d98d`、`ffbee2c546a6e0f560a96c8c610661258216c9ef627af8ffd3e6ff60ca1e8299`。

公共 payload 发布前必须通过 `assert_public_payload_safe()`；它拒绝 canonical solution、官方测试/失败输入、EvalPlus raw、reference code、Provider raw、请求头、Cookie、token/secret 等敏感字段及调用方 canary，异常消息不回显秘密值。身份哈希使用 `canonical_sha256()`；精确 JSONL 行使用包含末尾 LF 的 `jsonl_record_sha256()`。

### 阶段四 P1 第二标注者练习包

`tracejudge_phase4_p1_second_annotator_protocol` schema v1 冻结安排/阶段三指南哈希、指导老师于 2026-09-02 作出的 `approved / READY` 认定、20 条正式子集规则、5 条练习准入门槛、最多两轮校准、盲法、交付、停止、退出和一致性分析政策。其 `delivery_record_status=pending_completion`、`formal_packet_created=false` 和 `formal_data_collected=false` 是预注册冻结时的状态，不回写；后续状态分别由受限交付记录、正式 packet manifest 和正式标签 manifest 追加证明。当前正式回传已由后续私有 manifest 证明完成并冻结，不反向修改预注册时点字段。

`P1SingleDeliveryRecord` schema v1 作为公开、无个人信息的结构定义；实际记录只保存在 Git-ignored、`0700/0600` 私有目录。它绑定 Schema/Protocol/伦理身份，覆盖参与同意、五类实际渠道、确认收件与期限、报酬/致谢/署名、退出/保留/销毁、单次负责人授权、归档哈希、异常数和删除确认。`pending_completion` 可保留空值但强制 `data_collection_allowed=false`；只有全部前置项完整并显式切换为 `ready_for_practice_delivery` 时才可为真。CLI 只回显状态、缺失项数量和哈希，不回显渠道或联系人正文。

`tracejudge_phase4_p1_formal_subset_private_manifest` schema v1 保存确定性入选身份，只位于 Git-ignored `0700/0600` 目录；`tracejudge_phase4_p1_formal_subset_public_commitment` 只发布源 manifest 哈希、固定种子/算法、15+5 计数、3 个父题覆盖、单父题上限、顺序承诺哈希和私有 manifest 哈希，不包含入选 trace/problem ID、私有路径、标签、方法预测、Provider 状态或事后结果。两者的“未生成 formal packet”声明是子集冻结时状态；当前状态以后续的正式 packet manifest 为准。

`tracejudge_phase4_p1_practice_admission` schema v1 是 Git-ignored 私有准入记录。它精确绑定完成标签、回传归档和协调者参考的 SHA256，仅保存 Schema 有效数、三项预注册一致计数、零隐私/盲法异常确认和书面准入决定。它不复制标签、rationale、个人信息或渠道正文，并明确将练习分数排除于研究终点。

`tracejudge_phase4_p1_formal_annotation_packet` schema v1 只在单次交付记录允许数据收集、练习准入通过、正式 20 条子集逐字节验证后才可创建。它按固定哈希顺序产生 `formal_item_001..020`，并绑定三个文件：

- `participant/packet.jsonl`：20 条公开可见题面、结构化说明、候选代码、功能证据和公开动态证据；
- `participant/labels_template.jsonl`：20 条待填空标签，固定标注者、轮次和盲法元数据；
- `coordinator/identity_map.jsonl`：opaque item ID 到真实 trace 身份的映射，仅协调者保管。

参与者两个文件不含主标签、方法预测、Provider raw、反事实 mutation/预期影响、官方隐藏输入或身份映射。全部文件以 `0700/0600` 私有权限、不可覆盖写入，并可由 manifest 确定性重建后逐字节校验。

`tracejudge_phase4_p1_formal_labels` schema v1 是正式第二标注者回传的 Git-ignored 私有冻结集。它保存协调者报告的带时区收件时间，并分别记录本地文件系统观察到的 archive/labels 修改时间，不用文件修改时间反推发送时间；绑定正式截止时间、原始 `.7z`、原字节 `completed_labels.jsonl`、正式 packet manifest/packet/template/identity map、交付记录和回连后的 `annotations.jsonl`。预检只回显条数、类别计数、期限状态和 SHA256，不回显条目身份、具体标签或 rationale。冻结目录与文件权限为 `0700/0600` 且不可覆盖；一致性分析前 `agreement_kind=not_computed`。

`tracejudge_phase4_p1_adjudication_bundle` schema v1 是唯一一条过程细节分歧的 Git-ignored 私有待裁决包。它绑定冻结的 aggregate-only 一致性 manifest/analysis SHA256，并通过 `tracejudge_phase4_p1_adjudication_record` 保存精确的聚合分歧形状、`record_version=1` 和 `status=pending_human_review`。初始版本的条目 ID、trace 哈希、两份原标签哈希、裁决者、结论、rationale 和时间均必须为 `null`，并声明初始化器未访问逐条参与者数据。`tracejudge_phase4_p1_adjudication_working_record` 是供授权协调者复制到新的受限目录后填写的工作模板；本包内冻结模板本身不允许携带任何决定字段。初始化目录不可覆盖，manifest 绑定三个 payload 的逐字节 SHA256，目录/文件权限为 `0700/0600`。该 schema 只证明已建立可审计起点，不表示人工裁决已完成。

`tracejudge_phase4_p1_completed_adjudication_bundle` schema v1 是上述起点之后独立追加的私有完成态包。`tracejudge_phase4_p1_completed_adjudication_record` 绑定 pending manifest/record、aggregate-only 来源、正式非标签 packet 及盲化案例的规范化 SHA256，只保存发生分歧的 `plan_code_aligned`、`first_faulty_layer`、`first_faulty_step`、`error_type` 四字段及人类共识理由。它要求两位原始标注者确认、方法预测盲法、带时区起止时间和 AI 技术建议披露；明确不复制非分歧字段、不读取或嵌入两份逐条原标签/原 rationale，也不把共识回写主标签或 raw agreement。完成态目录仍为不可覆盖的 `0700/0600`，manifest 绑定 decision/report 哈希，并标记 `public_release_allowed=false`。

`tracejudge_phase4_p1_post_adjudication_sensitivity` schema v1 是完成裁决后的公开、聚合级 post-hoc 敏感性分析。它只绑定 aggregate-only 一致性 manifest 和私有完成态 manifest，保留原始完整记录 19/20、`has_error` 20/20 等 raw agreement，并另行记录 1/1 分歧已解决；`post_adjudication_inter_rater_agreement_created=false`，禁止把裁决状态写成新的 20/20 标注者一致率。`impact_envelopes` 对无分歧字段给出零影响，对四个分歧字段、联合定位标签和完整记录给出固定分母下最多一个样本的绝对变化上界。该 schema 不含逐条身份、原标签、最终裁决值、裁决理由或方法预测，也不创建共识标签集；公开 JSON / Markdown SHA256 为 `377725050f8adbb4afe88f0b0e01ae05b4a2bc670c6920034fc8bb5b0472a48b` / `7dd2f1f244c3bd09a2928b61c1ee36cb25e59a88e8631e0be3807d504384866d`。

`tracejudge_phase4_p1_inter_rater_agreement` schema v1 将第二标注者的 20 条与首轮主标注 57 条中的相同轨迹配对，只输出 aggregate-only 统计。分析前要求两份 manifest 匹配已记录 SHA256、两份 annotation records 匹配各自 manifest、20 条代码/结构化说明/功能证据哈希一致、15+5 构成正确。两轮协议文件的 SHA256 不同，但都绑定同一份阶段三标注指南，并经同一个 `AnnotationRecord` 七字段 Schema 约束；分析显式保存两份协议哈希和共享指南哈希，不以协议字节相同作为伪条件。

二元字段保存双向四格计数、原始一致率及 Wilson 95% 区间、正/负类一致率和适用条件下的 Cohen's κ；κ 区间使用固定 seed `20260904` 的 10,000 次配对条目 percentile bootstrap。首错层/步骤/错误类型及其联合标签分别报告“全 20 条（含共同 null）/至少一人判错/双方都判错”三个精确一致率，不对稀疏条件多分类字段报告 κ。产物明确声明原始标签未改写、未裁决、未输出分歧条目、trace ID、逐条标签或 rationale，Provider/Docker/网络调用均为 0。

正式聚合包 `phase4_p1_inter_rater_agreement_v1` 的 manifest / `agreement.json` / `report.md` SHA256 分别为 `20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76` / `fe9c66d505c0ce472deb652676ac38ea4d6849547323a1e3061ad1d9deea2135` / `0f3134d18a1d3fda1c4235951c442d57651fe587c6201df0549568818f677734`，目录/文件权限为 `0700/0600`，不可覆盖，且已从两份冻结源确定性复算验证。

`tracejudge_phase4_p1_public_practice_source` schema v1 只包含 5 条 MIT 公开自建 Fixture，不包含协调者参考。它不是人类参与者数据；生成器只在源文件精确匹配冻结 SHA256 后执行候选代码，并与阶段三自然/overlay manifest 的题号、代码哈希和结构化说明哈希检查零重合。

`tracejudge_phase4_p1_public_practice_bundle` schema v1 是确定性、不可覆盖的公开练习冻结：

- `manifest.json` 绑定安排、Protocol、阶段三指南、练习源、自然/overlay manifest、生成实现、顺序、三个 JSONL 哈希及 Provider/Docker/网络零调用声明；
- `participant/packet.jsonl` 只包含公开题面、结构化说明、候选代码、脱敏状态和公开执行证据，不包含参考答案或阶段三标签；
- `participant/labels_template.jsonl` 固定化名 `p1_rater_02`、第一轮校准和八个 `null` 标签字段；
- 协调者参考是非参与者的公开 Fixture 参考，但为保护准入练习有效性，只保存于 Git-ignored、`0700/0600` 受限目录；公开 manifest 只保存逻辑 artifact ID 和精确 SHA256，不保存私有路径或正文。

### Resume identity

`Phase3ResumeIdentity` 精确绑定 overlay 与自然 manifest、轨迹顺序、方法材料组合哈希、五方法规格、Prompt bundle、输出 schema、完整实现、Provider 公开配置、人工标签 manifest/完成标签/回连记录哈希、Git、Python/依赖、AST、公开证据策略、标注协议和随机种子。clean Git 状态不保存伪造工作树指纹；dirty 状态必须有指纹。Gate B freeze 是不可变的一次性原子发布，不使用 resume。Gate E3 writer 每次 invocation 保存私有 `provider_raw.jsonl` 和通过敏感键/canary 检查的 `results.jsonl`；中断运行的 manifest 保持 `running`，续跑时先将前一 invocation 标为 `interrupted`。新 invocation 对历史已有终态的每一对只写 `reused` 和精确旧行 SHA256，不再调用 Provider；完成时输出完整 57 × 5 索引。

### Gate E4 聚合统计

`artifacts/experiments/phase3-statistics/<statistics_id>/` 只包含权限为 `0600` 的 `manifest.json` 与 `report.json`，目录权限为 `0700`，同名目录拒绝覆盖。manifest 绑定 cohort/natural manifest、人工标签 manifest/completed/annotation 三个哈希、E3 run manifest/results/index、统计实现、Git/Python/直接依赖和 report 精确字节哈希，并明确声明不含逐轨迹行、人工理由、Provider raw 或隐藏评测内容。

`report.json` 是确定性的聚合对象：包含分析口径、自然/反事实/总数量、原始与有效状态计数、每方法分子/分母及 Wilson 区间、有效结果混淆矩阵、两个自然 exact McNemar + Holm 比较、两个反事实父题聚类 percentile bootstrap 区间和五类反事实描述性拆分。它不包含 `trace_id`、`rationale`、逐条 judgment 或证书正文；`valid_only_confusion` 不能替代把失败计错的全分母主指标。

### Gate F 脱敏报告

`artifacts/experiments/phase3-reports/<report_id>/` 为 `0700`，内部五个文件均为 `0600` 且同名目录拒绝覆盖：

- `manifest.json`：绑定 E4 manifest/report、E3 manifest/results/index、Gate D 证书 manifest/confirmed 证书/公开执行证据、报告实现、Git/Python/直接依赖与四个输出 payload 哈希；
- `phase3_research_report.md`：脱敏结果解读，包含原始分子/分母、失败核算、预注册比较、探索性首错/反事实结果、限制、Material Passport 和 11/11 统计谬误扫描；
- `validation.json`：机器可审计的 `ANALYZED / CAUTION / CANNOT_VERIFY` 验证记录；
- `demo_certificate.json`：Gate D 公开 confirmed 工程 Fixture 证书的精确副本；
- `replay_command.txt`：证书携带的精确公开重放命令，不表示 Gate F 已执行重放。

Gate F 不写入逐轨迹标签/预测、标注理由、Provider raw、候选正文或隐藏评测内容。`demo_certificate.json` 只是公开工程 Fixture，不能当作五方法在研究 cohort 上的证书准确率。

## 流水线输出 JSON（`artifacts/*.json`）

这是 `run` / `batch` / `demo` 现有完整评估链路的格式，**与上述阶段一基线产物不同**。它由 `reporting/serializer.py:pipeline_result_to_dict()` 生成，顶层字段：`problem` / `solution` / `static_evidence` / `execution_result` / `llm_assessment` / `process_assessment` / `counterexample` / `error_certificate`，均为对应 Pydantic 模型的 `model_dump(mode="json")`。`batch` 命令将多条这样的记录以 JSONL 形式写入同一个文件。

HumanEval+ 阶段一公开投影没有测试或参考实现，`run` / `batch` 会在 Provider/沙盒执行前明确拒绝它；阶段一只能使用带匹配 `--dataset-manifest` 的 `baseline`，阶段二只能从已完成 run 使用独立 `evalplus`。生成/解析统计不是功能分数；固定 10 题执行统计也不是完整 HumanEval+ 结果或正式 benchmark 排名。
