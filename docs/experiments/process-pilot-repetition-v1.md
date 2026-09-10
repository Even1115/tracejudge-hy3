# 消融固定配置重复验证报告（n=5，2026-09-09 预登记 / 2026-09-10 执行完毕）

本文档报告预登记的固定配置重复验证：`scripts/run_process_pilot.py --phase ablate` 同一方案、同一配置重复 5 轮（rep1 + rep2–5），按 `docs/experiments/process-pilot-ablation-v1.md`「重复验证方案」执行，汇总产物为 `artifacts/experiments/process-pilot/mbpp12-ablation-repetition-tally-20260909-v1/tally-report.json`。

## 口径与绑定（全部通过校验）

- 5 轮均绑定：原 12 条 pilot v1 共识标签（`data/manifests/process_pilot_v1.json`）、原消融方案（plan SHA-256 `1448c841…609774`，与预登记值一致）、原条件集合与提示词指纹、逐条输入哈希、同一模型。
- 实现身份：`implementation_sha256` 14 个文件 5 轮逐字节一致。**注意**：重复期间工作树新增了 `development.py`/`repetition.py` 两个模块（会被指纹 glob 计入）；为保持与 rep1 的身份一致，rep2–5 在一个剔除这两个新增模块的独立副本（WSL `/home/even/tj-reprun`）中执行，启动前已逐哈希比对确认与 rep1 的 run-config 完全一致（14/14）。两个新增模块不在消融运行路径上（无任何导入），工作树本身未改动。
- 汇总器对每轮重放完整校验链：钉板文件哈希、运行身份、预注册方案、计分器重合并（`score_ablation`），任一不符即拒绝汇总。5 轮全部通过，无一轮被排除或选择性报告。
- **未中途切换标签阶段**：36 条开发标签只用于并列的补充计分（见 §5），原口径结果未改动。

## 逐轮执行记录

| 轮次 | 目录 | 状态 | 请求数 | 说明 |
|---|---|---|---|---|
| rep1 | `mbpp12-ablation-20260909-v1` | completed | 48 | 2026-09-09 完成 |
| rep2 | `…-rep2` | completed（resumed） | 51 | 两次 ValidationError 崩溃后经预登记的 `--resume` 完成；2 次崩溃各浪费 1 次请求，账目在 requests.jsonl |
| rep3 | `…-rep3` | completed | 48 | 一次通过 |
| rep4 | `…-rep4` | completed（resumed） | 49 | 1 次 ValidationError 崩溃后续跑完成；1 条判断记录为 provider_error（ProviderTimeoutError，重试预算耗尽） |
| rep5 | `…-rep5` | completed | 48 | 一次通过 |

判断覆盖：rep1/2/3/5 为 48/48 ok；rep4 为 47 ok + 1 provider_error（ablation_b × max_product）。失败行按协议保留并计入分母，不补跑替换。

### 事故如实记录

1. **rep2 首次完成运行的产物丢失**：2026-09-10 早晨 rep2 第一次完整执行成功（进程退出码 0），但产物只写在 WSL `/tmp`（tmpfs），归档前 WSL 虚拟机重启，产物全部丢失；该次运行的请求消耗已无法核验（上限 96 次）。rep2 随后重建副本重跑，最终产物即上表 51 请求的运行。**本会话 rep2 槽位的付费请求总量因此超出单轮 96 上限**（丢失运行 + 最终运行各一笔），特此披露。教训：需要跨重启存活的中间产物必须放 WSL home 并立即归档回 Windows 工作树。
2. **潜藏 ValidationError 崩溃（重复验证的实际发现）**：判定模型偶尔输出自相矛盾的评估——自报 `process_correct=False` 并给出结构化错误定位，但其 `reasoning_correct`/`plan_code_aligned` 两票重算的公开过程信号为 True。`judge_ablation` 用 `model_copy(update=…)` 覆盖 `process_correct`（不重新校验），随后的 `AblationPrediction` 构造触发契约校验"an error location requires an error assessment"并抛出未捕获的 ValidationError，整轮崩溃。rep1 未碰到纯属模型输出运气；rep2 命中 2 次、rep4 命中 1 次。崩溃判断的已消耗请求不返还（预登记规则），靠 `--resume` 续跑完成。**修复被有意推迟**：涉事文件全部在实现指纹 14 文件集合内，重复系列期间改动会破坏与 rep1 的身份一致性；列为方法冻结后的修复候选（见 §6）。
3. **rep4 超时行**：ablation_b × max_product 两次尝试均 ProviderTimeoutError，按协议记录为 provider_error 终态行；该样本在 B 条件的检出频率分母为 4 而非 5。

## 重复稳定性结果（rule-merged 视图，公开过程信号）

### 正确样本（gold=True，9 条 + 另见下）完全稳定

9 条 v1 共识为成立的样本，在全部 5 轮 × 4 条件下检出频率均为 0/5（无任何误报、无弃判）。

### 已知错误样本

| 样本 | 条件 A | 条件 B | 条件 C | 条件 D |
|---|---|---|---|---|
| max_product（P01） | 1/5 | 0/4（1 次超时失败） | 0/5 | 2/5 |
| division_elements（R03） | 0/5 | 0/5 | 0/5 | 0/5 |

### v1 unknown 样本（不计入准确率，只观察判定行为）

| 样本 | A | B | C | D |
|---|---|---|---|---|
| newman_prime | 0/5 | 1/5 | 0/5 | 1/5 |
| opposite_Signs | 0/5 | 0/5 | 0/5 | 0/5 |

### 飘动（flapping）清单（汇总器口径：5 轮中判定结果不一致的条件×样本）

- newman_prime × B（1/5 判错）、newman_prime × D（1/5）
- max_product × A（1/5）、max_product × D（2/5）

### 逐轮准确率（rule_merged，gold_known=10，含失败/缺失入分母）

| 轮次 | A | B | C | D |
|---|---|---|---|---|
| rep1 | 8/10 | 8/10 | 8/10 | 9/10 |
| rep2 | 8/10 | 8/10 | 8/10 | 8/10 |
| rep3 | 8/10 | 8/10 | 8/10 | 8/10 |
| rep4 | 9/10 | 8/10 | 8/10 | 9/10 |
| rep5 | 8/10 | 8/10 | 8/10 | 8/10 |

### 解读（与预登记一致的保守口径）

- 9 条成立样本的判定在 5 轮中**零飘动**；错误检出只在 max_product 的 A/D 条件与 newman_prime 的 B/D 条件上偶发。单轮报告的 D 条件"检出 max_product"结论（rep1 fn→tp）在 5 轮中只复现 2/5——单轮结果不足为凭，这正是预登记重复验证要回答的问题。
- division_elements（需求理解级 R03）在任何条件、任何轮次都未被检出（0/20），与单轮结论一致且稳定。
- 样本量 12、已知错误 2 条：只报告计数与飘动清单，不做统计显著性或泛化声明。

## 5. 补充计分：开发标签阶段并列口径（原口径不变）

按 2026-09-10 口径修正指令，36 条开发标签**不混入**上述冻结口径；补充计分单独成册，见 [process-pilot-devlabel-scoring-v1.md](process-pilot-devlabel-scoring-v1.md)（产物 `artifacts/experiments/process-pilot/mbpp12-ablation-devlabel-scoring-20260910-v1/`，离线重放同一批冻结预测，原口径数字与注册计分一致，标签差异仅 newman_prime、opposite_Signs 两条）。原口径结果（§3–§4 与已注册的消融计分）**逐字节保留、未改动**。

## 6. 后续（未执行）

- **方法冻结候选修复**：捕获 `judge_ablation` 路径的 ValidationError，将自相矛盾的模型输出记为 parse_error 终态行（或在合并前对 model_copy 结果重新校验）。修复会改变实现指纹，必须在本次重复系列之外的新运行身份下进行。
- 81 条保留集保持封存：重复验证已完成；待裁决规范成文与方法冻结后，再按独立标注协议启动。

## 测试状态

针对性验证通过（`tests/test_ablation_devlabel_scoring.py` 4 项、`tests/test_process_development.py` 9 项，含真实产物集成断言；汇总器与补充计分器均经真实 5 轮产物运行通过）；全套件仍有已归因失败（2026-09-09 最终实测 37 失败 / 969 通过 / 13 跳过，归因见 `process-pilot-ablation-v1.md` 测试归因记录及补充；本轮改动未触碰任何指纹内文件）。失败归因只说明原因，不替代全套件通过。
