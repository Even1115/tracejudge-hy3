# TraceJudge-Hy3

**基于“需求—推理—代码—执行证据”四层对齐，评估 AI 生成 Python 函数代码是否真正正确，并定位首个错误步骤、错误类型和代码位置。**


---

## 1. 项目要解决的问题

主流代码生成评测通常只看"代码是否通过测试"，这无法回答：模型是否真正理解了需求？解题思路是否成立？代码是否真的实现了它自己声称的方案？错误最早从哪一步开始？"代码通过测试"是否只是碰巧、投机或硬编码？

TraceJudge-Hy3 不只判断代码是否通过测试，还尝试验证它是否"对因而对"，并为疑似错误生成可以执行、可以复现的证据（而不是仅凭一次 LLM 判断下结论）。

完整的研究级方案（反事实配对集、消融实验、HumanEval+/MBPP+ 大规模评测等）见 [`TraceJudge-Hy3：基于四层对齐与可执行错误证书的代码生成过程评估系统.md`](TraceJudge-Hy3：基于四层对齐与可执行错误证书的代码生成过程评估系统.md)。**本 README 描述的是 v0.1 MVP 的真实实现范围，二者不完全一致，以本 README 和 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 为准。**

## 2. 四层对齐架构

```text
┌─────────────────────────┐
│ 题目：需求 + 函数签名     │
│ 可见/隐藏/挑战测试 + 参考代码 │
└────────────┬────────────┘
             ↓
┌─────────────────────────┐
│ Solver（Mock 或 Hy3）    │
│ 需求理解 / 设计摘要 / 边界 │
│ 分步实现计划 / 代码       │
└────────────┬────────────┘
             ↓
   ┌─────────┴─────────┐
   ↓                    ↓
┌───────────────┐  ┌───────────────────┐
│ AST 静态分析    │  │ 沙盒执行            │
│ 分支/循环/数据结构│  │ 可见/隐藏/挑战测试   │
│ 空输入判断/硬编码 │  │ 异常/超时/输出       │
└───────┬───────┘  └─────────┬─────────┘
        └─────────┬──────────┘
                   ↓
┌──────────────────────────────────┐
│ 四层评估器                         │
│ 需求—推理 / 推理内部一致性           │
│ 推理—代码 / 代码—执行证据            │
│ 规则证据 + LLM 判断交叉验证          │
└────────────────┬──────────────────┘
                   ↓
┌──────────────────────────────────┐
│ 反例生成与差分验证                  │
│ challenge/hidden 测试 → 边界候选     │
│ → 参考实现 vs 候选实现差分执行       │
│ → delta-debugging 最小化            │
└────────────────┬──────────────────┘
                   ↓
┌──────────────────────────────────┐
│ 可执行错误证书                     │
│ confirmed_bug / strongly_supported │
│ unverified_suspicion               │
│ 复核转移: cleared；首次正确: 无证书 │
└──────────────────────────────────┘
```

## 3. v0.1 已实现功能

- Pydantic v2 严格数据模型：`ProblemSpec`、`SolutionTrace`、`ExecutionSummary`、`StaticEvidence`、`ProcessAssessment`、`ErrorCertificate` 等（[`src/tracejudge_hy3/schemas/`](src/tracejudge_hy3/schemas/)）。
- 确定性 Mock Provider，内置真实、完整、符合 Schema 的示例解答（不是占位字符串），**无需真实 API Key 即可跑通端到端链路**（[`src/tracejudge_hy3/providers/mock.py`](src/tracejudge_hy3/providers/mock.py)）。
- 可选的 Hy3 OpenAI-compatible Provider：环境变量配置、超时与可配置的有限重试、JSON Schema/上下文引用校验后的修复重试（`HY3_MAX_RETRIES` 控制总额外调用，`HY3_MAX_PARSE_REPAIRS` 单独控制 JSON 修复调用硬上限）、耗时记录、日志中不暴露密钥片段（[`src/tracejudge_hy3/providers/hy3_openai.py`](src/tracejudge_hy3/providers/hy3_openai.py)）。
- 阶段一基线生成器：为每次运行创建唯一 `run_id`，逐题原子持久化原始输出与解析后 `SolutionTrace`，支持断点续跑、单题失败隔离和非敏感实验元数据（[`src/tracejudge_hy3/baseline/`](src/tracejudge_hy3/baseline/)）。
- 基于 `ast` 的静态分析：`if` / `for` / `while` 分类计数、输入相关循环、最大嵌套深度、比较运算符、数据结构、函数调用、返回行号、空输入与可疑硬编码启发式（[`src/tracejudge_hy3/static_analysis/ast_analyzer.py`](src/tracejudge_hy3/static_analysis/ast_analyzer.py)）。
- 沙盒执行：`DockerSandbox`（默认，用于真实模型代码，仅提供基础隔离）与 `TrustedLocalSandbox`（默认仅允许仓库内置且精确匹配的 Mock Fixture；阶段三另只放行 SHA256 精确匹配的公开反事实 bundle；其他代码需显式 `--allow-unsafe-local-exec`）（[`src/tracejudge_hy3/sandbox/`](src/tracejudge_hy3/sandbox/)）。
- 测试运行器：位置/关键字参数从 JSON 加载，不使用 `eval()`；父进程为每个用例创建新子进程并强制超时，有界捕获 stdout/stderr，独立记录输出、异常、超时和退出码（[`src/tracejudge_hy3/sandbox/test_runner.py`](src/tracejudge_hy3/sandbox/test_runner.py)）。
- 规则证据 + LLM 判断的四层评估：空输入声明—代码不一致、集合声明—代码不一致、单次遍历声明—嵌套循环不一致、复杂度声明不一致、执行失败归因（[`src/tracejudge_hy3/evaluator/`](src/tracejudge_hy3/evaluator/)）。
- 反例生成：优先复用与当前违反需求条款相关的 challenge/hidden 测试失败结果，其次基于相关测试的参数形状生成有限边界候选并与参考实现差分执行，并对列表参数做简单 delta-debugging 最小化（[`src/tracejudge_hy3/counterexample/`](src/tracejudge_hy3/counterexample/)）。
- 可执行错误证书聚合：新疑似问题直接产生 `confirmed_bug` / `strongly_supported` / `unverified_suspicion` 三种裁决。普通首次运行正确时不产生证书；`cleared` 仅用于显式传入既有证书后，复核的完整执行证据表明原疑似问题不再成立的状态转移（[`src/tracejudge_hy3/evaluator/evidence.py`](src/tracejudge_hy3/evaluator/evidence.py)）。
- CLI（Typer + Rich）：`doctor` / `demo` / `dataset convert-humanevalplus` / `dataset sample` / `dataset validate` / `baseline` / `evalplus` / 阶段三 Gate B–F 命令 / 阶段四 `artifact-preflight`、`artifact-freeze`、`artifact-verify`、`replay-receipt-preflight`、`replay-receipt`、`charts-preflight`、`charts-publish`、`charts-verify` / `run` / `batch`（[`src/tracejudge_hy3/cli.py`](src/tracejudge_hy3/cli.py)）。
- HumanEval+ 阶段一公开投影适配器：校验本地固定 revision 快照及其受控来源 manifest，把 164 道公开题面转换为不含答案/测试的 `ProblemSpec`，并仅依据公开 `problem_id` 生成固定种子 10 题 Pilot（`20260824`）或排除 Pilot 后的 45 题自然研究子集（`20260825`，schema v2）（[`src/tracejudge_hy3/dataset/humanevalplus.py`](src/tracejudge_hy3/dataset/humanevalplus.py)）。
- HumanEval+ 阶段二官方执行适配器：严格验证阶段一产物后只导出 `solution_trace.code`，支持 `all` 与 `phase1-success-only` 两种选择策略；在固定 digest 的官方镜像中逐题运行 Base 和 Extra 测试，并生成脱敏的单样本工程结果（[`src/tracejudge_hy3/evalplus/`](src/tracejudge_hy3/evalplus/)）。
- 阶段三 Gate A–C 已完成：正式自然 manifest 冻结 42 条轨迹，SHA256 `a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692`；公开反事实证据 run 独立执行 15 个主体，实际 `6 pass / 9 fail`、0 超时、0 基础设施错误、0 预期偏差，results SHA256 `19a138ecc2ce784b940e88e085a85ddddf92a564be7235bbd5a3e97bb39d2776`；最终 overlay 冻结 42 + 15 = 57 条五方法配对顺序，SHA256 `3290221625d687e6d7412a0544247dc81a34857b114a545458b93cc04e35d255`。Gate C 只读验收核算 57 × 5 = 285 个配对，方法规格、Prompt bundle、输出 schema SHA256 分别为 `4b8684852125ad3059b5001951479a2f164c7089eb64ff10cbdafafc39c534ff`、`c8d6c2c0f6bb1207af987746d912868bd102f90b334f5425528cbda5be9dd366`、`96da92777ee89bb69a65c61f4bdc9fc9e7cb7ac1ba94a52400f79ca1130821f3`，且未执行方法、Provider、Docker 或网络（[`src/tracejudge_hy3/phase3/`](src/tracejudge_hy3/phase3/)）。
- 阶段三 Gate D 已完成三等级公开工程证书发布与 confirmed 证书独立重放；Gate E1 正式私有盲法 packet 已导出，Gate E2 单人首轮 57 条标签已冻结。Gate E3 正式运行 `phase3_hy3_57x5_v1` 已产出完整 285 配对，其中 `valid_judgment=283`、`provider_error=2`；results / index SHA256 分别为 `332932e949281c84402046dbd25e0110fb7a7e7e224c71b17487226fa1098999` / `b1a6c6a61a4439d3e667ebd52ddba8cba98f8ee196c1cac8dce200f38c857247`。Gate E4 正式聚合统计已冻结，manifest / report SHA256 分别为 `7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8` / `972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`。Gate F 正式脱敏报告 `phase3_report_primary_round1_v1` 已发布，manifest / Markdown / validation SHA256 分别为 `0b8285ec04344e29670d752a37c4d5ecb41ea07d5dfc18a5715b56de3e800b06` / `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725` / `702bf96be5d0911088dfea5cb95562d6b8e25d147d972c78b0b6870cecbae113`，验证状态为 `ANALYZED`、总体置信为 `CAUTION`。
- 阶段四 P0 Gate A、B、C、E 的仓库内交付已完成：103 个关键产物的权限/哈希清单、13 个公开锚点、恢复验证和公开 replay receipt；Gate F Markdown 已以相同 SHA256 发布为受 Git 跟踪的 [正式脱敏报告](docs/releases/phase4/phase3_research_report_public_v1.md)，并附带 [发布审计说明](docs/releases/phase4/phase3_research_report_publication_notes_v1.md)；三张确定性聚合图表、[2 分钟公开 Fixture Demo](docs/releases/phase4/phase4_fixture_demo_v1.md)、[Release 检查单](docs/releases/phase4/phase4_release_checklist_v1.md)和[封版报告](docs/releases/phase4/phase4_closure_report_v1.md)均已就绪。Gate D/P1 研究增强延期且不阻塞 P0；该发布不重跑 Hy3，复现判定仍为 `CANNOT_VERIFY`。push、PR、merge、tag、Release 和附件上传仍待明确授权。
- 3 道内置示例题（`safe_mean` / `deduplicate_preserve_order` / `clamp`），来源标记为 `self_constructed_mvp_fixture`（见 §10 和 §15）。
- 指标计算：10 个纯函数指标，缺少人工标注时返回 `not_computable` 而不是伪造数值（[`src/tracejudge_hy3/reporting/metrics.py`](src/tracejudge_hy3/reporting/metrics.py)）。
- 单元测试与集成测试（`pytest`），Lint（`ruff`）。

## 4. v0.1 尚未实现的功能

见 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 的完整分类，摘要如下：

- HumanEval+ 数据的项目内自动下载、完整 164 题正式评测，以及 MBPP+ 接入与大规模评测；
- 第二标注者/重测一致性、扩展消融与更大规模复现；
- Web 前端、多 Agent 编排；
- 仓库级代码修改、多文件生成、多语言执行；
- 完整控制流图 / 符号执行 / mutation testing；
- 自动代码修复；
- 通用属性测试（仅有基于参数类型的有限边界候选）。

## 5. 安装

需要 Python 3.11+（推荐 3.12）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 6. 运行 Mock Demo 与阶段一实验（Mock 无需真实 API Key）

```bash
tracejudge doctor
tracejudge demo --mock --case faulty
tracejudge demo --mock --case correct
```

- `--case faulty`（默认）：reasoning 声称"先检查空列表并返回 0.0"，但代码实际只有 `sum(nums) / len(nums)`。可见测试全部通过；隐藏/挑战测试中的 `safe_mean([])` 暴露除零错误；系统输出 `A01_PLAN_CODE_MISMATCH`、首错步骤 `S1`、违反需求 `R1`、`[]` 反例，以及 `confirmed_bug` 错误证书。
- `--case correct`：同样的 reasoning，代码中包含 `if not nums: return 0.0`，全部测试通过，不产生错误证书。

完整 JSON 结果会保存到 `artifacts/demo_<case>_<timestamp>.json`（该目录已加入 `.gitignore`，只保留 `.gitkeep`）。

其他命令：

```bash
tracejudge run --dataset data/sample_problems.jsonl --problem-id clamp \
  --provider mock --sandbox trusted-local

tracejudge batch --dataset data/sample_problems.jsonl --provider mock \
  --sandbox trusted-local --limit 3 \
  --output artifacts/batch_results.jsonl
```

上述 `trusted-local` 命令之所以无需 unsafe opt-in，是因为三道内置题使用的是仓库自带、精确校验过的 Mock Fixture。`MockProvider` 对未知题目会 fallback 到数据集的 `reference_code`，但这类 fallback **不视为可信 Fixture**：应使用 Docker，否则必须显式传入 `--allow-unsafe-local-exec` 并自行承担风险。

> **v0.1 的样例数据（`data/sample_problems.jsonl`、`data/demo_annotations.jsonl`）仅用于验证系统链路是否可靠运行，不代表正式 Hy3 benchmark 评测结果，也不是 HumanEval/MBPP 等公开 benchmark 的替代。**

### 阶段一：生成基线解答

先用确定性 Mock Provider 做一次不访问网络的 dry run：

```bash
tracejudge baseline \
  --dataset data/sample_problems.jsonl \
  --provider mock \
  --output-dir artifacts/experiments/phase1
```

配置好 Hy3 后，使用同一入口运行真实生成：

```bash
tracejudge baseline \
  --dataset data/sample_problems.jsonl \
  --provider hy3 \
  --output-dir artifacts/experiments/phase1
```

新运行会自动生成唯一 `run_id`，并在第一次模型调用前显示 `run_id` 和产物目录。若一次运行中断，可在数据集 SHA256、数据集 provenance/实验标签（传入 manifest 时）、Provider 公开配置、TraceJudge Git commit/工作树指纹以及 Python/直接依赖环境未变的前提下续跑：

```bash
tracejudge baseline \
  --dataset data/sample_problems.jsonl \
  --provider mock \
  --output-dir artifacts/experiments/phase1 \
  --resume-run-id <run_id>
```

续跑会为历史上已成功的 `problem_id` 追加 `skipped` 事件，并重试上次为 `parse_error` 或 `provider_error` 的题目；上一个遗留为 `running` 的 invocation 会标记为 `interrupted`。内置 Mock 输出是仓库手工维护的确定性离线 Fixture，**不是真实模型结果**；匹配只使用公开 Prompt 视图，修改 `reference_code`/隐藏/challenge 字段不会影响它。未知题或公开题面已改的内置题会明确失败，不会退化为使用 `reference_code`。批次仍会处理完其他题目并保留产物；若最终存在失败题，CLI 以非零状态退出。

该命令只进行 Solver 生成和结构化解析：**不导入或执行候选代码，不运行可见/隐藏/challenge 测试，不做四层评估、反例生成、人工标注或指标对比**。发给 Solver 的题目上下文仅包含 `problem_id`、`requirement`、`function_signature`、公开需求条款的 `requirement_id`/`content`，以及可见测试的 `args`/`kwargs`/`expected`；不包含 `reference_code`、隐藏测试、challenge 测试或人工标注。`SolutionTrace` 中的说明是面向用户、可审查的需求理解、设计摘要和实现计划，不要求或保存模型私有思维链。

每次运行的产物为：

```text
<output-dir>/<run_id>/
├── manifest.json
├── responses.jsonl
└── summary.json
```

- `manifest.json` 的顶层 `schema_version` 是整个阶段一 artifact bundle 的版本；新运行固定写 v2。它记录数据集绝对路径、SHA256 与摘要，Git commit/分支/工作树状态与指纹，Python 和直接依赖版本，以及 Provider、模型、`reasoning_effort`、超时和最大重试次数等显式允许的非敏感配置。传入受支持的 `--dataset-manifest` 时，还会保存经白名单筛选的 revision、许可证、适配器、原始快照/公开投影哈希、确定性选择参数和 manifest SHA256。Hy3 会先从 endpoint 剔除 userinfo、query 和 fragment，再保存规范化 endpoint 的 SHA256 以便一致性校验，不保存 endpoint 本身；API Key、Authorization Header、完整请求头和其他凭据均不写入。旧 v1 run 保持只读且仍可由阶段二 exporter 验证；新 writer 拒绝向 v1 run 续写，避免同一 run 混合两种 response schema。
- `responses.jsonl` 是 UTF-8 事件日志，每题记录 `started_at` / `ended_at` / `duration_seconds`、尝试与重试次数、枚举化 `attempt_outcomes`、`raw_output_attempt` / `parse_attempted` / `parse_status` 和结构化错误，并将凭据安全脱敏后的 `raw_output` 与解析后的 `solution_trace` 分开保存。`attempt_outcomes` 的元素仅为 `success` / `parse_error` / `provider_error`，长度严格等于 `attempt_count`；`retry_count = attempt_count - 1` 表示所有额外实际调用，既可能是普通 Provider 重试，也可能是解析失败后的 JSON 修复调用，不能单独用它判断 repair。解析错误摘要不包含 Pydantic `input_value`，修复轮也只回传脱敏后的旧输出。最终状态仅为 `success` / `parse_error` / `provider_error` / `skipped`；`skipped` 的 outcome 序列为空。每条记录都先写入同目录临时文件，再原子替换；单题失败或非法 Unicode 字符不会中止后续题目。
- `summary.json` 的“最终结果”按每个题目最后一条非 `skipped` 事件统计，resume 的事件不会重复计入题目级解析指标。除原有成功/失败、解析率和耗时外，v2 还报告首次调用即解析成功、是否遇到解析失败、是否实际发送过后续 repair 调用、repair 后成功、终态解析失败，以及平均调用/重试次数。只有 `parse_error` 后确实存在下一次调用才算 repair attempted；单纯的 Provider 失败重试不算 JSON repair。`parse_success_rate = parsed / (parsed + failed)`；根本没有可解析输出的 Provider 失败不进入分母，但“先解析失败、后续请求又失败”的混合序列会进入分母。summary 不计算功能正确率、错误检测率或其他阶段二及以后的指标。

`data/sample_problems.jsonl` 在基线产物中固定标记为 `self_constructed_mvp_fixture_pilot`。这是自建工程 Fixture 的小规模 pilot，不是 HumanEval+、MBPP+ 或任何正式 benchmark 结果。

### HumanEval+ 固定 10 题阶段一 Pilot

本仓库提交了受控来源 manifest [`data/manifests/evalplus_humanevalplus_d32357cf.json`](data/manifests/evalplus_humanevalplus_d32357cf.json)，但**不提交也不自动下载**原始 HumanEval+ 内容。先把指定 revision 的完整 Hugging Face 快照放到已被 Git 忽略的 `artifacts/datasets/raw/humanevalplus/`。例如，已安装 `hf` CLI 时可执行：

```bash
hf download evalplus/humanevalplus \
  --repo-type dataset \
  --revision d32357cf319e50e9c8d8dab5ea876c72b0fd321b \
  --local-dir artifacts/datasets/raw/humanevalplus
```

项目转换命令本身是离线的，并会在发布任何输出前核对 revision、许可证、164 条题目，以及来源 manifest 中列出的完整原始快照大小和 SHA256。随后依次转换公开投影、按固定种子抽取 10 题并校验：

```bash
tracejudge dataset convert-humanevalplus \
  --input artifacts/datasets/raw/humanevalplus/test.jsonl \
  --revision d32357cf319e50e9c8d8dab5ea876c72b0fd321b \
  --manifest data/manifests/evalplus_humanevalplus_d32357cf.json \
  --output-dir artifacts/datasets/processed/humanevalplus-full

tracejudge dataset sample \
  --dataset artifacts/datasets/processed/humanevalplus-full/problems.jsonl \
  --manifest artifacts/datasets/processed/humanevalplus-full/dataset_manifest.json \
  --count 10 \
  --seed 20260824 \
  --output-dir artifacts/datasets/processed/humanevalplus-pilot-10

tracejudge dataset validate \
  --dataset artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl
```

该选择算法只组合固定种子与公开 `problem_id`；当前固定结果按数值顺序为 `HumanEval/8`、`HumanEval/26`、`HumanEval/41`、`HumanEval/51`、`HumanEval/70`、`HumanEval/81`、`HumanEval/95`、`HumanEval/96`、`HumanEval/105`、`HumanEval/120`。转换目录与 Pilot 目录都采用完整目录原子发布：同内容重跑幂等，已有目录内容不同时会拒绝覆盖。

真实 Hy3 阶段一生成必须同时传入 Pilot manifest：

```bash
tracejudge baseline \
  --dataset artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --provider hy3 \
  --output-dir artifacts/experiments/phase1
```

HumanEval+ 公开投影的 `visible_test_cases`、`hidden_test_cases`、`challenge_test_cases` 均为空，`reference_code` 是固定的 withheld sentinel，`difficulty` 为 `unknown`。只有公开的 `task_id`、`prompt` 和 `entry_point` 参与投影；`canonical_solution` 和官方 `test` 不会复制到投影、Pilot bundle、Solver Prompt 或基线产物中，仍只存在于被 Git 忽略的本地原始快照。缺少 `--dataset-manifest` 或 manifest 与数据集哈希、题目顺序/选择、revision、许可证、适配器等不一致时，`baseline` 会在 Provider 调用前拒绝运行。

若需续跑，应原样提供同一数据集和同一 manifest：

```bash
tracejudge baseline \
  --dataset artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --provider hy3 \
  --output-dir artifacts/experiments/phase1 \
  --resume-run-id <run_id>
```

续跑除通用运行环境外还会精确比较已记录的 provenance（包括 manifest SHA256）和 `experiment_label`。该 10 题运行的标签固定为 `humanevalplus_10_public_prompt_generation_pilot`，统计范围固定为 `generation_and_parsing_only`：它只报告生成/解析成功、失败和耗时，**不执行候选代码或官方测试，不产生功能正确率、HumanEval+ 分数或 pass@k，也不是正式 benchmark 结果**。`run` / `batch` 仍会拒绝这类公开投影；阶段二必须从已完成的阶段一 run 进入独立的 `tracejudge evalplus`。内置 Mock Provider 没有这 10 题的离线答案 Fixture；真实生成 Pilot 应使用 `--provider hy3`，普通单元测试仍不依赖网络或真实 Hy3。

### HumanEval+ 45 题自然研究子集

在固定 10 题 Pilot 之外，系统支持生成一个正式的 45 题自然研究 cohort。该子集使用同一 164 题公开投影，但排除 10 题 Pilot，并采用固定的研究种子 `20260825`、固定题数 45 和 `selection_role: "research_natural"`，数据集 manifest 为 schema v2。它不替代完整 164 题 HumanEval+，也不是正式 benchmark 排名。

先生成 10 题 Pilot（见上节），再基于它生成 45 题研究子集：

```bash
tracejudge dataset sample \
  --dataset artifacts/datasets/processed/humanevalplus-full/problems.jsonl \
  --manifest artifacts/datasets/processed/humanevalplus-full/dataset_manifest.json \
  --count 45 \
  --seed 20260825 \
  --output-dir artifacts/datasets/processed/humanevalplus-research-natural-45 \
  --exclude-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --selection-role research_natural
```

对应阶段一生成：

```bash
tracejudge baseline \
  --dataset artifacts/datasets/processed/humanevalplus-research-natural-45/problems.jsonl \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --provider hy3 \
  --output-dir artifacts/experiments/phase1
```

### HumanEval+ 固定 10 题阶段二 EvalPlus Pilot

阶段二不调用 Provider、Hy3、LLM Judge 或现有全链路 pipeline，也不在宿主机导入或执行候选代码。它固定使用下面的官方 Linux/amd64 镜像；该 digest 内实测 EvalPlus package 版本为 `0.4.0.dev2`，镜像源码 commit 为 `f11cfb92c1d52896a87f988cbebbd74727d56c7e`，并固定 HumanEval+ release `v0.1.10` 与 Python `3.11.10`：

```text
ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740
```

接口与执行语义以[镜像对应的固定源码 commit](https://github.com/evalplus/evalplus/commit/f11cfb92c1d52896a87f988cbebbd74727d56c7e)、其中的 [`evaluate.py`](https://github.com/evalplus/evalplus/blob/f11cfb92c1d52896a87f988cbebbd74727d56c7e/evalplus/evaluate.py)、[CLI 文档](https://github.com/evalplus/evalplus/blob/f11cfb92c1d52896a87f988cbebbd74727d56c7e/docs/cli.md)和[执行文档](https://github.com/evalplus/evalplus/blob/f11cfb92c1d52896a87f988cbebbd74727d56c7e/docs/execution.md)为准。

执行器固定为每题一个容器，调用该固定 commit 的官方接口时显式设置 `parallel=1`、`min_time_limit=4.0`、`gt_time_limit_factor=4.0` 和 `test_details=true`。该接口没有 `--output-file`；官方原始文件名由 samples 路径生成，当前单题输入对应 `sample_eval_results.json`。固定 10 题也不能直接对 164 题全量数据执行；适配器会在容器内从镜像自带的官方 release 数据构造单题、evaluation-only 的 `HUMANEVAL_OVERRIDE_PATH`，并先核对 10 题的公开 prompt 哈希和 entry point。

镜像不会在运行时自动拉取；首次运行前显式获取固定 digest：

```bash
docker pull --platform linux/amd64 \
  ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740
```

先可以使用不启动 Docker、不执行候选的 Mock dry run 验证输入和产物链路：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1-humanevalplus/phase1_20260824T040336563033Z_e23c1905d438 \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2-mock \
  --executor mock
```

真实执行命令：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1-humanevalplus/phase1_20260824T040336563033Z_e23c1905d438 \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2 \
  --executor docker \
  --parallel 2 \
  --per-task-timeout 180 \
  --batch-timeout 900 \
  --selection-policy all
```

若阶段一并未在全部题目上成功（例如 45 题自然研究子集），可改用 `--selection-policy phase1-success-only --min-success-count 30`，只把成功题目传入 EvalPlus，且要求成功数不少于 30：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1-humanevalplus/<run_id> \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2-research-natural \
  --executor docker \
  --parallel 2 \
  --per-task-timeout 180 \
  --batch-timeout 900 \
  --selection-policy phase1-success-only \
  --min-success-count 30
```

此模式会在阶段二 manifest 和 summary 中同时记录阶段一来源题数、成功导出数、被排除的 terminal `parse_error` 数和 `provider_error` 数。例如来源45题、成功32题、解析错误8题、Provider错误5题时，研究标签使用 `humanevalplus_32_of_45_evalplus_execution_research_natural`，不会写成45题都进入了执行器。筛选策略和最低成功阈值均进入续跑指纹；修改任一项都会拒绝续跑。

中断后可将原命令中的输出参数保持不变，并加上 `--resume-run-id <run_id>`。续跑会重用已完成题目，并拒绝阶段一来源、代码字节、数据 provenance、EvalPlus 固定身份、镜像、参数或隔离配置变化。

产物位于 `<output-dir>/<run_id>/`：`manifest.json`、`samples.jsonl`、`evalplus_raw_results.json`、`results.jsonl`、`summary.json` 和 `execution.log`。执行器只接受仓库内且经 `git check-ignore` 确认已忽略的输出位置；目录权限为 `0700`，文件为 `0600`。原始结果可能包含候选代码和失败测试输入，不得打印、提交或发送给模型。`results.jsonl` 仅保留状态、已观测失败数和哈希。

Docker task 使用只读 control、只读根文件系统、无网络、资源/PID/文件大小限制，并且只将两个预创建的宿主输出文件作为精确 RW bind，不挂载任何宿主可写目录。宿主等待容器完全退出后才读取结果。官方镜像中候选与 wrapper 仍共享 UID，因此 manifest 将安全边界明确标为 `basic_non_adversarial`：这是面向本 Pilot 的基础非对抗隔离，不是对主动恶意候选的完整防篡改证明；高对抗执行应升级到独立 UID 和 VM/microVM。

`--batch-timeout` 是候选任务调度截止，不包含前置镜像/数据 preflight；到期后执行器会并行请求容器清理，并给 worker 固定 5 秒确认尾段。仍未确认退出或 Docker 清理失败的题会记录为 `container_cleanup_failed` 基础设施错误，summary/CLI 因而不会把该 run 当成功实验。阶段一/数据静态输入另有 128 MiB 文件上限，单题候选代码上限为 2 MiB UTF-8 字节。

该固定 EvalPlus commit 的官方 raw 只报告 `pass` / `fail` / `timeout`；`fail` 同时包含错误答案、语法错误、缺失入口和普通候选异常，因此 summary 不伪造可细分的 execution-error 数。Base+Extra 通过只在 Base 和 Plus 状态都为 `pass` 时成立；通过率分母是实际完成官方执行的题数，不把基础设施失败当作代码失败。

### 阶段三 Gate B：冻结 research-natural 自然轨迹

`tracejudge phase3 preflight` 执行与冻结相同的全链路只读校验和内存 manifest 构造，但不创建输出目录或文件；失败时只报告固定安全阶段码。预检通过后，`tracejudge phase3 freeze` 才按预注册的 `all_phase1_successes` 规则原子冻结全部阶段一成功轨迹。两者都不调用 Hy3、Judge 或 Docker，不执行候选代码，也不打开阶段二 `samples.jsonl` / `evalplus_raw_results.json`。

先执行预检，并在安全阶段码或成功核算得到确认后单独决定是否执行冻结：

```bash
tracejudge phase3 preflight \
  --phase1-run artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5 \
  --phase2-run artifacts/experiments/phase2-research-natural/phase2_20260827T081939637435Z_3c366f64fc19 \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --freeze-id phase3_natural_42_v1 \
  --output-dir artifacts/experiments/phase3-freezes
```

只有预检通过后才使用以下发布命令：

```bash
tracejudge phase3 freeze \
  --phase1-run artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5 \
  --phase2-run artifacts/experiments/phase2-research-natural/phase2_20260827T081939637435Z_3c366f64fc19 \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --freeze-id phase3_natural_42_v1 \
  --output-dir artifacts/experiments/phase3-freezes
```

正式输出为 `artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json`，SHA256 为 `a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692`。同名目录存在时拒绝覆盖。manifest 记录 45 条来源的完整状态核算、42 条成功轨迹顺序、阶段一响应行与阶段二安全结果行引用、候选/说明/题面哈希和五种配对方法 ID；不保存 Provider raw、候选代码正文、说明正文、官方测试输入或 EvalPlus raw。该 manifest 只冻结自然轨迹，不表示五种方法已经运行，也不构成阶段三实验结论。

### 阶段三 Gate B：公开反事实证据与 overlay

公开反事实源固定为 `data/phase3/public_counterfactuals_v1.json`（SHA256 `a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df`）：3 个父 Fixture，五类各 3 条，共 15 条。父 Fixture 只用于派生和同代码证据，不进入研究分母。正式操作必须逐门槛进行，先只读预检：

```bash
tracejudge phase3 counterfactual-preflight \
  --execution-run-id phase3_cf_public_15_v1 \
  --source-bundle data/phase3/public_counterfactuals_v1.json \
  --output-dir artifacts/experiments/phase3-public-evidence
```

该命令不执行代码、不创建产物。确认预检显示 3 个父 Fixture、15 条反事实、15 个执行主体、预期 `6 / 9` 后，才可显式运行 `counterfactual-execute`。执行器不调用 Provider/Docker/Judge，只运行精确 SHA 白名单化的公开自建代码；它使用同机子进程和父进程超时，不是面向恶意代码的安全沙盒。执行结束会保留全部实际 pass/fail/timeout/基础设施错误与预期偏差，不自动重试。

证据无超时、基础设施错误或预期偏差后，先运行 `counterfactual-freeze-preflight` 完整核对自然 manifest、源 bundle、执行 manifest/results 和每条证据绑定，再由 `counterfactual-freeze` 原子发布 overlay。overlay 不改写自然 manifest；正式 42 条自然 + 15 条反事实将形成 57 条固定五方法配对顺序。任何同名目录、哈希变化、case/代码/说明绑定变化都会拒绝冻结。

正式 Gate B 已得到 57 条 overlay：`artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json`，SHA256 为 `3290221625d687e6d7412a0544247dc81a34857b114a545458b93cc04e35d255`。该产物仍只是研究输入，不是五方法结果。

### 阶段三 Gate C：五方法统一接口

`phase3/runner.py` 将自然 manifest 和 overlay 的精确字节哈希、57 条顺序、轨迹正文哈希及功能证据绑定后，以 trace-major 顺序生成完整 57 × 5 = 285 对。Test-only 仅看脱敏功能证据；其他四种方法分别按冻结白名单加入题面/说明/代码、AST 和公开动态证据。四个 Judge Prompt 独立版本化，共用 `MethodJudgment` 严格 schema；只允许“首次严格 JSON/Schema 失败→一次脱敏修复”，不会对 Provider 错误自动重试。

只读预检命令为：

```bash
tracejudge phase3 paired-preflight \
  --cohort-manifest artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json \
  --natural-manifest artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json \
  --provider mock \
  --model deterministic-phase3-mock-v1 \
  --temperature 0 \
  --timeout-seconds 120
```

该命令只读取两份白名单 manifest，核算方法规格、Prompt bundle 和输出 schema 哈希；不读取候选/说明正文，不创建产物，不运行五方法，也不连接 Provider、Docker 或网络。正式 Hy3 参数与执行由 Gate E3 的独立预检和显式授权入口处理。

正式只读验收已通过：自然 42 + 反事实 15 = 57 条、五方法、完整笛卡尔积 285 对；方法规格 SHA256 为 `4b8684852125ad3059b5001951479a2f164c7089eb64ff10cbdafafc39c534ff`，Prompt bundle SHA256 为 `c8d6c2c0f6bb1207af987746d912868bd102f90b334f5425528cbda5be9dd366`，输出 schema SHA256 为 `96da92777ee89bb69a65c61f4bdc9fc9e7cb7ac1ba94a52400f79ca1130821f3`。其中方法规格哈希绑定的是 `mock / deterministic-phase3-mock-v1 / temperature=0 / timeout=120s` 的 Gate C 接口身份；它不是正式 Hy3 运行规格或研究结果。

### 阶段三 Gate D：公开反例与错误证书

`phase3/public_evidence.py` 固定“公开 challenge → 最多 32 个确定性探针 → 最多 16 次列表最小化”的公开策略；只允许从 SHA256 精确白名单源恢复公开自建 Fixture。`phase3/replay.py` 不信任证书中的代码，也不接受 HumanEval+ 或外部候选；它重新绑定自然 manifest、overlay、公开源、代码/说明/功能证据哈希后，只执行一个公开反例，并要求重放得到与证书相同的执行证据哈希。

公开工程 claim bundle `data/phase3/public_certificate_claims_v1.json` 的 SHA256 为 `3b1df5e5a1e43c1b91e626c8656495a03d332bd4a5231550eb88c8928b93bb5f`，分别覆盖一个 `confirmed_bug`、一个 `strongly_supported` 和一个 `unverified_suspicion`。它只验证三等级降级语义和 replay 链路，不是五方法预测或人工金标。正式证书 manifest SHA256 为 `4d4d2f8ce5ee86d96aaeffbec2f2d686a395427a340703534b8460a866f144e8`，三证书 payloads SHA256 为 `19661ef014cd79d423a314adc52664fadc3b4c65959f99f8ef79854f0525af53`；confirmed 证书的独立 replay 成功复现一个公开失败，执行证据 SHA256 `cfd897334643853fc10901835a5203aa51ee7edd4442e314893c1e5bc152e670` 与证书一致。未调用 Provider、Docker 或网络；Gate D 已退出。

### 阶段三 Gate E1：冻结协议与盲法标注包

Gate E1 将标注指南与机器可校验协议分开冻结：`docs/experiments/phase3_annotation_guide_v1.md` SHA256 为 `0c789671fc926e8286ca7317eae0496efc9f39616783b2c8cbebd678de20beb1`，`data/phase3/annotation_protocol_v1.json` SHA256 为 `a2d77ae20102364170a6391c544437601c6e5871e86b9a01f64ad9492556ea85`。协议固定正类、失败分母、`unverified_suspicion` 处理、两项主比较、exact McNemar、父题聚类 bootstrap（10,000 次、seed `20260828`）和 Holm 口径。

`annotation-packet-preflight` 只读重建并哈希绑定 57 条白名单材料，不写文件、不执行候选，也不调用 Provider、Docker 或网络。`annotation-packet-export` 在同一身份通过后，把固定随机顺序的 opaque item、协调者 identity map 和未填写标签模板原子写入 Git-ignored 私有目录，权限为 `0700/0600`。packet 不包含五方法预测、其他标注者标签、反事实修改/预期影响/预期状态或官方隐藏输入；identity map 不应交给独立标注者。生成模板本身不等于取得人工标签；本轮后续已完成 Gate E2 冻结标签、E3 真实五方法运行和 E4 配对统计。

正式导出已固定 42 + 15 = 57 条；manifest / packet / identity map / 标签模板 SHA256 分别为 `b9897cb33631f21d6762fabadbafb84bf3ec8dfbafd9e026debf907be4851ee1` / `a8d2c328bc6d041d013f452edc28b4a552eae35f19d44139b99fa6855faf801d` / `c28cde4fc4b20c9b568b0e55d905218e9247b747797f6a9635b81ca157c74ec1` / `9700028c4f57f9e1f0674b37268d1c9f98a7316f27505fb522024912f5816db1`。

Gate E2 的 `annotation-labels-check` 只读 packet manifest、空模板和 working 标签，报告完成、待标注、无效、缺失、额外和顺序偏差，不打开 identity map。只有全部 57 条严格完成时，`annotation-labels-freeze-preflight` 才只读回连冻结 cohort；`annotation-labels-freeze` 再把规范化 opaque 标签和 trace-major 标注记录写入新的 Git-ignored `0700/0600` 目录。空模板、working 副本或进度检查都不是正式人工标签。

正式主标注集 `phase3_labels_primary_round1_v1` 已冻结 42 + 15 = 57 条；manifest / completed labels / annotation records SHA256 分别为 `fbf89aa950318392e49d01a5235461c4ce6ae94acb55842b963bb54048eac0a3` / `17b4e1b43fd2161aff7a0b3d63a7f5f31a89992db9fe75823697d2ac4c32d98d` / `ffbee2c546a6e0f560a96c8c610661258216c9ef627af8ffd3e6ff60ca1e8299`。它是单标注者、单轮次的私有标签集，`agreement_kind=not_computed`，不声称标注者间一致性。

Gate E3 的 `phase3 evaluate-preflight` 只读重建并哈希绑定 57 条方法材料、冻结人工标签、五方法规格、Hy3 公开配置与续跑身份；它不创建目录、不执行候选、不连接 Provider/Docker/网络。`phase3 evaluate` 必须显式传入 `--confirm-real-provider`，对 57 条轨迹产生 285 个 trace-major 配对；Test-only 不调 Provider，其余 228 对每对最多一次修复，最大 456 次 Provider 调用。正式运行 `phase3_hy3_57x5_v1` 已完成：完整配对 285、`valid_judgment=283`、`provider_error=2`、无恢复复用；失败继续保留在统计分母中。

Gate E4 的 `phase3 statistics-preflight` 严格绑定 cohort、私有人工标签 manifest/两个 payload、已完成 E3 run manifest/results/index、逐行哈希和 trace-major 顺序，在内存计算但不显示标签分布或方法结果，也不写文件。`phase3 statistics` 原子写入只含聚合数量的 `report.json` 与 `manifest.json`：主错误检测把无效结果计为错误并单独报告，自然轨迹对两个预注册基线使用双侧精确 McNemar 与 Holm 校正，反事实按父问题聚类进行固定 10,000 次 percentile bootstrap。正式 `phase3_stats_primary_round1_v1` 已发布，输出不含逐轨迹标签、标注理由、方法预测或 Provider raw。

Gate F 的 `phase3 report-preflight` 只读绑定 E4 manifest/report、E3 结构化运行账本与 Gate D 公开 confirmed 证书，在内存生成结果解读但不展示方法成绩。`phase3 report` 会原子写入脱敏 Markdown、`validation.json`、公开证书副本与重放命令；强制 11/11 统计谬误扫描，将 Test-only 的过程字段写为 N/A，对反事实只用父题 cluster bootstrap 做推断，并明确禁止把不显著解释为等效。正式 `phase3_report_primary_round1_v1` 已发布，验收确认 11/11 扫描、`0700/0600` 权限、五个不可覆盖文件和脱敏声明一致。阶段四 Gate C 已将该 Markdown 逐字节复制到受 Git 跟踪的发布目录；阶段四 receipt 只补强公开证书 replay 证据，不改变 `ANALYZED / CAUTION / CANNOT_VERIFY`。

## 7. 配置真实 Hy3

复制 `.env.example` 为 `.env` 并填入真实值：

```bash
cp .env.example .env
```

```env
HY3_BASE_URL=<你的 OpenAI-compatible Hy3 服务地址>
HY3_API_KEY=<你的 API Key>
HY3_MODEL=<模型名称>
HY3_REASONING_EFFORT=high
HY3_TIMEOUT_SECONDS=120
HY3_MAX_RETRIES=2
HY3_MAX_PARSE_REPAIRS=1
HY3_ENABLE_REASONING_EFFORT=true
```

```bash
tracejudge baseline --dataset data/sample_problems.jsonl --provider hy3 \
  --output-dir artifacts/experiments/phase1

# 现有的完整评估链路（会执行代码）
tracejudge run --dataset data/sample_problems.jsonl --problem-id safe_mean \
  --provider hy3 --sandbox docker
```

真实 Hy3 调用仅作为**可选模式**，使用 `HY3_TIMEOUT_SECONDS` 和 `HY3_MAX_RETRIES` 限定通用/阶段一入口的单次超时与总尝试次数，不会无限重试。Gate E3 为保留精确失败分母，额外将 Provider 交通重试固定为 0，仅严格 JSON 解析失败可进行一次结构化修复。真实 Hy3 不是 Mock dry run 或普通单元测试的前置条件；普通测试使用 Mock/替身，不访问真实 Hy3 API。真实 pilot 是否完成应以当次实验报告和实际产物为准。

## 8. 环境变量说明

| 变量 | 说明 |
|---|---|
| `HY3_BASE_URL` / `HY3_API_KEY` / `HY3_MODEL` | Hy3 OpenAI-compatible 服务地址、密钥、模型名称；三者均未设置时 `--provider hy3` 不可用，但不影响 `--provider mock` |
| `HY3_REASONING_EFFORT` | 通过 `extra_body.reasoning_effort` 传递给服务端（若 `HY3_ENABLE_REASONING_EFFORT=true`） |
| `HY3_TIMEOUT_SECONDS` / `HY3_MAX_RETRIES` | 单次调用超时与失败重试次数 |
| `HY3_MAX_PARSE_REPAIRS` | 解析失败后可追加修复 Prompt 的最大次数（硬上限，与普通 Provider 重试分开计数） |
| `HY3_ENABLE_REASONING_EFFORT` | 关闭后不发送 `reasoning_effort` 扩展参数，兼容不支持该参数的服务 |
| `TRACEJUDGE_SANDBOX` | 默认沙盒后端：`docker` 或 `trusted-local` |
| `TRACEJUDGE_DOCKER_IMAGE` | Docker 沙盒使用的镜像 |
| `TRACEJUDGE_TEST_TIMEOUT_SECONDS` | 单个测试用例超时时间 |
| `TRACEJUDGE_MEMORY_LIMIT` / `TRACEJUDGE_CPU_LIMIT` | Docker 容器资源限制 |
| `TRACEJUDGE_ARTIFACT_DIR` | 结果 JSON 输出目录 |

`.env` 已加入 `.gitignore`，仓库只提交 `.env.example`。

## 9. Docker 与本地执行的安全说明

详见 [`docs/safety.md`](docs/safety.md)。摘要：

- `DockerSandbox` 是**真实模型生成代码的默认执行方式**：禁止网络（`--network none`）、只读根文件系统与只读沙盒挂载、限制 CPU/内存/进程数、丢弃 Linux capabilities 并设置 `no-new-privileges`。正常路径使用 `--rm`，超时或异常路径还会按唯一容器名执行 `docker rm -f` 强制清理。**这仍只是"基础隔离"，不是绝对安全保证。**
- `TrustedLocalSandbox` 只做同机进程级隔离：父进程为每个用例新建子进程、强制超时并终止其进程组，但**没有网络隔离、资源配额或权限降级**。CLI/流水线默认只放行精确匹配的仓库 Mock Fixture；真实模型输出、外部数据集代码和 Mock fallback 都需显式 `--allow-unsafe-local-exec`。
- `tracejudge doctor` 会明确报告 Docker 是否可用；Docker 不可用时，真实模型模式默认无法运行，Mock Demo 仍可通过 `TrustedLocalSandbox` 正常运行。

## 10. 数据格式说明

详见 [`docs/data_format.md`](docs/data_format.md)。核心结构：

- `data/sample_problems.jsonl`：每行一个 `ProblemSpec`（JSON），字段包括 `requirement`、`function_signature`、`requirements`（需求条款）、`visible_test_cases` / `hidden_test_cases` / `challenge_test_cases`（结构化 `args`/`kwargs`/`expected`，不使用可执行的表达式字符串）、`reference_code`、`difficulty`、`source`、`tags`。
- `TestCase.expected` 支持一种特殊约定：`{"raises": "ValueError"}` 表示该用例期望抛出指定异常（用于 `clamp` 的非法区间测试），其余情况下 `expected` 就是普通 JSON 值。
- `data/mock_responses/*.json`：`SolutionTrace` 格式的确定性 Mock 解答。
- `data/demo_annotations.jsonl`：为内置 Mock Fixture 提供的少量人工标注 Ground Truth，**仅用于测试指标函数，不是正式人工标注研究**。
- `data/manifests/evalplus_humanevalplus_d32357cf.json`：固定 HumanEval+ revision、许可证和原始快照哈希的受控来源 manifest；不含答案或测试正文。
- `artifacts/datasets/`：本地 HumanEval+ 原始快照、公开投影和固定 10 题 Pilot bundle；整个 `artifacts/` 目录均被 Git 忽略。
- `artifacts/experiments/phase1/<run_id>/`：阶段一基线生成的 `manifest.json` / `responses.jsonl` / `summary.json`；详见 [`docs/data_format.md`](docs/data_format.md)。
- `artifacts/experiments/phase2/<run_id>/`：阶段二受限 samples/官方 raw 与脱敏逐题结果、summary、manifest 和有界日志；详见 [`docs/data_format.md`](docs/data_format.md)。

## 11. 测试

```bash
pytest -q
ruff check .
ruff format --check .
```

测试不调用真实 Hy3 API；阶段一测试会验证 Prompt 公开信息白名单、Mock 无网络、原子 JSONL、单题失败隔离、续跑及非敏感 manifest。HumanEval+ 阶段二单元测试使用替身执行器验证静态一致性、samples 最小导出、Docker 强化参数、主机不执行、脱敏、原子 checkpoint、summary 和续跑身份，普通 suite 不需要 Docker、网络、Hy3 或 OpenRouter。真实容器 integration 测试使用 `docker` marker 单独运行。

```bash
TRACEJUDGE_RUN_DOCKER_INTEGRATION=1 pytest -q -m docker tests/test_evalplus_docker_runner.py
```

## 12. 输出 JSON 示例

`tracejudge demo --mock --case faulty` 保存到 `artifacts/demo_faulty_<timestamp>.json` 的结构（节选）：

```json
{
  "problem": { "problem_id": "safe_mean", "...": "..." },
  "solution": { "code": "def safe_mean(nums: list[float]) -> float:\n    return sum(nums) / len(nums)\n", "...": "..." },
  "static_evidence": { "has_empty_input_check": false, "if_count": 0, "...": "..." },
  "execution_result": { "runtime_status": "completed", "results": [ "..." ] },
  "process_assessment": {
    "functional_correct": false,
    "process_correct": false,
    "first_faulty_layer": "alignment",
    "first_faulty_step": "S1",
    "violated_requirement": "R1",
    "error_type": "A01_PLAN_CODE_MISMATCH",
    "explanation": "步骤 S1（“检查 nums 是否为空列表...”）声称处理空输入，但 AST 静态分析未发现空输入判断分支。"
  },
  "counterexample": {
    "args": [[]],
    "expected": 0.0,
    "candidate_exception": "ZeroDivisionError",
    "source": "challenge_test"
  },
  "error_certificate": {
    "verdict": "confirmed_bug",
    "error_type": "A01_PLAN_CODE_MISMATCH",
    "violated_requirement": "R1",
    "first_faulty_step": "S1"
  }
}
```

## 13. 项目目录结构

```text
tracejudge-hy3/
├── README.md
├── pyproject.toml
├── .env.example
├── Makefile
├── IMPLEMENTATION_STATUS.md
├── src/tracejudge_hy3/
│   ├── cli.py                 # Typer CLI: dataset / baseline / run / batch 等
│   ├── baseline/              # 阶段一生成、断点续跑与产物持久化
│   ├── config.py               # 环境变量配置 (pydantic-settings)
│   ├── schemas/                 # 所有 Pydantic v2 数据模型
│   ├── providers/               # LLMProvider 抽象接口、Mock、Hy3 OpenAI-compatible
│   ├── prompts/                 # Solver / Evaluator / 阶段三五方法 Prompt
│   ├── parsing/                  # 结构化输出解析（含围栏/多余文本容错）
│   ├── dataset/                  # JSONL 加载 + HumanEval+ 阶段一公开投影
│   ├── evalplus/                 # 阶段二官方 EvalPlus 导出/隔离执行/脱敏
│   ├── phase3/                   # 阶段三契约、Gate B 冻结与 Gate C 配对接口
│   ├── static_analysis/          # AST 静态分析
│   ├── sandbox/                  # Docker / TrustedLocal 沙盒 + 测试运行器
│   ├── evaluator/                # 规则证据 + LLM 判断 + 组合 + 错误证书
│   ├── counterexample/           # 反例生成 / 差分执行 / 最小化
│   ├── pipeline/                 # 端到端流水线编排
│   └── reporting/                # 指标函数 + 结果序列化
├── data/
│   ├── sample_problems.jsonl     # 3 道内置示例题
│   ├── demo_annotations.jsonl    # Mock Fixture 的人工标注 Fixture
│   ├── manifests/                # 公开数据固定 revision/哈希来源清单
│   ├── mock_responses/           # 确定性 Mock 解答
│   └── phase3/                    # 公开自建反事实源 bundle（不含官方隐藏评测内容）
├── tests/
├── docs/
│   ├── architecture.md
│   ├── data_format.md
│   ├── safety.md
│   └── mvp_scope.md
└── artifacts/                    # 运行结果 (git 忽略，只保留 .gitkeep)
```

## 14. 后续路线（v0.2+ 优先级参考）

1. 将已落地的 HumanEval+ 10 题工程 Pilot 扩展为完整 164 题、多次复现的正式评测，接入 MBPP+，并构造研究级人工标注子集；
2. 在已完成的公开反例/证书 Gate D 基础上扩展更多独立公开 Fixture；
3. 按新的预注册身份执行第二标注者或跨时间复标，补充一致性统计、反事实父题 cluster、消融和 Provider 失败敏感性分析；
4. 扩展反例生成为更通用的属性测试与更完整的 delta-debugging；
5. 在阶段四静态聚合 SVG 基础上扩展跨运行可视化看板和批量评测报告。

## 15. 许可证与数据来源

- 代码许可证见 [`LICENSE`](LICENSE)（MIT）。
- `data/sample_problems.jsonl`、`data/mock_responses/`、`data/demo_annotations.jsonl` 均为本项目自建的工程验证 Fixture（`source: "self_constructed_mvp_fixture"`）；`data/phase3/public_counterfactuals_v1.json` 是 MIT 许可的公开自建阶段三反事实源。它们均不包含 HumanEval / MBPP 等公开题目正文或官方隐藏评测内容。
- 仓库只提交 HumanEval+ 来源 manifest 和公开投影适配器；原始快照、生成出的 164 题投影、10 题 Pilot 与实验结果均位于被 Git 忽略的 `artifacts/`。HumanEval+ 来源及许可证记录见 [`data/manifests/evalplus_humanevalplus_d32357cf.json`](data/manifests/evalplus_humanevalplus_d32357cf.json)。
- 本项目不训练或微调模型；模型能力调用通过用户自行配置的 Hy3（OpenAI-compatible）服务完成，仓库不包含任何真实密钥。
