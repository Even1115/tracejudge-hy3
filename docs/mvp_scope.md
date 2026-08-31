# v0.1 范围说明

本项目的完整研究级方案见根目录方案文档；v0.1 是一个刻意收紧范围的可运行 MVP，用于打通端到端链路并为后续版本打基础。范围约束**优先于**方案文档中的完整版本规划。

## 本次实现

- Python 3.11+，Python 函数级代码任务；
- JSON 可序列化的输入输出；
- 命令行应用（Typer + Rich），无 Web 前端；
- 3 道内置示例题（`safe_mean` / `deduplicate_preserve_order` / `clamp`）；
- 从本地固定 revision 快照离线生成 164 题 HumanEval+ 阶段一公开投影，并以固定 seed/公开题号生成可复现的 10 题 Pilot；投影不含 `canonical_solution`、官方测试或任何可执行测试；
- `baseline --dataset-manifest` 对 HumanEval+ provenance、投影哈希、题号顺序和选择参数做绑定，只生成/解析 Solver 输出；
- 固定官方镜像 digest、镜像内 EvalPlus package `0.4.0.dev2` 与源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`、Python/HumanEval+ release 的独立阶段二执行边界；支持固定 10 题工程 Pilot，以及从 45 题 research-natural 来源中按 `phase1-success-only` 导出的单候选 Base 与 Extra 正式运行；
- 基础 AST 静态分析（分支/循环/数据结构/空输入启发式/硬编码启发式）；
- 可见测试、隐藏测试、挑战测试；
- 基础需求—步骤—代码对齐（规则命中 + LLM 判断交叉验证）；
- 基础反例验证（challenge/hidden 测试复用 + 有限边界候选差分执行 + 简单 delta-debugging）；
- 结构化错误证书：新疑似问题使用 `confirmed_bug` / `strongly_supported` / `unverified_suspicion`；首次正确运行无证书，`cleared` 仅表示显式传入既有证书后的复核转移；
- 阶段三 Gate A/B 契约、42 条正式自然轨迹冻结，以及 15 条公开自建单因素反事实的预注册 source、精确白名单证据执行和 overlay 冻结工具；正式公开 evidence run 与 42 + 15 overlay 已完成；
- 阶段三 Gate D 公开 challenge/确定性探针策略、三等级公开工程证书 writer、只读预检和单用例 `phase3 replay`；正式证书产物与 confirmed 证书独立重放已完成，但仍只是公开工程 Fixture 证据，不构成五方法研究结果；
- 阶段三 Gate E1 冻结标注指南与机器可校验协议，正式 57 条私有盲法 packet 已导出；packet 与协调者 identity map 分离，不包含方法预测、其他标注者标签、反事实构造元数据或官方隐藏输入；
- 阶段三 Gate E2 提供不打开 identity map 的 working 标签进度/结构检查，以及仅在全部完成后允许的身份回连预检和 Git-ignored 私有原子冻结；单人首轮 57 条主标注已正式冻结；
- 阶段三 Gate E3 提供五方法正式运行的只读身份预检和显式 Hy3 执行入口；正式 `phase3_hy3_57x5_v1` 已完成 57 × 5 全部 285 配对，其中 283 条有效 judgment、2 条 Provider 失败；
- 阶段三 Gate E4 提供严格哈希/顺序/复用链绑定、全分母聚合指标、精确 McNemar + Holm 和父题聚类 bootstrap，并以不含逐轨迹内容的私有原子 writer 发布；正式 `phase3_stats_primary_round1_v1` 已冻结；
- 阶段三 Gate F 提供哈希绑定的脱敏结果解读、11/11 统计谬误扫描、Material Passport、公开错误证书 Demo 与原子不可覆盖 writer；入口已实现，正式报告尚未发布；
- Mock 模式（默认，无需 API Key）与真实 Hy3 Provider 接口（可选）；
- 单元测试与 README。

## 本次不实现

- Web 前端；
- 多 Agent 编排框架；
- 仓库级代码修改；
- 多文件生成任务；
- 多语言代码执行；
- HumanEval+ 快照的项目内自动下载、完整 164 题/多样本正式 benchmark 运行；
- MBPP+ 接入与评测；
- 完整 mutation testing 框架；
- 自动代码修复；
- 复杂控制流图或符号执行；
- Gate C 已实现五方法统一工程接口和 Mock 中断/resume 验证，Gate E1 packet 与 Gate E2 单人首轮正式标签已冻结，Gate E3 真实 Hy3 配对结果已产生，Gate E4 正式聚合统计已冻结，Gate F 脱敏解读入口已实现；但第二标注/重测一致性、正式 Gate F 报告和扩展消融仍不在当前证据内。当前证据只适用于本轮固定 cohort/模型/Prompt/标注设置，不产生因果、等效或完整 benchmark 结论。

模型输出中的"过程"是面向用户、可审查的解题说明和实现计划，不要求也不试图暴露模型不可见的内部思维链。

HumanEval+ 固定 10 题有两个彼此分离的证据范围：阶段一仅产生 `generation_and_parsing_only` 事实；阶段二仅产生固定子集的 single-sample generation→execution 工程 Pilot 事实。research-natural 正式运行同样属于固定来源、单候选配置下的功能证据，不是完整 164 题 pass@1、标准多样本 pass@k、正式 benchmark 排名或模型总体能力结论。解析成功不代表功能通过；`run` / `batch` 仍拒绝公开投影，只能通过已完成且 provenance 一致的阶段一产物进入 `tracejudge evalplus`。
