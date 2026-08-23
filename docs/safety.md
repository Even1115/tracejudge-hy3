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
- `MockProvider` 对未知题目会复用数据集的 `reference_code` 作为 fallback，但这不是仓库内置可信 Fixture，默认同样会拒绝本地执行；
- 真实模型输出、外部数据集代码、Mock fallback 或其他无可信来源的代码，应使用 Docker。只有用户显式传入 `--allow-unsafe-local-exec` 时才会跳过该来源检查，并由用户承担风险；
- 直接调用底层 `TrustedLocalSandbox.run()` 不会自动识别代码来源；来源强制位于 CLI/流水线。

## Docker 不可用时的行为

- `tracejudge doctor` 明确报告 Docker 不可用及原因；
- `tracejudge demo --mock` 仍然可以通过 `TrustedLocalSandbox` 正常运行（因为它只执行内置可信 Fixture）；
- `tracejudge run` / `tracejudge batch` 在 `--provider hy3`（或任何非 mock）且 Docker 不可用时，默认拒绝执行，用户必须显式传入 `--sandbox trusted-local --allow-unsafe-local-exec` 才能继续（并自行承担相应风险）。

## 测试用例执行方式

`sandbox/test_runner.py` 中的父 runner 从 JSON 加载 `args`/`kwargs`，每个用例都启动一个新子进程；子进程通过 `importlib` 动态加载候选代码模块后直接调用目标函数。测试输入**不使用 `eval()`**；子进程用独立报告文件向父进程返回受验证的结构化结果，stdout/stderr 只作为有界诊断信息，不作为结果协议。

这种报告协议会校验状态、用例 ID、必需字段和大小上限，但它不是密码学证明或强对抗性鉴证；在同一容器/用户身份下，敌意候选代码仍可能尝试干扰子进程报告。因此该机制只是 v0.1 的基础工程防护，不应当作对主动攻击者的完整安全边界。
