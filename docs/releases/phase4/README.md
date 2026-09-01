# Phase 4 public reproducibility evidence

本目录只保存可公开的阶段四复现证据，不保存私有路径、人工标签、逐轨迹预测、Provider raw、EvalPlus raw、官方测试正文或候选源码。

- `phase4_public_replay_receipt_v1.json`：在阶段四修改开始前，从 clean 阶段三合并提交执行一个精确白名单公开 Fixture 得到的 replay receipt。
- `phase4_public_artifact_digest_v1.json`：权限加固后从 clean commit `065085bfa27795d6432e1fcf8b6421103f0b00e8` 正式冻结；绑定 103 个关键产物、13 个公开锚点和 0 个权限警告。确定性 artifact-set SHA256 为 `84c584a116700430b7fea14c5f81d8b23f6094badc1dc410a013c7bd7615f13b`，对应私有 inventory manifest SHA256 为 `ad2e4489d608b8bdb21a3a108eb4eba5ca078f8db5b748cd6d6669d58d1ab997`。

公开 receipt 证明指定公开证书在记录环境中重现了同一失败并得到相同执行证据哈希；它不重跑 Hy3，也不证明五种方法的普遍有效性。
