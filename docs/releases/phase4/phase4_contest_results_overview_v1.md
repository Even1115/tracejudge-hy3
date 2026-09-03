# TraceJudge-Hy3 竞赛结果总览 v1

## Material Passport

- Origin Skill: academic-research-suite/experiment-agent
- Origin Mode: validate_engineering
- Verification Status: ANALYZED
- Version Label: phase4_contest_results_overview_v1

## 一句话结论

TraceJudge-Hy3 在 57 条冻结轨迹上对 5 种方法完成 285 个严格配对判断；最佳观察到的错误检测准确率为 56/57（98.2%），Full TraceJudge 的有效判断混淆矩阵为 TP=13、FP=1、TN=42、FN=1，对应误报率 1/43（2.33%）。这些是单主标注者、探索性结果，不构成普遍优越性结论。

## 四个核心数字

| 冻结轨迹 | 配对判断 | 最佳检测准确率 | Full 误报率 |
|---:|---:|---:|---:|
| **57**（42 自然 + 15 反事实） | **285**（5 方法） | **98.2%**（56/57，Four-layer Structured） | **2.33%**（1/43） |

最佳首错步骤定位为 Four-layer + AST 的 10/11（90.9%）；Full TraceJudge 为 9/11（81.8%）。结构化方法的最佳检测值不应被改写为“Full 方法显著优于全部基线”。

## 错误检测与误报率

| 方法 | 全分母准确率 | TP | FP | TN | FN | FPR=FP/(FP+TN) | 首错步骤 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Test-only | 54/57（94.7%） | 11 | 0 | 43 | 3 | 0/43（0.00%） | 0/11（0.0%） |
| Direct LLM Judge | 55/57（96.5%） | 14 | 1 | 41 | 0 | 1/42（2.38%） | 9/11（81.8%） |
| Four-layer Structured | 56/57（98.2%） | 14 | 1 | 42 | 0 | 1/43（2.33%） | 8/11（72.7%） |
| Four-layer + AST | 54/57（94.7%） | 14 | 2 | 40 | 0 | 2/42（4.76%） | 10/11（90.9%） |
| Full TraceJudge | 55/57（96.5%） | 13 | 1 | 42 | 1 | 1/43（2.33%） | 9/11（81.8%） |

准确率使用 57 条全分母并把 Provider 失败计为错误；TP/FP/TN/FN 与 FPR 只针对有效二元判断，因此两种口径不能互相替代。

## 人工核验覆盖

| 核验层 | 已完成/计划 | 覆盖率 | 当前状态 |
|---|---:|---:|---|
| 单主标注者盲法标签 | 57/57 | 100.0% | 已冻结，用于当前结果 |
| 第二标注者独立复标 | 0/20 | 0.0% | 尚未收集，agreement=`not_computed` |

这里的 100% 表示当前 57 条研究 cohort 都有第一位标注者标签，不表示已完成跨标注者验证。第二标注者完成前不报告 raw agreement 或 Cohen's κ。

## 难度代理结果

| 代理难度 | 纳入自然轨迹 | Base+Plus 通过率 |
|---|---:|---:|
| easy-proxy | 14 | 14/14（100.0%） |
| medium-proxy | 14 | 13/14（92.9%） |
| hard-proxy | 14 | 13/14（92.9%） |

观察到的下降从 `medium-proxy` 开始，hard 层没有继续下降，且只有 2 个失败；详见[难度代理分层分析](phase4_difficulty_proxy_analysis_v1.md)。

## 反事实结果与边界

- Full TraceJudge 相对 Test-only 在 15 条反事实上的检测准确率差为 +13.3 pp（14/15 vs 12/15），但只有 3 个父题 cluster，95% cluster bootstrap 区间为 [0, 20] pp。
- `reasoning_swap` 中 Test-only 为 0/3，四个 Judge 方法均为 3/3，说明只看测试无法发现“代码对、解释错”的过程问题。
- `equivalent_implementation` 中 Test-only 为 3/3，Judge 方法均为 2/3，说明语义等价实现仍可能被误报；这也是 FPR 必须独立展示的原因。

## 证据入口

- [冻结正式研究报告](phase3_research_report_public_v1.md)
- [难度代理分层分析](phase4_difficulty_proxy_analysis_v1.md)
- [确定性聚合图表](charts/phase4_public_charts_v1/)
- [2 分钟公开 Fixture Demo 脚本](phase4_fixture_demo_v1.md)

## 解释限制

- 当前为单主标注者、单轮标签；第二标注者一致性尚未计算。
- 57 条研究集和 3 个反事实父题不足以支持一般模型能力或因果结论。
- `ANALYZED / CAUTION / CANNOT_VERIFY` 边界保持不变；本总览没有重跑 Hy3，也没有覆盖任何冻结产物。
