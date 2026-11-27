# ADR-025：Phase 2 —— 原生思考流（reasoning_delta）、时间旅行（/undo & /rewind）与自定义项目命令

> 状态：已采纳（2026-11-27）。
> 关联：`docs/02-architecture/adr-015-v1.0-compat.md`、`docs/02-architecture/adr-018-f1-patch-parallel-turn-diff.md`、`docs/02-architecture/adr-024-agy-tui-redesign.md`。

## 背景

Phase 1 完成了 AGY 风格 TUI 架构重构。为进一步提升高频研发效率，Phase 2 引入三项核心能力：

1. **原生思考流（`reasoning_delta`）**：当前模型事件协议未透传思考内容，TUI 只能展示无内容的 `Thinking...` 等待动画。
2. **时间旅行（`/undo` & `/rewind`）**：每轮改文件后缺乏一键撤回文件与会话历史的能力。
3. **自定义项目命令（`.deity/commands/*.md`）**：团队/个人常用工作流提示词无法以斜杠命令形式在工作台与命令面板中一键复用。

## 决策

### 1. 事件协议加法扩展：`reasoning_delta`

- `ModelEvent.kind` 新增 `"reasoning_delta"`（纯加法扩展，`COMPAT_VERSION` 保持为 `1`，旧消费者可安全忽略未知事件）。
- `OpenAICompatibleProvider.parse()` 读取 `delta.reasoning_content` / `delta.reasoning` 发出 `ModelEvent("reasoning_delta", text=...)`。
- `AnthropicProvider.parse()` 读取 `content_block_delta` 中 `delta.type == "thinking_delta"` 的 `delta.thinking` 发出 `ModelEvent("reasoning_delta", text=...)`。
- `AgentLoop._emit_model_event()` 翻译为 live 事件 `ClientEvent(kind="reasoning_delta", payload={"text": ...})`；`_collect()` 不把思考文本混入最终 assistant 回复正文，也不计入不可重试的副作用输出。
- `ChatViewModel` 折叠 `reasoning_delta` 到 `view.reasoning` 与 `ViewDelta.reasoning_appended`，TUI 渲染为可点击折叠的 `ThinkingBlock`（正文或工具到达时自动折叠，随时可展开）。

### 2. 无 Schema 变更的时间旅行：`/undo` 与 `/rewind`

- `TurnDiffTracker` 在每次写工具（`write_file` / `apply_patch` / `delete_file`）落盘前记录受影响文件的原始字节快照（单文件 ≤2MB，工作区内路径边界校验）。
- `RewindHistory`（`application/rewind_service.py`）记录每个会话回合的检查点（`RewindPoint`：序号、提示词摘要、回合前会话消息数、文件前像字典）。
- `SQLiteSessionRepository.truncate_messages(session_id, keep_count)` 按 `ordinal` 截断回滚会话消息表，不改 SQLite Schema。
- `/undo` 回滚最近一个检查点（恢复被修改/删除的文件、删除本轮新建文件，并截断该轮写入的消息）；`/rewind` 列出检查点，`/rewind <序号>` 逆序回滚至目标检查点之前的状态。

### 3. `.deity/commands/*.md` 自定义命令

- 新增 `CustomCommandCatalog`（`application/custom_commands.py`），按优先级扫描 `~/.config/deity/commands/*.md`（用户级）与 `<workspace>/.deity/commands/*.md`（项目级覆盖同名用户级）。
- 严格校验命令文件名（`^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`）与文件体积（≤64KB），遇不可读目录安全跳过；内容作为任务模板，支持 `{{args}}` 与 `$ARGUMENTS` 参数插值。
- 发现的自定义命令自动注入 TUI `/` 补全菜单、`Ctrl+P` 命令面板与 `/help`，执行时展开为普通任务走完整权限、审批与工具管线，绝不绕过安全边界。

## 不改项

- 不修改 SQLite Schema 版本。
- 自定义命令展开后的任务与普通用户输入遵循完全一致的权限、沙箱与审批策略。
