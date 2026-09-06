# 跨数据集共享契约 v1

状态：**FROZEN**  
契约版本：`1`  
实现：`src/tracejudge_hy3/benchmark/contracts.py`

## 目的

该契约是 HumanEval+、MBPP+、LiveCodeBench 与 CodeJudge-Eval 并行接入时唯一共享的数据边界。它不替代各数据集的官方执行器，也不允许把隐藏测试、标准答案或原始失败输入带入 Solver/Judge 可见材料。

## 固定模型

| 模型 | 职责 |
|---|---|
| `BenchmarkDataset` | 固定数据集 revision、来源 manifest 哈希、许可证、适配器版本和能力 |
| `BenchmarkTaskIdentity` | 固定数据集、题号、语言、交互形式和实验用途 |
| `BenchmarkTask` | 仅保存公开题面、公开需求、难度和公开字段哈希 |
| `BenchmarkCandidate` | 绑定候选代码、来源和可选轨迹哈希 |
| `BenchmarkExecutionResult` | 保存官方执行器的脱敏分组状态与失败计数 |
| `BenchmarkJudgeRecord` | 保存统一的功能/过程判断、首错与 taxonomy 映射 |
| `BenchmarkExperimentManifest` | 在运行前冻结题号顺序、Prompt、代码与模型身份 |

## 两个正交维度

题目交互形式与实验用途不可混为一谈：

- `TaskInterface`：`function`、`standard_io`、`repository_patch`；
- `EvaluationMode`：`generation`、`judge_only`。

因此 HumanEval+/MBPP+ 可表示为 `function + generation`，LiveCodeBench 为 `standard_io + generation`，CodeJudge-Eval 为其原题交互形式加 `judge_only`。CodeJudge-Eval 不需要伪装成生成任务，也不得用缺失的推理轨迹报告首错步骤指标。

## 安全与复现不变量

1. 所有模型 `extra="forbid"` 且 `frozen=True`；字段变化必须发布契约 v2。
2. `BenchmarkTask` 不含隐藏测试、标准答案、参考代码和失败输入。
3. 公开题面由 `public_payload_sha256` 绑定；候选代码由 `code_sha256` 绑定。
4. 题号选择是有序集合，由 `selected_task_ids_sha256` 绑定，重复题号非法。
5. 适配器只把官方执行结果归一化为分组状态和计数；原始 evaluator payload 留在数据集专用私有边界。
6. 基础设施失败、未执行与候选失败是三个不同状态，不能合并进模型错误率。
7. `DatasetAdapter` 是所有数据集的最小接口；只有存在官方可执行 harness 的数据集才实现 `ExecutionResultAdapter`。

## v1 并行开发边界

- HumanEval+：已有桥接实现 `benchmark/humanevalplus.py`，作为兼容性参考。
- MBPP+：复用 v1 模型，新增自己的公开投影与 EvalPlus 结果适配器。
- LiveCodeBench：保留官方 stdin/stdout checker，只输出 v1 脱敏结果。
- CodeJudge-Eval：实现 `DatasetAdapter` 和 supplied-candidate loader；不要求实现执行适配器。

任何分支若需要新增公共字段、放宽状态含义或改变哈希算法，应停止合并并提出 v2，而不是在本地悄悄扩展 v1。
