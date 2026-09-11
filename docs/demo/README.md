# TraceJudge-Hy3 竞赛演示素材

## 当前全流程录制

- [九幕展示流程与录制脚本](full_flow_storyboard.md)：操作步骤、字幕、真实 Hy3 与离线预演分工。
- 工作台录制入口：`http://127.0.0.1:8765/?recording=1`，运行后手动逐幕展示、首错高亮、公开对照与结果总览。
- 新增“重点案例”入口：`/?case=tuple_str_int` 展示官方测试通过但边界承诺有缺陷；`/?case=find_char_long` 展示构造探针中计划 `>= 4` 与代码 `> 4` 的失配。两个入口均位于现有“典型案例”页，操作与证据说明见 [典型过程案例](featured_process_cases_v1.md)。
- 新增「60 秒看懂项目」四站导览：`/?tour=1` 或页面右上角按钮进入，依次展示重点案例 tuple_str_int、构造探针 find_char_long、已有证书重放结果与最新补充验证；只读取已有材料，不触发运行。操作与讲解建议见 [四站导览说明](tour_guide.md)。
- “效果与成本”入口／第 9 幕：新 Demo 的 Solver／Judge 逐次请求明细，以及已发布的五方法效果与成本对照。单条用量自动保存为 JSONL，并随结果 JSON 导出。
- Windows：`.\scripts\run_recording_demo.ps1` 自动检查并按需启动 Docker Desktop；加 `-Offline` 可跳过 Docker 进行离线预演。macOS / Linux：`./scripts/run_recording_demo.sh`。

README 的 GIF、截图和主 MP4 已更新为 2026-09-10 当前工作台画面；六幕 HTML 和 `qa/` 仍为历史素材，保留各自口径。

## 交付物

- [`assets/tracejudge_hy3_contest_demo.mp4`](assets/tracejudge_hy3_contest_demo.mp4)：2026-09-10 新版工作台录屏，107 秒；具体演示内容、模型调用与结果以画面内标识为准。文件 SHA-256：`580563829fc9dfd1d4fe9350633c2601711cef057e1b1605a9ccbd0f71c1cb5b`。
- [`assets/tracejudge_hy3_preview.gif`](assets/tracejudge_hy3_preview.gif)：2026-09-10 更新，16 秒、1440×1000、四张真实浏览器截图组成的循环 GIF；依次展示九幕入口、公开过程失配案例、最新补充验证和五轮消融结论，不是实时评估录屏。
- [`assets/tracejudge_hy3_process_case_20260910.png`](assets/tracejudge_hy3_process_case_20260910.png)：第 7 幕公开冻结案例，展示说明与代码的矛盾。
- [`assets/tracejudge_hy3_current_validation_20260910.png`](assets/tracejudge_hy3_current_validation_20260910.png)：第 8 幕最新补充验证完整区域，保留三个实验的指标、来源状态和限制。
- [`contest_silent_demo.html`](contest_silent_demo.html)：六幕自动播放/手动切换的代码原生演示页。
- [`qa/`](qa/)：经过浏览器检查的六张 1280×720 源画面。

## 真实流水线录屏演示（本目录新增）

- [`real_recording_guide.md`](real_recording_guide.md)：用于现场真实录屏的本地演示页（`scripts/run_recording_demo.sh` 启动，仅绑定 127.0.0.1）。与上面的确定性渲染视频不同，该页面的按钮真实启动项目评估流水线并展示当次执行结果；支持公开 Fixture 与真实 Hy3 两种严格分离的模式。

## 证据边界

演示中的 57 条轨迹、285 个配对判断、98.2% 最佳观察检测准确率和 2.33% Full TraceJudge 误报率来自已发布的阶段四竞赛结果总览，在第 8 幕标注为“历史阶段四冻结研究 · 57 条轨迹”。其下方另有“2026-09-10 最新补充验证”卡片：36 条开发集重计分（33/36、错误召回 2/5、误报 0/31）、五轮冻结消融重复（rep1 的 D 优势未稳定复现）、3 条构造定位探针（检出 3/3、精确位置 0/3），逐项 SHA-256 绑定来源，卡片自带限制说明（开发标签非独立留出集、探针全为构造错误、81 条保留集仍封存、各实验不合并排名）；可发布汇总见 `docs/releases/current_validation_summary_v1.json`，页面内“成果总览（只读）”入口指向 `docs/contest_results_overview.md`。“方法对比”页为 2026-09-09 历史 12 条三方法试点，“证据消融”页为同日历史单轮（rep1）2×2 消融加五轮重复结论，三者与当前单条 Demo 分别统计，不能合并为同一成绩。`safe_mean` 案例来自仓库公开自建 Fixture；画面中的 `hidden` / `challenge` 只是该 Fixture 的内部类别名称。

GIF 与两张 README 截图由已核验页面确定性采集；新版 MP4 是用户在本地工作台完成的屏幕录制。视频不等同于第三方隐藏测试或阶段三独立复现实验，模型参与、公开 Fixture、历史案例和聚合实验仍以画面标识区分。总体研究结果继续保留其探索性证据边界。

## 本地预览

在仓库根目录启动只绑定本机的静态服务：

```bash
python3 -m http.server 8766 --bind 127.0.0.1
```

然后打开：

```text
http://127.0.0.1:8766/docs/demo/contest_silent_demo.html?autoplay=1&recording=1
```

手动预览时可以使用左右方向键切换，按 `R` 重新自动播放。

## 重新渲染

以下命令仅适用于历史六幕素材，会覆盖当前主 MP4 和 `tracejudge_hy3_preview.gif`，不要在发布目录直接运行。新版图片使用 Playwright + 本机 Chrome 从工作台采集，GIF 将四张 1440×1000 截图按 3/4/5/4 秒编码，循环播放；采集仅允许本机 GET 请求，不发起评估。

需要 macOS 的 AVFoundation 与系统自带 `avconvert`。先确保 `qa/contest_scene_01.png` 至 `qa/contest_scene_06.png` 存在，然后执行：

```bash
clang -fobjc-arc -fblocks \
  -framework Foundation -framework AVFoundation -framework CoreGraphics \
  -framework ImageIO -framework VideoToolbox -framework CoreVideo -framework CoreMedia \
  scripts/render_contest_demo.m -o /tmp/tracejudge-render-contest-demo

/tmp/tracejudge-render-contest-demo

avconvert \
  --source /private/tmp/tracejudge_hy3_contest_demo_source.mov \
  --preset Preset1280x720 \
  --output docs/demo/assets/tracejudge_hy3_contest_demo.mp4 \
  --replace
```
