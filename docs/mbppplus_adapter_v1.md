# MBPP+ 接入适配 v1

状态：适配层与 120 题批量入口已实现，固定镜像已拉取；真实容器预检发现的问题已修复，最终冒烟待资源空闲后验证。见 [正式运行手册](mbpp_lcb_formal_execution.md)。
实现：`src/tracejudge_hy3/dataset/mbppplus.py`、`src/tracejudge_hy3/benchmark/mbppplus.py`、`src/tracejudge_hy3/evalplus_mbpp/`
契约：遵循 `docs/benchmark_contract_v1.md`（v1 未做任何修改）

## 固定的数据身份

数据源为官方 EvalPlus release 仓库 `github.com/evalplus/mbppplus_release`，即 pinned EvalPlus 评测器在隔离容器内实际消费的同一份语料：

| 项 | 固定值 |
|---|---|
| release 标签 | `v0.2.0` |
| 精确 revision | `64fc4195b858a17cdfdb3324f0baf37939144e14`（commit message `feat: mbpp v0.2.0`，已联网核验） |
| 数据文件 | `MbppPlus.jsonl`（gzip 资产 `MbppPlus.jsonl.gz`） |
| 任务数 | 378（HF `evalplus/mbppplus` 卡片行数一致；题号 `Mbpp/2`…`Mbpp/809`，不连续） |
| task_id 形式 | 字符串 `Mbpp/<非负整数>`（正则 `^Mbpp/(?:0\|[1-9][0-9]*)$`，已对官方资产首条记录核验） |
| 许可证 | `apache-2.0` |
| 官方 dataset MD5 | `ee43ecabebf20deef4bb776a405ac5b1` |
| source manifest | `data/manifests/evalplus_mbppplus_64fc4195.json`（记录 revision、许可证、expected_task_ids、逐文件 SHA-256/字节数、release 资产哈希） |

任何 revision、文件哈希、题号集合或 manifest 字段漂移都会使 `convert_mbppplus` 失败关闭。

## 公开投影边界

每行原始记录含 9 个字段：`task_id`、`prompt`、`entry_point`、`canonical_solution`、`contract`、`base_input`、`plus_input`、`atol`、`assertion`。适配器只把前 3 个公开字段投影为 `ProblemSpec`：

- `prompt` 是 docstring 风格自然语言题面（官方不含 `def` 行）；官方只发布函数名（`entry_point`），不发布参数签名，因此投影使用占位签名 `def <entry_point>(...):` 并打 `signature_not_published` 标签，不虚构参数；
- MBPP+ 官方不发布难度标签，`difficulty` 恒为 `unknown`，`BenchmarkDataset.capabilities` 不含 `PUBLISHED_DIFFICULTY`；
- `canonical_solution`、`contract`、`base_input`、`plus_input`、`atol`、`assertion` 永不离开私有边界：投影只做形状校验，不复制、不执行；产物 canary 测试断言这些字段不出现在任何公开 artifact 中。

## 契约桥接（frozen v1，无扩展）

`benchmark/mbppplus.py` 的 `MbppPlusBenchmarkAdapter` 同时满足 `DatasetAdapter` 与 `ExecutionResultAdapter`：

- `TaskInterface.FUNCTION` + `EvaluationMode.GENERATION`，语言 `python`；
- 与 HumanEval+ 共用 `_evalplus_execution.normalize_evalplus_execution_result`（抽取的最小可复用部分）：`base`/`plus` 两个 HIDDEN 分组，只保留分组状态与失败计数；`infrastructure_status` 为 `ok`/`error`/`mocked` 分别映射为候选结果、`INFRASTRUCTURE_ERROR`、`NOT_RUN`，三者绝不合并；
- 本轮 MBPP/LCB 修复不改动 HumanEval+ 的 `evalplus/` 包或 `benchmark/humanevalplus.py`；另一窗口的 HumanEval+ 改动保留不动。

## 执行边界

- 候选代码只在 pinned、断网的官方 EvalPlus 容器内执行；`evalplus_mbpp/` 是 `evalplus/` 的 MBPP+ 对等包，单独成包以保持 HumanEval+ 阶段二实现指纹逐字节不变；
- 官方 raw schema 与 HumanEval+ 相同（`date`/`hash`/`eval`，状态仅 `pass`/`fail`/`timeout`）；`fail` 合并 wrong answer 与候选异常，无法细分，summary 中 `execution_error_count` 恒为 `None` 并标注 `not_available_in_pinned_evalplus_raw_schema`；
- 断点恢复要求 dataset manifest、候选字节、实现指纹、executor/镜像身份与资源限制完全一致，任一变化拒绝续跑。

## 官方数据 quirk（已核验）

`Mbpp/793` 在 pinned v0.2.0 语料中 `plus_input` 为 `{}`（空 dict，全语料仅此一例）。已对官方源码核验语义：`get_mbpp_plus` → `mbpp_deserialize_inputs` 对 793 无特殊分支，原样透传；`trusted_exec`/`untrusted_check` 迭代零次 → plus 组 0 个测试、**空洞通过**。适配器据此只接受「list 或空 dict」，非空 dict 仍失败关闭。其余 377 行 `base_input`/`plus_input` 均为 list。

真实镜像的官方 loader 会把部分输入反序列化为 set/tuple；不能直接对 native 对象做 JSON 哈希或再次序列化成 override。容器入口先校验官方 release 原始文件 SHA-256/MD5，再读取其原始行，核对题号与公开身份，用原始行构造单题 override，由官方评测子进程恰好反序列化一次。私有指纹口径为 `verified_release_rows_before_mbpp_deserialization_canonical_json_v1`。该固定 release 含 15 个非有限浮点数，私有 JSON 指纹保留官方 loader 接受的 Infinity/NaN 语义；公开 artifact 仍禁止非有限数值。

## 确定性抽样

`select_mbppplus_problem_ids` / `sample_mbppplus`：仅使用公开题号，`sha256(f"{seed}\0{problem_id}")` 升序排名取前 N，输出按数值序排列，由 `ordered_problem_ids_sha256` 绑定；支持 `--exclude-manifest` 生成互斥队列。120 题抽样与完整 378 题实验共用同一算法与身份绑定。

## CLI 路径

```
tracejudge dataset convert-mbppplus --input MbppPlus.jsonl --revision 64fc4195… --manifest data/manifests/evalplus_mbppplus_64fc4195.json --output-dir <dir>
tracejudge dataset sample-mbppplus --dataset <full>/problems.jsonl --manifest <full>/dataset_manifest.json --count 120 --seed <seed> --output-dir <dir>
tracejudge dataset validate --dataset <dir>/problems.jsonl
tracejudge evalplus-mbpp --dataset-manifest <sample>/dataset_manifest.json --candidates candidates.jsonl --executor docker|mock [--resume-run-id <id>]
```

## 正式入口与剩余门禁

完整 378 题投影与 `sample120` 均已生成；本轮先运行固定 120，不扩至 378。
`scripts/run_mbppplus.py` 串接基线生成、严格候选导出、官方执行和组合报告，支持 `--resume`、`--phase`、`--preflight`、`--report-only`；默认容器并发 1。
真实运行必须通过 pass/WA/TLE 三项冒烟与 120 题公开身份核验。生成覆盖不足时保留成功结果，返回未完成，补跑失败项后再执行。
具体无费用准备、固定代码版本、付费启动和报告合并命令见 [正式运行手册](mbpp_lcb_formal_execution.md)。
