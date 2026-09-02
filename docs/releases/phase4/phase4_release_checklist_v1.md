# 阶段四公开 Release 检查单 v1

Checklist ID：`phase4_release_checklist_v1`

状态说明：`PASS` 表示已有仓库证据，`DEFERRED` 表示非阻塞的 P1，`OUT OF SCOPE` 表示 v0.2+，`PENDING AUTHORIZATION` 表示需要项目负责人另行授权的 Git 或发布动作。

## P0 封版证据

- [x] `PASS`：阶段三合并提交、目标分支与冻结产物哈希已核验，阶段一至阶段三正式运行未被修改或覆盖。
- [x] `PASS`：阶段四公开 artifact digest 仅包含身份、哈希和安全计数，SHA256 为 `9094352967dbe90598d477c8abc0cdf6d0ac2dc311ab1d675b61d4460b477033`。
- [x] `PASS`：公开 replay receipt 已持久化，SHA256 为 `c1ba43dfe40b19af6929ddc9749a24f335933e22dad43ba626cbfc7c56e1d784`；`reproduced_failure=true`、`evidence_hash_verified=true`。
- [x] `PASS`：阶段三脱敏报告已受 Git 跟踪且与冻结 Gate F Markdown 逐字节一致，SHA256 为 `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725`。
- [x] `PASS`：正式聚合图表 bundle `phase4_public_charts_v1` 已受 Git 跟踪，manifest SHA256 为 `20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71`。
- [x] `PASS`：公开 Fixture Demo 已覆盖完整评估、公开反例、错误证书和精确白名单 replay，演示预算不超过 2 分钟。
- [x] `PASS`：公开报告、图表、Demo 和 receipt 均保留 `ANALYZED / CAUTION / CANNOT_VERIFY`，没有写成因果、等效、完整 benchmark 或跨模型结论。

## 隐私与安全

- [x] `PASS`：公开文件不含 `.env`、API Key、Authorization、Cookie、代理凭据或绝对用户路径。
- [x] `PASS`：公开文件不含 Provider raw、结构化修复轮 raw、EvalPlus raw、官方测试正文、具体失败输入或 HumanEval+ canonical solution。
- [x] `PASS`：公开文件不含私有逐轨迹标签、标注理由、身份映射或未脱敏逐轨迹方法预测。
- [x] `PASS`：私有标签和 raw 仍位于 Git-ignored、权限受限目录，阶段四没有改变其隐私级别。
- [x] `PASS`：Demo 与 replay 只执行公开 `safe_mean` 白名单 Fixture；Provider、Docker、网络调用计数均为 0。

## 哈希与可复现性

- [x] `PASS`：阶段三统计 manifest / report SHA256 分别为 `7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8` / `972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`。
- [x] `PASS`：Gate F manifest / Markdown / validation SHA256 分别为 `0b8285ec04344e29670d752a37c4d5ecb41ea07d5dfc18a5715b56de3e800b06` / `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725` / `702bf96be5d0911088dfea5cb95562d6b8e25d147d972c78b0b6870cecbae113`。
- [x] `PASS`：三个 SVG SHA256 分别为 `33fc5806172729d2543280954fc09f2774aa13737ae5f922c35bd65905afe98c`、`a7020cb43b163fc52df533897bac72c4bef011691795ee0445899013808802b2`、`08b45448d1329b0c078365e68042f74ba54539e28a197c47bf210cf42b6a197f`。
- [x] `PASS`：图表可从受 Git 跟踪的 manifest 确定性重绘并逐字节验证。
- [x] `PASS`：公开 receipt 只证明记录环境中的单 Fixture replay；没有把 fresh-clone 或 Hy3 主实验复现写成已完成。

## 验证命令

从仓库根目录运行：

```bash
env -u TRACEJUDGE_RUN_DOCKER_INTEGRATION .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check origin/main...HEAD -- . \
  ':(exclude)docs/releases/phase4/phase3_research_report_public_v1.md'
.venv/bin/tracejudge phase4 charts-verify \
  --manifest docs/releases/phase4/charts/phase4_public_charts_v1/manifest.json \
  --manifest-sha256 20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71
```

发布前还应重新执行：

```bash
shasum -a 256 docs/releases/phase4/phase3_research_report_public_v1.md
cmp -s \
  docs/releases/phase4/phase3_research_report_public_v1.md \
  artifacts/experiments/phase3-reports/phase3_report_primary_round1_v1/phase3_research_report.md
shasum -a 256 \
  docs/releases/phase4/phase4_public_artifact_digest_v1.json \
  docs/releases/phase4/phase4_public_replay_receipt_v1.json \
  docs/releases/phase4/charts/phase4_public_charts_v1/manifest.json \
  docs/releases/phase4/charts/phase4_public_charts_v1/*.svg
git status --short --branch
```

公开报告的预期 SHA256 为 `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725`。它为保持 Gate F 逐字节身份而保留源文件的 EOF，因此从提交范围 whitespace 检查中明确排除，并改用 SHA256 和 `cmp` 单独验证；没有 Git-ignored 保管产物的全新 clone 只能执行公开文件 SHA256 检查，不能伪造 `cmp` 通过。

失败判断：任一测试或静态检查失败、公开报告 SHA256 或 `cmp` 不一致、其他提交范围存在 whitespace 错误、图表重绘不一致、出现未审查文件、工作树包含无关改动，或任何隐私 canary 命中。此时不得 tag 或发布 Release。

## 非阻塞后续范围

- [ ] `DEFERRED P1`：第二位标注者独立复标预先冻结的 15–20 条；无第二标注者时改为间隔至少 7 天的跨时间复标。
- [ ] `DEFERRED P1`：报告 raw agreement、混淆计数和适用条件下的 Cohen's kappa；增加公开反事实父题 cluster、消融证据和两条 Provider 失败敏感性分析均需新的预注册身份。
- [ ] `OUT OF SCOPE v0.2+`：完整 HumanEval+ 164 题、MBPP+、多模型 Judge、Web UI、自动修复、多文件和多语言执行。

## 需要项目负责人授权的动作

- [x] `PASS`：本检查单、Demo、封版报告、索引、状态和测试改动已经完成审查并提交到目标分支。
- [x] `PASS`：目标分支已 push，[Pull Request #4](https://github.com/Even1115/tracejudge-hy3/pull/4) 已创建。
- [ ] `PENDING AUTHORIZATION`：合并 Pull Request。
- [ ] `PENDING AUTHORIZATION`：创建 tag、生成 Release 或上传附件；在 Release 说明中记录最终 commit ID 及本检查单/封版报告自身 SHA256。

P0 Gate E 的仓库内交付、审查、提交、目标分支 push 和 Pull Request #4 创建已经完成；merge、tag、Release 和附件上传在获得明确授权前始终保持未勾选。
