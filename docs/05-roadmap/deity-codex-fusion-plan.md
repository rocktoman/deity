# Deity × Codex 融合计划

> 状态：**已确认 / 已批准（2026-11-15 20:31 CST）**：用户批准本文档进仓库，并按 F1 开始开发（F1 验收与节奏见 §4）。本文不修改既有路线图；与 `docs/05-roadmap/deity-cli-optimization-roadmap.md` 的关系见 §4 的对齐说明。
> 日期：2026-11-15
> 研究基线：
> - Codex：github.com/openai/codex，commit `ee344ca9458351a6ea102795fa73110ad3ede5dc`（2026-11-15，浅克隆于 `~/workspace/codex-study/codex`，150 MB，6,067 个 .rs 文件、约 237.6 万行 Rust）。
> - Deity：本地工作副本 `~/workspace/deity-work/deity`，main 分支 HEAD `cc19786ba3104020df819c43784ed0bb31cedb1f`（2026-11-15，Windows 运行时兼容修复）。V1.1（M1–M4）已全部合并 main。
> 许可：Codex 为 Apache-2.0（仓库根 `LICENSE` + `NOTICE`，已实读确认）。融合方式见 §5。
> 红线：本文不含任何 API 密钥；不复制 Codex 源码大段文本，只写机制与 Deity 侧落点。

---

## 1. Codex 通读结论

Codex 不是一个 CLI 脚本，而是一个 **workspace 化的大型系统**：`codex-rs/` 下有 100+ 个 crate。本文只覆盖与 Deity 可比的核心子集。

### 1.1 架构总览

- **协议层独立成 crate**：`codex-rs/protocol/`（`src/protocol.rs` 单文件约 6,963 行）定义提交/事件协议：客户端向会话提交 `Submission { id, Op }`，会话回吐 `Event { id, EventMsg }`。`Op` 是枚举（Interrupt、UserInput、ExecApproval、PatchApproval、Shutdown 等），`EventMsg` 是对应的事件枚举。CLI、TUI、`codex exec`、app-server（JSON-RPC，供 IDE/桌面端）全部是这套协议的不同前端——**核心与前端彻底解耦**，这是 Codex 能同时长出 TUI/exec/app-server/云任务的根本原因。
- **会话即长驻任务**：`core/src/session/handlers.rs` 的 `submission_loop` 在独立任务里消费提交队列；`core/src/session/turn.rs` 的 `run_turn` 是单轮循环：组装上下文 → 采样（带重试，`try_run_sampling_request`）→ 解析工具调用 → 执行 → 回注结果 → 直到无工具调用。任务类型被抽象为 `core/src/tasks/`（regular / compact / review / user_shell 四种 Task）。
- **Deity 对照**：Deity 的 CLI → Application（RunService）→ Core AgentLoop 分层与此同构，且 V1.1 已补上进程内事件总线；差距不在分层，而在**协议的显性化程度**——Codex 的 Op/Event 是带类型、带审批回执通道（oneshot）的正式契约，Deity 的事件目前主要是单向通知。

### 1.2 工具系统

- 工具注册/路由/编排分开：`core/src/tools/registry.rs`（注册与能力声明，如 `supports_parallel_tool_calls(name)` 按工具逐个声明）、`router.rs`（把模型输出路由成 ToolCall）、`orchestrator.rs`（审批+沙箱+执行的编排）、`handlers/`（每个工具一个 handler 文件，含 shell、apply_patch、multi_agents、mcp 等）。
- **并行是默认能力而非特例**：`core/src/tools/parallel.rs` 的 `ToolCallRuntime` 用 tokio 并发执行同一轮的多个工具调用，是否可并行由工具自身声明（只读工具可并行、写工具串行），并统一处理取消传播与结果排序。
- **统一 exec（unified_exec）**：`core/src/unified_exec/` 是持久进程管理器——shell 不是"跑完即弃"的一次性子进程，而是可寻址的会话进程：`exec` 启动后可用 `write_stdin` 继续喂输入、按 `yield_time_ms` 分段取输出、输出按 token 上限截断，后台终端默认超时 300 秒（`DEFAULT_MAX_BACKGROUND_TERMINAL_TIMEOUT_MS`）。dev server、长测试、交互式 REPL 都靠这套。
- **每轮 diff 追踪**：`core/src/turn_diff_tracker.rs` 在一轮内跟踪所有被工具改动文件的内容与修订号，轮末生成该轮的 unified diff（100 ms 超时则退化为粗粒度精确 diff）。TUI 的改动展示、review 功能都消费这份"每轮 diff"，而不是事后跑 `git diff` 猜。

### 1.3 apply_patch 协议（重点）

独立 crate `codex-rs/apply-patch/`（约 4,778 行），要点：

- **格式是给模型设计的文本协议**，不是 unified diff：`*** Begin Patch` / `*** End Patch` 包裹，内含 `*** Add File:` / `*** Delete File:` / `*** Update File:`（可带 `*** Move to:` 改名）三类 hunk；Update 内用 `@@ [上下文锚]` 分 chunk，行首 `+`/`-`/`空格` 表示增/删/上下文。文法写在 `parser.rs` 头部注释里（Lark 文法），解析器默认宽容模式（为迁就模型输出的不规范）。
- **定位靠分级模糊匹配**：`seek_sequence.rs` 的 `seek_sequence()` 对每个 chunk 的上下文序列按四级严格度依次尝试：精确匹配 → 忽略行尾空白 → 忽略首尾空白 → Unicode 标点归一化（弯引号/破折号→ASCII）后再匹配；`@@` 后的 change_context（如函数签名行）先把搜索范围收窄到该符号之后。找不到才报错。
- **先全量校验再落盘**：parser 只解析校验、file_update 才应用；一个 patch 里多文件多 hunk 原子性地先验后写，失败不写一半。

Deity 现状（实读 `src/deity/adapters/tools/__init__.py`）：`apply_patch` 是单文件单段的 `old`/`new` 精确替换，且要求 `old` 在文件中**恰好出现一次**（`current.count(args["old"]) != 1` 即 patch_conflict），无模糊、无多 hunk、增删文件靠独立工具。这是 V1.2 路线图里已点名的短板，Codex 给出了完整的对照实现。

### 1.4 沙箱与审批（重点）

Codex 是**三层防线**，Deity 目前只有一层半：

1. **OS 级沙箱**（`codex-rs/sandboxing/`）：`manager.rs` 按平台选后端——macOS Seatbelt（`.sbpl` 策略文件随仓库分发）、Linux Landlock + bubblewrap、Windows 受限令牌。沙箱策略由协议层 `SandboxPolicy` 枚举决定（`protocol.rs`）：`danger-full-access` / `read-only{network_access}` / `workspace-write{writable_roots, network_access, exclude_tmpdir_env_var...}` / `external-sandbox`。即"能写哪、能不能联网"是内核强制，不是应用层自觉。
2. **审批策略**（协议层 `AskForApproval` 枚举）：`untrusted`（未信任项目默认全问）/ `on-request`（默认，模型按需申请）/ `granular{...}`（按类别细粒度开关）/ `never`。审批是协议事件，前端只负责呈现与回执。
3. **命令级执行策略 execpolicy**（独立 crate `codex-rs/execpolicy/`）：规则文件用前缀模式（PrefixRule：程序名 + 参数前缀 token 序列）声明 `allow / prompt / prompt-user / forbidden` 四级 Decision；审批弹窗里用户选"以后允许"时，`amend.rs` 把该命令前缀**沉淀成一条持久规则**写回规则文件——审批决定从"会话级临时勾选"升级为"可审计、可编辑、可版本化的策略资产"。另有启发式兜底（heuristics fallback）处理未命中规则的命令。
4. **Guardian 自动审查**：`core/src/guardian/` + `guardian-context` crate + `prompts/templates/guardian/policy.md`——当审批策略要求审查时，由一个审查模型按策略文本对拟执行动作做风险判定（transcript 截断、预算、授权范围都有专门模块）。这是 Codex 对"审批疲劳"的回答：不是让用户全点允许，而是让模型审模型、人只管例外。

Deity 现状：Docker 后端可选（`adapters/execution/docker.py`，以 docker CLI 为边界、明确声明守护进程是可信组件），本地模式只有工作区路径边界 + 审批，AGENTS.md 里也诚实写明"本地工作区限制不等于操作系统沙箱"。审批在 V1.1 M4 已细化到会话级命令模式（首 token，如 `pytest *`），但**不落盘、无规则文件、无自动审查**。

### 1.5 上下文压缩 compact（重点）

- **触发**：token 预算驱动的自动压缩——模型目录给出每个模型的 `model_auto_compact_token_limit`（可配置覆盖、可按 scope 调整，见 `config/src/config_toml.rs`、`models-manager/`），上下文逼近阈值时在轮间自动触发；也有手动 /compact（作为一种 Task 类型 `core/src/tasks/compact.rs`）。
- **做法是 LLM 摘要而非截断**：`core/src/compact.rs` 用专门的摘要提示词（`prompts/src/compact.rs` → 模板文件 `prompts/templates/compact/prompt.md` + `summary_prefix.md`）让模型把历史压成结构化摘要，然后**重建替换历史**：新历史 = 初始上下文（系统提示/world state 按当前状态重建）+ 摘要 + 保留的近期条目（含未处理完的待办输入，`compact_pending_input.rs`）；压缩本身作为事件与 `CompactedItem` 持久化进 rollout，可回放。压缩前后有 hooks（PreCompact/PostCompact）。
- **注意平台专属部分**：`compact_remote_v2.rs` 是调用 OpenAI 服务端压缩接口的版本，与 Responses API 绑定，不可移植。

Deity 现状（实读 `src/deity/core/context/reliable.py`）：压缩是**确定性预算管理**——按 token 预算截断并维护 `compaction_ledger_v1` 账本（哪段被压、压成什么，有据可查），优点是可复现、零额外模型成本、与"可验证"定位契合；缺点是摘要质量不如 LLM 摘要、长会话信息损失大，且 CLI 的 `/compact` 至今只是"查看状态"，没有手动触发入口（`chat_commands.py` 注释自认）。

### 1.6 持久化、resume 与会话管理（重点）

- **Rollout = 每会话一个 append-only JSONL**（`codex-rs/rollout/`，约 18,792 行）：文件 `~/.codex/sessions/YYYY/MM/DD/rollout-<时间戳>-<thread_id>.jsonl`，首行 `SessionMeta`，其后逐条 RolloutItem（消息、工具调用、事件、压缩记录）；`policy.rs` 决定哪些条目落盘。恢复时 `session/rollout_reconstruction.rs` 从 JSONL 重建历史。
- **有意思的收敛**：Codex 后来又在 rollout 之上加了 SQLite 状态库（`rollout/src/state_db.rs`）做会话列表/搜索/元数据索引——即"JSONL 存正文、SQLite 做索引"。Deity 一开始就是 SQLite 事件库（schema v6），在这点上 Deity 的选择被 Codex 的演进反向印证；Deity 缺的是 Codex 那种**开箱即用的会话 UX**：`codex resume`（带选择器、--last）、`fork`（从历史会话分叉）、`archive/delete` 都是 CLI 一等子命令（`cli/src/main.rs`）。
- Deity 现状：有 `/resume [run-id]` 与 `/sessions` 列表切换（CLI 已有），但要用户记 run-id、无分叉、无归档语义；回放（replay）地基在后端但未产品化（V2.0 已规划）。

### 1.7 配置体系

- `config.toml` + **显式分层栈**（`config/src/config_layer_source.rs` 的 `ConfigLayerSource` 枚举）：打包默认值 < 系统级 < 企业托管（MDM/云 bundle）< 用户级（可叠加 profile）< 项目级 `.codex/config.toml` < 会话 flags；另有 `requirements.toml` 作为管理员**强制约束**（只能收紧、不能被下层放宽）。每层来源可追溯，`codex doctor` 一类命令能解释"这个值从哪来"。
- profiles 是同文件 `[profiles.<name>]` 表 + `--profile` 选择；CLI `-c key=value` 做一次性覆盖（SessionFlags 层）。
- Deity 现状：V1.1 已有 `deity init` 与 `deity config show` 分层展示，但校验仍是 `config.py` 手写 TOML 校验、无项目级配置层、无"约束层"概念。Codex 的分层枚举与来源追溯值得照搬结构。

### 1.8 MCP、非交互模式、多 Agent

- **MCP 双向**：客户端（`rmcp-client/` + `core/src/session/mcp*.rs`，配置 `[mcp_servers]`，stdio 与 streamable HTTP，含 OAuth）；服务端（`codex-mcp/` crate，`codex mcp-server` 把 Codex 本身暴露为 MCP 工具供其他 Agent 调用）；管理命令 `codex mcp`（`cli/src/mcp_cmd.rs`）。MCP 工具调用同样走审批协议。Deity 现状：有 MCP 客户端适配（`adapters/mcp/`），无服务端模式。
- **非交互 exec**：`codex-rs/exec/`——`codex exec` 一次性跑完退出；`--json` 时 stdout 是严格 JSONL 事件流（一行一事件，`event_processor_with_jsonl_output.rs`），供 CI/脚本消费；支持 `codex exec resume`。Deity 有一次性 run 入口，但没有稳定的机器可读事件输出契约。
- **多 Agent**：子 Agent 是**独立线程**（`core/src/tools/handlers/multi_agents/`：spawn_agent / send_message / followup_task / close_agent 等，命名空间 `multi_agent_v1`；`core/src/thread_spawn.rs`、`agent-roles` crate 定义角色），子线程有自己的 rollout、继承沙箱与审批策略，父子通过消息通信。注意：Codex 的子 Agent 同样在各自工作区/线程里干活，"可写"靠的是线程隔离 + 结果消息，而不是 patch 回收——这一点与 Deity V2.0 设想的"可写委派 + patch 回收"并不完全同路（见 §3 取舍）。
- **TUI**：`codex-rs/tui/`（ratatui）是事件驱动的 widget 树：`app.rs`（约 1,502 行）跑 App 事件循环，`chatwidget.rs`（约 2,174 行）管理对话区，底部 `bottom_pane/` 是输入编辑器（多行、补全、粘贴），历史以 cell 列表渲染、支持 backtrack（回退到某条历史消息重编重跑，`app_backtrack.rs`）。它消费的仍是同一套 Op/Event 协议——**TUI 只是协议的一个前端**，这正是它敢把 TUI 做厚的前提。Deity V1.1 已按路线图冻结 TUI，此判断与 Codex 的经验并不矛盾：Codex 的 TUI 之所以成立，是因为协议先行；Deity 应先补协议，不应急着解冻 TUI。
- **测试与 eval**：同文件 `*_tests.rs` 单元测试 1,714 个文件量级，`core/tests/suite/` 是用 mock Responses 服务器驱动的端到端会话集成测试（`core/src/test_support.rs`）。没有独立的"任务 benchmark 仓库"，质量门禁主要靠海量回归测试 + 快照。Deity 的 evals 体系（约 80 用例 + 可执行断言）在"任务级评估"上反而更有想法，缺的是 Codex 这种规模的核心回归密度。
- 其它值得记一笔的：`core/src/agents_md.rs` 分层加载 AGENTS.md（全局→项目→当前目录、拼接、上限截断）；`shell_snapshot.rs` 快照用户 shell 环境（函数/别名）让 exec 环境更像用户终端；`hooks` crate 提供生命周期钩子（Deity 已有 Hooks，理念一致）。

---

## 2. Deity 现状对照表

差距等级：★ 小（局部补齐）/ ★★ 中（一个子系统）/ ★★★ 大（跨子系统或新子系统）。

| 机制 | Codex 做法 | Deity 现状（HEAD cc19786 实查） | 差距 |
|---|---|---|---|
| 事件/提交协议 | protocol crate 显式 Op/Event 契约，审批带回执通道，多前端共用 | V1.1 进程内事件总线已统一双轨，但事件以单向通知为主，审批回执未协议化 | ★★ |
| Agent 循环 | Session 长驻 + submission_loop + Task 抽象（regular/compact/review/shell） | AgentLoop 单所有者 + RunService（1,287 行），任务类型未抽象成 Task | ★ |
| 工具并行 | registry 逐工具声明可并行，ToolCallRuntime 并发执行、统一取消 | 适配器写死串行（路线图自述 parallel_tool_calls=False 问题） | ★★ |
| shell 执行 | unified_exec 持久进程：write_stdin、yield 分段输出、后台终端 | 一次性子进程，timeout + 输出上限（run 预算参数），无后台/长驻 | ★★★ |
| apply_patch | 多文件多 hunk 文本协议 + 四级模糊 seek + 先验后写 | 单文件单段精确替换，old 必须恰好出现一次 | ★★★ |
| 每轮 diff | TurnDiffTracker 按轮跟踪内容修订，产出 per-turn diff | 只有 git_diff 工具看全仓 diff，无"本轮改动"一等概念 | ★★ |
| OS 沙箱 | Seatbelt/Landlock+bwrap/Windows 令牌，SandboxPolicy 三档+网络开关 | Docker 可选后端；本地仅路径边界+审批（明示非沙箱） | ★★★ |
| 审批策略 | AskForApproval 四模式 + granular 细粒度 | 策略 + 会话级命令模式允许（M4），模式维度较单一 | ★★ |
| 命令规则沉淀 | execpolicy 前缀规则文件，allow/prompt/prompt-user/forbidden，审批可 amend 成持久规则 | 会话级允许只活在内存，退出即失，无规则文件 | ★★ |
| 自动审查 | Guardian 审查模型按策略文本判定动作风险 | 无；靠人工审批（V2.0 信任分级是设想，未落地） | ★★★ |
| compact | token 阈值自动触发 + LLM 结构化摘要 + 历史重建 + 落盘可回放 | 确定性截断 + compaction_ledger_v1 账本；/compact 只能看不能触发 | ★★ |
| 会话持久化 | JSONL rollout（正文）+ SQLite state_db（索引），resume/fork/archive 一等命令 | SQLite 事件库（schema v6）+ checkpoint；/resume 要记 run-id，无 fork/archive | ★★ |
| 配置分层 | ConfigLayerSource 显式分层 + requirements 约束层 + 来源追溯 | init + config show 分层展示已有；无项目级层、无约束层、手写校验 | ★★ |
| MCP | 客户端 + 服务端 + 管理命令，调用走审批 | 仅客户端适配 | ★★ |
| 非交互输出 | codex exec --json 稳定 JSONL 事件契约 | 有 run 入口，无机器可读事件契约 | ★★ |
| 多 Agent | 子线程独立 rollout、消息通信、角色定义 | 只读委派，结果截断 16K 字符串拼回 | ★★ |
| 代码索引 | （Codex 本身也主要靠 rg + 文件工具，无内置符号索引） | lexical-v1 正则扫定义 | ★（双方都弱，非 Codex 优势项） |
| 任务级 eval | 海量回归测试为主，无独立任务 benchmark 体系 | evals 约 80 用例 + 可执行断言 + A/B 框架 | Deity 占优，保持 |


---

## 3. 融合清单（三类）

工作量级：S = 一周内局部改动；M = 一个里程碑（2–4 周量级）；L = 跨子系统、需 ADR 先行。

### 3.1 (a) 可直接借鉴、用 Python 重新实现的模式

| # | 条目 | Codex 出处 | Deity 落点 | 量级 |
|---|---|---|---|---|
| A1 | **apply_patch 协议升级**：`*** Begin/Update/Add/Delete Patch` 多文件多 hunk 格式 + `@@` 上下文锚 + seek 四级模糊（精确→行尾空白→首尾空白→Unicode 归一）+ 全量先验后写 | `apply-patch/src/parser.rs`、`seek_sequence.rs`、`file_update.rs` | `adapters/tools/__init__.py` 的 PatchArgs 与执行分支拆为独立 `adapters/tools/patch.py`；保留旧 old/new 形式一个版本的兼容层 | M |
| A2 | **只读工具并行**：工具注册表逐个声明 `supports_parallel`，ToolPipeline 对同轮只读调用并发执行、写调用串行，统一取消与保序回注 | `core/src/tools/registry.rs`、`parallel.rs` | `application/tool_pipeline.py` + `adapters/tools/__init__.py` 的工具元数据；OpenAI 适配器放开 `parallel_tool_calls` | M |
| A3 | **execpolicy 规则文件**：把 M4 的会话级命令模式升级为持久规则文件（如 `~/.deity/rules.toml` 或项目级），前缀模式 + allow/prompt/forbidden 三级（Deity 暂不需要 prompt-user 的双人语义），审批弹窗增加"记住为规则"并写回文件 | `execpolicy/src/{policy,rule,decision,amend}.rs` | 新增 `core/policy/exec_rules.py`（解析/匹配）+ `application/` 审批流 amend 动作 + `config show` 展示规则来源 | M |
| A4 | **TurnDiffTracker（每轮 diff）**：Run 开始时对将被改动文件做内容快照（惰性：首次写前快照），轮末产出 per-turn diff，作为事件持久化 | `core/src/turn_diff_tracker.rs` | `application/tool_pipeline.py` 写工具前后钩子 + `core/storage.py` 事件类型新增 `turn_diff` | S–M |
| A5 | **compact 手动触发 + LLM 摘要选项**：/compact 改为真触发；摘要器作为可插拔策略与现有确定性账本并存（摘要产物仍写入 compaction_ledger，可审计） | `core/src/compact.rs`、`prompts/templates/compact/prompt.md`（模板思路，文本需按 Apache-2.0 署名要求处理，见 §5） | `core/context/reliable.py` 增加 `LlmSummarizer` 策略；`interfaces/cli/chat_commands.py` 的 `_handle_compact` 接真入口 | M |
| A6 | **会话 UX 补齐**：resume 改选择器（列表选，不记 id）、会话自动标题、fork（从某 run 分叉新会话）、archive 标记 | `cli/src/main.rs`（resume/fork/archive）、`rollout/src/session_index.rs` | `interfaces/cli/chat_commands.py` 的 /sessions、/resume 扩展；`core/storage.py` 会话元数据加 title/archived 字段（schema 迁移走既有版本机制） | M |
| A7 | **JSONL 导出**：run 可导出为 Codex 式 JSONL（SessionMeta 首行 + 逐条事件），便于外部工具/审计消费；不改变 SQLite 主存储 | `rollout/src/recorder.rs`、`policy.rs` | 新增 `deity export <run-id>` 命令，落 `interfaces/cli/` + `application/` 导出服务 | S |
| A8 | **AGENTS.md 分层加载对齐**：全局→仓库根→当前目录拼接 + 总量上限 + 来源标注（Deity 已读 AGENTS.md，补分层与上限即可） | `core/src/agents_md.rs` | `core/context/` 上下文组装处 | S |

### 3.2 (b) 思路可借鉴、需按 Deity 架构改造

| # | 条目 | 改造要点 | Deity 落点 | 量级 |
|---|---|---|---|---|
| B1 | **unified_exec 式持久 shell**：进程会话表（id→进程）、write_stdin、分段 yield 取输出、后台运行 + 轮末不杀（或按策略回收）。必须服从 Deity 的预算/取消不变量（AGENTS.md §4：取消向子进程传播、未知副作用不重试） | 落在执行适配层而非 Core；进程会话状态进 SQLite 事件流以便回放 | `adapters/execution/local.py` 之上新增 `session_process.py`；`adapters/tools/__init__.py` shell 工具加 session 参数 | L |
| B2 | **Linux 轻量沙箱档**：不移植 Landlock Rust 实现，而是借 Codex 的 SandboxPolicy 三档语义（read-only / workspace-write / full），在 Linux 上以 `bwrap` 命令行（Deity 已有"shell out 到 docker CLI"的先例）实现 workspace-write 档；macOS/Windows 暂不跟，文档明示覆盖范围，不作虚假承诺 | 策略枚举进 contracts，执行后端可插拔 | `core/contracts/` 加 SandboxPolicy；`adapters/execution/` 新增 `bwrap.py` 后端，与 docker/local 并列 | L |
| B3 | **Guardian 式自动审查 → Deity 信任分级**：借"审查者按策略文本判风险"的机制，但判定依据换成 Deity 自己的资产：验证结果、eval 历史、动作类型（只读/可回滚/不可逆），审查结论写入证据链。审查模型只用 Qwen（既定模型决策），且审查失败默认回退人工审批 | 这是 V2.0 信任分级的实现路径，不是新方向 | 新增 `application/trust_review.py`，挂在 ToolPipeline 审批段之前；结论进 `core/verification/` 证据模型 | L |
| B4 | **协议显性化**：把事件总线的事件补成"提交/回执"双向契约（审批、提问、注入输入），为未来 MCP server 模式与 exec --json 打地基；不追求 Codex 6963 行协议的完备度，只补 Deity 前端真实需要的 Op | 需 ADR（AGENTS.md §7：事件协议变更先 ADR + 迁移方案） | `core/contracts/` + `core/events`（现事件定义处） | M–L |
| B5 | **config 约束层**：在现有分层之上加一层"项目约束"（仓库内 `.deity/requirements.toml` 只能收紧：如禁止 full-access、强制审批类别），来源追溯沿用 config show | 与 A3 规则文件同批设计，避免两套文件互相打架 | `application/config.py` | M |
| B6 | **多 Agent 线程化**：借子线程生命周期（spawn/message/close、独立 run 记录、继承策略），但结果回收坚持 Deity V2.0 既定路线（patch 形式 + 父级审查），不照搬 Codex 的纯消息回收 | 与既有 worktree 实验、委派树合并设计 | `core/delegation/` 演进 | L |
| B7 | **exec --json 事件契约**：一次性 run 增加 `--json` 输出模式，事件 schema 与 A7 导出格式共用一套定义 | 依赖 B4 的事件定义稳定 | `interfaces/cli/` run 命令 | S–M |

### 3.3 (c) 不采用（及理由）

| 条目 | 理由 |
|---|---|
| 远程 compact（compact_remote_v2） | 绑定 OpenAI 服务端接口与 Responses API，与 Deity 模型无关定位冲突 |
| Responses API 协议适配 | Deity 已有 OpenAI-compatible + Anthropic 双协议抽象，引第三协议只服务单一厂商，不做 |
| Seatbelt / Windows 沙箱的原生实现 | 平台专属 Rust 系统编程，Python 侧无对应能力；Deity 用 bwrap/docker 覆盖 Linux 主场景即可（B2） |
| ChatGPT 账号登录、云任务（cloud-tasks） | OpenAI 平台业务，与本地 CLI 优先定位无关 |
| app-server / IDE 协议全套 | 当前无 IDE 前端需求；B4 只补协议地基，等真有前端再说 |
| Codex TUI 的厚度（backtrack UI、全功能编辑器） | V1.1 已决策冻结 TUI；Codex 的经验恰恰说明 TUI 要等协议成熟后再投入，不是现在解冻的理由 |
| 把存储从 SQLite 改成 JSONL 主存 | Codex 自己都在 JSONL 之上补了 SQLite 索引（state_db.rs）；Deity 的 SQLite 事件库在查询/回放/证据上更强，只借 JSONL 作导出格式（A7） |
| 用 spawn 线程的纯消息回收替代 patch 回收 | Deity 的可验证差异化要求子 Agent 产出可审查的 patch，这点不让步 |

---

## 4. 分阶段计划（与既有路线图对齐）

对齐原则：既有路线图 `deity-cli-optimization-roadmap.md` 的 V1.2/V2.0 条目**不推翻**；本计划只做三件事——给既有条目补 Codex 的实现参照、调整两处顺序、新增少量 Codex 带来的条目。标注：= Codex 实现证明该条目方向正确且有现成参照；= 既有条目按 Codex 做法具体化；= 建议改动既有安排；= 路线图没有、Codex 带来的新条目。

| 既有条目 | 与 Codex 的关系 |
|---|---|
| V1.2-1 多 hunk/模糊 patch | → A1，参照 apply-patch crate 的文法与 seek 分级 |
| V1.2-1 只读工具并行 | → A2，参照 registry 声明 + ToolCallRuntime |
| V1.2-1 后台 shell | → B1，参照 unified_exec 的进程会话模型 |
| V1.2-2 tree-sitter 索引 | 与 Codex 无关（Codex 同样没有内置符号索引），保持既有安排，不因本计划提前或延后 |
| V1.2-3 分层上下文 + 手动 compact | → A5：compact 摘要策略参照 Codex 的"摘要+历史重建+保留待办"，但保留 Deity 确定性账本作审计层 |
| V1.2-5 /undo /rewind + 每轮快照 | → A4 先行：TurnDiffTracker 式的 per-turn diff 是 undo 的前置数据，先有每轮 diff，快照/回滚才有抓手 |
| V2.0-1 证据时间线 | A4 的 per-turn diff + A5 的压缩账本都是证据时间线的现成数据源 |
| V2.0-2 /replay 产品化 | Codex 的 rollout 重建恢复证明"落盘即可重演"的工程可行性；A7 的 JSONL 导出与之互补 |
| V2.0-3 信任分级自动审批 | → B3：实现路径明确为"Guardian 机制 + Deity 证据数据" |
| V2.0-4 可写多 Agent 委派 | → B6：借 Codex 子线程生命周期，但坚持 patch 回收，不采用纯消息回收 |
| （新增）execpolicy 规则文件 | → A3：路线图 M4 只做了会话级模式，Codex 证明"沉淀为持久规则"才是审批体验的终点形态 |
| （新增）沙箱分级语义 | → B2：路线图只把"非沙箱"列为短板未给路径；Codex 的 SandboxPolicy 三档给出可落地的分级定义，Deity 先在 Linux 落一档 |
| （新增）exec --json / MCP server | → B7 + MCP 服务端列入观察：等 B4 协议地基完成后再评估，不进近期里程碑 |

### 建议里程碑顺序

- **F1（= V1.2 上半，建议下一里程碑）：编辑与执行效率**
内容：A1（patch 协议）+ A2（只读并行）+ A4（per-turn diff）。
目标：模型改代码的成功率与轮次效率可测地提升；每轮改动可视。
验收：沿用路线图 V1.2 标准（10 个真实中型仓库任务跑自带 benchmark，对比 V1.0 的成功率与平均轮次）；patch 工具新增测试覆盖多 hunk/模糊四级/先验后写原子性；per-turn diff 与 git diff 结果一致性抽查。
进展（2026-11-15）：A1/A2/A4 已实现（ADR-018；patch.py + 运行时分段批次 + TurnDiffDispatcher/turn_diff 事件），新增 18 个专项测试；10 任务 Qwen 基准对比与 per-turn diff 抽查随 F1 交付一并完成。
- **F2（= V1.2 下半）：长任务与审批资产**
内容：B1（持久 shell）+ A3（规则文件）+ A5（compact 触发与摘要）。
目标：dev server/长测试能跑；审批决定能沉淀；长会话不丢魂。
验收：后台进程跑 dev server 并被工具读到输出的端到端用例；规则文件 amend 全流程测试；compact 后继续任务的回归用例（含账本可查）。
进展（2026-11-15）：B1/A3/A5 已实现（ADR-019；session_process 会话表 + approval_rules 规则文件 + compact_summary_v1 摘要），新增 31 个专项测试；dev server 端到端、amend 全流程、compact 后继续任务三项验收用例均已落地。
- **F3：安全分级与会话 UX**
内容：B2（bwrap 沙箱档）+ A6（resume 选择器/fork/archive）+ A7（JSONL 导出）+ B5（约束层）。
目标：Linux 上 workspace-write 成为真沙箱而非自觉；会话管理追平主流手感。
验收：沙箱越界写入/联网的拒绝测试；fork 后两条会话独立演进的集成测试。无 bwrap 的环境必须明确降级提示（不假装安全）。
进展（2026-11-16）：B2/A6/A7/B5 已实现（ADR-020；BwrapExecutionBackend + SandboxPolicy 三档 + schema v7 会话标题/归档/分叉 + run_export JSONL + requirements.toml 约束层），新增 23 个专项测试；沙箱只写暂存区、fork 双分叉独立演进两项验收用例均已落地。
- **F4（= V2.0 主体）：差异化兑现**
内容：B3（信任分级）+ B6（可写委派 patch 回收）+ 证据时间线 UI + /replay 产品化（均属既有 V2.0 条目，此处只排期不改义）+ B4/B7 视前端需求推进。
验收：沿用路线图 V2.0 标准（README 第一屏换成证据时间线截图）。
进展（2026-11-16）：B3/B6/证据时间线/replay 已实现（ADR-021；grade_delegation 三级分级 + 可写委派暂存隔离与 patch 回收 + application/timeline 聚合与 CLI/聊天双表面 + 顶层 deity replay），新增 17 个专项测试；B4/B7 无真实前端需求，按约不推进。README 第一屏截图一项待用户侧确认视觉素材后补。

---

## 5. 许可与融合方式

- Codex 仓库根目录 `LICENSE` 为 **Apache License 2.0**，另有 `NOTICE` 文件（已实读）。Deity 与 Codex 语言不同（Python vs Rust），不存在链接/二进制分发层面的义务触发；现实融合方式是**模式移植**：读懂机制后在 Deity 架构内用 Python 重新实现。这类"思想借鉴"本身不受版权约束，但工程伦理与许可合规上仍应做到：
1. 若直接移植了成段的算法逻辑（如 seek_sequence 的分级匹配顺序）或协议/模板文本（如 patch 格式文法、compact 摘要提示词模板），在对应 Deity 文件头或 docs 中注明来源（"参照 openai/codex（Apache-2.0）<路径> 重新实现"），并在仓库放一份第三方声明汇总（建议 `docs/06-legal/third-party-notices.md`，附 Apache-2.0 全文链接与 Codex 的 NOTICE 要点）。
2. 不把 Codex 的商标、名称用于 Deity 的产品命名或宣传（Apache-2.0 不授予商标权）。
3. 本草稿只描述机制，未复制 Codex 源码；后续实现 PR 应在提交说明中标注参照的 Codex 文件路径，便于审计。

## 6. 风险与取舍

1. **最大的风险是做成 Codex 复刻**。Codex 的强项（平台沙箱、协议完备、海量回归）多是 OpenAI 的组织资源堆出来的；Deity 是单人项目，差异化在**可验证性**：证据链、回放、确定性账本、eval 资产——这些 Codex 反而弱。融合的每一条都应回答"它让 Deity 的验证故事更强还是更弱"，只借能加强的（如 per-turn diff、Guardian 机制），不借会稀释的（如纯消息多 Agent 回收）。
2. **规模错位**：Codex 核心子集动辄上万行（rollout 一个 crate 1.9 万行）。Deity 的对应实现应以"够用且可测"为限，A/B 类条目落地时先写最小版本 + 测试，不要按 Codex 的完备度对齐。
3. **沙箱的诚实问题**：B2 只覆盖 Linux/bwrap 一个档。Deity AGENTS.md 已立下"不作虚假安全承诺"的规矩，F3 的文案与 doctor 输出必须明示各平台的真实覆盖，宁可少承诺。
4. **协议变更的兼容性**：B4/B5/A6 都动事件与存储，按 Deity 规矩需先 ADR + 迁移方案（AGENTS.md §7），排期里已把它们放在 F3/F4，不要提前夹带进 F1。
5. **摘要 compact 的模型成本与不确定性**：A5 的 LLM 摘要引入额外调用与不可复现性；保留确定性账本作为审计与回退（摘要失败→账本截断），且摘要所用模型遵循既定决策（Qwen / qwen3.8-omni-flash，密钥只走环境变量）。
6. **Codex 演进很快**：本计划基于 2026-11-15 的 commit ee344ca；Codex 近一年重构频繁（会话/协议层仍在长），参照时以机制为准，不追具体 API 形态。建议每个 F 里程碑开工前快速复查一次对应模块是否有大改。

---

## 附：本次通读实际打开过的 Codex 文件（证据清单）

- 协议与循环：`codex-rs/protocol/src/protocol.rs`（Op/EventMsg/AskForApproval/SandboxPolicy 定义）、`core/src/session/handlers.rs`（submission_loop）、`core/src/session/turn.rs`（run_turn）、`core/src/tasks/`、`core/src/codex_thread.rs`
- 工具：`core/src/tools/{registry,parallel,orchestrator}.rs`、`core/src/tools/handlers/multi_agents/`、`core/src/unified_exec/{mod,process_manager}.rs`、`core/src/turn_diff_tracker.rs`
- patch：`apply-patch/src/{parser,seek_sequence,file_update}.rs`
- 沙箱与审批：`sandboxing/src/manager.rs`、`execpolicy/src/{policy,rule,decision,amend}.rs`、`core/src/guardian/`、`prompts/templates/guardian/policy.md`、`docs/execpolicy.md`
- compact：`core/src/compact.rs`、`prompts/src/compact.rs`、`prompts/templates/compact/{prompt,summary_prefix}.md`、`config/src/config_toml.rs`（auto_compact 配置项）
- 持久化：`rollout/src/{recorder,policy,state_db,rollout_file_name}.rs`、`core/src/session/rollout_reconstruction.rs`、`cli/src/main.rs`（resume/fork/archive）
- 配置：`config/src/config_layer_source.rs`、`config/src/state.rs`（ConfigLayerStack）
- MCP/exec/TUI：`codex-mcp/src/`、`rmcp-client/src/`、`cli/src/mcp_cmd.rs`、`exec/src/{lib,event_processor_with_jsonl_output}.rs`、`tui/src/{app,chatwidget}.rs`、`app-server/README.md`
- 许可：`LICENSE`、`NOTICE`

Deity 侧实查文件：`docs/05-roadmap/deity-cli-optimization-roadmap.md`、`AGENTS.md`、`src/deity/adapters/tools/__init__.py`（apply_patch 现行实现）、`src/deity/core/context/reliable.py`（compact 账本）、`src/deity/interfaces/cli/chat_commands.py`（/resume、/compact 现状）、`src/deity/adapters/execution/{local,docker}.py`、`src/deity/application/{run_service,tool_pipeline,config}.py`（行数与职责核对）。
