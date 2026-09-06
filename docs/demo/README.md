# TraceJudge-Hy3 竞赛无声演示

## 交付物

- [`assets/tracejudge_hy3_contest_demo.mp4`](assets/tracejudge_hy3_contest_demo.mp4)：74 秒、1280×720、无声 MP4。
- [`assets/tracejudge_hy3_preview.gif`](assets/tracejudge_hy3_preview.gif)：12.2 秒、640×360、循环 GIF，用于 README 首屏预览。
- [`contest_silent_demo.html`](contest_silent_demo.html)：六幕自动播放/手动切换的代码原生演示页。
- [`qa/`](qa/)：经过浏览器检查的六张 1280×720 源画面。

## 真实流水线录屏演示（本目录新增）

- [`real_recording_guide.md`](real_recording_guide.md)：用于现场真实录屏的本地演示页（`scripts/run_recording_demo.sh` 启动，仅绑定 127.0.0.1）。与上面的确定性渲染视频不同，该页面的按钮真实启动项目评估流水线并展示当次执行结果；支持公开 Fixture 与真实 Hy3 两种严格分离的模式。

## 证据边界

演示中的 57 条轨迹、285 个配对判断、98.2% 最佳观察检测准确率和 2.33% Full TraceJudge 误报率来自已发布的阶段四竞赛结果总览。`safe_mean` 案例来自仓库公开自建 Fixture；画面中的 `hidden` / `challenge` 只是该 Fixture 的内部类别名称。

视频由已核验画面确定性渲染，不冒充实时 Hy3 调用、第三方隐藏测试录屏或阶段三独立复现实验。当前结果仍是单主标注者、探索性证据，第二标注者一致性尚未计算。

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
