# HumanEval+ 剩余三题：容器诊断与恢复决策

日期：2026-09-06（北京时间）。本记录是基础设施诊断，不是新的正式评测成绩。

## 结论

- **HumanEval/103、104：按原 180 秒外层超时，串行即可正常执行。** 本次 Base 与 Extra 均通过，无需重新生成代码或提高超时。此前并行运行的超时与临时资源争用相符，但缺少当时资源快照，不能确定其历史根因。
- **HumanEval/15：官方参考答案缓存写入触发 `OSError 27 / File too large`。** 当前容器的单文件软、硬上限均为 134217728 字节（128 MiB）。增加时间或盲目重试不能解决这项文件大小限制。
- **推荐独立、可追溯的补充恢复，不删除或覆盖原实验。** 保留原 161 条有效判定（包括 7 条失败判定）；先处理第 15 题的缓存限制，再补齐基础设施失败记录。无需调用模型 API。

## 1. 原实验与未修改范围

来源目录：
`artifacts/experiments/phase2-humanevalplus-full/phase2_20260905T145639966557Z_b6138841922a/`

原正式记录仍为：164 个候选、161 条有效执行、Base 160/161、Base+Extra 154/161、3 条基础设施错误，`evaluation_complete=false`。

诊断直接校验并复用原阶段一的 164 份生成结果与全量 manifest；只选择原阶段二的三个基础设施失败项。各轮诊断结束时，来源目录文件哈希均保持不变（`source_unchanged=true`）。未调用模型 API，未下载数据，未修改候选、隐藏测试、冻结 EvalPlus 源码或原正式结果。

## 2. 同参数串行复测

下表耗时为 Docker `StartedAt` 至 `FinishedAt`，不含容器创建和清理开销。全部状态均在清理容器之前读取；三题均自然退出，没有诊断程序主动 kill。

| 题目 | 容器退出码 | OOMKilled | 容器耗时 | 含管理开销耗时 | 结果 |
| --- | ---: | --- | ---: | ---: | --- |
| HumanEval/15 | 2 | false | 15.196 s | 16.058 s | `candidate_isolation_unavailable`；无有效判定 |
| HumanEval/103 | 0 | false | 29.181 s | 30.630 s | Base=pass，Extra=pass |
| HumanEval/104 | 0 | false | 28.061 s | 28.994 s | Base=pass，Extra=pass |

机器可读证据：[逐题退出/OOM/耗时](../artifacts/experiments/humanevalplus-diagnostics/pending3-serial-180-v1/report.json)。

诊断开始及各题开始前均检查没有其他运行中的容器。固定镜像 digest、候选 hash、官方执行参数不变；每题 1 CPU、4 GiB 容器内存限额、1 GiB tmpfs、128 个进程上限、180 秒外层超时。诊断期间只读资源检查显示 Docker VM 共 6 CPU、4108914688 字节内存，宿主为 arm64。资源检查不代表历史失败时的资源占用，`OOMKilled=false` 也不是“系统从未出现过内存压力”的证明。

## 3. 第 15 题的内部错误定位

原 wrapper 的内部子进程把所有异常折叠为退出码 73，父进程再映射为 `candidate_isolation_unavailable` 并退出 2。因此不能按错误名称直接断言隔离机制失效。

使用独立临时 wrapper 副本，仅细分异常报告：不改官方评测调用、不改测试、不输出异常原文、traceback、候选或隐藏测试。结果仅用于诊断，明确标记 `formal_scoring_eligible=false`。

| 诊断层次 | 安全错误标签 | 容器退出码 | OOMKilled | 容器耗时 |
| --- | --- | ---: | --- | ---: |
| 异常类别 | `diagnostic_executor_failed_oserror` | 2 | false | 19.086 s |
| 系统错误编号 | `diagnostic_oserror_27_file_too_large` | 2 | false | 13.661 s |
| 可信源码阶段 | `diagnostic_oserror27_groundtruth_cache` | 2 | false | 16.463 s |

阶段分类检查异常调用栈是否含固定 EvalPlus 模块的 `get_groundtruth`，只输出预定义标签。只读检查固定镜像的官方函数还确认：该函数通过 `pickle.dump(expected_output, f)` 写入 `.pkl` 参考答案缓存。结合容器 `--ulimit fsize=134217728:134217728`，证据指向参考答案缓存触及单文件上限，而非模型调用失败或候选已被判错。

证据：[异常类别](../artifacts/experiments/humanevalplus-diagnostics/task15-error-category-v1/report.json)、[系统错误编号](../artifacts/experiments/humanevalplus-diagnostics/task15-errno-v2/report.json)、[缓存阶段确认](../artifacts/experiments/humanevalplus-diagnostics/task15-cache-stage-v3/report.json)。前两版诊断脚本快照保存在各自目录，SHA256 与该轮报告记录一致。

未测量完整缓存最终大小，尚未验证新的文件上限取值。因此本报告不承诺简单设为某个更大数值即可完成，也不建议取消文件限制。

## 4. 正式恢复决策

1. **先处理第 15 题执行器的缓存限制。** 将可信临时缓存容量需求与对外发布产物的大小/完整性约束分开考虑；保留网络隔离、候选与测试不变、输出检查及资源边界。先做单题受控验证，再启动正式补充执行。不要通过截断测试、改候选或关闭结果完整性校验解决问题。
2. **103、104 使用原 180 秒上限串行补跑。** 本次已有有效诊断执行结果和原候选 hash，但尚未合入正式成绩；无需重跑其余 161 题。
3. **使用独立恢复记录。** 原实验记录的提交为 `10864227396fe82687998a1484e1ec17d939c709`、分支为 `phase4-p1-research-enhancements`；当前提交为 `67085adb75cb64d349f6cb89ded3f27356b5c967`、分支为 `benchmark/mbpp-lcb-wsl`。虽然 EvalPlus 实现 hash 仍同为 `5c8c81659109a441ccf1aecad6704c17e50689f2bf3a5eb0771dc7c34ce0d2ca`，现有续跑检查同时绑定 Git 和执行配置，因此不能直接在当前环境以旧 run_id 续跑，也不能编辑旧 manifest 冒充原配置。
4. **补充恢复应绑定原始来源。** 新记录需绑定旧 manifest、samples、results 的 hash，明确复用的 161 个 ID、补跑的 3 个 ID、新执行配置和实现版本。保留全部旧结果及失败尝试，只在派生汇总中替换基础设施错误；这项正式恢复/汇总功能尚未在本次诊断中实施。
5. **完整覆盖后再报告完整分母。** 第 15 题仍无有效判定，当前正式报告仍只能使用原 161 题执行结果；不能把诊断成功两题直接写成“164 题全量完成”。

## 5. 诊断实现与安全边界

新增两个独立诊断脚本及测试，不修改生产执行器。脚本只操作自身创建的精确容器名，清理前持久化状态；若超时需主动终止，会分别记录终止前、终止后的状态，防止将主动 kill 产生的 137 误记为 OOM。所有诊断候选与官方 raw 结果保留在忽略的实验目录中，不作为公开报告内容。

本次工作完成的是“记录证据并决定恢复方式”，不是正式恢复或执行器修复；原 161 条有效判定及 164 份生成结果继续保留。

验证：诊断与相关 EvalPlus 导出、解析、运行器、Docker、CLI 回归共 **215 passed、3 skipped**；其中新增诊断测试 9 项通过。四个新增 Python 文件的 Ruff 检查及格式检查通过。最后检查 Docker 无运行中容器，诊断容器均已清理。
