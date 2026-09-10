# 2026-09-10 校验修复、标签 v2 与探针复验

已完成离线实现和验证，真实模型调用 0 次、候选代码执行 0 次。
开发集不重跑，81 条保留集继续封存。旧标签、四份运行及其比较报告保留。

## 校验与失败诊断

明确矛盾或有依据的限制错误，需要至少一个公开过程维度为 false：
`reasoning_correct` 或 `plan_code_aligned`。不再把所有错误强制归给 reasoning。
例如计划正确而代码边界错误时，reasoning=true / alignment=false 是允许的。
若两个维度均未判 false，则错误分类仍被拒绝；需求原文、引用位置和总体一致性检查继续执行。
字段引用只能证明出处，校验器不能替代语义审核，也不会改写布尔值或删除定位来满足约束。
提示词和输出 schema 保留，变化通过新运行的实现指纹绑定；不混入旧运行续跑。

每个已观察到的失败请求由提供方在重试前记录 `diagnostics.jsonl`，包括：

- seq、method、item_id、当前判断累计尝试次数，以及本次调用内尝试编号；与 requests.jsonl 对应。
- stage：provider / schema / context_consistency；错误类型和脱敏错误消息。
- 脱敏响应片段、截断标记、完整脱敏响应的 SHA-256。

先对全文脱敏，再计算哈希、截断和写入；错误消息上限 4000 字符、响应片段 24000 字符。
不保存完整提示词、环境变量、配置密钥或 HTTP 请求头。
修复了既有 `Authorization: Bearer …` 脱敏顺序问题，防止只遮住 Bearer 却留下令牌。
诊断逐条追加并 fsync，报告包含该文件哈希，方法比较加载器核验声明的诊断哈希。
没有失败时可以没有该文件。诊断不增加调用，不重置预算；每题最多 2 次请求仍适用。
进程被突然终止且尚未观察到错误时，仍可能只有已计费的 dispatched 记录。

旧探针三条失败原文此前未保存，不能补造；已确认的校验缺陷来自保存的成功 baseline
输出所重建的静态回归，不是找回了当时失败的模型输出。
因此不能把旧三条 parse_error 全归因于该缺陷，也不能归因于并行运行。

## tuple_str_int 标签复核与统一离线重计分

复核说明：`artifacts/annotation-development/mbpp36-dev-20260910-v2/review_notes.md`。
修订：同目录 `revisions.jsonl`；配置：`data/manifests/process_development_labels_v2.json`。
仅 item-89cda539ad51dde89041 由成立改为错误，其余 35 条不变。

依据是解答明确承诺 `"(5,)"` 返回 `(5,)`，但其分割、逐项 int 转换算法会遇到空片段。
这是静态推演，未执行候选。算法存在边界缺陷，代码与 S1/S2 步骤一致：
reasoning=false / alignment=true，reasoning 层 P01，定位到 edge_cases_considered[1]，
步骤级首错为 null。历史官方 base/plus 通过状态保留，不据此反推公开过程成立。
复核已接触两方法预测，是预测后的 AI 辅助开发复核，供项目负责人审核，不是独立盲审金标。

重计分文件：
`artifacts/experiments/process-method-v2/dev36-label-rescore-20260910-v2/label-rescore.json`。
原口径与新口径并列，两个方法、judge_raw / rule_merged 两个视图全部计算。
两方法各自的结果如下，两视图一致：

| 标签口径 | 准确率 | TP / FP / TN / FN | 错误召回 | 误报率 |
| --- | --- | --- | --- | --- |
| 原 v1：32 成立、4 错误 | 32/36 | 1 / 1 / 31 / 3 | 1/4 | 1/32 |
| 修订 v2：31 成立、5 错误 | 33/36 | 2 / 0 / 31 / 3 | 2/5 | 0/31 |

两方法仍持平；不是方法提升。tuple_str_int 两方法均 fp→tp，
max_product 的 tp→fn 与 text_lowercase_underscore 的 fn→tp 交换仍存在。
原口径完整重算与旧 comparison.json 逐键一致。
新配置绑定原标签与输入哈希、审核说明与修订哈希、两份已审核运行报告哈希。
预测先按原运行身份校验，随后只替换计分标签；输入变化会被拒绝。

如需再次复核重计分，用新的输出目录，下面命令不调用模型：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/rescore_process_label_revision.py' --output artifacts/experiments/process-method-v2/dev36-label-rescore-20260910-v2-recheck
```

## 下一步仅运行三个探针

已分别完成下列 v2 目录的离线预检与实现绑定，没有 predictions.jsonl 或 requests.jsonl。
baseline 方案 SHA-256：`55168529df271cedeb3e5c428e504533eb84f509492433d57492b10615c9c6e3`。
assumption 方案 SHA-256：`d18eb1984457a7855a96882099f149cba76de86c1e1aae60f5ad5d97a1a1315a`。
每方法 3 个判断、最多 6 次请求，合计 6 个判断、最多 12 次请求。费用未知。
合成响应的三探针端到端测试通过只说明链路可接受合法结果，不能当真实模型成绩。

由用户执行下面两条真实调用命令；建议顺序执行，方便观察失败诊断：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric baseline --output artifacts/experiments/process-method-v2/probes3-baseline-20260910-v2 --execute
```

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --dataset probes --rubric assumption_audit_v1 --output artifacts/experiments/process-method-v2/probes3-assumptions-20260910-v2 --execute
```

两条结束后离线比较：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python '/mnt/e/犀牛鸟/tracejudge-hy3/scripts/run_process_method_validation.py' --phase compare --dataset probes --baseline-run artifacts/experiments/process-method-v2/probes3-baseline-20260910-v2 --audit-run artifacts/experiments/process-method-v2/probes3-assumptions-20260910-v2 --output artifacts/experiments/process-method-v2/probes3-comparison-20260910-v2
```

运行意外中断时，只在代码、输入、配置不变的前提下，对原 v2 命令加 `--resume`；
已消耗两次的失败判断不会自动再试。旧 v1 不能用当前实现续跑。
先核对三个探针的失败覆盖、定位与诊断，再决定后续方法调整；暂不扩大到 36 条开发集。

## 验证记录

- 修复前两项回归分别复现错误拒绝与缺少 diagnostics.jsonl。
- 修复后 99 项关联测试全部通过：方法校验、标签修订、消融、live runner、Hy3 provider、开发加载、历史重计分。
- 包括失败逐次保存、密钥脱敏、provider/schema/context 分类、续跑不重置预算、三个探针的两种模拟流程、原五轮汇总保持一致。
- Ruff check 通过，修改文件格式检查通过。
- 两份探针真实材料离线预检成功；没有真实 API 调用、候选执行、开发集重跑或 Git 提交。
- 本轮未运行全套件，不能将历史全套件的已归因失败表述为本次全套件通过。
