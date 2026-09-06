# LiveCodeBench 外部验证适配 v1

状态：适配层与批量入口已实现；60 题选择锁已落盘；固定 amd64 镜像已构建，五种状态真实冒烟通过过一次；最终源码绑定门禁待空闲时重新执行；真实 60 题运行尚未执行。见 [正式运行手册](mbpp_lcb_formal_execution.md)。  
实现：`src/tracejudge_hy3/benchmark/livecodebench.py`、`src/tracejudge_hy3/lcb/`  
契约：遵循 `docs/benchmark_contract_v1.md`（v1 未做任何修改）

## 固定的数据身份

数据源为 Hugging Face `livecodebench/code_generation_lite`（仅 code-generation 场景；自修复、测试输出预测、代码执行等开发/调试类题不在此数据集中，适配器另以 schema 校验防御性排除）：

| 项 | 固定值 |
|---|---|
| release 标签 | `release_v6`（窗口 2023-05 至 2025-04，1055 题全集） |
| HF 精确 revision | `0fe84c3912ea0c4d4a78037083943e8f0c4dd505`（2025-06-05，非 `latest`） |
| 数据文件 | `test.jsonl` … `test6.jsonl`，逐文件固定 LFS SHA-256 与字节数 |
| loader 脚本 | `code_generation_lite.py` SHA-256 `973e3798…f7bb` |
| 许可证 | HF 卡片仅标注 `cc`（变体未指明），loader 脚本 DatasetInfo 声明 MIT；两处原文均记录于 `LCB_LICENSE`，未做擅自归并 |
| source manifest | 上述全部 pin 的 canonical SHA-256（`LiveCodeBenchPin.source_manifest_sha256()`） |

任何 revision、文件哈希或字节数变化都会使 `verify_source_directory` / `load_tasks` 失败关闭。

## 官方 checker 边界

| 项 | 固定值 |
|---|---|
| 仓库 | `github.com/LiveCodeBench/LiveCodeBench`（无 git tag，固定精确 commit） |
| commit | `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24`（2025-07-16） |
| 固定文件 | `lcb_runner/evaluation/testing_util.py`、`compute_code_generation_metrics.py`、`lcb_runner/benchmarks/code_generation.py`、`lcb_runner/prompts/code_generation.py`，逐文件 SHA-256 |

已核验的官方语义（pinned `testing_util.py` stdio 分支）：

- 每测试点结果码：`True`=通过、`-2`=wrong answer、`-3`=超时、`-4`=运行时错误；
- 语法错误在官方 harness 内经 `compile_code` 异常落入 `-4` 桶，官方不单独报告编译错误——因此容器入口在调用官方 checker 前附加一次纯 `compile()` 语法探针，归一化时将语法失败区分到 `COMPILE_ERROR`；
- metadata `error_code = -5`（TestRunnerError）是官方测试框架自身异常，归一化为 `INFRASTRUCTURE_ERROR`，绝不计入候选错误率；
- 评测样本 `input_output` 按 公开测试 + 隐藏测试 顺序合并，适配器据此把每测试点结果切成 `public`（PUBLIC）与 `private`（HIDDEN）两个脱敏分组。

隔离不变量：

- 宿主进程**绝不** `exec`/导入/编译候选代码；候选只在隔离容器内的官方 checker 中运行（容器 runner 已实现：`src/tracejudge_hy3/lcb/`，见下节）；
- 宿主**绝不**解码 `private_test_cases`（官方 loader 用 `pickle`，解码本身即执行数据）；隐藏测试仅以原始字节参与哈希，并作为不透明字符串原样传入容器；
- 归一化结果只保留分组状态、失败计数、总测试数、耗时与原始结果哈希；`error_message`/`inputs`/`expected`/`output` 一律不得离开私有边界（原始结果 schema 为精确字段集，多余字段直接拒绝）。

## 隔离容器 runner（`src/tracejudge_hy3/lcb/`）

| 组件 | 职责 |
|---|---|
| `lcb/container_entrypoint.py` | 容器侧入口：校验请求 → `compile()` 语法探针（`compile_ok`）→ 容器内按官方链路解码公开/隐藏测试 → 复刻官方 `check_correctness` 全局超时包装与 `evaluate_generations_by_problem` 单候选语义（`[-2]` 默认、numpy 类型修正、异常→`-5`）调 pinned `testing_util.run_test` → 写出严格 11 字段 raw result |
| `lcb/docker_runner.py` | 宿主侧 runner：staging、加固 `docker run`、cleanup、raw result 读取与身份交叉校验、基础设施错误映射 |
| `docker/livecodebench/Dockerfile` | 固定执行镜像 `tracejudge-lcb-checker` |

镜像 pin：

| 项 | 固定值 |
|---|---|
| 基础镜像 | `ganler/evalplus@sha256:26b118098bef281fe8dfe999bf05f1d5b45374b4e6c00161ec0f30592aef4740`（原 Python digest 实为 arm64，已替换） |
| numpy | `2.2.5`（pinned checker commit 自身 `uv.lock`；stdio 分支唯一第三方依赖） |
| checker 源码 | build 时从 pinned checkout COPY 进 `/opt/lcb`，运行容器不再挂载 checkout |
| 平台 | `linux/amd64`；runner 接受 `sha256:<64hex>` 本地 image ID 或 `tracejudge-lcb-checker@sha256:<64hex>` registry digest，tag 一律拒绝，两种身份分别校验 |
| 本机 image ID | `sha256:2afa62ef77c6a26c119d41a79df18528668b873ccc840564bf7ffce360d43e55`；由 `docker image inspect --format='{{.Id}}' tracejudge-lcb-checker` 获取 |

加固不变量（与 `evalplus/docker_runner.py` 同型）：`--network none`、`--read-only`、只读 `/control` bind（entrypoint + request + candidate，落锁 0555/0444）、**唯一**可写挂载为 host 预建的精确文件 `/output/result.json`（非目录）、`--cap-drop ALL`、`no-new-privileges`、`--memory/--memory-swap/--cpus/--pids-limit`、tmpfs `/tmp` noexec、`--ulimit fsize`、代理环境变量清空、有界 subprocess（stdout/stderr 上限）、detached + `docker wait` + 任何路径 `docker rm -f -v` 兜底。

官方语义复刻要点（pinned 源码核验）：

- `grade_stdio` 首个失败测试即截断返回（prefix 语义），未执行的测试不出现在结果里——归一化的 `total_test_count` 是**实际执行数**，不伪造完整长度；
- 官方全局超时（`check_correctness` 的 `metadata_list[0]` IndexError 路径）落入 `-5` TestRunnerError = 基础设施；
- raw candidate 过探针但官方 `compile_code` 包装失败（`run_test` 返回 `None`）→ 解包异常 → `-5` = 基础设施，不算候选编译错误；
- 入口常量 `CHECKER_COMMIT`/`EXECUTOR_ID` 与 11 字段 schema 为自包含副本，测试断言与适配器 pin 零漂移。

基础设施错误映射：容器创建失败 `container_start_error`、等待超时/取消 `container_timeout`、非零退出 `container_exit_error`、结果缺失/schema 漂移/身份不符 `invalid_raw_result`、镜像 digest 不符 `image_mismatch`、Docker 不可达 `docker_unavailable`——全部归一为 `infrastructure_status="error"` 的合法 raw result，由适配器归一为 `INFRASTRUCTURE_ERROR`，绝不计入候选错误率。

## 60 题选择协议（`LCB60_PROTOCOL`，算法标识 `lcb60-…-v1`）

1. 仅保留 stdio 题：`metadata.func_name` 缺失、`starter_code` 为空、公开测试 `testtype` 全为 `stdin`；函数式（call-based）题排除；
2. 日期窗口：`contest_date ∈ [2024-06-01, 2025-04-30]`（始于 release_v2 结束之后，故 `test.jsonl`/`test2.jsonl` 不可能贡献；止于 release_v6 边界）；
3. 难度分层：使用数据集公开难度（easy/medium/hard 各 20 题）；难度缺失或未知值直接报错，**不**伪造为 `unknown`；
4. 排序键：`sha256(f"{seed}\0{question_id}")` 升序，seed = `20260904`；层内取前 20；
5. 输出顺序固定为 easy ×20、medium ×20、hard ×20，由 `ordered_ids_sha256` 绑定；
6. 任一难度层合格题不足 20 时失败关闭。

选择 lock（`build_selection_lock` / `verify_selection_lock`）把 cohort、协议、pin 三元绑定；`verify_selection_lock` 从数据完整重算并逐字段比对，任何漂移失败关闭。真实选择已落盘于 `artifacts/datasets/livecodebench/selection60.json`（60 条，各难度 20），公开投影为同目录 `tasks60.json`。源数据包含窗口之外的空公开样例记录（如 abc350_c），加载层允许空 list，不能因此拒绝整个 release；非 list 仍拒绝。加载时仅保留所选题的隐藏字符串，避免常驻整份 4 GB 语料；从不解码隐藏测试。

## Prompt 与 Solver 可见面

`BenchmarkTask.prompt` 逐字复刻官方 generic stdio 模板（`### Question:` / `### Format: Read the inputs from stdin …` / ` ```python\n# YOUR CODE HERE\n``` `），并附加显式的 `### Public Sample Tests` 段（仅解码后的公开样例），模板版本 `official-generic-stdio-plus-public-samples-v1`。Solver 可见：公开题面、stdin/stdout 格式说明、公开样例。不可见：隐藏测试（从未解码）、官方参考实现、checker 内部数据。契约字段无需扩展，v1 足够表达。

## Smoke test（真实运行前门禁）

`run_smoke_check(dataset_dir, checker_dir, pin)` 三项全过才 `ready`：

1. **数据身份**：6 个数据文件 + loader 脚本的存在性、非符号链接、字节数、SHA-256 与 pin 完全一致；
2. **checker 身份**：4 个固定 checker 文件哈希一致，且 `git rev-parse HEAD` 等于固定 commit；
3. **容器环境**：Docker CLI 存在且 daemon 可达。

全部为可注入实现（command runner / which），离线测试不需要 Docker 或网络；该门禁不执行任何候选代码或官方评测。

## 测试

`tests/test_livecodebench_adapter.py`（合成 fixture，无网络/无 Docker/无真实数据）：最小 fixture → frozen `BenchmarkTask`；pin/篡改/重复题号失败关闭；选择确定性、分层配额、输入顺序无关、薄 strata 失败关闭；lock 绑定与漂移拒绝；pass / wrong answer / timeout / 运行时错误 / 编译错误 / TestRunnerError / 基础设施错误 / 未执行 的归一化映射；跨题结果、候选哈希错配、schema 不完整或多字段、非法结果码的失败关闭；私有 canary 不泄漏；manifest 绑定；smoke check 三分支。

`tests/test_lcb_docker_runner.py`（fake docker CLI，无真实容器/网络/候选执行）：加固 argv 全参数断言（network none、read-only、cap-drop、精确文件 bind、资源限额、fsize、cleanup）；非 digest 镜像与非 amd64 平台拒绝；镜像 digest 核验；pass / wrong answer / timeout / 运行时错误 / 编译错误 / TestRunnerError 端到端（runner → raw result → 适配器归一化）；容器超时/创建失败/非零退出/结果缺失/schema 漂移/身份不符的基础设施映射；控制目录 0555 落锁与 staging 内容核对；entrypoint 常量与 schema 零漂移；entrypoint 纯逻辑（请求校验、compile 探针、官方 pickle 解码链——fixture 由测试自建、结果码白名单、numpy 类型修正 stub）。

## 正式入口与恢复

`scripts/run_livecodebench.py` 支持 `--prepare`、`--preflight`、`--phase all|generate|execute`、`--resume --run-id` 和 `--report-only`。
`lcb/generation.py` 提供独立 stdio 提示词，允许 stdin/stdout，不复用函数任务中禁用 print 的约束；复用 Hy3 的有限重试和结构解析修复。
`lcb/experiment.py` 按题原子落盘，记录来源/配置/代码哈希与事件链；仅补生成失败与基础设施失败，保留所有成功候选和已完成官方判定。
报告区分生成覆盖率、全 60 分母通过率、实际执行分母通过率及基础设施计数，并按难度分层；过程 Judge 不在本轮入口中运行。
运行顺序、冻结与报告汇总命令见 [正式运行手册](mbpp_lcb_formal_execution.md)。
