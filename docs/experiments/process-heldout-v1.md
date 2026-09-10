# 方法评估保留集冻结记录（process_heldout_v1，2026-09-09）

日期：2026-09-09。本文档记录保留集的冻结协议、两项前置审计（近重复 / 历史暴露）的结果，以及冻结后的承诺。**未创建任何标签**——按标注指南，先冻结协议与材料哈希，再谈标注。

## 冻结对象

- 清单：`data/manifests/process_heldout_v1.json`（schema `tracejudge-process-heldout-v1`）。
- 审计详情：`artifacts/process-heldout/mbpp84-audit-20260909-v1/heldout-audit.json`（哈希已写入清单）。
- 材料来源：冻结标注包 `artifacts/process-annotations/mbpp120-process-v1/`（manifest SHA-256 `f1d6733d…b003da`，与试点/开发配置同一绑定）。
- 84 条 reserved_candidate，逐条登记 item_id、parent_task_id、packet_item_sha256 与状态。

## 近重复审计（对开发集 36 条）

机械比较，未打开保留集文本阅读。规则预登记如下：

- 函数名与任一开发样本相同 → 排除；
- 需求文本去样板词、去除断言行后的 Jaccard ≥ 0.7 → 排除；
- 0.4 ≤ Jaccard < 0.7 → 标记保留（在审计中留痕）。

结果：

- **排除 3 条**：Mbpp/104（与开发集 Mbpp/569 sort_sublists 需求文本完全相同，Jaccard 1.0）、Mbpp/563（与开发集 Mbpp/725 需求文本相同，Jaccard 1.0）、Mbpp/267（与开发集 Mbpp/287 近重复，Jaccard 0.8，函数名 square_Sum 相同）。
- **标记保留 3 条**：Mbpp/233（0.43 vs Mbpp/731）、Mbpp/281（0.5 vs Mbpp/253）、Mbpp/769（0.4 vs Mbpp/145）。后续分析若涉及这些样本的检出结论，须连同该标记一起报告。
- **保留 81 条**进入 held_out。

## 历史暴露审计（如实记录，不隐瞒）

- 全部 84 条保留候选的父任务均出现在 2026-09-05 的 4 次 phase1 基线生成运行中（`artifacts/experiments/phase1-mbpp/`：1 次 mock provider、3 次 hy3 真实生成，experiment_label `mbppplus_120_public_prompt_generation_pilot`，角色为**解答轨迹生成**）。即：模型曾为这些任务生成过解答，但彼时没有任何过程标签，也没有任何过程评估方法的结果用于调参。
- Mbpp/267、Mbpp/432、Mbpp/456、Mbpp/765 在 `docs/two_dataset_interim_evaluation_report.md` 中被提及，内容仅限执行状态级信息（重试恢复、官方 timeout），无标签或判断内容。
- 结论：本保留集的含义是"**过程标注与方法调优闭环之外**"，不是"任何运行都未接触过"。凡引用本保留集结果的报告必须带上这句限定。

## 冻结承诺

1. 条目集合、排除规则与材料哈希先于任何标注冻结；不允许看完标签或成绩后重新抽签还继续称为未见集。
2. 保留集的任何方法预测不得影响标签创建。
3. 标注启动时，标签文件将由新配置以哈希钉定（同 process_development_v1 的绑定方式）。
4. 标注协议启动前，保留集文本保持未开封；仅协调者角色的机械审计（如本次近重复比较）允许接触。
5. 反事实构造样本包与开发集标签永不混入保留集统计。

## 后续步骤（未执行）

- 制定保留集标注协议（标注者、独立性声明、与开发集标签的同规程校验）。
- 方法评估运行（真实模型调用由人执行，本文档不授权任何付费 API 调用）。

## 测试状态

针对性验证通过；全套件仍有已归因失败（2026-09-09 最终实测 37 失败 / 969 通过 / 13 跳过，归因见 process-pilot-ablation-v1.md 测试归因记录及补充）。
