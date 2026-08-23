# 实现状态 (v0.1)

本文档如实区分"已经实现并通过测试"和"未来计划"，不把计划写成已完成。

## 已完成

- Pydantic v2 严格数据模型（`ProblemSpec` / `RequirementItem` / `TestCase` / `SolutionTrace` / `ImplementationStep` / `TestExecutionResult` / `StaticEvidence` / `ExecutionSummary` / `ProcessAssessment` / `ErrorCertificate` / `Counterexample`），`TestCase.args`/`kwargs` 使用 `Field(default_factory=...)`，无跨实例共享可变默认值问题。
- 确定性 Mock Provider：`safe_mean` 的 correct/faulty 两个真实 Fixture，`deduplicate_preserve_order`/`clamp` 各一个正确 Fixture，未覆盖题目时退化为基于参考代码的通用 Fallback（仍是 schema 合法的完整解答，不是占位字符串）。该 Fallback 不具备仓库 Fixture 的可信来源，不会在未 opt-in 时通过 `TrustedLocalSandbox` 执行。
- Mock Provider 的 `evaluate_process()` 是一个独立于 `evaluator/rule_based.py` 的启发式实现，用来模拟"LLM 判断"这一路信号。
- Hy3 OpenAI-compatible Provider：无硬编码端点/模型/密钥，配置缺失时抛出 `ProviderAuthError`，支持超时、可配置的有限重试、JSON Schema 与步骤/需求 ID 上下文校验、修复重试、耗时日志，且日志不暴露密钥片段。**未接入真实服务测试**（单元测试不调用真实 API，见"尚未实现"）。
- AST 静态分析：函数定义与行范围、参数、`if`/`for`/`while` 分类计数、输入相关循环、最大嵌套深度、比较运算符、数据结构使用、函数调用、返回行号、字面量、空输入判断启发式、可疑硬编码启发式、语法错误捕获。已有独立单元测试（`tests/test_ast_analyzer.py`）。
- 沙盒执行：`DockerSandbox`（基础隔离，见 `docs/safety.md`）与 `TrustedLocalSandbox` 均已实现；测试运行器不使用 `eval()`，由父进程为每个用例创建新子进程、强制超时与进程组清理，有界捕获 stdout/stderr，并独立记录输出、异常、超时和退出码。
- 四层评估：`evaluator/rule_based.py`（纯规则）+ `evaluator/hy3_judge.py`（LLM 判断包装）+ `evaluator/alignment.py`（合并，规则证据优先）。
- 反例生成：`counterexample/generator.py`（复用已执行的 challenge/hidden 测试失败 + 有限边界候选）、`differential.py`（参考实现 vs 候选实现差分执行）、`minimizer.py`（列表参数的简单 delta-debugging）。
- 错误证书聚合（`evaluator/evidence.py`）：新疑似问题可产生 `confirmed_bug` / `strongly_supported` / `unverified_suspicion`；普通首次正确运行返回无证书。`cleared` 是显式传入既有证书后、复核通过时的状态转移，不是每次正确运行的默认结果。
- CLI：`doctor` / `demo --mock --case {correct,faulty}` / `run` / `batch`，均有集成测试通过 Typer `CliRunner` 验证。
- 指标函数（10 个，`reporting/metrics.py`），全部为纯函数，缺少人工标注时返回 `not_computable`；`data/demo_annotations.jsonl` 提供 4 条 Fixture 标注用于测试。
- 单元测试覆盖：schema 校验、JSONL 加载、结构化输出解析（含 Markdown 围栏/多余文本容错）、AST 正确代码与语法错误、可见通过/隐藏失败、运行时异常、超时结构、空输入对齐判断（含误报防护）、反例差分验证、错误证书状态聚合、完整 Mock Demo Pipeline、指标函数。

## 部分实现（已知局限）

- **空输入 / 硬编码等启发式**：仅覆盖 `if not x` / `len(x) == 0` 等少数常见写法和关键词匹配（"空列表"/"empty"/"集合"/"set"/"单次遍历" 等），不是通用语义理解；对措辞不同但语义等价的表达可能漏检或误判。
- **沙盒隔离**：`DockerSandbox` 提供的是基础隔离（见 `docs/safety.md`），不是绝对安全；它在 `--rm` 之外对超时/异常路径执行强制容器清理。`TrustedLocalSandbox` 有父进程超时和每例子进程隔离，但没有资源/网络/权限隔离；CLI/流水线默认只允许精确匹配的内置 Mock Fixture，其他代码（包括 Mock fallback）需显式 unsafe opt-in。
- **反例生成**：只基于参数类型做有限的边界候选（空/单元素/重复/零/负数/近似边界值），不是通用属性测试或符号执行；找不到反例时明确返回 `None`，不会把 LLM 猜测升级为 `confirmed_bug`。
- **delta-debugging 最小化**：只处理"第一个长度 >1 的列表参数"，逐元素贪心删除，不是通用 ddmin，也不处理嵌套结构或多个列表参数。
- **规则证据覆盖面**：`evaluator/rule_based.py` 目前只覆盖空输入声明、集合声明、单次遍历声明、复杂度声明、以及执行失败的粗粒度归因（异常类型 → 边界/运行时/超时），不是完整的需求—推理—代码三层通用比对。
- **Hy3 Provider**：代码路径完整实现，但本次开发环境没有真实 Hy3 Key，因此未做真实网络调用验证；`extra_body.reasoning_effort` 的兼容性未针对具体 Hy3 服务实测。

## 尚未实现

- HumanEval+ / MBPP+ 等公开 benchmark 的自动下载、筛选与大规模评测；
- 反事实配对挑战集的系统化构造（reasoning/code/shortcut/equivalent/boundary 五类）；
- 人工标注子集（`data/demo_annotations.jsonl` 只是 4 条工程 Fixture，不是研究级标注集）；
- 对照实验（Test-only / Direct Judge / +四层对齐 / +静态分析 / 完整方法）与模块级消融实验；
- 通用属性测试（Hypothesis 等）；
- 多模型 Judge / 多次评估一致性分析；
- Web UI 与结果可视化图表；
- 自动代码修复；
- 完整控制流图、符号执行、mutation testing 框架；
- 多文件/仓库级代码生成任务、多语言执行。
