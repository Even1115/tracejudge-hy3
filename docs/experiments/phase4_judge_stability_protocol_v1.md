# Phase 4 Full TraceJudge 小规模稳定性实验协议 v1

## Material Passport

- Origin Skill: experiment-agent
- Origin Mode: plan
- Origin Date: 2026-09-03
- Verification Status: UNVERIFIED
- Version Label: `phase4_judge_stability_public4x5_v1`

本文档在真实 Hy3 稳定性运行前固定研究设计。当前只表示代码、离线 Mock 验收和正式入口已经实现，不表示 20 个真实评审已经完成，也不预告结果方向。

## Experiment Overview

- **Title**：Full TraceJudge 在四类公开案例上的五次重复判断稳定性
- **Objective**：描述同一模型、同一 Prompt 和同一输入下，三个主要判断字段是否保持一致，并完整核算解析与 Provider 失败。
- **Research question**：在四个固定公开案例上，Full TraceJudge 的 `has_error`、`first_faulty_step` 与 `error_type` 判断在五次独立评审中是否稳定？
- **Type**：探索性、目的性选择的公开 Fixture 重复评审；不是新的主实验，也不是总体性能检验。

## Fixed Design

只使用 `safe_mean` 同一父题，减少任务差异带来的混杂。案例顺序固定如下：

| case_id | 冻结 trace | 角色 | 公开执行预期 |
|---|---|---|---|
| `normal_correct` | `public-parent:safe_mean:v1` | 正常正确 | pass |
| `reasoning_swap` | `counterfactual:safe_mean:reasoning_swap:v1` | 答案/代码正确但说明来自另一目标 | pass |
| `boundary_error` | `counterfactual:safe_mean:boundary_deletion:v1` | 删除空输入边界分支 | fail |
| `equivalent_implementation` | `counterfactual:safe_mean:equivalent_implementation:v1` | 写法不同但语义等价 | pass |

公开源固定为 `data/phase3/public_counterfactuals_v1.json`，SHA256 为 `a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df`。功能与公开动态证据来自既有独立 run `phase3_cf_public_15_v1`，其 `results.jsonl` SHA256 为 `19a138ecc2ce784b940e88e085a85ddddf92a564be7235bbd5a3e97bb39d2776`。正式预检还会逐字节复核 manifest、结果行、Fixture、代码和 replay 绑定。

每个案例独立评审 5 次，共 20 个评审单元。执行顺序采用 `repeat_major_fixed_case_order_v1`：每轮依次运行上述四例，共五轮。方法固定为现有 `full_tracejudge`，温度默认 `0.0`，沿用严格 JSON Schema 与最多一次修复策略。因而名义为 20 次底层请求；若发生首次解析失败，最多为 40 次。正式报告必须写实际请求数和使用修复的评审单元数。

## Inputs and Identity

正式 `protocol.json` 必须绑定：

- Git commit；若仅作开发预演并允许 dirty，还要绑定 working-tree SHA256；
- Python 版本、脱敏后的 Provider/模型公开配置及其哈希；
- Full Prompt 版本与 SHA256、输出 Schema SHA256、Full 方法规格 SHA256；
- 四个材料 payload 的集合 SHA256，以及每个案例的 `method_input_sha256`；
- 公开源、公开执行 manifest 和 results SHA256；
- 稳定性实现 SHA256。

正式论文或比赛补充实验应在 clean worktree 上运行，不使用隐藏的 `--allow-dirty` 开关。

## Analysis Plan

主要字段按预先固定顺序报告：

1. `has_error`；
2. `first_faulty_step`；
3. `error_type`。

同时报告三字段联合标签，作为更严格的补充指标。对于每个案例和字段，只在有效判断之间枚举全部成对比较；相同值的比较计为一致。总体一致率把四个案例内部的一致对数相加，再除以四个案例内部的可比对数之和，不进行跨案例比较。

无错误判断中的 `first_faulty_step=null` 与 `error_type=null` 统一编码为显式类别 `<none>`，表示合法空值，不表示运行缺失。另报告每个案例的值分布、众数比例，以及是否达到完整的 5/5 全一致。

Provider 失败、两次输出后仍解析失败、AST/公开证据/基础设施失败不进入对应字段的成对分母，必须按原始次数单独报告；不得删除失败后重新补跑并伪装成原 20 个单元。发生进程中断时，只能用同一 run ID、完全相同的协议身份从下一个未完成单元续跑。

本实验不预设“通过阈值”，不做显著性检验，也不把 4 个目的性案例当作随机样本。无论结果高低都保留完整分子、分母、失败和分布。

## Expected Outputs

```text
artifacts/experiments/phase4-judge-stability/<run_id>/
├── manifest.json
├── protocol.json
├── results.jsonl
├── report.json
├── REPORT.md
└── trials/
    ├── trial_001.json
    └── ... trial_020.json
```

每个 trial 独立原子落盘，便于中断续跑；最终 `manifest.json` 绑定结果和两种报告的 SHA256。目录和文件权限分别收紧为 `0700/0600`，并位于 Git-ignored `artifacts/` 下。

## Commands

先在 clean commit 上执行只读预检；它不创建目录、不调用 Provider：

```bash
tracejudge phase4 stability-preflight \
  --run-id phase4_stability_hy3_public4x5_v1
```

核对屏幕中的案例数 `4`、重复数 `5`、评审单元 `20`、名义/最大请求 `20/40` 以及 Protocol SHA256 后，再显式启动真实运行：

```bash
tracejudge phase4 stability-run \
  --run-id phase4_stability_hy3_public4x5_v1 \
  --confirm-real-provider
```

如进程在完成前中断，保持配置、代码和输入不变：

```bash
tracejudge phase4 stability-preflight \
  --run-id phase4_stability_hy3_public4x5_v1 \
  --resume

tracejudge phase4 stability-run \
  --run-id phase4_stability_hy3_public4x5_v1 \
  --resume \
  --confirm-real-provider
```

身份有任何变化时续跑必须失败；此时应使用新 run ID 重新开始，而不是覆盖旧目录。

## Conclusion Boundary

输出只描述当前 Hy3 服务版本、当前 Full Prompt、四个公开 `safe_mean` 案例、各五次评审的运行内稳定性。它不能证明 57 条主实验、其他题目、其他模型、其他温度或未来服务版本同样稳定。该实验使用独立 run ID、目录、manifest 和报告，`main_experiment_merge_allowed=false`；不得回写或并入冻结的 57 × 5 主实验统计。
