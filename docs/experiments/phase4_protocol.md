# 阶段四：复现加固、脱敏成果发布与项目封版

版本：P0 封版 + P1 Gate D 两标注者一致性分析版 + 完成态裁决记录 / 2026-09-04

阶段四只为既有阶段一至阶段三结果建立复现、备份、公开发布和封版证据，不覆盖或重解释冻结运行。任何新增研究运行必须使用新的 ID、目录、manifest 和报告版本。

P0 Gate A、B、C、E 的仓库内交付已经完成。Gate D/P1 已完成第二标注者的 5 条公开 Fixture 练习校准、正式 15+5 盲化包交付、20 条独立复标、原始回传冻结、聚合一致性分析，并已由两位原始标注者对唯一一条过程细节分歧形成记录在案的共识。练习标签仅用于校准，排除于研究终点。P1 不回写阶段三产物，也不改变 P0 的封版结论。

## 1. Gate B 边界

Gate B 允许：

- 以固定 allowlist 流式计算 Git-ignored 关键产物的大小、mode 和 SHA256；
- 写入 Git-ignored 私有完整 inventory，以及不含私有路径和正文的公开 digest；
- 在恢复目录上逐文件验证大小、mode 和 SHA256；
- 使用 Gate D 已冻结的精确公开 Fixture 和 confirmed 证书执行一个本地 replay；
- 持久化不含反例输入、候选源码、逐轨迹预测或 Provider raw 的公开 receipt。

Gate B 禁止：

- 读取或输出 `.env`、凭据、Authorization、Cookie 或代理信息；
- 解析、打印或复制 Provider raw、EvalPlus raw、官方测试正文或人工标签正文；
- 重跑阶段三 Hy3、重试两条 Provider 失败、调用 Docker 或网络；
- 覆盖已有 inventory、digest 或 receipt。

## 2. Schema 与产物

### 2.1 私有 inventory

`tracejudge_phase4_private_artifact_inventory` schema v1 仅保存：逻辑 artifact ID、仓库相对路径、隐私级别、文件大小、精确 mode 和 SHA256。它同时保存排除采集时间的 `artifact_set_sha256`；因此同一 inventory ID、Git 身份与产物集合会得到相同的集合摘要，而带 `created_at` 的完整 manifest SHA256 仍用于绑定某次正式冻结。writer 不解析文件内容，拒绝绝对路径、路径穿越、符号链接、非普通文件、缺失文件和重复 ID/路径。

默认 inventory ID 为 `phase4_artifact_inventory_v1`，输出位置：

```text
artifacts/experiments/phase4-reproducibility/
└── phase4_artifact_inventory_v1/
    └── manifest.json
```

目录和文件分别为 `0700/0600`，继续受 `.gitignore` 保护。

### 2.2 公开 digest

`tracejudge_phase4_public_artifact_digest` schema v1 仅保存确定性的 `artifact_set_sha256`、私有 inventory 的完整文件哈希、关键公开锚点的逻辑 ID/大小/SHA256、Git 身份和权限警告计数。它不保存私有相对路径、正文、逐轨迹标签、方法预测、Provider raw 或隐藏评测内容。

### 2.3 公开 replay receipt

`tracejudge_phase4_public_replay_receipt` schema v1 绑定证书、证书 manifest、42+15 cohort、自然 manifest、公开源和 replay 实现哈希。receipt 只声明是否重现失败、证据哈希是否一致、执行用例数和安全边界；不保存反例具体输入、候选源码或运行 raw。

本轮首次 receipt 在修改 Phase4 代码前，从 clean commit `1de193266d4be57df412614cbc06f4da0eb5868c` 执行，避免把未提交实现误写入运行身份。

### 2.4 Gate C 脱敏报告

Gate F Markdown 已以相同 SHA256 发布为受 Git 跟踪的 `docs/releases/phase4/phase3_research_report_public_v1.md`，发布说明单独记录隐私审计、公开 receipt、已知展示缺口和 `ANALYZED / CAUTION / CANNOT_VERIFY` 边界。阶段四不修改冻结报告正文。

### 2.5 Gate E 图表、Demo 与封版

`phase4_public_charts_v1` 从冻结 E4 聚合统计生成三张确定性 SVG 和公开 manifest，不包含逐轨迹标签、预测、Provider raw、隐藏评测内容或 trace ID。公开 `safe_mean` Fixture Demo 在 2 分钟时间线内展示评估、反例、证书和 replay。Release 检查单与封版报告记录 P1/v0.2+ 边界及外部发布授权状态。

### 2.6 Gate D/P1 第二标注者准备

`phase4_p1_second_annotator_protocol_v1` 将标注安排、阶段三指南、准入条件、盲法、交付、停止、退出和分析边界冻结为机器可校验协议。安排/Protocol SHA256 分别为 `15a0de0efc0b6695b8021f5912ea6670bfc234f686a89cfcf5d267b53e3d7c6b` / `3f7268eb757f452d3902de3d60274ce2d45fb022ba047e64bfd5e680b044bf6c`。伦理认定已为 `approved / READY`；但完整单次交付记录仍是独立硬门，完成前 `data_collection_allowed=false`，不允许发包或收集答案。

`phase4_p1_public_practice_v1` 使用 5 条新建 MIT 公开 Fixture，包含 2 条无错和 3 条需求/推理/对齐层练习错误。生成器在执行前要求精确源 SHA256，并对阶段三自然 manifest 和 overlay 核对题号、代码哈希与结构化说明哈希零重合。正式 manifest SHA256 为 `cc6beef9b439a42a3011700096a9e8541edad211d5ea1733b47d07c9ad8ce855`，绑定 5 条、15 个公开用例、`READY / pending_completion` 双门状态及 Provider/Docker/网络调用各 0。公开 bundle 只含 `participant/` 练习材料/空模板和 manifest；公开自建 Fixture 的协调者参考不是人类标签，但仍保存于 Git-ignored `0700/0600` 目录，manifest 只保存无答案的逻辑 artifact ID、存储类别和 SHA256，不保存私有路径或正文。

公开单次交付记录 Schema SHA256 为 `3e2cf0921da2bebac52505cc87e503d2f559d889c9672c53a57614116f32fdd9`；Git-ignored 私有记录已回填参与同意/核验时间、负责人授权、五类渠道、期限、练习回传归档 SHA256 和数据处理条款，预检缺失数为 0、`data_collection_allowed=true`。原始收件点未被倒推；`participant_receipt_verified_at` 记录的是协调者根据已完成回传归档及哈希完成事实核验的时间，依据单独保存在受限目录。Schema 本身不含个人信息；实际私有记录明确禁止公开。正式子集 `phase4_p1_formal_subset_v1` 为 15 条自然 + 5 条反事实，反事实覆盖 3 个父题且每父题最多 2 条。私有 manifest / 不含入选 ID 的公开 commitment SHA256 分别为 `03782826f5054238962c5a007116e9df204fc9f69e8dc746925c8bef2ac2082c` / `b5090ad78715857455852e3450fa606f4963ca726a3df91a1b6603d372c491a2`。

5 条练习回传的 Schema、`has_error`、`process_correct`、错误条目首错层分别为 5/5、5/5、5/5、3/3，且隐私/盲法异常数为 0。私有准入记录 `phase4_p1_practice_admission_rater02_v1` SHA256 为 `d7461f2562ae62162733fdde945e28b92b433e96f54de34d8a4e788d9f2fb1ae`；它只保存输入哈希、聚合校准得分、零违规确认与书面准入决定，不复制 rationale 或标签正文。正式 packet 已以 `0700/0600` 权限生成并逐字节重建验证；manifest / participant packet / labels template / coordinator identity map SHA256 分别为 `8297183a615e53f62dff40bed33c3b2d83f3b3ed45ba06b3f8882759f6fcde2f` / `1fe8f5329340153abed64031328918eaee4752cabe803966220f6cfc9604e743` / `8e9b17c298411f8893b8bed20079d0113a5d3fbe4c0ad51cd7e92ad1859503d8` / `dbc5ebe4bd24a1e53c3b437a1af039eb5e5c3f8f8593e1824a1c732d0938a6d5`。

正式 20 条已于 `2026-09-04T07:20:00+08:00` 按时回收并冻结，第二标注者标为有错 6 条、无错 14 条；正式标签 manifest / annotation records SHA256 分别为 `80c583d47b9e428e0148fcf7c556a9d6f4342541eed1d079554e73592b2496cf` / `45e36073ac397ba23fbfbb6101562eb8ec2fe105ad76f2c4d58f0ad62fb72a7b`。两轮使用不同的冻结协议文件，但共同绑定同一份阶段三标注指南和同一 `AnnotationRecord` 标签 Schema；一致性分析分别核对每轮协议哈希，不把二者伪装为同一协议。

聚合一致性产物只含计数、比例和区间，不含 trace ID、逐条标签、rationale 或分歧清单。`has_error` 原始一致率为 20/20（100.0%，Wilson 95% CI 83.9%–100.0%），Cohen's κ=1.000；`plan_code_aligned` 为 19/20、κ=0.875（配对 bootstrap 95% CI 0.579–1.000）；完整七字段记录为 19/20；双方都判错的 6 条中，首错层/步骤/错误类型联合标签为 5/6。零分歧使 `has_error` 的经验 bootstrap κ 区间机械退化为 1–1，不能据此声称总体可靠性必为 1。聚合 manifest / JSON / Markdown SHA256 分别为 `20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76` / `fe9c66d505c0ce472deb652676ac38ea4d6849547323a1e3061ad1d9deea2135` / `0f3134d18a1d3fda1c4235951c442d57651fe587c6201df0549568818f677734`，已从两份冻结源确定性复算验证。

待裁决包 `phase4_p1_adjudication_pending_v1` 只从上述 aggregate-only 产物锁定“20 条中恰有 1 条完整记录分歧”，初始化程序不读取两份逐条标签，也不包含条目身份、原标签、rationale 或裁决结论。其 manifest / pending record / working template / instructions SHA256 分别为 `5dc8e34b1e6842b41db294b035e374afc2df77899433362b8af80b74c0da9009` / `c424d94a843d8048b989ec9980038063e367938e81ad69a9fd914dd28f6598ec` / `5caee8d5e8b04ce3fdfd434417e9dffc438484c60a91c89be7df3dfa0d4d0c2a` / `208e1bb22ab68dbfccbe04996345c1e7b8b2c28e7cdb6e0c727184fed64c1d01`，已通过 Schema、逐文件哈希与 `0700/0600` 权限验证。该包仍保持原始 `pending_human_review` 状态，作为不可变审计起点。

两位原始标注者于 `2026-09-04T11:00:00+08:00` 至 `2026-09-04T14:00:00+08:00` 在保持方法预测盲法的前提下，以 `documented_consensus` 完成四个分歧字段的裁决。完成态另存为 `phase4_p1_adjudication_completed_v1`，其 manifest / decision / report SHA256 为 `6e48963ee7cfe6cda2f113271286612af1640ca1abaf0eaeacedb62de2639287` / `f3f32476283a5c5e4b65af8a6250352b3b2ca60592929feef671fd52ac3f442a` / `13ba1a737fba0e0ab30f065f0447a600c834736cfd9b71fa32fba155d4a7c557`，状态为 `completed_human_consensus`。完成器只读取正式 packet 中的非标签案例材料，未读取两份逐条原标签；Codex 曾向协调者提供技术性初审建议但不是裁决者，其建议在双方独立复核前是否向两人展示未报告。决定正文保持私有，且两份原标签、pending 包、raw agreement 和主实验结果均未覆盖。

完成态之后另行发布 `phase4_p1_post_adjudication_sensitivity_v1` 聚合级 post-hoc 敏感性分析。它保留原始完整七字段 19/20 和 `has_error` 20/20，一方面记录唯一分歧 1/1 已通过共识解决，另一方面明确不生成“裁决后 20/20 一致率”。固定 20 条分母下，`has_error`、`process_correct` 和 `reasoning_correct` 的标签影响为 0；`plan_code_aligned`、首错层、首错步骤、错误类型和完整记录最多受 1 条（5.0 pp）影响，6 条双方判错条件分母上的定位上界为 16.7 pp。由于分析不读取方法预测或逐条原标签，未计算具体方法分数变化。公开 JSON / Markdown SHA256 为 `377725050f8adbb4afe88f0b0e01ae05b4a2bc670c6920034fc8bb5b0472a48b` / `7dd2f1f244c3bd09a2928b61c1ee36cb25e59a88e8631e0be3807d504384866d`。

### 2.7 独立 Judge 稳定性附加实验

`phase4_judge_stability_public4x5_v1` 固定使用同一 `safe_mean` 父题的正常正确、reasoning swap、边界删除和等价实现四个公开案例，各运行 Full TraceJudge 5 次。20 个评审单元使用独立 run ID、目录、manifest 与报告，逐条原子 checkpoint；报告 `has_error`、首错步骤、错误类型及联合标签的一致性，同时单列 Provider/解析/其他失败和实际请求数。它不读取人工标签或隐藏评测正文，且 `main_experiment_merge_allowed=false`，不得回写 57 × 5 主实验。

协议、分析口径、续跑规则和结论边界见 `docs/experiments/phase4_judge_stability_protocol_v1.md`。实现及离线 Mock 验收已经完成；真实 Hy3 的 20 个评审尚未启动，因此这里不报告稳定性数值。

### 2.8 P1 Material Passport

- Source：冻结的 42+15 cohort、正式 20 条私有子集 manifest、公开 commitment、练习回传归档/标签哈希和协调者参考。
- Transform：练习只计算预注册的准入汇总；正式包只在交付硬门通过后重建公开可见材料，并以确定性哈希顺序映射到 `formal_item_001..020`。
- Current stage：正式 20 条复标、原始聚合一致性统计、唯一分歧的两位原始标注者共识记录和首版裁决后敏感性分析均已冻结并验证。
- Outputs：Git-ignored `0700/0600` 原始标签集、aggregate-only 一致性包、不可变 pending 起点和独立 completed 共识包；公开文档包含聚合结果、完成状态和固定分母影响上界。
- Gate：后续精确方法分数分析必须显式选择并版本化标签参考集、方法结果和分母口径，不得覆盖两份原始标签、pending 起点、当前 raw agreement 或 57×5 主结果。

## 3. CLI

所有命令在仓库根目录执行。

只读 inventory 预检：

```bash
tracejudge phase4 artifact-preflight \
  --inventory-id phase4_artifact_inventory_v1
```

正式冻结要求 clean worktree 且私有文件不存在 group/other 权限位：

```bash
tracejudge phase4 artifact-freeze \
  --inventory-id phase4_artifact_inventory_v1 \
  --private-output-dir artifacts/experiments/phase4-reproducibility \
  --public-output-dir docs/releases/phase4
```

恢复验证：

```bash
tracejudge phase4 artifact-verify \
  --manifest artifacts/experiments/phase4-reproducibility/phase4_artifact_inventory_v1/manifest.json \
  --repo-root <恢复后的仓库根目录>
```

公开 receipt 预检会执行一个精确白名单公开用例，但不写文件：

```bash
tracejudge phase4 replay-receipt-preflight \
  --receipt-id phase4_public_replay_receipt_v1
```

正式持久化：

```bash
tracejudge phase4 replay-receipt \
  --receipt-id phase4_public_replay_receipt_v1 \
  --output-dir docs/releases/phase4
```

图表只读预检、发布和逐字节验证分别使用：

```bash
tracejudge phase4 charts-preflight
tracejudge phase4 charts-publish
tracejudge phase4 charts-verify \
  --manifest docs/releases/phase4/charts/phase4_public_charts_v1/manifest.json \
  --manifest-sha256 20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71
```

公开 Fixture Demo 使用：

```bash
tracejudge demo --mock --case faulty --per-test-timeout-seconds 5
```

P1 公开练习包的只读预检、不可覆盖发布和逐字节验证使用：

```bash
tracejudge phase4 p1-practice-preflight
tracejudge phase4 p1-practice-publish
tracejudge phase4 p1-practice-verify \
  --manifest-sha256 cc6beef9b439a42a3011700096a9e8541edad211d5ea1733b47d07c9ad8ce855
```

三个命令只执行精确哈希白名单的自建公开 Fixture，不读取阶段三标签、不调用 Provider/Docker/网络，也不生成正式 20 条复标包。

单次交付记录模板已创建；日常只读检查使用：

```bash
tracejudge phase4 p1-delivery-preflight
```

它只报告状态、缺失项数量和哈希，不回显私有渠道或联系人。记录为 `pending_completion` 时必须继续禁止发包和数据收集。

正式 20 条子集的只读重建和精确验证使用：

```bash
tracejudge phase4 p1-formal-subset-preflight
tracejudge phase4 p1-formal-subset-verify \
  --commitment-sha256 b5090ad78715857455852e3450fa606f4963ca726a3df91a1b6603d372c491a2
```

一次性 `p1-delivery-init` 和 `p1-formal-subset-freeze` 已执行，均拒绝覆盖现有冻结；不要为“刷新”哈希而重复运行或删除既有私有产物。

Judge 稳定性实验先只读预检，核对 4 × 5、20/40 名义/最大请求和 Protocol SHA256；确认 clean commit 后才显式启动真实 Provider：

```bash
tracejudge phase4 stability-preflight \
  --run-id phase4_stability_hy3_public4x5_v1

tracejudge phase4 stability-run \
  --run-id phase4_stability_hy3_public4x5_v1 \
  --confirm-real-provider
```

发生中断时只允许在代码、配置、输入和 Git 身份完全相同的条件下加 `--resume`；否则应改用新 run ID。

练习回传校验完成后，准入记录只冻结一次：

```bash
tracejudge phase4 p1-practice-admission-freeze \
  --completed-labels /path/to/practice_labels_completed.jsonl \
  --returned-archive-sha256 <64-hex-sha256> \
  --public-evidence-rationales-confirmed \
  --coordinator-written-authorization-confirmed
```

现有准入记录已冻结，不得重复执行或覆盖。私有交付记录已通过完整性门禁，下列预检、导出和逐字节验证已按顺序执行：

```bash
tracejudge phase4 p1-formal-packet-preflight
tracejudge phase4 p1-formal-packet-export
tracejudge phase4 p1-formal-packet-verify \
  --manifest-sha256 <formal-packet-manifest-sha256>
```

门禁通过后，对外只发送 `participant/packet.jsonl` 和 `participant/labels_template.jsonl`。`coordinator/identity_map.jsonl` 只由项目协调者保管，不得放入参与者归档。

正式回传必须先把原始归档和解压标签置于同一个 `0700` 受限目录，并将文件权限收紧为 `0600`。协调者确认解压绑定和带时区的实际收件时间后，依次执行：

```bash
tracejudge phase4 p1-formal-labels-preflight \
  --completed-labels /restricted/path/labels_completed.jsonl \
  --returned-archive /restricted/path/labels_returned.7z \
  --received-at 2026-09-04T07:20:00+08:00 \
  --archive-extraction-confirmed
tracejudge phase4 p1-formal-labels-freeze \
  --completed-labels /restricted/path/labels_completed.jsonl \
  --returned-archive /restricted/path/labels_returned.7z \
  --received-at 2026-09-04T07:20:00+08:00 \
  --archive-extraction-confirmed
tracejudge phase4 p1-formal-labels-verify \
  --manifest-sha256 <formal-labels-manifest-sha256>
```

预检要求 20 行全部完成、ID/顺序/固定字段与原模板一致、标签条件约束有效、收件时间不晚于交付记录中的正式截止时间，并把原始归档、完成标签、正式 packet manifest、交付记录和 coordinator identity map 精确绑定。冻结操作不可覆盖，私有保存原始 archive、原字节完成标签和回连后的 20 条 annotation records；`agreement_kind` 在一致性分析前保持 `not_computed`。

两位标注者一致性分析按“只读预检 → 一次性冻结 → 从源复算验证”执行：

```bash
tracejudge phase4 p1-agreement-preflight
tracejudge phase4 p1-agreement-publish
tracejudge phase4 p1-agreement-verify \
  --manifest-sha256 20d11548ed638c34bb9054d12893e28bd5c18e3028091dc5186e914182471c76
```

三个命令均锁定主标注与第二标注 manifest 的既有 SHA256，要求 20 条轨迹身份及代码/说明/功能证据哈希逐项匹配、每轮记录绑定各自冻结协议、两轮共享同一标注指南与标签 Schema。输出包括四个二元字段的原始一致率/双向混淆计数/适用时的 Cohen's κ、首错字段在三个分母下的精确一致率和自然/反事实分层；不输出逐条分歧。`process_correct` 是 `has_error` 的 Schema 强制补集，不作为第二份独立证据重复计数。

单条分歧的待裁决记录按“聚合源预检 → 一次性初始化 → 固定哈希验证”执行：

```bash
tracejudge phase4 p1-adjudication-preflight
tracejudge phase4 p1-adjudication-init
tracejudge phase4 p1-adjudication-verify \
  --manifest-sha256 5dc8e34b1e6842b41db294b035e374afc2df77899433362b8af80b74c0da9009
```

初始化目录已存在时命令必须失败，任一冻结文件的字节变化也必须使验证失败。该步骤只创建 `pending_human_review` 记录，不代表分歧已裁决，也不允许将后续结论回写至任一原标签。

记录在案的双方共识使用独立命令追加，并要求显式确认双方同意和方法预测盲法；真实决定参数仅在受限环境中传入：

```bash
tracejudge phase4 p1-adjudication-consensus-complete \
  --annotation-item-id '<private blinded item id>' \
  --plan-code-aligned '<true|false>' \
  --first-faulty-layer '<layer>' \
  --first-faulty-step '<step>' \
  --error-type '<taxonomy code>' \
  --rationale '<private consensus rationale>' \
  --started-at '<ISO 8601 with offset>' \
  --completed-at '<ISO 8601 with offset>' \
  --both-confirmed \
  --method-blinding-confirmed

tracejudge phase4 p1-adjudication-completed-verify \
  --manifest-sha256 6e48963ee7cfe6cda2f113271286612af1640ca1abaf0eaeacedb62de2639287
```

完成命令在目标目录已存在时失败；验证命令检查私有权限、Schema、decision/report 哈希、两者语义一致性和来源绑定。它不把裁决写回原标签或原始一致性包。

裁决后聚合敏感性分析按“从固定来源发布 → 固定哈希验证”执行：

```bash
tracejudge phase4 p1-adjudication-sensitivity-publish
tracejudge phase4 p1-adjudication-sensitivity-verify \
  --json-sha256 377725050f8adbb4afe88f0b0e01ae05b4a2bc670c6920034fc8bb5b0472a48b \
  --markdown-sha256 7dd2f1f244c3bd09a2928b61c1ee36cb25e59a88e8631e0be3807d504384866d
```

发布器不打开两份逐条原标签或五种方法预测，只从 aggregate-only 一致性包和完成态裁决包计算原始一致性快照、1/1 已解决状态和固定分母影响上界。验证器检查源 manifest、确定性重建、隐私 canary、11/11 统计谬误扫描及零 Provider/Docker/网络调用；该结果是 post-hoc 敏感性读数，不替代预注册的原始一致性结果。

## 4. 私有备份与恢复

备份必须位于仓库外、访问受限且静态加密的介质。不要把 archive、私有 inventory 或恢复日志上传到公开网盘、Issue、Release 或 Git。

推荐流程：

1. 在 clean worktree 生成正式 private inventory；
2. 从 inventory 的 `artifacts[].relative_path` 生成仅本地使用的文件清单；
3. 按清单复制到加密备份，而不是递归复制整个 `artifacts/`；
4. 在独立临时目录恢复；
5. 使用 `artifact-verify` 对恢复目录核对大小、精确 mode 和 SHA256；
6. 只公开 inventory 整体哈希、公开 digest 和“恢复验证通过”状态，不公开备份路径或私有文件清单。

若恢复工具不能保留 POSIX mode，恢复后必须先收紧权限再验证。任何缺失、哈希、大小或 mode 不一致都视为恢复失败，不能通过重新生成 manifest 绕过。

## 5. P0 封版状态

- focused/full 普通测试、Ruff 和格式检查全部通过；最终提交范围 whitespace 检查排除逐字节冻结公开报告后通过，该报告另以固定 SHA256 和 `cmp` 验证；
- Docker 集成测试保持显式 opt-in，本门槛不执行；
- 私有权限警告清零；
- clean commit 上生成不可覆盖的 private inventory 和 public digest；
- 公开 replay receipt 已持久化并通过 Schema、哈希与隐私测试；
- Gate F 脱敏报告已逐字节发布为受 Git 跟踪的公开副本；
- 三张聚合 SVG、2 分钟 Fixture Demo、Release 检查单和封版报告已交付；
- 不修改阶段三冻结运行、统计或 Gate F 文件。

以上仓库内退出条件均已满足。Gate D/P1 和 v0.2+ 不阻塞本次 P0；Pull Request #4 已通过 merge commit `9627d93b668891c1fba0b255e403168afa731bf1` 合并到 `main`，`v0.1.0` tag、GitHub Release 和 11 个白名单附件已发布。P1 伦理状态为 `READY`；协议、公开练习、准入、交付、正式 20 条复标、删除确认和聚合一致性分析均已完成并验证；单条分歧完成态共识记录和裁决后敏感性分析亦已完成并验证。原始 19/20 完整记录一致率保持原样；1/1 分歧已解决不构成新的 20/20 一致率。任何精确裁决后方法分数仍必须建立新的版本化分析产物。
