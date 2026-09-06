# HumanEval+ 164 题阶段二执行

## 修复与数据来源

阶段二导出器现在直接接受全量 `tracejudge_humanevalplus_public_projection`
manifest，严格验证固定 revision、来源 hash、全量选择算法
`all-pinned-task-ids-numeric-order-v1` 和 `HumanEval/0` 到 `HumanEval/163`
的完整数值顺序。缺失、重复、乱序、越界题号以及伪造字段都会被拒绝。

全量 manifest 本来没有 `parent_manifest_sha256`、`limitations` 或
`selection.seed`。导出器在内部以 `None` 表达不存在的 parent/seed，并准确匹配
阶段一记录的 provenance；不修改原始文件或为它们补写字段。
10 题 Pilot、45 题 research-natural 保持原有选择与导出语义。

全量阶段二新增 `selection_role: full`，完整导出时使用
`humanevalplus_164_evalplus_execution_full` 标签。Mock 的 summary 始终标记
`mock_dry_run_only`，不产生功能通过率。若显式选择 `phase1-success-only` 且
未导出全部题目，仍记录完整来源 164、成功数量及排除数量，不称作全量执行完成。

## 复用已经生成的 164 份代码

本次修复位于主仓库。以下命令使用主仓库的修复代码，读取原 HumanEval 独立
工作树中的阶段一结果，并将阶段二结果写入主仓库的新目录。无需重新生成代码，
无需调用 Hy3 API。运行及续跑期间应保持阶段二实现、依赖和执行参数固定；
阶段二 manifest 会记录实现 hash 和 Git 状态。

```bash
TJ_MAIN="/Users/even/Desktop/犀牛鸟/实战阶段/tracejudge-hy3"
TJ_HE="/Users/even/Desktop/犀牛鸟/实战阶段/tracejudge-hy3-humaneval-run"
TJ_PY="$TJ_MAIN/.venv/bin/python"
TJ_PHASE1="$TJ_HE/artifacts/experiments/phase1-humanevalplus-full/phase1_20260905T085110677252Z_65d2a9ad3f12"
TJ_DATASET="$TJ_MAIN/artifacts/datasets/processed/humanevalplus-full/dataset_manifest.json"

cd "$TJ_MAIN"
PYTHONPATH="$TJ_MAIN/src" "$TJ_PY" -m tracejudge_hy3.cli evalplus \
  --baseline-run "$TJ_PHASE1" \
  --dataset-manifest "$TJ_DATASET" \
  --output-dir "$TJ_MAIN/artifacts/experiments/phase2-humanevalplus-full" \
  --executor docker \
  --selection-policy all \
  --parallel 2 \
  --per-task-timeout 180 \
  --batch-timeout 5400
```

镜像沿用已安装的固定 digest
`ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740`。
命令先做静态来源校验，再校验 Docker 运行时及全部公开题目身份，最后隔离执行
每份候选。输出包括 `manifest.json`、`samples.jsonl`、`results.jsonl`、
`evalplus_raw_results.json`、`summary.json` 和 `execution.log`。

若本次正式执行中断，保存启动时打印的新 `phase2_...` run ID；在相同命令后
追加 `--resume-run-id <本次正式阶段二_run_id>`。旧的、在导出前失败且未创建
目录的 `phase2_20260905T143706068756Z_840cc4ef3a75` 不能续跑；mock 目录也
不能用作 Docker 正式运行的续跑来源。

## 本次验证记录

- 现有阶段一真实产物已静态导出 164 个唯一候选、164 条来源引用和 164 个公开
  题目身份；只取成功记录中的 `solution_trace.code`，续跑的 skipped 事件不增加样本。
- 全量 samples SHA256：
  `b08ca5adeb86fd559306aef901c0a4c5c80b2b8660d0b2114fbadcec0fee7f62`。
- 真实数据 mock 已完成：
  `artifacts/experiments/phase2-humanevalplus-full-mock/phase2_20260905T144903800940Z_cfbf40d788e6`。
  来源 164、导出 164、Mock 记录 164、实际执行 0，Base/Plus 均为 N/A。
- 修复前后，阶段一 manifest/responses/summary 与全量 dataset manifest 的
  SHA256 均未变化；旧 Pilot 导出 10 条、research-natural 导出 42/45 条，
  两者 samples SHA256 与内部 dataset identity hash 均未变化。
- 本机真实 Docker 预检通过：`ready=true`、`verified_task_count=164`，
  EvalPlus `0.4.0.dev2`、HumanEval+ `v0.1.10`、固定镜像 digest 与全部公开
  题目身份匹配。此次预检未执行候选代码。
- 相关回归测试为 231 passed、3 skipped；限定修改范围的 Ruff 检查及格式
  检查通过。新增覆盖全量导出、篡改拒绝、来源文件不变、mock/续跑及 CLI
  完成状态；原 10 题、45 题路径继续通过回归验证。

这些验证证明既有产物能够进入阶段二。最终功能成绩以 Docker 正式运行的
summary 为准：确认 `actual_execution_count=164`、`evaluation_complete=true`
和 `infrastructure_error_count=0` 后，报告 Base 与 Base+Extra 的通过数量及分母。
候选未通过测试是评测结果；基础设施失败须单列并完成恢复，不能作为候选错误。
