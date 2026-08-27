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

## 流水线输出 JSON（`artifacts/*.json`）

这是 `run` / `batch` / `demo` 现有完整评估链路的格式，**与上述阶段一基线产物不同**。它由 `reporting/serializer.py:pipeline_result_to_dict()` 生成，顶层字段：`problem` / `solution` / `static_evidence` / `execution_result` / `llm_assessment` / `process_assessment` / `counterexample` / `error_certificate`，均为对应 Pydantic 模型的 `model_dump(mode="json")`。`batch` 命令将多条这样的记录以 JSONL 形式写入同一个文件。

HumanEval+ 阶段一公开投影没有测试或参考实现，`run` / `batch` 会在 Provider/沙盒执行前明确拒绝它；阶段一只能使用带匹配 `--dataset-manifest` 的 `baseline`，阶段二只能从已完成 run 使用独立 `evalplus`。生成/解析统计不是功能分数；固定 10 题执行统计也不是完整 HumanEval+ 结果或正式 benchmark 排名。
