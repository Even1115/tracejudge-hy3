# TraceJudge-Hy3 真实流水线录屏演示指南

本页面用于录制两分钟以内、无旁白的比赛 Demo。与
[`contest_silent_demo.html`](contest_silent_demo.html)（预制画面的确定性渲染）不同，
本页面的按钮会**真实启动项目现有评估流水线**，展示的是当次实际执行结果。

## 启动

```bash
scripts/run_recording_demo.sh
```

等价于 `.venv/bin/python -m tracejudge_hy3.demo_app.server --port 8765`，只绑定 `127.0.0.1`。
可用 `PORT=8766 scripts/run_recording_demo.sh` 更换端口。

- 普通预览：<http://127.0.0.1:8765/>
- 录制模式：<http://127.0.0.1:8765/?recording=1>（隐藏提示文字和重置按钮）

## 两种运行模式

| 模式 | 内容 | 前置条件 |
|---|---|---|
| 公开可复现 Fixture | `safe_mean` 自建公开题目；Mock 生成 + 真实执行静态分析、沙盒测试、四层评估、反例、证书与重放 | 无（离线可用） |
| 真实 Hy3 | 等价于 `tracejudge run --dataset data/sample_problems.jsonl --problem-id safe_mean --provider hy3 --sandbox docker` | 服务端环境变量 `HY3_BASE_URL` / `HY3_API_KEY` / `HY3_MODEL`，且本机 Docker 可用 |

Fixture 模式页面显著标注“公开 Fixture；未调用真实 Hy3”。真实 Hy3 模式忠实展示
当次生成与评估结果（可能正确也可能错误）；未配置、Docker 不可用、超时或解析失败时，
页面只显示简短安全错误，**不会悄悄切换到 Mock**。

API Key 只存在于服务端环境变量，不发送给浏览器、不写入页面、不出现在演示日志中。
服务端只接受白名单模式参数（`fixture` / `hy3`），不把浏览器输入拼接进任何命令；
流水线通过项目 Python API 直接调用，不经过 shell。

## 录制操作顺序（建议 1280×720 或 1920×1080，全屏浏览器）

1. 启动服务，打开 `http://127.0.0.1:8765/?recording=1`。
2. 开始录制。页面首屏为题目与需求（字幕 ① 已就位）。
3. 选择“公开可复现 Fixture”，点击 **开始评估**。
4. 页面依次真实展示：结构化解答（含步骤与代码）→ 四层对齐与静态证据 →
   沙盒测试执行（可见通过、`[]` 触发 ZeroDivisionError）→ 首错定位
   （`S1` / `R1` / `A01_PLAN_CODE_MISMATCH`）→ 最小反例 `[[]]`、
   `confirmed_bug` 证书与重放复现结果 → 结果文件相对路径与耗时。
5. 末尾自动出现“已发布结果总览”：57 条冻结轨迹、285 个配对判断、98.2% 最佳观察
   检测准确率、2.33% Full 误报率、57/57 第一标注者覆盖、0/20 第二标注者、
   easy/medium/hard 代理难度，以及探索性结果声明。
6. 停止录制。总时长约 30–50 秒，余量充足。

若比赛要求展示真实 Hy3：先在服务端 `.env` 或环境变量完成配置并确认 Docker 运行，
**正式录制前先手动点一次真实 Hy3 模式确认可用**（会产生真实 API 调用与费用）。

## 数据来源与一致性

结果总览数字只来自两处公开材料，由
[`src/tracejudge_hy3/demo_app/overview.py`](../../src/tracejudge_hy3/demo_app/overview.py)
集中封装：

1. 优先读取哈希绑定的结构化聚合产物（`phase4.contest_summary.build_contest_summary`，
   需要本机存在 Git-ignored 冻结产物；只含聚合数量）；
2. 否则回退解析受 Git 跟踪的公开 Markdown
   （`phase4_contest_results_overview_v1.md` 与 `phase4_difficulty_proxy_analysis_v1.md`）。

两者同时可用时会互相核对，不一致则拒绝展示；`tests/test_demo_app.py` 锁定上述数字。

## 安全与隐私边界

- 服务只监听 `127.0.0.1`，不对局域网/公网开放；静态文件白名单服务，拒绝路径穿越。
- 页面不读取/展示 `.env`、API Key、Authorization Header、endpoint 地址、绝对路径、
  终端历史或用户名；结果文件只显示 `artifacts/` 相对路径。
- 不涉及 HumanEval+ canonical solution、非公开测试正文、标注者身份映射、
  私有逐轨迹标签/理由或逐条方法预测；Fixture 的 hidden/challenge 是自建公开题目的
  内部类别名称（页面有注明）。
- 每次运行把完整结果 JSON 保存在 Git-ignored 的 `artifacts/demo_web_<mode>_<时间戳>.json`，
  不覆盖已有文件，也不覆盖 `docs/demo/assets/` 中的 MP4/GIF。

## 验证

```bash
.venv/bin/pytest -q tests/test_demo_app.py
```

覆盖：Fixture 模式真实跑通并稳定得到 `confirmed_bug`、Hy3 未配置时诚实报错且
不泄露密钥、未知模式/路径穿越被拒绝、仅绑定 127.0.0.1、总览两种来源一致。
