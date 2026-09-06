# CodeJudge-Eval judge-only 外部迁移：full-v1 正式实验报告

| 字段 | 值 |
| --- | --- |
| 实验 ID | `codejudge-eval-judge-only-v1` |
| 运行 ID | `full-v1`（2026-09-05，约 59.5 分钟） |
| 契约 | benchmark contract v1（FROZEN，未修改） |
| 适配器 | `codejudge-eval-judge-only` v1 |
| Git | `78c46e507591eebe53d8643ae6f2d166869c0965`（dirty=True，含本集成未提交代码） |
| 运行目录 | `data/codejudge_eval/runs/full-v1/` |

> **修订说明（2026-09-05，scoring layer v2）**：本文所有"重算"数字来自
> `runs/full-v1/report_analysis_v2.json`——由原始 `records.jsonl` 用修正后的
> 评分层离线重算的派生文件；历史原件（manifest/provider_raw/records/report）
> 字节不变。修订要点：gold 分布极不均衡（AC=4、非 AC=86），raw accuracy 与
> majority baseline 完全相同；旧 `verdict_macro_f1=0.688675` 的算法把
> 无预测的 `NOT_AC_UNSPECIFIED` 类从宏平均中跳过（实际只平均 6 类却报 7 类），
> 且标签空间本身不兼容，已从结论中撤下；错误类型"一致率 29.8%"混淆了执行结果
> 与根因 taxonomy，改用 verdict 推导的 execution-outcome 指标。

## 1. 目的与边界

使用 CodeJudge-Eval 的既有题目、候选代码与错误标签，独立外部验证 TraceJudge
（Hy3 backend）的**代码正确性判断、误报率、错误类型与难度泛化**。

- 本实验**不是**生成能力评测；所有候选均为数据集提供
  （`CandidateOrigin.DATASET_PROVIDED`，`EvaluationMode.JUDGE_ONLY`）。
- 数据集没有 Hy3 原生解题轨迹，因此**禁止报告**过程正确率、首个推理步骤定位、
  错误证书重放成功率；所有 `BenchmarkJudgeRecord` 的 process/first-step/certificate
  字段恒为 `None`。
- 外部数据从未用于调 Prompt 或阈值；Prompt 在任何 held-out 调用前冻结并记录 hash。

## 2. 数据来源（2026-09-04 核验）

- 论文：*CodeJudge-Eval: Can Large Language Models be Good Judges in Code
  Understanding?*（arXiv:2408.10718，COLING 2025）。
- 代码仓库 `CodeLLM-Research/CodeJudge-Eval`：MIT，HEAD `c168cd2…`（仅含
  README/LICENSE）。
- 数据 HuggingFace `CodeResearch/CodeJudge-Eval`：MIT，revision
  `6541deb0c42e56ec1300d94d4e9ae7b3b4cc764a`。
- 仅使用三个 0-shot 文件（1-shot 文件从不读取）：

| 文件 | 记录数 | sha256 |
| --- | --- | --- |
| CodeJudge_Eval_0shot_easy.json | 1860 | `9be68076…d1c7e5` |
| CodeJudge_Eval_0shot_middle.json | 1860 | `4690ec01…a63e70` |
| CodeJudge_Eval_0shot_hard.json | 1860 | `eb86bf28…f77581` |

`source_manifest_sha256 = 4759ceeec170ee3e4325a91f58e95301434e4a8a79436356dcd7c70b0371ef1f`

关键数据事实：三个文件共享同一批 457 道 APPS 题——"难度"是**评判粒度**
（easy 3 选项 / middle 6 选项 / hard 9 选项），而非不同题集。每条记录的选项语义
由其 `input` 字段的 Choices 段逐条校验（钉死的选项表），不一致即 fail-closed。

## 3. 选择协议（v2，已冻结）

`data/codejudge_eval/selection_v2.json`，
`selected_task_ids_sha256 = 53c99dc181d24e4cd9ce994b5c7e40f7b38fa0540c78def2bb990205d59caf82`。

算法（`SELECTION_ALGORITHM`）：按 `(task_id, data_id)` 数值排序；每题取
`data_id` 最小候选；单个 `random.Random(20240904)` 按 easy→medium→hard 顺序从
**未被占用**的题号中各 `sample(30)`（跨难度不放回，重叠 0）。

加载排除：task 51 的 2 个候选 ×3 文件共 6 条因真实界面无法判定被排除并计数
（`exclude_ambiguous_interface=True` 显式开启；检测规则未放宽；manifest 记
`excluded-ambiguous-interface-records:6`）。加载总量 5574 条，每难度 457 题。

**Gold 分布警示**：选题未按标签分层，90 条中 gold AC 仅 4 条（easy 2、medium 2、
hard 0），非 AC 86 条。本设计因此不能可靠估计正确代码 FPR，也不能支持细粒度
标签结论——这是本报告结论边界的主要来源（见 §9）。

## 4. Taxonomy crosswalk 与标签语义

| 金标签 | 性质 | normalized_error_type |
| --- | --- | --- |
| AC | 执行结果 | —（functional_correct=True） |
| WA | 执行结果 | `E03_WRONG_OUTPUT` |
| RE | 执行结果 | `E01_RUNTIME_EXCEPTION` |
| TLE | 执行结果 | `E02_TIMEOUT_OR_RESOURCE_ERROR` |
| CE | 执行结果 | 空（v1 taxonomy 无编译错误类型；计 unmapped） |
| 混合（middle F；hard E/G/H/I） | 执行结果（多类折叠） | 空（计 unmapped） |
| easy C（Not AC 未指定） | 执行结果（未区分） | 空（计 unmapped） |

`source_error_type` 始终保留原始判定类。90 条中 gold mapped incorrect 47 条、
unmapped incorrect 39 条。

**标签空间不对称**：judge 预测空间为 AC/CE/WA/RE/TLE/MIXED 六类，无法输出
easy 的 `NOT_AC_UNSPECIFIED`，也无法区分 hard 中各种 MIXED 组合。因此任何
"CodeJudge-Eval 原生七类 macro-F1"的说法都不成立；本文只报告：(a) binary
functional correctness（全部 90 条）；(b) coarse verdict macro-F1（明确标注口径）；
(c) verdict 推导的执行结果一致性（仅限 gold 可映射子集）。judge 的根因
`error_type`（R/P/A/C/E 码）只作描述性统计，**不与执行结果 gold 计算一致率**。

## 5. Judge-only Prompt 与冻结

- 版本 `codejudge_eval_judge_only_v1`；`judge_prompt_sha256 =
  a10cdc748b31c419e1cd54dc127a759d8be0518c9e36a1dfa17a3f7d0780ed8b`
  （version + 完整 system prompt + 输出 JSON Schema 的 canonical hash）。
- Prompt 明确声明：不存在 Hy3 原生轨迹、禁止过程/首错声明、禁止引用隐藏测试。
- Judge 可见投影仅含 `interface / language / difficulty / statement /
  candidate_code / entry_point`；金标字母、选项语义、生成模型、原始 URL、官方
  `input` prompt、`data_id` 均不进入 Prompt（有泄漏检测测试）。

## 6. 运行配置（冻结）

provider=`hy3`，model=`tencent/hy3`，temperature=0.0，timeout=120s，
max_retries=2（provider 层），parse repair=1（runner 层独占），
reasoning_effort=high，endpoint 仅记录 sha256 指纹
（`76ef4ad6…77591`）。原始响应私密落盘 `provider_raw.jsonl`。

## 7. 结果

**运行健康度：90/90 valid_judgment；provider_error 0；parse_error 0。**
（注：provider 内部重试当时未做持久化审计，`provider_raw.jsonl` 中无任何错误
记录；因此本报告不对瞬时错误次数作任何声明。重试审计为后续修复项，见 §9.8。）

### 7.1 Binary functional correctness（主结果）

**关键背景：gold AC=4、非 AC=86。永远预测"非 AC"的 majority baseline
accuracy = 86/90 = 95.6%——与本实验的 raw accuracy 完全相同。** 因此 raw
accuracy 与该基线下的 binary F1 都不能单独作为判断能力证据；主结果必须结合
balanced accuracy、specificity、FPR 区间一起解读。

| 指标 | 值 | Wilson 95% |
| --- | --- | --- |
| 正确性 accuracy（全分母） | 86/90 = 95.6% | [89.1%, 98.3%] |
| **majority baseline accuracy** | **86/90 = 95.6%** | [89.1%, 98.3%] |
| **balanced accuracy** | **85.8%**（sens 96.5% + spec 75.0%）/2 | 见分量 |
| sensitivity（检出错误代码） | 83/86 = 96.5% | [90.2%, 98.8%] |
| specificity（放行正确代码） | 3/4 = 75.0% | [30.1%, 95.4%] ⚠️ |
| 正确代码 FPR | 1/4 = 25.0% | [4.6%, 69.9%] ⚠️ |
| MCC | 0.591 | — |
| 错误检测 precision / recall / F1 | 0.988 / 0.965 / 0.976 | — |

⚠️ specificity/FPR 的分母只有 4：这两个点估计在统计上近乎无信息，本设计无法
给出可靠结论（需 v3 分层方案，见 §9）。

解读：majority baseline 的 sensitivity 为 100%、specificity 为 0%、balanced
accuracy 恰为 50%。judge 的 balanced accuracy 点估计 85.8% 高于 50%，MCC
0.591 为正，提示判断带有超越基线的真实信号；但 specificity 一侧只有 4 个
样本（Wilson [30.1%, 95.4%]），且未做显著性检验，证据仍不精确——任何关于
"误报率"或"显著优于基线"的定量说法都不被本设计支持。

### 7.2 分难度（accuracy 全分母）

| 难度 | accuracy | Wilson 95% | 金标 AC 数 |
| --- | --- | --- | --- |
| easy | 28/30 = 93.3% | [78.7%, 98.2%] | 2 |
| medium | 29/30 = 96.7% | [83.3%, 99.4%] | 2 |
| hard | 29/30 = 96.7% | [83.3%, 99.4%] | 0 |

三个**不相交**子样本的点估计接近（93.3%–96.7%），Wilson 区间大幅重叠。但是：
未做显著性检验；且三个子样本使用不同题目，标签粒度与题目难度、候选分布在
本设计中完全混淆，**当前设计不能隔离标签粒度效应**——粒度问题留给 v3 实验 B
的配对设计。

### 7.3 分金标错误类型（functional judgment accuracy，全分母）

| 金标 | n | accuracy |
| --- | --- | --- |
| WA | 35 | 94.3% |
| NOT_AC_UNSPECIFIED | 26 | 96.2% |
| RE | 10 | 100% |
| MIXED | 9 | 100% |
| AC | 4 | 75.0% |
| CE | 4 | 100% |
| TLE | 2 | 100% |

### 7.4 Coarse verdict macro-F1（修正口径）

旧报告中的 `macro-F1 = 0.689` 有**两层错误**，已从结论中撤下：

1. **算法错误**：旧 `_macro_f1` 在某类有 gold 支持但零预测时得到 `f1=None`
   并将其跳过——报告"七类"实际只平均了六类。按 `zero_division=0` 机械重算，
   同样的混淆矩阵在全部七个 gold 类上的值为 **0.590293**（
   `NOT_AC_UNSPECIFIED` 类 F1=0 计入平均）。
2. **口径错误**：judge 根本无法输出 `NOT_AC_UNSPECIFIED`，全标签空间的
   macro-F1 无论怎么算都不是"CodeJudge-Eval 原生七类分类能力"的度量。

修正后的报告口径（均 `zero_division=0`，类数与平均类数一致）：

| 口径 | 类集合 | 样本 | macro-F1 |
| --- | --- | --- | --- |
| 全 gold 标签空间（**仅诊断**） | 7 类（含 judge 不可达的 NOT_AC_UNSPECIFIED） | 90 | 0.590 |
| **compatible-subset** | 6 类（judge 可输出集合） | 纳入 64、排除 26（NOT_AC_UNSPECIFIED） | **0.809** |

compatible-subset 的排除是显式计数，不把 `NOT_AC_UNSPECIFIED` 强行映射成任何
WA/RE/TLE。分块 F1：CE 1.000、RE 1.000、WA 0.806、TLE 0.800、AC 0.667、
MIXED 0.583。注意 hard 的多种 MIXED 组合在 gold 侧已折叠为单一 MIXED，judge
侧的 MIXED 定义一致（"同时存在多类错误"），该粗粒度上可比；更细的组合区分
超出双方标签空间，不报告。

### 7.5 执行结果一致性（替代旧"错误类型一致率"）

旧报告用 judge 的根因 `error_type` 直接与 gold 的执行结果标签比较，得到
29.8%——这是**不同语义空间的错误比较**，该结论（"细粒度归因难"）撤下。

正确口径：从 judge 的 **verdict** 推导执行结果（WA→E03、RE→E01、TLE→E02；
AC/CE/MIXED 无单一映射），只在 gold 可映射的 47 条（WA 35、RE 10、TLE 2）上
计算：

**execution-outcome agreement = 37/47 = 78.7%（Wilson 95% [65.1%, 88.0%]）**。

其余 10 条分歧中 8 条是 gold 单类 WA 被 judge 判为 MIXED（judge 怀疑多类错误，
无单一执行结果可比），2 条 gold WA 被判 AC（即 §8 中确认为标签噪声的
medium/83 与 hard/171）。

根因 taxonomy 描述性分布（87 条 valid incorrect 判定；不作一致率）：P01 27、
E01 13、R01 12、E03 5、C04 4、E02 3、C02 2、P03 2、R02 1、C05 1、null 14。
这些 R/P/A/C 码在 gold 中没有对应物，仅刻画 judge 的归因倾向。

## 8. 错误分析（详见 `docs/codejudge_eval_error_analysis.md`）

4 条功能分歧（1 fp + 3 fn）逐条归因：

| 样本 | 金标 | Judge | 结论 |
| --- | --- | --- | --- |
| easy/135 | AC | TLE | **Judge 真错（FP）**：只看 O(k) 上界、忽略提前退出。证据：题面+代码的**解析证明**（余数链 r_i=i−1 持续当且仅当 i\|n+1，最不利输入 n+1=lcm(2..42) 也在 i=43 退出）+ 自写代码数值验证 |
| easy/4123 | Not AC | AC | **判金标噪声** |
| medium/83 | WA | AC | **判金标噪声** |
| hard/171 | WA | AC | **判金标噪声** |

三条标签噪声判定的证据分级：

1. **解析层**：题面与代码的静态分析未发现违反规格之处；
2. **有限差分测试**：Docker 沙箱内与参考实现差分（88,569 / 5,210 / 89,380 例，
   0 不匹配）——覆盖有限，不是充分性证明；
3. **残留风险**：官方 harness 未重放；若其输入编码与题面规格不符（如 CRLF
   残留），金标仍可能因 harness 细节而异。差分执行为独立分析，**不构成证书
   重放指标**。

**修正口径**：对 4 个功能分歧样本做事后复核后，judge 的判断在 89/90 上与
复核结论一致（98.9%）。这是**探索性结果**——只有 4 个分歧样本接受了复核，
其余 86 条 judge/gold 一致样本没有全部接受独立执行复核，因此它不是"修正后的
完整 ground truth"；正式口径仍以原始金标为准（§7）。

## 9. 限制

1. **Gold 分布极不均衡**（AC=4/90）：raw accuracy 等于 majority baseline；
   specificity/FPR 分母为 4，统计上近乎无信息。修复需 v3 实验 A 的按标签
   分层选择协议。
2. **标签粒度效应未隔离**：三个不相交子样本的点估计接近，但无显著性检验，
   且粒度与题目、候选分布混淆；需 v3 实验 B 的同题配对设计。
3. **标签空间不对称**：judge 预测空间无法表达 `NOT_AC_UNSPECIFIED` 与 hard 的
   MIXED 组合细分；全局 macro-F1 只能作诊断，不能作为原生分类能力指标。
4. **标签噪声存在**：4 条分歧中 3 条经解析+有限差分复核判定为金标噪声
   （残留官方 harness 未重放的风险），与数据集"标签依赖测试充分性"的已知
   限制一致。
5. 单一 provider/模型（tencent/hy3，temperature 0）、单次运行；结论不外推到
   其他模型。
6. 排除 6 条歧义界面记录（task 51），候选池覆盖 457 题中的 456 题。
7. git dirty=True：实验代码在运行时尚未提交，复现需以本分支工作区为准；
   dirty 工作区不能称为完全可复现。
8. **审计缺口**：provider 内部重试次数、逐次耗时未持久化（`provider_raw.jsonl`
   只记录最终返回）；manifest 绑定的是 90 条全量选择而非实际 judged 子集
   （smoke run 亦如此）；`started_at` 在运行结束后才写入 report。这些已在
   后续修复中处理（manifest sidecar、时间与重试审计），但 full-v1 历史原件
   保持原样。

## 10. 复现

```bash
# 数据（固定 revision 6541deb0…）置于 data/codejudge_eval/，仅三个 0-shot 文件
python3 scripts/run_codejudge_eval_judge_only.py --run-id <new-run-id>
# 用修正后的评分层离线重算 full-v1（不改动历史原件）
python3 scripts/recompute_full_v1_analysis.py
# 差分验证（三条金标噪声的有限差分证据）
python3 scripts/differential_verify_codejudge.py
```

运行脚本在任何模型调用前重写并校验冻结选择、文件 hash 与 manifest；
`selection_v2.json` 不一致或文件被篡改即 fail-closed。
