# P1 裁决后敏感性分析 v1

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: validate
- Origin Date: 2026-09-04
- Verification Status: ANALYZED
- Version Label: phase4_p1_post_adjudication_sensitivity_v1
- Source Agreement Manifest SHA256: `20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76`
- Source Completed Adjudication Manifest SHA256: `6e48963ee7cfe6cda2f113271286612af1640ca1abaf0eaeacedb62de2639287`

## 结论先行

- 原始完整七字段一致率保持 **19/20（95.0%）**；原始 `has_error` 一致率保持 **20/20（100.0%）**。
- 唯一过程细节分歧已通过两位原始标注者的 `documented_consensus` 完成裁决：**1/1 已解决，0 条未解决**。
- 这不能写成“裁决后标注者一致率为 20/20”。裁决解决分歧，不会改写已经观察到的原始两标注者一致性。
- `has_error`、`process_correct`、`reasoning_correct` 的固定 P1 标签确定不变；计划—代码对齐和定位类指标在固定 20 条分母下最多受 1 条影响。
- 本分析没有读取逐条原标签或方法预测，因此不伪造具体方法分数的变化方向。

## 原始一致性快照（保持不变）

完整七字段记录：19/20（95.0%）。

| 二元字段 | 原始一致率 | Cohen's κ |
|---|---:|---:|
| `has_error` | 20/20（100.0%） | 1.000 |
| `process_correct` | 20/20（100.0%） | 1.000 |
| `reasoning_correct` | 20/20（100.0%） | 1.000 |
| `plan_code_aligned` | 19/20（95.0%） | 0.875 |

| 定位字段 | 全 20 条（含双方无错 null） | 双方判错的 6 条 |
|---|---:|---:|
| `first_faulty_layer` | 19/20（95.0%） | 5/6（83.3%） |
| `first_faulty_step` | 19/20（95.0%） | 5/6（83.3%） |
| `error_type` | 19/20（95.0%） | 5/6（83.3%） |
| `joint_fault_label` | 19/20（95.0%） | 5/6（83.3%） |

## 裁决后的下游影响包络

以下百分点评估只适用于固定完整分母：1/20 = 5.0 pp，1/6 = 16.7 pp。它们是最大绝对变化上界，不是实际方法性能变化。

| 指标 | 标签影响 | 固定 20 条上界 | 固定 6 条条件分母上界 | 依据 |
|---|---|---:|---:|---|
| `has_error_detection` | 确定不变 | ≤ 0.0 pp | — | 双方原始 has_error 无分歧，裁决不触及该字段，因此固定 P1 子集上的检测标签不变。 |
| `process_correct` | 确定不变 | ≤ 0.0 pp | — | process_correct 是 has_error 的 Schema 强制补集，裁决未改变 has_error。 |
| `reasoning_correct` | 确定不变 | ≤ 0.0 pp | — | 双方原始 reasoning_correct 无分歧，裁决不触及该字段。 |
| `plan_code_aligned` | 最多影响 1 条 | ≤ 5.0 pp | — | 该字段属于唯一分歧；若构建另行版本化的共识参考集，固定 20 条分母中最多改变 1 条。 |
| `first_faulty_layer` | 最多影响 1 条 | ≤ 5.0 pp | ≤ 16.7 pp | 该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。 |
| `first_faulty_step` | 最多影响 1 条 | ≤ 5.0 pp | ≤ 16.7 pp | 该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。 |
| `error_type` | 最多影响 1 条 | ≤ 5.0 pp | ≤ 16.7 pp | 该定位字段属于唯一分歧；全 20 条最多改变 1 条，双方判错的 6 条条件分母中也最多改变 1 条。 |
| `joint_fault_label` | 最多影响 1 条 | ≤ 5.0 pp | ≤ 16.7 pp | 联合标签由三个均存在分歧的定位字段组成；全 20 条和双方判错的 6 条中最多各影响 1 条。 |
| `full_seven_field_reference_record` | 最多影响 1 条 | ≤ 5.0 pp | — | 七字段完整记录的唯一分歧已解决，但这表示 1/1 分歧完成裁决，不产生新的标注者一致率。 |

## 为什么不报告精确方法分数变化

本产物没有打开五种方法的逐条预测，也没有打开两位标注者的逐条原标签。完成态裁决记录只提供最终四字段决定，不提供两份原始逐条值。因此可以严格确认零影响字段、分歧解决状态和一条样本的变化上界，但不能在没有明确下游目标、分母和版本号的情况下声称某方法准确率具体上升或下降。

如以后需要精确裁决后方法分数，应新建独立分析 ID，明确“以共识参考集替代哪一版标签、哪些方法结果、采用何种无效判断分母”，并与当前 57×5 主分析并列报告，不得覆盖。

## 统计谬误扫描

- Coverage：11/11
- Overall Confidence：CAUTION

| 谬误 | 严重度 | 检查结果 |
|---|---|---|
| `simpsons_paradox` | NOTE | 未合并相互冲突的分层趋势；本报告分别保留全 20 条与双方判错 6 条分母。 |
| `ecological_fallacy` | NOTE | 不从聚合变化上界推断任一标注者的个人能力或判断动机。 |
| `berksons_paradox` | CAUTION | 20 条是预先冻结的确定性子集，不能外推到全部任务。 |
| `collider_bias` | NOTE | 未按裁决结果筛选条目或进行条件回归。 |
| `base_rate_neglect` | NOTE | 同时保留 6 条双方判错与 14 条双方无错的基率背景。 |
| `regression_to_the_mean` | NOTE | 没有按极端得分选择前后测样本。 |
| `survivorship_bias` | NOTE | 正式 20/20 条均进入原始一致性分析，没有排除未完成条目。 |
| `look_elsewhere_effect` | CAUTION | 仅分析预先观测到分歧的固定字段，不搜索额外终点。 |
| `garden_of_forking_paths` | CAUTION | 裁决后分析明确标为 post-hoc，原始 19/20 与 20/20 指标保持主读数。 |
| `correlation_not_causation` | NOTE | 只给出描述性影响上界，不声称裁决导致模型性能变化。 |
| `reverse_causality` | NOTE | 未使用方法预测决定裁决，也不建立时间方向因果模型。 |

## 隐私与复现

- 输入仅为 aggregate-only 一致性包和独立完成态裁决包；未读取两份逐条原标签。
- 输出不含盲化条目 ID、最终裁决值、裁决理由、案例哈希或方法预测。
- 新增 Provider、Docker、网络调用均为 0。
- JSON 与 Markdown 均可从固定源哈希确定性重建。

## 结论边界

原始两标注者完整七字段一致率保持 19/20，has_error 一致率保持 20/20；唯一过程细节分歧随后以记录在案的人类共识解决。该解决状态是 1/1 分歧已裁决，不是新的 20/20 标注者一致率。由于本分析未读取逐条原标签或方法预测，只有has_error、process_correct、reasoning_correct 的零影响可以确定；计划—代码对齐与定位类下游分数仅报告固定分母下最多一个条目的影响上界。任何精确方法分数变化都必须在另行版本化、预先声明口径的下游分析中计算，不得覆盖 57×5 主结果。
