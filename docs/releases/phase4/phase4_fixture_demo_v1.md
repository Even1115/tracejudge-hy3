# 阶段四公开 Fixture Demo v1

Demo ID：`phase4_fixture_demo_v1`

## 目标与边界

本 Demo 在不超过 2 分钟内，用仓库自建且可公开的 `safe_mean` Fixture 展示完整评估、反例、错误证书和证书 replay。它不使用 HumanEval+ canonical solution、EvalPlus Base/Extra 测试正文、私有逐轨迹标签或预测，也不调用真实 Hy3、Docker 或网络。

`demo` 输出中出现的 `hidden` / `challenge` 是自建公开 Fixture 内部的测试类别名称，不代表公开了 EvalPlus 或其他第三方隐藏测试。Demo 产生的完整 JSON 继续留在 Git-ignored `artifacts/`，不得误作为阶段三逐轨迹研究产物提交。

## 演示前检查

在仓库根目录执行：

```bash
test -x .venv/bin/tracejudge
test -f docs/releases/phase4/phase4_public_replay_receipt_v1.json
```

两条命令均应以退出码 0 结束。任何文件缺失、虚拟环境不可执行或工作目录错误都应先修复，不要临时改用真实 Provider。

## 2 分钟时间线

| 时间 | 展示内容 | 讲解要点 |
|---|---|---|
| 00:00–00:10 | 说明对象 | `safe_mean` 是公开自建 Fixture；本次没有真实 Hy3、Docker 或网络。 |
| 00:10–00:35 | 运行完整 Mock 评估 | 可见用例通过，但空列表公开反例触发 `ZeroDivisionError`。 |
| 00:35–01:05 | 指向评估输出 | 说明 AST 未发现空输入分支、首错定位为 `alignment / S1 / R1`、反例为 `args=[[]]`。 |
| 01:05–01:30 | 运行公开证书 replay | 应显示“重现证书失败：是”“证据哈希一致：是”和“Provider / Docker / 网络：否 / 否 / 否”。 |
| 01:30–01:50 | 指向持久化 receipt | receipt 只含公开身份、哈希和安全计数；`reproduced_failure=true`、`evidence_hash_verified=true`。 |
| 01:50–02:00 | 结论边界 | 这是单个工程 Fixture 的可执行证据，不是五方法普遍有效性或阶段三独立复现实验。 |

## 演示命令

第一步运行完整公开 Mock Fixture。默认时间戳文件名避免覆盖已有结果：

```bash
env -u TRACEJUDGE_RUN_DOCKER_INTEGRATION .venv/bin/tracejudge demo \
  --mock \
  --case faulty \
  --per-test-timeout-seconds 5
```

预期输出至少包含：

- `题目 safe_mean`；
- 可见测试通过，空列表公开 Fixture 用例触发 `ZeroDivisionError`；
- `反例 (Counterexample)`，来源为 `challenge_test`；
- `错误证书 (ErrorCertificate)`，`verdict: confirmed_bug`；
- 结果写入 Git-ignored `artifacts/`。

失败判断：命令非零退出、出现真实 Provider 配置请求、启动 Docker、尝试联网、没有反例、证书不是 `confirmed_bug`，或写入路径不在 `artifacts/`。

第二步在保留阶段三 Git-ignored 正式产物的项目保管副本中，执行 receipt 记录的精确白名单 replay：

```bash
env -u TRACEJUDGE_RUN_DOCKER_INTEGRATION .venv/bin/tracejudge phase3 replay \
  --certificate artifacts/experiments/phase3-public-certificates/phase3_gate_d_public_certificates_v1/certificates/certificate_001.json \
  --cohort-manifest artifacts/experiments/phase3-freezes/phase3_cohort_42_plus_15_v1/manifest.json \
  --natural-manifest artifacts/experiments/phase3-freezes/phase3_natural_42_v1/manifest.json \
  --source-bundle data/phase3/public_counterfactuals_v1.json
```

预期输出为 `重现证书失败：是`、`证据哈希一致：是`、执行公开用例 1 条、执行证据 SHA256 `cfd897334643853fc10901835a5203aa51ee7edd4442e314893c1e5bc152e670`，且 Provider / Docker / 网络均为否。

若是没有 Git-ignored 阶段三保管产物的全新 clone，第二步预期因输入缺失而停止；这不是隐私材料应被补进 Git 的理由。此时只展示受 Git 跟踪的 [`phase4_public_replay_receipt_v1.json`](phase4_public_replay_receipt_v1.json)，并如实保留 `CANNOT_VERIFY`。

## 已验证的演示预算

Gate E 本地干跑中，完整 Mock Demo 用时约 0.93 秒，公开证书 replay 用时约 0.76 秒；二者均未调用 Provider、Docker 或网络。该时间仅用于确认 2 分钟脚本有充足余量，不构成跨机器性能承诺。

