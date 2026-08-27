# 架构说明 (v0.1)

## 端到端流水线

`src/tracejudge_hy3/pipeline/runner.py:run_pipeline()` 是唯一的编排入口，固定顺序执行，不使用多 Agent 框架：

1. **生成**：`provider.generate_solution(problem)` 返回 `SolutionTrace`（Mock 或 Hy3）。
2. **静态分析**：`static_analysis.ast_analyzer.analyze_code()` 基于 `ast` 提取 `StaticEvidence`。
3. **执行**：`SandboxBackend.run()` 在 Docker 或 TrustedLocal 中运行可见/隐藏/挑战测试，父进程为每个用例创建新子进程并强制超时，得到包含有界 stdout/stderr 与退出码的 `ExecutionSummary`。
4. **四层评估**：
   - `evaluator/rule_based.py`：纯规则、不依赖 LLM 的对齐检查（空输入声明、集合声明、单次遍历声明、复杂度声明、执行失败归因）。
   - `evaluator/hy3_judge.py`：调用 `provider.evaluate_process()` 获取 LLM 判断（Mock 模式下是一个独立的启发式模拟）。
   - `evaluator/alignment.py`：合并两者——`functional_correct` 始终来自执行结果（不采信任何一方的自称）；规则命中时优先采用规则的 layer/step/error_type，LLM 的不同意见保留在 `explanation`/`secondary_error_types` 中，而不是被丢弃。
5. **反例生成**：仅当 `process_assessment.error_type is not None` 时才触发；优先复用与 `violated_requirement` 相关的 challenge/hidden 失败结果，其次从相关测试的参数形状派生边界候选并差分执行，找到后对列表参数做最小化。不会用只关联其他需求条款的失败来确认当前判断。
6. **错误证书**：`evaluator/evidence.py` 根据"是否有相关可执行反例 / 是否有已执行且关联同一需求条款的隐藏或挑战测试证据 / 是否只有规则命中 / 是否只有 LLM 意见"决定新问题的 `verdict`。首次正确运行无证书；只有显式携带既有证书进行复核，且完整执行证据已排除原问题时，才转移为 `cleared`。

## HumanEval+ 阶段二独立执行边界

`src/tracejudge_hy3/evalplus/runner.py:run_evalplus_experiment()` 是一条与上述全链路完全独立的编排入口。它不导入 Provider，不调用 `run_pipeline()`，不进行 AST/四层判断、LLM Judge、反例生成或参考实现差分执行。固定流程为：

1. `exporter.py` 在任何输出目录或 Docker 进程创建前，完整校验阶段一 manifest/summary/responses、数据集 manifest 和相邻公开 `problems.jsonl`；默认 `--selection-policy all` 要求数据集中每题都有唯一历史 `success`，也支持 `--selection-policy phase1-success-only --min-success-count N` 只导出至少 N 道成功题目。
2. `runner.py` 将最小 `task_id` / `solution` samples 原子写入私有运行目录，记录来源记录/代码哈希，并同时绑定来源题数、成功导出数、解析/Provider 排除数、筛选策略和阈值；这些字段均进入组合续跑指纹。
3. `docker_runner.py` 先校验固定镜像 digest、linux/amd64 平台、镜像内 EvalPlus package `0.4.0.dev2` 与源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`、Python/HumanEval+ release 身份和输入题号对应的公开 prompt/entry-point 身份，再为每题启动一次受限容器。
4. `container_entrypoint.py` 只在容器内加载官方 EvalPlus-native release 数据，从 164 题中生成当前单题的 evaluation-only override，然后调用固定参数的官方 `evalplus.evaluate`。
5. task 只读挂载单题 control，并只把两个预创建宿主文件作为精确 RW bind；宿主等待容器完全退出后才验证/读取结果，清理容器后复制为新的私有 inode，不把仓库或宿主可写目录交给候选。
6. `parser.py` 严格绑定该固定 EvalPlus commit 实际产生的 raw schema；候选代码仅保存 SHA256，失败输入只计数，实值仅存在权限 `0600` 的原始 bundle。
7. 主调度器逐题保存 raw/safe checkpoint；完成题可安全跳过，任何来源、代码、镜像、官方数据身份、执行参数或实现指纹变化都拒绝续跑。

Mock executor 只验证这条输入与产物链；它不调用 `run_task()`，不启动 Docker，并将所有题显式记为 `mocked` 而非通过、失败或基础设施错误。

## 为什么规则证据优先于 LLM 判断

设计文档的问题四明确指出，单纯依赖 LLM-as-judge 会产生误报、无法复现的缺陷描述、结果不稳定等问题。v0.1 的应对方式是：

- 规则命中的判断（如"reasoning 声称处理空输入但代码没有对应分支"）完全基于可复算的 AST 证据，不依赖 LLM 的自由文本理解；
- `functional_correct` 永远来自沙盒执行结果，任何一方的"我认为对/错"都不能覆盖它；
- 只有当反例被沙盒实际执行复现，或已有与当前 `violated_requirement` 相关的隐藏/挑战测试独立复现违反需求的行为时，才升级为 `confirmed_bug`；仅有规则证据但没有独立反例时是 `strongly_supported`；只有 LLM 一家之言时是 `unverified_suspicion`。

这是 v0.1 里"降低 LLM-as-judge 误报"的具体实现，而不是完整的对照/消融实验（那部分留给后续版本，见 `IMPLEMENTATION_STATUS.md`）。

## 模块边界

| 模块 | 职责 | 不负责 |
|---|---|---|
| `schemas/` | 数据契约 | 业务逻辑 |
| `providers/` | 生成解答 + LLM 过程判断 | 静态分析、执行、聚合 |
| `static_analysis/` | AST 结构化证据 | 语义正确性判断 |
| `sandbox/` | 隔离执行、结果结构化 | 测试用例设计 |
| `evaluator/` | 四层对齐判断 + 证书聚合 | 反例搜索本身 |
| `counterexample/` | 差分执行、最小化 | 判断"是否算错误" |
| `pipeline/` | 固定编排顺序 | 具体算法细节 |
| `evalplus/` | 阶段一严格导出、官方 EvalPlus 容器执行、脱敏与续跑 | Provider/LLM、宿主执行、四层评估 |
| `reporting/` | 指标计算、结果序列化 | 数据采集 |
