# 过程评估开发集完成与保留集冻结（2026-09-09）

本文档汇总任务三的交付：开发集 36 条全部完成过程标注、构造性能力探针样本包、保留集冻结。所有工作离线完成，未调用任何付费模型 API，未执行任何候选代码。

## 1. 开发集标注完成（36/36）

- 标签目录：`artifacts/annotation-development/mbpp36-dev-20260909-v1/`
  - `labels_carried_pilot_v1.jsonl`：12 条试点共识标签，逐字节复制自经审核的 `submitted_a.jsonl`（SHA-256 一致）。
  - `labels_extension_20260909.jsonl`：27 条扩展标注（24 条开发集独有样本 + 3 条试点样本复核：2 条 v1 unknown 的裁决 + 1 条 v1 已判成立样本的复核维持），annotator `annotator_dev`。
  - `review_notes.md`：规程、unknown 裁决的完整公开材料推理链、先前接触披露、能力缺口发现。
  - `build_labels.py`：构建脚本；写入前对每行执行 PilotLabel 模式校验 + 针对冻结包材料的上下文校验（步骤/需求 ID、引文子串、代码行区间），任一行不合格整体拒绝。
- 配置：`data/manifests/process_development_v1.json`（schema `tracejudge-process-development-config-v1`）；`annotation_provenance` 如实声明：结转行为独立性未经核实的试点共识，扩展行为 2026-09-09 单人 AI 辅助复核（仅用公开材料，接触史已披露）。
- 加载器：`src/tracejudge_hy3/process_eval_v2/development.py` 的 `prepare_development`：结转行必须与经审核的试点提交逐行一致（经试点配置与审核报告双重哈希钉定），扩展行覆盖校验 = 恰好 36 条，每条均通过上下文校验。
- 标签分布：**32 成立 / 4 错误 / 0 unknown**。4 条错误全部带 supported 定位：
  - max_product（P01，v1 共识）、division_elements（R03，v1 共识 + 2026-09-09 裁决维持）；
  - 新增 text_lowercase_underscore（R03：需求说 contains，需求理解静默改写为整串组成）与 opposite_Signs（R03：需求理解把"opposite sign"改写为"一个为负另一个不为负"，0 的归属假设在公开需求中不存在）——两条均与 division_elements 同一裁决标准，完整推理链见 review_notes.md。
- 2 条 v1 试点 unknown（newman_prime → 过程成立；opposite_Signs → R03）的裁决与 1 条复核维持（sample_nam，v1 本已判成立，扩展行标签内容不变）只进入新标签阶段 `post_feedback_development_extension`；已完成的消融运行及其计分仍绑定 v1 共识标签，旧结果保留不改动。

## 2. 构造性能力探针（反事实样本包）

自然样本通览确认两项能力无覆盖：计划—代码失配（36 条全部一致）、步骤级首错定位（4 条错误均为理解级，无步骤 ID）。按 phase3 反事实先例构造：

- 文件：`data/process-eval/mbpp_process_counterfactuals_v1.json`；构建脚本 `scripts/build_process_counterfactuals.py`（父样本哈希钉定冻结包，金标签写入前全量校验）。
- 3 条探针：
  - **CF-1**（父 add_pairwise）：代码 `zip(tup, tup[1:])`→`tup[2:]`，计划不变 → 金标签 reasoning=True / aligned=False / A01 / 首错步骤 S2。
  - **CF-2**（父 filter_oddnumbers）：步骤 S2 条件反转（`x % 2 == 0`），代码跟随 → reasoning=False / aligned=True / R01 / 首错步骤 S2。
  - **CF-3**（父 find_char_long）：代码 `>= 4`→`> 4`，计划不变 → reasoning=True / aligned=False / A01 / 首错步骤 S2。
- 诚实性标记（构造即强制）：`constructed=true`；变异代码**从未执行**，功能证据如实记为 `unavailable_never_executed`；**排除在一切真实样本统计之外**（检出率、配对比较、排行榜）。

## 3. 保留集冻结

- 清单 `data/manifests/process_heldout_v1.json` + 审计 `artifacts/process-heldout/mbpp84-audit-20260909-v1/heldout-audit.json` + 协议文档 [process-heldout-v1.md](process-heldout-v1.md)；冻结脚本 `scripts/freeze_process_heldout.py`。
- 近重复审计（机械比较，未开封阅读）：排除 3 条（Mbpp/104、Mbpp/563 需求文本与开发集完全相同；Mbpp/267 近重复 0.8），标记保留 3 条（Mbpp/233、Mbpp/281、Mbpp/769），**81 条进入 held_out**。
- 历史暴露审计如实记录：全部 84 条父任务均出现在 2026-09-05 的 phase1 基线生成运行中（1 mock + 3 hy3，角色为解答生成）；4 条在中期报告中被提及（仅执行状态级）。保留集含义 = "过程标注与方法调优闭环之外"，引用其结果须带此限定。
- **未创建任何标签**；冻结承诺含"不看标签/成绩重抽签""方法预测不得影响标签创建"。

## 测试状态

针对性验证通过；全套件仍有已归因失败（2026-09-09 最终实测 37 失败 / 969 通过 / 13 跳过；37 项 = 35 CRLF 环境 + 1 HEAD 既有 + 1 环境/设计行为，归因明细见 [process-pilot-ablation-v1.md](process-pilot-ablation-v1.md) 测试归因记录及其补充）。失败归因只说明原因，不替代全套件通过。

针对性验证明细（WSL 正式环境，PYTHONPATH=src）：

- `tests/test_process_development.py`：9 项（结转篡改拒绝、扩展身份/越界/覆盖缺口拒绝、审核绑定拒绝、真实配置 36 条加载 + 标签分布 + unknown 裁决断言）。
- `tests/test_process_counterfactuals.py`：6 项（诚实性标记、父样本哈希绑定、sole-change 精确性、金标签对变异轨迹校验、能力语义、ID 不碰撞）。
- `tests/test_process_heldout.py`：6 项（清单绑定、84 条全覆盖、与开发集不重叠且无标签字段、审计规则可复算、历史暴露如实、哈希与协调者一致）。
- 合并回归：上述 + 试点/消融/重复验证/共享评估器/材料导出相关 9 个套件共 82 项全部通过。
