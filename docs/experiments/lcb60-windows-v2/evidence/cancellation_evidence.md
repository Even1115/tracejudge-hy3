# LCB v2：约 300 秒取消的新增证据

记录日期：2026-09-06。本文追加诊断证据，不修改既有结果、冻结源码或正式配置。

## 已确认

用户提供的 OpenRouter AtlasCloud 请求详情显示：

- 创建时间为 `2026-09-06T10:40:29.263Z`，与用户终端最后一次重试的时间相符。
- `finish_reason=cancelled`、`cancelled=true`、`streamed=true`。
- `generation_time=299729` 毫秒，即 299.729 秒；页面总耗时约 301.1 秒。
- 输入 1,131 token，输出 28,463 token；页面计费 $0.023。
- 该记录说明上游产生了计费用量，随后被记录为取消；它不能证明本地收到完整的最终代码。
- 页面未启用 I/O logging。此次诊断不需要启用它，也未采集完整请求或回答。

结合此前六次重试均约 304 秒、多个供应商的生成用时均接近 300 秒，存在某个共同时间限制或连接中断的可能性。**这仍是推断，尚不能定位取消发生在客户端、代理、OpenRouter 还是上游供应商。** `cancelled` 也不能证明用户手动中止了程序。

## 实际执行的离线请求检查

执行命令：

```powershell
wsl -d Ubuntu-24.04 -- /home/even/projects/tracejudge-hy3-lcb60-windows-20260906/.venv/bin/python -B /mnt/e/犀牛鸟/tracejudge-hy3/artifacts/benchmark-readiness/windows-lcb-v2-repeat-20260906T1048Z/probe_request_shape.py
```

结果：**1 passed，0 failed**，详见同目录 `request_shape.json`。

- 使用正式 v2 冻结源码中的 provider 和本机 OpenAI SDK 3.8.0。
- 使用假凭据、自写消息和 MockTransport；socket 连接被禁止，真实模型 API 调用数为 0。
- 实际序列化请求的字段只有 `messages`、`model`、`reasoning_effort`，**没有 `stream` 字段**。
- 测试中使用与正式配置相同的 reasoning effort `high`、timeout 120 秒；HTTP 客户端得到 connect/read/write/pool 各 120 秒，没有通过这些字段设置 300 秒总时限。
- 官方文档通过 `stream=true` 显式启用客户端流式响应。因而截图的 `streamed=true` 与上述请求形状存在需要解释的差异；不能据此断言本地程序已经请求流式，也不能断言服务端实际给本地返回了 SSE。
- MockTransport 证明当前安装客户端的请求形状，不证明历史请求在代理、平台或上游的全部处理过程。

此前三个本地 HTTP fixture 已证实：持续到达的响应块可以让总时长超过单次 read timeout，最终无有效 JSON 时会变为 ProviderResponseError。该实验解释了为什么 120 秒配置不必在总时长 120 秒终止，但不能确定这次真实响应体或取消方。

## 对结果和下一步的影响

此前只读审计中的 v2 结果仍为：53 passed、3 wrong_answer、1 compile_error、1 timeout、2 provider_error；`complete=false`。本次没有再次执行模型生成或容器评测，也没有改写结果。两题 provider_error 尚无有效执行结果，不能作为错误答案替代计数。

下一步应依据现有 generation/request ID 向平台查询取消来源及原因，重点核对约 300 秒时谁关闭或取消了连接，以及 `streamed=true` 对应哪一段链路。页面中的 request ID 截断，本文不猜测或转录不确定的完整 ID。

在取消来源明确前，不继续同样的付费重试，也不据此调整 prompt、reasoning effort、生成预算、正式超时或冻结实现。若平台元数据不足，最小诊断增量可以仅记录响应 Content-Type、状态、generation/request ID、响应字节数、耗时和异常类别，不记录响应正文；该增量应先提出方案再修改源码，不能混入既有冻结 run。

## 官方参考

- [OpenRouter 流式请求与取消](https://openrouter.ai/docs/api_reference/streaming)：显式流式参数及通过断开连接取消的机制；并未据此证明本次取消的触发方。
- [OpenRouter generation 元数据接口](https://openrouter.ai/docs/api/api-reference/generations/get-request-%26-usage-metadata-for-a-generation)：可查询请求与用量元数据。
- [HTTPX timeout 语义](https://www.python-httpx.org/advanced/timeouts/)：read timeout 限制等待下一块响应数据的时间。

本次未调用真实模型 API，未修改实验协议或配置，未公开凭据、隐藏测试或完整模型响应。
