# 方法对比页：三方法 pilot 结果的只读展示适配层

2026-09-09。本工作包在演示页面新增“方法对比”页签，展示同一道题、同一个冻结候选在 Direct Judge、Structured Judge、Full System 三种评估方法下的判断与证据。适配层只读取 `run_process_pilot.py` 已落盘并哈希绑定的运行产物，不修改实验执行、提示词、计分逻辑、冻结输入或任何正在写入的结果。

## 页面与数据源

- 页签：演示页 header 的“方法对比”（`data-view="compare"`），与“评估演示”“典型案例”并列，已有功能不变。
- 接口：`GET /api/method-comparison`（`src/tracejudge_hy3/demo_app/server.py`），数据由 `src/tracejudge_hy3/demo_app/comparison.py` 组装。
- 显式配置的数据源（不是“最新目录”，也不是 `current.json`；更换运行需有意修改常量）：
  - `PILOT_RUN_DIR = artifacts/experiments/process-pilot/mbpp12-run-20260909-v1`
  - `PILOT_CONFIG = data/manifests/process_pilot_v1.json`
- 当前接入的是真实已完成运行：12 样本 × 3 方法，36 次判断全部 `ok`，运行报告状态 `completed`。

## 来源校验（任一失败即拒绝展示，页面呈现“实验结果尚未就绪”）

1. 运行目录必须是仓库内相对路径、非符号链接。
2. `run-report.json` 的 `files_sha256` 必须与实际 `predictions.jsonl`、`requests.jsonl`、`run-config.json` 的 SHA-256 完全一致——不一致说明结果仍在写入或被修改。
3. `run-config.json` 的 pilot 配置哈希、方法集合、逐条输入哈希必须与 `prepare_pilot` 当前复算结果一致（`prepare_pilot` 本身对标注包、审核快照、执行记录、候选代码做哈希核验）。
4. 预测逐行按 `Prediction` 契约解析，并调用离线计分器 `score_predictions` 完成与计分完全相同的校验：重复或未知身份、输入哈希不符、引用越界都会被拒绝。
5. 人工标签只来自 `prepare_pilot` 加载的哈希绑定、已审核提交（annotator A/B 一致后取 A），绝不读取工作副本；标签只用于展示与对照，不进入任何模型请求（页面本身也不发起模型请求）。
6. `requests.jsonl` 台账按 `seq` 配对 dispatched/outcome，重复序号、未知事件或与运行报告计数不符都会被拒绝；未记录的用量保持 `null`，页面显示“未记录”，不填零。

API 不接受任意文件系统路径：唯一允许的查询参数是 `?fixture=synthetic`（见下），其余查询一律 400。所有模型文本、代码、标注理由在前端仅以 `textContent` 渲染；合成数据内置一条 HTML/脚本探针，浏览器验证确认其按纯文本显示且不执行。

## 语义口径

- 公开过程结论 = `reasoning_correct AND plan_code_aligned` 的三值逻辑（`public_process_correct`），与人工过程标签对应；不使用旧系统含功能结果的联合 `process_correct` 作为对比信号（联合结论单独一行展示，并注明仅 Full System 接入功能证据）。
- 功能证据（MBPP base/plus 官方汇总状态）独立成区展示；功能失败不视为过程错误；不伪造逐用例执行结果。
- 误报/漏报仅在对应维度有明确人工参考时标记；未知标签保持“未知”。
- 当前首错步骤与结构化首错位置金标数均为 0：页面显示“暂无定位参考”，可比较三种方法给出的位置，但不标记定位正确，null/null 不算命中。
- 引用出现在原文只证明绑定成功，不证明错误判断成立；绑定失败时显示“引用无法核验”，不猜测位置。
- 分歧只在已完成判断（`status=ok`）的方法之间按标量字段（说明正确性、计划—代码对齐、公开过程结论、首错层级、首错步骤、错误类型）计算；调用失败、缺失、弃答不算分歧。三方法完全一致时如实显示“一致”。
- 人工参考区明确标注：反馈后的开发集修订共识，非独立测试集金标；标注者独立性未核实。

## 合成界面测试数据

`GET /api/method-comparison?fixture=synthetic` 返回 `synthetic_method_comparison()` 生成的 4 条合成样本，覆盖：三方法一致、方法分歧+误报/漏报、参考未知+调用失败、结果缺失+长文本+未记录用量。载荷 `data_kind="synthetic_ui_test"`，页面顶部常驻琥珀色“界面测试数据”横幅，不混入真实实验列表，也不进入任何计分。真实结果不可用时页面提供空状态（“实验结果尚未就绪”及所需文件说明），并可选择载入合成数据验证页面状态。

## 验证记录

- `tests/test_demo_comparison.py`（16 项）：合成 pilot + 合成运行目录覆盖载荷组装、误报/漏报/未知/弃答分类、失败与缺失不算分歧、报告哈希不符拒绝、缺失报告拒绝、重复/过期/错配预测拒绝、方法集合不符拒绝、未记录用量不填零、部分运行状态如实披露、真实运行一致性（12 样本、36 请求、标签汇总非硬编码、无绝对路径与密钥）、合成与真实载荷同构、HTTP 层的合成端点与任意查询拒绝。
- 相关套件：`test_demo_app / test_demo_costs / test_demo_launcher / test_demo_pipeline / test_demo_comparison / test_process_pilot / test_process_pilot_ablation / test_shared_evaluator` 共 98 通过、10 跳过（跳过为既有的 Docker/真实 API 用例）。
- 浏览器验证（playwright-core + 系统 Chrome，脚本在 `%TEMP%/pw-verify/verify.js`，截图在同目录 `shots/`）：52/52 通过，覆盖三页签切换与已有评估/案例/成本功能、样本筛选定义与数量、分歧高亮与文字标识、人工参考展开与未知显示、原文引用与代码行定位、空状态/部分结果/失败/未知/长文本、480px 窄屏无页面级横向溢出、键盘操作与焦点保持、页面操作全部为本地 GET（无模型请求）、不可信文本探针未被执行为 HTML。
- 本改动 Python 文件 Ruff check 与 format 均通过。

## 接入新运行 / 剩余限制

- 指向新运行时修改 `comparison.py` 的 `PILOT_RUN_DIR`（并通过同一套校验）；若生产者协议变化（文件清单、哈希字段、台账事件），适配层会拒绝而非猜测，需同步更新 `_verify_report_hashes` / `_parse_usage` 的契约。
- 适配层在每次请求时重新读取与校验产物（12 样本规模开销可忽略）；更大规模运行如需缓存，应以报告哈希为缓存键，不能以目录mtime。
- 结构化位置金标仍为 0；一旦存在受支持的金标，`location_gold.has_reference` 会自动切换人工参考区的展示，但页面只比较位置、不断言定位正确性（该断言属于计分器）。
