# 阶段四：复现加固、脱敏成果发布与项目封版

版本：Gate B 实现草案 / 2026-09-01

阶段四只为既有阶段一至阶段三结果建立复现、备份、公开发布和封版证据，不覆盖或重解释冻结运行。任何新增研究运行必须使用新的 ID、目录、manifest 和报告版本。

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

## 5. 当前退出条件

- focused/full 普通测试、Ruff、格式检查与 `git diff --check` 全部通过；
- Docker 集成测试保持显式 opt-in，本门槛不执行；
- 私有权限警告清零；
- clean commit 上生成不可覆盖的 private inventory 和 public digest；
- 公开 replay receipt 已持久化并通过 Schema、哈希与隐私测试；
- 不修改阶段三冻结运行、统计或 Gate F 文件。
