# CodeJudge-Eval v3 预注册实验方案（尚未运行）

状态：**preregistered-not-run**。本文件冻结协议、选择 manifest、Prompt bundle、
运行顺序、失败处理和 scorer；不做任何真实模型调用。两个研究问题分为两个独立
确认性 holdout，不混为一个实验。原 prompt-v3 选择从未运行，因其中仍暴露与 easy
条件相矛盾的 `error_type` 辅助字段而明确 superseded；正式运行只接受 prompt-v4
绑定的新选择文件。

| 字段 | 实验 A | 实验 B |
| --- | --- | --- |
| 实验 ID | `codejudge-eval-v3a-functional` | `codejudge-eval-v3b-granularity` |
| 研究问题 | Judge 区分 AC 与非 AC 的能力 | 标签粒度（easy/middle/hard）是否影响判断 |
| 冻结选择 | `data/codejudge_eval/selection_v3a_v4.json` | `data/codejudge_eval/selection_v3b_v4.json` |
| seed | 20260905 | 20260906 |
| 规模 | 120 候选（60 AC + 60 非 AC） | 50 候选 × 3 粒度条件 = 150 次调用 |
| Prompt bundle | v4（移除 root-cause `error_type`） | v4（同 bundle，三个粒度条件） |

两个 holdout 均**排除 full-v1 已使用的 90 个原始题号**；B 进一步排除 A 占用
的 120 个题号。排除集 hash 记入各自 selection manifest
（`excluded_raw_task_ids_sha256`）。

## 实验 A：平衡的 functional correctness 外部验证

### 设计

- 标签来源：**hard 文件**（最细粒度 gold；functional_correct 与粒度无关）。
- 分层配额：AC = 60；非 AC 按 CE/WA/RE/TLE/MIXED 各 12，共 60。
- 每题至多一个候选；题内取该分层中 `data_id` 最小的候选。
- 候选来源（generator source）与难度分布**只报告、不设配额**，作为描述性
  检查写入分析报告。
- 降级规则（预注册）：AC 池不足配额 → **fail closed**；某非 AC 分层不足 →
  缺口按固定顺序 CE→WA→RE→TLE→MIXED 向有余量的分层再分配，记入
  `degradation_log`；再分配后非 AC 总量仍不足 → fail closed。不允许静默改
  配额。（实际冻结结果：各池充足，`degradation_log` 为空。）

### 预注册指标（主 → 次）

1. **balanced accuracy**（主指标）；
2. sensitivity、specificity（Wilson 95% 区间）；
3. precision、recall、F1；
4. MCC；
5. FPR / FNR（Wilson 95% 区间）；
6. majority-class baseline accuracy（对照）。

失败记录（provider/parse）保留在相应 gold 类的全分母，并在 sensitivity、
specificity、balanced accuracy 与 overall accuracy 中计为未判断正确，不依赖 gold
把失败插补成某个预测类。balanced accuracy 的 95% 区间使用按 gold 正负类分别
重采样的候选级 percentile bootstrap（10,000 次，seed=20260907）；二项比例使用
Wilson 95% 区间。precision 与 MCC 只能在有效二元预测上计算，必须显式标为
`valid-prediction diagnostic` 并同时报告 coverage；F1 使用 valid-prediction
precision/recall，不能替代全分母主指标或隐藏失败率。

## 实验 B：标签粒度效应的配对验证

### 设计

- **同一题目、同一候选**在 easy/middle/hard 三种粒度条件下各判定一次
  （每次调用相互独立）；三文件候选逐字节相同已核验（`(task_id, data_id)`
  键一一对应、code 完全一致）。
- 分层：hard gold 8 个非 AC 类（CE、WA、RE、TLE、WA+RE、WA+TLE、RE+TLE、
  WA+RE+TLE）各 5 个 + AC 对照 10 个 = 50 候选。
- 调用顺序：若固定 easy→middle→hard，则条件与调用位置完全混淆（prompt
  缓存/漂移）。预注册采用 **Latin-square 平衡**：三个轮换
  (easy,middle,hard) / (middle,hard,easy) / (hard,easy,middle) 按条目序号
  mod 3 确定性分配，写入每条 `call_order`；每个条件在首/次/末位出现的
  次数至多相差 1。三次调用之间无状态共享。
- Prompt：v4 bundle 的三个条件变体。**粒度操纵是真实的——每个条件有自己
  的输出空间和 JSON Schema**（见下节"输出空间"），不是同构输出的措辞
  变换。三个条件的 Schema 均不再暴露 root-cause `error_type`。同一候选的三次
  判定经 `normalize_v4_verdict` 映射到公共的规范化
  形式（functional_correct / ce / outcomes 集合）后做配对比较；规范化
  只在条件空间可表达时细化（easy NOT_AC、middle MIXED 的 outcomes 为
  `None`，绝不虚构）。每条件另经 `derive_v4_label` 映射到该粒度自身的
  gold 字母空间做 exact-match 评分。

### 输出空间（每个条件一个 Schema）

| 条件 | verdict 枚举 | execution_outcomes | gold 空间 |
| --- | --- | --- | --- |
| easy | {AC, CE, NOT_AC} | 无此字段 | A/B/C |
| middle | {AC, CE, WA, RE, TLE, MIXED} | 无此字段 | A–F |
| hard | {AC, CE, ERRORED} | ⊆ {WA, RE, TLE}（可多选，不折叠） | A–I |

一致性约束：AC ⇔ functional_correct=true（且 hard 下 outcomes 空）；
CE ⇒ outcomes 空；ERRORED ⇒ outcomes 非空。hard 条件的
`execution_outcomes` **先拒绝任何非 WA/RE/TLE 的取值（fail closed），再做
排序去重**——混合了合法与非法标签的响应整体作废进入 repair 流程，绝不
静默丢弃非法标签。

### 预注册指标与统计

- **主指标**：对每个候选、每个条件先形成“该条件的 functional_correct 是否与
  gold 一致”的二元正确性指标；
  三对条件比较（easy↔middle、easy↔hard、middle↔hard）使用**配对 McNemar
  精确检验**。`b` 固定定义为前一条件正确/后一条件错误，`c` 固定定义为前一条件
  错误/后一条件正确；双侧 α=0.05，**Holm step-down** 处理固定的 3 个比较。
- **检验功效声明（预注册）**：N=50 的配对 McNemar 精确检验功效由不一致
  对数 b+c 决定而非 N 本身。若真实不一致率约 20% 且 b:c=2:1，按 N=50、
  双侧未校正 α=0.05 的精确检验计算，功效约 **0.106**；固定 b+c=10 时约
  **0.104**。Holm 校正后不可能更高。因此本实验定位为**效应量估计 + 精确检验**，
  阴性结果不得解读为“无粒度效应”的证明。
- **次指标**：各粒度下推导标签与 gold 的 exact-match 率（配对差 + 区间）；
  hard 条件的 execution-outcome 集合 exact-match 率；三条件规范化形式的
  成对一致率。
- **配对差区间**：只在两条件均为有效 judgment 的 complete pairs 上计算；采用
  候选级非参数 percentile bootstrap，10,000 次，base seed=20260908，三个比较
  的 functional-correctness 差依固定顺序分别使用 seed+0/+1/+2，native-label
  exact-match 差使用 seed+100/+101/+102。
- **缺失处理与停止条件**：固定 N=50×3，无中期查看（no peeking）。不插补任何
  provider/parse 失败；配对检验采用 complete pairs，并同时报告 planned、complete
  与 incomplete 数量。若任一条件失败率 >5%（50 条中即至少 3 条失败），所有涉及
  该条件的比较记为不可用；Holm 家族仍固定为 3，未使用的槽位机械置 p=1，仅用于
  保持家族大小，报告中的不可用比较不显示 p 值。每条件 full-denominator accuracy
  与 exact-match 仍把失败计为未判断正确。
- **禁止**：不得用三个不相交样本的 accuracy 直接推断粒度效应（full-v1 的
  教训，见 full-v1 报告 §7.2）。

## Prompt bundle v4 冻结

- bundle 版本 `codejudge_eval_judge_only_v4`；hash 绑定：版本、三个条件
  system prompt（各嵌入本条件的 JSON Schema）、各条件输出空间、user/repair
  模板版本、输出 JSON Schema、`max_parse_repairs`、hard 推导表。
- v4 判定 Schema 每条件一个（见上表）：easy/middle 为单 `verdict` 枚举；
  hard 为 `{functional_correct, verdict ∈ {AC, CE, ERRORED},
  execution_outcomes ⊆ {WA, RE, TLE}, explanation, confidence?}`。三个条件均
  **没有 `error_type` 字段**。
- v1（`a10cdc74…`）与 v2 bundle 不受影响，full-v1 仍可验证。
- 旧 v3 bundle hash `581755e5…` 及 `selection_v3a.json` / `selection_v3b.json`
  保留不改，但状态为 superseded-never-run；正式 runner 拒绝加载它们。
- 外部数据从不用于调 Prompt 或阈值；v4 文本冻结后不得修改，如需修改必须新建
  v5 bundle 与新选择，旧版本只保留审计用途。

## 冻结产物核验

```bash
python3 scripts/freeze_codejudge_v3_v4_selection.py  # 已执行；重跑拒绝覆盖
python3 scripts/run_codejudge_eval_v3.py --experiment a --preflight
python3 scripts/run_codejudge_eval_v3.py --experiment b --preflight
```

两个选择 manifest 均含：seed、算法串、配额、排除集 hash、源文件 hash、
prompt bundle v4 hash、有序条目及其 `entries_sha256`、降级日志、状态
`preregistered-not-run`。正式运行 manifest 还必须在首个调用前绑定：本协议 hash、
选择文件 hash、完整有序调用计划 hash、逐文件源码/依赖锁 hash、Python 版本、git
commit、provider 非敏感配置及预注册统计参数。运行脚本支持精确前缀 resume；只有
120/120 或 150/150 覆盖通过后才能生成报告和 completion receipt。
本地原始数据文件可不进入 Git，但其逐文件 hash 必须与选择 manifest 完全一致；正式
输出写入 Git 已忽略的 `artifacts/codejudge_eval_v3/<run-id>/`，目录权限为 0700、
文件权限为 0600，包含原始 provider 响应的文件不得发布。

## 正式真实 API 启动命令

预检必须先返回 `ready_for_formal_real_api=true`，预检不创建运行目录且网络调用数为 0。
正式调用需要显式确认；不提供 `--confirm-real-provider` 时 fail closed：

```bash
python3 scripts/run_codejudge_eval_v3.py --experiment a --confirm-real-provider
python3 scripts/run_codejudge_eval_v3.py --experiment b --confirm-real-provider
```

若进程中断，使用原始 run id 恢复；runner 会验证 manifest、环境与完整记录前缀，
不会重跑已持久化的条件记录：

```bash
python3 scripts/run_codejudge_eval_v3.py --experiment a --run-id <run-id> --resume --confirm-real-provider
```
