# 过程评估 pilot：首错位置、离线预检、预算化真实运行与离线计分

2026-09-09。本工作包入口为 `scripts/run_process_pilot.py`，提供三个阶段：离线预检（默认）、真实模型调度（`--phase run --execute`，需显式授权）与离线计分（`--phase score`）。默认阶段不读取 API 凭据、不发起网络请求、不启动候选代码。

后续工作：2×2 证据消融实验见 [process-pilot-ablation-v1.md](process-pilot-ablation-v1.md)；两个错误案例的证据审核见 [process-pilot-case-review-v1.md](process-pilot-case-review-v1.md)。

## 已完成的行为

- `ProcessAssessment.first_faulty_location` 是可选结构化字段，保留旧数据兼容性。位置包含 `source_field`、原文 `quote`，按需提供 `step_id`、`entry_index`、`code_span`。摘要错误可以保持 `first_faulty_step=null`。
- 引文必须出现在指定的冻结字段；步骤 ID、边界条目索引、代码行范围均校验。规则输出、共享评估入口及 Hy3 输出校验接入此字段。引用存在仅证明位置绑定，不能证明错误判断正确。
- 原系统 `process_correct` 继续表示功能与过程的联合结论。离线计分使用 `reasoning_correct AND plan_code_aligned` 的三值逻辑，与人工过程标签对应，独立记录功能证据。
- 固定配置 `data/manifests/process_pilot_v1.json` 绑定材料清单、修订标注和审核快照。预检继续验证源注册表、执行结果、候选代码、记录与条目哈希；不同来源、未解决分歧、缺失或重复条目会阻止运行。
- 输出的 `judgment-inputs.jsonl` 不含人工标签或 rationale。新视图移除标题中的 `Mbpp/数字:` 前缀；原始材料不改写，已发生的题目身份暴露不能撤销。官方 base/plus 成绩作为独立的汇总证据提供，不伪造逐用例执行结果。
- 真实运行已实现（`process_eval_v2/live.py`）：三种方法按统一定义调度，请求预算与单判断尝试上限强制执行，检查点可断点续跑，结果直接以 Prediction 契约写入、与既有离线计分器兼容。预检报告的 `ready_for_live_run` 现为 `true`。

## 三种方法的定义

三种方法使用同一份冻结材料（`judgment-inputs.jsonl` 中的 `PilotInput`），同一 Judge 模型与生成参数，逐条独立判断，互不参照。Judge 看不到人工标签、rationale、协调者分层标签、参考实现或隐藏测试内容；可见字段由方法定义枚举，并写入每次运行的身份指纹（`method_fingerprints()`）。

- `direct_judge`：仅见题目公共信息（title/requirement/function_signature/requirements）与冻结解答轨迹。提示词是最简评审（两个问题：推理是否正确、计划与代码是否一致）。输出为精简的 `DirectJudgment` schema（两个三值布尔 + 解释），由运行器映射为 `ProcessAssessment`，`functional_correct` 固定为 `null`，`process_correct` 按三值 AND 重算。不输出定位字段。
- `structured_judge`：相同输入材料，但使用完整结构化评审提示（四个层级、错误分类、引文绑定的首错位置）。输出完整 `ProcessAssessment` schema（含 `first_faulty_layer`/`first_faulty_step`/`first_faulty_location`/`error_type`），其中 `first_faulty_location` 的引文与字段绑定由 `_context_check` 校验。`functional_correct` 同样强制为 `null` 并重算 `process_correct`。
- `full_system`：在 structured_judge 的模型判断之外，增加确定性证据层：(1) 对冻结候选代码运行真实 AST 静态分析（`analyze_code`）得到 `StaticEvidence`；(2) 官方汇总功能证据（base/plus 状态，绑定到冻结候选与执行记录的哈希，无逐用例记录）；(3) 确定性规则层 `evaluate_alignment_rules` 与 `combine_assessment` 的既有组合逻辑；最终由 `apply_aggregate_functional` 把绑定后的官方功能结论写入 `functional_correct` 并按三值逻辑重算联合 `process_correct`。提示词中明确说明该功能证据是“汇总状态而非逐用例执行记录，不得据此编造测试细节”。

可比性边界：三种方法的模型、材料、预算口径一致，但 `full_system` 多了静态与汇总功能证据，其 `functional_correct` 非空而前两种恒为 `null`——跨方法比较过程维度（reasoning/plan 对齐）是主要可比对象，联合 `process_correct` 的跨方法比较须注明该证据差。三种方法均不重新调用 Solver、不重新生成解答、不执行候选代码。

## 预算口径与运行状态

- 一次“请求”= 一次 `_call_model` 尝试，包含失败响应与格式修复追问；SDK 内部重试已禁用（`max_retries=0`），唯一重试层是 provider 的 `_call_with_retries`，其上限由运行器固定为 `hy3_max_retries = max_attempts_per_judgment - 1`、`hy3_max_parse_repairs = min(1, max_attempts_per_judgment - 1)`，运行器自身不再叠加任何重试层。
- 默认上限：总请求 72、单判断 2（12 项 × 3 方法 = 36 次判断 × 2），可用 `--max-requests`、`--max-attempts-per-judgment` 调整。发送前在 `PilotJudgeProvider._call_model` 内做预算闸门：超限抛出 `BudgetExhaustedError` / `JudgmentCapExceededError`（均为 `ProviderError` 子类），请求根本不会发出。
- 保守记账：每个请求在发送前先写入 `requests.jsonl` 的 `dispatched` 行；进程在收到结果前崩溃也视为已消耗。续跑不重置已消耗预算。
- 失败分类依据遥测结果而非消息匹配：最后一次尝试收到了响应但解析失败记为 `parse_error`，否则记为 `provider_error`。费用保持未知（`money_estimate: null`），仅记录请求数、各状态计数、耗时与 token 用量（若 provider 返回 usage）。任何产物（含报告、JSONL、锁文件）都不写入 API Key、Authorization 头或环境变量；配置指纹只存端点的 SHA-256。
- 状态与退出码：`completed`（0）、`budget_exhausted`/`interrupted`/`partial_execution`（1，报告已保存）、`blocked`/拒绝续跑（2）。认证错误在首次出现后记录为 `provider_error` 并立即停止（`stop_reason: auth_error`）。任何状态都可对已产生的 `predictions.jsonl` 离线计分；规划数字绝不当作实测成绩报告。

## 断点恢复

- 每个运行目录包含：`run-config.json`（运行身份）、`predictions.jsonl`（追加写、逐行 fsync 的 Prediction 契约）、`requests.jsonl`（追加写的请求台账：dispatched/outcome 配对）、`run-report.json`（本轮报告）、`run.lock`（`O_CREAT|O_EXCL`，持有进程退出后按死 pid 收养；读不出 pid 时保守视为存活并拒绝）。
- 运行身份绑定：pilot 配置 SHA-256、逐条输入哈希、方法集合、逐方法提示词/可见字段/输出 schema 指纹、Prediction schema 哈希、provider 公开生成配置（已脱敏）、预算上限、实现文件哈希。任一项变化即拒绝续跑（`refusing to mix runs`）。
- 续跑行为：跳过已成功行；仍有剩余尝试的失败行在续跑开始时通过原子重写（临时文件 + `os.replace`）移除旧行后重试，已消耗尝试计入预算；`dispatched` 无配对 `outcome` 的中断请求按已消耗处理。检查点逐行严格解析，任何损坏、哈希不符或重复行都拒绝续跑；不会覆盖已完成历史。

## 执行离线预检

在项目目录下使用已安装依赖的 **Python 3.11+**。本机 Conda `Even` 环境是 Python 3.9.24，不满足项目要求；请使用下面的 WSL 命令。脚本会在导入项目依赖前检查版本并给出提示。

其他已满足版本及依赖要求的环境可以执行：

```powershell
python scripts/run_process_pilot.py --output artifacts/experiments/process-pilot/my-preflight-v1
```

本机已验证的 WSL 环境可从 PowerShell 直接执行以下一整行：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --output artifacts/experiments/process-pilot/my-preflight-v1
```

省略 `--output` 只输出预检 JSON，不创建目录。输出目录必须是 `artifacts/experiments/process-pilot` 下的新目录，已存在则拒绝覆盖。

输出包含 `preflight.json`、`judgment-inputs.jsonl`、`prediction.schema.json`、`manifest.json`。清单绑定输出文件及离线实现的 SHA-256，不复制私有人工标签。预算规划（36 次判断、72 次请求上限）只是计划；真实请求数见运行报告。

## 执行真实运行（需显式授权）

真实运行会调用付费模型 API。凭据读取顺序为：真实环境变量 > 项目根目录的 `.env`（与启动目录无关，可从任意目录执行）。首次运行：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase run --execute --output artifacts/experiments/process-pilot/mbpp12-run-20260909-v1
```

中断、预算耗尽或存在可重试失败后，断点续跑（同一目录，身份一致才接受）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase run --execute --resume --output artifacts/experiments/process-pilot/mbpp12-run-20260909-v1
```

不带 `--resume` 指向已存在目录会被拒绝；缺少凭据会在创建目录前以 `[blocked] ProviderAuthError` 退出（退出码 2）。每轮结束打印并保存 `run-report.json`：状态、停止原因、预算消耗与剩余、各方法 ok/provider_error/parse_error/pending 计数、请求状态与 token 用量、待完成清单及输入哈希。

## 对已有预测离线计分

预测为 JSONL，每行对应一个 `(method, item_id)`，方法值为 `direct_judge`、`structured_judge` 或 `full_system`。`input_sha256` 必须使用同次预检输出中的条目哈希。成功行 `status="ok"` 且包含符合导出 schema 的 `assessment`；失败行用 `provider_error` 或 `parse_error`，并令 `assessment=null`。缺失的行会统计为 missing。运行阶段写出的 `predictions.jsonl` 直接满足该契约，部分结果也可计分：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase score --predictions artifacts/experiments/process-pilot/mbpp12-run-20260909-v1/predictions.jsonl --output artifacts/experiments/process-pilot/mbpp12-score-20260909-v1
```

`scores.json` 会绑定预测文件 SHA-256、输入哈希及标注来源。错误是正类：`tp` 表示正确发现错误，`fp` 表示误报。`accuracy_all_known` 的分母包含全部已知标签，缺失、失败和弃答不算正确；`*_decided` 指标仅计算明确预测，须与 `decision_coverage_known` 一起阅读。空分母返回 null。位置指标仅对有支持且非空的金标计算，且要求模型判定过程有错。结构化位置是字段、原文引文等完整对象的精确匹配，语义相近的引文不自动算命中。

## 已审核数据的适用范围

12 条开发集修订共识：8 正确、2 错误、2 未知。功能汇总证据为 6 通过、6 未通过，与过程标签分别记录。两个错误有层级标签，但均没有非空的首错步骤或结构化首错位置；本轮不从理由文字自动推导这些标签，步骤和结构化定位指标的分母为 0、值为 null。

这些是反馈后的开发标签，不能表述为独立留出测试集成绩。标注者是否为两名独立人类尚未核实。未知标签保留在覆盖率中，排除出二分类准确率分母。

## 本机验证记录

真实材料预检已保存至 `artifacts/experiments/process-pilot/mbpp12-preflight-20260909-v1/`，状态为 `ready_for_offline_scoring`、`ready_for_live_run: true`，Solver、Judge 和基准候选执行次数均为 0。上述 12 条标签及其来源通过哈希核验。

离线验证使用 mock transport（无任何真实网络与付费调用）共 24 项测试通过，覆盖：三种方法按各自定义运行且无标签泄漏、禁止 Solver 调用与候选执行、成功/认证错误/超时/格式错误/格式修复各路径、多层重试不突破单判断与总预算上限、中断后续跑（成功跳过、预算累计、失败重试、在途中断按已消耗处理）、配置/输入变化拒绝续跑、损坏检查点与并发锁保护、预测文件与既有计分器兼容、项目根 `.env` 凭据加载（环境变量优先）、全部运行产物不含密钥或 Authorization 头。pilot 相关测试 24/24 通过；全套件 881 项通过，39 项失败均为 phase3/phase4 既有报告与标注断言，与本改动无关（本改动只触及未跟踪的新文件）。本轮 Python 文件 Ruff check 与 format 均通过。真实模型调用尚未执行，等待显式授权。
