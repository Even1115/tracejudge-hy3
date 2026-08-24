# 实现状态 (v0.1)

本文档如实区分"已经实现并通过测试"和"未来计划"，不把计划写成已完成。

## 已完成

- Pydantic v2 严格数据模型（`ProblemSpec` / `RequirementItem` / `TestCase` / `SolutionTrace` / `ImplementationStep` / `TestExecutionResult` / `StaticEvidence` / `ExecutionSummary` / `ProcessAssessment` / `ErrorCertificate` / `Counterexample`），`TestCase.args`/`kwargs` 使用 `Field(default_factory=...)`，无跨实例共享可变默认值问题。
- 确定性 Mock Provider：`safe_mean` 的 correct/faulty 两个手工离线 Fixture，`deduplicate_preserve_order`/`clamp` 各一个正确 Fixture；它们不是真实模型结果。现有完整评估链路对未覆盖题目仍可退化为基于参考代码的通用 Fallback，但该 Fallback 不具备仓库 Fixture 的可信来源，不会在未 opt-in 时通过 `TrustedLocalSandbox` 执行。**阶段一基线入口禁止这个 Fallback**：它只用公开 Prompt 视图匹配 Fixture，未知题或公开题面已改的内置题返回 `provider_error`，不会用 `reference_code` 构造解答或写入产物。
- Mock Provider 的 `evaluate_process()` 是一个独立于 `evaluator/rule_based.py` 的启发式实现，用来模拟"LLM 判断"这一路信号。
- Hy3 OpenAI-compatible Provider：无硬编码端点/模型/密钥，配置缺失时抛出 `ProviderAuthError`，支持超时、可配置的有限重试、JSON Schema 与步骤/需求 ID 上下文校验、修复重试、耗时日志，且日志不暴露密钥片段。修复轮只回传凭据脱敏后的旧输出，Pydantic 错误摘要不含 `input_value`。阶段一调用另外保留最后一份脱敏 `raw_output`、解析状态、尝试次数和分类错误，并仅对外暴露可复现的非敏感生成配置（endpoint 只保留剔除 userinfo/query/fragment 后的指纹）。普通自动测试使用替身，不依赖真实 API。
- 阶段一基线生成（`baseline/runner.py`）：只调用 Solver，不导入或执行候选代码，不向 Solver 发送或执行隐藏/challenge 测试，也不进入四层评估。每次新运行自动创建唯一 `run_id` 和 `<output-dir>/<run_id>/{manifest.json,responses.jsonl,summary.json}`；结果以 UTF-8 JSONL 逐题通过同目录临时文件 + 原子替换持久化。单题失败结构化记录为 `parse_error` 或 `provider_error` 并继续批次；续跑校验数据集 SHA256、数据集 provenance/实验标签（使用 manifest 时）、Provider 公开配置、Git commit/工作树指纹和 Python/直接依赖环境，已成功题记录 `skipped`，失败题重试，中断 invocation 会在续跑时标记为 `interrupted`。
- 阶段一数据隔离：Solver Prompt 只序列化 `problem_id`、`requirement`、`function_signature`、公开需求条款的 ID/内容和可见测试的输入/期望值；不包含 `reference_code`、隐藏测试、challenge 测试、需求的内部验证提示或人工标注。输出中的说明限定为面向用户的需求理解、设计摘要和实现计划，不要求模型私有思维链。
- HumanEval+ 阶段一接入（`dataset/humanevalplus.py`）：离线校验本地固定 revision 的 164 题完整 Hugging Face 快照和受控 SHA256 来源 manifest；只把公开 `task_id` / `prompt` / `entry_point` 转换为 `ProblemSpec`，三类测试数组为空、`reference_code` 使用固定 withheld sentinel、`difficulty` 为 `unknown`。`canonical_solution` / 官方 `test` 只留在被 Git 忽略的本地原始快照，不复制到公开投影、Pilot bundle 或基线产物。
- HumanEval+ 固定 10 题 Pilot：使用 `sha256(seed\0problem_id)-lowest-v1`、种子 `20260824` 仅依据公开题号确定性选择，bundle 包含公开 `problems.jsonl` 和与其哈希绑定的 `dataset_manifest.json`，采用完整目录原子发布。manifest 记录 revision、许可证、原始快照/投影哈希、适配器、题目顺序和选择参数；原始缓存与转换结果均在被 Git 忽略的 `artifacts/`，仓库仅提交受控来源 manifest。
- 阶段一实验元数据与汇总：`raw_output` 与解析后 `solution_trace` 分存；manifest 保存数据集 SHA256/摘要、Git commit/分支/工作区状态、Python/直接依赖版本与 Provider 非敏感配置，不保存 API Key 或请求头。summary 仅统计生成/解析的总数、成功/失败数、解析成功率和平均耗时，不计算功能正确率、错误检测率等尚无证据的指标。内置 3 题运行标记为 `self_constructed_mvp_fixture_pilot`，不是 HumanEval+/MBPP+ 或正式 benchmark。
- AST 静态分析：函数定义与行范围、参数、`if`/`for`/`while` 分类计数、输入相关循环、最大嵌套深度、比较运算符、数据结构使用、函数调用、返回行号、字面量、空输入判断启发式、可疑硬编码启发式、语法错误捕获。已有独立单元测试（`tests/test_ast_analyzer.py`）。
- 沙盒执行：`DockerSandbox`（基础隔离，见 `docs/safety.md`）与 `TrustedLocalSandbox` 均已实现；测试运行器不使用 `eval()`，由父进程为每个用例创建新子进程、强制超时与进程组清理，有界捕获 stdout/stderr，并独立记录输出、异常、超时和退出码。
- 四层评估：`evaluator/rule_based.py`（纯规则）+ `evaluator/hy3_judge.py`（LLM 判断包装）+ `evaluator/alignment.py`（合并，规则证据优先）。
- 反例生成：`counterexample/generator.py`（复用已执行的 challenge/hidden 测试失败 + 有限边界候选）、`differential.py`（参考实现 vs 候选实现差分执行）、`minimizer.py`（列表参数的简单 delta-debugging）。
- 错误证书聚合（`evaluator/evidence.py`）：新疑似问题可产生 `confirmed_bug` / `strongly_supported` / `unverified_suspicion`；普通首次正确运行返回无证书。`cleared` 是显式传入既有证书后、复核通过时的状态转移，不是每次正确运行的默认结果。
- CLI：`doctor` / `demo --mock --case {correct,faulty}` / `dataset convert-humanevalplus` / `dataset sample` / `dataset validate` / `baseline` / `run` / `batch`。`baseline` 提供 `--dataset-manifest`，HumanEval+ 公开投影必须传入匹配的 manifest，且会在 Provider 调用前验证 provenance；`run` / `batch` 明确拒绝该阶段一投影，避免误执行 withheld sentinel 或把空测试当作功能证据。
- 指标函数（10 个，`reporting/metrics.py`），全部为纯函数，缺少人工标注时返回 `not_computable`；`data/demo_annotations.jsonl` 提供 4 条 Fixture 标注用于测试。
- 单元测试覆盖：schema 校验、JSONL 加载、结构化输出解析（含 Markdown 围栏/多余文本容错）、AST 正确代码与语法错误、可见通过/隐藏失败、运行时异常、超时结构、空输入对齐判断（含误报防护）、反例差分验证、错误证书状态聚合、完整 Mock Demo Pipeline、指标函数，以及阶段一 Prompt 不泄露、原始/结构化输出分存、单题失败隔离、断点续跑、manifest 脱敏、JSONL 中断可读、汇总一致性和 Mock 无网络。HumanEval+ 测试另覆盖受控快照校验、答案/官方测试不可达、固定抽样、原子且不可覆盖的 bundle 发布、manifest/provenance 篡改拒绝、baseline 只生成以及 `run`/`batch` 拒绝。

## 部分实现（已知局限）

- **空输入 / 硬编码等启发式**：仅覆盖 `if not x` / `len(x) == 0` 等少数常见写法和关键词匹配（"空列表"/"empty"/"集合"/"set"/"单次遍历" 等），不是通用语义理解；对措辞不同但语义等价的表达可能漏检或误判。
- **沙盒隔离**：`DockerSandbox` 提供的是基础隔离（见 `docs/safety.md`），不是绝对安全；它在 `--rm` 之外对超时/异常路径执行强制容器清理。`TrustedLocalSandbox` 有父进程超时和每例子进程隔离，但没有资源/网络/权限隔离；CLI/流水线默认只允许精确匹配的内置 Mock Fixture，其他代码（包括 Mock fallback）需显式 unsafe opt-in。
- **反例生成**：只基于参数类型做有限的边界候选（空/单元素/重复/零/负数/近似边界值），不是通用属性测试或符号执行；找不到反例时明确返回 `None`，不会把 LLM 猜测升级为 `confirmed_bug`。
- **delta-debugging 最小化**：只处理"第一个长度 >1 的列表参数"，逐元素贪心删除，不是通用 ddmin，也不处理嵌套结构或多个列表参数。
- **规则证据覆盖面**：`evaluator/rule_based.py` 目前只覆盖空输入声明、集合声明、单次遍历声明、复杂度声明、以及执行失败的粗粒度归因（异常类型 → 边界/运行时/超时），不是完整的需求—推理—代码三层通用比对。
- **阶段一数据规模与证据边界**：仓库仍只直接附带 3 道自建工程 Fixture；另已支持从用户本地固定快照生成完整 164 题 HumanEval+ 公开投影和确定性 10 题 Pilot。HumanEval+ 路径只做 Solver 生成与结构化解析，没有执行候选代码或官方测试，因此解析成功不等于解答正确，不能据此报告功能正确率、HumanEval+ 分数、pass@k 或正式 benchmark 结论。
- **Hy3 Provider**：网络可达性、认证和 `extra_body.reasoning_effort` 的兼容性取决于用户配置的具体 OpenAI-compatible 服务。普通测试不做真实网络调用；真实 pilot 的成功与失败应由当次实验产物和报告记录，不作为永久产品状态。

## 尚未实现

- HumanEval+ 快照的项目内自动下载，以及独立的官方 EvalPlus 测试执行、功能评分和大规模评测；
- MBPP+ 接入、筛选与评测；
- 反事实配对挑战集的系统化构造（reasoning/code/shortcut/equivalent/boundary 五类）；
- 人工标注子集（`data/demo_annotations.jsonl` 只是 4 条工程 Fixture，不是研究级标注集）；
- 对照实验（Test-only / Direct Judge / +四层对齐 / +静态分析 / 完整方法）与模块级消融实验；
- 通用属性测试（Hypothesis 等）；
- 多模型 Judge / 多次评估一致性分析；
- Web UI 与结果可视化图表；
- 自动代码修复；
- 完整控制流图、符号执行、mutation testing 框架；
- 多文件/仓库级代码生成任务、多语言执行。
