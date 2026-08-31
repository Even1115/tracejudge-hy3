# 阶段三研究协议与门槛状态

版本：Gate A–D 已通过，Gate E1 packet 与 E2 主标注已冻结，Gate E3 执行入口已实现 / 2026-08-31

本文冻结阶段三的输入、输出、隐私、配对、公开证书、标注和续跑契约。它不是阶段三实验报告；完整自然 + 反事实研究集已冻结为 57 条，Gate C 已通过，Gate D 三等级公开工程证书已发布且 confirmed 证书完成独立重放。Gate E1 盲法标注包与 Gate E2 单人首轮 57 条主标注已正式冻结，Gate E3 只读预检/显式 Hy3 执行入口已实现；但真实五方法结果与配对统计均未产生，研究假设尚未得到验证。

## 1. 已核验的上游锚点

- 阶段一正式 run：`phase1_20260826T130038779522Z_5f55a45bb5e5`；来源 45，`success=42`、`parse_error=0`、`provider_error=3`。
- 阶段二正式 run：`phase2_20260827T081939637435Z_3c366f64fc19`；实际执行 42，Base `41/42`，Base+Extra `40/42`，timeout、基础设施错误和容器清理错误均为 0。
- 阶段三只消费阶段一成功轨迹与阶段二 `results.jsonl` 脱敏行；45 条来源的最终状态仍完整进入来源分母。
- 上述结果是固定 research-natural 来源、每题单候选的功能证据，不是完整 164 题成绩、标准 pass@k 或模型总体能力结论。

Gate B 正式自然轨迹已原子冻结到 `artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json`：来源 45、自然轨迹 42、parse 0、Provider 3，SHA256 `a4116a7ddb7ac910b79bd52e9530db79dd0f05c9edee8ecd947fc78c35c03692`。独立只读验收确认字节哈希与 CLI 一致、目录/文件权限为 `0700/0600`、目录只含 manifest、42 条轨迹唯一且顺序精确、45 条来源唯一、42 条成功均有纳入引用、阶段一/二证据绑定同一 `problem_id/code_sha256`、五方法 ID 顺序正确，敏感键与候选代码正文字段扫描通过。该产物只冻结自然轨迹。

反事实源为 `data/phase3/public_counterfactuals_v1.json`：3 个公开自建父 Fixture，五类各 3 条，共 15 条，固定 type-major 顺序；精确源 SHA256 为 `a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df`。正式证据 run `phase3_cf_public_15_v1` 得到 `6 pass / 9 fail`、0 超时、0 基础设施错误、0 预期偏差，results SHA256 为 `19a138ecc2ce784b940e88e085a85ddddf92a564be7235bbd5a3e97bb39d2776`。最终 overlay `phase3_cohort_42_plus_15_v1` 冻结为 42 + 15 = 57 条，manifest SHA256 为 `3290221625d687e6d7412a0544247dc81a34857b114a545458b93cc04e35d255`；3 个父 Fixture 不进入分母。Gate B 已退出。

## 2. 能力矩阵

| 能力 | 当前状态 | 正式研究阻塞项 |
| --- | --- | --- |
| 阶段一/二身份与逐行引用 | 正式 manifest 已绑定阶段一/二 bundle 与精确 JSONL 行 SHA256 | 反事实走独立公开 Fixture 证据，不读取 HumanEval+ 私有文件 |
| 自然轨迹 | 42 条正式轨迹、45 条来源核算和五方法 ID 已冻结并通过独立验收 | 无；该子项完成 |
| 反事实轨迹 | 正式证据 run 与 15 条 overlay 已冻结，与 42 条自然轨迹构成 57 条顺序 | 无；Gate B 完成 |
| 五方法配对 | 固定可见/禁用输入、版本化 Prompt、严格解析、成本/失败记账、trace-major writer 和 Mock 中断/resume 已实现；E3 只读预检与显式 Hy3 入口已绑定 57 × 5 = 285 对、228 个 Provider 对及最大 456 次调用 | 尚无真实 Hy3 结果；必须先通过正式只读预检并显式授权 |
| AST / 四层对齐 | 现有 MVP 可复用 | 规则覆盖有限，尚未适配冻结轨迹输入与统一结果 schema |
| 公开动态反例 | 已固定 challenge 优先、最多 32 个确定性探针、最多 16 次列表最小化和精确白名单执行策略 | Gate D 已完成一个 confirmed 证书独立 replay；仍不支持任意外部代码或 HumanEval+ 候选 |
| 错误证书 | 三等级生产器、原子 writer、正式证书 manifest 和 confirmed 独立 replay 均完成 | 工程 Fixture 只验证链路，不是五方法有效性结果 |
| 人工标注 | 冻结指南/协议、完整材料再绑定、固定 seed opaque ID、packet/identity map 分离与 `0700/0600` 原子 writer 已实现；正式 57 条盲法 packet 和单人首轮主标注已冻结 | 尚无第二标注者或重测轮次，`agreement_kind=not_computed` |
| 统计 | 现有纯函数指标可复用 | exact McNemar、配对区间、bootstrap、Holm 与失败分母尚未实现 |
| 隐私 | 公共产物敏感键与 canary fail-closed 检查已实现 | 后续每个 writer 仍须强制调用并做端到端 canary 测试 |
| Resume | 严格 identity、逐 invocation 私有 raw/公开结果、中断识别和 `reused` 精确行哈希已实现并用 Mock 验证；E3 额外绑定自然 manifest、方法材料、Provider 公开配置和私有标签三个哈希 | 真实 Hy3 续跑尚未授权或执行 |

## 3. 冻结研究对象

### 3.1 自然轨迹

预注册默认策略为 `all_phase1_successes`：纳入阶段一正式 run 的全部 42 条成功轨迹。这样不依据阶段二通过状态、完整方法预测或人工标签做后验筛选；3 条 Provider 失败继续保留在 45 条来源核算中，但因没有完整结构化轨迹，不伪造成可评估自然轨迹。

每条自然轨迹必须同时绑定：

- 精确阶段一 bundle 身份、响应行号、响应行 SHA256、`invocation_id`；
- 公开题面、完整结构化轨迹、结构化说明和候选代码 SHA256；
- 精确阶段二 bundle 身份、安全结果行号与行 SHA256；
- 安全结果中的 `problem_id` 和 `code_sha256`。

该策略已由 Gate B 正式命令冻结。若上游字节或核算发生变化，后续命令必须停止，不得自动改为较小样本。

### 3.2 反事实轨迹

反事实只允许一次明确修改，并保存 parent 与修改前后哈希。仅说明发生变化且代码字节完全相同时，才可用 `reuse_same_code` 复用功能证据。任何代码变化必须使用 `independent_evalplus` 或 `independent_public_fixture` 取得与变体 `code_sha256` 对应的新证据；没有独立证据的变体不得进入五方法主配对集。

本轮固定选择公开自建 Fixture 路径，不实现或混用 HumanEval+ “冻结候选集合执行”扩展：

- 父 Fixture：`safe_mean`、`clamp`、`deduplicate_preserve_order`，均为 MIT 许可的公开自建题面和 visible/challenge 测试；父版本只提供派生与同代码证据，不进入研究轨迹分母；
- 顺序：`reasoning_swap` → `code_defect` → `boundary_deletion` → `shortcut` → `equivalent_implementation`，每类依父 Fixture 固定顺序各 3 条；
- 单因素：reasoning 只改结构化说明并保持代码字节；其余四类只改代码并保持完整说明字节等价；每个改码变体代码唯一；
- 证据：执行 3 个父代码 + 12 个改码主体。reasoning 复用父代码的同一逐行证据；其余变体的 `execution_subject_id` 必须等于自身 `trace_id`；
- 退出：所有 15 个执行主体均无 timeout/基础设施错误，实际状态与预注册预期一致，随后 overlay 才能冻结 42 + 15 = 57 条五方法配对顺序。

## 4. 五种方法的固定输入边界

| 方法 | 可见输入 | AST | 公开动态证据 | LLM 结构化解析 |
| --- | --- | --- | --- | --- |
| Test-only | 阶段二/公开 Fixture 脱敏功能证据 | 否 | 否 | 不适用 |
| Direct LLM Judge | 公开题面、结构化轨迹、代码、脱敏功能证据 | 否 | 否 | 严格 JSON Schema，最多一次修复 |
| 四层结构化 Judge | 同上 | 否 | 否 | 严格 JSON Schema，最多一次修复 |
| 四层对齐 + AST | 同上，加冻结 AST 证据 | 是 | 否 | 严格 JSON Schema，最多一次修复 |
| 完整 TraceJudge | 同上，加冻结 AST 与公开动态证据 | 是 | 是 | 严格 JSON Schema，最多一次修复 |

所有方法一律禁用 `canonical_solution`、官方测试输入、官方失败输入、EvalPlus raw 与凭据。Prompt 版本、Prompt 哈希、输出 schema 哈希、模型、参数、超时和成本必须在运行前冻结。禁止正则局部抽取后冒充有效 judgment。

最终 `PairedEvaluationIndex` 必须按 trace-major 顺序包含“全部冻结轨迹 × 五种方法”的完整笛卡尔积。`provider_error`、`parse_error`、`ast_error`、`public_execution_timeout`、`infrastructure_error`、`skipped` 和 `reused` 都是保留分母的显式状态，不能通过漏行删除。

## 5. 错误证书等级

- `confirmed_bug`：必须带已在受限环境验证的公开反例及 replay 命令。
- `strongly_supported`：没有可执行反例，但至少有可公开复算的静态证据；不得携带 replayable counterexample。
- `unverified_suspicion`：公开证据不足；不得携带反例、replay 命令或伪称可复现的证据。
- 无错误证书：方法判断没有错误时不生成证书。

阶段二隐藏功能失败只可作为脱敏状态证据，不能单独升级为公开可重放的 `confirmed_bug`。Demo 不得展示 HumanEval+ 隐藏输入。

## 6. 公开与私有产物边界

允许公开：公开题面/需求、经许可证与脱敏审核的结构化轨迹和候选代码、AST 摘要、阶段二安全状态、哈希、公开 Fixture、公开反例、证书及统计聚合。

必须留在 Git-ignored 受限目录：Provider raw、修复轮 raw、EvalPlus `samples.jsonl`、`evalplus_raw_results.json`、官方失败输入、canonical solution、私有执行日志和任何凭据。

公共 writer 必须先执行递归敏感键与 canary 检查；错误消息只报告字段路径，不回显 canary 或秘密值。哈希使用 UTF-8、key 排序、紧凑 JSON、禁止 NaN 的确定性编码；JSONL 行身份包含末尾 LF。

## 7. Resume identity

阶段三续跑至少精确匹配：冻结 manifest、轨迹顺序、方法规格、Prompt bundle、输出 schema、实现、Git commit/branch/工作树指纹、Python 与直接依赖、AST 实现、公开证据策略、标注协议和随机种子。

clean 工作树不保存伪造指纹；dirty 工作树必须保存非空指纹。恢复时只允许把已完成的精确结果行记为 `reused`，并引用原行 SHA256；不得自动重试失败事件，也不得把中断或基础设施失败改写成模型/候选错误。

## 8. 最小目录和 CLI 边界

Gate A–E3 当前实现：

```text
src/tracejudge_hy3/phase3/
├── __init__.py
├── cohort.py
├── counterfactual.py
├── contracts.py
├── parser.py
├── privacy.py
├── materials.py
├── annotations.py
├── labels.py
├── execution.py
└── runner.py
tests/
├── test_phase3_cohort.py
├── test_phase3_counterfactual.py
├── test_phase3_contracts.py
├── test_phase3_parser.py
├── test_phase3_privacy.py
└── test_phase3_runner.py
```

当前 Gate D 与后续门槛的最小结构：

```text
src/tracejudge_hy3/phase3/
├── public_evidence.py    # Gate D：公开探针与受限执行
├── replay.py             # Gate D：只消费公开证书
├── materials.py          # Gate E：只重建白名单方法/标注材料
├── annotations.py        # Gate E：冻结协议与盲法 packet/identity map
├── labels.py             # Gate E：进度检查、身份回连与人工标签冻结
├── execution.py          # Gate E：正式 Hy3 只读预检与显式配对运行
├── statistics.py         # Gate E：配对统计
└── report.py             # Gate F：脱敏报告
```

`phase3 preflight` 与 `phase3 freeze` 处理自然轨迹；四个 `phase3 counterfactual-*` 命令处理精确白名单公开证据和 overlay。`phase3 paired-preflight` 是 Gate C 只读入口。Gate D 的 `certificate-preflight`、`certificate-generate` 和 `replay` 已完成正式工程验收。Gate E1 的 `annotation-packet-preflight` / `annotation-packet-export` 已完成正式 57 条私有 packet 导出。Gate E2 的 `annotation-labels-check`、`annotation-labels-freeze-preflight` 和 `annotation-labels-freeze` 已产生单人首轮 57 条正式冻结标签。Gate E3 的 `evaluate-preflight` 不写入或连接 Provider；`evaluate` 必须使用 `hy3`、显式传入 `--confirm-real-provider` 并绑定同一 resume identity。真实五方法运行尚未执行，统计与 Gate F `phase3 report` 尚未注册。

Gate C 正式只读验收已通过：自然 42 + 反事实 15 = 57 条、五方法、完整配对 285；方法规格 SHA256 `4b8684852125ad3059b5001951479a2f164c7089eb64ff10cbdafafc39c534ff`，Prompt bundle SHA256 `c8d6c2c0f6bb1207af987746d912868bd102f90b334f5425528cbda5be9dd366`，输出 schema SHA256 `96da92777ee89bb69a65c61f4bdc9fc9e7cb7ac1ba94a52400f79ca1130821f3`。方法规格哈希绑定 `mock / deterministic-phase3-mock-v1 / temperature=0 / timeout=120s`，只用于接口身份验收；正式 Hy3 Provider、模型、参数与预算必须另行冻结并授权。

## 9. 门槛 A–F 的退出条件

1. Gate A：契约、隐私边界、状态矩阵、目录和测试计划通过普通测试。
2. Gate B：42 条自然轨迹及拟纳入反事实的顺序、哈希、来源核算与公开 manifest 冻结。
3. Gate C：五方法统一读取同一 manifest；Mock 覆盖有效/失败/中断/resume，真实 Hy3 另行授权。
4. Gate D：公开反例可在受限环境重放，三等级证书端到端通过公开 Fixture。
5. Gate E：标注协议先冻结，再运行方法和 exact McNemar/区间；结果仅称探索性证据。
6. Gate F：报告原始数量、全部失败、限制和脱敏真实证书；Demo 只展示公开证据。

每个门槛完成后停止，提交证据并等待是否进入下一门槛。

当前门槛状态：Gate A–D 已退出；Gate E 已进入。E1 正式私有主标注包和 E2 单人首轮 57 条人工标签已冻结；E3 正式执行入口已实现但尚未运行真实 Hy3。下一阶段必须先在 clean worktree 上通过 `evaluate-preflight`，再单独授权真实 Provider 运行。
