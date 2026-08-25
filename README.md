# TraceJudge-Hy3 (v0.1 MVP)

**基于“需求—推理—代码—执行证据”四层对齐，评估 AI 生成 Python 函数代码是否真正正确，并定位首个错误步骤、错误类型和代码位置。**

> 本项目为犀牛鸟开源活动个人参赛作品，与腾讯官方发布无关。项目不隶属于、不代表任何官方组织，命名和内容均不构成官方发布。

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
- 可选的 Hy3 OpenAI-compatible Provider：环境变量配置、超时与可配置的有限重试、JSON Schema/上下文引用校验后的修复重试、耗时记录、日志中不暴露密钥片段（[`src/tracejudge_hy3/providers/hy3_openai.py`](src/tracejudge_hy3/providers/hy3_openai.py)）。
- 阶段一基线生成器：为每次运行创建唯一 `run_id`，逐题原子持久化原始输出与解析后 `SolutionTrace`，支持断点续跑、单题失败隔离和非敏感实验元数据（[`src/tracejudge_hy3/baseline/`](src/tracejudge_hy3/baseline/)）。
- 基于 `ast` 的静态分析：`if` / `for` / `while` 分类计数、输入相关循环、最大嵌套深度、比较运算符、数据结构、函数调用、返回行号、空输入与可疑硬编码启发式（[`src/tracejudge_hy3/static_analysis/ast_analyzer.py`](src/tracejudge_hy3/static_analysis/ast_analyzer.py)）。
- 沙盒执行：`DockerSandbox`（默认，用于真实模型代码，仅提供基础隔离）与 `TrustedLocalSandbox`（默认仅允许仓库内置且精确匹配的 Mock Fixture；其他代码需显式 `--allow-unsafe-local-exec`）（[`src/tracejudge_hy3/sandbox/`](src/tracejudge_hy3/sandbox/)）。
- 测试运行器：位置/关键字参数从 JSON 加载，不使用 `eval()`；父进程为每个用例创建新子进程并强制超时，有界捕获 stdout/stderr，独立记录输出、异常、超时和退出码（[`src/tracejudge_hy3/sandbox/test_runner.py`](src/tracejudge_hy3/sandbox/test_runner.py)）。
- 规则证据 + LLM 判断的四层评估：空输入声明—代码不一致、集合声明—代码不一致、单次遍历声明—嵌套循环不一致、复杂度声明不一致、执行失败归因（[`src/tracejudge_hy3/evaluator/`](src/tracejudge_hy3/evaluator/)）。
- 反例生成：优先复用与当前违反需求条款相关的 challenge/hidden 测试失败结果，其次基于相关测试的参数形状生成有限边界候选并与参考实现差分执行，并对列表参数做简单 delta-debugging 最小化（[`src/tracejudge_hy3/counterexample/`](src/tracejudge_hy3/counterexample/)）。
- 可执行错误证书聚合：新疑似问题直接产生 `confirmed_bug` / `strongly_supported` / `unverified_suspicion` 三种裁决。普通首次运行正确时不产生证书；`cleared` 仅用于显式传入既有证书后，复核的完整执行证据表明原疑似问题不再成立的状态转移（[`src/tracejudge_hy3/evaluator/evidence.py`](src/tracejudge_hy3/evaluator/evidence.py)）。
- CLI（Typer + Rich）：`doctor` / `demo` / `dataset convert-humanevalplus` / `dataset sample` / `dataset validate` / `baseline` / `evalplus` / `run` / `batch`（[`src/tracejudge_hy3/cli.py`](src/tracejudge_hy3/cli.py)）。
- HumanEval+ 阶段一公开投影适配器：校验本地固定 revision 快照及其受控来源 manifest，把 164 道公开题面转换为不含答案/测试的 `ProblemSpec`，并仅依据公开 `problem_id` 生成固定种子 10 题 Pilot（[`src/tracejudge_hy3/dataset/humanevalplus.py`](src/tracejudge_hy3/dataset/humanevalplus.py)）。
- HumanEval+ 阶段二官方执行适配器：严格验证阶段一产物后只导出 `solution_trace.code`，在固定 digest 的官方镜像（EvalPlus package `0.4.0.dev2`、源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`）中逐题运行 Base 和 Extra 测试，并生成脱敏的 10 题单样本工程 Pilot 结果（[`src/tracejudge_hy3/evalplus/`](src/tracejudge_hy3/evalplus/)）。
- 3 道内置示例题（`safe_mean` / `deduplicate_preserve_order` / `clamp`），来源标记为 `self_constructed_mvp_fixture`（见 §10 和 §15）。
- 指标计算：10 个纯函数指标，缺少人工标注时返回 `not_computable` 而不是伪造数值（[`src/tracejudge_hy3/reporting/metrics.py`](src/tracejudge_hy3/reporting/metrics.py)）。
- 单元测试与集成测试（`pytest`），Lint（`ruff`）。

## 4. v0.1 尚未实现的功能

见 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 的完整分类，摘要如下：

- HumanEval+ 数据的项目内自动下载、完整 164 题正式评测，以及 MBPP+ 接入与大规模评测；
- 反事实配对挑战集、人工标注集、消融实验、对照实验；
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
  --batch-timeout 900
```

中断后可将原命令中的输出参数保持不变，并加上 `--resume-run-id <run_id>`。续跑会重用已完成题目，并拒绝阶段一来源、代码字节、数据 provenance、EvalPlus 固定身份、镜像、参数或隔离配置变化。

产物位于 `<output-dir>/<run_id>/`：`manifest.json`、`samples.jsonl`、`evalplus_raw_results.json`、`results.jsonl`、`summary.json` 和 `execution.log`。执行器只接受仓库内且经 `git check-ignore` 确认已忽略的输出位置；目录权限为 `0700`，文件为 `0600`。原始结果可能包含候选代码和失败测试输入，不得打印、提交或发送给模型。`results.jsonl` 仅保留状态、已观测失败数和哈希。

Docker task 使用只读 control、只读根文件系统、无网络、资源/PID/文件大小限制，并且只将两个预创建的宿主输出文件作为精确 RW bind，不挂载任何宿主可写目录。宿主等待容器完全退出后才读取结果。官方镜像中候选与 wrapper 仍共享 UID，因此 manifest 将安全边界明确标为 `basic_non_adversarial`：这是面向本 Pilot 的基础非对抗隔离，不是对主动恶意候选的完整防篡改证明；高对抗执行应升级到独立 UID 和 VM/microVM。

`--batch-timeout` 是候选任务调度截止，不包含前置镜像/数据 preflight；到期后执行器会并行请求容器清理，并给 worker 固定 5 秒确认尾段。仍未确认退出或 Docker 清理失败的题会记录为 `container_cleanup_failed` 基础设施错误，summary/CLI 因而不会把该 run 当成功实验。阶段一/数据静态输入另有 128 MiB 文件上限，单题候选代码上限为 2 MiB UTF-8 字节。

该固定 EvalPlus commit 的官方 raw 只报告 `pass` / `fail` / `timeout`；`fail` 同时包含错误答案、语法错误、缺失入口和普通候选异常，因此 summary 不伪造可细分的 execution-error 数。Base+Extra 通过只在 Base 和 Plus 状态都为 `pass` 时成立；通过率分母是实际完成官方执行的题数，不把基础设施失败当作代码失败。

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
HY3_ENABLE_REASONING_EFFORT=true
```

```bash
tracejudge baseline --dataset data/sample_problems.jsonl --provider hy3 \
  --output-dir artifacts/experiments/phase1

# 现有的完整评估链路（会执行代码）
tracejudge run --dataset data/sample_problems.jsonl --problem-id safe_mean \
  --provider hy3 --sandbox docker
```

真实 Hy3 调用仅作为**可选模式**，使用 `HY3_TIMEOUT_SECONDS` 和 `HY3_MAX_RETRIES` 限定单次超时与总尝试次数，不会无限重试。它不是 Mock dry run 或普通单元测试的前置条件；普通测试使用 Mock/替身，不访问真实 Hy3 API。真实 pilot 是否完成应以当次实验报告和实际产物为准。

## 8. 环境变量说明

| 变量 | 说明 |
|---|---|
| `HY3_BASE_URL` / `HY3_API_KEY` / `HY3_MODEL` | Hy3 OpenAI-compatible 服务地址、密钥、模型名称；三者均未设置时 `--provider hy3` 不可用，但不影响 `--provider mock` |
| `HY3_REASONING_EFFORT` | 通过 `extra_body.reasoning_effort` 传递给服务端（若 `HY3_ENABLE_REASONING_EFFORT=true`） |
| `HY3_TIMEOUT_SECONDS` / `HY3_MAX_RETRIES` | 单次调用超时与失败重试次数 |
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
│   ├── prompts/                 # Solver / Evaluator Prompt 构造
│   ├── parsing/                  # 结构化输出解析（含围栏/多余文本容错）
│   ├── dataset/                  # JSONL 加载 + HumanEval+ 阶段一公开投影
│   ├── evalplus/                 # 阶段二官方 EvalPlus 导出/隔离执行/脱敏
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
│   └── mock_responses/           # 确定性 Mock 解答
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
2. 构建真实的反事实配对样本（reasoning 反事实、code 反事实、shortcut 反事实、equivalent 反事实、boundary 反事实）；
3. 引入模块级消融实验（Test-only / Direct Judge / +四层对齐 / +静态分析 / 完整方法）；
4. 扩展反例生成为更通用的属性测试与更完整的 delta-debugging；
5. 简单结果可视化（图表）与批量评测报告。

## 15. 许可证与数据来源

- 代码许可证见 [`LICENSE`](LICENSE)（MIT）。
- `data/sample_problems.jsonl`、`data/mock_responses/`、`data/demo_annotations.jsonl` 均为本项目自建的工程验证 Fixture（`source: "self_constructed_mvp_fixture"`），不包含 HumanEval / MBPP 等公开题目正文。
- 仓库只提交 HumanEval+ 来源 manifest 和公开投影适配器；原始快照、生成出的 164 题投影、10 题 Pilot 与实验结果均位于被 Git 忽略的 `artifacts/`。HumanEval+ 来源及许可证记录见 [`data/manifests/evalplus_humanevalplus_d32357cf.json`](data/manifests/evalplus_humanevalplus_d32357cf.json)。
- 本项目不训练或微调模型；模型能力调用通过用户自行配置的 Hy3（OpenAI-compatible）服务完成，仓库不包含任何真实密钥。
