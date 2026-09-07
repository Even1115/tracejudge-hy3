# 运行、配置与开发指南

本文承接首页省略的安装、CLI、实验复现、配置和开发细节。项目当前成果见 [README](../README.md)；四数据集的运行状态、固定分母和失败情况见[四数据集评测报告](four_dataset_evaluation_report.md)。下方 10 题 Pilot、45 题自然研究子集及阶段三、四 Gate 是**版本化历史实验协议**，不表示当前工作版仍只完成这些规模。

所有命令从仓库根目录执行。代码块注明了 Shell；Bash 的反斜杠续行不能直接粘贴到 PowerShell，PowerShell 可将同一条命令合成一行。`<run_id>` 等占位符应替换为自己的实际产物名称。

## 安装与环境检查

需要 Python 3.11+，推荐 Python 3.12。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\tracejudge.exe doctor
```

后文的 `tracejudge`、`python`、`pytest`、`ruff` 命令需要对应虚拟环境。Windows 可激活 `.\.venv\Scripts\Activate.ps1`，也可像上面一样使用 `.venv\Scripts\` 下的可执行文件，无需激活。

macOS / Linux（Bash）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
tracejudge doctor
```

`doctor` 检查 Python、配置和 Docker 等依赖，不运行解题评测。Mock Fixture 不需要真实 API Key。

## 打开可视化工作台

Windows 离线预演：

```powershell
.\scripts\run_recording_demo.ps1 -Offline
```

`-Offline` 在演示子进程中屏蔽三项 Hy3 连接配置，跳过 Docker，只启用公开 Fixture。即使根目录 `.env` 已配置密钥，页面也不会启用真实 Hy3；要调用模型，应使用下方正常启动方式。

Windows 真实 Hy3 模式，先完成[配置真实 Hy3](#配置真实-hy3)，并安装 Docker Desktop：

```powershell
.\scripts\run_recording_demo.ps1
```

启动器会复用已运行的 Docker 引擎；必要时启动 Docker Desktop 并等待就绪，默认最多 120 秒。只启动页面不会触发模型请求；在页面选择“真实 Hy3”并点击“开始评估”后，才运行完整流水线。

```powershell
# 自定义端口；Docker 启动较慢时延长等待
.\scripts\run_recording_demo.ps1 -Port 8766 -DockerWaitSeconds 240
```

同端口已有模式兼容的健康工作台时，PowerShell 启动器会复用它。若是旧离线服务或其他应用占用端口，脚本会提示停止原终端的服务或改用 `-Port`，不会自动终止进程。修改 `.env` 后应重启对应服务；已打开的旧离线页面不会因为文件变化自动变为真实模式。

macOS / Linux（Bash）：

```bash
./scripts/run_demo.sh
# 自定义端口
PORT=8766 ./scripts/run_demo.sh
```

Bash 启动器固定使用 `.venv/bin/python`，会检查依赖但不会自动启动 Docker Desktop。未配置 Hy3 时仍可使用 Fixture；真实模式需提前启动 Docker 并配置三项 Hy3 参数。若希望即使存在 `.env` 也只启用离线 Fixture，可显式屏蔽本次进程的配置：

```bash
HY3_BASE_URL= HY3_API_KEY= HY3_MODEL= TRACEJUDGE_DEMO_OFFLINE=1 ./scripts/run_demo.sh
```

默认入口为 [本地工作台](http://127.0.0.1:8765/) 和 [全流程录制模式](http://127.0.0.1:8765/?recording=1)。修改端口时同步修改地址。运行中的终端按 `Ctrl+C` 停止服务。

页面可以展示题目、结构化解答、测试结果、四层对齐、首错定位、证书与重放、公开实验对照和成本。单次运行完成后可下载结果 JSON、人类可读 HTML 报告和错误证书 JSON。完整录制步骤见[全流程分镜](demo/full_flow_storyboard.md)。

单次 Demo 的成本日志位于 `artifacts/demo_costs/<sample_id>.jsonl`：逐请求记录输入/输出 token、请求耗时、调用与重试，按 Solver / Judge 分开汇总，并记录全流程与证书重放耗时。上游没有返回 token usage 时显示缺失及覆盖情况，不能把缺失值当作零；历史方法表使用已发布报告可提供的字段，不补造旧实验未记录的 token。

公开 Fixture 回归卡片可离线重复生成：

```bash
python -m tracejudge_hy3.demo_app.regression
```

回归卡片只读取公开反事实、冻结报告和 replay receipt。无法从公开聚合恢复的字段保留 `not_computable` / `not_recorded`。

## CLI：单题与批量运行

### 无需 API 的 Mock Demo

```bash
tracejudge demo --mock --case faulty
tracejudge demo --mock --case correct
```

`faulty` 中的说明声称先处理空列表，代码却直接计算 `sum(nums) / len(nums)`；可见测试通过，隐藏/挑战测试以空列表暴露除零，产生首错步骤 `S1`、需求 `R1`、`[]` 反例和 `confirmed_bug`。`correct` 补有空列表分支，全部测试通过且不生成错误证书。

结果默认保存在 `artifacts/demo_<case>_<timestamp>.json`。CLI `demo` 只支持 Mock；真实 Hy3 使用 `run --provider hy3` 或工作台。

```bash
tracejudge run --dataset data/sample_problems.jsonl --problem-id clamp \
  --provider mock --sandbox trusted-local

tracejudge batch --dataset data/sample_problems.jsonl --provider mock \
  --sandbox trusted-local --limit 3 --output artifacts/batch_results.jsonl
```

上述本地执行命令使用精确匹配的仓库 Mock Fixture。未知题目的 Mock fallback 不属于可信 Fixture；外部代码使用 Docker。详见[执行边界](#执行边界)。这些样例仅验证系统链路，不构成真实模型 benchmark 成绩。

### 配置真实 Hy3

首次配置时，复制根目录模板为 `.env`，已有配置则直接编辑原文件。

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS / Linux：

```bash
cp .env.example .env
```

在 `.env` 中填写：

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

服务地址、密钥、模型名称三项必须**全部非空**。仅填写 API Key 不足以启用真实模式。设置由进程环境和根目录 `.env` 加载，进程环境优先；`.env` 已被 Git 忽略，仓库只提交模板。

```bash
# 仅 Solver 生成和结构化解析，不执行代码
tracejudge baseline --dataset data/sample_problems.jsonl --provider hy3 \
  --output-dir artifacts/experiments/phase1

# 完整评估链路：生成、Docker 执行、过程评估与证据
tracejudge run --dataset data/sample_problems.jsonl --problem-id safe_mean \
  --provider hy3 --sandbox docker
```

### 环境变量

| 变量 | 默认值 / 用途 |
|---|---|
| `HY3_BASE_URL` / `HY3_API_KEY` / `HY3_MODEL` | 无默认值，三项共同决定 Hy3 是否配置完整 |
| `HY3_REASONING_EFFORT` | `high`；启用时经 `extra_body.reasoning_effort` 传递 |
| `HY3_TIMEOUT_SECONDS` | `120`；单次请求超时秒数 |
| `HY3_MAX_RETRIES` | `2`；通用 Provider 入口允许的总额外调用数 |
| `HY3_MAX_PARSE_REPAIRS` | `1`；JSON 修复调用硬上限，当前允许 `0` 或 `1` |
| `HY3_ENABLE_REASONING_EFFORT` | `true`；不支持该扩展参数的服务可设为 `false` |
| `TRACEJUDGE_SANDBOX` | `docker`；也可设为 `trusted-local`，仍受执行白名单约束 |
| `TRACEJUDGE_DOCKER_IMAGE` | `python:3.11-slim`；通用流水线沙盒镜像 |
| `TRACEJUDGE_TEST_TIMEOUT_SECONDS` | `5`；单用例超时秒数 |
| `TRACEJUDGE_MEMORY_LIMIT` / `TRACEJUDGE_CPU_LIMIT` | `256m` / `1`；Docker 容器资源限制 |
| `TRACEJUDGE_ARTIFACT_DIR` | `artifacts`；通用结果与 Demo 成本输出位置 |

配置定义见 [config.py](../src/tracejudge_hy3/config.py)。阶段三 Gate E3 为保持预注册的失败分母，单独固定交通重试为零，只允许一次严格 JSON 修复；它不照搬通用入口的重试策略。

## 实验入口：先区分生成、功能执行与过程判断

| 用途 | 入口 | 主要产物 / 解释范围 |
|---|---|---|
| Solver 生成 | `tracejudge baseline` | 原始输出、结构化解答、解析与请求记录；不产生功能正确率 |
| HumanEval+ 官方执行 | `tracejudge evalplus` | 已生成代码的 Base / Extra 执行；不调用 Solver 或 Judge |
| 当前四数据集扩展 | 下方各适配器文档 | 各数据集的固定样本、方法和失败分母 |
| 五方法过程评估 | `tracejudge phase3` | 冻结轨迹、人工标签、方法结果与配对统计 |
| 公开证据与复核 | `tracejudge phase4` | 产物清单、重放 receipt、图表、复标与稳定性附加实验 |

当前扩展实验的准确命令和状态分别见：[HumanEval+ 全量执行](humanevalplus_full_execution.md)、[MBPP+ 适配器](mbppplus_adapter_v1.md)、[MBPP+ / LiveCodeBench 正式执行](mbpp_lcb_formal_execution.md)、[LiveCodeBench 适配器](livecodebench_adapter_v1.md)、[CodeJudge Judge-only](codejudge_eval_judge_only.md)、[CodeJudge v3 协议](codejudge_eval_v3_protocol.md)。汇总时以[四数据集报告](four_dataset_evaluation_report.md)的截止时间和完整状态为准。

公开四数据集证据包可只读核验，统一使用 UTF-8 模式，避免 Windows 本地默认编码影响跨平台 JSON 比较：

```bash
python -X utf8 docs/evaluation_release/2026-09-06-four-datasets-v1/verify_release.py
```

此入口核对发布文件、来源和内部一致性，不重新调用模型或执行候选；通过核验不等于重新完成一次独立实验。

### 阶段一生成、输出与续跑

先使用自建样例验证生成产物链路：

```bash
tracejudge baseline --dataset data/sample_problems.jsonl --provider mock \
  --output-dir artifacts/experiments/phase1

tracejudge baseline --dataset data/sample_problems.jsonl --provider mock \
  --output-dir artifacts/experiments/phase1 --resume-run-id <run_id>
```

每次新运行创建唯一 `run_id`，在第一次调用前显示位置，并保存：

```text
<output-dir>/<run_id>/
├── manifest.json       # 版本、数据 provenance、配置与运行身份
├── responses.jsonl     # 逐题原始输出、解析后 SolutionTrace、尝试及状态
└── summary.json        # 按题目最终状态汇总，不重复计算 skipped
```

当前阶段一 writer 写 schema v2；旧 v1 运行保持只读，不能续写混合版本。续跑要求数据集、manifest、Provider 公开配置、代码/工作树指纹和 Python/直接依赖身份一致；已成功题写入 `skipped`，失败题可继续尝试，遗留 invocation 标为 `interrupted`。单题失败保留记录并继续处理其他题；最终仍有失败时 CLI 非零退出。

Solver Prompt 仅包含公开题面、需求条款、函数签名与可见测试，不发送 `reference_code`、隐藏测试、挑战测试或人工标签。保存的解答说明是面向用户的需求理解、设计摘要和实现计划，不要求或保存模型私有思维链。

`responses.jsonl` 保留 `attempt_count`、`retry_count` 和 `attempt_outcomes`。重试可能来自 Provider 失败，也可能是 JSON 修复；只有解析失败后确有下一次调用才算实际 repair。汇总的解析成功率不等于功能正确率。详细字段和脱敏约束见[数据格式](data_format.md)。

## 历史协议：10 题 Pilot 与 45 题自然研究子集

本节保留早期实验的可追溯命令。下载会访问数据源，`baseline --provider hy3` 会调用真实模型，`evalplus --executor docker` 会执行候选；它们不属于浏览 README 或运行普通测试的前置步骤。已有正式运行不应通过覆盖产物来“更新结果”。

### 固定 HumanEval+ 来源与 10 题选择

仓库包含[来源 manifest](../data/manifests/evalplus_humanevalplus_d32357cf.json)，不提交原始快照或自动下载数据。已安装 `hf` CLI 时可显式取得固定版本：

```bash
hf download evalplus/humanevalplus --repo-type dataset \
  --revision d32357cf319e50e9c8d8dab5ea876c72b0fd321b \
  --local-dir artifacts/datasets/raw/humanevalplus

tracejudge dataset convert-humanevalplus \
  --input artifacts/datasets/raw/humanevalplus/test.jsonl \
  --revision d32357cf319e50e9c8d8dab5ea876c72b0fd321b \
  --manifest data/manifests/evalplus_humanevalplus_d32357cf.json \
  --output-dir artifacts/datasets/processed/humanevalplus-full

tracejudge dataset sample \
  --dataset artifacts/datasets/processed/humanevalplus-full/problems.jsonl \
  --manifest artifacts/datasets/processed/humanevalplus-full/dataset_manifest.json \
  --count 10 --seed 20260824 \
  --output-dir artifacts/datasets/processed/humanevalplus-pilot-10

tracejudge dataset validate \
  --dataset artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl
```

转换器离线核对 revision、许可证、164 道题及原始文件大小与 SHA256。固定选择只使用种子和公开 `problem_id`，结果为 `HumanEval/8, 26, 41, 51, 70, 81, 95, 96, 105, 120`。输出采用目录原子发布；同内容幂等，不同内容拒绝覆盖。

```bash
tracejudge baseline \
  --dataset artifacts/datasets/processed/humanevalplus-pilot-10/problems.jsonl \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --provider hy3 --output-dir artifacts/experiments/phase1
```

续跑在原命令末尾追加 `--resume-run-id <run_id>`，保持相同 manifest。该 Pilot 标签为 `humanevalplus_10_public_prompt_generation_pilot`，只报告生成和解析。公开投影的三类测试列表为空，参考代码为 withheld sentinel，不包含官方答案/测试。它不适用于 `run` / `batch` 全链路入口，功能执行必须通过阶段二适配器。内置 Mock 没有这些外部题目的离线生成 Fixture。

### 排除 Pilot 后的 45 题研究子集

```bash
tracejudge dataset sample \
  --dataset artifacts/datasets/processed/humanevalplus-full/problems.jsonl \
  --manifest artifacts/datasets/processed/humanevalplus-full/dataset_manifest.json \
  --count 45 --seed 20260825 \
  --output-dir artifacts/datasets/processed/humanevalplus-research-natural-45 \
  --exclude-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --selection-role research_natural

tracejudge baseline \
  --dataset artifacts/datasets/processed/humanevalplus-research-natural-45/problems.jsonl \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --provider hy3 --output-dir artifacts/experiments/phase1
```

该 manifest 使用 schema v2，以研究种子 `20260825` 固定选择 45 题，并排除先前 10 题。后来冻结的 42 条自然轨迹来自这一历史 cohort；45 是来源题数，42 是成功轨迹数，不能相互替换。

### 阶段二官方 EvalPlus 执行

固定 Linux/amd64 镜像为下方 digest，实测包版本 `0.4.0.dev2`、源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`、HumanEval+ release `v0.1.10`、Python `3.11.10`。镜像需提前显式拉取，执行器不会在运行时自动拉取：

```bash
docker pull --platform linux/amd64 \
  ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740
```

先用 Mock executor 检查阶段一产物链路；它不启动 Docker、不执行候选：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1/<phase1_run_id> \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2-mock --executor mock
```

正式执行已全部生成成功的 Pilot：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1/<phase1_run_id> \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-pilot-10/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2 --executor docker \
  --parallel 2 --per-task-timeout 180 --batch-timeout 900 --selection-policy all
```

45 题研究子集若阶段一存在失败，历史协议使用明确的成功题筛选和最低成功数：

```bash
tracejudge evalplus \
  --baseline-run artifacts/experiments/phase1/<phase1_run_id> \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --output-dir artifacts/experiments/phase2-research-natural --executor docker \
  --parallel 2 --per-task-timeout 180 --batch-timeout 900 \
  --selection-policy phase1-success-only --min-success-count 30
```

续跑追加 `--resume-run-id <phase2_run_id>`。来源、代码字节、provenance、固定镜像、参数与隔离配置变化会拒绝续跑。`phase1-success-only` 同时记录来源题数、成功导出数及排除的解析/Provider 失败，不能把成功子集成绩写成全部来源题成绩。

每题单独容器；官方内部 `parallel=1`、`min_time_limit=4.0`、`gt_time_limit_factor=4.0`、`test_details=true`。宿主机不执行候选，容器使用 evaluation-only 单题数据覆盖，官方测试不进入 Solver Prompt。实际产物包括 `manifest.json`、`samples.jsonl`、`evalplus_raw_results.json`、`results.jsonl`、`summary.json` 和 `execution.log`。原始 samples/raw 可能含候选和失败测试内容，应保留在被 Git 忽略的受限目录；公开结果只使用脱敏结果。

官方 raw 的 `fail` 同时涵盖错误答案、语法错误和普通候选异常，不应伪造更细的执行错误分类。Base+Extra 通过要求两者均为 `pass`；执行层通过率分母为实际完成官方执行题数，基础设施失败单独保留。公开汇总如果采用计划全分母，应同时明确这两个口径。

## 历史协议：阶段三、四 Gate

完整参数与固定证据路径见[阶段三协议](experiments/phase3_protocol.md)、[阶段四协议](experiments/phase4_protocol.md)、[标注指南](experiments/phase3_annotation_guide_v1.md)及[正式研究报告](releases/phase4/phase3_research_report_public_v1.md)。下面保留最常用复现入口；私有标签和被忽略的原始运行目录不会随 Git 克隆自动出现。

### 自然轨迹、公开反事实与五方法预检

以下是历史正式来源路径，用于核对既有运行。新实验须使用其实际来源和新版本 ID：

```bash
tracejudge phase3 preflight \
  --phase1-run artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5 \
  --phase2-run artifacts/experiments/phase2-research-natural/phase2_20260827T081939637435Z_3c366f64fc19 \
  --dataset-manifest artifacts/datasets/processed/humanevalplus-research-natural-45/dataset_manifest.json \
  --freeze-id phase3_natural_42_v1 --output-dir artifacts/experiments/phase3-freezes
```

`preflight` 全链路只读核对；通过后，用相同参数运行 `tracejudge phase3 freeze` 原子冻结自然 manifest。同名输出拒绝覆盖，两者均不调用 Provider、Docker，也不执行候选。

```bash
tracejudge phase3 counterfactual-preflight \
  --execution-run-id phase3_cf_public_15_v1 \
  --source-bundle data/phase3/public_counterfactuals_v1.json \
  --output-dir artifacts/experiments/phase3-public-evidence
```

通过后，同参数的 `counterfactual-execute` 只执行 SHA256 白名单内的公开自建代码，无 Provider 调用；预期为 15 个主体、6 pass / 9 fail。执行结果须核对超时、基础设施错误和预期偏差。随后由 `counterfactual-freeze-preflight` / `counterfactual-freeze` 绑定自然 manifest 与执行证据，冻结 42 + 15 条 overlay，保持原自然 manifest 不变。

```bash
tracejudge phase3 paired-preflight \
  --cohort-manifest artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json \
  --natural-manifest artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json \
  --provider mock --model deterministic-phase3-mock-v1 \
  --temperature 0 --timeout-seconds 120
```

该命令只核对顺序、规格、Prompt 与 Schema 哈希，不运行五方法。正式五方法研究的入口按用途分为：

| 阶段 | CLI 入口 | 核心约束 |
|---|---|---|
| Gate D 证书 | `certificate-preflight` / `certificate-generate` / `replay` | 分级发布公开证书，confirmed 可独立重放 |
| Gate E1 盲法材料 | `annotation-packet-preflight` / `annotation-packet-export` | opaque item、私有 identity map、空标签模板分别保存 |
| Gate E2 标签 | `annotation-labels-check` / `annotation-labels-freeze-preflight` / `annotation-labels-freeze` | 模板或进度不等于正式人工标签 |
| Gate E3 方法运行 | `evaluate-preflight` / `evaluate` | 真正执行必须显式 `--confirm-real-provider` |
| Gate E4 统计 | `statistics-preflight` / `statistics` | 冻结全配对顺序，失败保留，私有逐条标签不公开 |
| Gate F 报告 | `report-preflight` / `report` | 发布脱敏聚合、证书与统计解释边界 |

表内子命令均接在 `tracejudge phase3` 后；可用 `tracejudge phase3 <子命令> --help` 查看参数。57 × 5 主实验计划 285 判断，Test-only 不调 Provider，其余 228 判断每个最多一次 JSON 修复。冻结正式结果为 283 有效、2 Provider 失败，失败不删行；这属于历史已完成研究，不由预检命令重新计算。

### 阶段四公开证据与稳定性

`tracejudge phase4` 提供 `artifact-*` 产物清单、`replay-receipt*` 独立重放回执、`charts-*` 聚合图表及 `p1-*` 复标/裁决入口，完整命令见[阶段四协议](experiments/phase4_protocol.md)。公开文件索引见[阶段四发布目录](releases/phase4/README.md)。

四公开案例 × 五次 Judge 重复是独立附加实验，不并入 57 × 5 主实验。准备新运行时使用新的 run ID：

```bash
tracejudge phase4 stability-preflight --run-id <new_stability_run_id>

tracejudge phase4 stability-run --run-id <new_stability_run_id> \
  --confirm-real-provider
```

此实验需已有冻结公开执行证据，最多 20 个判断、40 次底层请求，报告实际调用和修复数。已完成的正式 run 可离线生成公开敏感性报告，不再次调用 Hy3：

```bash
tracejudge phase4 stability-sensitivity-publish \
  --run-dir artifacts/experiments/phase4-judge-stability/phase4_stability_hy3_public4x5_v1 \
  --output-dir docs/releases/phase4
```

原始首错步骤精确成对一致率 90.0% 与事后精确别名规范化的 100.0% 必须并列解释，不能互相替代。完整规则见[稳定性协议](experiments/phase4_judge_stability_protocol_v1.md)。

## 数据、输出与实现位置

核心模型为 `ProblemSpec`、`SolutionTrace`、`ExecutionSummary`、`StaticEvidence`、`ProcessAssessment`、`ErrorCertificate`，见 [schemas](../src/tracejudge_hy3/schemas/) 和[数据格式说明](data_format.md)。

| 位置 | 内容 |
|---|---|
| `data/sample_problems.jsonl` | 三道自建样例，按行保存题面、需求条款、三类测试、参考代码等 |
| `data/mock_responses/` | Schema 完整的确定性 Mock 解答 |
| `data/demo_annotations.jsonl` | 验证指标函数的标注 Fixture，不是正式人工研究标签 |
| `data/manifests/` | 固定数据 revision、许可证与哈希清单 |
| `data/phase3/` | 公开自建反事实源与协议，不包含官方隐藏评测内容 |
| `artifacts/datasets/` | 本地原始快照、转换投影与样本 bundle，Git 忽略 |
| `artifacts/experiments/` | 分阶段运行、续跑、证据与受限产物，Git 忽略 |
| `artifacts/demo_costs/` | 默认的 Demo 单样本成本日志 |
| `docs/releases/phase4/` | 版本化公开报告、回执、图表与回归卡片 |

测试通过结构化 `args` / `kwargs` / `expected` 表达，不使用 `eval()`。`expected: {"raises": "ValueError"}` 表示期望抛出对应异常；其他 `expected` 为普通 JSON 值。

完整流水线 JSON 的主要字段包括 `problem`、`solution`、`static_evidence`、`execution_result`、`process_assessment`、`counterexample`、`error_certificate`。以 faulty Mock 为例，过程判断关联 `S1` / `R1` / `A01_PLAN_CODE_MISMATCH`，反例是空列表，期望 `0.0` 而候选触发 `ZeroDivisionError`，证书裁决为 `confirmed_bug`。首次运行无错误时不生成证书；`cleared` 用于已有证书的显式复核状态转移。

实现按职责划分：

```text
src/tracejudge_hy3/
├── cli.py              # CLI 命令
├── config.py           # 环境与 .env 配置
├── schemas/            # 严格数据模型
├── providers/          # Mock / Hy3 / 请求观测
├── prompts/            # Solver / Judge / 五方法 Prompt
├── parsing/            # 结构化输出解析
├── baseline/           # 生成、断点续跑、持久化
├── dataset/            # 数据加载与投影适配
├── evalplus/           # 官方功能执行适配
├── phase3/             # 冻结、配对评估、标签、统计
├── phase4/             # 证据发布、复标、稳定性
├── static_analysis/    # AST 证据
├── sandbox/            # Docker / 可信本地执行
├── evaluator/          # 四层评估与证书
├── counterexample/     # 反例、差分执行与最小化
├── pipeline/           # 全链路编排
├── reporting/          # 指标与序列化
└── demo_app/           # 本地 HTTP 工作台、前端与单次成本
```

## 执行边界

Docker 是真实模型代码的默认执行方式：无网络、只读文件系统与受限挂载、资源/PID 限制及异常清理。它提供基础隔离；阶段二固定官方镜像中的候选与 wrapper 共享 UID，其声明边界为 `basic_non_adversarial`。完整说明见[安全与隔离](safety.md)。

`TrustedLocalSandbox` 是同机子进程与超时隔离，没有网络隔离、资源配额或权限降级。普通流水线默认只允许精确匹配的仓库 Mock Fixture；阶段三另只允许哈希匹配的公开反事实。其他代码需显式 `--allow-unsafe-local-exec`，常规真实模型运行使用 Docker 即可。

## 开发检查

激活对应虚拟环境后：

```bash
pytest -q
ruff check .
ruff format --check .
```

普通测试使用 Mock/替身，不调用真实 Hy3 API。官方执行适配器的单元测试验证输入、隔离参数、脱敏、checkpoint、统计与续跑身份；真实 Docker integration 单独 opt-in，需预先具备固定镜像。

macOS / Linux：

```bash
TRACEJUDGE_RUN_DOCKER_INTEGRATION=1 pytest -q -m docker tests/test_evalplus_docker_runner.py
```

Windows PowerShell：

```powershell
$env:TRACEJUDGE_RUN_DOCKER_INTEGRATION = '1'
try {
    .\.venv\Scripts\python.exe -m pytest -q -m docker tests/test_evalplus_docker_runner.py
}
finally {
    Remove-Item Env:TRACEJUDGE_RUN_DOCKER_INTEGRATION
}
```

架构背景见[架构文档](architecture.md)；[v0.1 MVP 范围](mvp_scope.md)和[历史实现状态](../IMPLEMENTATION_STATUS.md)用于理解早期版本，当前已实现能力与后续计划以[README](../README.md)及版本化实验报告为准。
