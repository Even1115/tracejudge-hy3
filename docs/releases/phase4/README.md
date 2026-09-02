# Phase 4 public reproducibility evidence

本目录只保存可公开的阶段四复现证据，不保存私有路径、人工标签、逐轨迹预测、Provider raw、EvalPlus raw、官方测试正文或候选源码。

- `phase4_public_replay_receipt_v1.json`：在阶段四修改开始前，从 clean 阶段三合并提交执行一个精确白名单公开 Fixture 得到的 replay receipt。
- `phase4_public_artifact_digest_v1.json`：权限加固后从 clean commit `065085bfa27795d6432e1fcf8b6421103f0b00e8` 正式冻结；绑定 103 个关键产物、13 个公开锚点和 0 个权限警告。确定性 artifact-set SHA256 为 `84c584a116700430b7fea14c5f81d8b23f6094badc1dc410a013c7bd7615f13b`，对应私有 inventory manifest SHA256 为 `ad2e4489d608b8bdb21a3a108eb4eba5ca078f8db5b748cd6d6669d58d1ab997`。
- `phase3_research_report_public_v1.md`：与 Git-ignored Gate F 冻结 Markdown 逐字节一致的正式公开副本，SHA256 为 `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725`。
- `phase3_research_report_publication_notes_v1.md`：Gate C 发布审计、阶段四补充证据、已知展示缺口和结论边界。
- [`charts/phase4_public_charts_v1/manifest.json`](charts/phase4_public_charts_v1/manifest.json)：三张仅含聚合结果的确定性 SVG 与 manifest；manifest SHA256 为 `20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71`。
- [`phase4_fixture_demo_v1.md`](phase4_fixture_demo_v1.md)：不超过 2 分钟的公开 `safe_mean` Fixture 演示，覆盖评估、反例、错误证书与精确白名单 replay。
- [`phase4_release_checklist_v1.md`](phase4_release_checklist_v1.md)：隐私、哈希、测试、范围和发布权限检查单。
- [`phase4_closure_report_v1.md`](phase4_closure_report_v1.md)：P0 Gate A、B、C、E 封版状态、阶段三证据边界、P1/v0.2+ 范围和最终授权边界；Gate D 为非阻塞研究增强。

公开 receipt 证明指定公开证书在记录环境中重现了同一失败并得到相同执行证据哈希；它不重跑 Hy3，也不证明五种方法的普遍有效性。公开研究报告继续保持 `ANALYZED / CAUTION / CANNOT_VERIFY`，不得将发布动作解释为证据等级提升。

阶段四 P0 的仓库内交付、审查和提交已完成；push、PR、merge、tag、Release 和任何附件上传仍需项目负责人明确授权。
