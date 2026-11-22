# ADR-023：Codex 第二轮移植——工具输出预览、审批 diff 与 /model 弹窗、/usage、/rename、沙箱模式切换

> 状态：已接受（2026-11-22，Codex 移植第二轮）。
> 关联：`docs/02-architecture/adr-018-f1-patch-parallel-turn-diff.md`（事件协议与 turn_diff 前例）、`docs/02-architecture/adr-020-f3-sandbox-session-export-requirements.md`（沙箱三档与会话标题）、`docs/02-architecture/adr-022-interactive-workbench.md`（工作台与审批桥）、`~/workspace/deity-work/deity-codex-port-plan.md`（Codex 对照表 §2/§4/§5/§8）。

## 背景

第一轮 Codex 移植（PR #19）明确留了 6 项未做，其中两项要改事件协议
（当时无 ADR 故未做），其余四项用户现已确认需要。本 ADR 一次性定下
这 6 项的协议与行为决定。

Codex 对照（`codex-rs/tui`，提交 ee344ca9，Apache-2.0，Python 重写，
见 `docs/06-legal/third-party-notices.md` 的 Codex 条目）：

- ExecCell：工具输出预览 + 非零退出码附 `(exit N)`；
- ApprovalOverlay：审批内 diff 预览（增绿删红）；
- ModelDialog：`/model` 的交互式模型选择器（列表 + 搜索）；
- `/usage`：账号用量汇总（TokenUsage）；
- `/rename`：重命名当前会话；
- 沙箱：`SandboxPolicy` 四档（DangerFullAccess / ReadOnly /
  ExternalSandbox / WorkspaceWrite）+ 后端（bwrap / landlock /
  seatbelt / windows）+ 模式配置 `sandbox_mode`。

## 决定

### 1. `tool_call_completed` 事件新增 `output_preview` 与 `exit_code`（事件协议扩展）

- payload 新增两个可选字段：`output_preview`（str，见下）与
  `exit_code`（int | null）。AgentLoop 在 `_finish_call` 处从
  `ToolResult` 取数：`exit_code` 直接取 `result.exit_code`（无则
  null）；`output_preview` 取 `result.output` 的前 3 行、总长 ≤500
  字符，超限截断并附 `…（已截断）`，空输出为空串。
- 截断口径集中在 `core/contracts/client.py` 的
  `output_preview_for_event()`（纯函数），Core 不依赖 CLI。
- **兼容**：纯加法扩展。旧消费者忽略未知字段；`schema_version`
  不变（仍为 1，与 ADR-018 的 turn_diff 前例一致）；事件本身不
  落库——live 事件只走进程内事件总线（ADR-017），持久化侧
  `tool_executions.result_json` 早已有 `exit_code`，
  `outcome_json` 早已有 `output[:16000]`，故**无需 SQLite 迁移**。
- **渲染**：工作台 ToolBlock 与行式 `_complete_tool` 在 `⎿ 状态 ·
  耗时` 行后追加预览行（缩进、次级色，最多 3 行）；`exit_code`
  非零时在该行附 `(exit N)`（Codex 口径：零值不显示，不刷屏）；
  `/tools` 展开详情同步显示预览与退出码。

### 2. `ApprovalRequest` 新增 `diff`（审批协议扩展）

- `ApprovalRequest` 新增可选字段 `diff: str | None = None`（统一
  diff 文本，`render_diff` 默认上限 200 行，超限截断并提示）。
- 生成口径集中在 `application/approval_diff.py` 的
  `approval_diff(tool_name, args, workspace)`（纯计算，不写盘；
  落在 Application 层而非 `chat_approval.py`，因 `RunService` 与
  `ClientService` 也要调用——Application 层调 CLI 层会违反分层纪律，
  CLI 层调 Application 层是允许方向；`chat_render.render_diff`
  亦委托给同一实现，避免两份 unified diff 渲染漂移）：
  `apply_patch` 旧形态（path+old/new）渲染 diff、新形态取 patch
  文本直接用（已是 diff 形态，截断）；`write_file` 读盘取旧内容与
  新内容做 diff；`delete_file` 旧内容对空内容做 diff；其余工具返回
  None；读盘失败或内容超 200_000 字符返回 None（不展示，不阻塞审批）。
  旧形态键名以工具 schema 为准（`old`/`new`），`old_text`/`new_text`
  也接受（旧卡片代码曾用此拼写——顺带修掉该 latent bug：原卡片对
  真实旧形态调用从未显示过 diff）。
- 填充点：`RunService._authorize` 与
  `ClientService._register_approval` 在构造 `ApprovalRequest` 时
  附上；委派审批（`_approve_writable_delegation`）不附（无文件目标）。
- **兼容**：可选字段默认 None；`approval_id_for` 的哈希口径不变
  （只含工具名与参数摘要，不含 diff）；`ApprovalRequest` 只存内存，
  不落库，无迁移。
- **渲染**：行式 ApprovalCard 改用同一 helper（`delete_file` 由此
  获得 diff，原先只有文字提示）；工作台审批桥把 diff 随
  `PendingPrompt` 交给面板，面板内以首轮 diff 块同款配色渲染
  （新增行中性浅灰底、删除行红底，无绿色），diff 块在可滚动区内。
  审批语义与五种决定一字不变。

### 3. `/model` 弹窗（仅工作台）

- 工作台内 `/model` 无参数时打开交互式模型选择器：列表取
  `KNOWN_MODELS[provider]`，当前模型打 ● 标记；顶部过滤输入框，
  输入子串实时过滤；点击行或 Enter 选首个匹配项切换本次会话模型；
  Esc 关闭，焦点回输入框。无固定列表的提供方（`openai_compatible`）
  不开弹窗，仍显示行内列表文本。
- 行式 `/model` 不变（行内列表 + `/model <名称>` 切换）。

### 4. `/usage`（行式与工作台）

- 展示本会话本地用量汇总：输入/输出 tokens、轮次、工具调用数、
  上下文占用、耗时；无 usage 事件时 token 段明确标注未接通，不编
  数字。deity 无账号/订阅后端，**不展示配额**，文案中明说仅本地用量。
- 实现为 `ChatStatus.usage_text()`（与 `/cost` 的 `cost_text()` 并列），
  两界面共用。

### 5. `/rename <新标题>`（行式与工作台）

- 存储层新增 `SQLiteSessionRepository.rename_session(session_id,
  title, *, workspace_root=None)`：标题去空白、截断 80 字符（与
  F3/A6 自动标题同一口径）；行存在时 UPDATE 标题；行不存在且给了
  `workspace_root` 时 INSERT 一行（新会话尚未跑任务时也能改名）；
  否则抛 KeyError。只改 `sessions.title`，不碰消息与运行历史，不改
  schema。
- 命令行式与工作台共用语义；改名后 `/sessions` 列表与会话侧栏显示
  新标题（侧栏重填）。

### 6. 沙箱模式切换（Codex `sandbox_mode` 的 deity 映射）

Codex 映射（详见对照表新增 §10）：

| Codex | deity | 结论 |
|---|---|---|
| `SandboxPolicy::ReadOnly` / `WorkspaceWrite` | `SandboxPolicy.READ_ONLY` / `WORKSPACE_WRITE`（ADR-020） | 已有；本次加用户可见的会话内切换 |
| `DangerFullAccess` | `SandboxPolicy.FULL_ACCESS`（仅 local 后端可表达） | 不搬：命令永不切换到 full-access，弱化边界的行为不提供 |
| `ExternalSandbox` | 无对应概念 | 不搬：deity 不在外部沙箱内运行 |
| `network_access` 标志 | `SandboxConfig.network`（bwrap 硬要求 none，ADR-020 B2） | 不搬：bwrap 后端无过滤式出站能力 |
| 后端 bwrap | `BwrapExecutionBackend`（Linux） | 已有 |
| 后端 landlock | 无 | 不搬：bwrap 已提供命名空间隔离，第二 Linux 后端无用户可见收益 |
| 后端 seatbelt（macOS） | 无 | 不搬：本机 Linux 环境无法验证 `sandbox-exec` 行为，不提交未验证的后端 |
| 后端 windows（mxc） | 无 | 不搬：deity 不面向 Windows 沙箱 |
| `sandbox_mode` 配置 | `[sandbox] policy`（配置文件）+ 本次新增 `/sandbox` 会话内切换 | 本次实现后者 |

- 新增 `/sandbox` 斜杠命令（行式与工作台）：无参数显示当前后端、
  策略、是否启用、网络与可用性（bwrap 需 Linux + 系统 bwrap 二进制）；
  带参数 `read-only` / `workspace-write` 时切换本次会话后续运行的
  沙箱策略（`RunService.set_sandbox_policy`，`dataclasses.replace`
  替换冻结配置；`_sandbox_shell` 每次调用重读 `self.sandbox`，故
  无需重建管线）。拒绝项：`full-access`（永不经命令切换）、沙箱未
  启用或后端为 `local`（无隔离后端可切换）、未知策略名——均打印原因，
  不静默失败。持久化仍走配置文件 `[sandbox] policy`，命令切换仅
  本次会话内生效，文案中明说。

## 不做

- 推理 effort 选择（`ModelConfig` 无 effort 字段，见第一轮结论）。
- 审批 diff 的逐行行号（面板内 diff 块只做行着色，不做行号列，面板空间有限）。
- `/usage` 的历史累计与多会话汇总（只展示当前会话）。
