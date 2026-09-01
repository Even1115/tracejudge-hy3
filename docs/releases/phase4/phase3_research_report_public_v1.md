# TraceJudge-Hy3 阶段三研究验证报告

## Material Passport

- Origin Skill: academic-research-suite/experiment-agent
- Origin Mode: validate
- Origin Date: 2026-08-31
- Verification Status: ANALYZED
- Version Label: phase3_gate_f_report_v1

## 1. 报告身份与结论边界

- Report ID: `phase3_report_primary_round1_v1`
- Paired run: `phase3_hy3_57x5_v1`
- Statistics: `phase3_stats_primary_round1_v1`
- 研究规模：42 条自然轨迹 + 15 条反事实轨迹 = 57 条；5 种方法，共 285 个配对。
- 本报告只提供探索性证据，不代表完整 HumanEval+ 排名、标准 pass@k、普遍模型能力或因果效应。
- 总体置信等级：**CAUTION**。核心原因是单标注者单轮次、自然错误基率低、反事实仅 3 个父题 cluster，以及 2 个 Provider 失败。

## 2. 执行覆盖与失败核算

285 个配对中，`valid_judgment=283`，`provider_error=2`；其余 parse/AST/公开执行超时/基础设施/skipped/reused 均为 0。
两条 Provider 连接失败未重试、未删行，按预注册协议在主指标中计为错误并单独报告。

| 方法 | 有效/总数 | Provider 失败 | 实际尝试 | JSON 修复 | 已知 Prompt tokens | 已知 Completion tokens | 总耗时（秒） | 金额成本 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Test-only | 57/57 | 0 | 0 | 0 | 0 / 0 rows | 0 / 0 rows | 0.0 | 不适用 |
| Direct LLM Judge | 56/57 | 1 | 57 | 0 | 130459 / 56 rows | 124037 / 56 rows | 1281.4 | 不可用 |
| Four-layer Structured Judge | 57/57 | 0 | 58 | 1 | 137420 / 57 rows | 134436 / 57 rows | 1429.4 | 不可用 |
| Four-layer + AST | 56/57 | 1 | 57 | 0 | 144942 / 56 rows | 129709 / 56 rows | 1268.5 | 不可用 |
| Full TraceJudge | 57/57 | 0 | 58 | 1 | 169300 / 57 rows | 145578 / 57 rows | 1442.9 | 不可用 |

金额成本未由 Provider 返回，不能由 token 数反推或声称精确费用。耗时为逐配对 duration 求和，不等同于端到端墙钟时间。

## 3. 主终点：错误存在判断

人工标签正类为 14/57（24.6%）：自然轨迹 2/42（4.8%），反事实轨迹 12/15（80.0%）。因此总体准确率必须与来源分层和混淆计数一起解释。

| 方法 | 全部 57 条 | 自然 42 条 | 反事实 15 条 |
|---|---|---|---|
| Test-only | 54/57（94.7%）；95% Wilson CI [85.6%, 98.2%] | 42/42（100.0%）；95% Wilson CI [91.6%, 100.0%] | 12/15（80.0%） |
| Direct LLM Judge | 55/57（96.5%）；95% Wilson CI [88.1%, 99.0%] | 41/42（97.6%）；95% Wilson CI [87.7%, 99.6%] | 14/15（93.3%） |
| Four-layer Structured Judge | 56/57（98.2%）；95% Wilson CI [90.7%, 99.7%] | 42/42（100.0%）；95% Wilson CI [91.6%, 100.0%] | 14/15（93.3%） |
| Four-layer + AST | 54/57（94.7%）；95% Wilson CI [85.6%, 98.2%] | 40/42（95.2%）；95% Wilson CI [84.2%, 98.7%] | 14/15（93.3%） |
| Full TraceJudge | 55/57（96.5%）；95% Wilson CI [88.1%, 99.0%] | 41/42（97.6%）；95% Wilson CI [87.7%, 99.6%] | 14/15（93.3%） |

反事实单方法列只报告原始数和比例；E4 中的独立二项 Wilson 区间没有建模同父题相关性，Gate F 不将其用于推断。

描述性地，Four-layer Structured Judge 为 56/57（98.2%），Full TraceJudge 为 55/57（96.5%）。这不是预注册的结构化方法对完整方法确认性比较，不能据此声称某组件有确定增益或损害。

## 4. 预注册配对主比较

### 4.1 自然轨迹：双侧精确 McNemar + Holm

| 比较 | Full 正确 | 基线正确 | Full−基线 | n01（基线错/Full 对） | n10（基线对/Full 错） | exact p | Holm p |
|---|---:|---:|---:|---:|---:|---:|---:|
| full_tracejudge_vs_test_only | 41/42 | 42/42 | -2.4 pp | 0 | 1 | 1.000 | 1.000 |
| full_tracejudge_vs_direct_llm_judge | 41/42 | 41/42 | +0.0 pp | 1 | 1 | 1.000 | 1.000 |

自然集没有观察到 Full TraceJudge 优于两个预注册基线的证据：相对 Test-only 少 1 个正确判断，相对 Direct Judge 正确数相同；两个校正后 p 值均为 1.000。该结果不能反向证明方法等效。

### 4.2 反事实：父题 cluster bootstrap

| 比较 | Full 正确 | 基线正确 | Full−基线 | 95% cluster bootstrap CI | 父题 clusters |
|---|---:|---:|---:|---|---:|
| full_tracejudge_vs_test_only | 14/15 | 12/15 | +13.3 pp | [+0.0, +20.0] pp | 3 |
| full_tracejudge_vs_direct_llm_judge | 14/15 | 14/15 | +0.0 pp | [+0.0, +0.0] pp | 3 |

Full 相对 Test-only 多 2/15 个正确判断，差值 +13.3 pp，但区间含 0；相对 Direct Judge 为 0/15，区间 [0,0] 也不能作为等效证据，因为只有 3 个父题 cluster。

## 5. 过程与首错定位（探索性）

| 方法 | 过程判断 | 推理判断 | 计划—代码对齐 | 首错层 | 首错步骤 | 错误类型 |
|---|---|---|---|---|---|---|
| Test-only | N/A | N/A | N/A | 2/14（14.3%） | 0/11（0.0%） | 2/14（14.3%） |
| Direct LLM Judge | 49/57（86.0%） | 53/57（93.0%） | 55/57（96.5%） | 9/14（64.3%） | 9/11（81.8%） | 11/14（78.6%） |
| Four-layer Structured Judge | 49/57（86.0%） | 54/57（94.7%） | 52/57（91.2%） | 13/14（92.9%） | 8/11（72.7%） | 13/14（92.9%） |
| Four-layer + AST | 48/57（84.2%） | 54/57（94.7%） | 54/57（94.7%） | 10/14（71.4%） | 10/11（90.9%） | 13/14（92.9%） |
| Full TraceJudge | 50/57（87.7%） | 56/57（98.2%） | 55/57（96.5%） | 7/14（50.0%） | 9/11（81.8%） | 10/14（71.4%） |

Four-layer Structured Judge 的首错层和错误类型为 13/14；Four-layer + AST 的首错步骤为 10/11；Full TraceJudge 分别为 7/14、9/11、10/14。由于这些不是预注册确认性比较且分母很小，只能描述，不能据此归因组件效果。

## 6. 反事实类型拆分

| 修改类型 | Test-only | Direct | Structured | +AST | Full |
|---|---:|---:|---:|---:|---:|
| reasoning_swap | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| code_defect | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| boundary_deletion | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| shortcut | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| equivalent_implementation | 3/3 | 2/3 | 2/3 | 2/3 | 2/3 |

Test-only 在 reasoning_swap 为 0/3，而四个 Judge 均为 3/3；在 equivalent_implementation 中 Test-only 为 3/3，其余方法均为 2/3。每类只有 3 条，不能形成普遍机制结论。

## 7. 公开错误证书 Demo

该 Demo 来自 Gate D 的公开自建 Fixture，不是从 HumanEval+ 隐藏失败输入恢复，也不代表五方法证书有效率。输出目录同时保存原始脱敏 JSON 证书的逐字节副本。

- Certificate ID: `certificate:gate-d-confirmed-safe-mean-empty-v1`
- Problem: `safe_mean`
- Verdict: `confirmed_bug`
- 公开需求：An empty list returns 0.0 without raising an exception.
- 首错层 / 步骤：`implementation` / `S1`
- 错误类型：`C01_BOUNDARY_ERROR`
- 公开执行证据 SHA256：`cfd897334643853fc10901835a5203aa51ee7edd4442e314893c1e5bc152e670`
- 受限公开 Fixture 已验证：`true`
- Gate F replay receipt：未生成；本门槛不自动执行候选。

重放命令：

    tracejudge phase3 replay --certificate artifacts/experiments/phase3-public-certificates/phase3_gate_d_public_certificates_v1/certificates/certificate_001.json --cohort-manifest artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json --natural-manifest artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json --source-bundle data/phase3/public_counterfactuals_v1.json

## 8. 统计验证与警告

- Verification Status: **ANALYZED**
- Overall Confidence: **CAUTION**
- Reproducibility: **CANNOT_VERIFY**（Gate F 未重跑外部 Hy3；聚合输入与输出已进行精确哈希绑定）

| 警告 | 影响 |
|---|---|
| 只有一名主标注者和一轮标签；agreement_kind=not_computed。 | 全部人工标签比较 |
| 反事实只有 3 个父问题 cluster，bootstrap 区间不稳定。 | 反事实配对差值 |
| E4 中反事实单方法 Wilson 区间未建模同父题相关性，Gate F 不将其用于推断。 | 反事实单方法准确率区间 |
| Test-only 不输出过程、推理或计划代码字段；对应 0 分子是结构性不适用。 | Test-only 非二元检测指标 |
| 285 对中 2 对为 Provider 连接失败，已按协议计入全分母错误并单独报告。 | Direct LLM Judge、Four-layer + AST |
| p=1 或差值区间 [0,0] 都不能证明方法等效。 | 所有无差异主比较 |
| Gate D 证书是公开工程 Fixture，不代表五方法在研究 cohort 上的证书有效率。 | 错误证书 Demo |

## 9. 统计谬误扫描

覆盖：**11/11**。CAUTION 表示需要限制解释，不表示数据必然错误。

| 类型 | 严重度 | 检查结果 | 报告护栏 |
|---|---|---|---|
| 1. Simpson's paradox | CAUTION | 总体 Full−Test-only 为 +1/57，但自然集为 −1/42、反事实为 +2/15；这不是经典同向子组反转，却说明总体值会掩盖明显的来源异质性。 | 始终并列报告自然与反事实分层结果，不以总体排名替代配对主比较。 |
| 2. Ecological fallacy | NOTE | 分析单位是冻结轨迹；没有从聚合方法指标推断单个题目、开发者或一般模型行为。 | 结论限定在本轮 57 条冻结轨迹。 |
| 3. Berkson's paradox | CAUTION | 自然轨迹只来自阶段一成功生成的 42/45 条，属于按可分析性筛选后的群体。 | 不外推到全部生成尝试；同时保留 45 条来源核算。 |
| 4. Collider bias | NOTE | 未进行含控制变量的回归或条件化分析，本轮没有可识别的 collider 调整。 | 不对未建模的控制路径作因果解释。 |
| 5. Base-rate neglect | CAUTION | 人工错误正类为 14/57；自然仅 2/42，反事实为 12/15。总体准确率受到负类占比和来源构成影响。 | 同时报告正类基率、混淆计数、precision/recall 和分层准确率。 |
| 6. Regression to the mean | NOTE | 没有按极端前测分数选组的前后测设计，不适用。 | 不使用改善或退步的前后测语言。 |
| 7. Survivorship bias | CAUTION | 3 条阶段一 Provider 失败没有完整轨迹，不能进入 57 条过程评估，但仍属于 45 条来源分母。 | 明确区分来源覆盖与完整轨迹上的条件性研究结果。 |
| 8. Look-elsewhere effect | CAUTION | 两项预注册自然主比较使用 Holm；其余多指标和五类反事实拆分均为探索性描述。 | 不从次要指标中事后挑选显著性结论。 |
| 9. Garden of forking paths | NOTE | cohort、标签协议、主比较、seed 和失败分母均在比较前冻结；仍存在单标注者与工程选择带来的研究自由度。 | 将全部结果标记为探索性并保留冻结哈希。 |
| 10. Correlation is not causation | CAUTION | 配对结果描述固定实现和预算下的关联差异，不能证明 AST、动态反例或四层结构本身造成性能变化。 | 使用“观察到”而非“导致/提升”的因果措辞。 |
| 11. Reverse causality | NOTE | 没有时间方向或预测因果主张，不适用。 | 不把标签与方法判断的对应关系解释为方向性因果。 |

## 10. 可支持与不可支持的结论

可以支持：在本轮固定 Hy3、Prompt、单候选、57 条冻结轨迹和既定预算下，五方法已完成严格配对；完整方法在反事实 reasoning_swap 上捕获了 Test-only 看不到的说明错误，但没有在两个预注册主比较中显示确定优势。

不能支持：完整 TraceJudge 普遍优于简单方法；AST 或动态反例造成了性能提升；p=1 证明方法等效；3 条/类型证明普遍机制；单标注者标签具有跨标注者一致性；Gate D 工程证书代表研究 cohort 的证书准确率。

## 11. 审计身份

- Statistics manifest SHA256: `7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8`
- Statistics report SHA256: `972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`
- Paired run manifest SHA256: `685b25af287bfc973c5000573eac0cf4ff505f91d95fff2faa403f69626f1edc`
- Paired results SHA256: `332932e949281c84402046dbd25e0110fb7a7e7e224c71b17487226fa1098999`
- Paired index SHA256: `b1a6c6a61a4439d3e667ebd52ddba8cba98f8ee196c1cac8dce200f38c857247`
- Certificate manifest SHA256: `4d4d2f8ce5ee86d96aaeffbec2f2d686a395427a340703534b8460a866f144e8`
- Confirmed certificate SHA256: `d332ce1fbe601547763b580fbe0e22286737711627ae8f45937ff568e62c9cdb`

