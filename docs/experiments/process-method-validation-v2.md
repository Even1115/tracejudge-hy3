# 过程评估方法修复与开发验证 v2

2026-09-10。本轮按“输出一致性修复 → 需求与假设核对 → 开发集及构造探针验证”推进。这里只完成实现、离线检查与待运行方案；尚未测量新方法的真实模型效果。81 条保留集未开封、未标注。

## 1. 矛盾输出修复

已用重建用例复现 rep2/rep4 的故障：模型给出 reasoning=true、alignment=true，却同时给出 process=false 和错误定位。旧路径在模型调用完成后用 `model_copy(update=...)` 重算 process=true，跳过验证；随后嵌套 `AblationPrediction` 验证崩溃。

`consistency.py` 现在把公开过程三值逻辑与错误元数据的一致性校验放到 provider 的 `extra_check` 中。矛盾输出进入已有预算内的修复循环；持续无效则记录 `parse_error`。重算后的对象使用完整 `model_validate`，不删除原文引用、错误类型或位置来强行通过。结构化 pilot 与完整系统的同类入口也接入检查；官方功能结果仍由独立步骤处理。

回归覆盖首次矛盾后成功修复、两次矛盾耗尽预算、有效错误定位保持、成功项续跑不重复、无定位但有错误分类的矛盾。旧运行只留存最终预测和请求账目，未找到失败响应全文，因此用例明确标为“按事故结构重建”，不冒称真实响应回放。

这些变化使用新的实现指纹。历史五轮数据及报告保持原样；新代码不得续跑旧实验目录。旧提示词默认仍为 baseline，历史计分结果另做兼容性重放，不改写历史结果。

## 2. 通用需求—假设核对

新增 `assumption_audit_v1`：核对输入范围、量词、边界、输出语义及算法前提，输出逐字绑定的需求引文与解答主张，并区分四类：

| 分类 | 含义 |
|---|---|
| explicit_contradiction | 与明确需求或公开允许范围内的推导直接矛盾 |
| unsupported_restriction | 有语义/行为依据的额外限制，不能只因为题面未明说就判错 |
| reasonable_interpretation | 不违反公开约束的合理解释或等价实现 |
| semantic_ambiguity | 公开材料不足以排除多种解释，必要时保留 unknown |

每项记录 `requirement_id`、`requirement_quote`、`claim`、`category`、`rationale`。程序校验出处和显式跨字段矛盾；语义分类本身仍需要人工审核，引用存在不证明判断正确。没有可疑假设允许空 checks，算法正确性和计划—代码对齐仍需检查。没有任何题名、任务 ID 或针对已知标签的特殊规则。

这是“提示词与结构化核对输出”的整体方法改动，并非已经证明某一个单独提示语的因果贡献。没有修改自然样本的金标签，也没有加入按关键字自动判错的规则。

## 3. 固定比较条件及样本范围

本轮先只使用 `ablation_a`：基线与新方法看到完全相同的公开题目、冻结解答和代码，均不看到 AST 证据、官方功能汇总或人工标签。静态规则在模型外对同一输出合并，分别报告 judge_raw 与 rule_merged。这能避免将官方功能信息差异混入方法比较。

配置 `data/manifests/process_method_validation_v2.json` 钉定当前开发配置与探针包的哈希。两组都使用修复后的同一代码、模型参数和预算机制；对照基线需要新运行，不复用历史单轮结果作为此次基线。

| 样本组 | 样本数 | 方法 | 初次判断计划 | 最大请求数 |
|---|---:|---|---:|---:|
| development | 36 | baseline | 36 | 72 |
| development | 36 | assumption_audit_v1 | 36 | 72 |
| probes | 3 | baseline | 3 | 6 |
| probes | 3 | assumption_audit_v1 | 3 | 6 |

共 78 个计划判断、上限 156 次请求，费用未知。每个判断最多 2 次真实请求尝试，包含修复/重试；续跑不重置消费，模型输出失败不返还请求。重复采样不是本轮默认动作，也不按成绩决定追加次数。

development：32 成立、4 错误、0 unknown；4 个 supported 层级标签、0 个步骤级标签、2 个结构化位置标签。标签为单人 AI 辅助开发复核，0 unknown 是裁决后的状态，不代表需求歧义客观消失。

probes：3 条构造能力探针，有步骤级标签；与自然样本独立统计。加载器验证冻结父材料和探针包，对 Judge 使用不含变异名称的匿名编号；变异类型、预期影响和金标签不进入模型输入。`constructed_unexecuted` / `not_executed` 明示功能证据不可用，不执行变异代码。探针未覆盖的能力不能据此宣称有效。

## 4. 预检、执行与续跑

本机 Even 是 Python 3.9，使用现有 WSL Python 3.12。以下每段均为可直接在 PowerShell 执行的一整行。四个输出目录已通过本轮离线预检；真正调用必须显式带 `--execute`。不得将运行产物放在 `/tmp`。预检将输入与实现绑定；若此后修改代码，应创建新版本目录重新预检，不能绕过绑定继续调用。

开发集基线预检（省略 `--execute`，可重复读取不变的预检方案）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset development --rubric baseline --output artifacts/experiments/process-method-v2/dev36-baseline-20260910-v1
```

四个真实运行命令，按顺序执行、每轮结束后再启动下一轮：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset development --rubric baseline --output artifacts/experiments/process-method-v2/dev36-baseline-20260910-v1 --execute
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset development --rubric assumption_audit_v1 --output artifacts/experiments/process-method-v2/dev36-assumptions-20260910-v1 --execute
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric baseline --output artifacts/experiments/process-method-v2/probes3-baseline-20260910-v1 --execute
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric assumption_audit_v1 --output artifacts/experiments/process-method-v2/probes3-assumptions-20260910-v1 --execute
```

续跑示例；其他目录同理，在对应原命令末尾加 `--resume`：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset development --rubric assumption_audit_v1 --output artifacts/experiments/process-method-v2/dev36-assumptions-20260910-v1 --execute --resume
```

## 5. 离线规则重合并与计分

compare 阶段验证运行文件哈希、样本/方案身份及基线与新方法的模型、预算、实现一致性，再通过既有计分器重合并规则。同时输出失败/缺失/未知覆盖和两视图配对变化。部分执行也能比较，但状态必须连同覆盖率阅读。没有预测文件或报告时拒绝比较，不能把预检计划当作成绩。

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --phase compare --dataset development --baseline-run artifacts/experiments/process-method-v2/dev36-baseline-20260910-v1 --audit-run artifacts/experiments/process-method-v2/dev36-assumptions-20260910-v1 --output artifacts/experiments/process-method-v2/dev36-comparison-20260910-v1
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --phase compare --dataset probes --baseline-run artifacts/experiments/process-method-v2/probes3-baseline-20260910-v1 --audit-run artifacts/experiments/process-method-v2/probes3-assumptions-20260910-v1 --output artifacts/experiments/process-method-v2/probes3-comparison-20260910-v1
```

比较产物为各目录下 `comparison.json`。先看新增 tp 是否伴随新增 fp/弃答/失败，再审核逐条 checks 的语义依据。保留需求歧义与标签接触披露，不以总准确率单独决定新方法更优。开发验证完成并审查误报后，才决定方法冻结和保留集标注。

## 6. 本轮验证记录

- 修复前：两个重建矛盾输出测试均在 `AblationPrediction` 处复现嵌套 ValidationError。
- 修复后：本轮与关联回归测试 121 项通过；另外新增的历史五轮全量重放测试 1 项通过，汇总与冻结 `tally-report.json` 逐键相等。合计 122 项相关测试通过，Ruff 检查与格式化通过。
- 四份真实材料预检全部成功，计划判断数依次为 36、36、3、3；均未创建预测或请求账目，Solver/Judge/基准候选执行次数为 0。
- `offline-validation.json` 记录来源绑定、标签计数、定位分母与静态规则触发情况；`validation-binding.json` 绑定预检时实现及入口文件。它们不是模型评估成绩。
- 本轮未重跑全套件，不能将此前 37/969/13 作为修改后的全套件实测结果。固定结论：针对性验证通过；此前全套件存在已归因失败，本轮未重新核验全套件。
- 没有修改历史五轮产物、原始标签或保留集；没有 Git 提交。
