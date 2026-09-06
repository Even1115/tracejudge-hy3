# 三数据集阶段报告：公开证据包

日期：2026-09-06。版本：`three-dataset-interim-v3-public1`。
入口：[阶段报告](../../two_dataset_interim_evaluation_report.md)。

本包保留 HumanEval+ 全量 164 题、MBPP+ 固定 120 题及 CodeJudge-Eval 的既有结果。
LiveCodeBench 60 题由用户告知正在另一台电脑运行；本机未检查远端产物，结果仍为空。
这是一份三数据集阶段报告，不是已完成四数据集的最终报告。

## 文件与可核对内容

| 文件 | 内容 |
| --- | --- |
| [results.json](results.json) | HumanEval+ 生成/执行/恢复摘要；MBPP+ 分母、通过率、Wilson 区间与失败分类；CodeJudge 修订 full-v1、断网 v3-A、完整 v3-A 与 v3-B 汇总 |
| [configurations.json](configurations.json) | 数据版本、选择身份、模型非敏感设置、运行代码身份、镜像与限时；历史 dirty 状态及跨批次差异不隐藏 |
| [source_hashes.json](source_hashes.json) | 36 个本地来源文件的精确字节 SHA256、文件大小和逻辑标识；不附原始内容 |
| [SHA256SUMS](SHA256SUMS) | 本发布包和阶段报告的校验值，路径相对于仓库根目录；不包含此校验清单自身 |
| [verify_release.py](verify_release.py) | 仅依赖 Python 标准库的只读检查，不需要模型、Docker、原始实验文件或项目依赖 |
| [livecodebench_handoff.md](livecodebench_handoff.md) | 另一台电脑的结果核验清单和下一版报告接续要求 |

`results.json` 的 `source_ids` 对应 `source_hashes.json`。JSON 中的指标沿用原件，
是字段白名单投影，不是重新运行模型。MBPP+ 的尝试数及状态组合另与本地逐条记录核对。
本次发布只调整链接、状态和证据可用性，不修改既有统计结论。

## 结果范围

| 评测 | 主结果 | 边界 |
| --- | --- | --- |
| HumanEval+ | Base 163/164；Base+Extra 157/164 | 全量单候选，跨批次恢复；不是官方排行榜认证 |
| MBPP+ | Base 117/120；Base+Extra 95/120，Wilson 95% [71.05%, 85.47%] | 固定 120/378 子集；2 个官方超时计入未通过 |
| CodeJudge v3-A | 完整轮 balanced accuracy 90%；原断网轮 88.3% | 平衡 holdout；两轮都保留，不解释为性能提升 |
| CodeJudge v3-B | 三条件功能准确率均为 98%；原生标签 96% / 84% / 72% | 50 个配对候选；无显著差异不代表等效 |
| LiveCodeBench | 待核验，无结果值 | 60 题运行进展仅来自用户告知 |

这些评测覆盖代码生成功能正确性和外部 judge-only 判断，不直接验证过程错误定位、
首错步骤或错误证书重放。不同任务的百分比不求总平均。

## 数据可用性与保密边界

本包不包含凭据、服务端点、机器绝对路径、原始 Provider 响应、候选源代码、完整
Prompt、官方隐藏测试、失败输入或逐候选判断记录。原件仍在本地私有实验目录中，
没有解除 `artifacts/` 的 Git 忽略规则，也没有将 `data/` 原始文件加入本次提交。

来源哈希与公开投影的哈希有不同含义：前者标识私有原件，后者检查公开文件完整性。
**有哈希不等于可访问原件，也不等于独立复现。** 公开读者可以检查分子、分母、比例、
部分区间和来源关联；从逐条记录重新计分、复算配对 bootstrap 或重放测试仍需要
未公开的实验原件。本地既往验证状态不构成外部第三方认证。

配置文件如实记录 CodeJudge 的全局 dirty/实验范围 clean，以及 HumanEval+ 恢复
所含未提交修复。MBPP+ 的 `1956f013…` 来自独立本地冻结仓库，本次报告提交不会
自动将该仓库的 Git 对象同步到 GitHub。相同远端模型别名也不保证权重永远不变。

## 在另一台电脑核对公开包

从仓库根目录运行以下只读检查；Windows/WSL 可将 `python3` 替换为可用的 Python 3.10+：

```bash
python3 docs/evaluation_release/2026-09-06/verify_release.py
```

还可检查发布文件和 Markdown 引用是否已在 Git 索引中：

```bash
python3 docs/evaluation_release/2026-09-06/verify_release.py --check-index
```

这不是模型或候选执行命令，不会修改实验环境，也不会读取本机的凭据或私有数据。
检查通过仅表示公开包内部一致、校验值匹配；不把它表述为原始实验全部独立复现。

## 后续版本

保留本阶段报告及此目录作为固定发布版本。另一台机器核验 LiveCodeBench 后，新建
四数据集报告与独立的 LiveCodeBench 公开证据包，引用本包；不覆盖既有摘要、重试历史
或哈希清单。详细交接要求见[交接说明](livecodebench_handoff.md)。
