# MBPP+ 120 题：正式实验前准备验收

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: run（仅准备与验证）
- Origin Date: 2026-09-06
- Verification Status: VERIFIED（仅本地准备与真实容器预检，不含正式模型实验）
- Version Label: mbpp_preparation_v1

## 结论

**实验前准备通过，可以使用下方固定快照启动 MBPP+ 120 题正式实验。**

本轮没有调用 HY3 API，没有生成正式候选，也没有产生 MBPP+ 正式通过率。HY3 配置存在，模型、端点指纹、推理设置、超时、重试和解析修复配置与已完成的 HumanEval+ 阶段一一致；未向远端验证密钥、额度或服务可用性。

本轮只准备 MBPP+，没有运行或改写 LiveCodeBench 的选择锁、冒烟或正式实验。后续报告仍须区分代码生成正确性与过程错误定位能力。

## 超时差异的原因与修复

直接检查固定镜像内的 `evalplus/eval/__init__.py`，SHA-256 为 `76857b678cddca08dcaf54d7927b9a77826715cf657654ca5baf6c2b267f4c34`：

- `unsafe_execute` 捕获函数调用内的 `time_limit` 异常，将该测试标为失败；这一层可以产生官方 `fail`。
- `untrusted_check` 在评测子进程整体未完成时终止子进程，返回官方 `timeout`。
- 旧冒烟把函数内无限循环一律预期为 `timeout`，混淆了两层语义。修复的是冒烟协议和放行检查，不是官方判分、隐藏测试或候选结果。

新协议 `mbpp-real-smoke-v2` 的真实结果如下；四项基础设施错误均为零：

| 自编样例 | Base 实际状态 | Extra 实际状态 | 逐题容器耗时 | 门禁 |
|---|---|---|---|---|
| 正确答案 | pass | pass | 40.09 秒 | 通过 |
| 错误答案 | fail | fail | 32.96 秒 | 通过 |
| 函数调用内无限循环 | fail | timeout | 111.02 秒 | 通过 |
| 顶层无限循环 | timeout | timeout | 146.06 秒 | 通过 |

调用内无限循环的 Base 必须为 `fail`，Extra 允许官方 `fail` 或 `timeout`，不能为 `pass`；顶层无限循环的两组均必须为 `timeout`。正式入口核对四项覆盖、真实状态、版本、镜像及执行代码身份，不能只修改 `ready` 放行。

这也意味着正式报告中的官方 `timeout` 数量不能解释成“所有测试调用超时的总数”；`fail` 无法可靠细分为错误答案、候选异常或被捕获的逐测试超时。

## 准备凭证与冻结版本

- 完成时间：2026-09-06 13:50:56（北京时间）。
- 执行命令：`.venv/bin/python -u scripts/prepare_external_benchmarks.py --dataset mbpp`，退出码 0。
- 固定子集：120 题；官方容器逐一核对公开题面哈希及入口，`verified_task_count=120`。
- 冻结 ID：`910cc78164ccc5c0`。
- 独立快照 commit：`1956f013eacf35bdd04cfbedad3535964869c633`。
- 快照位置：`artifacts/benchmark-runtime/910cc78164ccc5c0/`；217 个冻结文件逐个校验通过，快照 Git 状态干净，当前依赖与冻结时一致。
- 主工作树未暂存、未提交；没有复制 `.env`。原有 HumanEval+ 修改和实验产物保留，164 题恢复报告再次通过输入、输出、原始记录及汇总一致性校验。
- 正式资源限制：容器并发 1、单容器 CPU 1、内存 4g、临时空间 1g、单题外层时限 180 秒、批次时限 14400 秒；没有为冒烟放宽限制。

| 凭证 | SHA-256 |
|---|---|
| [四项真实冒烟](../artifacts/benchmark-readiness/mbpp.json) | `1b09b1415621fad5ce58504fd243d3289b50f5d048cf6f7e04c66fb0ed5ee575` |
| [120 题官方预检](../artifacts/benchmark-runtime/910cc78164ccc5c0/artifacts/benchmark-preflight/mbpp/receipt.json) | `ccb8e37465f807d64d7c7299590a3b3ef41535f32a1e3240690263ec512e188c` |
| 子集 dataset manifest | `031ebeaecf4210722a5702f7f85daf3b710b766348703c4a579bfd574ffa32cd` |
| 子集公开题目文件 | `f49ba9dab45976da5fe995e28a2937839183db025a8819c9c6c20c598722c04f` |

旧失败冒烟凭证原始字节已归档至 `artifacts/benchmark-readiness/history/mbpp-481ca8b56dc8a3df7a1dff46a9b267916bf132421f06a5a633d814e5ebdc2db1.json`，未删除历史证据。私有测试、原始候选和凭据未写入本报告。

## 回归检查

- 最终 MBPP、启动入口、共享契约及 LCB 恢复相关测试：**103 passed**。
- 完整套件首轮：848 passed、3 skipped、4 failed、7 errors。异常中 3 项来自仍使用旧三项协议的启动器测试夹具，已同步四项协议；另外 8 项来自沙箱禁止 localhost 端口绑定，不是实验判分错误。
- 在更新夹具并允许本地端口后，启动器、新准备测试和演示服务相关复核：**37 passed**，覆盖上述全部 11 项异常。没有通过修改演示服务实现或放宽断言绕过失败。
- 修改范围内 `ruff check`、`ruff format --check` 及 `git diff --check` 通过。
- 共享 `contracts.py`、`humanevalplus.py`、`benchmark_contract_v1.md` 与契约测试未修改。预检结束后没有遗留运行容器。

## 下一步：正式启动（会调用付费 API，本轮未执行）

始终使用下面的实际快照路径，不要在续跑时重新读取可能已改变的 `current.json`。

```bash
TJ_MAIN="/Users/even/Desktop/犀牛鸟/实战阶段/tracejudge-hy3"
TJ_RUNTIME="$TJ_MAIN/artifacts/benchmark-runtime/910cc78164ccc5c0"
cd "$TJ_MAIN"

"$TJ_MAIN/.venv/bin/python" "$TJ_RUNTIME/scripts/run_mbppplus.py" \
  --project-root "$TJ_MAIN" --run-id mbpp120-v1 \
  --confirm-real-provider --parallel 1
```

这条命令会先生成 120 份候选，覆盖完整后执行官方 Base/Extra，并生成组合报告。保持当前配置、依赖及数据不变，不同时启动其他评测容器。

如断网或中断，保留原结果，在同一个快照、同一个 run ID 上续跑：

```bash
"$TJ_MAIN/.venv/bin/python" "$TJ_RUNTIME/scripts/run_mbppplus.py" \
  --project-root "$TJ_MAIN" --run-id mbpp120-v1 \
  --confirm-real-provider --parallel 1 --resume
```

成功生成项不重新生成；已有官方失败结论不为提高分数重跑，只恢复允许补跑的基础设施/未完成项。旧探索性 MBPP 95 成功/25 失败结果保留，不拼接进本轮正式 run。

最终组合结果位于 `<runtime>/artifacts/experiments/mbppplus/mbpp120-v1/report.json`。等生成与执行覆盖、基础设施错误及两组通过数核对完成，再追加到现有数据集报告；现在不填写任何正式成绩。
