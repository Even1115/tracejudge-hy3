# CodeJudge-Eval judge-only 外部迁移

状态：**实现完成（合成 Fixture 验证），真实数据运行前冻结**  
实现：`src/tracejudge_hy3/benchmark/codejudge_eval.py`、`judge_only_prompt.py`、`judge_only_runner.py`、`judge_only_metrics.py`、`judge_only_manifest.py`

## 目的与边界

使用 CodeJudge-Eval 既有题目、候选代码与错误标签，judge-only 地外部验证
TraceJudge 的代码正确性判断、误报率、错误类型与难度泛化。该数据集**不用于**生成
能力评测，**不得**用于宣称推理首错定位：数据集没有 Hy3 原生解题轨迹，
`BenchmarkJudgeRecord` 的 process/first-step/certificate 字段恒为 `None`。

## 已核验的数据来源（2026-09-04）

- 论文：*CodeJudge-Eval: Can Large Language Models be Good Judges in Code
  Understanding?*（arXiv:2408.10718，COLING 2025）。
- 代码仓库：`CodeLLM-Research/CodeJudge-Eval`，MIT，HEAD
  `c168cd2702c3da3b7c8d08ec7a572e7d9f7f1fbf`（2024-12-03；仓库仅含 README/LICENSE）。
- 数据：HuggingFace `CodeResearch/CodeJudge-Eval`，revision
  `6541deb0c42e56ec1300d94d4e9ae7b3b4cc764a`（2024-08-21），MIT。
- 文件：`CodeJudge_Eval_{0shot,1shot}_{easy,middle,hard}.json`。**本集成只读
  0-shot 三个文件**；1-shot 文件从不读取。
- 记录字段：`task_id`（题号，同一题的多个候选共享）、`statement`（APPS 公开
  题面）、`code`（Python 候选代码）、`answer`（金标选项字母）、`url`（原题
  链接）、`input`（官方 judge prompt，含选项表）、`source`（生成模型名）、
  `data_id`（文件内唯一记录 id）。
- 选项语义按难度文件不同（加载时逐条对 `input` 的 Choices 段做一致性校验，
  不一致即 fail-closed）：

| 文件 | 选项 |
| --- | --- |
| easy | A=AC，B=CE，C=Not AC（不区分类型） |
| middle | A=AC，B=CE，C=仅 WA，D=仅 RE，E=仅 TLE，F=混合 ≥2 类 |
| hard | A=AC，B=CE，C=仅 WA，D=仅 RE，E=WA+RE，F=仅 TLE，G=WA+TLE，H=RE+TLE，I=WA+RE+TLE |

## 90 条选择协议（v2，跨难度不相交）

真实数据事实：三个 0-shot 文件共享同一批 457 道 APPS 题（每文件 1860 条候选；
难度 = 评判粒度而非不同题集）。因此 v2 协议要求跨难度题号**不相交**。

`select_samples`（固定参数 `SELECTION_SEED=20240904`，每难度 30 条）：

1. 每个难度文件内按 `(task_id, data_id)` 数值排序；每题只取 `data_id` 最小的一个
   候选（保证选题号唯一、样本不重复）。
2. 单个 `random.Random(20240904)` 按 easy→medium→hard 固定顺序，每个难度从
   **未被前面难度占用**的题号中 `sample(30)`（跨难度不放回）；选题顺序即冻结顺序。
3. `CodeJudgeEvalSelection` 绑定：有序题号 `selected_task_ids_sha256`、每个来源
   文件的 sha256、每条入选记录的 `source_record_sha256` 与候选 `code_sha256`。
4. `verify_selection_files` 在运行前重算文件 hash，篡改即 fail-closed。

已冻结：`data/codejudge_eval/selection_v2.json`，`selected_task_ids_sha256 =
53c99dc181d24e4cd9ce994b5c7e40f7b38fa0540c78def2bb990205d59caf82`，跨难度重叠 0。

### 真实数据加载纪要（2026-09-05）

- 三文件 sha256：easy `9be68076…`，middle `4690ec01…`，hard `eb86bf28…`；
  `source_manifest_sha256 = 4759ceeec170ee3e4325a91f58e95301434e4a8a79436356dcd7c70b0371ef1f`。
- 加载 5574 条样本；task 51 的 2 条候选 ×3 文件（俄文题面 + 无 stdin 标记）因
  真实界面无法判定被**排除并计数**（共 6 条，`exclude_ambiguous_interface=True`
  显式开启，检测规则未放宽），记入 manifest limitation
  `excluded-ambiguous-interface-records:6`。
- 每难度 457 题 ≥ 30，满足选择协议。
- 入选 90 条的金标分布不均衡：AC 仅 4 条（easy 2、medium 2、hard 0），因此
  正确代码 FPR 的分母极小，解读时须给 Wilson 区间；如需更紧的 FPR 估计，应另立
  按标签分层的 v3 选择协议，而不是本地改样。

## Taxonomy crosswalk（显式，不强行归类）

| 金标签 | normalized_error_type |
| --- | --- |
| AC（functional_correct=True） | — |
| WA | `E03_WRONG_OUTPUT` |
| RE | `E01_RUNTIME_EXCEPTION` |
| TLE | `E02_TIMEOUT_OR_RESOURCE_ERROR` |
| CE | `None`（v1 taxonomy 无编译错误类型，计 unmapped） |
| 混合（middle F；hard E/G/H/I） | `None`（多类错误不可单值归类，计 unmapped） |
| easy C（Not AC 未指定） | `None`（计 unmapped） |

`source_error_type` 始终保留原始判定类（AC/CE/WA/RE/TLE/MIXED/NOT_AC_UNSPECIFIED）。

## Prompt 冻结

- 独立 judge-only Prompt：`judge_only_prompt.py`，明确声明不存在 Hy3 轨迹、
  禁止过程/首错声明。
- 两级 hash 绑定：
  - **v1**（`codejudge_eval_judge_only_v1`，`judge_only_prompt_sha256()`）=
    `canonical_sha256({version, system_prompt})`——历史绑定，保持逐字节不变，
    full-v1 的 manifest hash 仍可验证。
  - **v2 bundle**（`codejudge_eval_judge_only_v2`，
    `judge_only_prompt_bundle_sha256()`）= canonical hash over
    {version, system_prompt, user_template_version, repair_template_version,
    output_schema, max_parse_repairs}——新运行的 manifest 记录该值。
- parse repair 请求（`judge-only-repair-v2` 模板）重新包含完整原始 user prompt
  （公开投影 + 候选代码）、截断至 2000 字符的上一次无效输出与脱敏诊断；
  gold、数据集原始 `input`、generator source、URL、data_id 均不进入。
- 外部数据从不用于调 Prompt 或阈值。

## Judge 可见投影

`judge_visible_projection` 只含：`interface / language / difficulty / statement /
candidate_code / entry_point`。金标字母、选项语义、生成模型、原始 URL、官方
`input` prompt、`data_id` 均不进入 Prompt。

## 指标边界

允许（`judge_only_metrics.score_judge_only`，失败保留全分母，provider/parse 分开；
scoring layer v2）：

- binary functional correctness：correctness accuracy（全分母，Wilson 95%）、
  valid-only precision/recall/F1、sensitivity/specificity（含 Wilson）、
  balanced accuracy、MCC、majority-class baseline accuracy、正确代码 FPR；
- coarse verdict macro-F1（`zero_division=0`，类数与平均类数恒一致）：分两个
  显式口径报告——全 gold 标签空间（**仅诊断**：judge 无法输出
  `NOT_AC_UNSPECIFIED`，不得称为原生七类能力）与 compatible-subset
  （只纳入 judge 可输出的 6 类 gold 标签，排除数显式计数，不做任何强制映射）；
- execution-outcome agreement：仅从 judge **verdict** 推导（WA→E03、RE→E01、
  TLE→E02），只在 gold 可映射样本上计算；judge 的根因 `error_type`
  （R/P/A/C/E 码）只作描述性分布，**不与执行结果 gold 计算一致率**；
- 分难度、分 source error type、crosswalk unmapped 统计、provider/parse
  failure 计数。

禁止（不计算）：process correctness、first-step localization、certificate replay
（除非另立独立可执行验证实验）。

## 运行方式（已实现）

`scripts/run_codejudge_eval_judge_only.py`（freeze-before-run）：

1. 加载固定 revision 数据（歧义界面记录排除并计数），重推导选择并校验等于
   冻结的 `selection_v2.json` 与当前文件 hash。
2. 在任何模型调用前写入：
   - `manifest.json`（共享契约 manifest；**prompt bundle v2 hash**、选题 hash、
     git commit/dirty、provider/model、排除计数）；
   - `manifest_sidecar.json`（CodeJudge 专属）：实际 judged 子集的有序
     (task_id, candidate_id, candidate_sha256) 三元组及其 hash、运行源码逐文件
     hash、依赖锁 hash（uv.lock + constraints）、Python 版本、数据与选择文件
     hash、非敏感 provider 配置、`started_at`（首次调用前）；
   - `provider_config.json`（temperature=0.0、timeout=120s、max_retries=2、
     reasoning_effort=high、endpoint 指纹）。
   `started_at` 在首次模型调用前记录。正式（非 smoke）运行要求 clean commit，
   否则需显式 `--allow-dirty`，且 sidecar 的 reproducibility_note 明确标注
   dirty 工作区**不可称完全可复现**（以源文件 hash 兜底）。
3. 顺序评判选中样本；原始响应私密落盘 `provider_raw.jsonl`（已加入
   `.gitignore`，普通 `git add` 不会纳入）；每条样本输出一个 frozen
   `BenchmarkJudgeRecord` 到 `records.jsonl`。
4. `score_judge_only` 生成 `report.json`（仅允许指标；provider/parse 失败
   保留全分母并单独计数）；`completion_receipt.json` 绑定
   started/completed/duration、provider 调用与重试聚合计数、parse repair
   计数及全部输出文件 hash。

`--limit N` 为 **smoke 运行**：实验 ID 变为 `codejudge-eval-judge-only-v2-smoke`，
manifest 只绑定实际 judged 的前 N 条子集（含 `smoke-run`、`judged-n:N`
limitations），不得与正式实验混淆。新运行的实验 ID 为
`codejudge-eval-judge-only-v2`（绑定 v2 prompt bundle）；`codejudge-eval-judge-only-v1`
仅作为 full-v1 历史实验的不可变标识保留（`HISTORICAL_EXPERIMENT_ID_V1`），新运行
不得再铸造。历史 `runs/smoke/` 由修复前脚本生成，见其中 `SUPERSEDED.md`。

真实 provider：`benchmark/hy3_judge_only.py` 的 `Hy3JudgeOnlyProvider`，复用
`.env` 的 HY3_* 配置；provider 层只做 provider-error 重试，parse repair 由
runner 的 `MAX_PARSE_REPAIRS=1` 独占；`audit_counters()` 提供
calls/attempts/retries 聚合计数进入 completion receipt。
