"""Render a report from the metadata-only audit; never open original model events."""
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent
WINDOWS_OUT = 'E:/犀牛鸟/tracejudge-hy3/artifacts/reports/' + OUT.name
WINDOWS_READINESS = 'E:/犀牛鸟/tracejudge-hy3/artifacts/benchmark-readiness'
audit = json.loads((OUT / 'audit.json').read_text(encoding='utf-8'))
report = audit['report']
ident = audit['identity']
timeline = audit['timeline']
rows = audit['task_statuses']
assert all(v is True for k, v in audit['checks'].items() if k != 'result_file_count')
assert report['planned_n'] == 60 and report['actual_execution_n'] == 58
assert report['execution_status_counts'] == {'compile_error': 1, 'failed': 3, 'passed': 53, 'timeout': 1}
assert report['generation_status_counts'] == {'provider_error': 2, 'success': 58}
assert report['provider_attempts'] == 74 and report['complete'] is False

def local(iso):
    return datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')

def percent(v):
    return f'{v:.2%}'

def rate(metric):
    return f"{metric['numerator']}/{metric['denominator']} = {percent(metric['value'])}"

def interval(metric):
    return '–'.join(percent(v) for v in metric['wilson_95'])

metric_rows = '\n'.join(
    f'| {name} | {rate(report[key])} | {interval(report[key])} |'
    for name, key in [('生成覆盖率', 'generation_coverage'), ('固定 60 题当前通过比例（主指标）', 'pass_full_denominator'),
                      ('已执行题目中的通过率（补充指标）', 'pass_conditional_on_execution')])
difficulty_rows = []
for difficulty, label in [('easy', 'Easy'), ('medium', 'Medium'), ('hard', 'Hard')]:
    d = report['per_difficulty'][difficulty]
    s = d['execution_status_counts']
    difficulty_rows.append(f"| {label} | {d['planned_n']} | {d['actual_execution_n']} | {s.get('passed', 0)} | {s.get('failed', 0)} | {s.get('compile_error', 0)} | {s.get('timeout', 0)} | {d['generation_status_counts'].get('provider_error', 0)} | {percent(d['pass_full_denominator']['value'])} | {percent(d['pass_conditional_on_execution']['value'])} |")
labels = {'failed': '错误答案', 'compile_error': '编译错误', 'timeout': '执行超时', 'not_run': '服务异常；未执行'}
failure_rows = '\n'.join(
    f"| {r['index']} | `{r['task_id']}` | {r['difficulty']} | {labels[r['execution_status']]} | {r['provider_attempt_count_total']} |"
    for r in audit['non_pass_tasks'])
hash_entries = [
    ('固定有序题号', ident['ordered_ids_sha256']), ('selection60.json 文件', ident['selection_file_sha256']),
    ('公开任务投影（canonical）', ident['tasks_sha256']), ('Prompt bundle', ident['prompt_sha256']),
    ('完整 source_identity（canonical）', ident['source_sha256']), ('FREEZE.json 文件', ident['freeze_manifest_sha256']),
    ('uv.lock 文件', ident['uv_lock_sha256']), ('模型 Provider 源码', ident['provider_source_sha256']),
    ('LCB smoke receipt 文件', ident['smoke_receipt_sha256']),
] + list(ident['execution_source_sha256'].items())
hash_rows = '\n'.join(f'| {label} | `{sha}` |' for label, sha in hash_entries)
raw_hashes = json.loads((OUT / 'result_files_sha256.json').read_text())
control_rows = '\n'.join(f'| `{name}` | `{raw_hashes[name]}` |' for name in ['manifest.json', 'report.json', 'completion_receipt.json'])

text = f'''# Hy3 在 LiveCodeBench 60 题子集上的实验报告

实验日期：2026 年 9 月 6 日。报告版本：v2 运行的阶段性报告，第 1 版。

数据审计截至：{local(audit['audit_time_utc'])}（北京时间，UTC+8）。

Run ID：`{audit['run_id']}`。完整性状态：**未完成，`complete=false`**。

## 摘要

本实验使用 Hy3 对固定的 LiveCodeBench 60 题标准输入输出子集进行 Python 单候选代码生成，并通过固定版本的官方 checker 在隔离 Linux 容器中评测。Easy、Medium、Hard 各 20 题。当前 58 题成功生成并获得有效执行结论：53 题通过、3 题错误答案、1 题编译错误、1 题执行超时；另有 2 题在服务调用重试后仍未获得可评测答案，记录为 `provider_error`。

固定 60 题上的当前通过比例为 **53/60 = 88.33%**；在已执行的 58 题中，通过率为 **53/58 = 91.38%**。后者仅作为补充指标，不替代 60 题分母。两题服务异常均属于 Hard，排除它们会改变已评测样本的构成。本报告保留缺失状态，不将服务异常归为错误答案，也不宣称全部 60 题已完成有效评测。

## 1. 实验目的与范围

本轮考察给定公开题面后生成程序的功能正确性，范围仅为 LiveCodeBench 60。未运行过程 Judge，不能据此评价推理过程评分、错误证书或过程评估能力。HumanEval+、MBPP+ 以及其他机器的实验均不纳入本报告。

数据来源为 `livecodebench/code_generation_lite` 的固定 `release_v6`，Hugging Face revision 为 `0fe84c3912ea0c4d4a78037083943e8f0c4dd505`。选择范围为 2024-06-01 至 2025-04-30 的符合适配器条件的标准输入输出任务。仅使用无函数入口、无 starter code 的适用任务。

选择算法沿用仓库固定规则：seed 为 `20260904`，按难度分层，每层按 `SHA-256(seed + NUL + question_id)` 排序取最低的 20 题，再按锁定顺序执行。本报告生成时重新核对了有序题号 SHA-256 和选择锁，没有重新抽样。该样本为人为平衡的固定子集，不代表完整 LCB 或自然难度分布。

## 2. 模型、生成与执行设置

| 项目 | 本轮设置与证据 |
| --- | --- |
| 模型请求标识 | `tencent/hy3`，经 OpenRouter 调用 |
| 已查看的单条请求版本 | 平台详情显示 `tencent/hy3-20260706`；尚未逐请求核实全部实际版本与供应商 |
| 生成语言与接口 | Python 完整程序；stdin 输入、stdout 输出 |
| 生成格式 | `SolutionTrace` JSON；包含代码及可审阅的简要说明 |
| Prompt 版本 | `{ident['prompt_version']}`；完整身份见附录 |
| 输入范围 | 公开题面、公开需求和公开样例；隐藏测试不进入生成消息 |
| 推理配置 | `reasoning_effort=high`，已启用 |
| 单候选规则 | 每题保留成功生成的候选；不会依据测试失败反复生成并择优 |
| 服务错误重试 | `max_retries={ident['provider']['max_retries']}`，即原有额外重试预算；resume 的追加尝试保留历史记录 |
| 结构化输出修复 | `max_parse_repairs={ident['provider']['max_parse_repairs']}`，沿用既有机制 |
| Provider timeout | `{ident['provider']['timeout_seconds']:.0f}` 秒；离线核验为 connect/read/write/pool 的等待配置，不等同于整个调用的总时限 |
| 其他请求参数 | 当前冻结客户端未显式传入 `temperature`、`max_tokens` 或 `stream`；平台默认值没有在现有本地清单中逐项锁定 |
| Checker commit | `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24` |
| 容器平台与并发 | `linux/amd64`；顺序执行，每次一个评测容器 |
| 容器资源限制 | CPU `{ident['limits']['cpus']}`；内存 `{ident['limits']['memory']}`；PIDs `{ident['limits']['pids']}`；tmpfs `{ident['limits']['tmpfs_size']}` |
| 执行时间限制 | 每题 `{ident['limits']['per_task_timeout_seconds']}` 秒；每个测试 `{ident['per_test_timeout_seconds']}` 秒 |
| 执行隔离 | 候选及隐藏测试仅在容器内处理；禁用容器网络，采用只读根文件系统与受限挂载 |

这里的 Provider 等待时间与候选执行时间是不同层面的限制。一次题目执行的聚合耗时可以包含多项测试及启动开销，不能直接与单测试 6 秒限额比较。

## 3. 运行平台与版本冻结

本轮在 Windows 上的 WSL2 / Ubuntu-24.04、x86_64 环境中运行，使用 `/home` 下的 Linux 项目与独立虚拟环境。没有使用 Windows 原生 Python 或复用 Mac 虚拟环境。Docker Desktop 的 Linux 容器路径已通过真实评测验证。

| 项目 | 身份 |
| --- | --- |
| 主项目目录 | `/home/even/projects/tracejudge-hy3-lcb60-windows-20260906` |
| 源分支 | `benchmark/mbpp-lcb-wsl` |
| 源 HEAD | `67085adb75cb64d349f6cb89ded3f27356b5c967`；冻结时另含两处已授权实现修复 |
| 实际运行快照 | `{audit['runtime']}` |
| 实际快照完整 commit | `{audit['runtime_commit']}` |
| 快照状态 | 本次核验 Git 干净；源码身份与运行 manifest、冒烟 receipt 匹配 |
| Python | `{ident['python']}` |
| 绑定依赖版本 | OpenAI SDK `{ident['dependencies']['openai']}`；Pydantic `{ident['dependencies']['pydantic']}`；pydantic-settings `{ident['dependencies']['pydantic-settings']}`；Typer `{ident['dependencies']['typer']}`；Rich `{ident['dependencies']['rich']}` |
| 本地 Docker image ID | `{ident['image']}` |

上述镜像标识为 Docker 的完整本地 `.Id`，不是 registry manifest digest。实验身份以实际冻结快照为准，当前主工作树后续的其他修改不属于本轮版本。

v2 之前的 v1 曾暴露两处实现问题：异常 JSON 没有进入既有 Provider 错误记录流程，以及固定选择中的最大请求超过容器入口原 64 MiB 传输上限。经授权分别增加异常转换、将有界传输上限调整为 128 MiB 后，生成了新的 v2 冻结快照。抽样、题数、prompt、评分、共享契约和时间口径未因此更改。v2 从固定 60 题重新生成，没有导入 v1 候选或合并其得分。

## 4. 运行过程与重试记录

以下时间均为北京时间。时间取自 manifest、事件及 completion receipt；端点之间的耗时包含该阶段的程序开销，不等同于全部时间都在模型计算。

| 阶段 | 时间范围 | 耗时 | Provider 尝试 | 阶段结果 |
| --- | --- | --- | ---: | --- |
| v2 首轮 | {local(timeline['initial_started_at'])} 至 {local(timeline['initial_finished_at'])} | 约 2 小时 28 分 09 秒 | {timeline['initial_provider_attempts']} | 58 题有效生成并执行，2 题 provider_error |
| 同一 v2 的恢复运行 | {local(timeline['resume_started_at'])} 至 {local(timeline['resume_last_event_completed_at'])} | 约 30 分 20 秒 | {timeline['resume_provider_attempts']} | 仅追加两题生成事件，各 3 次尝试；仍均失败 |
| 两个运行阶段合计 | 不含中间暂停 | 约 2 小时 58 分 28 秒 | {report['provider_attempts']} | 最终题目状态未因恢复改变 |

本轮共有 **62 条 generation 事件、58 条 execution 事件，共 120 条事件**。74 次 Provider 尝试中，58 次记录为成功、16 次记录为服务异常。API 尝试次数不等同于题目数或独立有效候选数，不能将本轮描述为“只调用 API 60 次”。

两题 `arc189_d`、`arc186_e` 在 v2 中各有两条 generation 事件，每条含 3 次失败尝试，故各累计 6 次尝试。恢复期间没有追加 execution 事件，已有通过、错误答案、编译错误与超时结果均被保留。

## 5. 总体结果与指标口径

| 题目最终状态 | 数量 | 占固定 60 题 |
| --- | ---: | ---: |
| 通过 `passed` | 53 | 88.33% |
| 错误答案 `failed / wrong_answer` | 3 | 5.00% |
| 编译错误 `compile_error` | 1 | 1.67% |
| 执行超时 `timeout` | 1 | 1.67% |
| 运行时错误 `runtime_error` | 0 | 0.00% |
| 容器执行基础设施错误 `infrastructure_error` | 0 | 0.00% |
| 服务异常且未执行 `provider_error` | 2 | 3.33% |
| 合计 | 60 | 100.00% |

| 指标 | 结果 | 程序输出的 Wilson 95% 区间 |
| --- | --- | --- |
{metric_rows}

88.33% 表示当前固定 cohort 上已实际取得通过结论的比例；它仍受两题服务失败影响，不能解读为模型能力已被完整测定。91.38% 条件于获得有效执行结果，仅作为补充。Wilson 区间沿用原程序输出，不能消除固定分层选择、服务缺失或模型随机性带来的限制，也不应解读为自然题目总体通过率的置信区间。

`execution_pending_n=0` 表示成功生成的 58 题都已有执行结论；它不表示两题 Provider 失败已经完成。因此，结果文件中的 `complete=false` 是正确的完整性标记。

## 6. 分难度结果

| 难度 | 选题数 | 有效执行 | 通过 | 错误答案 | 编译错误 | 超时 | 服务异常 | 通过/选题数 | 通过/有效执行 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(difficulty_rows)}

Easy 与 Medium 均完成全部 20 题有效评测，分别通过 20 题与 19 题。Hard 已执行 18 题，通过 14 题；另外两题仍缺少有效答案。这支持对本子集作分层描述，但不足以推断模型在全部简单题上必然通过，或在完整困难题总体上的准确成功率。

## 7. 未通过及未完成题目

| 固定序号 | 题号 | 难度 | 最终状态 | 本 run 累计 Provider 尝试 |
| ---: | --- | --- | --- | ---: |
{failure_rows}

以上仅报告题号和判定元数据，没有公开隐藏测试或模型回答。错误答案与编译错误的具体代码原因未在本次报告工作中重新分析。`abc389_g` 的已记录执行耗时约 6.720 秒，最终判定为执行超时；没有据此调整原时间限制。

v1 曾受传输上限影响的 `abc373_c` 在 v2 已通过。由于 v2 重新生成了候选，这条结果只能确认本轮该题顺利执行，不能用来单独量化修复带来的模型性能提升。

## 8. 两题服务异常：证据与尚未确认事项

恢复运行的六次请求均先出现 HTTP 200，再在约 304 秒后记录 `ProviderResponseError`。HTTP 接收成功不保证完整回答成功返回。本地失败事件只保留错误类别，`raw_output` 和 `solution` 均为空，因此不能将每次错误都确定为同一种响应解析问题。

用户提供的最后一条 AtlasCloud 请求详情显示：`cancelled=true`、`finish_reason=cancelled`，生成耗时 299.729 秒，总耗时约 301.1 秒，输出计数 28,463 token，费用 $0.023。其时间与最后一次恢复尝试相符。这证明平台记录了生成后取消及用量，不能证明本地获得完整最终代码，也不能证明用户手动取消。

此前六条平台记录涉及多个供应商，生成时长均接近 300 秒，提示可能存在共同时间限制或连接中断；**取消由客户端、代理、OpenRouter 还是上游触发仍未确认**，不得将该推断作为已经定位的根因。

离线检查显示：当前冻结客户端没有显式发送 `stream`，但平台详情记录 `streamed=true`；二者对应哪一段链路尚需核实。三个本地 HTTP fixture 验证了“持续响应块使总耗时超过 read timeout”以及“结束时没有有效 JSON 可产生 ProviderResponseError”的机制；它们不证明真实上游响应体的具体内容。[OpenRouter 流式与取消文档](https://openrouter.ai/docs/api_reference/streaming)及[HTTPX timeout 文档](https://www.python-httpx.org/advanced/timeouts/)用于解释机制，不作为本次故障来源的直接证据。

费用披露：平台截图中的最近六次重试费用合计约 **$0.1178**，是显示金额相加的局部估计，不是完整 v2 或 v1+v2 的总成本。本轮未进行完整账单核对，也未把平台输出 token 数当作最终可用答案长度。

## 9. 验证与结果完整性

以下运行前验证取自此前保存的本机证据，本次生成报告没有重新运行容器或候选。

| 验证 | 记录结果 |
| --- | --- |
| 修复后的相关离线回归 | 125 passed / 0 failed / 0 skipped / 2 deselected；1 条已有 collection warning |
| 两项 deselected 的原因 | 遵守宿主不编译候选、不执行私有测试解码的限制；不记作测试通过 |
| 五种真实容器 fixture | 通过、错误答案、超时、运行时错误、编译错误均达到预期，5/5 |
| 大请求真实容器 fixture | 69,014,330 字节自写请求在新入口通过；旧入口可复现基础设施失败 |
| 最终冻结快照预检 | 60 tasks，退出码 0；收据绑定本机镜像与执行源码 |
| 故障诊断离线 fixture | HTTP timeout 行为 3/3；请求形状检查 1/1；全部不调用模型 |
| 本报告最新只读审计 | 事件链有效；原 summarize 重算等于 report.json；completion receipt 匹配 |
| 身份核对 | 冻结源码、依赖、任务、选择锁、prompt、镜像与冒烟源码绑定一致 |
| 结果保全 | 124 个原始结果文件与上次审计完全一致，且本次检查前后哈希未变 |

本次审计加载已准备的公开任务投影和原始事件，通过仓库既有验证逻辑重算，仅导出必要元数据；没有重新扫描数 GB 原始数据或重新执行隐藏测试。原始数据六文件及 loader 的固定 revision、大小、SHA-256 验证证据来自此前准备和最终 preflight。

## 10. 结论边界与后续处理

当前证据支持：在这组固定 60 题上取得 53 题通过，58 题已有有效评测；在本机 v2 中未观察到容器基础设施失败。两题服务异常仍使本轮处于未完整状态。

本轮只生成每题一个可用候选，不进行多候选择优；但允许既定服务错误重试与结构化输出修复。不能把它包装成零重试 API 测试或官方完整 LiveCodeBench 榜单分数。模型通过别名请求且供应商路由可变化，现有资料未逐请求锁定全部远端模型版本；也没有训练数据无污染保证。本结果不证明过程 Judge 能力。

后续优先根据已有 generation/request ID 核实取消来源。若继续原实验，必须保持同一 v2 快照、run ID 和既有配置；有效 WA/CE/TLE 结论保持不变。任何新增传输逻辑或预算、超时、prompt 变更应另行审查和记录身份，不能静默混入当前 run。若无法补齐，可将本报告作为带有明确未完成标记的阶段成果保存。

## 附录 A：关键身份 SHA-256

| 对象 | SHA-256 |
| --- | --- |
{hash_rows}

| 原始结果控制文件 | 本次审计 SHA-256 |
| --- | --- |
{control_rows}

## 附录 B：证据与产物位置

原始实验目录位于 WSL：

```text
{audit['run_directory']}
```

该目录含模型输出与候选，仅在本机保留；无需将整个目录公开。报告附带的 CSV 和审计 JSON 仅包含统计、题号、状态及版本身份。

- [本次只读审计结果 audit.json]({WINDOWS_OUT}/audit.json)
- [全部 60 题状态表 task_statuses.csv]({WINDOWS_OUT}/task_statuses.csv)
- [原始结果文件 SHA-256 清单]({WINDOWS_OUT}/result_files_sha256.json)
- [只读审计脚本]({WINDOWS_OUT}/audit_current.py)
- [报告生成脚本]({WINDOWS_OUT}/render_report.py)
- [运行前修复与 readiness 证据]({WINDOWS_READINESS}/windows_lcb_readiness.md)
- [恢复运行审计]({WINDOWS_READINESS}/windows-lcb-v2-repeat-20260906T1048Z/repeat_audit.json)
- [取消详情补充诊断]({WINDOWS_READINESS}/windows-lcb-v2-repeat-20260906T1048Z/cancellation_evidence.md)

本次用于核对统计的命令（已执行，离线）：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python -B /mnt/e/犀牛鸟/tracejudge-hy3/artifacts/reports/{OUT.name}/audit_current.py
```

脚本使用独占创建保护已有报告文件；原样重复运行会拒绝覆盖输出。后续更新报告应使用新的报告目录，并保留此次版本。

正式实验历史上由用户执行了真实模型请求；**本次报告生成没有新增真实模型 API 调用，没有修改实验协议、源码、配置或原始结果，没有读取 .env 或 API Key，没有解码隐藏测试，也没有公开凭据、隐藏测试或完整模型原始响应。**
'''

assert text.count('| Hard |') == 1
assert '53/60 = 88.33%' in text and '53/58 = 91.38%' in text
assert 'complete=false' in text
with (OUT / 'experiment_report.md').open('x', encoding='utf-8') as handle:
    handle.write(text)

# Verify the delivered table against the audit, and scan only safe exports.
with (OUT / 'task_statuses.csv').open(encoding='utf-8-sig', newline='') as handle:
    exported = list(csv.DictReader(handle))
assert len(exported) == 60 and len({r['task_id'] for r in exported}) == 60
assert Counter(r['execution_status'] for r in exported) == Counter(r['execution_status'] for r in rows)
assert Counter(r['difficulty'] for r in exported) == {'easy': 20, 'medium': 20, 'hard': 20}
assert sum(int(r['provider_attempt_count_total']) for r in exported) == 74
assert set(exported[0]) == set(rows[0])
for filename in ['experiment_report.md', 'audit.json', 'task_statuses.csv']:
    data = (OUT / filename).read_text(encoding='utf-8-sig')
    assert not re.search(r'\bsk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,}', data)
    assert 'private_test_cases' not in data
    assert '"raw_output":' not in data
    assert '"solution":' not in data
validation = {
    'report_scope': audit['run_id'],
    'task_rows': len(exported), 'all_aggregate_counts_match_audit': True,
    'csv_difficulty_and_attempt_totals_match': True,
    'incomplete_status_and_both_denominators_present': True,
    'safe_export_scan_passed': True, 'real_model_api_calls': 0,
}
with (OUT / 'validation.json').open('x', encoding='utf-8') as handle:
    json.dump(validation, handle, ensure_ascii=False, indent=2)
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir()) if p.is_file()}
with (OUT / 'report_bundle_sha256.json').open('x', encoding='utf-8') as handle:
    json.dump(hashes, handle, ensure_ascii=False, indent=2)
print(json.dumps({'report': str(OUT / 'experiment_report.md'), 'validation': validation}, ensure_ascii=False, indent=2))
