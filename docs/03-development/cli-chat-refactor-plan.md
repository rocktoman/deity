# Deity CLI 聊天对话交互重构方案

> 状态：**Proposed，仅方案，未实现任何代码**。
> 基线：V0.7 提交 `e65b9fc`（实现 V0.7 只读多 Agent 委派）；本文基于该基线核实现状后编写。
> 动手时机：**等 v0.8–v1.0 全部开发完成、全量功能测试跑完之后再动手**。动手前必须先读
> `docs/03-development/v0.8-v1.0-implementation-plan.md` 并执行 `git log --oneline -5`
> 与 `git status`，确认 v0.8–v1.0 的实际落地状态（另一个协调 agent 正在同一仓库做
> v0.8–v1.0 开发，本文档编写期间它已提交 `e12f141 编写 v0.8-v1.0 实施计划并记录 CLI 交互后续项`）。
> 本方案只写文档，不改任何代码。

## 0. 方案说明与阅读约定

- 本方案的“现状”全部来自对基线代码的直接阅读，不是推测；凡写“不存在”的，都是在基线
  里 grep/阅读后确认没有的。
- 凡提到 Claude Code 的交互细节，均标注为“参考 Claude Code 公开交互惯例，需在实现时
  以实际体验为准”，本文没有查阅 Claude Code 官方源码，也没有把它的任何实现复制进来。
- 术语：CLI 指 `deity` / `deity run` 进入的交互式命令行会话（`Deity> ` 提示符）；
  TUI 指 V0.8 设计里的 Textual 全屏界面，两者是两条线，见第 2 节与第 4.6 节。

---

## 1. 现状与痛点

基线文件规模（`wc -l` 核实）：

| 文件 | 行数 | 在重构中的角色 |
|---|---|---|
| `src/deity/interfaces/cli/main.py` | 480 | 入口、Typer 装配、交互循环、审批闭包 |
| `src/deity/interfaces/cli/plan.py` | 220 | `plan`/`build`/`plans` 命令、Build 审批 |
| `src/deity/interfaces/cli/menu.py` | 84 | 手写 raw tty 选择器 `choose()` |
| `src/deity/interfaces/cli/sandbox.py` | 166 | 本次重构不动 |
| `src/deity/interfaces/cli/verify.py` | 86 | 本次重构不动 |
| `src/deity/interfaces/cli/code.py` | 87 | 本次重构不动，但 `@symbol` 可复用它的 `LexicalCodeSearch` |

### P1. 没有流式输出：整轮跑完才一次性打印结果

- `main.py::_interactive()` 的任务分支是 `result = await service.run(...)`，然后
  `console.print(result.final_text or "无最终回答")`。用户在模型思考、调工具的
  几十秒甚至几分钟里看不到任何进展。
- 根本原因不在 CLI，而在服务层没有事件订阅口：
  `RunService.run()`（`src/deity/application/run_service.py:793`）和
  `AgentLoop.run()`（`src/deity/core/runtime/__init__.py:138`）都只返回
  `AgentResult`（`core/contracts/__init__.py:151`：`run_id/status/final_text/error_code/evidence_ids`，
  无 token/usage 字段）。
- 模型事件其实存在，但只在内部消费：`ModelEvent`（`core/contracts/__init__.py:219`，
  kind 为 `text_delta / tool_call_started / tool_call_arguments_delta / message_completed /
  usage / error`）由 `AgentLoop._tracked_stream()` / `_collect()` 在循环内部消化，
  没有任何 `on_event / progress / callback / subscribe / publish` 的对外接口
  （对 `run_service.py` 与 `core/runtime/__init__.py` grep 确认无此类符号）。
- 结论：流式渲染不是“CLI 改个打印”就能做的事，必须先有第 4.1 节的事件订阅前置工作，
  而这正是 V0.8 设计 §2.1（统一客户端协议 `start_run/subscribe_events/approve/cancel/resume`）
  要求的东西。见第 5 节 Phase 0。

### P2. 输入体验停留在 `input()`：无历史、无多行、无补全

- `_interactive()` 用内置 `input("Deity> ")` / `input("Deity[plan]> ")` 逐行读入，
  `.strip()` 后处理。上下键不能翻历史；粘贴多行文本会在第一个换行处直接提交；
  没有 `@文件` 引用、没有 `/` 命令补全、没有参数提示。
- `menu.py::choose()` 是手写 raw tty 选择器：Unix 下 `termios` + `tty.setraw()`、
  手动解析 `\x1b[A/B`、Windows 下 `msvcrt.getwch()`；只支持上下键 + Enter + Esc，
  无模糊搜索、无翻页、无鼠标、无多选。调用点只有三处：`_interactive()` 里的
  `/skill`、`/mcp`、无 plan_id 的 `/build`。
- 非 TTY 时 `choose()` 有降级分支（打印编号列表 + `input("选择编号…")`），这是现有
  行为，重构必须保留（见第 4.5 节、第 6 节）。

### P3. 审批体验只有“打印参数 + 是/否”，且三处重复

- 三个内容相同的 `approve(name, args)` 闭包分别写在 `main.py::run()`、
  `main.py::resume_run()`、`plan.py::_execute_build()` 里：把 `args` 去掉 `content`
  后 `console.print(f"工具 {name} 请求执行：{displayed}")`，再
  `typer.confirm("允许本次调用？", default=False)`。问题：
  - 看不到要改的文件 diff、要跑的命令全文、风险等级；
  - 只有“本次允许 / 拒绝”两种选择，没有“本次会话都允许”；
  - `RunService._authorize()`（`run_service.py:185`）先按 `PermissionConfig`
    （`shell/workspace_write: ask/auto/deny`）决策，`ask` 时才调 `approve` 回调——
    这个安全链本身是对的，重构只能换展示层，不能绕过它。
- `plan.py::confirm_build()` 与 `authorize_dirty_targets()` 同样是逐项
  `typer.confirm`（计划批准、每个 dirty 文件逐个问），与工具审批的交互风格不统一。

### P4. 运行过程不可见：没有工具调用树、没有状态

- 运行中不知道现在是第几轮、调了哪个工具、工具成功还是失败、花了多久、用了多少 token。
  `AgentResult` 没有 usage 字段；`usage` 只在 `ModelEvent` 里出现过一次，且未向上传递。
- `README.md` CLI 一节写明“交互模式在一次启动内复用会话，并将最近四轮问答作为后续
  模型上下文”——代码里是 `RunService.run()` 取 `repo.messages(session)` 的最后 8 条
  user/assistant 消息（即 4 轮），会话内没有任何地方向用户展示这个上下文状态。

### P5. 中断语义不完整：Esc 无效，Ctrl-C 直接退出整个 CLI

- 运行中按 Esc 没有任何效果；`RunService.cancel(run_id)`（`run_service.py:177`）
  已经存在（会级联取消子 Run），但 CLI 里没有任何调用点——grep 确认 CLI 从未调用它。
- `_interactive()` 里的 `await service.run(...)` 只捕获
  `ModelFailure/ProtocolError/BudgetExceeded/ValueError/OSError`，不处理
  `KeyboardInterrupt`；`run()` 外层捕获 `KeyboardInterrupt` 后打印“运行已取消”并以
  退出码 130 结束整个进程。也就是说：运行中 Ctrl-C = 退出 Deity，而不是“中断本轮，
  回到提示符”。

### P6. 会话管理只有“进”没有“管”

- 启动时 `active_session = session_id or str(uuid4())`，一次启动一个会话，无法在会话内
  切换；`deity sessions`（`main.py::sessions()`）只能列出会话 id 与工作区，
  不能在交互模式里用；`deity resume <run-id>` 是独立子命令，交互模式里没有 `/resume`。
- 没有 `/compact`（`RunConfig.compaction_enabled` 配置项存在，但交互模式里无触发入口）、
  没有 `/clear`、`/cost`、`/model`、`/status`。
- `/help` 只打印一行长字符串，没有按命令分组、没有参数说明。

### P7. 斜杠命令是硬编码 if/elif 链，且选择结果只拼成自然语言提示

- `_interactive()` 里 `/plan /normal /build /skill /mcp /help /exit /quit` 是一串
  `if prompt == ... / prompt.startswith(...)`，新增命令要改同一函数；未知命令只回
  “未知命令。输入 /help 查看可用命令。”，没有“你是不是想输入 …”的纠错。
- `/skill` 与 `/mcp` 选中的结果只存在 `pending_skill: str | None` /
  `pending_mcp_tool: str | None` 两个变量里，下一条任务拼成
  `"请先加载并使用 {name} Skill。"` / `"如适用，请使用 {tool} 工具。"` 再加
  `"任务：{prompt}"` 发给模型——是自然语言提示，不是类型化指令；任务失败后
  `pending_*` 被清空（`test_interactive_cli.py::test_slash_selection_is_consumed_after_failed_run`
  已覆盖此行为），重构时要保留“选择只对下一条任务生效”这一语义。

### P8. 没有 `@文件` 引用、没有 `!` shell 模式

- 想让模型看某个文件，只能在自然语言里写路径，模型自己再去读；没有 `@src/…` 这种
  先由 CLI 解析、校验工作区边界、再把内容或引用交给模型的显式机制。
- 想快速跑一条 shell 命令验证想法，必须发一轮模型任务走工具调用，没有 `!pytest …`
  这种直通本地 shell 的快捷方式（直通也必须走 `PermissionConfig.shell` 的 ask/auto/deny，
  不能绕过，见第 2 节）。

### P9. `main.py` 480 行职责混杂，`_interactive()` 约 250 行

- 同一个文件里有：Typer app 装配（`app/runs_app/mcp_app/skills_app/plans_app/code_app/sandbox_app`）、
  `skills list/inspect`、`mcp list/test`、`sessions`、`runs inspect/tree`、`resume`、
  `run()` 单次任务入口、`_interactive()` 交互循环。交互循环内部又混着
  计划模式状态机、Skill/MCP 选择、Build 流程、错误处理。加一个命令或改一次渲染都要
  动这个函数，回归面大。
- 现有交互测试基线（必须全部保留并通过）：
  `tests/integration/test_interactive_cli.py`（219 行，9 个测试：同会话历史复用、
  仓库 AGENTS.md 不替换当前任务、无任务进入提示符、`deity` 裸命令进入交互、
  未配置时 plan 命令禁用、中文无引号任务、/skill 只影响下一条任务、/mcp 只做发现
  不启动其他 server、失败后选择被消费）与
  `tests/integration/test_interactive_extensions.py`（35 行，方向键回绕、
  MCP 发现不启动其他 server）。

---

## 2. 目标与非目标

### 2.1 目标

1. 交互可视：模型文本流式显示；工具调用以折叠树呈现（调用、参数摘要、结果状态、
   耗时）；文件写入/修改渲染 unified diff；底部状态栏显示模型、轮次、token、耗时、预算。
2. 输入顺手：历史回溯、多行输入、`@文件` 引用、`/命令` 补全与参数提示、模糊选择器
   替代手写 `menu.py`，同时保留非 TTY/管道下的现有降级行为。
3. 审批可信：内联审批卡片（工具名 + diff/命令全文 + 风险提示），选项为
   允许一次 / 本次会话允许 / 拒绝；审批安全链不变。
4. 会话可管：`/resume`、`/sessions`、`/compact`、`/clear`、`/cost`、`/model`、
   `/status`；模式切换（普通 / 计划 / 自动接受）显式、可回退。
5. 结构可维护：`main.py` 拆分后 `_interactive()` 不再是 250 行大函数；每个新模块
   都有确定性测试；单次任务模式（`deity run <任务>`、管道输入）行为不变。

### 2.2 非目标（明确不做）

- **不改 Core / Application 协议**：不改 `AgentLoop` 状态机、不改工具调度与权限
  决策、不改 SQLite schema（除非 Phase 0 的事件订阅确实需要新表/字段，那要单独写 ADR，
  见第 4.1 节）。
- **不改审批安全链**：`schema validation → policy → workspace boundary → approval →
  execution → audit` 不变；`PermissionConfig` 的 ask/auto/deny 语义不变；
  `!` shell 直通同样走 `shell` 权限决策，不能成为绕过审批的后门。
- **不引入 LangChain / LangGraph / Deep Agents Runtime**，不引入 FastAPI，
  不把本地工作区约束描述为沙箱。
- **不把 TUI 并进 CLI**：Textual 只留在 V0.8 的 TUI 线；基础 CLI 的新依赖只允许
  `prompt_toolkit` 一个可选 extra（见第 4.5 节、第 7 节）。
- **不做自动模型切换、不做 RAG、不做多 Agent 的 CLI 编排界面**（V0.7 的
  `runs tree` 保持原样，只补一个更易读的渲染，不改语义）。
- **不改单次任务与脚本化使用**：`deity run <任务>`、stdin 管道、退出码
  （成功 0、运行失败 1、配置错误 2、取消 130）保持不变。

---

## 3. 参考 Claude Code 的交互清单

> 统一说明：以下每条都是“参考 Claude Code 公开交互惯例，需在实现时以实际体验为准”，
> 不是对 Claude Code 源码的复述；落地时以 Deity 现有 `RunService` / `PlanService` 接口
> 能支撑的范围为准，撑不住的先记为缺口（见第 4.1 节），不要为抄交互而改 Core 边界。

| # | 借鉴点 | 在 Deity 的落地方式 |
|---|---|---|
| C1 | 流式输出：模型边生成边显示，工具调用穿插在文本流中 | Phase 0 先接通事件流；渲染器按 `text_delta` 逐段追加文本，`tool_call_started` 时插入一个折叠节点，工具返回后在同一节点上补结果与耗时。非 TTY 时关闭流式渲染，改回整段打印（与现状一致）。 |
| C2 | 工具调用树：读/写/搜索/shell 各自成行，可展开看参数与结果 | 渲染器按 `call_id` 归组：`read_file → src/…:行`、`write/patch → diff 摘要`、`shell → 命令 + exit_code`、`search → 命中数`。工具名与参数摘要来自 `ToolCall`（`core/contracts/__init__.py:202`），结果状态来自 `ToolResult.status`（`ok/error/denied/timeout/cancelled`）。 |
| C3 | diff 渲染：改动以统一 diff 展示，新增行绿、删除行红 | 写/补丁类工具在审批卡片与执行后都渲染 diff。diff 来源优先用 `GitWorkspaceAdapter.diff()`（`RunService.run()` 已在成功后 `record_evidence(run_id, "git_diff", diff)`，见 `run_service.py:793` 附近）；无 Git 时回退到“旧内容 vs 新内容”的内存 diff（`difflib.unified_diff`，只用于展示，不写盘）。 |
| C4 | 内联审批卡片：工具、参数、diff、风险在一张卡片里，选一次/会话允许/拒绝 | 新模块 `chat_approval.py`：`ApprovalCard`（Rich Panel）+ `ApprovalDecision`（`allow_once / allow_session / deny`）。`allow_session` 只在内存里记“本会话、本工具名、本工作区”的允许表，会话结束即失效；`deny` 仍走现有 `ToolResult.status="denied"`。审批回调的 bool 签名在过渡期保留（见第 4.4 节），新决策先在 CLI 层折叠成 bool 再传给 `RunService`，不改 `approve` 的类型。 |
| C5 | `@文件` 引用：`@路径` 把文件内容或引用带进任务 | 新模块 `chat_mentions.py`：输入层解析 `@` 后的路径（Tab 补全用工作区内相对路径，ripgrep 索引不在本计划内）；解析时做工作区边界校验（`Path.resolve().is_relative_to(workspace)`，越界直接报错不发送）与大小上限（复用 `code_intelligence.max_file_bytes` 的思路，默认单文件 2_000_000 字节，上限写入 `ChatConfig`，见第 4.5 节）；把引用以“文件路径 + 内容摘要/全文”的结构化形式拼进任务，而不是只拼一句话。`@symbol名` 复用 `code.py::_search()` 的 `LexicalCodeSearch.find_symbol()`，不另写索引。 |
| C6 | `/命令` 补全与帮助：输入 `/` 即列出命令与参数说明 | 新模块 `chat_commands.py`：命令注册表（见第 4.3 节），`prompt_toolkit` 的 `Completer` 在输入 `/` 时列出命令与一行说明，输入 `/build ` 时补全当前会话可 Build 的 plan_id（来源 `PlanService.buildable(session_id)`）。 |
| C7 | 历史与多行输入：上下键翻历史，粘贴多行不误提交 | `prompt_toolkit` 的 `PromptSession(history=FileHistory(...))` + 多行模式（默认 Enter 提交、`Alt+Enter` 换行；粘贴含换行时按括号粘贴处理，不立即提交）。历史文件放 `~/.local/share/deity/chat_history`（与现有 db 目录 `~/.local/share/deity/deity.db` 同级），只存用户输入的原始文本，不存模型输出与密钥。 |
| C8 | 中断语义：Esc 取消当前输入，Ctrl-C 中断本轮并回到提示符 | 输入态：Esc 清空当前输入行；运行态：第一次 Ctrl-C 调用 `RunService.cancel(active_run_id)`（该方法已存在，见 P5），等待循环以 `cancelled` 终态收尾并回到提示符；运行态第二次 Ctrl-C 才退出 CLI（退出码 130 保持不变）。需要 `AgentLoop.cancel()` 的语义支撑：它已向模型流与子进程传播取消（AGENTS.md §4），CLI 只需调它，不要自己杀进程。 |
| C9 | 会话命令：`/resume`、`/sessions`、`/compact`、`/clear`、`/cost`、`/model`、`/status` | 见第 4.3 节命令表。`/cost` 的 token/耗时数据来自 `ModelEvent.kind == "usage"`（`input_tokens/output_tokens` 字段已存在于 `ModelEvent`，但目前未向上传递，见 P4）与事件流里的 `message_completed`；若 Phase 0 未接通 usage 事件，`/cost` 第一版只显示“本会话轮次/工具调用数/耗时”，并明确标注 token 未接通，不得编造数字。`/compact` 调用 `RunService` 已有的压缩路径（`RunConfig.compaction_enabled` 的实际执行入口以实现时核实为准；若不存在则本命令只做“清空内存上下文显示、保留 SQLite 历史”的降级实现，并在文档里写明）。 |
| C10 | 模式切换：Shift+Tab 在普通/计划/自动接受间循环 | Deity 已有 `/plan`（持续计划模式）与 `/normal`（普通模式）。本方案不引入新的按键绑定冲突：保留 `/plan` 与 `/normal` 命令，新增 `/auto`（自动接受模式：仅当 `permission` 全为 `auto` 或用户在本次会话显式确认后才进入，退出即恢复 ask；进入时必须打印一行明确提示，不得静默放宽权限）与 `Shift+Tab` 循环切换（prompt_toolkit key binding，仅在 prompt_toolkit 可用时生效；回落模式下只用命令切换）。模式只影响 CLI 的默认审批行为，不改 `PermissionConfig` 本身。 |
| C11 | `!` shell 模式：`!命令` 直通本地 shell，不经过模型 | 新命令 `!` 前缀：输入以 `!` 开头时不发给模型，直接在本机工作区执行（`subprocess`，复用现有 shell 工具的 timeout 与输出上限思路，具体数值写入 `ChatConfig`，默认值 timeout 30 秒、输出上限 16_000 字符，与 `VerificationConfig.timeout_seconds/max_output_chars` 对齐）；执行前仍走 `RunService._authorize("shell", ...)` 的决策逻辑（CLI 层通过 `RunService` 的公开方法触发，不得自造审批）；输出以代码块形式显示，不写入会话历史（即不作为模型上下文，只作为用户可见的终端输出）。 |
| C12 | 状态栏：模型、上下文占用、成本、耗时常驻可见 | 底部状态栏（`prompt_toolkit` 的 `bottom_toolbar`；回落模式下改为每轮结束打印一行摘要）。内容：模型名（`settings.model.name`）、当前模式（普通/计划/自动接受）、本会话轮次、上下文消息数（`repo.messages()` 的计数口径：最近 8 条）、token（有 usage 事件时才显示，无则显示 `—`）。 |

---

## 4. 架构与模块拆分

### 4.1 前置缺口：事件订阅（Phase 0，最大前置依赖）

现状缺口（已核实）：

- `AgentLoop.run()` 只返回 `AgentResult`；`ModelEvent` 只在循环内部消费；
  `RunService` 没有 `subscribe/start_run` 方法；`AgentResult` 没有 usage 字段；
  CLI 从未调用 `RunService.cancel()`。
- V0.8 设计 §2.1 要求 Application 层公开
  `start_run / subscribe_events / approve / cancel / resume`，事件带
  `seq / run_id / schema_version`，支持“快照 + 增量”重放，CLI 与 TUI 共用服务层。

本方案的决策（实现时按此顺序判断）：

1. **优先复用**：动手前先读 `docs/03-development/v0.8-v1.0-implementation-plan.md`
   并 grep `subscribe_events / start_run` 是否已落地。若已落地，CLI 重构直接复用，
   不另起一套。
2. **若未落地**：在本计划的 Phase 0 里实现最小版事件订阅，但必须写 ADR
   （`docs/02-architecture/adr-0XX-chat-event-subscription.md`，编号按实现时
   已有 ADR 的最大值顺延），内容包括：为什么 CLI 需要它、为什么选“快照 + 增量”
   而不是“只推 live 事件”、事件 schema 版本号（从 1 开始）、旧事件只读兼容策略。
3. 最小版接口（建议，具体签名以 ADR 为准）：
   - `core/contracts/__init__.py` 新增冻结 dataclass `ChatEvent`：
     `kind: Literal["text_delta","tool_call_started","tool_call_arguments_delta",
     "tool_call_completed","usage","approval_requested","run_status","error"]`，
     `run_id: str`、`seq: int`、`schema_version: int = 1`，
     以及按 kind 填充的 `text / call_id / name / arguments_summary / status /
     input_tokens / output_tokens / error_code` 等可选字段；`ModelEvent` 保持不变，
     `ChatEvent` 由 Application 层从 `ModelEvent` + `ToolResult` + `RunStatus` 翻译而来，
     Core 不直接依赖 CLI。
   - `RunService` 新增 `async def run_stream(task, *, model_name, session_id=None,
     execution_mode=...) -> AsyncIterator[ChatEvent]`：内部复用现有 `run()` 的
     装配逻辑（`_compose_dispatcher`、上下文组装、最近 8 条消息），只把
     `AgentLoop` 的事件流接出来；`AgentLoop` 侧新增可选参数
     `event_sink: Callable[[ChatEvent], None] | None = None`（或等价的
     `run_stream()` 变体，二选一，以 ADR 为准），默认 `None` 时行为与现状完全一致。
   - 订阅语义：先从 `SQLiteSessionRepository.events(run_id)` 读已持久化的
     事件做快照（该表已有 `events(run_id, sequence)` 唯一约束与索引，见
     `adapters/storage/__init__.py:56`），再接 live 增量；重放时绝不重新执行
     副作用工具（与 V0.8 §2.4、V0.9 回放规则一致）。
   - `AgentResult` 不加字段；usage 与耗时由事件流汇总后放在 `ChatEvent(kind="usage")`
     与 `run_status` 里，渲染层据此画 `/cost` 与状态栏。

### 4.2 模块拆分：`main.py` 480 行拆成什么

原则：先拆分再加功能（Phase 1 只拆分，零行为变化）；CLI 只做渲染与输入，
不直接调模型、不直接改文件；所有模型/工具调用仍走 `RunService` / `PlanService`。

| 新文件（`src/deity/interfaces/cli/` 下） | 职责 | 依赖 |
|---|---|---|
| `chat/__init__.py`（或扁平文件，见下） | 导出交互会话入口 | — |
| `chat_session.py` | `ChatSession` 状态机：`session_id`、`plan_mode`、`approval_mode`（`ask/auto/session` 三态，见 C10）、`pending_skill`、`pending_mcp_tool`、历史文件路径；`_interactive()` 的循环本体搬到这里，函数名改为 `run_chat_session(service, model_name, session_id)` | `RunService`、`PlanService` |
| `chat_input.py` | 输入层：`PromptSession` 封装（prompt_toolkit 可用时）与 `PlainInput` 回落（见第 4.5 节）；历史、多行、补全器、按键绑定（Esc/Ctrl-C）都在这里，不进 `chat_session.py` | `prompt_toolkit`（可选）、`chat_mentions.py`、`chat_commands.py` 的补全器 |
| `chat_render.py` | 渲染层：`StreamingRenderer`（消费 `AsyncIterator[ChatEvent]`，Rich `Live` + Panel）；工具调用树节点、diff 渲染（`difflib.unified_diff` + `rich.syntax.Syntax`）、状态栏摘要；所有 Rich 输出集中在这里，`chat_session.py` 不直接 `console.print` 业务内容 | `ChatEvent`、`rich` |
| `chat_approval.py` | `ApprovalCard`（Rich Panel：工具名、参数摘要、diff/命令全文、风险提示）与 `ApprovalDecision`（`allow_once / allow_session / deny`）；`allow_session` 的内存允许表（key 为 `(tool_name, workspace)`，会话结束清空）；把决策折叠成现有 `approve(name, args) -> bool` 的适配器 | `RunService._authorize` 的决策结果（只读，不复制其逻辑） |
| `chat_commands.py` | 斜杠命令注册表：`Command(name, usage, description, handler)`；`dispatch(prompt, ctx) -> bool`（返回 True 表示已处理，不发给模型）；`/help` 按分组打印；未知命令给“你是不是想输入 …”建议（编辑距离 ≤2 的命令名） | `PlanService`、`RunService`、`chat_session.py` 的 `ChatContext` |
| `chat_mentions.py` | `@路径` 与 `@symbol` 解析：工作区边界校验、大小上限、相对路径补全；返回结构化的 `Mention` 列表（`kind/path/content_excerpt`），由 `chat_session.py` 拼进任务 | `LexicalCodeSearch`（复用 `code.py::_search()` 的构造方式）、工作区 `Path` |
| `chat_status.py` | 状态栏与 `/cost`、`/status` 的数据汇总：轮次、工具调用数、耗时、token（有 usage 事件时）、上下文消息数；只读 `SQLiteSessionRepository.messages/events` 做计数，不改写 | `ChatEvent` 聚合结果 |
| `chat_config.py` | `ChatConfig` dataclass（见第 4.5 节）与 `load_chat_config()`；`[chat]` 配置段的解析入口 | `application/config.py::load_config` |

拆分方式二选一（实现时定，不要两种混用）：

- **方案 A（推荐）：新建 `chat/` 子包**，上面 8 个文件放子包内，`main.py` 只留
  Typer 装配与单次任务入口；`menu.py` 的 `choose()` 在 Phase 2 被
  `prompt_toolkit` 的选择器替代后删除，删除前确认三个调用点已全部迁移。
- **方案 B（保守）：扁平文件**，即 `chat_session.py` 等直接放在 `interfaces/cli/`
  下。优点是 import 路径变化小，缺点是该目录文件数从 8 个涨到 16 个。
  无论选哪种，`main.py` 拆分后的目标是不超过约 200 行，且不再包含
  `_interactive()` 的循环体。

保留不动的文件：`code.py`、`sandbox.py`、`verify.py`、`plan.py` 的
`plan_command / build_command / plans list/show`（只把 `plan.py` 里的
`confirm_build / authorize_dirty_targets / build_approved` 的交互调用点
改成走 `chat_approval.py`，函数签名不变）。

### 4.3 斜杠命令表（重构后）

`chat_commands.py` 注册表覆盖的命令。`◐` 表示现状已有、`●` 表示新增、
`△` 表示现状有但行为要改。

| 命令 | 变化 | 行为（精确） |
|---|---|---|
| `/help` | △ | 分组打印：会话 / 计划 / 工具 / 模式四组，每组命令 + 一行说明；不再是一行长字符串 |
| `/exit`、`/quit` | ◐ | 退出交互，行为不变 |
| `/plan` | ◐ | 进入持续计划模式；前置条件不变（`plan_build_enabled` 与 `git_guard_enabled` 同时开启，否则提示并拒绝） |
| `/normal` | ◐ | 返回普通模式 |
| `/build [计划ID] [--allow-dirty 路径]` | △ | 无 plan_id 时用新选择器列出 `PlanService.buildable(session_id)`；有 plan_id 时走现有 `get_buildable` 校验；dirty 文件逐个确认改为一张卡片集中确认（仍逐路径授权，语义不变） |
| `/skill [名称]` | △ | 无参数时用新选择器（模糊搜索）替代 `menu.choose()`；选中后仍只影响下一条任务（`pending_skill` 语义不变，见 P7） |
| `/mcp` | △ | 同上；只做发现，不调用业务工具；选中的工具仍只影响下一条任务 |
| `/resume [run-id]` | ● | 交互内恢复：无参数时列出本会话最近的中断 run（`RunService.inspect()` 的 `decision == "resume"` 者优先）；有参数时调 `RunService.resume(run_id)`；恢复失败时打印原因并回到提示符 |
| `/sessions` | ● | 列出 `SQLiteSessionRepository.sessions()` 的会话（id + 工作区 + 最近消息时间，时间字段以实现时核实为准）；输入编号切换会话，切换后 `plan_mode` 与 `pending_*` 清空 |
| `/compact` | ● | 触发上下文压缩（复用 `RunConfig.compaction_enabled` 的执行路径；若实现时核实无独立触发入口，则降级为“清空本轮内存上下文显示、SQLite 历史保留”，并在 `/help` 与文档里写明降级行为） |
| `/clear` | ● | 清屏并清空当前会话的终端显示历史，不删除 SQLite 里的消息 |
| `/cost` | ● | 显示本会话轮次、工具调用数、耗时；有 usage 事件时加 token 与估算成本（成本单价以配置为准，无配置时只显示 token 数，不编造成本金额） |
| `/model` | ● | 显示当前模型名与 provider；`/model <名称>` 切换本会话模型（只改内存里的 `model_name`，不写配置文件；切换后打印确认） |
| `/status` | ● | 显示模型、模式、会话 id、工作区、轮次、上下文消息数（最近 8 条口径）、token（有则显示） |
| `/auto` | ● | 进入自动接受模式（见 C10：需显式确认，退出恢复 ask）；`/normal` 或再次 `/auto` 退出 |
| `/doctor` | ● | 复用 V0.8 §2.3 的 `deity doctor` 逻辑（若 v0.8 已实现则直接调它；若未实现则本命令只做最小检查：配置可解析、模型已配置、git/rg/sqlite 可用，并标注为最小版） |

`!` 前缀不属于斜杠命令：见 C11，单独在 `chat_input.py` 里识别（`prompt.startswith("!")`），
不进命令注册表。

### 4.4 与 `RunService` / `PlanService` 的接口契约

重构前后 CLI 只能调以下公开接口（其余一律不碰）：

| 服务 | 方法 | 重构中的用法 |
|---|---|---|
| `RunService` | `from_config(settings, db_path, workspace, approve)` | 入口装配，不变 |
| `RunService` | `run(task, model_name, session_id, execution_mode)` | 单次任务与 Phase 1 的交互循环仍用它；Phase 2 起改走 `run_stream()`（见第 4.1 节） |
| `RunService` | `run_stream(...) -> AsyncIterator[ChatEvent]` | Phase 0 新增，Phase 2 渲染器消费它 |
| `RunService` | `run_build(build_run_id, plan_id)` | `/build` 仍用它（经 `plan.py::build_approved`） |
| `RunService` | `resume(run_id)` | `/resume` 与 `deity resume` 共用 |
| `RunService` | `inspect(run_id)` | `/resume` 的候选筛选与 `runs inspect` 共用 |
| `RunService` | `cancel(run_id)` | 运行态 Ctrl-C 调用它（P5 的修复点） |
| `RunService` | `mcp_list()` / `mcp_test(server)` | `/mcp` 发现用，不变 |
| `RunService` | `skills_list()` / `skills_errors()` / `skills_inspect(name)` | `/skill` 用，不变 |
| `PlanService` | `create(task, model_name, session_id)` | 计划模式用，不变 |
| `PlanService` | `buildable(session_id)` / `get_buildable(plan_id, session_id)` | `/build` 选择用，不变 |
| `PlanService` | `list(session_id)` / `get(plan_id)` | `/sessions` 关联的计划查询用（只读） |
| `SQLiteSessionRepository` | `sessions()` / `messages(session_id)` / `events(run_id)` | `/sessions`、`/status`、事件快照用，只读 |

审批回调的过渡方案：`RunService.__init__` 的
`approve: Callable[[str, dict[str, Any]], bool] | None` 签名在 Phase 1–2 保持不变；
`chat_approval.py` 提供 `make_approve(card_renderer, session_allowlist)` 工厂，
返回符合该签名的闭包，内部把 `ApprovalDecision` 折叠成 bool。
若后续要把 `allow_session` 下沉到 Application 层，必须另写 ADR，不得在 CLI
重构里顺手改 `RunService._authorize()`。

### 4.5 配置与依赖：`prompt_toolkit` 可选 extra 与回落策略

新增配置段（`application/config.py` 增加 `ChatConfig`，`load_config` 的顶层
key 白名单增加 `"chat"`；不写配置时全用默认值）：

```toml
[chat]
enabled = true            # false 时整个交互重构关闭，恢复旧 input() 循环
history_file = ""         # 为空时用 ~/.local/share/deity/chat_history
max_history = 500         # 内存历史条数上限
multiline = true          # true 时 Alt+Enter 换行、Enter 提交
status_line = true        # 底部状态栏开关
streaming = true          # 流式渲染开关；false 时恢复整段打印
approval_default = "ask"  # ask | auto | deny 的 CLI 侧默认值，不得放宽 PermissionConfig
mention_max_bytes = 2000000
shell_timeout_seconds = 30
shell_max_output_chars = 16000
```

依赖（`pyproject.toml`）：

```toml
[project.optional-dependencies]
chat = ["prompt_toolkit>=3.0.5,<4"]
```

- `prompt_toolkit` 只在 `chat_input.py` 与选择器里 import，且必须用
  `try: import prompt_toolkit / except ImportError:` 包裹；未安装时
  `chat_input.py` 返回 `PlainInput`（即现有 `input()` 行为 + 保留
  `menu.py::choose()` 的编号降级，直到 Phase 2 完成选择器迁移）。
- 版本与许可证：实现时锁定一个已发布的 3.x 小版本并记录在
  `docs/03-development/cli-chat-refactor-plan.md` 的实施记录附录里（不要写在本方案正文里，
  因为写方案时还没选）；许可证为 BSD-3-Clause（实现时以 `pip show prompt_toolkit`
  的实际输出为准，若不一致以实际为准并更新文档）。
- **非 TTY / 管道 / CI 环境**：`sys.stdin.isatty() and sys.stdout.isatty()` 为 False 时，
  整个 prompt_toolkit 路径禁用：输入走 `input()`、选择走编号、输出走整段打印、
  不写历史文件；`deity run <任务>` 单次模式完全不受影响。
- **终端兼容**：`TERM=dumb`、无颜色终端、窄终端（< 80 列）时，Rich 渲染降级为
  纯文本（`Console(force_terminal=False, width=...)` 的既有行为），diff 只显示
  前 200 行并提示“已截断”。

### 4.6 与 V0.8 TUI 的分工

| 维度 | CLI（本方案） | TUI（V0.8 §2.2） |
|---|---|---|
| 渲染技术 | Rich + prompt_toolkit（可选） | Textual（可选 extra） |
| 事件来源 | 同一个 `ChatEvent` / `subscribe_events`（第 4.1 节） | 同一个 |
| 审批界面 | 内联卡片（终端内嵌） | `ApprovalDialog` 弹窗 |
| 状态 | 视图状态独立于 Run 状态（与 V0.8 §2.2 的要求一致） | 同左 |
| 依赖 | 基础 CLI 不依赖 Textual；未装 Textual 时 CLI 正常（V0.8 验收项之一） | 未装 Textual 时 TUI 入口明确报错并提示安装命令，CLI 不受影响 |
| 并行开发 | 本方案动手时 V0.8 已完成测试；若 `chat_events.py` 与 TUI 的 `RunViewModel` 对同一事件字段命名有分歧，以 V0.8 已落地的命名为准，CLI 跟随，不另起命名 | — |

---

## 5. 分阶段实施

> 顺序铁律：Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4。
> 每阶段结束都必须跑第 6 节对应的测试并通过才能进下一阶段；
> 任一阶段发现需要改 Core/Application 协议的，先停下来写 ADR，不要边写边改。

### Phase 0 — 事件订阅前置（若 V0.8 §2.1 已落地则跳过本阶段，只做复用确认）

| 项目 | 内容 |
|---|---|
| 目标 | CLI 能拿到 `ChatEvent` 流；`/cost` 能拿到 usage；运行态 Ctrl-C 能调 `cancel()` |
| 文件 | `src/deity/core/contracts/__init__.py`（新增 `ChatEvent`）；`src/deity/core/runtime/__init__.py`（`AgentLoop` 增加 `event_sink` 或 `run_stream`，二选一，ADR 定）；`src/deity/application/run_service.py`（新增 `run_stream()`）；`docs/02-architecture/adr-0XX-chat-event-subscription.md`（新 ADR）；`tests/unit/test_chat_events.py`（新） |
| 测试 | `ModelEvent` 六种 kind 到 `ChatEvent` 的翻译单测；快照+增量重放不执行副作用的单测；`cancel()` 后循环以 `cancelled` 终态结束的单测 |
| 验收 | `run_stream()` 对 FakeModel 输出与 `run()` 的最终 `AgentResult` 一致（同一 fixture、同一 `final_text`、同一 `run_id` 语义）；重放 100 条事件不产生任何工具调用；无 `prompt_toolkit` 时本阶段零影响 |

### Phase 1 — 拆分与等价重构（只搬家，不加功能）

| 项目 | 内容 |
|---|---|
| 目标 | `main.py` 从 480 行降到约 200 行以内；`_interactive()` 循环体搬进 `chat_session.py`；行为与基线逐行一致 |
| 文件 | 新建 `chat_session.py`、`chat_commands.py`（先只注册现状 7 个命令：`/help /plan /normal /build /skill /mcp /exit`，`dispatch` 逻辑从 `_interactive()` 的 if/elif 链原样搬运）；新建 `chat_config.py`（`ChatConfig`，默认值全部等于现状行为）；`main.py` 只留 Typer 装配与 `run()` 单次入口；`plan.py` 不动 |
| 测试 | 现有 9 + 2 个交互测试全部通过（`test_interactive_cli.py`、`test_interactive_extensions.py`）；新增 `tests/integration/test_chat_session_parity.py`：用 `monkeypatch` 伪造 `input()` 输入同一组脚本（`/help`、空行、未知命令、`/exit`），对比拆分前后输出逐行一致 |
| 验收 | `ruff check .`、`ruff format --check .`、`mypy src`、`pytest tests/integration/test_interactive_cli.py tests/integration/test_interactive_extensions.py` 全过；`deity run <任务>`、管道输入、退出码不变；`git diff --stat` 里没有 `run_service.py` 与 `core/` 的改动 |

### Phase 2 — 输入与渲染（本次重构的核心交付）

| 项目 | 内容 |
|---|---|
| 目标 | 流式输出、工具调用树、diff 渲染、状态栏、prompt_toolkit 输入（历史/多行/补全）、新选择器替代 `menu.choose()` |
| 文件 | 新建 `chat_input.py`、`chat_render.py`、`chat_mentions.py`、`chat_status.py`；`pyproject.toml` 增加 `[project.optional-dependencies] chat`；`chat_commands.py` 接入补全器；删除 `menu.py`（删除前确认 `/skill`、`/mcp`、`/build` 三个调用点已全部迁移，且非 TTY 编号降级已在新选择器里实现）；`README.md` CLI 一节更新输入与渲染说明 |
| 测试 | `tests/unit/test_chat_input.py`（历史、多行、补全、回落）；`tests/unit/test_chat_render.py`（Fake `ChatEvent` 流 → 渲染快照，含工具树折叠、diff 截断、长输出背压）；`tests/integration/test_chat_streaming.py`（FakeModel 端到端：流式输出的最终文本与 `run()` 的 `final_text` 一致）；`tests/integration/test_chat_non_tty.py`（stdin/stdout 非 TTY 时行为与基线一致） |
| 验收 | 未装 `prompt_toolkit` 时 CLI 功能与基线一致（只缺历史/补全）；未装 Textual 时 CLI 正常；长输出（单次 100KB 文本事件）不卡死、不丢尾；`Esc` 在输入态清空当前行；运行态第一次 Ctrl-C 回到提示符（`cancel()` 被调用），第二次 Ctrl-C 退出（退出码 130） |

### Phase 3 — 命令、会话与审批（交互完整度）

| 项目 | 内容 |
|---|---|
| 目标 | 新命令全部可用；审批卡片上线；模式切换上线；`!` shell 上线 |
| 文件 | 新建 `chat_approval.py`；`chat_commands.py` 增加 `/resume /sessions /compact /clear /cost /model /status /auto /doctor`；`chat_session.py` 接入 `approval_mode` 状态机与 `!` 前缀分发；`plan.py` 的 `confirm_build / authorize_dirty_targets` 改走 `chat_approval.py` 的卡片（签名不变）；`docs/03-development/cli-chat-refactor-usage.md`（新，用法文档） |
| 测试 | `tests/unit/test_chat_approval.py`（三种决策的折叠逻辑、`allow_session` 仅本会话有效、deny 不执行）；`tests/integration/test_chat_commands.py`（每个新命令的正常/异常路径：`/resume` 无中断 run、`/sessions` 空列表、`/compact` 未开启、`!` 越界路径拒绝）；`tests/integration/test_chat_modes.py`（普通/计划/自动接受切换，`/auto` 未确认时拒绝进入） |
| 验收 | `/cost` 在无 usage 事件时明确显示 token 未接通（不得编造数字）；`allow_session` 在会话退出后失效（重启 CLI 后再次审批）；`!` 执行越界路径或 `shell=deny` 时拒绝并记录审计事件；`/doctor` 在 v0.8 已实现时与 `deity doctor` 输出一致 |

### Phase 4 — 回归、文档与发布门禁

| 项目 | 内容 |
|---|---|
| 目标 | 全量回归通过；交互录制回放测试落地；文档与回退方案齐全 |
| 文件 | `tests/e2e/test_chat_regression.py`（新，录制回放）；`README.md`、`docs/03-development/cli-chat-refactor-usage.md` 定稿；`docs/04-evaluation/` 增加 CLI 交互评估记录（若有）；`pyproject.toml` 的 `chat` extra 版本锁定 |
| 测试 | 全量 `pytest`；交互录制回放（见第 6 节）；`ruff check .`、`ruff format --check .`、`mypy src` |
| 验收 | 第 8 节验收清单逐项打勾；回退开关验证通过（`[chat] enabled = false` 时恢复旧 `input()` 循环，且现有交互测试仍通过）；发布说明里写明新增命令与行为变化 |

---

## 6. 测试计划

测试原则（AGENTS.md §6）：Core 用 FakeModel/FakeTool，禁止单测依赖真实模型网络；
每个工具覆盖成功、参数错误、权限拒绝、越界、超时、取消；评估在隔离 fixture 中运行。

| 类别 | 内容 | 位置（建议） |
|---|---|---|
| 单元测试（确定性） | `ChatEvent` 翻译：六种 `ModelEvent` kind 各一例，含 `usage` 缺失、`tool_call_arguments_delta` 分片 JSON 拼接；`chat_mentions` 边界：越界路径、超大文件、不存在的路径；`chat_approval` 决策折叠：`allow_once/allow_session/deny` × `shell ask/auto/deny`；`chat_commands` 注册表：未知命令建议（编辑距离）、参数缺失报错 | `tests/unit/test_chat_events.py`、`test_chat_mentions.py`、`test_chat_approval.py`、`test_chat_commands.py` |
| 单元测试（渲染） | `StreamingRenderer` 输入 Fake `ChatEvent` 流，断言快照：文本逐段追加顺序、工具树节点开闭、diff 行数截断（>200 行截断并提示）、`usage` 事件汇总到状态栏；长输出背压：100KB 单事件、1000 条连续事件不丢、不卡死（用 Fake 流 + 超时断言） | `tests/unit/test_chat_render.py`、`test_chat_input.py` |
| 集成测试 | FakeModel 端到端：流式最终文本 == `run()` 的 `final_text`；同一 fixture 下 `run_stream()` 与 `run()` 的 `run_id` 语义一致；`/skill`、`/mcp` 选择只影响下一条任务（复用现有 `test_slash_selection_is_consumed_after_failed_run` 的用例并扩展到新选择器）；运行态取消：FakeModel 长流 + `cancel()` 后终态为 `cancelled`，且 SQLite 里无未终态 run | `tests/integration/test_chat_streaming.py`、`test_chat_commands.py`、`test_chat_modes.py` |
| 交互录制回放 | 录制真实终端输入序列（按键 + 输入文本 + 时间戳）为 JSON fixture，回放时用 FakeModel 重放并对比渲染快照；回放模式绝不执行真实写入或网络副作用（与 V0.9 回放规则一致）；至少覆盖：正常问答、审批拒绝、运行中 Ctrl-C、`/build` dirty 文件拒绝 | `tests/e2e/test_chat_regression.py` + `tests/fixtures/chat/` |
| 非 TTY 降级 | `stdin/stdout` 非 TTY 时：输入走 `input()`、选择走编号、输出整段打印、不写历史文件；`TERM=dumb` 时无颜色、无状态栏；管道输入 `echo "任务" \| deity run` 行为与基线一致 | `tests/integration/test_chat_non_tty.py` |
| 审批与取消回归 | 现有权限回归测试全过；`allow_session` 跨会话不泄漏；`!` shell 的 `deny` 与越界拒绝；`authorize_dirty_targets` 仍逐路径授权（卡片只是展示变化） | 复用现有权限/计划测试 + `test_chat_approval.py` |
| 性能/长输出 | 单轮 100KB 文本、1000 次工具调用的 Fake 流在 10 秒内渲染完成（阈值以首轮基线测量后写入 ADR/实施记录校准，不得伪称已达标）；内存增长：连续 50 轮问答后常驻内存增长不超过首轮基线测量值的 20%（同上，阈值待校准） | `tests/e2e/test_chat_regression.py`（性能段，标记 `slow`） |

测试基线（重构全程不得回退）：现有 `tests/integration/test_interactive_cli.py`（9 测试）、
`tests/integration/test_interactive_extensions.py`（2 测试）、`tests/integration/test_cli.py`、
`tests/e2e/test_v05_plan_build_cli.py`、`tests/e2e/test_cli_extensions.py`。

---

## 7. 依赖与风险

| # | 风险 / 依赖 | 影响 | 缓解与回退 |
|---|---|---|---|
| R1 | `prompt_toolkit` 引入 | 新增可选依赖；版本、许可证、维护状态需在实现时核实 | 只做可选 extra（`chat`），基础安装不受影响；`try/except ImportError` 回落；实现时锁定小版本并把许可证实际输出记入实施记录 |
| R2 | 终端兼容性 | Windows（`msvcrt` 路径）、`TERM=dumb`、窄终端、旧终端不支持括号粘贴 | 非 TTY 与 `TERM=dumb` 时全功能降级；Windows 路径保留现有 `menu.py` 的 `msvcrt` 思路直到新选择器在 Windows 上实测通过；窄终端 diff 截断 |
| R3 | 与 v0.8 TUI 并行开发冲突 | 另一协调 agent 正在同一仓库做 v0.8–v1.0，可能同时改 `main.py`、`menu.py`、`run_service.py` | 本方案动手前必须等 v0.8–v1.0 测试跑完；动手时先 `git pull` / `git log` 确认 TUI 落地状态；`ChatEvent` 命名以 V0.8 已落地的为准，CLI 跟随 |
| R4 | Phase 0 事件订阅改动面 | 若 V0.8 §2.1 未落地，`AgentLoop` 与 `RunService` 要加新接口，存在回归风险 | 先写 ADR 再动手；`event_sink=None` 时行为与现状完全一致；Phase 0 单独验收后再进 Phase 1 |
| R5 | 审批体验改动被误读为放宽权限 | `allow_session` 与 `/auto` 若实现不当会实质放宽审批 | `allow_session` 只在内存、只对 `(tool_name, workspace)`、会话结束失效；`/auto` 需显式确认并打印提示；`PermissionConfig` 本身不动；审批回归测试全覆盖 |
| R6 | 流式渲染与取消的竞态 | 取消时渲染器可能正在写最后一批事件，导致重复打印或状态错乱 | 渲染器以 `run_status` 事件为准终结一轮；取消后只打印一行“已取消”摘要，不再追加模型文本；取消回归测试覆盖 |
| R7 | 长输出背压 | 模型高速输出时 Rich `Live` 刷新跟不上，导致卡顿或丢尾 | 渲染器按事件节流刷新（例如每 50ms 或每 4KB 刷新一次，具体数值以实测校准）；超长单行截断显示；性能测试见第 6 节 |
| R8 | 历史文件含敏感内容 | 用户可能把含密钥的文本粘贴进输入，历史文件会落盘 | 历史文件只存用户输入原始文本；文档里明确提醒不要粘贴密钥；`[chat] max_history` 上限；不记录模型输出；实现时评估是否需要对形如 `sk-`、`ghp_` 的输入做脱敏提示（只提示，不静默改写用户输入） |

回退方案（总开关）：`[chat] enabled = false` 时，`main.py` 的交互入口恢复到
基线 `_interactive()` 的 `input()` 循环（保留一份 `legacy_interactive()`，
实现时从基线原样复制，不得重写）；`prompt_toolkit` 未安装时自动等价于该开关关闭。
回退验证：开关关闭后，现有 9 + 2 个交互测试仍全部通过（Phase 4 验收项）。

---

## 8. 验收清单

> 可执行、可逐项打勾；打勾的依据是测试命令的真实输出，不得凭印象打勾；
> 未运行的项明确标 `NOT_RUN`，不得描述为已通过。

### Phase 0

- [ ] `ChatEvent` 契约 ADR 已写，事件 kind、seq、schema_version 已定
- [ ] `run_stream()` 对 FakeModel 的最终 `AgentResult` 与 `run()` 一致（同一 fixture）
- [ ] 快照 + 增量重放 100 条事件，零工具调用
- [ ] `cancel()` 后终态为 `cancelled`，SQLite 无未终态 run

### Phase 1

- [ ] `main.py` ≤ 约 200 行，不再包含 `_interactive()` 循环体
- [ ] 现有交互测试 9 + 2 全部通过
- [ ] `tests/integration/test_chat_session_parity.py` 拆分前后输出逐行一致
- [ ] `ruff check .`、`ruff format --check .`、`mypy src` 通过
- [ ] `git diff --stat` 无 `core/` 与 `run_service.py` 的改动（Phase 0 除外）

### Phase 2

- [ ] 流式输出可用；工具调用树可折叠；diff 渲染正确；状态栏显示模型/轮次/token/耗时
- [ ] 历史（上下键）、多行（Alt+Enter）、`@文件` 补全、`/命令` 补全可用
- [ ] `menu.py` 已删除且三个调用点全部迁移；非 TTY 编号降级可用
- [ ] 未装 `prompt_toolkit` 时 CLI 功能与基线一致；未装 Textual 时 CLI 正常
- [ ] 长输出（100KB 单事件）不卡死、不丢尾

### Phase 3

- [ ] `/resume /sessions /compact /clear /cost /model /status /auto /doctor` 全部可用
- [ ] 审批卡片三种决策正确；`allow_session` 会话退出后失效
- [ ] `!` shell 越界与 `deny` 时拒绝并有审计事件
- [ ] `/auto` 未确认时拒绝进入；进入时有明确提示

### Phase 4（最终）

- [ ] 全量 `pytest` 通过（记录通过/失败数）
- [ ] 交互录制回放测试通过，回放零真实副作用
- [ ] `[chat] enabled = false` 回退验证通过
- [ ] `README.md` 与 `docs/03-development/cli-chat-refactor-usage.md` 已更新
- [ ] 实施记录附录已补：`prompt_toolkit` 实际锁定版本与许可证、性能阈值实测值

---

## 附录 A. 动手前检查单（实现 agent 第一件事）

1. `git status` 干净（或先确认未提交改动属于谁的，不要覆盖）。
2. `git log --oneline -5`：确认 v0.8–v1.0 是否已合入、测试是否已跑完。
3. 读 `docs/03-development/v0.8-v1.0-implementation-plan.md`：确认 `subscribe_events` 是否已落地。
4. `grep -rn "subscribe_events\|run_stream" src/deity`：确认 Phase 0 是否可跳过。
5. `uv run python -m pytest tests/integration/test_interactive_cli.py tests/integration/test_interactive_extensions.py -q`：确认基线测试先通过，再动手。

## 附录 B. 本方案未决事项（实现时决定，不要在方案里假装已定）

1. `chat/` 子包 vs 扁平文件（二选一，见第 4.2 节）。
2. `AgentLoop` 的事件出口形态：`event_sink` 参数 vs `run_stream()` 变体（二选一，ADR 定）。
3. `prompt_toolkit` 锁定版本与许可证实际输出（实现时以 `pip show` 为准）。
4. 渲染刷新节流数值与性能阈值（以首轮基线测量后校准）。
5. `/compact` 的执行入口：复用现有压缩路径，还是降级实现（实现时核实 `RunConfig.compaction_enabled` 的实际执行入口）。
