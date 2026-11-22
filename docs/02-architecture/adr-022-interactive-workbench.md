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

## 修订（2026-11-17）：Claude Code 观感与模式切换对齐

依据：用户给出 Claude Code 实际截图，要求工作台观感与交互向其对齐。本修订只动呈现与入口交互，不改事件协议、配置格式、SQLite schema 与审批语义。

1. **欢迎头**：启动时对话区顶部呈现紧凑边框头块——左侧神之眼徽记（三角形 + 眼睛 + 光芒，字符绘制、对称极简，不引入图片资源），右侧并排标题行：欢迎语 + 版本（取包 `__version__`）、模型名、工作区路径与按键提示。原先单独的一行系统欢迎消息与之合并，不重复欢迎。
2. **输入区**：输入框改为带 ❯ 提示符的边框盒子；状态行从输入框上方移到其正下方，内容沿用 `ChatStatus.summary_line()` 口径（模型/模式/轮次/工具/token/上下文/耗时 + Run 状态 + 工作区 + 会话），并在尾部固定呈现当前模式与「(shift+tab 切换)」提示。
3. **Shift+Tab 模式循环**：普通 → 计划 → 自动接受 → 普通。状态转移与 `/plan`、`/auto`、`/normal` 完全共用同一套实现（`_enter_plan` / `_enter_normal` / `_auto_worker`），不另起语义。关键安全决定：切入自动接受时**仍走审批桥的显式确认**（与 `/auto` 同一确认面板），确认通过后才离开计划模式，取消确认则留在计划模式——不因快捷键而静默绕过自动接受的既有确认语义。有待决审批/确认时不切换模式，避免与审批面板争用。
4. **对话区紧凑化**：工具调用改为一行式条目 `⏺ 工具名(参数摘要)`，完成后结果以 `⎿` 缩进次行呈现（状态 + 耗时），去掉工具块的左边框面板感；模型文本保持 Markdown 流式渲染，`/tools [call_id]` 展开完整参数的能力不变。

## 修订（2026-11-21）：Klee 风格对齐（P0–P2）

依据：用户确认《Deity TUI 参考 Klee 风格改造计划》（signerlabs/Klee 的 SwiftUI 设计语言提炼），按其 §6 决策 D1–D7 实施。本修订仍只动呈现层（`interfaces/tui/` 主题、CSS 与呈现组件），不改事件协议、配置格式、SQLite schema 与审批语义；工具调用保持一行式可见（不抄 Klee 的「界面不渲染工具调用」）。

1. **P0 视觉统一**：新增 `interfaces/tui/theme.py` 注册深浅两套 deity 主题——品牌色取 Klee 深绿 `#1E4C35`（浅底原色、深底提亮变体 `#2E7D5B`），边框色降为中性灰，定义四级文字色 token（次级/三级/弱提示）与 user-bg/hover-bg/code-bg 辅助色块；默认主题按 `COLORFGBG` 粗判终端明暗、缺失时深色。欢迎头/输入行/审批面板边框改 `round`；用户消息保留 `❯` 行并加极淡 accent 底色块（D4）；系统消息落三级色，错误预留 `.msg-error` 分级（P1 落地呈现）；工具行改 Rich 分段着色（⏺ 与工具名品牌绿、⎿ 行次级色、状态词按结果着色）；Markdown 代码块定制底色与左侧 accent 线、标题层级收紧；神之眼徽记保留并随主题重新着色（D2）。
2. **P1 交互对齐**：等待指示行、错误横幅块、每轮性能指标行、输入区状态提示 + 免责行、欢迎区时间问候语、滚动节流。思考折叠卡（原计划 P1-1）经前置核查（P1-0）确认事件源缺失——`ModelEvent.kind` 无 reasoning/thinking，两个模型适配层均不解析思考内容，`ChatViewModel` 无 thinking 折叠——本轮不实施、不伪造事件；仅落地不冒充思考内容的等待指示（与 Klee 等待态药丸对应）。若未来要做真实思考呈现，须先按兼容性规则为事件协议立 ADR 并扩展适配层。
3. **P2 细节打磨**：审批按钮 hover 反馈、默认隐藏可呼出的会话侧栏（D3，宽屏才可用）、空状态文案补「下一步」、监视台 `TuiApp` 观感对齐（共用主题，仅观感，行为不变，D7）、状态栏瘦身为「模型 · 模式 · 运行态」极简三项（D5，详情回归 `/status` 与每轮指标行）。

## 2026-11-22 修订：Ctrl+C 空闲退出

用户反馈：不希望用 Ctrl+Q 退出，希望 Ctrl+C 能退出。工作台 Ctrl+C 语义由「取消当前 Run、无 Run 时不误退」改为分级处理（`action_cancel_run`）：待决确认面板（计划构建/自动接受确认）打开时先取消确认；Run 进行中取消 Run（待决审批仍先拒绝并连带取消，与此前一致）；完全空闲时退出应用，清理与 `action_quit` 在空闲态等价。Esc 保持只取消不退出（输入框内 Esc 与 Ctrl+C 不再同路：`CancelRequested` 携带按键名分流）。Ctrl+Q 退出保留不变。欢迎头、/help 与 README 的按键提示同步更新为「Ctrl+C 取消/退出」。

## 2026-11-22 修订：模型失败呈现诊断细节

用户在真机使用中撞到可诊断性缺陷：模型调用失败时界面只显示失败代码（如 `model_http`），HTTP 状态码与服务端错误信息被适配器丢弃，用户无从自查是模型名写错、端点不对还是服务不可用。本修订给既有失败类型加可选诊断字段，不改事件协议、配置格式与 SQLite schema：

1. `ModelFailure` 新增可选字段 `detail: str | None`（关键字参数，默认 None，构造兼容）；`str(exc)` 仍为 code 本身，依赖该语义的代码不变。
2. 两个模型适配器（OpenAI 兼容 / Anthropic）统一填充 detail，提取与脱敏规则收敛在 `adapters/model/_errors.py` 单点实现：状态类错误（APIStatusError）取 `HTTP <状态码>` + 服务端消息摘要——消息只取 SDK 已从响应体提取的 message 字段，空白折叠（去换行）后截断至 200 字符；连接类错误（APIConnectionError / APITimeoutError）只取异常类型名，不取异常消息（传输层消息可能含代理/URL 等本地网络细节）。
3. **sanitized 边界**：detail 绝不包含 API key、请求头、完整请求体；拿不准的字段宁可不取。服务端消息属于服务端返回文本，截断与折叠防止其撑爆界面或借换行伪造界面内容。
4. 呈现：`run_error_message` 对带 detail 的 ModelFailure 返回 `代码（detail）`，TUI 与行式 CLI 共用同一出口；无 detail 时与此前完全一致。

## 2026-11-22 修订：/help 一行一命令与 /model 可选模型列表

用户反馈：/help 输出是分号连成的整段、终端换行后难读；/model 只显示当前模型、看不到可选模型。改动：HELP_TEXT 改为由 COMMANDS 注册表逐条生成、每条命令占一行（行式与工作台共用同一文本，行式打印加 markup=False，避免用法里的 [call_id] 被 Rich 当样式吞掉）；新增 KNOWN_MODELS（qwen/anthropic 常用模型、openai_compatible 无固定列表），/model 无参数时展示当前模型 + 本提供方可选模型（当前项打 ● 标记），/model <名称> 仍只改会话内模型名、不换提供方。两处原按旧格式断言的整段输出测试随格式改写（非放宽）。
