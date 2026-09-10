# 证据消融页：2×2 消融结果的只读展示适配层

2026-09-09。本工作包在演示页面新增"证据消融"页签，展示统一提示下四个证据条件（A/B/C/D）对同一批 12 条冻结候选的判断对照。适配层只读取 `run_process_pilot.py --phase ablate` 与 `--phase ablation-score` 已落盘并哈希绑定的产物，不修改实验执行、提示词、计分逻辑、冻结输入、标签或任何正在写入的结果。消融实验本身的定义与结果见 [process-pilot-ablation-v1.md](process-pilot-ablation-v1.md)。

本页与"方法对比"页是**两个独立实验**：方法对比展示历史三方法（direct/structured/full_system，提示不同）的运行，本页展示统一提示下的 2×2 证据消融；条件标识 `ablation_a/b/c/d` 显示为"A · 仅公开材料 / B · +静态证据 / C · +功能汇总 / D · +静态+功能"，**不改名为 Direct/Structured/Full，也不与三方法合并成任何排行榜**。

## 页面与数据源

- 页签：演示页 header 的"证据消融"（`data-view="ablation"`），与"评估演示""典型案例""方法对比"并列，已有功能不变。
- 接口：`GET /api/ablation-comparison`（`src/tracejudge_hy3/demo_app/server.py`），数据由 `src/tracejudge_hy3/demo_app/ablation_view.py` 组装。
- 显式配置的数据源（不是"最新目录"，也不是 `current.json`；更换运行需有意修改常量并更新文档）：
  - `ABLATION_RUN_DIR = artifacts/experiments/process-pilot/mbpp12-ablation-20260909-v1`
  - `ABLATION_SCORES_FILE = artifacts/experiments/process-pilot/mbpp12-ablation-score-20260909-v1/ablation-scores.json`
  - `PILOT_CONFIG = data/manifests/process_pilot_v1.json`
  - `EXPECTED_PLAN_SHA256 = 1448c841986d6a4bdc0e0b5ba887fededf7c05b6fc7bc2272ddae239c9609774`（预注册方案哈希）
- 当前接入的是真实已完成运行：12 样本 × 4 条件，48 次判断全部 `ok`，运行报告状态 `completed`。

## 来源校验（任一失败即拒绝展示，页面呈现"实验结果尚未就绪"）

1. 运行目录与计分文件必须是仓库内相对路径、非符号链接、不越出仓库根。
2. `run-report.json` 的 `files_sha256` 必须与 `predictions.jsonl`、`requests.jsonl`、`run-config.json`、`ablation-plan.json` 的实际 SHA-256 完全一致。
3. `run-config.json` 身份核验：pilot 配置哈希、条件集合、逐条件提示词/可见字段/输出契约指纹（对当前实现重算）、逐条输入哈希、`plan_sha256` 必须等于登记的 `EXPECTED_PLAN_SHA256`。
4. `ablation-plan.json` 内容指纹必须等于方案哈希，方案内 12 条输入哈希与当前绑定材料一致，执行顺序完整覆盖 48 个（条件 × 样本）对。
5. 预测逐行按 `AblationPrediction` 契约解析，并调用离线计分器 `score_ablation` 完成全部校验：身份去重、输入哈希、引用绑定，以及对每条成功预测的**离线规则重合并**。
6. `ablation-scores.json` 必须哈希绑定同一份预测文件（`predictions_sha256`），且在所有计分器产生的字段上与一次全新 `score_ablation` 重算**逐键相等**——页面不重新实现计分规则，也不展示与计分器不一致的数字；`verified_sources`（含绝对路径）等绑定字段不转发给浏览器。
7. 人工标签只来自 `prepare_pilot` 加载的哈希绑定、已审核来源；只用于展示与对照，不进入任何模型请求（页面本身也不发起模型请求）。
8. `requests.jsonl` 台账按 `method` 字段携带条件标识做配对解析（复用 `comparison.py` 的台账校验，按条件集合参数化）；未记录的用量保持 `null`，页面显示"未记录"，不填零；金额未由 Provider 返回，成本保持未知，不估算。

API 不接受任意文件系统路径：唯一允许的查询参数是 `?fixture=synthetic`，其余一律 400。所有模型文本、代码、解释与人工理由在前端仅以 `textContent` 渲染；合成数据内置一条 HTML/脚本探针，浏览器验证确认其按纯文本显示且不执行。

## 页面结构与语义口径

- **实验概览**：运行状态、样本数、人工参考计数（8 正确 / 2 错误 / 2 未知，开发集修订共识）、条件 × 证据矩阵（"不提供"指证据不进入 Judge 输入，AST 静态分析与确定性规则层对全部条件离线运行）、请求/耗时/token 与预算（金额未知保持未知）、小样本与定位金标为 0 等限制。
- **计分表**：judge_raw / rule_merged 两视图切换（aria-pressed 同步）；逐条件覆盖率、已知标签准确率、tp/fp/tn/fn、误报率与错误召回；预注册成对比较（B−A、C−A、D−B、D−C）的转移计数与变化样本清单；规则触发与改变判断汇总——本轮规则 0 次触发时如实显示"本轮规则未触发，未观察到规则合并贡献"且不外推。
- **样本列表与筛选**：全部样本 / 条件分歧 / 相对标签误报 / 相对标签漏报 / 参考未知 / 调用失败 / 结果缺失，各带计数与定义注记；分歧只按已完成判断的标量字段计算，失败、缺失、弃答不冒充分歧；默认展示全部样本，不只展示有利结果。两个重点案例（max_product、division_elements）有快捷按钮。
- **共用样本 + 四列对照**：共用冻结输入在上（题目、需求、解答轨迹、官方功能汇总独立成区）；四列 A/B/C/D 对照表含状态、可见证据、说明正确性、计划—代码对齐、公开过程结论（当前视图，相对标签的 tp/fp/tn/fn 标记）、首错层级/步骤/错误类型、规则触发与是否改变判断、请求耗时与 token；逐字段标记"一致/分歧/无法全比/—"。
- **条件卡片**：Judge 原始解释逐字展示（标注"模型输出文字，未执行验证"）；规则合并改变判断时另示合并后解释与触发规则的依据文本；引用按钮点击后在共用输入中定位并高亮（引用匹配只证明位置绑定成立）；绑定失败显示"引用无法核验"，不猜测位置。
- **重点案例备注**：max_product 标明"本轮仅 D 检出（单次观察，尚需重复实验验证）"，模型反例 `[-2,0,1]`（模型输出文字）与审核反例 `[0,2,3]`（人工静态推演）分列，均标注未执行验证，官方只有 base/plus 汇总状态；division_elements 标明"四条件漏判，标签争议已裁决维持"（2026-09-09 裁决，维持 R03 原标签，完整推理链见案例审核文档），历史 full_system 的检出仅在独立"历史参考"子块中呈现并注明实验设置不同。
- **人工参考**：标签与理由默认折叠，标注开发集修订共识性质；定位金标为 0 时显示"暂无定位参考"，null/null 不算命中。

## 合成界面测试数据

`GET /api/ablation-comparison?fixture=synthetic` 返回 `synthetic_ablation_comparison()` 生成的 5 条合成样本，覆盖：四条件一致、仅 D 检出（重点案例形态，含 XSS 探针与可定位引用）、规则合并改变判断（B 条件）、参考未知 + 弃答 + 调用失败、结果缺失 + 长文本 + 未记录用量。载荷 `data_kind="synthetic_ui_test"`，页面顶部常驻琥珀色"界面测试数据"横幅，不混入真实实验列表，也不进入任何计分。真实结果不可用时页面提供空状态（"实验结果尚未就绪"及所需文件说明），并可选择载入合成数据验证页面状态。

## 验证记录

**离线测试**（WSL venv，`PYTHONPATH=src`，无任何真实网络与付费调用）：

- `tests/test_demo_ablation.py` 12 项通过：四条件载荷组装与计分一致性、条件标识不改名、规则合并前后视图与触发依据、失败/缺失/弃答不冒充分歧、计分文件篡改与换绑拒绝、运行身份（方案哈希/条件集合/指纹/配置绑定）拒绝、重点案例备注指向未知样本拒绝、合成数据标记与状态覆盖、合成与真实载荷结构一致、HTTP 合成端点与任意查询 400；真实 pinned 运行集成测试（产物存在时）逐项核对方案哈希、48 次请求、A/B/C 8/10 与 D 9/10、max_product fn/fn/fn/tp、division_elements fn×4、D 解释原文含 `[-2,0,1]`、载荷不含仓库路径与密钥。
- 受影响测试文件合并回归：`test_demo_ablation.py` + `test_demo_comparison.py` + `test_demo_app.py` + `test_benchmark_launchers.py` 共 57 项通过。本轮触及的 Python 文件 Ruff check 与 format 均通过。

**测试状态（固定表述）**：针对性验证通过；全套件仍有已归因失败（38 失败 / 924 通过 / 13 跳过，归因明细见 [process-pilot-ablation-v1.md](process-pilot-ablation-v1.md) "测试归因记录"）。失败归因只说明原因，不替代全套件通过。

**浏览器验证**（playwright-core + 系统 Chrome，只读脚本 `%TEMP%/pw-verify/verify-ablation.js`，64/64 通过）：

- 真实运行：页签与来源标识、证据矩阵、运行身份（方案哈希/种子/预算）、成本未知、标签计数、计分表四条件数字与计分文件一致、成对比较与规则未触发说明、视图切换两视图数字一致、筛选计数（全部 12 / 分歧 2 / 误报 0 / 漏报 2 / 未知 2 / 失败 0 / 缺失 0）、两个重点案例的备注/历史参考/四列对照、引用点击定位与无障碍播报、人工参考折叠展开、键盘激活筛选、窄屏单列且不比既有页面多溢出、浏览全程仅 GET 且无运行/模型请求、控制台无错误；评估演示/典型案例/方法对比页无回归。
- 空状态（空仓库根服务器）：明确拒绝展示并给出原因、不静默回退；载入合成数据后横幅/徽标/规则合并视图差异/缺失失败占位均正确，XSS 探针不执行不注入。
- 截图：`%TEMP%/pw-verify/shots/ablation-{overview,max-product,division,narrow,empty,synthetic}.png`。

## 使用步骤

```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd '/mnt/e/犀牛鸟/tracejudge-hy3' && PYTHONPATH=src /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python -m tracejudge_hy3.demo_app.server --port 8766"
```

访问 `http://127.0.0.1:8766/`，点击 header 的"证据消融"。（默认端口 8765 被本机另一任务占用时按上例改用 8766；页面只监听 127.0.0.1。）

## 剩余限制

- 页面只读：浏览、筛选、视图切换、引用定位均不触发模型请求、Solver 或候选执行。
- 计分差异为每个条件每题一次判断的单次观察；页面与文档均不外推。
- 条件分歧计数包含人工参考未知样本上的字段差异（如 newman_prime 的 D 条件给出 R01 而其他条件未给出）：此类分歧不构成误报/漏报，页面在筛选定义中如实说明。
- 更换运行目录或计分文件需有意修改 `ablation_view.py` 常量并重新核验，页面不提供任意路径入口。
