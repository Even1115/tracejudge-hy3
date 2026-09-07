# 四数据集报告公开证据包

版本：`four-dataset-interim-v1`，2026-09-06。

[阅读四数据集报告](../../four_dataset_evaluation_report.md) · [主指标 CSV](summary.csv) · [完整汇总 JSON](results.json)

HumanEval+、MBPP+、LiveCodeBench、CodeJudge-Eval 均已纳入。LCB 仍有两题 provider_error，故这是阶段性综合报告，不能称所有题目均完成有效评测。

## 内容

| 文件 | 用途 |
| --- | --- |
| results.json | 保留原三数据集结果字段，加入 LCB 当前指标；另列据 HumanEval 聚合计数补算的 Wilson 区间 |
| summary.csv | 四组主指标及分母；CodeJudge 的 balanced accuracy 没有单一分子、分母，相关单元格留空 |
| configurations.json | 固定数据、选择、非敏感模型设置、执行资源与运行源码身份 |
| source_hashes.json | 原 36 项来源及新增 LCB 4 项来源；原件不随此包发布 |
| verification_evidence.json | Git 原始字节校验、CRLF 差异说明、本机来源哈希及 LCB 原始事件核验 |
| SHA256SUMS | 本公开包、主报告及限定换行规则的精确字节 SHA-256；不包含该清单自身 |
| verify_release.py | 标准库只读验证：字节哈希、链接、指标、分母、来源关联、空白和敏感字段边界 |

此包没有包含模型回答、候选代码、隐藏测试、完整 Prompt、凭据或机器绝对路径。统计和来源摘要不足以独立复算全部私有逐条记录。

旧三数据集公开包在 Windows 工作树中发生 CRLF 换行转换。此次先逐个确认 Git 原始 blob 与原 SHA256SUMS 相等，再将未经修改的 blob 放入独立副本运行原校验器；没有改期望值或重写旧证据。本包新增文件固定 LF 换行。

## 验证

从仓库根目录运行：

```bash
python docs/evaluation_release/2026-09-06-four-datasets-v1/verify_release.py
```

已暂存后可追加 `--check-index`。默认校验不联网、不调用模型或 Docker、不读取私有实验目录，也不写入文件。

生成记录与复核工作副本在本机 Git 忽略的报告目录中；本公开包不复制这些私有工作目录。旧三数据集与 LCB 单集报告均保留。后续补齐 LCB 或变更统计时应新建报告版本，不覆盖本次证据。
