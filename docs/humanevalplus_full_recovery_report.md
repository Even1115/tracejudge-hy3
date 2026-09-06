# HumanEval+ 全量 164 题：补充执行与结果报告

完成日期：2026-09-06。状态：**164/164 均有有效执行结果，基础设施错误已清零**。

## 1. 最终结果

| 指标 | 结果 |
| --- | --- |
| 候选数 / 有效执行题数 | 164 / 164 |
| HumanEval Base 通过 | **163/164（99.39%）** |
| HumanEval Base+Extra 通过 | **157/164（95.73%）** |
| 错误答案或候选异常 | 7 |
| 官方 timeout | 0 |
| 基础设施错误 | 0 |

这是固定 164 份已有候选代码的全量单候选功能正确性结果，不是 TraceJudge 自评准确率，也不代表官方排行榜认证。本次没有调用混元或其他模型 API，没有重新生成或修改候选代码。

Extra 测试使 6 道 Base 通过的题目暴露失败。这说明扩展测试确实补充了基础测试的检查范围；本报告不据此推断模型间显著差异。

## 2. 三个来源如何组成 164 题

| 来源 | 题数 | 处理方式 |
| --- | ---: | --- |
| 原阶段二有效执行 | 161 | 原样保留全部结果，包括 7 条失败判定 |
| 已完成的串行诊断 | 2 | 核验并复用 HumanEval/103、104，不重复执行 |
| 本次缓存修复后的执行 | 1 | 仅执行 HumanEval/15，Base 与 Extra 均通过 |

第 15 题容器退出码 **0**，`OOMKilled=false`，容器运行 **42.748 秒**，包含管理开销耗时 **44.802 秒**；未触及原 180 秒外层超时，容器已清理。

这是一个明确标记的跨批次派生汇总，未伪装成原 run 的续跑。原阶段一生成结果、原阶段二六个文件、原诊断输入均保持不变；旧报告仍保留其历史状态。

## 3. 缓存修复与实验边界

第 15 题原错误是 EvalPlus 写入参考答案 pickle 缓存时触发 `OSError 27 / File too large`。修复使用显式启用的 `gzip-stream-v1`：只对私有临时目录中形如 `<hash>.pkl` 的参考答案缓存进行流式压缩，pickle 的逻辑读写内容保持不变。

以下约束均未放宽：

- 128 MiB 单文件软/硬上限及结果大小检查；
- 1 GiB tmpfs、4 GiB 容器内存限额、1 CPU、128 个进程上限；
- 180 秒外层超时、官方内部测试参数、隐藏测试与题目集合；
- 禁网、只读根文件系统、固定镜像、官方结果强制写入与完整性校验。

压缩默认关闭，仅本次第 15 题补充执行显式启用。新的实现 hash、源码快照、执行器差异已写入补充 manifest。没有改动共享 benchmark 契约或 MBPP+/LiveCodeBench 文件。

固定镜像：`ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740`。
固定 EvalPlus：`0.4.0.dev2`，commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`；HumanEval+ 数据版本 `v0.1.10`。

## 4. 保留的失败结果

| 题目 | Base | Extra |
| --- | --- | --- |
| HumanEval/32 | fail | fail |
| HumanEval/39 | pass | fail |
| HumanEval/91 | pass | fail |
| HumanEval/99 | pass | fail |
| HumanEval/124 | pass | fail |
| HumanEval/132 | pass | fail |
| HumanEval/151 | pass | fail |

这 7 条均来自原有效执行，本次没有重跑、替换或筛选。固定 EvalPlus 原始状态不能进一步可靠区分“错误答案”与“候选运行异常”；也未在公开报告中披露具体隐藏测试。

## 5. 证据与离线复核

补充目录：`artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/`。

- [生成的结果报告](../artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/report.md)
- [全量指标](../artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/summary.json)
- [来源、配置与哈希 manifest](../artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/manifest.json)
- [每题来源记录](../artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/provenance.jsonl)
- [独立离线复核记录](../artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1/verification.json)

离线复核确认：164 个题号完整、唯一且按数字排序；原 161 条结果内容不变；所有输入、输出和源码快照 hash 一致；候选 hash、原始结果、安全结果、汇总指标相互一致。原始结果 bundle 仍是私有评测数据，位于 Git 忽略目录中。

从仓库根目录再次复核（不调用 Docker 或模型）：

```bash
.venv/bin/python scripts/verify_humanevalplus_recovery.py \
  artifacts/experiments/phase2-humanevalplus-full-recovery/full-164-recovery-20260906-v1
```

回归验证：**273 passed、3 skipped**，涵盖本次压缩与合并测试、HumanEval+/EvalPlus、MBPP+ 相关测试及共享契约；新增压缩测试包括在真实低文件上限下写入超过该上限的逻辑数据并完成无损读回。

## 6. 解释限制

- 第 103、104 题原诊断未当场保存 raw 文件 hash，本次导入时首次冻结；使用原候选 hash、当时安全判定和容器证据交叉核验，不能称为历史防篡改证明。
- 汇总跨多个执行批次，第 15 题还改变了临时缓存编码；各题耗时不适合直接做严格性能比较。
- 每题只有一份候选，未估计多次随机生成的方差；公开基准可能存在训练污染。
- 本报告回答已有候选的功能正确性问题；CodeJudge-Eval judge-only 的标签判定能力属于另一项实验，应分别解读。
