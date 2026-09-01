# 阶段三研究协议与门槛状态

版本：Gate A–F 已完成，阶段四公开发布已补充 / 2026-09-01

本文冻结阶段三的输入、输出、隐私、配对、公开证书、标注、续跑和统计契约。它不是阶段三实验报告；Gate F 冻结源仍是 Git-ignored 产物 `phase3_report_primary_round1_v1`，阶段四 Gate C 已将其 Markdown 以相同 SHA256 发布为受 Git 跟踪的 `docs/releases/phase4/phase3_research_report_public_v1.md`。完整自然 + 反事实研究集已冻结为 57 条，Gate C 已通过，Gate D 三等级公开工程证书已发布且 confirmed 证书完成独立重放。Gate E1 盲法标注包与 Gate E2 单人首轮 57 条主标注已正式冻结；Gate E3 的真实 Hy3 运行已形成完整 285 配对，其中 283 条有效 judgment、2 条 Provider 失败。Gate E4 正式统计与 Gate F 脱敏研究报告均已发布，Gate A–F 已完成。

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
| 五方法配对 | 正式 `phase3_hy3_57x5_v1` 已完成 57 × 5 = 285 对；283 条有效、2 条 Provider 失败，失败保留分母 | 无需重跑或 resume；E4 已精确绑定现有 manifest/results/index |
| AST / 四层对齐 | 已进入正式 `four_layer_ast` / `full_tracejudge` 配对方法并完成 E3/E4 | 规则覆盖仍有限，现有证据不能归因 AST 或其他组件的因果效果 |
| 公开动态反例 | 已固定 challenge 优先、最多 32 个确定性探针、最多 16 次列表最小化和精确白名单执行策略 | Gate D 已完成一个 confirmed 证书独立 replay；仍不支持任意外部代码或 HumanEval+ 候选 |
| 错误证书 | 三等级生产器、原子 writer、正式证书 manifest 和 confirmed 独立 replay 均完成 | 工程 Fixture 只验证链路，不是五方法有效性结果 |
| 人工标注 | 冻结指南/协议、完整材料再绑定、固定 seed opaque ID、packet/identity map 分离与 `0700/0600` 原子 writer 已实现；正式 57 条盲法 packet 和单人首轮主标注已冻结 | 尚无第二标注者或重测轮次，`agreement_kind=not_computed` |
| 统计 | E4 已实现全分母指标、Wilson 区间、exact McNemar、父题聚类 bootstrap、Holm、严格输入绑定与聚合 writer；正式产物已冻结 | 单标注者一致性仍为 `not_computed` |
| 脱敏报告 | Gate F 已实现哈希绑定、11/11 统计谬误扫描、Material Passport、公开证书 Demo 与原子 writer；正式报告已发布，阶段四已增加受 Git 跟踪的逐字节公开副本 | 验证状态为 `ANALYZED / CAUTION / CANNOT_VERIFY`，公开发布和证书 receipt 不表示已重跑外部 Hy3 或证明方法等效 |
| 隐私 | 公共产物敏感键与 canary fail-closed 检查已实现 | 后续每个 writer 仍须强制调用并做端到端 canary 测试 |
| Resume | 严格 identity、逐 invocation 私有 raw/公开结果、中断识别和 `reused` 精确行哈希已实现；E4 会解析复用链到原始终态，但仍保留最终 `reused` 数量 | 正式 E3 已完成，不应再次 resume 或重跑 |

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

### 7.1 Gate E4 统计口径

- 主二元终点为 `has_error` 是否与冻结人工标签一致；`unverified_suspicion` 对“是否有错”仍算正类，但不等同于已确认错误证书。
- `provider_error`、`parse_error`、`ast_error`、公开执行超时、基础设施错误和 `skipped` 均在 57 × 5 全分母中计为错误并按状态另报；`reused` 必须沿精确旧行 SHA256 解析到原始终态，不能被当成新 judgment。
- 每种方法按全部、42 条自然、15 条反事实分别报告原始分子/分母、judgment 可用率、主准确率、过程/推理/计划代码一致性、首错层/步骤/错误类型准确率及 95% Wilson 区间。有效结果的混淆矩阵单列，不能取代全分母指标。
- 自然轨迹只做两个预注册比较：完整 TraceJudge 分别对 Test-only 和 Direct LLM Judge。报告 `n01`（基线错、完整方法对）与 `n10`（基线对、完整方法错）、双侧精确 McNemar p 值，并对两个 p 值做 Holm 校正。
- 反事实轨迹对同两项比较报告准确率差；以 `parent_trace_id` 为聚类单位，固定 10,000 次、seed `20260828`，用 type-7 线性插值 percentile 规则给出 95% 区间。只有 3 个父题聚类，区间必须标注不稳定。
- 所有结果仅称探索性证据。单标注者单轮次的 agreement 保持 `not_computed`；不显著不表示等效，关联也不建立因果。

### 7.2 Gate F 解读口径

- 主比较以原始分子/分母、配对差值和预注册区间为主；不以单一总体准确率排名替代自然/反事实分层。
- E4 的反事实单方法 Wilson 区间未建模同父题相关性，Gate F 只报原始比例，推断仅使用父题 cluster bootstrap。
- Test-only 不输出过程、推理和计划—代码字段；其对应 0 分子为结构性不适用，报告写作 N/A，不解释为能力为零。
- 强制检查 Simpson、ecological、Berkson、collider、base-rate、regression-to-mean、survivorship、look-elsewhere、forking paths、correlation/causation 和 reverse causality 共 11 类统计谬误。
- 当前验证状态为 `ANALYZED`、总体置信为 `CAUTION`、复现判定为 `CANNOT_VERIFY`：输入已精确哈希绑定，但 Gate F 不重跑外部 Hy3。阶段四已单独持久化公开证书 replay receipt；它只复核一个公开 Fixture，不改变 Hy3 主实验的复现判定。

## 8. 最小目录和 CLI 边界

Gate A–F 当前实现：

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
├── report.py
├── statistics.py
└── runner.py
tests/
├── test_phase3_cohort.py
├── test_phase3_counterfactual.py
├── test_phase3_contracts.py
├── test_phase3_parser.py
├── test_phase3_privacy.py
├── test_phase3_report.py
├── test_phase3_statistics.py
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

`phase3 preflight` 与 `phase3 freeze` 处理自然轨迹；四个 `phase3 counterfactual-*` 命令处理精确白名单公开证据和 overlay。`phase3 paired-preflight` 是 Gate C 只读入口。Gate D 的 `certificate-preflight`、`certificate-generate` 和 `replay` 已完成正式工程验收。Gate E1 的 `annotation-packet-preflight` / `annotation-packet-export` 已完成正式 57 条私有 packet 导出。Gate E2 的 `annotation-labels-check`、`annotation-labels-freeze-preflight` 和 `annotation-labels-freeze` 已产生单人首轮 57 条正式冻结标签。Gate E3 的 `evaluate-preflight` 与显式授权 `evaluate` 已完成正式运行。Gate E4 正式聚合统计已发布。Gate F 的 `report-preflight` 只读验证脱敏报告身份且不展示方法成绩；`report` 已原子发布 Markdown、validation、公开证书 Demo 和重放命令。正式 Gate F 报告已通过哈希、权限和脱敏验收；阶段四 Gate C 只发布同字节副本和审计说明，不覆盖 Gate F 产物。

E3 正式 run `phase3_hy3_57x5_v1` 的 run manifest / results / index SHA256 分别为 `685b25af287bfc973c5000573eac0cf4ff505f91d95fff2faa403f69626f1edc` / `332932e949281c84402046dbd25e0110fb7a7e7e224c71b17487226fa1098999` / `b1a6c6a61a4439d3e667ebd52ddba8cba98f8ee196c1cac8dce200f38c857247`；完整配对 285，`valid_judgment=283`、`provider_error=2`、恢复复用 0。

E4 正式统计 `phase3_stats_primary_round1_v1` 的 manifest / report SHA256 分别为 `7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8` / `972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`；仅包含聚合统计，不包含逐轨迹标签、预测或 Provider raw。

Gate C 正式只读验收已通过：自然 42 + 反事实 15 = 57 条、五方法、完整配对 285；方法规格 SHA256 `4b8684852125ad3059b5001951479a2f164c7089eb64ff10cbdafafc39c534ff`，Prompt bundle SHA256 `c8d6c2c0f6bb1207af987746d912868bd102f90b334f5425528cbda5be9dd366`，输出 schema SHA256 `96da92777ee89bb69a65c61f4bdc9fc9e7cb7ac1ba94a52400f79ca1130821f3`。方法规格哈希绑定 `mock / deterministic-phase3-mock-v1 / temperature=0 / timeout=120s`，只用于接口身份验收；正式 Hy3 Provider、模型、参数与预算必须另行冻结并授权。

## 9. 门槛 A–F 的退出条件

1. Gate A：契约、隐私边界、状态矩阵、目录和测试计划通过普通测试。
2. Gate B：42 条自然轨迹及拟纳入反事实的顺序、哈希、来源核算与公开 manifest 冻结。
3. Gate C：五方法统一读取同一 manifest；Mock 覆盖有效/失败/中断/resume，真实 Hy3 另行授权。
4. Gate D：公开反例可在受限环境重放，三等级证书端到端通过公开 Fixture。
5. Gate E：标注协议先冻结，再运行方法和 exact McNemar/区间；结果仅称探索性证据。
6. Gate F：报告原始数量、全部失败、限制和脱敏真实证书；Demo 只展示公开证据。

每个门槛完成后停止，提交证据并等待是否进入下一门槛。

当前门槛状态：Gate A–F 已完成。正式 Gate F manifest / Markdown / validation SHA256 分别为 `0b8285ec04344e29670d752a37c4d5ecb41ea07d5dfc18a5715b56de3e800b06` / `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725` / `702bf96be5d0911088dfea5cb95562d6b8e25d147d972c78b0b6870cecbae113`。两条 Provider 失败仍保留在 285 全分母；Gate F 未重跑 E3。阶段四公开 receipt 后续完成一个公开证书 replay，但没有重跑 Hy3，因此复现判定保持 `CANNOT_VERIFY`。
