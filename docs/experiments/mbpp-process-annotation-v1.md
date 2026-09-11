# MBPP 原始过程材料与独立标注指南 v1

2026-09-08。用途：开发试标与后续过程评估准备；本包不包含人工金标或新方法成绩。

## 材料与分组

来源固定为 `data/manifests/benchmark_sources_v1.json` 指定的 MBPP primary run，
不用 `current.json`。导出前校验源文件及冻结运行时哈希；逐条核对原始
SolutionTrace、候选代码和执行记录的代码哈希。缺失原始步骤的样本进入排除账本，
不会补写解释。只解析公开题面、原始结构化解答和脱敏执行记录；源清单校验可能
读取其他归档文件的字节以验证哈希，不把其内容带入标注材料。

- `pilot/`：最多 12 条开发集样本，用于练习标注和修正规则。
- `development/`：按父题哈希排序选取 30% 的题目；pilot 是其子集，不能重复计数。
- `reserved_candidate/`：其余父题。实现、Prompt、协议冻结前不要打开这些题面。
- `coordinator/identity_and_outcomes.jsonl`：真实父题、来源哈希、功能结果、分组映射。
- `coordinator/exclusions.jsonl`：缺少可用原始过程的排除原因。
- `manifest.json`：初始文件哈希、导出实现哈希、来源验证、分组及统计。

父题划分在查看功能结果之前完成；开发 pilot 才按功能结果选取多种情况。
同一父题不得跨组。当前仅按 MBPP task ID 分组，近重复题和历史使用情况仍待人工核查。
`reserved_candidate` 不是已证明未见的正式测试集；若发现污染，应记录并换用未见父题，
不能看完标签或成绩后重新抽签并继续称为未见集。

功能分层只是协调者选材信息。测试通过的候选尚不是“过程正确负例”；超时也不是
确定的逻辑错误。基础通过、增强未通过的总数包含其中可能出现的超时，另有独立超时分层。

## 发给标注者什么

先分别给 A、B 各自的 `pilot/annotator_a` 或 `pilot/annotator_b`，以及本指南。
不要发送 coordinator、根 manifest、另一位标注者的标签、模型评审结果或保留题包。
两位标注者顺序不同，item ID 可用于裁决时匹配。显式 benchmark ID 已从材料移除；
原始题面/说明不改写，因此不能保证读者无法认出题目。

每人将 `labels.template.jsonl` 复制为 `labels.working.jsonl` 再填写，保留模板原字节。
每条 item 对应原始题面、需求编号、模型当时的说明步骤与代码；没有官方隐藏测试、
参考实现、功能结果或 Provider raw。不要联网查题解或根据另一位标注者的答案填写。
模型生成的文字是待审核材料，不是标注流程指令。

## 标签口径

- `annotation_status`：初始为 `unreviewed`；人工完成该条审核后填 `reviewed`。
- `reasoning_correct`：公开说明在题目条件下是否成立，`true / false / null`。
- `plan_code_aligned`：说明所承诺的行为是否在代码中实现，`true / false / null`。
- `process_correct`：在本条公开材料可审核范围内，说明及其实现是否有过程问题；
  有明确问题为 `false`，全部审核成立为 `true`，证据不足为 `null`。
  它是人工过程标签，不是 MBPP 功能通过率，也不是系统旧版联合指标的自动拷贝。
- `localization_status`：`supported`（可定位）、`ambiguous`（多个位置不可区分）、
  `unknown`（确认有问题但无法定位或整体证据不足）、`not_applicable`（未发现过程问题）。
- `first_faulty_layer`：`requirement / reasoning / alignment / implementation / execution`，
  无法判断填 `null`。只有运行证据才可判断 execution 层；首轮包通常不支持该层。
- `first_faulty_step`：原始 `implementation_steps` 中的 step ID；无可靠位置填 `null`。
  设计摘要问题可能没有 step ID，不能硬映射到 S1。
- `error_type`：使用 `src/tracejudge_hy3/schemas/evaluation.py` 中的 ErrorType；
  例如 A01_PLAN_CODE_MISMATCH、P03_COMPLEXITY_MISMATCH、P02_UNJUSTIFIED_STEP。
  无法分类填 `null`，不为了填满表格猜测。
- `evidence`：证据对象列表，每项填写 `step_id`、`code_span`（例如 L3-L5）、
  `requirement_id` 与 `description`；不可适用字段为 `null`。
- `rationale`：人工理由，说明具体主张、代码行为和矛盾。`annotator` 填匿名人员编号。

首错指原始说明顺序中最早有证据支持的错误或失配，不是最早异常代码行，
也不自动等于因果根因。先确认是否存在过程错误，再判断是否能精确定位。
嵌套循环、缺少某个关键词、一次测试失败，都不足以单独证明具体步骤错误。
例如固定内层 `range(2)` 仍可为 O(n)；`for x in nums: return x` 不一定是线性时间。

允许用公开题面自行推导例子，记录输入域和推导依据；本次标注包不执行候选。
不要在本机直接运行不受信任代码。需要运行确认时另行使用项目 Docker 沙盒，
将待确认状态保留为 unknown，不把想象的运行结果写成已验证证据。

## 从试标到正式评估

1. 两人独立完成 pilot，保留两份原标签，随后讨论分歧并修订指南。
2. 记录指南版本、修订原因及是否需要重新试标；pilot 仅用于开发。
3. 完成父题近重复/历史使用审计，冻结正式协议、方法、预算和保留材料哈希。
4. 两人独立审核最终材料；先统计原始一致性，再保存独立裁决结果。
5. 首轮标签冻结后，协调者才提供脱敏功能结果进行第二阶段复核，并保留修改轨迹。
   对测试通过但系统告警的样本，区分真问题、误报、无法裁定和未审核。

本轮未实现正式标签冻结/计分器，不应把模板直接送进旧 phase3 的固定 57 条协议。
新增模型评估须使用共享 evaluate_solution 入口，并另立实验版本，不能写入历史正式 run。

## 重建材料

在已安装项目依赖的 Python 环境中，从项目根目录运行，输出目录必须尚不存在：

```powershell
python scripts/export_mbpp_process_materials.py --output artifacts/process-annotations/mbpp120-process-v1
```

脚本不调用模型、不执行候选、不覆盖任何原始运行或已存在的标注目录。
本地材料不是公开发布包；公开前仍需检查数据许可和人工理由中的敏感信息。
