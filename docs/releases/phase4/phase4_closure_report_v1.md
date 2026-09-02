# TraceJudge-Hy3 阶段四封版报告 v1

Closure ID：`phase4_closure_report_v1`

封版状态：**P0 REPOSITORY DELIVERABLES COMPLETE / PUBLICATION PENDING AUTHORIZATION**

## 1. 封版对象

阶段四以“复现加固、脱敏成果发布与项目封版”为目标，不修改阶段一至阶段三冻结运行。阶段三正式身份保持：

- 研究运行：`phase3_hy3_57x5_v1`；
- 统计运行：`phase3_stats_primary_round1_v1`；
- 报告运行：`phase3_report_primary_round1_v1`；
- 研究集：42 条自然轨迹 + 15 条反事实轨迹 = 57 条；
- 完整配对：57 x 5 = 285；
- 运行结果：`valid_judgment=283`、`provider_error=2`；
- 证据等级：`ANALYZED / CAUTION / CANNOT_VERIFY`。

阶段四没有重跑 Hy3 主实验，没有重试两条 Provider 失败，没有执行 Docker 或网络实验，也没有修改任何阶段三正式结果。

## 2. P0 Gate 完成情况

| Gate | 状态 | 封版证据 |
|---|---|---|
| Gate A：现状审计与范围冻结 | 完成 | Git、阶段三合并提交、冻结哈希、私有/公开边界和过期文档已经核验。 |
| Gate B：复现清单与公开 replay receipt | 完成 | 103 个私有关键产物、13 个公开锚点、0 个权限警告；公开 digest 与 replay receipt 已受 Git 跟踪。 |
| Gate C：脱敏报告与文档发布 | 完成 | Gate F Markdown 逐字节公开，发布说明记录隐私审计、展示缺口和结论边界。 |
| Gate D：一致性与稳健性增强 | 非 P0，延期 | 第二标注者/跨时间复标、扩展反事实、消融和 Provider 失败敏感性分析均未执行。 |
| Gate E：Demo、图表与 Release 封版 | 仓库内交付完成 | 3 张确定性聚合 SVG、2 分钟公开 Fixture Demo、Release 检查单和本封版报告已就绪；push/tag/Release 待授权。 |

## 3. 可公开交付物

| 交付物 | 身份或 SHA256 | 内容边界 |
|---|---|---|
| 阶段三脱敏研究报告 | `29eaef9f44a964308ab26b9821c472b0d13837eee587a3e687faa861edb4d725` | 与冻结 Gate F Markdown 逐字节一致，只含聚合结论与公开 Fixture 证据。 |
| 阶段四 artifact digest | `9094352967dbe90598d477c8abc0cdf6d0ac2dc311ab1d675b61d4460b477033` | 不含产物正文、私有相对路径或绝对路径。 |
| 阶段四 replay receipt | `c1ba43dfe40b19af6929ddc9749a24f335933e22dad43ba626cbfc7c56e1d784` | 单个 `safe_mean` 公开证书 replay；Provider/Docker/网络均为 0。 |
| 阶段四图表 manifest | `20d94ad514400ff7ebe72b8d288eb6a208b571069878091b4b6b481659f30d71` | 仅含聚合计数、区间、预注册比较、哈希和运行身份。 |
| 图 1：cohort 与执行核算 | `33fc5806172729d2543280954fc09f2774aa13737ae5f922c35bd65905afe98c` | 42 + 15、57 x 5、283 + 2 的聚合核算。 |
| 图 2：按来源的错误检测 | `a7020cb43b163fc52df533897bac72c4bef011691795ee0445899013808802b2` | 五方法的聚合分子/分母与描述性区间。 |
| 图 3：预注册配对比较 | `08b45448d1329b0c078365e68042f74ba54539e28a197c47bf210cf42b6a197f` | 自然轨迹 McNemar/Holm 与 3 个父题 cluster bootstrap 的有限证据。 |
| 公开 Fixture Demo | `phase4_fixture_demo_v1` | 自建 `safe_mean` 的评估、反例、证书和 replay，不含私有研究行。 |
| Release 检查单 | `phase4_release_checklist_v1` | 隐私、哈希、测试、范围和授权边界。 |

阶段三冻结统计 manifest / report SHA256 保持为 `7efbdc9c36340593be09e192ea0e7b15297d5e69c4192fa4b49583558b368bf8` / `972e7c0f5eac36d59035ec65376133fbcc0dfa941281e97fb7dcc70f02360a10`；Gate F manifest / validation SHA256 保持为 `0b8285ec04344e29670d752a37c4d5ecb41ea07d5dfc18a5715b56de3e800b06` / `702bf96be5d0911088dfea5cb95562d6b8e25d147d972c78b0b6870cecbae113`。

## 4. Demo 与可复现性验收

Gate E 在本地执行公开 `safe_mean` Fixture：完整 Mock Demo 约 0.93 秒，依次产生可见/公开 Fixture 测试结果、`args=[[]]` 反例、`confirmed_bug` 错误证书；同一证书的精确白名单 replay 约 0.76 秒，重现失败且执行证据 SHA256 一致。两次执行均为受限本地公开 Fixture，Provider、Docker、网络调用均为 0。

正式图表通过 manifest 哈希绑定、隐私校验和逐字节重绘验证。图表只是阶段三聚合结果的静态展示，不新增样本、标签、模型调用或统计推断。

本轮封版验证结果：Gate E focused tests 为 43 passed；完整普通测试为 496 passed、3 skipped，3 项 skipped 均为未启用的 Docker 集成检查；Ruff 检查、Ruff 格式检查、`git diff --check` 和图表逐字节验证全部通过。测试和图表验证没有调用真实 Hy3、Docker 或网络。

## 5. 研究结论与限制

阶段三已经完成一个固定 cohort、固定 Prompt、单模型单次运行和单标注者首轮标注下的探索性研究闭环。图表显示的差异必须结合原始分母、两条 Provider 失败、3 个反事实父题 cluster 和不确定性区间解读。

现有证据不能支持：

- TraceJudge 在一般代码生成任务上普遍优于简单方法；
- 各模块具有已识别的因果贡献；
- 不显著差异等于方法等效；
- 完整 HumanEval+、MBPP+ 或跨模型 benchmark 排名；
- 单 Fixture replay 等于阶段三 Hy3 主实验已被独立复现。

因此封版发布不会提升证据等级，状态继续为 `ANALYZED / CAUTION / CANNOT_VERIFY`。

## 6. P1 与 v0.2+ 边界

P1 研究增强未执行且不阻塞本次 P0 封版：第二位标注者独立复标 15–20 条；无第二标注者时至少间隔 7 天的跨时间复标；raw agreement、混淆计数和适用条件下的 Cohen's kappa；增加公开反事实父题 cluster；扩展 AST、结构化对齐和动态反例消融；两条 Provider 失败的独立敏感性分析。任何一项启动前都必须冻结样本、比较、停止规则和新的运行身份，不得覆盖阶段三结果。

v0.2+ 保持在本轮之外：完整 HumanEval+ 164 题、MBPP+、多模型 Judge、Web UI、自动修复、多文件和多语言执行。

## 7. 最终授权边界

本报告完成的是仓库内 P0 Gate E 交付准备，不代表已发生外部发布。当前未由自动化执行 commit、push、merge、tag、Release 创建、附件上传或分支删除。

项目负责人授权发布前，应按 [`phase4_release_checklist_v1.md`](phase4_release_checklist_v1.md) 重新运行测试、Ruff、格式检查、`git diff --check`、图表逐字节验证和公开产物哈希检查。最终封版 commit ID，以及本报告和检查单自身 SHA256，应在 commit 产生后写入 Release 说明，而不是在自引用文档中预先伪造。
