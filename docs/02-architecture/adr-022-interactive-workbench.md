# ADR-022：解冻 TUI —— 全屏交互工作台成为默认 TTY 界面

状态：已采纳（交互工作台实施基线）。依据：用户 2026-11-16 明确产品诉求（运行 deity 直接进入自己的 agent，进入后是 Codex / Claude Code 式交互界面）；本 ADR 取代 V1.1-M4 的 TUI 冻结决策（见 [ADR-017](adr-017-in-process-event-bus.md) 时期路线图与 `interfaces/tui` 冻结说明）。事件通路沿用 [ADR-017](adr-017-in-process-event-bus.md) 的进程内事件总线与 [ADR-016](adr-016-chat-live-events.md) 的 live 事件词汇，不另起通路。

## 背景

- 此前入口形态：`deity run` 不带任务进入行式 REPL（M3 已有 Markdown 块渲染、工具节点折叠、状态行、斜杠命令与四选项审批卡），但不是全屏界面；`deity tui` 是 V1.1-M4 起冻结的只读监视台（四块 Static 面板、无输入框），且 `[tui] enabled` 默认 `false`，用户运行 `deity` 无法直接进入 agent 工作台。
- 地基已经就绪：进程内事件总线（ADR-017）、`RunService.run_stream()` live 事件流、`ChatViewModel` 状态树（文本/工具节点/计数/终态）、`ChatStatus` 状态聚合（含上下文占用口径）、`ApprovalController` 审批状态机（允许一次/本次会话允许/shell 命令模式允许/永久允许存规则/拒绝）与持久规则层（ADR-019 A3）。缺的只是把它们装进一个全屏界面。

## 决策

### 定位与分工

1. **全屏交互工作台**（新 `interfaces/tui/interactive.py` 的 `InteractiveApp`）是 TTY 下的默认交互界面：底部输入框、流式对话区、界面内审批、状态栏。用户运行 `deity`（无子命令）或 `deity run`（不带任务）在 TTY 下直接进入工作台；`deity tui` 进入同一界面。
2. **行式 REPL 保留**：非 TTY / 管道 / CI 场景（stdin 或 stdout 非终端）仍走既有 `run_chat_session` 行式路径，行为与测试口径不变；`deity run "任务"` 单次执行语义不变。
3. **监视台保留**：既有 `TuiApp`（`interfaces/tui/app.py`）不改行为，定位收窄为「接入已有 Run 的监视台」：`deity tui --run-id <id>` 进入它（经 ClientService 订阅，可批/拒、取消、恢复该 Run），跨进程看别人/别的终端正在跑的 Run 时使用。工作台本身只跟随自己在本界面内创建的 Run。
4. **`[tui] enabled` 默认改为 `true`**：该开关的原语义是给冻结实验加的保险，现改为用户退出阀门——显式设 `false` 时 `deity tui` 仍以退出码 2 提示未启用，无子命令/`run` 的 TTY 路由回落行式 REPL。配置格式不变（仍是 `enabled`/`max_events_buffer` 两键），仅默认值变化。
5. Textual 未安装时：显式 `deity tui` 维持退出码 2 + 安装提示；无子命令/`run` 的 TTY 路由回落行式 REPL（不因可选 extra 缺失而让主入口失败）。

### 工作台实现

6. **事件与状态**：工作台在本进程内驱动 `RunService.run_stream()`（与行式 REPL 同一通路），事件喂入既有 `ChatViewModel` 折叠；对话区按 ViewDelta 增量建块（文本块用 Textual `Markdown` 渲染、工具节点块结构化呈现并可经 `/tools` 展开参数），不新造事件语义。
7. **审批**：不复制审批语义。工作台用一个线程桥（`interfaces/tui/approval_bridge.py`）复用既有 `ApprovalController` 本体：控制器渲染到捕获用 `Console`（StringIO），其 `input_fn` 阻塞工作线程，直到界面内审批面板给出选择（允许一次/本会话允许/shell 命令模式允许/永久允许存规则/拒绝，与 CLI 卡片选项一一对应，含模式文案与规则落盘）。会话允许表、命令模式派生、规则层先行表态都由原控制器与 `RunService._authorize` 完成，工作台只负责呈现与回填选择。界面退出时桥置 abort，阻塞中的审批按拒绝收口，不留挂起线程。
8. **状态栏**：复用 `ChatStatus.summary_line()` 口径（模型、工作区、模式、轮次、工具数、当前工具、token、上下文占用、耗时）+ 会话 id 与 Run 状态；无 usage 数据时显示 `—`，不编造数字（与 CLI 同口径）。
9. **斜杠命令**：工作台实现与界面相容的子集——`/help /status /cost /tools /clear /compact /plan /build /normal /model /auto /sessions /resume /doctor /exit /quit`，语义对齐 `chat_commands`（计划模式、/build 的脏文件集中确认改走界面确认面板；/sessions 列出并可带 id 切换；/resume 带 run id 从检查点续跑并回显结果）。`/skill /mcp /fork /timeline` 在工作台内明确提示去行式 CLI 使用，不静默失效。
10. **取消与退出**：Ctrl+C 取消当前 Run（经 `RunService.cancel()`，与 REPL 同语义），无 Run 时不误退；Ctrl+Q 或 `/exit` 退出，先 abort 审批桥再取消活动 Run，不留残留进程/挂起任务。
11. **计划模式**：工作台内 `/plan` 后提交任务走 `PlanService` 创建计划 → 界面展示计划文本（复用 `plan_lines`）→ 界面确认构建 → 脏文件集中确认 → `build_approved` 执行，与行式流程同语义。

## 不做

- 不改事件协议、不改 SQLite schema、不改 `RunService`/`ClientService` 的公开语义；监视台 `TuiApp` 代码不改动（仅文档定位更新）。
- 工作台不做跨进程接入（那是监视台的活），不做多 Run 并发跟随（一个界面同一时刻只跟随自己提交的当前 Run）。
- 不引入新依赖：Textual 仍是既有 `tui` extra；mypy 对 `deity.interfaces.tui.interactive` 沿用 `tui.app` 的放宽口径。
- 真实模型（Qwen）端到端验证本期为 NOT_RUN（无可用密钥通道，密钥不得从聊天记录取回）；行为验证以 FakeModel 测试替身完成，与仓库测试规则一致。

## 后果

- `tests/integration/test_doctor_cli.py::test_tui_command_disabled_by_default` 的前提（默认关闭）失效，改为「显式配置 enabled=false 时退出码 2」的等价用例。
- README 的 TUI 章节与 `deity tui` 帮助文本同步更新（不再写「默认关闭/仅监视」）。
