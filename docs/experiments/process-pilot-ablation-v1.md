# 过程评估 pilot：2×2 证据消融实验（预注册方案、预算化运行与离线计分）

2026-09-09。本文档是消融实验的运行文档；三方法 pilot 的定义与运行记录见 [process-pilot-v1.md](process-pilot-v1.md)，两个错误案例的证据审核见 [process-pilot-case-review-v1.md](process-pilot-case-review-v1.md)。

**实验目的**：上一轮真实运行中 `full_system` 识别了两条开发集错误而 `direct_judge`/`structured_judge` 漏判，但增益可能来自静态证据、官方功能汇总、提示差异或规则合并中的任何一项。本实验通过 2×2 证据消融**控制变量定位贡献来源，不以"证明完整系统最好"为目标**。

入口仍为 `scripts/run_process_pilot.py`：`--phase ablate` 负责消融的计划登记与预算化真实判断，`--phase ablation-score` 负责离线计分。不带 `--execute` 的 ablate 阶段只做离线计划登记：不读取 API 凭据、不构造 provider、不发起网络请求、不执行候选代码。

## 实验定义

### 四个条件（2×2 证据消融）

所有条件使用同一核心任务说明、同一错误分类体系、同一输出 schema（`build_evaluator_json_schema()`）、同一 Judge 模型与生成参数、同一批 12 条冻结候选。系统提示中**唯一**的差异是证据可用性说明段（`ABLATION_SYSTEM_CORE` 中的 `{evidence_block}` 插值点，对应 `_EVIDENCE_BLOCK` 的四段中文说明）；用户负载仅按条件增删证据字段：

- `ablation_a`：公开题目（title/requirement/function_signature/requirements）+ 冻结解答轨迹。无任何证据字段。
- `ablation_b`：A + `static_evidence`（对冻结代码的真实 AST 静态分析）。
- `ablation_c`：A + `official_aggregate_functional_evidence`（官方 MBPP base/plus 总体通过状态的**汇总**快照，绑定哈希；提示中明确它不是逐用例执行记录，不得据此编造具体测试用例）。
- `ablation_d`：A + 两者。

统一约束：四个条件都不含针对两条已知错误的任何提示；不提供功能证据的条件写明"未提供"，绝不伪造 pass/fail 或执行记录；Judge 一律看不到人工标签、rationale、协调者分层信息、参考实现和隐藏测试内容；模型统一输出 reasoning/alignment/错误层级/引文定位，`functional_correct` 由模型留 null，官方功能结论只记录在预测行独立的 `functional_evidence` 字段，绝不并入过程信号；过程信号保持三值 `reasoning_correct AND plan_code_aligned`。

### 预注册方案

在发出任何请求**之前**，完整方案（条件定义与指纹、12 条输入哈希、固定种子 `seed=20260909` 的交错执行顺序、预算、停止规则、指标、预注册比较、标签范围说明）被序列化并哈希，写入 `artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1/ablation-plan.json`（plan_sha256 `1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774`）。方案哈希进入运行身份；续跑或复用目录时若方案内容变化（预算、输入、条件指纹任一改变）即拒绝（"the pre-registered plan changed ... refusing to mix runs"），必须使用新目录。执行顺序是对 48 个（条件 × 条目）对的固定种子交错洗牌，保存在方案中用于恢复；不追称旧运行已预注册。

预注册的成对比较（第二个为基线，均在 rule-merged 视图上按样本配对）：`B−A`（静态证据给 Judge 的效应）、`C−A`（汇总功能结果给 Judge 的效应）、`D−B`、`D−C`。

### Judge 原始判断与规则合并的分离

每条成功预测保存三层结构（`AblationPrediction`）：`judge_raw`（模型原始结构化判断）、`rule_report`（五条确定性规则的逐条观察——是否触发、规则 id 与依据——及 `merged_rule` 合并结果）、`assessment`（离线规则合并后的最终判断）。规则对**全部四个条件**统一离线运行（包括 Judge 未看到静态证据的 A/C），因此可以把"静态证据给 Judge 的效应"与"确定性规则的效应"分开；规则合并是对**同一份 judge 输出**的离线后处理，不消耗任何模型请求。汇总功能证据保持汇总：不伪造成逐用例 ExecutionSummary，不将测试失败直接改写成 reasoning 错误。

历史核实：对 12 条冻结候选离线运行确定性规则（纯 AST，无执行、无 API），**0 条触发**——历史 full_system 的增益来自 Judge 看到证据，而非规则合并；本消融中规则合并是否有贡献以真实运行的 `rule_effects` 为准，若仍未触发则报告"本轮未观察到规则合并贡献"。

## 预算口径与运行状态

- 一次"请求"= 一次 `_call_model` 尝试，含失败响应与格式修复追问；SDK 内部重试禁用（`max_retries=0`），运行器自身无额外重试层，provider 上限固定为 `hy3_max_retries = max_attempts_per_judgment - 1`、`hy3_max_parse_repairs = min(1, max_attempts_per_judgment - 1)`。离线规则合并不计为模型请求。
- 默认上限：总请求 **96**、单判断 **2**（12 项 × 4 条件 = 48 次判断 × 2 = 96，预算完整覆盖全部重试）。发送前在 `PilotJudgeProvider._call_model` 内做预算闸门，超限抛出 `BudgetExhaustedError`/`JudgmentCapExceededError`，请求根本不会发出。
- 保守记账沿用 live 运行器的 `RequestLedger`：发送前先写 `dispatched` 行；进程在收到结果前崩溃视为已消耗；被中断判断已花的尝试在续跑时不返还。费用保持未知（`money_estimate: null`），只记录请求数、各状态计数、耗时与 token 用量（若 provider 返回 usage）；费用未知则保持未知，不估造金额。任何产物不写入 API Key、Authorization 头或环境变量。
- 状态与退出码与三方法运行一致：`completed`（0）、`budget_exhausted`/`interrupted`/`partial_execution`（1，报告已保存）、`blocked`/拒绝（2）；认证错误首次出现即停止（`stop_reason: auth_error`）。任何状态都可对已产生的 `predictions.jsonl` 离线计分；规划数字绝不当作实测成绩。

## 断点恢复

- 运行目录内容：`ablation-plan.json`（预注册方案）、`ablation-preflight.json`（离线登记摘要）、`run-config.json`（运行身份）、`predictions.jsonl`（追加写、逐行 fsync 的 `AblationPrediction` 契约）、`requests.jsonl`（请求台账：dispatched/outcome 配对）、`run-report.json`（本轮报告）、`run.lock`（并发锁，行为同 live）。
- 运行身份绑定：pilot 配置 SHA-256、方案 SHA-256、逐条输入哈希、条件集合与逐条件提示词/可见字段/输出 schema 指纹、AblationPrediction schema 哈希、provider 公开生成配置（已脱敏）、预算上限、实现文件哈希。任一项变化即拒绝续跑。
- 续跑行为：跳过已成功行；仍有剩余尝试的失败行在续跑开始时经原子重写移除旧行后重试，已消耗尝试计入预算；`dispatched` 无配对 `outcome` 的中断请求按已消耗处理。检查点逐行严格解析，损坏、哈希不符、重复行均拒绝续跑。
- 仅含方案的目录（离线登记阶段产物）直接以 `--execute` 开始全新调度，无需 `--resume`；已有 `run-config.json` 的目录必须加 `--resume`。

## 命令（PowerShell 单行，WSL 环境）

所有命令一条完成，可在任意目录执行（凭据读取顺序：真实环境变量 > 项目根 `.env`）。本机 Conda `Even` 为 Python 3.9.24 不满足要求；以下使用已验证的 WSL Python。

1. 离线预检/方案登记（无凭据、无网络；目录 `mbpp12-ablation-20260909-v1` 已按此登记）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase ablate --output artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1
```

2. 首次真实运行（调用付费模型 API，需显式授权）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase ablate --execute --output artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1
```

3. 断点续跑（中断、预算耗尽或存在可重试失败后；身份与方案一致才接受）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase ablate --execute --resume --output artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1
```

4. 离线计分（纯离线，含规则重合并校验；若运行目录存在 `run-report.json` 会自动并入预算/请求摘要）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_pilot.py' --project-root '/mnt/e/犀牛鸟/tracejudge-hy3' --phase ablation-score --predictions artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1/predictions.jsonl --output artifacts/experiments/process-pilot/mbpp12-ablation-score-20260909-v1
```

## 输出位置与计分报告

- 运行目录：`artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1/`（方案、预检、配置、预测、台账、报告、锁）。
- 计分输出目录：`artifacts/experiments/process-pilot/mbpp12-ablation-score-20260909-v1/`，含 `ablation-scores.json`、`preflight.json`、`prediction.schema.json`（AblationPrediction 契约）、`judgment-inputs.jsonl`、`manifest.json`。
- `ablation-scores.json` 内容：四条件在 `views.judge_raw` 与 `views.rule_merged` 两个视图下的已知标签准确率、错误召回/精度、误报率、覆盖率（ok/missing/provider_error/parse_error）及 missing/provider_error/parse_error/abstain/unknown-gold-decided 分项计数；`per_sample` 给出每样本两视图的判定类别、触发规则、规则是否改变结论及判断依据；`rule_effects` 列出各条件触发规则计数与被规则改变判断的样本及字段级前后差异；`paired_comparisons` 给出四条预注册比较的逐样本转移计数与变化清单；`functional_verdicts` 独立记录官方汇总结论；`predictions_sha256`、输入哈希与标注来源均被绑定。
- 可溯源性校验：计分器对每条成功预测**离线重算** `build_rule_report` 与 `merge_offline`，与存储的 `rule_report`/`assessment` 不一致即拒绝计分（"refusing to score"），保证合并结果确实来自所存的 judge 输出。条件标识 `ablation_a/b/c/d` 与历史 `direct_judge`/`structured_judge`/`full_system` 完全分离，互不冒充；旧的 `score_predictions` 计分器与历史报告保持字节兼容。历史 direct_judge 无定位字段，跨历史比较时该项显示"不适用"，历史文件保持原样。

## 适用范围与剩余限制

- 已知过程标签仅 10 条（8 正确、2 错误），另 2 条未知保留在覆盖率但排除出二分类准确率分母。只报告计数与成对变化，**不做统计显著性或泛化声明**。
- 标签是反馈后的开发共识，不是独立留出测试集；标注者独立性未核实。
- 首错步骤与结构化定位金标数量为 0，这些指标保持 null，绝不从 rationale 文字自动填充。
- 全部 12 条 alignment 金标均为 true，无法做任何 mismatch 检出声明。
- 历史三方法结果（`mbpp12-run-20260909-v1`、`mbpp12-score-20260909-v1`）仅作历史参照，不能替代本轮消融结果。

## 真实运行与计分结果（2026-09-09）

真实运行已完成（用户显式授权执行），目录 `artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1/`：状态 `completed`，48/48 判断全部 ok，消耗 48/96 请求（无重试、无修复追问），总耗时约 1854 秒，token 用量 input 135272 / output 141162，费用未知（`money_estimate: null`）。方案哈希与预注册一致（`1448c841…9774`），无混入。计分输出在 `artifacts/experiments/process-pilot/mbpp12-ablation-score-20260909-v1/ablation-scores.json`，计分器对全部 48 条预测离线重算规则合并，全部一致。演示页面"证据消融"页签对以上产物做只读、哈希校验的可视化展示，接入与验证见 [process-pilot-ablation-view-v1.md](process-pilot-ablation-view-v1.md)。

### 过程信号（reasoning AND alignment）结果

| 条件 | 已知准确率 | tp | fp | tn | fn |
|---|---|---|---|---|---|
| ablation_a（基线） | 8/10 | 0 | 0 | 8 | 2 |
| ablation_b（+静态） | 8/10 | 0 | 0 | 8 | 2 |
| ablation_c（+功能汇总） | 8/10 | 0 | 0 | 8 | 2 |
| ablation_d（+两者） | 9/10 | 1 | 0 | 8 | 1 |

judge_raw 与 rule_merged 两个视图逐行相同：四条件 48 次判断中确定性规则 0 次触发，**本轮规则未触发，未观察到规则合并贡献**（与历史离线核实一致）。这只说明本轮样本上的观察，不外推为规则在其他任务上无效。

### 成对比较（预注册）

以下均为"本轮每个条件每题一次判断"下的单次观察，不构成必要条件、稳定交互效应或一般因果结论，尚需重复实验验证。

- `B−A`：12 条样本零变化（`tn->tn` 8、`fn->fn` 2、`unknown_decided` 2）。本轮观察中，静态证据进入 Judge 输入未伴随任何判断变化。
- `C−A`：12 条样本零变化。本轮观察中，官方功能汇总进入 Judge 输入未伴随任何判断变化——对 max_product，C 的 Judge 明确看到 base pass / plus fail，但按提示约束将"功能未通过不自动意味着过程错误"执行到底，未深入排查。
- `D−B` 与 `D−C`：均仅 1 条变化，`item-e5b9ff02f8bd9e3f3b61`（max_product）`fn->tp`。本轮每个条件每题一次判断中，A/B/C 未检出该样本，D 检出；D 的 Judge 指出单次扫描只在递增段起点累乘、未考虑段内后缀在含零/负数时更优，并在解释文本中自举反例 `[-2,0,1]`（模型输出文字，未执行验证），与案例审核中的零值推演方向一致。尚需重复实验验证该差异是否稳定复现，不能据此断言"必须同时提供两类证据才能检出"。

### 与历史 full_system 的差异（如实记录）

历史 `full_system` 在 10 条已知标签上 10/10（两条错误均检出）；本轮 `ablation_d` 为 9/10——division_elements（`item-caf2c4228350e46f5b68`）本轮四个条件**相对当前冻结标签全部漏判**，包括证据最全的 D。D 的 Judge 明确写道"功能未通过不等于过程有错"，并认定 `//` 取舍与示例一致、过程自洽。

两件事需要分开：其一，**当前计分依据**是 12 条冻结开发共识标签，本轮漏判计数据此得出，标签与历史成绩均不改动；其二，**标签争议**已于 2026-09-09 裁决：仅凭公开需求文本与示例复核（不查阅任何方法预测），维持 R03 原标签不变，不产生新标签版本、无需重计分；完整推理链与边界见 [process-pilot-case-review-v1.md](process-pilot-case-review-v1.md) "标签争议裁决记录"一节。历史 full_system 与本轮 ablation_d 的提示和处理并不完全相同（后者提示统一、功能结论不写入判断字段），历史与本轮判断不同，尚不能区分提示差异与采样波动的影响，不能直接归因比较。

### max_product 证据等级（如实记录）

- D 的 Judge 解释中自举的反例 `[-2,0,1]` 是**模型输出文字**，未执行验证；案例审核中的反例 `[0,2,3]` 预期 6 是**人工静态推演**，同样未执行验证（候选代码本轮未运行）。
- 官方证据只有 base pass / plus fail 的**汇总**状态；不能声称上述反例就是官方 plus 失败的具体用例或原因。
- D 判断中的原文引用与冻结字段匹配成功，只证明**位置绑定**成立，不单独证明错误判断正确。

### 结论边界

48 次判断、10 条已知标签、2 条已知错误：以上只是计数与逐样本配对变化，不构成统计显著性结论，也不外推到其他题目或模型。可报告的事实是：在本轮统一提示下，①规则未触发，未观察到规则合并贡献；②B、C 相对 A 未伴随判断变化；③仅 D 在 max_product 上观察到一次 fn→tp，尚需重复实验验证；④division_elements 相对冻结标签漏判，其标签争议已裁决维持原标签（见案例审核文档裁决记录）。

## 重复验证方案（2026-09-09 预注册，先于任何新增运行）

- **重复次数**：共 n=5 轮完整 A/B/C/D 运行，次数在任何新增轮次开始前登记，事后不增减。第 1 轮为已完成的 `mbpp12-ablation-20260909-v1`：它与后续轮次绑定同一预注册方案（plan_sha256 `1448c841…`，`plan_fingerprint` 不含时间戳，逐轮可用产物核验），条件、提示、输出契约与预算相同；新增 4 轮（rep2–rep5）。
- **固定配置**：同一 pilot 配置、同一方案哈希、同一组条件提示/可见字段/输出契约指纹、同一模型配置；每轮预算上限 96 请求（48 判断 × 最多 2 次尝试），与首轮一致。若某轮身份核验（方案哈希/条件指纹/输入哈希/模型配置/实现指纹）与其他轮不一致，汇总脚本拒绝合并，不做悄悄池化。
- **运行方式**：新增轮次目录 `mbpp12-ablation-20260909-v1-rep2` … `-rep5`，均使用未修改的 `--phase ablate --execute`；中断轮次用 `--resume` 续跑（同一身份核验）。最终以非 completed 结束的轮次如实按部分完成纳入：失败/缺失/弃答单独计数，**绝不重跑替换结果不利的轮次**。
- **统计口径（预注册）**：主口径为 rule_merged 视图的公开过程结论（judge_raw 视图同时报告）。逐样本×逐条件报告 5 轮中"判过程不成立"的次数 k 与逐轮结果清单：对已知错误样本即 tp 频率，对已知正确样本即误报频率；弃答（ok 但三值结论为 null）、调用失败、结果缺失分别计数，既不算检出也不算未检出。**波动**定义为同一（样本，条件）在已完成的非弃答轮次中检出次数既非 0 也非全中。另报告每轮的成对比较（B−A、C−A、D−B、D−C）转移计数。
- **报告承诺**：报告全部 5 轮，不挑选任一轮作为代表结果；每轮数字来自对预测记录的离线计分器重算（`score_ablation` 含确定性重合并核验），未通过核验的轮次不纳入。金额未由 Provider 返回，成本保持未知。
- **执行分工**：新增轮次为付费 API 调用，由人工触发；汇总为纯离线脚本 `scripts/tally_ablation_repeats.py`，不发起任何模型请求。

新增 4 轮尚未执行；完成后逐轮结果与检出频率填于本节，不改动上面任何既有轮次的数字。

新增轮次命令（付费 API，人工逐轮执行；每轮跑完再跑下一轮）：

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd '/mnt/e/犀牛鸟/tracejudge-hy3' && PYTHONPATH=src /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python scripts/run_process_pilot.py --phase ablate --execute --output artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep2"
```

（rep3/rep4/rep5 同上，仅替换目录名尾部；中断的轮次加 `--resume` 续跑，不换目录重来。）

全部轮次完成后的汇总命令（纯离线，无模型调用）：

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd '/mnt/e/犀牛鸟/tracejudge-hy3' && PYTHONPATH=src /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python scripts/tally_ablation_repeats.py --runs artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1 artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep2 artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep3 artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep4 artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1-rep5 --expect-plan-sha256 1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774 --output artifacts/experiments/process-pilot/mbpp12-ablation-repetition-tally-20260909-v1"
```

## 离线验证记录

使用 mock transport（无任何真实网络与付费调用）共 36 项 pilot 测试通过（test_process_pilot.py 10 + test_process_pilot_live.py 14 + test_process_pilot_ablation.py 12），其中消融新增 12 项覆盖：四条件可见字段正确且无隐藏证据/标签/凭据泄漏、核心提示与输出契约四条件一致、固定种子交错顺序完整且与方案一致、离线方案登记不构造 provider 且可重复、完整运行的可溯源性与两个视图计分、规则贡献与 Judge 效应分离（含字段级前后差异）、预算覆盖修复追问与紧预算中途停止、中断续跑不重做成功且预算累计、方案或输入变化拒绝混跑、空预测按 missing 计分而不伪造、篡改合并结果拒绝计分、运行产物不含密钥、CLI 参数守卫。本轮触及的 Python 文件 Ruff check 与 format 均通过。

2026-09-09 新增重复验证汇总（tally）的离线验证：`tests/test_process_pilot_repetition.py` 11 项通过——合成三轮检出频率与波动判定（k/n 分母只含已完成且未弃答轮次）、弃答/调用失败/结果缺失分别计数且不算检出、未知金标样本逐轮判定方向如实报告、混合身份（模型配置不同）拒绝汇总、方案哈希与预登记值不符拒绝、钉板文件篡改拒绝、目录重复拒绝、换绑 pilot 配置拒绝、越仓库路径拒绝、CLI 落盘 tally-report.json + manifest 哈希钉板且不覆盖既有目录；并对真实第 1 轮产物做集成核验（方案哈希、completed 状态、max_product 仅 D 检出、division_elements 零检出与文档一致）。合并回归：test_process_pilot_repetition.py + test_process_pilot_ablation.py + test_demo_ablation.py 共 35 项通过；新文件 Ruff check 与 format 均通过。tally 全程离线，不发起模型请求。

2026-09-09 在同一工作树（WSL venv，`PYTHONPATH=src`）运行全套件实测：**38 失败、924 通过、13 跳过**（612 秒，日志 `/tmp/tj-fullsuite.log`）。失败全部位于 phase3/phase4 报告与标注断言文件及 test_benchmark_codejudge_v3_execution、test_demo_app、test_evalplus_exporter、test_humaneval_internal_diagnostics。按本任务约定的归因规则（"在旧文件失败 ≠ 既有问题"；只有同环境可比基线或直接证据才能标"确认既有"）逐项归因：**35 项为 CRLF 环境差异、1 项为 HEAD 既有失败、1 项为环境/设计行为、1 项为脏工作树内既有改动引起的测试间污染（已做最小修复）**；本轮改动未引入新失败。详细证据见本文末"测试归因记录"一节。

## 测试归因记录（2026-09-09）

归因方法与边界：基线是在 WSL `/tmp/tj-baseline` 用 `git clone --no-local -c core.autocrlf=false` 得到的**独立 LF 副本**（不动工作树，不 stash/reset/restore 制造基线），用同一 venv、同一 `PYTHONPATH=src` 对 11 个含失败的测试文件重跑（`38 failed` 全套件日志 `/tmp/tj-fullsuite.log`，失败清单 `/tmp/tj-failed-list.txt`，基线日志 `/tmp/tj-baseline.log`）。LF 基线结果：135 通过、5 失败、8 错误、1 跳过。分类如下。

**环境问题（35 项，CRLF 字节哈希）**：test_phase3_annotations 10、test_phase3_execution 1、test_phase4_p1_annotations 7、test_phase4_p1_formal_packet 1、test_phase4_p1_study 7、test_phase4_contest_summary 2、test_phase4_public_release 中 2 项哈希绑定断言、test_humaneval_internal_diagnostics 4、test_evalplus_exporter 1。这些测试对**哈希冻结的字节级文件**做 SHA-256 断言；本工作树是 `core.autocrlf=true` 的 Windows 检出，被测文件落盘为 CRLF，与冻结时（LF）哈希不符。证据：①LF 基线中这 35 项全部通过；②逐字节 diff 显示差异全部为 `\r`；③对三个代表性文件直接验证——LF 归一化后的 SHA-256 与各自钉死的哈希完全一致（annotation_protocol `a2d77ae2…`、chart manifest `20d94ad5…`、evalplus 受控源清单 `0572b2f0…`，后者含 57 处 CRLF）；④`git diff HEAD -- data/phase3/` 为空（phase3 改动仅为行尾差异）。其中 test_evalplus_exporter 的 `test_existing_real_schema_v1_pilot_remains_readable_without_rewrite` 在基线中因阶段一 pilot 产物被 git 忽略而跳过、未被基线覆盖，其 CRLF 归因由上述钉死哈希直接比对单独证明。

**确认既有（1 项，真实 HEAD 失败）**：test_phase4_public_release.py::test_implementation_status_marks_release_and_p1_agreement_complete 在干净的 LF HEAD 基线中同样失败（断言 `'阶段四 P0 + P1 Gate D 一致性分析' in status` 不成立，文档内容漂移），与本任务改动无关，不属本次修复范围。

**环境/设计行为（1 项）**：test_benchmark_codejudge_v3_execution.py::test_formal_artifacts_resume_from_exact_persisted_prefix。本工作树失败于按设计的脏树拒绝（"cannot resume a dirty TraceJudge checkout without a working-tree fingerprint"）；LF 基线则因被 git 忽略的钉死数据集（CodeJudge_Eval_0shot_easy.json）缺失而失败。两种环境下均非代码回归。基线另有 2 项 contest_summary 失败与 8 项 codejudge ERROR 均源于基线副本缺少 git 忽略的本地数据，属基线侧数据可得性，不计入归因。

**确认既有 + 本次最小修复（1 项）**：test_demo_app.py::test_fixture_mode_runs_real_pipeline_and_finds_the_bug 仅在全套件中失败、单文件运行通过。根因是工作树内**未提交的** `scripts/run_mbppplus.py` 在 preflight 后新增 `os.chdir(project)`（注释意图：规避 Docker Desktop on WSL 删除 staging inode 的问题）；pytest 经 `mbpp_cli` fixture 以进程内方式运行 CLI，fixture 的临时项目根在 `tmp_path`，导致 cwd 泄漏给字母序在后的 test_demo_app.py；`run_demo("fixture")` 把产物写到 `<cwd>/artifacts/…`，而 `_public_artifact_reference` 按 `Path.cwd()` 计算相对路径，仓库根相对断言失败。证据：产物实际出现在 `/tmp/pytest-of-even/pytest-38/test_mbpp_incomplete_generatio0/artifacts/`，证明泄漏来自 test_benchmark_launchers.py 的完整生成路径测试，且两个 preflight 测试同样泄漏。归因：相对本任务起点的工作树为**确认既有**（由此前未提交的 launcher 工作引入；干净 LF HEAD 上同一测试对 28/28 通过，最小对复现证明与本任务改动无关），**不影响本次页面工作**（仅影响 fixture 产物路径断言）。最小修复：`tests/test_benchmark_launchers.py` 的 `mbpp_cli` fixture 增加 `monkeypatch.chdir(Path.cwd())`，teardown 时恢复调用方 cwd。修复后该测试对 29/29 通过。

**本次引入并修复**：无（本任务的消融、文档与页面改动未引入任何新失败）。

**归因未确认**：无。38 项失败全部有上述可归类的证据支撑。

**已通过的针对性验证**：36 项 pilot 测试（mock transport）；修复后的 test_benchmark_launchers.py + test_demo_app.py 配对 29/29；重复验证 tally 合并回归 35/35（test_process_pilot_repetition.py + test_process_pilot_ablation.py + test_demo_ablation.py，含对真实第 1 轮产物的集成核验）；本轮新增的消融展示页测试与浏览器验证（含四条件数字与计分文件逐项核对、重点案例、空状态与 XSS 探针）见 [process-pilot-ablation-view-v1.md](process-pilot-ablation-view-v1.md) 的验证记录。未重跑 612 秒全套件（遵循"先用已有日志与针对性测试"的约定）；上述 35 项 CRLF 失败在任何 `autocrlf=true` 的 Windows 检出上都会重现，属检出环境问题而非代码缺陷。

**测试状态（固定表述）**：针对性验证通过；全套件仍有已归因失败（2026-09-09 实测 38 失败 / 924 通过 / 13 跳过；其中 1 项已修复，37 项未修复 = 35 CRLF 环境 + 1 HEAD 既有 + 1 环境/设计行为）。失败归因只说明原因，不替代全套件通过。

**2026-09-09 第二次全套件实测与归因补充**（任务三收尾时）：全套件一度实测 47 失败 / 959 通过。逐项比对当日 38 项失败清单：净增 10 项（test_baseline_experiment 5、test_humanevalplus_ingestion 1、test_phase4_stability 4），另有 1 项（test_demo_app 污染项）随此前最小修复转为通过。10 项新增失败共一根因：`baseline/runner.py` 的 `_git_command*` 助手 5 秒超时，在脏树增大后 `git diff --binary HEAD` 于 /mnt/e 9P 挂载实测约 7.4 秒，超时使工作树指纹为 None，触发按设计的脏树拒绝（"cannot resume a dirty TraceJudge checkout without a working-tree fingerprint"）。直接证据：同一 git diff 命令单独执行 rc=0 但耗时 7.4s > 5s；`_git_metadata` 探测复现 `working_tree_sha256=None`。本时段改动只新增未跟踪文件（不进入 `git diff HEAD`），非内容回归。最小修复：超时提升为命名常量 `_GIT_COMMAND_TIMEOUT_SECONDS = 30`（不改任何语义，仅消除慢挂载上的假"不可用"）；修复后受影响 3 个文件 39 项全部通过。**最终实测：37 失败 / 969 通过 / 13 跳过，剩余 37 项即此前已归因集合（35 CRLF 环境 + 1 HEAD 既有 + 1 环境/设计行为）**。

（历史记录，写作于真实运行之前：当时真实模型调用尚未执行。**后续状态**：真实运行已于 2026-09-09 完成，见上节"真实运行与计分结果"。）
