# 阶段三人工标注指南 v1

状态：Gate E 预注册版本 / 2026-08-28

本指南适用于冻结 cohort `phase3_cohort_42_plus_15_v1` 的 57 条轨迹。标注必须在查看五种方法预测之前完成。标注包只能包含公开题面、结构化解题说明、候选代码、与该代码哈希绑定的脱敏功能状态，以及公开自建 Fixture 的可发布执行详情。

## 1. 盲法边界

标注者不得看到：

- 五种方法的预测、Provider raw 或修复轮输出；
- 其他标注者的标签和理由；
- 反事实的修改类型、唯一修改、预期影响或父轨迹提示；
- HumanEval+ `canonical_solution`、官方 Base/Extra 测试输入、具体失败输入或 EvalPlus raw；
- API Key、Authorization、Cookie、代理或端点凭据。

标注包使用固定随机种子打乱，并以 `item_001` 等不含来源语义的盲化 ID 呈现。真实 `trace_id` 仅存在于协调者持有的身份映射中。

## 2. 标注单位与判定顺序

每个盲化条目独立标注一次，按以下顺序判断：

1. 核对公开需求与结构化说明是否一致；
2. 核对说明各步骤之间是否有证据支持；
3. 核对说明与候选代码是否一致；
4. 核对候选代码与脱敏/公开功能证据是否一致；
5. 如有错误，定位第一个有证据支持的偏离层级和步骤；
6. 选择一个主要错误类型；后续症状不取代首错。

不得仅凭代码风格、说明简短或实现不同于常见答案判错。合法等价实现应判为无错误。

## 3. 必填字段

- `process_correct`：需求理解、解题说明及其与代码的对齐是否整体正确；
- `has_error`：是否存在需求、推理、对齐、实现或执行层面的可支持错误；
- `reasoning_correct`：说明所述算法和推导是否能一般性满足公开需求；
- `plan_code_aligned`：代码是否实现了说明声称的关键步骤；
- `first_faulty_layer`：首个有证据支持的偏离层级；
- `first_faulty_step`：若能绑定结构化步骤，则记录最早步骤 ID；否则为 `null`；
- `error_type`：主要错误类型；
- `rationale`：简短、可审计的公开依据，不得写入官方隐藏输入或不可公开内容。

当 `has_error=false` 时，`first_faulty_layer`、`first_faulty_step` 和 `error_type` 必须全部为 `null`。当 `has_error=true` 时，必须填写 `first_faulty_layer`、`error_type` 和 `rationale`；只有确实无法绑定步骤时，`first_faulty_step` 才可为 `null`。

## 4. 首错层级

- `requirement`：最早偏离发生在公开目标、输入输出或必要条件理解；
- `reasoning`：需求理解可接受，但算法、推导、复杂度或边界论证首先失效；
- `alignment`：说明本身可接受，但计划与代码首先不一致，或关键实现没有说明；
- `implementation`：说明和计划可接受，但代码实现首先出现缺陷；
- `execution`：前述层级没有可支持首错，但公开/脱敏执行证据显示失败。

首错必须是“最早有证据支持的偏离”，不是最明显的最终症状。

## 5. 主要错误类型

- `R01_REQUIREMENT_MISREAD`：误读函数目标、输入或输出；
- `R02_CONDITION_OMISSION`：遗漏公开必要条件；
- `R03_UNSUPPORTED_ASSUMPTION`：引入公开需求不存在的假设；
- `P01_ALGORITHM_ERROR`：所述算法不能一般性解决问题；
- `P02_UNJUSTIFIED_STEP`：结论无法由前序步骤或公开证据支持；
- `P03_COMPLEXITY_MISMATCH`：复杂度声明与方案或代码冲突；
- `A01_PLAN_CODE_MISMATCH`：代码未实现说明声称的步骤；
- `A02_UNEXPLAINED_IMPLEMENTATION`：影响行为的关键实现未在说明中出现；
- `C01_BOUNDARY_ERROR`：空输入、极值、重复值或边界处理错误；
- `C02_CONTROL_FLOW_ERROR`：分支、循环或状态更新错误；
- `C03_DATA_STRUCTURE_ERROR`：数据结构操作与算法意图不符；
- `C04_INTERFACE_OR_FORMAT_ERROR`：签名、返回类型或格式不符合公开要求；
- `C05_HARDCODED_SHORTCUT`：依赖样例、常量或有限输入特征；
- `E01_RUNTIME_EXCEPTION`：产生未处理异常；
- `E02_TIMEOUT_OR_RESOURCE_ERROR`：超时或资源异常；
- `E03_WRONG_OUTPUT`：公开/脱敏执行状态表明输出错误。

## 6. 功能证据的使用

HumanEval+ 自然轨迹只提供 Base/Extra 的脱敏状态和哈希。标注者不得推断或要求查看具体官方失败输入。脱敏失败可以支持“存在功能错误”，但不能单独确定公开可重放反例、具体边界输入或更细的首错原因。

公开自建 Fixture 可以展示公开用例、期望值和实际结果。只有这些明确可发布的执行详情可以用于具体反例理由。

## 7. 一致性方案

主标注者完成全部 57 条。若有第二位标注者，在查看主标签之前按固定设计元数据抽取 15–20 条独立标注，先计算原始一致率和 Cohen’s κ，再讨论分歧。若没有第二位标注者，同一标注者至少间隔 7 天、隐藏旧标签并重新打乱顺序后复标；该结果只能称为 intra-rater self-consistency。

## 8. 统计口径

主要二分类终点是相对于冻结人工标签的“是否存在错误”判断正确性。有效 judgment 中 `has_error=true` 计为预测正类；`unverified_suspicion` 在该终点中仍是疑似错误预测，但不得计为 confirmed 证书。Provider、解析、AST、公开执行或基础设施失败保留在 57×5 分母中，并在主准确性终点按未得到正确判断处理，同时单列失败数量。

自然集的主要比较为 Full TraceJudge vs Test-only、Full TraceJudge vs Direct LLM Judge，报告 `n01/n10` 和双侧 exact McNemar。反事实集按父题聚类 bootstrap；固定 10,000 次、种子 `20260828`，报告配对百分点差及 95% percentile 区间。确认性主比较如作显著性判断使用 Holm 校正。所有比例同时报告原始数量，40–60 条结果只称为探索性证据；不显著不表示等价。
