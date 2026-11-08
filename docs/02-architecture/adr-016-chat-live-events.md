# ADR-016：CLI 聊天实时事件流（live-only outlet）

> 状态：已接受（2026-11-09，CLI 聊天重构 Phase 0）。
> 关联：`docs/03-development/cli-chat-refactor-plan.md` §4.1/§5 Phase 0；
> `docs/02-architecture/adr-014-v0.8-client-protocol.md`（v0.8 统一客户端协议）。

## 背景

CLI 聊天重构需要流式渲染：模型文本边生成边显示、工具调用穿插呈现。
动手前核实（2026-11-09，`main` @ `9b46b62`）的现状：

- v0.8 §2.1 已落地：`ClientService` 提供 `start_run / subscribe_events /
  approve / cancel / resume`，事件为 `ClientEvent`（`seq / run_id /
  schema_version = 1`），快照 + 增量订阅、`after_seq` 重连（`subscribe_events`
  先读 SQLite 已持久化事件，再轮询增量，默认 100ms）。
- usage **无缺口**：`AgentLoop` 每轮把模型返回的 usage 经
  `SQLiteSessionRepository.record_usage()` 写成 `ModelUsage` 事件
  （payload 含 `input_tokens / output_tokens`），可经 `subscribe_events` /
  `collect()` 读到，`/cost` 可以直接用它，不需要补 usage 通道。
- **缺口（真实存在）**：持久化事件只有粗粒度
  （`RunCreated / ModelTurnCompleted / ToolClaimed / ToolCompleted /
  ModelUsage / RunStatusChanged` 等），`text_delta` /
  `tool_call_arguments_delta` 从未写入 SQLite；`ModelEvent` 只在
  `AgentLoop._collect()` 内部消费；`RunService.run()` 只返回最终
  `AgentResult`，其 `event_sink: EventPublisher` 参数只在结束时发一条
  `RunStatusChanged`。因此 CLI 无法经 `ClientService` 订阅拿到实时文本流，
  流式渲染在 Phase 0 之前不可做。

## 决定

1. **复用，不另起一套**：不新增方案 §4.1 建议的 `ChatEvent` 类型。
   实时流与持久化订阅共用 v0.8 已落地的 `ClientEvent`
   （`core/contracts/client.py`），CLI 跟随 v0.8 命名（方案 §4.6）。
2. **AgentLoop 增加可选 live 出口**：`run / run_created / run_existing`
   增加可选关键字参数 `live_event_sink: Callable[[ClientEvent], None] |
   None = None`，沿 `_run_owned` → `_execute` 透传；为 `None`
   时行为与现状完全一致。sink 是同步回调，只做观测：
   抛异常时捕获丢弃，绝不影响 Run 的执行与终态。
   发出的 kind：`text_delta`、`tool_call_started`、
   `tool_call_arguments_delta`、`message_completed`、`usage`
   （由模型流翻译而来）、`tool_call_completed`（工具执行并持久化
   `record_tool` 后发出，payload 含 `call_id / name / status`）。
3. **RunService 增加 `run_stream()`**：
   `async def run_stream(task, *, model_name, session_id=None,
   execution_mode=...) -> AsyncIterator[ClientEvent]`。
   内部复用现有 `run()` 的全部装配逻辑（`_compose_dispatcher`、上下文组装、
   最近 8 条消息、审批链），只把 `AgentLoop` 的 live 事件接出来；
   结束时补一条终态 `run_status` 事件（payload 含 `status / final_text`）；
   Run 被 `cancel()` 时补终态 `run_status`（`status=cancelled`）后正常结束，
   不向消费者抛 `CancelledError`；其它异常（`BudgetExceeded` 等）在排空
   已发事件后原样向上传播，与 `run()` 的异常语义一致。
4. **live 流不持久化、不参与重放**：实时事件只走内存队列，不写 `events`
   表；回放仍只读 SQLite 已持久化事件（v0.9 回放规则不变），重放 100 条
   事件零工具调用的性质不受影响。
5. **seq 语义**：live 流的 `seq` 由 `AgentLoop` 按 Run 内计数器自 1 起
   单调分配，仅在该条 live 流内有效；终态 `run_status` 事件的 `seq` 为
   已转发事件数 + 1。live `seq` **不是** SQLite `events.sequence`，
   消费者不得把 live 流与持久化订阅按 `seq` 合并去重。

## 为什么不把 text_delta 持久化进 SQLite 再复用 subscribe_events

- 审计日志语义：`events` 表是 Run 的审计证据（状态、工具执行、usage），
  逐 token/逐分片的文本增量不是审计事实，写入会改变其含义与体积。
- 存储膨胀：一次长回答可产生数千条增量事件，SQLite 事件表与索引无谓膨胀。
- 延迟：订阅轮询默认 100ms，流式渲染需要毫秒级到达；live 内存队列无此延迟。
- 因此 live 出口与持久化订阅是两条互补通道：live 只做实时渲染，
  持久化订阅仍负责快照、重连与重放。

## 为什么不用 ClientService.start_run + subscribe 做流式

同上：`ClientService.subscribe_events` 的唯一事件源是 SQLite，
没有 live 文本可订阅。`run_stream` 与 `ClientService` 分工不同：
前者驱动一次 Run 并给出 live 流，后者管理已启动 Run 的订阅、审批与取消。
CLI 流式任务走 `run_stream`；TUI 继续走 `ClientService`，两者不冲突。

## 兼容性

- 全部新增参数可选且默认为 `None`；`ModelEvent`、`ClientEvent`、
  `ClientService`、`RunService.run()` 签名与行为不变；SQLite schema 不变；
  `schema_version` 仍为 1。
- 现有调用方（`ClientService.start_run`、`RunService.resume`、
  `run_build`）不传新参数，行为不变。
- `/cost` 的 token 数据优先用 live `usage` 事件；无 live 流时
  （如事后查看）可用 `ClientService.collect()` 读 `ModelUsage` 事件，
  两处 payload 字段一致（`input_tokens / output_tokens`）。

## 未决事项的决定（方案附录 B 第 2 项）

`AgentLoop` 的事件出口形态定为 **`event_sink` 式同步回调 +
`RunService.run_stream()` 异步迭代器**，二者组合而非二选一：
回调是 Core 侧最小的观测钩子（不改变循环、不引入异步队列），
`run_stream()` 是 Application 侧对 CLI 友好的消费形态。
不新增 `ChatEvent` 类型（附录 B 第 2 项的另一半，随 v0.8 命名解决）。
