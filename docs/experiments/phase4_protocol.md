# 阶段四：复现加固、脱敏成果发布与项目封版

版本：P0 封版 + P1 Gate D 标注准备版（练习已准入）/ 2026-09-03

阶段四只为既有阶段一至阶段三结果建立复现、备份、公开发布和封版证据，不覆盖或重解释冻结运行。任何新增研究运行必须使用新的 ID、目录、manifest 和报告版本。

P0 Gate A、B、C、E 的仓库内交付已经完成。Gate D/P1 已完成第二标注者的 5 条公开 Fixture 练习校准，练习记录通过冻结阈值并已书面准入正式 20 条复标。练习标签仅用于校准，排除于研究终点。单次交付记录仍为 `pending_completion`，因此正式盲化包仍未创建/发送，仍未发包或开始人类数据收集，20 条第二标注者数据和一致性统计仍未生成。P1 不回写阶段三产物，也不改变 P0 的封版结论。

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

公开单次交付记录 Schema SHA256 为 `3e2cf0921da2bebac52505cc87e503d2f559d889c9672c53a57614116f32fdd9`；Git-ignored 私有模板已经以 `0700/0600` 权限创建，已回填参与同意/时间、负责人授权、五类渠道、练习回传归档 SHA256 和已确定条款；当前仍缺 6 项具体交付记录，状态为 `pending_completion`。Schema 本身不含个人信息；实际私有记录允许保存受限联系信息，因此明确禁止公开。正式子集 `phase4_p1_formal_subset_v1` 已在查看练习答案前冻结 15 条自然 + 5 条反事实；反事实覆盖 3 个父题且每父题最多 2 条。私有 manifest / 不含入选 ID 的公开 commitment SHA256 分别为 `03782826f5054238962c5a007116e9df204fc9f69e8dc746925c8bef2ac2082c` / `b5090ad78715857455852e3450fa606f4963ca726a3df91a1b6603d372c491a2`。冻结过程未读取主标签、方法预测、Provider 状态或事后结果，且未创建正式 packet。

5 条练习回传的 Schema、`has_error`、`process_correct`、错误条目首错层分别为 5/5、5/5、5/5、3/3，且隐私/盲法异常数为 0。私有准入记录 `phase4_p1_practice_admission_rater02_v1` SHA256 为 `d7461f2562ae62162733fdde945e28b92b433e96f54de34d8a4e788d9f2fb1ae`；它只保存输入哈希、聚合校准得分、零违规确认与书面准入决定，不复制 rationale 或标签正文。正式 packet 生成器已实现，但现有交付记录的预检在 `P4D_P1_DELIVERY` 失败关闭，所以未读取/写出正式 20 条材料。

### 2.7 独立 Judge 稳定性附加实验

`phase4_judge_stability_public4x5_v1` 固定使用同一 `safe_mean` 父题的正常正确、reasoning swap、边界删除和等价实现四个公开案例，各运行 Full TraceJudge 5 次。20 个评审单元使用独立 run ID、目录、manifest 与报告，逐条原子 checkpoint；报告 `has_error`、首错步骤、错误类型及联合标签的一致性，同时单列 Provider/解析/其他失败和实际请求数。它不读取人工标签或隐藏评测正文，且 `main_experiment_merge_allowed=false`，不得回写 57 × 5 主实验。

协议、分析口径、续跑规则和结论边界见 `docs/experiments/phase4_judge_stability_protocol_v1.md`。实现及离线 Mock 验收已经完成；真实 Hy3 的 20 个评审尚未启动，因此这里不报告稳定性数值。

### 2.8 P1 Material Passport

- Source：冻结的 42+15 cohort、正式 20 条私有子集 manifest、公开 commitment、练习回传归档/标签哈希和协调者参考。
- Transform：练习只计算预注册的准入汇总；正式包只在交付硬门通过后重建公开可见材料，并以确定性哈希顺序映射到 `formal_item_001..020`。
- Current stage：练习校准已完成并准入；正式包交付被单次交付记录阻断。
- Outputs：Git-ignored `0700/0600` 准入记录；门禁通过后将产生参与者 `packet.jsonl` + `labels_template.jsonl`，以及单独的协调者 `identity_map.jsonl`。
- Gate：必须将真实、已发生的同意/收件时间、五类渠道、期限、报酬/署名、退出/保留/销毁、联系方式和负责人授权填入私有交付记录；不得猜测或回填未发生时间。

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

现有准入记录已冻结，不得重复执行或覆盖。在私有交付记录完整且 `data_collection_allowed=true` 之前，下列预检/导出都必须失败关闭：

```bash
tracejudge phase4 p1-formal-packet-preflight
tracejudge phase4 p1-formal-packet-export
tracejudge phase4 p1-formal-packet-verify \
  --manifest-sha256 <formal-packet-manifest-sha256>
```

门禁通过后，对外只发送 `participant/packet.jsonl` 和 `participant/labels_template.jsonl`。`coordinator/identity_map.jsonl` 只由项目协调者保管，不得放入参与者归档。

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

以上仓库内退出条件均已满足。Gate D/P1 和 v0.2+ 不阻塞本次 P0；Pull Request #4 已通过 merge commit `9627d93b668891c1fba0b255e403168afa731bf1` 合并到 `main`，`v0.1.0` tag、GitHub Release 和 11 个白名单附件已发布。P1 伦理状态已为 `READY`，协议、公开练习包、练习准入记录、交付记录 Schema/私有模板和正式 20 条子集均已冻结；但单次交付记录尚未完成，正式 20 条盲化包仍未创建/发送，正式复标数据收集未开始。
