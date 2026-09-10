# 定位输出规范 v2 与独立引用覆盖审核

2026-09-10。已完成实现、离线报告与新探针预检；本次真实模型调用、候选执行、开发集重跑均为 0。
旧预测、标签、精确匹配计分、原五轮消融产物不改。81 条保留集继续封存。

## 1. 新输出规范与历史兼容

新方法名为 `baseline_location_v2` / `assumption_audit_location_v2`。
新配置 `data/manifests/process_method_location_v2.json` 绑定原配置哈希，
数据和每题最多两次请求的预算保持一致；新提示和输出 schema 使用独立指纹。
旧 `baseline` / `assumption_audit_v1` 提示、schema 指纹不变，仍能加载并重放历史报告。
本次没有修改共享 ProcessAssessment / FaultLocation 的历史 schema。

新规范作用于评估顶层、first_faulty_location 和 checks[*].claim 中的所有 code_span：

- 只接受 null 或 `^L[1-9][0-9]*(?:-L[1-9][0-9]*)?$`，例如 `L3`、`L3-L5`。
- 行号从 solution_trace.code 第一行起算，闭区间，不得逆序或超出代码行数。
- 代码文字应放在 source_field=code 的 quote，quote 必须逐字来自指定字段。
- 不确定行号时输出 null，不要把表达式或函数名放进 code_span。
- implementation_steps 使用真实 step_id；code 的 step_id 必须为 null。
- first_faulty_step 与结构化位置的 step_id 保持一致。affected_steps 不是首错步骤。

`location_format.py` 为 Judge 生成附带 pattern、描述和示例的新 JSON Schema 副本，
并通过 extra_check 执行相同约束。错误仍进入现有预算内修复和脱敏诊断，不自动删除或转换字段。
新方法的离线加载也会重验这些约束。旧成功预测的顶层 code_span 曾允许自由文本，
因此不能把旧预测伪装成符合新规范的模型输出。

此前三探针 v2 共 11 次请求，5 次首次尝试失败随后修复：4 次 code_span 格式失败、1 次布尔矛盾。
新规范针对已观察到的格式问题；尚无新版本真实调用结果，不能宣称请求次数已降低。

## 2. 步骤位置与代码位置的审核

| 探针 | 冻结金标 | 保存的预测位置 | 审核解释 |
| --- | --- | --- | --- |
| add_pairwise，probe-c37f9befe938d0bc2a6e | alignment / S2，短语“偏移一位” | 两方法均引用 code 的 tup[2:]，step=null | 计划要求相邻配对，代码偏移两位。代码引用能支持失配判断，但不是步骤来源地址，不能自动计为 S2 命中。 |
| filter_oddnumbers，probe-ccb9336523c7bcc36667 | reasoning / S2，短语 x % 2 == 0 | 两方法均引用 S2 的完整句子 | 矛盾出现在步骤本身，代码照着错误条件实现。步骤命中；整句不等于短语，精确匹配仍不命中。 |
| find_char_long，probe-07c8d275bebcd1296b3c | alignment / S2，短语 length >= 4 | 基线引用 code；假设核对引用 S2 完整句子 | >=4 的计划正确而 >4 的代码不一致。代码位置与计划义务位置都能用于说明失配；现有金标采用后者。 |

对两个 alignment 探针，金标 S2 表示“发生失配的计划步骤”，不应解读成“该步骤的推理本身错误”。
当前结构化位置只选择一个来源，代码定位时 first_faulty_step 必须为 null；
相关计划步骤可在 explanation / affected_steps 中解释，但不自动转成首错步骤。
这限制了现有步骤指标的解释范围。未来若分设缺陷位置与相关计划步骤，需另开合同版本和评价协议，
不能回填旧判断或根据模型输出更换金标。

## 3. 新增补充指标，旧指标保留

协议：`data/manifests/process_location_coverage_v1.json`。
指标名：`same_source_gold_quote_coverage_v1`；协议哈希由实现校验。
这是在观察三探针 v2 结果后定义的事后补充指标，不是预注册主指标。

分母：错误金标且定位 supported、有结构化位置，金标引用在指定来源文本中唯一出现。
没有合格金标时返回 null；缺失、失败、弃答仍留在合格分母中且不命中。
不唯一的金标引用独立记为 ambiguous_gold_quote，不悄悄移除而不披露。

命中同时要求：预测成功且公开过程判错；首错层级一致；source_field、step_id、entry_index
均一致；预测引用已通过原文绑定，并逐字包含完整金标引用。
不做跨字段推断、释义匹配、大小写/空白归一化，不从 affected_steps 猜首错步骤。
每条报告原/预测引用与字符数，避免把“引用更长”误解成更精确。

已对相同历史预测离线生成：
`artifacts/experiments/process-method-v2/probes3-location-review-20260910-v1/location-review.json`。
报告保留 original_comparison，另列 supplementary，绑定来源、协议与审核实现哈希。

| 指标 | 基线 | 假设核对 |
| --- | --- | --- |
| 原结构化位置精确匹配 | 0/3 | 0/3 |
| 原首错步骤命中 | 1/3 | 2/3 |
| 新同源金标引用覆盖 | 1/3 | 2/3 |

judge_raw / rule_merged 两视图一致。完整旧比较结果重放后与保存报告相等。
覆盖增加来自对同一步骤整句引用的描述，不是新模型成绩；跨来源的代码引用仍不计覆盖。
该指标也不能证明语义正确、首错定位精确或泛化效果；三个全为错误的构造探针不能评估总体误报率。

## 4. 新三探针预检与真实执行命令

已准备两份独立新目录，每方法 3 个判断、最多 6 次请求，合计最多 12 次请求。
新代码尚未进行真实调用。只安排三探针，不扩大到开发集。

基线预检方案 SHA：`3948af2964842f981f7c26a696fad9048e42819d38ebe274cbc35202fb07d207`。
假设核对预检方案 SHA：`01bb5f67e42abb4392d4ff4cd7c6b3e1e0a5af81ce9a77b71cd0c7bbd2c7a855`。

用户需要真实复验时，顺序运行：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric baseline_location_v2 --output artifacts/experiments/process-method-v2/probes3-baseline-location-20260910-v1 --execute
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric assumption_audit_location_v2 --output artifacts/experiments/process-method-v2/probes3-assumptions-location-20260910-v1 --execute
```

两方法都结束后生成原口径比较：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --phase compare --location-v2 --dataset probes --baseline-run artifacts/experiments/process-method-v2/probes3-baseline-location-20260910-v1 --audit-run artifacts/experiments/process-method-v2/probes3-assumptions-location-20260910-v1 --output artifacts/experiments/process-method-v2/probes3-location-comparison-20260910-v1
```

另行导出新预测的补充定位报告：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/review_process_locations.py' --location-v2 --baseline-run artifacts/experiments/process-method-v2/probes3-baseline-location-20260910-v1 --audit-run artifacts/experiments/process-method-v2/probes3-assumptions-location-20260910-v1 --output artifacts/experiments/process-method-v2/probes3-location-review-20260910-v2
```

意外中断可在相同实现/配置下对原执行命令加 --resume；预算不重置。
此前运行目录不能用当前实现续跑。若预检后再修改实现，需另建运行目录，不修改旧绑定。
对比时先看有效覆盖和格式修复次数，再看原定位指标与独立补充指标，不挑选成功样本。

## 5. 验证

- 117 项相关测试通过，涵盖新字段格式、逆序/越界行号、预算内修复、新旧方法身份隔离、两种新方法的三个探针模拟流程、历史指纹及报告重放、引用覆盖分母和跨来源拒绝。
- Ruff check / format 通过。旧精确匹配计分器未修改。
- 新方法真实材料预检成功，均为 plan_registered_offline，模型/候选执行次数为 0。
- 未运行全套件，不宣称全套件通过；本轮未做 Git 提交。
