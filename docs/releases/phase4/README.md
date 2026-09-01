# Phase 4 public reproducibility evidence

本目录只保存可公开的阶段四复现证据，不保存私有路径、人工标签、逐轨迹预测、Provider raw、EvalPlus raw、官方测试正文或候选源码。

- `phase4_public_replay_receipt_v1.json`：在阶段四修改开始前，从 clean 阶段三合并提交执行一个精确白名单公开 Fixture 得到的 replay receipt。
- `phase4_public_artifact_digest_v1.json`：须在权限加固、Phase4 实现提交并回到 clean worktree 后，由 `phase4 artifact-freeze` 生成；当前不预造该文件。

公开 receipt 证明指定公开证书在记录环境中重现了同一失败并得到相同执行证据哈希；它不重跑 Hy3，也不证明五种方法的普遍有效性。
