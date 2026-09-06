# Windows / WSL LCB60 修复后 readiness

结论：**READY_FOR_API_START**。最终核验时间：2026-09-06 14:34:06（北京时间）。两处实现缺陷已经修复，相关离线回归、真实大请求容器验证、五态冒烟和新冻结快照预检均通过。**本轮没有调用真实模型 API，也没有启动正式实验。**

用户此前已完成 40 题的真实生成；第 41 题的上游响应仍可能失败。本轮修复保证已复现的 HTTP JSON 解析异常进入原有有限重试和错误记录流程，不代表已经验证上游服务恢复。

## 已完成的修复

| 修改 | 原因和修复后行为 |
| --- | --- |
| `src/tracejudge_hy3/providers/hy3_openai.py` | 捕获 SDK 请求边界的 `json.JSONDecodeError`，转换为静态脱敏 `ProviderResponseError`。沿用现有最多 2 次额外重试；耗尽后记录 `provider_error`。不修改 prompt、解析修复预算或超时。 |
| `src/tracejudge_hy3/lcb/container_entrypoint.py` | 请求传输上限由 64 MiB 调至有界的 128 MiB，以容纳固定选择中最大 69,014,330 字节请求。保留 schema、文件类型、摘要和容器隔离检查。 |
| 两个对应测试文件 | 新增 8 个 SDK MockTransport 用例及 2 个大请求边界用例。 |

128 MiB 的依据是本次固定 60 题的实际序列化大小。它不代表任意未来原始记录都能容纳；原始记录大小与外层 JSON 传输大小不能混为一谈。

Windows 源项目与 WSL 源项目均包含上述四个文件的修改，未提交源工作树。原冻结快照未改；新快照按仓库原机制生成独立干净 Git commit。没有修改抽样、题数、prompt、评分、共享契约、task/test 超时、锁文件或身份门禁。

## 实际验证结果

| 检查 | 结果 |
| --- | --- |
| 新增用例运行于未修改旧快照 | 7 failed / 3 passed / 62 deselected；失败位置正是两处缺陷，证明用例能检出原问题 |
| 修复后相关离线回归 | **125 passed / 0 failed / 0 skipped / 2 deselected / 0 collection errors**，2.53 秒 |
| 原两项排除测试 | `test_entrypoint_compile_probe`、`test_entrypoint_official_private_decode_chain`，遵守宿主不编译候选、不执行私有解码的边界 |
| 现有警告 | 1 条 `TestVisibility` 的 PytestCollectionWarning，与本次修复无关 |
| Ruff 静态检查 | 四个修改文件全部通过 |
| 大请求，旧快照 + 原镜像 | 69,014,330 字节自写 fixture → `infrastructure_error / container_exit_error`，复现成功 |
| 大请求，新快照 + 同一镜像 | 相同大小自写 fixture → **passed** |
| 超出 128 MiB 的请求 | 自写稀疏文件在读取前被拒绝，边界测试通过 |
| 新快照 `--preflight` | **退出 0：60 tasks；no API calls** |

大请求 fixture 使用自写 echo 测试，给合法 opaque 字符串补 ASCII 空白，按原 runner 完整序列化规则精确达到 69,014,330 字节。调用的是原 `LCBDockerRunner.run_task()`；宿主只序列化自写 fixture，不解码私有字符串。读取、解码及候选执行均由真实隔离容器完成。每次只运行一个评测容器。

从新冻结快照执行原 `scripts/smoke_external_benchmarks.py --dataset lcb` 的五态结果：

| Fixture | 实际状态 | 结果 |
| --- | --- | --- |
| pass | passed | PASS |
| wrong_answer | failed / wrong_answer | PASS |
| timeout | timeout / time_limit_exceeded | PASS |
| runtime_error | runtime_error | PASS |
| compile_error | compile_error | PASS |

本轮没有重新构建或下载镜像。入口脚本由 runner 从源码复制、只读挂载进容器，因此此修复由新源码身份体现；镜像内固定 checker 没有变化。

## 本机环境、数据及身份

| 项目 | 实际身份 |
| --- | --- |
| WSL 项目绝对路径 | `/home/even/projects/tracejudge-hy3-lcb60-windows-20260906` |
| Python | 项目 `.venv/bin/python`，Linux Python 3.12.3；没有使用 Windows Python 或 Mac 环境 |
| 既有环境 | WSL2 / Ubuntu-24.04 / x86_64；Docker Desktop Linux 容器与该发行版集成已在本轮真实执行中再次验证可用 |
| 源分支、HEAD | `benchmark/mbpp-lcb-wsl`，`67085adb75cb64d349f6cb89ded3f27356b5c967`，另有明确的四文件未提交修改 |
| 新冻结快照 | `/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/artifacts/benchmark-runtime/f4249a0448427578` |
| 新快照完整 commit | `0096a408f7703aabf1fadd7dd38400290d74438d` |
| 快照清单 | 208 文件；每个文件与实际修复源码及 `FREEZE.json` 哈希相等；Git 干净 |
| inventory SHA-256 | `ccb8528ab51c846144642bae740ac15a2c64c0eab9cba6b2bad9269f1313e5df` |
| 完整 source_identity SHA-256 | `cbc6e1b6029da63037125ea5e6a3966ffb2837ab0335bfcdce116ecccbc2c6c7` |
| Provider 源码 SHA-256 | `46ffb1e760e4285715a2d071666d2a10fcae3594a424df5332f78690e08e6633` |
| 新入口源码 SHA-256 | `6ff1840f36f5eafb36e4e32621dd06a7e82a57dc7b82e85309d9b7fb31b83d84` |
| 原 runner SHA-256，未改 | `1f44e809a3e0d78ebd127d8e66134ae782fd70a3bb4d68917212d31da3b69087` |
| 原 adapter SHA-256，未改 | `f5dce4bdf4a739f625c612b53e33f68be891d48627f0a2bd607cab313821a693` |
| 完整本地镜像 `.Id` | `sha256:98466e5add3e7198167d81f098a3005b285a5daca4b9ae7ceb70b7bbb9e070c2` |
| 镜像平台 | `linux/amd64`；用 inspect 返回的完整 `.Id` 核验，没有拼接 repository@本地ID |
| checker | 独立干净 `.git`，commit `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24`，四个固定文件哈希再验证通过 |
| 数据 | 原 release_v6 六文件及 loader，由最终 preflight 原加载器验证固定 revision、大小和 SHA-256 |
| 固定 HF revision | `0fe84c3912ea0c4d4a78037083943e8f0c4dd505` |
| 固定选择 | 60 题，easy/medium/hard 各 20；未重新设计或修改选择锁 |
| 有序题号 SHA-256 | `01a4b6bbf91d8337530f005e34b3caf8b9f19fecb46510f98329dbc958d4017e` |
| selection60 文件 SHA-256 | `bb30680876e0f1780954fa92c208704a8d50529c2bf240e1a396d61005daf381`，修复前后未变 |
| uv.lock SHA-256 | `d6c9ba5c7f569d4a5dea7cc072d87a7b0c549d0d8d00ebdc9098ddf1a0c4a287`，未更新依赖 |
| 模型配置 | 只验证本机必需字段存在；timeout 120 秒、max_retries 2 未变 |
| 实验限额 | 原 task 600 秒 / test 6 秒，单评测容器；进程 `umask 022` |
| 容器占用 | 本轮开始和结束均无运行容器，未停止他人容器 |

仍使用 WSL `/home` 下的数据和产物，未回到 `/mnt/e` 正式运行。冻结所需的小型 MBPP manifest 和 tracked data/docs 公共文件按原规则包含在 208 文件清单中；没有等待其他机器或要求 MBPP 数据齐全。

新 receipt：`/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/artifacts/benchmark-readiness/lcb.json`，SHA-256 为 `12d454b494841e0f4772501c74592c3ae62a139430dff515c0b5fea801bd5e37`。其三份执行源码哈希与本机源代码、新快照完全相等；provider 则由完整 source_identity / FREEZE 清单绑定。

## 旧结果保全与新 run

旧快照 `1edbad4c80a9db0c`、commit `bb0993e4fa4de2781dfb2ce5366a1457dad47e0d` 及旧 run `lcb60-windows-20260906-v1` 的 **85 个文件全部哈希未变**。旧状态仍为 40 题生成成功、38 题通过、1 题错误答案、1 题基础设施失败、20 题尚未生成，`complete=false`。

旧 `lcb.json`、`image.json`、`current.json` 均保存在原目录下的 `*.before-codefix-20260906T0621Z.json`，并另有独占备份。没有删除或覆盖旧实验记录。

**新源码不能对旧 run 强行 `--resume`。** 当前入口不支持跨源码身份导入旧生成结果。若使用下面的新 run，会从固定 60 题重新生成，不会自动复用旧 40 题；这会产生新的 API 调用。这里只交付命令，没有执行。

## 正式命令（仅文本，不执行）

在 Ubuntu-24.04 的 WSL 终端中使用。新 run ID `lcb60-windows-20260906-v2` 在交付时尚不存在。不要把下列新快照路径与旧 `v1` 混用。

首次启动：

```bash
cd /home/even/projects/tracejudge-hy3-lcb60-windows-20260906
umask 022

/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python \
  /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/artifacts/benchmark-runtime/f4249a0448427578/scripts/run_livecodebench.py \
  --project-root /home/even/projects/tracejudge-hy3-lcb60-windows-20260906 \
  --image sha256:98466e5add3e7198167d81f098a3005b285a5daca4b9ae7ceb70b7bbb9e070c2 \
  --run-id lcb60-windows-20260906-v2 \
  --confirm-real-provider
```

只在上述 `v2` 已启动后，对同一新快照、同一 run 的恢复命令：

```bash
cd /home/even/projects/tracejudge-hy3-lcb60-windows-20260906
umask 022

/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python \
  /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/artifacts/benchmark-runtime/f4249a0448427578/scripts/run_livecodebench.py \
  --project-root /home/even/projects/tracejudge-hy3-lcb60-windows-20260906 \
  --image sha256:98466e5add3e7198167d81f098a3005b285a5daca4b9ae7ceb70b7bbb9e070c2 \
  --run-id lcb60-windows-20260906-v2 \
  --confirm-real-provider \
  --resume
```

## 实际执行命令与证据

以下是本轮已执行的检查编排命令，不包括上面的正式启动文本：

```bash
TJ_PY=/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python
TJ_CHECK=/mnt/e/犀牛鸟/tracejudge-hy3/artifacts/benchmark-readiness/windows-lcb-codefix-20260906T0621Z
"$TJ_PY" -B "$TJ_CHECK/preserve_and_sync.py" preserve
"$TJ_PY" -B "$TJ_CHECK/run_regressions.py" red
"$TJ_PY" -B "$TJ_CHECK/preserve_and_sync.py" sync
"$TJ_PY" -B "$TJ_CHECK/run_regressions.py" green
"$TJ_PY" -B "$TJ_CHECK/freeze_and_smoke.py" freeze
"$TJ_PY" -B "$TJ_CHECK/large_request_container.py" red
"$TJ_PY" -B "$TJ_CHECK/large_request_container.py" green
"$TJ_PY" -B "$TJ_CHECK/freeze_and_smoke.py" smoke
"$TJ_PY" -B "$TJ_CHECK/freeze_and_smoke.py" preflight
"$TJ_PY" -B "$TJ_CHECK/verify_final.py"
```

`red` 离线回归预期退出 1，其余验证退出 0。另运行 `.venv/bin/ruff check --no-cache` 检查四个变更文件。编排脚本使用独占创建和原状态校验，属于检查记录，不应照抄覆盖重跑。

五态和预检实际底层命令分别为新快照的 `scripts/smoke_external_benchmarks.py --project-root <本机项目> --dataset lcb --image <完整本地ID>` 与 `scripts/run_livecodebench.py --project-root <本机项目> --image <完整本地ID> --preflight`；完整参数记录在 `*.command.json`。

本报告相邻证据包括 `implemented.patch`、`offline_red.json`、`offline_green.json`、`large_request_red.json`、`large_request_green.json`、`five_status_receipt.json`、`freeze_result.json`、`final_evidence.json` 及原脚本日志。Linux 归档目录为 `/home/even/projects/tracejudge-hy3-lcb60-windows-20260906/artifacts/benchmark-readiness/windows-lcb-codefix-20260906T0621Z`。

## 未验证事项和边界确认

- **本轮未重新验证真实 API 连通性、稳定性或额度。** 用户此前有成功调用，但两次失败响应体未保存，具体上游原因仍未知。
- 未重新执行正式题目的候选或真实隐藏测试；大请求复现使用自写 fixture，不能把它当作 `abc373_c` 正式执行结果。
- 没有启动 `v2`，没有跨版本迁移或合并旧结果。
- 本轮没有调用真实模型 API；没有修改实验抽样、题数、prompt、评分、共享契约或超时；没有公开凭据、隐藏测试或完整模型响应；没有在宿主执行/编译候选、解码私有测试；没有绕过身份门禁。
