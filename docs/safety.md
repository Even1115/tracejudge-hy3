# 安全说明 (v0.1)

## 核心原则

代码执行沙盒提供的是**基础隔离（basic isolation）**，不是绝对安全保证。任何声称"绝对安全"的说法都是不准确的；v0.1 的目标是把明显的、常见的风险面（网络访问、资源耗尽、文件系统写入、危险权限）降到较低水平，而不是防御一切可能的容器逃逸或侧信道攻击。

## DockerSandbox（真实模型代码的默认执行方式）

通过 Docker CLI 启动一次性容器（[`src/tracejudge_hy3/sandbox/docker_backend.py`](../src/tracejudge_hy3/sandbox/docker_backend.py)），使用的限制：

- `--network none`：禁止网络访问；
- `--memory` / `--cpus`：内存与 CPU 限制（默认 256m / 1 核，可通过环境变量调整）；
- `--pids-limit 128`：限制进程数，缓解 fork 炸弹；
- `--cap-drop ALL`：丢弃全部 Linux capabilities；
- `--security-opt no-new-privileges`：禁止提权；
- `--read-only`：容器根文件系统只读；
- `-v <tmpdir>:/sandbox:ro`：代码与测试以只读方式挂载；
- `--tmpfs /tmp` + `PYTHONDONTWRITEBYTECODE=1`：避免依赖容器内可写的持久层；
- 宿主父进程通过 `subprocess.run(..., timeout=...)` 设置容器整体超时；容器内的 runner 又为每个测试用例新建子进程，由父进程执行单例超时和进程组终止，不依赖候选代码可覆盖的 `signal.alarm`；
- 每个用例的 stdout 和 stderr 分别最多保留 64 KiB，同时持续排空管道以避免候选代码靠大量输出阻塞 runner；序列化结果和报告也有独立上限；
- 容器使用唯一名称；正常退出依赖 `--rm`，超时、启动异常或异常退出路径会额外执行 `docker rm -f <container_name>` 强制清理，清理失败会写入错误信息。

`tracejudge doctor` 会检测 `docker` CLI 是否存在、`docker info` 是否可执行，并明确报告结果（不会假装 Docker 可用）。

## TrustedLocalSandbox（仅限可信代码）

`src/tracejudge_hy3/sandbox/trusted_local.py` 只做同机进程级隔离：外层 runner 由宿主进程设置整体超时，runner 内部为每个用例新建 Python 子进程，单例超时时终止整个进程组，并对 stdout/stderr 做有界捕获。它仍然**没有网络隔离、没有 CPU/内存等资源限制、没有权限降级**。因此：

- CLI/流水线默认只允许仓库内置、且生成结果与 Fixture 精确匹配的 Mock 解答（如 `tracejudge demo --mock`）；
- 阶段三 `counterfactual-execute` 是独立入口，只接受 `data/phase3/public_counterfactuals_v1.json` 的固定 SHA256 `a6195fb0867c69607bfa7a346b8112c49dfbe4d9d85700e2238d5bb1e22731df`；执行前还要求每段代码只有一个与公开签名匹配的顶层函数、不得 import、不得调用 `eval`/`exec`/`open`/`compile`/`__import__` 等危险入口。任何字节变化都会在本地执行前拒绝；
- Gate C `phase3 paired-preflight` 只读两份冻结 manifest 和方法/Prompt/schema 身份，不读取候选正文、不执行候选或方法、不创建产物，不连接 Provider、Docker 或网络；
- Gate D `certificate-preflight` / `certificate-generate` 只读取并重新绑定冻结 manifest、SHA256 精确白名单公开源和 Gate B 脱敏证据，不执行候选；`phase3 replay` 不接受证书携带代码，只从同一精确白名单源恢复候选并执行一个公开用例。证书/manifest 发布前经过敏感键和 canary fail-closed 检查；
- Gate E1 `annotation-packet-preflight` 仅重建和哈希 57 条冻结白名单材料，不执行候选、Provider、Docker 或网络，也不写入文件；`annotation-packet-export` 只把盲法 packet、未填写模板和协调者专用 identity map 原子写入 Git-ignored 的 `0700/0600` 私有目录。packet 在写入前去除反事实构造/预期元数据、方法预测与官方隐藏输入，identity map 不得交给独立标注者；
- Gate E2 `annotation-labels-check` 只读 packet manifest/模板和 opaque working 标签，不打开 identity map；其他人可见的文件权限会被拒绝。只有完成 57/57 且行顺序、协议、标注者、轮次和盲法元数据全部一致时，冻结预检才读取 identity map 并回连 cohort。冻结产物仅写入新的 Git-ignored `0700/0600` 目录，不覆盖旧版本，不打印标签分布、理由或真实 trace 身份；
- Gate E3 `evaluate-preflight` 只按字节哈希绑定已冻结私有标签，标签内容不进入任一方法 Prompt；预检不创建目录、不连接 Provider/网络。`evaluate` 必须显式确认真实 Provider，拒绝未配置或模型不一致的 Hy3；非敏感 Provider 配置、标签三个哈希和完整实现哈希全部进入 resume identity。交通层禁止自动重试，仅解析失败允许一次脱敏结构化修复；
- Gate E4 `statistics-preflight` / `statistics` 不打开 Provider raw、候选正文或隐藏评测内容，只读取冻结标签记录与 E3 的结构化结果。输入必须匹配用户显式给出的 run manifest/results/index SHA256、完整 trace-major 顺序和标签三个哈希；公共 report 只含聚合计数并通过敏感键/canary 检查，不含 trace ID、标注理由或逐条方法预测。正式统计默认拒绝 dirty worktree；
- Gate C 内部 writer 把 Provider raw 仅写入 Git-ignored、`0700/0600` 的 invocation 目录；公开 `results.jsonl` 不保存 raw 正文，并对敏感键和调用方 canary 执行 fail-closed 检查。严格解析器不从 Markdown 围栏或前后文中抽取 JSON，只允许一次脱敏 schema 修复，不对 Provider 错误自动重试；
- `MockProvider` 对未知题目会复用数据集的 `reference_code` 作为 fallback，但这不是仓库内置可信 Fixture，默认同样会拒绝本地执行；
- 真实模型输出、外部数据集代码、Mock fallback 或其他无可信来源的代码，应使用 Docker。只有用户显式传入 `--allow-unsafe-local-exec` 时才会跳过该来源检查，并由用户承担风险；
- 直接调用底层 `TrustedLocalSandbox.run()` 不会自动识别代码来源；来源强制位于 CLI/流水线。

上述阶段三白名单和 AST 检查是来源完整性措施，不把 `TrustedLocalSandbox` 升级为网络/资源/权限隔离环境。正式证据只运行仓库公开自建 Fixture；HumanEval+ 候选、外部代码或真实模型输出不进入该入口。

## Docker 不可用时的行为

- `tracejudge doctor` 明确报告 Docker 不可用及原因；
- `tracejudge demo --mock` 仍然可以通过 `TrustedLocalSandbox` 正常运行（因为它只执行内置可信 Fixture）；
- `tracejudge run` / `tracejudge batch` 在 `--provider hy3`（或任何非 mock）且 Docker 不可用时，默认拒绝执行，用户必须显式传入 `--sandbox trusted-local --allow-unsafe-local-exec` 才能继续（并自行承担相应风险）。

## 阶段二 EvalPlus 容器边界

`tracejudge evalplus --executor docker` 不复用通用 `DockerSandbox`，而是使用专用的官方 EvalPlus 执行协议。它固定官方镜像 digest 和 `linux/amd64` 平台，并校验镜像内 EvalPlus package `0.4.0.dev2`、源码 commit `f11cfb92c1d52896a87f988cbebbd74727d56c7e`、Python 3.11.10 与 HumanEval+ release v0.1.10 身份。输入题号来自阶段一数据集 manifest（10 题 Pilot 或 45 题研究子集等），每题独立容器执行。运行时使用：

- `--pull never` 防止执行时静默替换镜像；运行前通过 `docker image inspect` 验证 RepoDigest、OS 和 architecture；
- `--network none`、`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`；
- 显式 `--memory 4g --memory-swap 4g --cpus 1 --pids-limit 128`；
- 只读挂载当前单题的 `/control` 输入（宿主 staging 目录 `0555`、文件 `0444`）；不挂载仓库、用户主目录、`.env`、SSH 目录或 Docker socket，不转发宿主环境变量，并显式清空大小写代理变量；
- 只向 `/tmp` 提供带 `noexec,nosuid,nodev` 的 1 GiB 有界 tmpfs，并将 EvalPlus 子进程的 `HOME` / `XDG_CACHE_HOME` 重定向到其中；
- task 容器只额外得到两个预创建、宿主拥有的精确文件 bind（官方 raw 与控制结果），不暴露可枚举的宿主可写目录；每个文件受 128 MiB `fsize` 上限约束；
- task 先以唯一名称 detached 启动，宿主等待 PID 1 以状态 0 完全退出后才读取这两个文件；随后执行 `docker rm -f -v`，再把 raw 复制为宿主新建的 `0600` inode；
- 容器内可信父进程在评测结束后用 `O_NOFOLLOW | O_TRUNC` 覆写固定结果文件，并复核 inode、大小、SHA256、题号、候选代码哈希和单题 override MD5；
- preflight 使用 `--rm`；超时、取消、启动异常、非零退出或控制协议异常都会按唯一容器名强制清理，创建与取消竞态会在创建成功后再次检查；
- 每题容器有独立宿主外层超时，批次调度另有总超时；达到批次截止后，清理命令并行执行（每个最多 10 秒），再给 worker 固定 5 秒确认尾段。未确认退出或清理失败会显式记为 `container_cleanup_failed`，不会当作代码失败或静默成功。

官方 EvalPlus 镜像内的执行用户为 root，候选与 wrapper 仍共享容器 UID；这是当前官方镜像/预置 cache 兼容性的已知限制。manifest 将这一级别明确记录为 `basic_non_adversarial`。强制覆写和退出后读取用于防止常见的意外旧文件/提前结果，但不是对主动恶意候选的密码学完整性证明。EvalPlus 的 `reliability_guard` 也不是安全沙盒。容器已丢弃 capabilities、禁止提权、无网络且不暴露宿主目录，但高对抗场景仍应改用一次性 VM/microVM、独立 UID/进程边界和专用宿主。

### EvalPlus 原始结果的信息分级

EvalPlus raw 会复制候选 solution，并可能记录具体失败输入。因此 `samples.jsonl` 和 `evalplus_raw_results.json` 均是 evaluation-only 受限产物：只写入被 `.gitignore` 覆盖的运行目录，目录权限 `0700`、文件权限 `0600`，不打印、不进入普通日志、不传回 Hy3 或任何后续模型。

`results.jsonl`、`summary.json`、`manifest.json` 和 `execution.log` 全部使用正向白名单：仅保留状态、失败数量、时间、哈希和有界基础设施事件。日志不保存外部 stdout/stderr 原文，只允许长度、SHA256、退出码和清理状态等安全诊断，且总长度限制为 64 KiB。

## 测试用例执行方式

`sandbox/test_runner.py` 中的父 runner 从 JSON 加载 `args`/`kwargs`，每个用例都启动一个新子进程；子进程通过 `importlib` 动态加载候选代码模块后直接调用目标函数。测试输入**不使用 `eval()`**；子进程用独立报告文件向父进程返回受验证的结构化结果，stdout/stderr 只作为有界诊断信息，不作为结果协议。

这种报告协议会校验状态、用例 ID、必需字段和大小上限，但它不是密码学证明或强对抗性鉴证；在同一容器/用户身份下，敌意候选代码仍可能尝试干扰子进程报告。因此该机制只是 v0.1 的基础工程防护，不应当作对主动攻击者的完整安全边界。
