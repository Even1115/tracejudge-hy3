# TraceJudge-Hy3 阶段二自然研究子集实验报告

> 文档状态：正式执行中
> 协议版本：`phase2_research_natural_v1`
> 建立日期：2026-08-26
> 当前证据状态：阶段一、Mock 链和修复后的真实 Docker 验收已核验；首次正式 Docker run 在 preflight 阶段失败，候选实际执行数为 0；当前代码已通过固定镜像 preflight、42 题 cohort preflight 和单题官方 EvalPlus smoke，修复后的新正式 run 待执行
> 定稿条件：完成真实 Docker smoke、正式 EvalPlus 执行和产物完整性验收

## 1. 实验目的与证据边界

本实验评估固定 Hy3 配置在 HumanEval+ 自然研究子集上生成的单个候选，经过阶段一生成与严格结构化解析后，能否通过官方 EvalPlus Base 与 Extra 测试。实验同时记录从公开题面到可执行候选的 Pipeline Coverage，以及候选进入官方执行器后的功能结果。

本报告回答以下问题：

1. 固定 45 题来源中有多少题产生了可进入阶段二的结构化候选？
2. 这些候选在官方 Base 测试上的通过数量和比例是多少？
3. 这些候选在 Base+Extra 联合口径上的通过数量和比例是多少？
4. 执行过程中出现了多少候选 timeout、基础设施错误和容器清理失败？

本报告不提供完整 164 题 HumanEval+ 分数、不计算标准多样本 pass@k、不构成官方排行榜结果，也不用于单独证明 TraceJudge 过程评估器的研究有效性。

## 2. 实验状态

| 环节 | 状态 | 证据 |
| --- | --- | --- |
| 45 题自然研究数据集 | 已完成并核验 | schema v2、固定 seed、排除 10 题 Pilot |
| 阶段一真实 Hy3 生成 | 已完成并核验 | 42 success、0 parse error、3 provider error |
| 阶段二导出门槛 | 已通过 | 42 ≥ 预设最低成功数 30 |
| 当前代码真实 Docker 验收 | 已通过 | 2026-08-27 真实标记测试 `3 passed, 50 deselected in 50.74s`；覆盖固定镜像 preflight、42 题 cohort preflight 和单题官方执行 smoke |
| 阶段二 Mock 产物链预检 | 已通过 | run `phase2_20260827T061539511466Z_8529bad881a6`；45/42/0/3 一致，不作为功能证据 |
| 阶段二真实 EvalPlus 执行 | 首次尝试失败，待新建 run | run `phase2_20260827T065923524982Z_ad32756614a7` 在 preflight 阶段因仅支持 10 题的旧协议失败；0 题实际执行 |
| 阶段二产物完整性验收 | 首次失败 run 已留档；最终验收待执行 | 失败 run 的脱敏 manifest、summary、results 和 execution log 哈希已记录 |

## 3. 数据集与抽样协议

### 3.1 数据来源

| 字段 | 固定值 |
| --- | --- |
| 数据集 | HumanEval+ |
| 固定 revision | `d32357cf319e50e9c8d8dab5ea876c72b0fd321b` |
| 数据 manifest schema | `2` |
| 实验标签 | `humanevalplus_45_public_prompt_generation_research_natural` |
| selection role | `research_natural` |
| 选择算法 | `sha256(seed\0problem_id)-lowest-v1` |
| seed | `20260825` |
| 来源题数 | 45 |
| Pilot 排除数 | 10 |
| 公开投影 SHA256 | `701ed34b3a66032f0f356734607709fb3d65f753dbe01cf4b4395c4409df2dc0` |
| 有序题号 SHA256 | `dd49434ed84260eb724d5ee31bad492beaf3f72bc3210da4e046e732fac3976e` |
| 被排除题号 SHA256 | `5dd5b86895b7526057401e404df794e460f7e725a851df8f40ea8d6e8fe7c111` |
| dataset manifest SHA256 | `686be1bd21657e341309a23d7ce8927bc7046c2ff18b0079d4663d203c51b809` |

阶段一只读取公开题面、函数签名和 entry point。`canonical_solution`、Base/Extra 测试正文及官方失败输入不进入 Hy3 Prompt。

### 3.2 样本冻结规则

- 45 题在任何阶段二结果产生前已经由固定算法和 seed 冻结。
- 10 题工程 Pilot 被确定性排除，不进入本次确认性自然研究 cohort。
- 阶段二不得依据候选功能结果重新抽题或修改顺序。
- 阶段一正常完成后不得为了提高成功数而反复 resume。
- 阶段一成功数低于 30 时，实验停止，不降低门槛、不重新抽样。

## 4. 阶段一来源与筛选

### 4.1 主阶段一运行

| 字段 | 结果 |
| --- | --- |
| run ID | `phase1_20260826T130038779522Z_5f55a45bb5e5` |
| 状态 | `completed` |
| Provider | `hy3` |
| 模型 | `tencent/hy3` |
| reasoning effort | `high` |
| 最大普通重试数 | 2 |
| 最大 JSON repair 数 | 1 |
| Git commit | `4778a8d30599b6e3df4a3330be202331a1559263` |
| Git dirty | `true` |
| manifest SHA256 | `d541d49e987016831b4f294077db70cde2c58766ed190c7ec34b427a3593ed04` |
| summary SHA256 | `8bfd96a78758b100c35e905e9413e9d9542c488208cbdd409850ce3d392c2397` |
| responses SHA256 | `6de19276a1897b7678f91fc601c70394ca96526c322eefa36a182b364f18ec4d` |

阶段一运行时工作树为 dirty 状态。该事实由 manifest 显式记录，报告不得将本次运行描述为来自干净冻结 commit。阶段二应继续绑定该阶段一来源 run、manifest 和候选代码哈希。

### 4.2 阶段一结果

| 指标 | 原始数量 | 比例/说明 |
| --- | ---: | --- |
| 来源题目 | 45 | 固定分母 |
| 最终 success | 42 | 42/45 = 93.33% |
| 最终 parse error | 0 | 0/45 = 0.00% |
| 最终 provider error | 3 | 3/45 = 6.67% |
| 首次调用即解析成功 | 41 | 41/45 = 91.11% |
| 遇到解析失败 | 0 | 未触发 JSON repair |
| repair attempted | 0 | 无结构化修复调用 |
| repair success | 0 | 不适用 |
| terminal parse error | 0 | 0/45 = 0.00% |
| 平均尝试次数 | 1.1556 | 按 45 题计算 |
| 平均重试次数 | 0.1556 | 按 45 题计算 |
| 平均耗时 | 53.1128 秒 | 不含 skipped；本次 skipped 为 0 |

阶段一的 `parse_success_rate=100%` 使用 42 个取得可解析输出的题目作为分母，即 42/42。端到端生成覆盖率必须报告为 42/45，而不能写成 45/45。

### 4.3 未达到门槛的早期运行

| 字段 | 记录 |
| --- | --- |
| run ID | `phase1_20260826T020458520423Z_fec4cd346876` |
| 分类 | `excluded_provider_failure_run` |
| success | 4/45 |
| parse error | 0/45 |
| provider error | 41/45 |
| Provider 失败调用 | 123 |
| 排除理由 | success_count 低于预设最低门槛 30 |
| 是否进入阶段二 | 否 |
| manifest SHA256 | `f0d4bb5618c4b7adfd8119dc27279fd73097585e1f817eccf947dbcb40ac16f1` |
| summary SHA256 | `f8dcb75110c5ccd80f9028493564f6d12264368856c10c622119ad7ace060f3d` |

该 run 与主 run 使用相同数据集、模型、公开 Provider 配置、Git commit 和 working-tree 指纹。41 个终态失败均为 `ProviderResponseError`。这一聚合证据显示其不满足阶段二准入条件，但不足以进一步断言具体服务故障类型。该 run 保留用于审计，不进入阶段二功能结果，也不从实验历史中静默删除。

## 5. 阶段二执行协议

### 5.1 输入选择

| 参数 | 固定值 |
| --- | --- |
| baseline run | `phase1_20260826T130038779522Z_5f55a45bb5e5` |
| selection policy | `phase1-success-only` |
| minimum success count | 30 |
| 来源题数 | 45 |
| 预期成功导出数 | 42 |
| 排除 parse error | 0 |
| 排除 provider error | 3 |

阶段二只导出每题唯一历史 `success` 记录中的 `solution_trace.code`，不使用 `raw_output` 代替代码。导出后必须验证：

```text
42 + 0 + 3 = 45
```

### 5.2 官方执行器身份

| 字段 | 固定值 |
| --- | --- |
| executor | official EvalPlus Docker |
| image | `ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740` |
| platform | `linux/amd64` |
| EvalPlus package | `0.4.0.dev2` |
| EvalPlus source commit | `f11cfb92c1d52896a87f988cbebbd74727d56c7e` |
| Python | `3.11.10` |
| HumanEval+ release | `v0.1.10` |
| host parallel | 2 |
| official evaluator parallel | 1 |
| per-task outer timeout | 180 秒 |
| batch scheduling deadline | 5400 秒 |
| minimum time limit | 4.0 秒 |
| ground-truth time factor | 4.0 |
| test details | true |

阶段二不调用 Hy3、Direct Judge、四层 Judge 或现有全链路 pipeline。每个候选在独立容器中运行一次 Base+Extra 评测。

### 5.3 隔离和私有数据边界

- 容器无网络、只读根文件系统、capability drop、PID/CPU/内存/文件大小受限。
- 宿主等待容器退出后才读取两个精确输出文件。
- 官方 raw、samples 和具体失败输入只允许存入 Git-ignored 的 `0700` 运行目录和 `0600` 文件。
- 普通日志、CLI、报告和阶段三输入不得包含官方失败输入实值。
- 当前容器中候选与 wrapper 共享 UID，安全边界属于 `basic_non_adversarial`，不是主动对抗候选的完整防篡改证明。

## 6. 预设验收标准

以下条件在查看阶段二功能结果前固定：

1. 当前代码的真实 Docker 集成测试必须同时覆盖固定镜像 preflight、42 题研究 cohort preflight 和单题官方 EvalPlus smoke，结果为 `3 passed`，不能是 skipped。
2. 阶段二 executor 必须为 `docker`，不能以 Mock 结果代替。
3. `source_problem_count` 必须为 45。
4. `exported_success_count` 必须为 42，且不少于 30。
5. `excluded_parse_error_count` 必须为 0。
6. `excluded_provider_error_count` 必须为 3。
7. `result_count` 必须等于 42。
8. `mock_not_executed_count` 必须为 0。
9. `infrastructure_error_count` 必须为 0。
10. `container_cleanup_failed_count` 必须为 0。
11. `evaluation_complete` 必须为 true。
12. manifest、summary、results、execution log 和 raw bundle 必须通过最终哈希校验。

任何硬门槛失败都必须如实记录。基础设施错误不能计作候选代码失败，也不能从分母中静默删除后声称实验完整。

首次正式 preflight 失败后新增一项矫正性门槛：修复后的当前 commit 必须通过原有镜像 preflight、单题官方 smoke 和新增的 42 题研究 cohort preflight，即显式 Docker 标记测试应为 `3 passed`。新增项只校验公开任务身份，不执行候选代码。该门槛是针对已观测基础设施缺陷的修复验收，不是查看功能结果后修改评价指标。

## 7. 阶段二结果

### 7.1 Docker smoke 与修复后验收

| 指标 | 结果 |
| --- | --- |
| 执行日期 | 2026-08-27 |
| 固定镜像 preflight | 通过（10题 Pilot 公开身份） |
| 单题真实官方 EvalPlus smoke | 通过 |
| 修复前测试结果 | `2 passed, 42 deselected in 49.41s` |
| 修复前覆盖缺口 | 该 smoke 仅覆盖 10 题 preflight，未覆盖 42 题研究 cohort；此缺口导致首次正式 run 才暴露固定 10 题限制 |
| 修复后当前代码结果 | `3 passed, 50 deselected in 50.74s` |
| 修复后覆盖范围 | 固定镜像与运行时身份 preflight；正式 42 题 `research-natural` cohort preflight；单题官方 EvalPlus Base+Extra 执行 smoke |

修复后的三项真实 Docker 测试已全部通过。其中 42 题 cohort preflight 只校验公开任务身份和数据一致性，不执行 42 个候选；单题 smoke 用于确认固定 EvalPlus 执行路径可用。因此，该 `3 passed` 是正式运行前的工程验收证据，不是 42 题功能评测结果。

### 7.2 首次正式执行尝试（基础设施失败，排除功能结果）

| 字段 | 结果 |
| --- | --- |
| run ID | `phase2_20260827T065923524982Z_ad32756614a7` |
| 创建时间 | `2026-08-27T06:59:23.738Z` |
| 完成时间 | `2026-08-27T06:59:23.955Z` |
| experiment label | `humanevalplus_42_of_45_evalplus_execution_research_natural` |
| execution mode | `docker` |
| preflight | `failed` / `executor_error` |
| 阶段二结果记录 | 42 |
| 实际官方执行 | 0 |
| 基础设施错误 | 42（同一 preflight 错误向所有待执行题目的安全映射） |
| 容器清理失败 | 0 |
| `evaluation_complete` | `false` |

第一次正式阶段二尝试在候选执行前失败：42 个待执行候选均被统一标记为 `executor_error`，`actual_execution_count=0`，原因是 Docker preflight 的宿主与容器协议仍固定要求恰好 10 题。本次不产生 Base、Base+Extra 或任何功能正确率，不得记为 0% 通过率。

修复将 preflight 边界改为接受 1—164 个唯一 HumanEval+ 公开任务身份，并为 42 题 cohort 增加回归测试。由于修复会改变实现指纹，失败 run 保留用于审计，修复后必须在干净新 commit 上创建新的正式 run，不对该 run 执行跨实现指纹续跑。

### 7.3 最终正式执行概况（待新 run）

| 指标 | 原始数量 | 分母/说明 |
| --- | ---: | --- |
| 阶段一来源题目 | 45 | 固定来源 cohort |
| 阶段一成功导出 | 42 | 42/45 = 93.33% |
| 实际官方执行 | `TBD` | 目标 42 |
| Base pass | `TBD` | 分母为实际官方执行数 |
| Base fail | `TBD` | 分母为实际官方执行数 |
| Base+Extra pass | `TBD` | 必须同时满足 Base 与 Plus 为 pass |
| Plus fail | `TBD` | 分母为实际官方执行数 |
| timeout | `TBD` | 候选执行结果 |
| wrong answer or candidate exception | `TBD` | 固定 raw 无法进一步可靠细分 |
| infrastructure error | `TBD` | 不计作候选失败 |
| container cleanup failed | `TBD` | 必须为 0 才通过硬验收 |
| resume reused | `TBD` | 如未续跑则为 0 |

### 7.4 通过率

| 指标 | 数值 | 计算式 |
| --- | --- | --- |
| Pipeline Coverage | 93.33% | 42/45 |
| Base pass rate | `TBD` | Base pass / 实际官方执行数 |
| Base+Extra pass rate | `TBD` | Base+Extra pass / 实际官方执行数 |
| 来源 cohort 到 Base pass | `TBD` | Base pass / 45 |
| 来源 cohort 到 Base+Extra pass | `TBD` | Base+Extra pass / 45 |

Base 和 Base+Extra 的条件通过率必须与 45 题来源 cohort 的端到端比例并列报告。只报告 42 个阶段一成功候选中的通过率会产生成功条件化偏差。

### 7.5 逐题脱敏结果

逐题表只允许从阶段二 `results.jsonl` 的安全字段生成，不得复制 EvalPlus raw 或失败输入。

| problem ID | Base status | Plus status | passed Base | passed Base+Extra | observed Base fail count | observed Plus fail count | duration | error type |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## 8. 基础设施与异常记录

| 事件 | 数量 | 是否影响完整性 | 处理 |
| --- | ---: | --- | --- |
| preflight failure | 1 | 是 | 已定位为旧协议恰好 10 题的限制；修复后新建正式 run |
| preflight 错误映射的 `executor_error` 结果 | 42 | 是 | 仅表示统一 preflight 失败，不是 42 次独立候选执行失败 |
| batch deadline not started | 0 | 是 | 首次尝试未进入任务调度 |
| batch timeout | 0 | 是 | 首次尝试未进入任务调度 |
| container cleanup failed | 0 | 是 | 首次尝试未观测到清理失败 |
| transport/parser infrastructure error | 0 | 是 | 首次尝试未进入 raw 传输或解析 |
| candidate timeout | 0 | 否 | 没有候选实际执行 |

如发生续跑，应记录每次 invocation 的时间、原因、实际执行数和 reused 数。不得通过创建新 run 隐藏原 run 的基础设施错误。本次因修复改变实现指纹而必须新建 run，但失败 run ID、原因、脱敏计数和哈希仍在本报告中保留。

## 9. 产物与可复现性

### 9.1 阶段二运行身份

| 字段 | 值 |
| --- | --- |
| phase2 run ID | `TBD` |
| experiment label | `TBD（预期：humanevalplus_42_of_45_evalplus_execution_research_natural）` |
| execution mode | `TBD（必须为 docker）` |
| created at | `TBD` |
| completed at | `TBD` |
| host system | `TBD` |
| host architecture | `TBD` |
| image ID | `TBD` |
| runtime dataset SHA256 | `TBD` |
| implementation SHA256 | `TBD` |

### 9.2 最终产物哈希

| 文件 | SHA256 |
| --- | --- |
| `manifest.json` | `TBD` |
| `summary.json` | `TBD` |
| `results.jsonl` | `TBD` |
| `execution.log` | `TBD` |
| `samples.jsonl` | `TBD（只记录哈希，不公开内容）` |
| `evalplus_raw_results.json` | `TBD（只记录哈希，不公开内容）` |

### 9.3 首次失败 run 的脱敏证据哈希

| 文件 | SHA256 |
| --- | --- |
| `manifest.json` | `be3e57797418e86b8e82af5aa37e7d814eb5ea94d0ca65d287beae1a95825d44` |
| `summary.json` | `df07cc11f4f7d52695344308090403b94d7c6d4fbf1fb184bd346f5034306ea0` |
| `results.jsonl` | `a149a5bc75275f562867e90622f16a3da34b2733f09a4ef3ead2c79ade2606fd` |
| `execution.log` | `fdf90bf02a57870757cac8f6f83791a5574c01833211c734ba210dd32a262411` |

本表只固化失败 run 的脱敏审计产物。`samples.jsonl` 和 `evalplus_raw_results.json` 仍保持 evaluation-only，不读取、不展示，也不在此处用作功能证据。

### 9.4 本地证据路径

```text
artifacts/datasets/processed/humanevalplus-research-natural-45/
artifacts/experiments/phase1-research-natural/phase1_20260826T130038779522Z_5f55a45bb5e5/
artifacts/experiments/phase2-research-natural/phase2_20260827T065923524982Z_ad32756614a7/
artifacts/experiments/phase2-research-natural/<phase2_run_id>/
```

所有运行产物均应保持 Git ignored。报告可以保存安全哈希和脱敏计数，但不得提交官方 raw、samples、具体失败输入或未审查的模型原始输出。

## 10. 结果解释规则

允许的结论：

- 固定 45 题来源中有 42 题产生了可进入官方执行器的结构化候选。
- 在固定模型、Prompt、参数和单候选配置下，报告实际 Base 与 Base+Extra 原始通过数。
- 描述候选 timeout、基础设施错误和执行稳定性。
- 将本次结果作为阶段三自然轨迹研究子集的功能证据来源。

禁止的结论：

- 将结果称为完整 HumanEval+ 成绩或官方排行榜成绩。
- 将单候选结果写成标准多样本 pass@k。
- 用 42 个成功候选的条件通过率代表全部 45 题端到端表现。
- 从本次小规模固定子集推导普遍的模型能力或难度规律。
- 将 EvalPlus `fail` 擅自细分为错误答案、语法错误或运行异常。
- 将 Provider 失败或基础设施失败伪装成候选代码失败。

## 11. 局限性

1. 阶段二只执行阶段一成功的 42 个候选，功能结果存在成功条件化；报告通过并列给出 42 题条件分母和 45 题来源分母缓解这一问题。
2. 每题只有一个自然生成候选，不能计算标准多样本 pass@k。
3. 45 题不是完整 164 题 HumanEval+，不能代表完整基准。
4. HumanEval+ 是公开基准，无法排除训练污染或记忆影响。
5. 固定 EvalPlus raw 的 `fail` 合并错误答案和候选异常，无法可靠细分 execution error。
6. `test_details=true` 保存的是官方已观测失败输入；timeout 等情况下不保证数量等于所有理论失败用例总数。
7. 当前 Docker 边界属于 `basic_non_adversarial`，不提供主动恶意候选下的强完整性保证。
8. 阶段一主 run 的 Git 工作树为 dirty，虽然 manifest 保存了 working-tree 指纹，但复现解释弱于干净 commit。
9. 在相同公开配置下曾出现一次大规模 `ProviderResponseError` 运行，说明外部 Provider 可用性会显著影响 Pipeline Coverage。

## 12. 数据管理与伦理声明

本实验不涉及人类参与者或个人敏感数据。阶段一只向 Hy3 提交公开题面，阶段二候选代码只在受限官方 EvalPlus 容器中执行。HumanEval+ 官方答案、Base/Extra 测试正文、官方失败输入、EvalPlus raw 和未脱敏模型原始输出均按 evaluation-only 数据管理，不进入公开报告或阶段三 Judge 输入。

## 13. 待完成清单

- [x] 当前代码版本真实 Docker preflight 通过（含 42 题研究 cohort preflight）。
- [x] 当前代码版本真实单题 EvalPlus smoke 通过。
- [x] Mock 导出确认 45/42/0/3 统计一致。
- [x] 首次正式 run 的 preflight 失败、0 题实际执行和 42 条 `executor_error` 映射已留档。
- [ ] 在支持 1—164 题 preflight 的干净新 commit 上新建正式 run。
- [ ] 正式阶段二 Docker run 完成。
- [ ] 验证阶段二 experiment label 不含 Pilot 身份。
- [ ] 验证实际结果数为 42。
- [ ] 验证基础设施错误和清理失败均为 0。
- [ ] 填写 Base 与 Base+Extra 原始计数和双重分母。
- [ ] 填写逐题脱敏结果。
- [ ] 固化阶段二六类产物哈希。
- [ ] 完成限制、结论和可复现性复核。

## 14. 结论

当前只能得出工程性结论：45 题固定来源中有 42 题通过阶段一准入；首次 42 题正式 preflight 因旧协议的固定 10 题限制失败，该 run 的 `actual_execution_count=0`，不产生功能正确率。修复后的当前代码已通过固定镜像 preflight、42 题研究 cohort preflight 和单题官方 EvalPlus Base+Extra smoke，但新的 42 题正式 Docker run 仍待执行。

`TBD：最终研究结论仅在修复后的新阶段二 Docker run 和完整性验收结束后填写。结论必须同时报告阶段一 42/45 Pipeline Coverage、阶段二实际执行分母、Base 原始通过数和 Base+Extra 原始通过数。`
