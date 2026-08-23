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
- 基于 `ast` 的静态分析：`if` / `for` / `while` 分类计数、输入相关循环、最大嵌套深度、比较运算符、数据结构、函数调用、返回行号、空输入与可疑硬编码启发式（[`src/tracejudge_hy3/static_analysis/ast_analyzer.py`](src/tracejudge_hy3/static_analysis/ast_analyzer.py)）。
- 沙盒执行：`DockerSandbox`（默认，用于真实模型代码，仅提供基础隔离）与 `TrustedLocalSandbox`（默认仅允许仓库内置且精确匹配的 Mock Fixture；其他代码需显式 `--allow-unsafe-local-exec`）（[`src/tracejudge_hy3/sandbox/`](src/tracejudge_hy3/sandbox/)）。
- 测试运行器：位置/关键字参数从 JSON 加载，不使用 `eval()`；父进程为每个用例创建新子进程并强制超时，有界捕获 stdout/stderr，独立记录输出、异常、超时和退出码（[`src/tracejudge_hy3/sandbox/test_runner.py`](src/tracejudge_hy3/sandbox/test_runner.py)）。
- 规则证据 + LLM 判断的四层评估：空输入声明—代码不一致、集合声明—代码不一致、单次遍历声明—嵌套循环不一致、复杂度声明不一致、执行失败归因（[`src/tracejudge_hy3/evaluator/`](src/tracejudge_hy3/evaluator/)）。
- 反例生成：优先复用与当前违反需求条款相关的 challenge/hidden 测试失败结果，其次基于相关测试的参数形状生成有限边界候选并与参考实现差分执行，并对列表参数做简单 delta-debugging 最小化（[`src/tracejudge_hy3/counterexample/`](src/tracejudge_hy3/counterexample/)）。
- 可执行错误证书聚合：新疑似问题直接产生 `confirmed_bug` / `strongly_supported` / `unverified_suspicion` 三种裁决。普通首次运行正确时不产生证书；`cleared` 仅用于显式传入既有证书后，复核的完整执行证据表明原疑似问题不再成立的状态转移（[`src/tracejudge_hy3/evaluator/evidence.py`](src/tracejudge_hy3/evaluator/evidence.py)）。
- CLI（Typer + Rich）：`doctor` / `demo` / `run` / `batch`（[`src/tracejudge_hy3/cli.py`](src/tracejudge_hy3/cli.py)）。
- 3 道内置示例题（`safe_mean` / `deduplicate_preserve_order` / `clamp`），来源标记为 `self_constructed_mvp_fixture`（见 §12）。
- 指标计算：10 个纯函数指标，缺少人工标注时返回 `not_computable` 而不是伪造数值（[`src/tracejudge_hy3/reporting/metrics.py`](src/tracejudge_hy3/reporting/metrics.py)）。
- 单元测试与集成测试（`pytest`），Lint（`ruff`）。

## 4. v0.1 尚未实现的功能

见 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) 的完整分类，摘要如下：

- HumanEval / MBPP 等公开 benchmark 的自动下载与大规模评测；
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

## 6. 运行 Mock Demo（无需真实 API Key）

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
tracejudge run --dataset data/sample_problems.jsonl --problem-id safe_mean --provider hy3 --sandbox docker
```

真实 Hy3 调用仅作为**可选模式**，从不是本地 Demo 或单元测试的前置条件——单元测试不会调用真实 API。

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

## 11. 测试

```bash
pytest -q
ruff check .
ruff format --check .
```

测试不调用真实 Hy3 API；Docker 相关单元测试通过替身验证可用性探测、强化参数和超时后的强制清理，不要求本机 Docker 一定可用。

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
│   ├── cli.py                 # Typer CLI: doctor / demo / run / batch
│   ├── config.py               # 环境变量配置 (pydantic-settings)
│   ├── schemas/                 # 所有 Pydantic v2 数据模型
│   ├── providers/               # LLMProvider 抽象接口、Mock、Hy3 OpenAI-compatible
│   ├── prompts/                 # Solver / Evaluator Prompt 构造
│   ├── parsing/                  # 结构化输出解析（含围栏/多余文本容错）
│   ├── dataset/                  # JSONL 数据集加载
│   ├── static_analysis/          # AST 静态分析
│   ├── sandbox/                  # Docker / TrustedLocal 沙盒 + 测试运行器
│   ├── evaluator/                # 规则证据 + LLM 判断 + 组合 + 错误证书
│   ├── counterexample/           # 反例生成 / 差分执行 / 最小化
│   ├── pipeline/                 # 端到端流水线编排
│   └── reporting/                # 指标函数 + 结果序列化
├── data/
│   ├── sample_problems.jsonl     # 3 道内置示例题
│   ├── demo_annotations.jsonl    # Mock Fixture 的人工标注 Fixture
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

1. 接入 HumanEval+ / MBPP+ 的子集并做人工标注，替换/补充自建 Fixture；
2. 构建真实的反事实配对样本（reasoning 反事实、code 反事实、shortcut 反事实、equivalent 反事实、boundary 反事实）；
3. 引入模块级消融实验（Test-only / Direct Judge / +四层对齐 / +静态分析 / 完整方法）；
4. 扩展反例生成为更通用的属性测试与更完整的 delta-debugging；
5. 简单结果可视化（图表）与批量评测报告。

## 15. 许可证与数据来源

- 代码许可证见 [`LICENSE`](LICENSE)（MIT）。
- `data/sample_problems.jsonl`、`data/mock_responses/`、`data/demo_annotations.jsonl` 均为本项目自建的工程验证 Fixture（`source: "self_constructed_mvp_fixture"`），不包含 HumanEval / MBPP 等公开数据集内容。
- 本项目不训练或微调模型；模型能力调用通过用户自行配置的 Hy3（OpenAI-compatible）服务完成，仓库不包含任何真实密钥。
