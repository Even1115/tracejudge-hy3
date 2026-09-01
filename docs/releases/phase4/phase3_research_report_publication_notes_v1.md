# 阶段三脱敏研究报告公开发布说明 v1

Publication ID：`phase4_gate_c_phase3_report_publication_v1`

## 发布对象与身份

- 受 Git 跟踪的公开报告：[`phase3_research_report_public_v1.md`](phase3_research_report_public_v1.md)
- 冻结来源：`phase3_report_primary_round1_v1/phase3_research_report.md`
- 公开报告与冻结来源逐字节一致：是
- Markdown SHA256：`29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725`
- Gate F manifest SHA256：`0b8285ec04344e29670d752a37c4d5ecb41ea07d5dfc18a5715b56de3e800b06`
- Gate F validation SHA256：`702bf96be5d0911088dfea5cb95562d6b8e25d147d972c78b0b6870cecbae113`
- Statistics report SHA256：`972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`

阶段四没有修改或重新生成阶段三冻结报告。仓库内 Gate F 只读预检在不写文件、不执行候选、不调用 Provider、Docker 或网络的条件下，重新得到相同的 Markdown 与 validation 哈希。

## 隐私审计

公开报告只包含聚合统计、运行身份、确定性哈希和公开自建 Fixture 的证书说明。Gate F manifest 与 validation 声明不包含逐轨迹预测、标注理由、Provider raw 或隐藏评测内容；阶段四额外扫描未发现绝对用户路径、凭据字段、HumanEval+ canonical solution、官方测试正文、具体失败输入、identity map 或私有逐轨迹记录。

以下内容仍只保存在 Git-ignored、权限受限的目录中，没有因本次发布而改变隐私级别：人工标签与理由、身份映射、逐轨迹方法预测、Provider raw、EvalPlus raw、官方测试正文和失败输入。

## 阶段四补充证据

- 公开 replay receipt：[`phase4_public_replay_receipt_v1.json`](phase4_public_replay_receipt_v1.json)，SHA256 `c1ba43dfe40b19af6929ddc9749a24f335933e22dad43ba626cbfc7c56e1d784`
- 公开 artifact digest：[`phase4_public_artifact_digest_v1.json`](phase4_public_artifact_digest_v1.json)，SHA256 `9094352967dbe90598d477c8abc0cdf6d0ac2dc311ab1d675b61d4460b477033`

冻结报告中“Gate F replay receipt：未生成”描述的是 Gate F 当时的执行边界，仍属历史事实。阶段四 receipt 后续从 clean 阶段三合并提交独立持久化，只证明一个精确白名单公开 Fixture 的证书 replay 和执行证据哈希一致；它没有重跑 Hy3 主实验，也不会把总体复现判定从 `CANNOT_VERIFY` 升级。

## 已知展示缺口

Gate F 统计谬误扫描的 base-rate 护栏写明应同时报告混淆计数和 precision/recall。对应聚合字段存在于冻结 E4 `report.json` 的 `valid_only_confusion` 中，但本次逐字节发布的 Gate F Markdown 没有展示这些列。因此本公开报告 v1 不能被描述为已经在正文中完整展示混淆计数和 precision/recall；若后续补充，必须使用新的阶段四报告 ID、文件和哈希，不得覆盖本文件。

## 结论与完整性边界

- Verification Status：`ANALYZED`
- Overall Confidence：`CAUTION`
- Reproducibility：`CANNOT_VERIFY`
- 外部文献与原创性联网核验：未执行；本报告没有参考文献表，本次审计只核验仓库内冻结证据和发布边界。
- 独立重复实验：未执行。

现有证据仍受单标注者单轮次、仅 3 个反事实父题 cluster、单模型单次运行和 2 条 Provider 失败限制。公开发布不支持普遍优越性、组件因果贡献、方法等效、完整 benchmark 排名或跨模型推广结论。
