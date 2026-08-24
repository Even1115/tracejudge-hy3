# v0.1 范围说明

本项目的完整研究级方案见根目录方案文档；v0.1 是一个刻意收紧范围的可运行 MVP，用于打通端到端链路并为后续版本打基础。范围约束**优先于**方案文档中的完整版本规划。

## 本次实现

- Python 3.11+，Python 函数级代码任务；
- JSON 可序列化的输入输出；
- 命令行应用（Typer + Rich），无 Web 前端；
- 3 道内置示例题（`safe_mean` / `deduplicate_preserve_order` / `clamp`）；
- 从本地固定 revision 快照离线生成 164 题 HumanEval+ 阶段一公开投影，并以固定 seed/公开题号生成可复现的 10 题 Pilot；投影不含 `canonical_solution`、官方测试或任何可执行测试；
- `baseline --dataset-manifest` 对 HumanEval+ provenance、投影哈希、题号顺序和选择参数做绑定，只生成/解析 Solver 输出；
- 固定 EvalPlus v0.3.1 commit/官方镜像 digest/Python/HumanEval+ release 的独立阶段二执行边界；仅支持上述固定 10 题、每题单候选的 Base 与 Extra 工程 Pilot；
- 基础 AST 静态分析（分支/循环/数据结构/空输入启发式/硬编码启发式）；
- 可见测试、隐藏测试、挑战测试；
- 基础需求—步骤—代码对齐（规则命中 + LLM 判断交叉验证）；
- 基础反例验证（challenge/hidden 测试复用 + 有限边界候选差分执行 + 简单 delta-debugging）；
- 结构化错误证书：新疑似问题使用 `confirmed_bug` / `strongly_supported` / `unverified_suspicion`；首次正确运行无证书，`cleared` 仅表示显式传入既有证书后的复核转移；
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
- 反事实配对集、人工标注集、对照实验、消融实验（这些属于方案文档中更完整版本的内容）。

模型输出中的"过程"是面向用户、可审查的解题说明和实现计划，不要求也不试图暴露模型不可见的内部思维链。

HumanEval+ 固定 10 题有两个彼此分离的证据范围：阶段一仅产生 `generation_and_parsing_only` 事实；阶段二仅产生固定子集的 single-sample generation→execution 工程 Pilot 事实。解析成功不代表功能通过，阶段二的 10 题通过率也不是完整 164 题 pass@1、正式 benchmark 排名或模型总体能力结论。`run` / `batch` 仍拒绝公开投影；只能通过已完成阶段一产物进入 `tracejudge evalplus`。
