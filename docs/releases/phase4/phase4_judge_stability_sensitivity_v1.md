# Full TraceJudge 标识符规范化敏感性报告 v1

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: validate
- Origin Date: 2026-09-03
- Verification Status: ANALYZED
- Version Label: phase4_judge_stability_identifier_sensitivity_v1
- Source Run ID: `phase4_stability_hy3_public4x5_v1`
- Source Protocol SHA256: `703d246afe819b78c861d70bca7ca2413fc86a7dbe8bcdf94c1a95fa31062c84`
- Source Results SHA256: `95875439f210cbc7430871fdb43c8fff831a5ddfed16d31706584adb9ee4e16e`

## 分析目的

本报告不重新运行 Hy3。它在保持预注册原始字符串精确一致率不变的前提下，检查一个已观察到的字段路径别名是否改变 `first_faulty_step` 与三字段联合标签的一致率。

## 主结果与事后结果必须并列

| 口径 | 首错步骤一致对/可比对 | 成对一致率 | 研究身份 |
|---|---:|---:|---|
| 原始精确字符串匹配 | 36/40 | **90.0%** | 预注册主结果，保持不变 |
| 精确别名规范化 | 40/40 | **100.0%** | post-hoc 敏感性分析，不替代主结果 |

## 规范化规则

只使用一条精确白名单映射：

```text
solution_trace.requirement_understanding → requirement_understanding
```

不删除任意前缀、不做模糊匹配，也不合并其他标识符。该映射只影响 `reasoning_swap` 第 4 次判断；两种字符串都指向输入结构中的同一 `solution_trace.requirement_understanding` 字段。

## 总体敏感性

| 字段 | 原始精确一致 | 规范化一致 | 变化 |
|---|---:|---:|---:|
| `has_error` | 40/40（100.0%） | 40/40（100.0%） | +0.0 pp |
| `first_faulty_step` | 36/40（90.0%） | 40/40（100.0%） | +10.0 pp |
| `error_type` | 40/40（100.0%） | 40/40（100.0%） | +0.0 pp |
| 三字段联合标签 | 36/40（90.0%） | 40/40（100.0%） | +10.0 pp |

## 分案例敏感性

| 案例 | 首错步骤：原始 | 首错步骤：规范化 | 联合标签：原始 → 规范化 |
|---|---:|---:|---:|
| `normal_correct` | 10/10（100.0%） | 10/10（100.0%） | 100.0% → 100.0% |
| `reasoning_swap` | 6/10（60.0%） | 10/10（100.0%） | 60.0% → 100.0% |
| `boundary_error` | 10/10（100.0%） | 10/10（100.0%） | 100.0% → 100.0% |
| `equivalent_implementation` | 10/10（100.0%） | 10/10（100.0%） | 100.0% → 100.0% |

`normal_correct`、`boundary_error` 和 `equivalent_implementation` 的所有读数均未改变。`reasoning_swap` 的首错步骤和联合标签从 6/10（60.0%）变为 10/10（100.0%）。

## 解释

证据支持的描述是：Full TraceJudge 在这四个公开案例中稳定判断“是否有错”和“错误类型”；原始首错步骤精确一致率为 90.0%。唯一表面差异是同一位置的两种字符串写法，说明输出标识符应由未来版本的 Schema 或后处理层进行规范化。

这不能证明 Judge 在一般任务上的首错定位达到 100%，也不能把事后规范化读数写成预注册结果。

## 验证与完整性

- 20/20 个源判断通过严格 Schema 校验，trial ID 唯一且顺序完整；
- source protocol、results、JSON report 与 Markdown report 的 SHA256 均与 manifest 一致；
- 重新计算的原始四字段一致率与冻结 `report.json` 完全一致；
- 本分析新增 Provider、Docker、网络调用均为 0；
- 未公开 trial 级 evidence summary、Provider raw、隐藏测试或人工标签。

## 统计谬误扫描

- Coverage：11/11；Overall Confidence：CAUTION。
- Base-rate neglect：四个案例是目的性选择的 2 个有错、2 个无错，不能视为真实错误率。
- Garden of forking paths：规范化规则在看到结果后定义，因此明确标为 post-hoc，并保留原始指标。
- Look-elsewhere effect：只报告预注册三字段与联合标签，没有按结果筛选额外性能终点。
- Simpson、生态谬误、Berkson、collider、均值回归、生存者偏差、相关因果化和反向因果在本描述性重复评审设计中未发现适用证据。

## 结论边界

规范化结果是读取既有 20 次判断后进行的事后敏感性分析，仅说明一个已观察到的路径前缀别名如何影响字符串精确一致率。预注册的原始 90.0% 首错步骤成对一致率保持主结果；规范化后的 100.0% 不得替代主结果、并入冻结的 57×5 主实验，或外推到其他任务、模型、Prompt 与未来服务版本。
