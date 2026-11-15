# ADR-017：进程内事件总线统一 live 事件投递（V1.1-M2）

> 状态：已接受（2026-11-15，V1.1-M2）。
> 关联：`docs/05-roadmap/deity-cli-optimization-roadmap.md`（V1.1 立项依据）；
> `docs/02-architecture/adr-016-chat-live-events.md`（live 事件的产生方式不变）。

## 背景

ADR-016 之后 live 事件有两条投递路径：CLI 的 `RunService.run_stream()`
用一条临时 Queue 转发 `live_event_sink` 事件；`ClientService` 则只靠
SQLite 每 0.1 秒轮询持久化事件，拿不到 `text_delta` 等 live 事件，
TUI 与 CLI 看到的事件词汇也不同（live 的 `tool_call_completed` /
`run_status` 与持久化的 `ToolCompleted` / `RunStatusChanged` 并存）。
渲染器（StreamingRenderer）同时做事件折叠与绘制，没有独立的视图状态。

## 决定

1. 新增 `deity.application.event_bus.InProcessEventBus` 作为进程内
   live 事件的唯一通道：按 run_id 分发、每 Run 有界回放缓冲
   （晚订阅先回放 seq 大于 after_seq 的事件再跟随 live）、订阅队列
   有界、满队列以 `slow_consumer` 失败（不静默丢事件）。
2. `RunService.run_stream()` 改经总线投递（内部一张总线实例），
   对外事件流与终态语义不变。
3. `ClientService.start_run()` 把 Run 的 `live_event_sink` 接到总线，
   并在 Run 终态时补发 `run_status` 事件（seq 接续该 Run 的 live
   序号）；`subscribe_events()` 对本服务进程内启动的 Run 走总线，
   其余 Run（跨进程接入、resume 后继、历史 Run）仍走 SQLite。
4. SQLite 的定位不变：持久化、跨进程接入与事后重放；`collect()`
   始终读持久化事件。live seq 与持久化 seq 仍不合并（ADR-016）。
5. CLI 侧新增 `ChatViewModel` 折叠 live 事件成状态树，
   StreamingRenderer 只按 ViewDelta 绘制；TUI 的 RunViewModel
   补齐对 live 词汇（`tool_call_completed` / `run_status`）的折叠，
   两种词汇在视图层同义处理。

## 后果

- 进程内 live 不再轮询 SQLite，延迟与词汇统一；跨进程监视仍依赖
  持久化事件，语义与之前一致。
- 总线回放缓冲有界（默认 4096 条/Run），超长 Run 的晚订阅可能丢弃
  最早的 live 事件；需要完整历史时用 `collect()` 读持久化事件。
- 回退方案：`ClientService` 构造参数 `event_bus` 可注入，`subscribe_events`
  的总线分支只按 `_tasks` 命中触发；移除该分支即回到全 SQLite 订阅。
