# MBPP+ 120 / LiveCodeBench 60：正式运行手册

本轮范围是单候选代码生成 + 官方测试执行；不是过程 Judge 的新实验。
共享 `contracts.py`、`humanevalplus.py`、`benchmark_contract_v1.md` 和契约测试不做修改。

## 当前状态（2026-09-06）

- MBPP+：378 题完整公开投影、固定 120 题子集均已落盘并通过身份校验；固定 EvalPlus 镜像已拉取。
- LiveCodeBench：release_v6 全量源文件校验通过，60 题选择锁已生成，easy/medium/hard 各 20 题；有序题号 SHA-256 为 `01a4b6bbf91d8337530f005e34b3caf8b9f19fecb46510f98329dbc958d4017e`。
- LCB 的真实官方 checker 容器曾通过 pass / WA / TLE / RE / CE 五项冒烟。新增源码身份门禁后，需要重新生成与最终冻结代码匹配的 receipt。
- MBPP+ 的 native set/tuple、非有限浮点数兼容问题已修复。冒烟 v2 区分函数调用内超时（官方可记为 `fail`）与评测子进程超时（`timeout`），不修改官方判分；必须通过下述四项真实冒烟，不能把离线测试当作通过凭证。
- HumanEval+ 的全量执行及恢复已完成。当前主机 8 GB RAM，仍不要同时启动多个容器评测，不要删除旧结果，不要通过增大超时掩盖资源争抢。
- 本轮没有运行这两个数据集的正式付费模型实验。下面的一键准备命令不会调用模型 API，任何门禁失败即停止。

## 1. 先等另一窗口的评测完整结束

即使两个测试点之间 `docker ps` 暂时为空，也不能视为整场评测结束。
启动脚本额外检查当前活动的评测容器，只读检查，不会停止其他任务。
本机默认只运行一个评测容器：MBPP+ `--parallel 1`，LCB 顺序执行。

## 2. 一键冻结版本并完成最终门禁（无 API 费用）

在项目主目录运行：

```bash
cd "/Users/even/Desktop/犀牛鸟/实战阶段/tracejudge-hy3"
.venv/bin/python scripts/prepare_external_benchmarks.py
```

该命令检查无其他评测容器、冻结独立运行代码，随后依次完成 MBPP 四项真实冒烟和 120 题预检、LCB60 选择锁验证和五项真实冒烟及预检。
仅全部成功后打印 `[ready] Both cohorts passed real container gates` 和正式运行命令。
看到 `[blocked]` 时先解决报告的问题，不要跳过门禁或手工修改 `ready`。

只准备 MBPP+（不运行或改写 LCB 的选择/预检记录）：

```bash
.venv/bin/python scripts/prepare_external_benchmarks.py --dataset mbpp
```

该路径成功标志为 `[ready] MBPP passed real container gates`。四项冒烟为正确答案、错误答案、函数调用内无限循环、顶层无限循环；同时核对 Base/Extra 状态，基础设施错误不能算冒烟通过。函数调用内无限循环在 `Mbpp/2` 的 Base 预期为 `fail`，Extra 允许官方 `fail` 或 `timeout`，但绝不允许 `pass`；顶层无限循环两组均须为 `timeout`。
当前凭证是 `artifacts/benchmark-readiness/mbpp.json`；旧凭证原始字节保存在相邻 `history/` 中。120 题的容器核验凭证位于 `<runtime>/artifacts/benchmark-preflight/mbpp/receipt.json`，包含数据、冒烟、执行代码哈希和资源限制。预检只检查 HY3 配置是否存在，不请求远端，因此不证明密钥、余额或网络可用。

独立快照位于 `artifacts/benchmark-runtime/<freeze_id>/`，拥有自己的 clean Git commit。
源文件逐字节绑定，**不会暂存或提交主工作树**，不复制 `.env`、原始数据或实验输出。
指针位于 `artifacts/benchmark-runtime/current.json`，完整源文件清单在快照 `FREEZE.json`。
正式实验使用快照源码和主目录 `.venv`，从主目录 `.env` 读取配置；请勿更新依赖或更换模型配置后续跑。

## 3. 分别启动正式实验

每次打开终端先设置以下变量，保持当前目录在主项目，以便读取已有 `.env`：

```bash
cd "/Users/even/Desktop/犀牛鸟/实战阶段/tracejudge-hy3"
TJ_MAIN="$PWD"
TJ_PY="$TJ_MAIN/.venv/bin/python"
TJ_RUNTIME=$("$TJ_PY" -c 'import json; print(json.load(open("artifacts/benchmark-runtime/current.json"))["runtime_root"])')
```

先运行 MBPP+ 120（默认串行容器）：

```bash
"$TJ_PY" "$TJ_RUNTIME/scripts/run_mbppplus.py" \
  --project-root "$TJ_MAIN" --run-id mbpp120-v1 \
  --confirm-real-provider --parallel 1
```

等 MBPP+ 完整结束后，再运行 LiveCodeBench 60：

```bash
"$TJ_PY" "$TJ_RUNTIME/scripts/run_livecodebench.py" \
  --project-root "$TJ_MAIN" --run-id lcb60-v1 \
  --confirm-real-provider
```

也可先单独 `--phase generate`，之后同一 run ID 加 `--phase execute --resume` 执行；执行阶段不需要 `--confirm-real-provider`。
虽然生成阶段不运行候选容器，入口仍会先验证容器条件，以免付费生成后才发现无法评测。
正式入口要求 v2 四项冒烟凭证，并重新核对每项实际状态、固定镜像和执行源码（包括共享解析器与冒烟脚本）；单独修改 `ready` 或复用旧版三项凭证不能放行。

## 4. 断网 / 中断后的恢复

保存启动时打印的**实际快照路径**。如果后续重新冻结出新版本，不要重新读取新指针续跑旧实验；使用旧快照路径。
不删原结果，不改模型/温度/预算/数据/镜像/超时。相同命令加 `--resume`：

```bash
"$TJ_PY" "$TJ_RUNTIME/scripts/run_mbppplus.py" \
  --project-root "$TJ_MAIN" --run-id mbpp120-v1 \
  --confirm-real-provider --parallel 1 --resume

"$TJ_PY" "$TJ_RUNTIME/scripts/run_livecodebench.py" \
  --project-root "$TJ_MAIN" --run-id lcb60-v1 \
  --confirm-real-provider --resume
```

仍然分开运行，不要把两个恢复命令一起启动。

- 已成功生成的候选不会重新生成；有限次 provider/parse 失败允许补跑，历史尝试保留。
- 已有官方执行结论（包含 WA/TLE/RE/CE）不会为了提高分数重新执行；只补基础设施失败或未执行项。
- MBPP 的阶段二入口严格要求 120 个生成成功的候选。若覆盖率不足，保存阶段一结果并返回退出码 2，续跑补齐后再进入阶段二。
- LCB 按题原子 checkpoint，带哈希链；重启后核对数据、源码、provider 配置、镜像与限制，不匹配就拒绝续跑。
- MBPP 旧 `phase1-mbpp/...0471296d55d4` 的 95 成功/25 provider 失败属于旧 dirty 代码版本。保留作探索性材料，不拼接进本轮正式 120 题 run，也不要在新版本上强制续跑。

## 5. 产物与追加报告

所有正式结果在**所用快照**内部，不在主项目的旧实验目录：

```text
<runtime>/artifacts/experiments/
  mbppplus/mbpp120-v1/
    generation/mbpp120-v1/{manifest,summary}.json
    execution/mbpp120-v1/{manifest,summary}.json
    report.json
  livecodebench/lcb60-v1/
    manifest.json
    events/000000.json ...
    report.json
    completion_receipt.json
```

LCB 的 events 含私有模型输出与候选，只在本地留存；不要把整个实验目录公开提交。
官方隐藏测试只在容器中解码/运行，公开报告仅发布聚合统计。

生成可追加到现有双数据集报告的 Markdown（离线，无 API）：

```bash
"$TJ_PY" "$TJ_RUNTIME/scripts/report_external_benchmarks.py" \
  --mbpp-run "$TJ_RUNTIME/artifacts/experiments/mbppplus/mbpp120-v1" \
  --lcb-run "$TJ_RUNTIME/artifacts/experiments/livecodebench/lcb60-v1" \
  --output "$TJ_MAIN/artifacts/reports/mbpp_lcb_results_v1.md"
```

输出文件已存在时拒绝覆盖；需要修订时使用新版本文件名。也可只给一个 run。
表中分别报告：生成成功/原始 N、base+plus 或 LCB 官方通过/原始 N、通过/实际执行 N、基础设施/未执行计数及 Wilson 区间。
固定分层的 LCB60 不等于自然难度分布，更不是完整排行榜成绩；两项代码生成结果不能直接证明过程评估质量。

## 镜像与恢复准备

已安装的 EvalPlus 镜像：
`ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740`（linux/amd64）。

本机 LCB 镜像的不可变本地 ID：
`sha256:2afa62ef77c6a26c119d41a79df18528668b873ccc840564bf7ffce360d43e55`（linux/amd64）。
镜像 ID 不等于 registry manifest digest，不能改写成 `tracejudge-lcb-checker@sha256:2afa...`。
本地构建的 `RepoDigests` 可能为空，应由 `.Id` 校验。

若换机器或镜像被清理，先拉取固定 EvalPlus 镜像并从已校验的 checker checkout 重建 LCB，重新运行冒烟和冻结；不要把新镜像用于旧 run 的续跑。
