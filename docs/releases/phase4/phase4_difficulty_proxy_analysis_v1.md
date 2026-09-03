# TraceJudge-Hy3 难度代理分层分析 v1

## Material Passport

- Origin Skill: academic-research-suite/experiment-agent
- Origin Mode: validate_engineering
- Verification Status: ANALYZED
- Version Label: phase4_difficulty_proxy_analysis_v1

## 结论

HumanEval+ 在本项目所用字段中没有官方难度标签，因此这里不把 `unknown` 改写成伪官方的 easy/medium/hard。我们使用不读取模型输出、人工标签或执行结果的**参考实现结构复杂度代理**，先对冻结的 45 题来源队列等量分层，再观察其中成功生成并纳入自然研究集的 42 条轨迹。

观察到的下降从 `medium-proxy` 开始：`easy-proxy` 为 14/14（100.0%），`medium-proxy` 和 `hard-proxy` 均为 13/14（92.9%）。hard 层没有继续下降，且总共只有 2 个失败，因此不能声称已经找到稳定、可推广的难度退化点。

## 分层结果

| 代理难度 | 来源题数 | 纳入自然轨迹 | 来源排除 | Base+Plus 通过 | 通过率（95% Wilson CI） | 代理分数范围 |
|---|---:|---:|---:|---:|---:|---:|
| easy-proxy | 15 | 14 | 1 | 14/14 | 100.0% [78.5%, 100.0%] | 31–117（中位 76） |
| medium-proxy | 15 | 14 | 1 | 13/14 | 92.9% [68.5%, 98.7%] | 125–216（中位 166） |
| hard-proxy | 15 | 14 | 1 | 13/14 | 92.9% [68.5%, 98.7%] | 219–401（中位 296） |

`来源排除` 是阶段一 Provider 失败，不是根据难度、标签或结果替换题目；三个代理层各有 1 条来源题未进入 42 条自然轨迹。

## 代理定义

对固定 revision 的 HumanEval+ 参考实现计算：

```text
score = 10 × 非空代码行
      + 20 × 控制流节点数
      + 10 × 最大控制流嵌套深度
      + AST 节点数
      + 5 × max(参数数 − 1, 0)
```

在连接任何执行结果前，按 `score` 排序并切成 15/15/15；同分时只用固定盐与公开 `problem_id` 的 SHA256 破同分。分数仅用于本 45 题样本内排序，不是心理测量量尺。反事实 15 条用于测试变异机制，故继续按 mutation type 分析，不混入任务难度层。

## 复现身份

- Dataset revision: `d32357cf319e50e9c8d8dab5ea876c72b0fd321b`
- Raw snapshot SHA256: `908377f1daf28dcb36846db73a5662b2e05a9907407c2696c89ad9d3b0b04492`
- 45-task source SHA256: `701ed34b3a66032f0f356734607709fb3d65f753dbe01cf4b4395c4409df2dc0`
- 42-trace natural manifest SHA256: `a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692`
- 只读复算：`.venv/bin/python -m tracejudge_hy3.phase4.contest_summary --difficulty`

## 限制

- 这是参考实现结构复杂度代理，不是 HumanEval+ 官方难度，也不等于人类感知难度。
- 每题只有一个模型候选，样本量不足以估计可靠的难度曲线。
- 通过率描述 Solver 代码的 EvalPlus Base+Plus 结果，不是过程评估器准确率。
- 当前结果只支持报告从 `medium-proxy` 开始的观察性下降，不支持一般化的难度退化结论。
