# Screen Demo Pipeline Strategy

更新时间：2026-09-21

## 定位

`screen_demo` 是 V3.4 的 beta pipeline，用于软件功能演示、网页 walkthrough、内部工具培训和版本发布演示。

它的目标不是在当前版本里直接完成高质量自动录屏，而是先建立稳定的生产合同：

- 明确 demo 目标、受众、产品入口和演示步骤。
- 把录屏前置条件、账号/测试数据、隐私遮罩和不稳定页面风险显式化。
- 让录屏、交互高亮、字幕、配音、合成和发布都进入同一套 preflight、execution plan、stage manifest 与 artifact 审核链路。
- 为后续真实 Playwright/浏览器录制 worker、鼠标轨迹/高亮渲染、HTML/Canvas composition graph 留出清晰接口。

## 当前边界

当前版本是编排骨架和合同验证，不自动执行真实浏览器录屏。

`browser_recording` 和 `interaction_highlight` 能力在默认工具目录中都有 degraded 本地占位工具，所以 run 可以继续进入审核与规划状态；真实录屏工具作为可选/未来高质量实现接入。

`capture`、`compose` 和 `export` 阶段在 V3.4 中默认保持 planned，不自动派发到 worker，避免调用尚未实现的录屏或演示合成 payload。

## 阶段设计

1. `intake`
   - 归一化用户输入：产品、URL、目标平台、时长、比例、演示目标。
   - 输出 `intake_brief`。

2. `demo_plan`
   - 制定演示承诺、功能路径、步骤顺序和前置条件。
   - 输出 `demo_flow_plan`。
   - 需要人工审核，防止错误功能路径进入录制。

3. `recording_plan`
   - 将演示计划转成可复现的浏览器动作、视口、隐私遮罩、测试数据和旁白提示。
   - 输出 `recording_plan`。
   - 需要人工审核，特别关注登录态、敏感信息和页面稳定性。

4. `capture`
   - 规划或执行录屏资产采集。
   - 输出 `screen_capture_manifest`。
   - 当前版本保持 planned，不自动派发。

5. `annotation`
   - 规划鼠标、点击、高亮、缩放、callout、字幕和可选旁白/音乐。
   - 输出 `interaction_annotation_plan`。

6. `edit`
   - 制定时间线、裁切/重构图、字幕、声音和合成指令。
   - 输出 `edit_plan`。

7. `compose`
   - 规划演示视频渲染。
   - 输出 `render_summary`。
   - 当前版本保持 planned，不自动派发。

8. `final_review`
   - 人工确认演示准确性、画面可读性、敏感信息、字幕和音频。
   - 输出 `review_notes`。

9. `export`
   - 规划发布包与访问链接。
   - 输出 `publish_package`。
   - 当前版本保持 planned，不自动派发。

## Artifact 合同

- `demo_flow_plan.v1`：演示目标、受众、产品入口、功能步骤、前置条件和风险。
- `recording_plan.v1`：浏览器动作、视口、遮罩、测试数据、录制设置。
- `screen_capture_manifest.v1`：录屏片段、截图、采集状态和元数据。
- `interaction_annotation_plan.v1`：高亮、缩放、callout、字幕和音频 cue。

这些 artifact 均允许 placeholder payload，用于 beta 阶段贯通状态机、审核门禁和 manifest 诊断。

## 后续升级方向

- 接入真实 Playwright 浏览器录制 worker。
- 增加网页可访问性/登录状态预检。
- 接入鼠标轨迹、点击波纹、区域高亮、局部放大等自动标注。
- 让 `compose` 使用后续 V5 composition graph 输出更稳定的演示视频。
- 支持脚本放映室将结构化脚本直接转换为 demo steps。
