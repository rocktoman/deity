"""全屏交互工作台（ADR-022）：底部输入、流式对话、界面内审批、状态栏。

定位：TTY 下的默认交互界面（无子命令的 `deity`、`deity run` 不带任务、
`deity tui` 无 --run-id 时进入）。与行式 REPL 共用同一条事件通路：
本进程内驱动 RunService.run_stream()，事件喂入既有 ChatViewModel
折叠成状态树，对话区按 ViewDelta 增量建块；审批经 TuiApprovalBridge
复用既有 ApprovalController（语义与 CLI 卡片完全一致），本模块只做
呈现与回填。监视既有 Run 是 interfaces/tui/app.py 的 TuiApp 的活。

按键：Enter 提交、Ctrl+J/Shift+Enter 换行、首/末行 ↑/↓ 走输入历史、
Ctrl+C 或 Esc 取消当前 Run、Ctrl+Q 退出。
"""

import asyncio
import shlex
import time
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Footer, Header, Markdown, Static, TextArea

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.build_service import BuildService
from deity.application.doctor import run_doctor
from deity.application.plan_service import PlanService
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_commands import HELP_TEXT, ChatState, run_error_message
from deity.interfaces.cli.chat_status import ChatStatus
from deity.interfaces.cli.chat_view_model import ChatViewModel, ToolNode
from deity.interfaces.cli.plan import build_approved, plan_lines
from deity.interfaces.tui.approval_bridge import PendingPrompt, TuiApprovalBridge

_CLI_ONLY_COMMANDS = ("/skill", "/mcp", "/fork", "/timeline")


class ChatInput(TextArea):
    """工作台输入框：Enter 提交、Ctrl+J 换行、首/末行方向键走历史。"""

    class Submitted(Message):
        """用户提交了一段输入（Enter）；文本是否采纳由 App 决定。"""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CancelRequested(Message):
        """输入框内按 Ctrl+C / Esc：请求取消当前 Run。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    async def _on_key(self, event: Any) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            if text.strip():
                self._push_history(text)
            self.post_message(self.Submitted(text))
            return
        if event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key in ("ctrl+c", "escape"):
            event.prevent_default()
            event.stop()
            self.post_message(self.CancelRequested())
            return
        if event.key == "up" and self.cursor_location[0] == 0 and self._history:
            event.prevent_default()
            event.stop()
            self._history_prev()
            return
        if (
            event.key == "down"
            and self._history_index is not None
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            event.prevent_default()
            event.stop()
            self._history_next()
            return
        await super()._on_key(event)

    def _push_history(self, text: str) -> None:
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = None
        self._draft = ""

    def _set_text(self, text: str) -> None:
        self.text = text
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))

    def _history_prev(self) -> None:
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_text(self._history[self._history_index])

    def _history_next(self) -> None:
        assert self._history_index is not None
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_text(self._history[self._history_index])
        else:
            self._history_index = None
            self._set_text(self._draft)


class InteractiveApp(App[None]):
    """Codex / Claude Code 式交互界面：对话区 + 审批面板 + 状态栏 + 输入框。"""

    CSS: ClassVar[str] = """
    #conversation { height: 1fr; padding: 0 1; }
    #prompt-panel { display: none; border: solid $warning; padding: 0 1; }
    #prompt-panel.show { display: block; }
    #prompt-scroll { max-height: 14; }
    #status-bar { height: 1; background: $panel; padding: 0 1; }
    #input { height: 5; border: solid $primary; }
    .msg-user { margin-top: 1; text-style: bold; }
    .msg-system { color: $text-muted; }
    .msg-tool { border-left: thick $primary; padding-left: 1; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+c", "cancel_run", "取消"),
        Binding("ctrl+q", "quit", "退出"),
    ]

    def __init__(
        self,
        service: RunService,
        *,
        bridge: TuiApprovalBridge,
        model_name: str,
        workspace: Path,
        session_id: str | None = None,
        config_path: Path | None = None,
        initial_task: str | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self._bridge = bridge
        self._workspace = workspace
        self._config_path = config_path
        self.state = ChatState(session_id=session_id or str(uuid4()), model_name=model_name)
        self.session_status = ChatStatus()
        self.session_status.context_window_tokens = service.context_window_tokens
        self._initial_task = initial_task
        self._view: ChatViewModel | None = None
        self._run_active = False
        self._current_run_id: str | None = None
        self._last_run_id: str | None = None
        self._last_plan_id: str | None = None
        self._text_block: Markdown | None = None
        self._rendered_text_len = 0
        self._tool_blocks: dict[str, Static] = {}
        self._prompt_handler: Any = None
        self._confirm_futures: list[asyncio.Future[bool]] = []
        self.title = "Deity"
        self.sub_title = str(workspace)
        bridge.on_prompt = lambda pending: self.call_from_thread(self._present_prompt, pending)
        bridge.on_prompt_done = lambda: self.call_from_thread(self._hide_prompt)

    # -- 布局 -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="conversation")
        with Vertical(id="prompt-panel"):
            with VerticalScroll(id="prompt-scroll"):
                yield Static("", id="prompt-text")
            with Horizontal(id="prompt-buttons"):
                yield Button("允许一次", id="btn-allow-once", variant="success")
                yield Button("本次会话都允许", id="btn-allow-session")
                yield Button("永久允许（保存为规则）", id="btn-allow-always")
                yield Button("拒绝", id="btn-deny", variant="error")
                yield Button("确认", id="btn-confirm", variant="success")
                yield Button("取消", id="btn-cancel")
        yield Static("", id="status-bar")
        yield ChatInput(id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#input", ChatInput).focus()
        self._hide_prompt()
        self._refresh_status()
        self.set_interval(0.5, self._refresh_status)
        self._append_system("欢迎来到 Deity。输入任务开始，/help 查看命令，Ctrl+Q 退出。")
        if self._initial_task:
            self._submit(self._initial_task)

    # -- 对话区 -----------------------------------------------------------
    @property
    def _conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    def _append_system(self, text: str) -> None:
        self._conversation.mount(Static(text, classes="msg-system"))
        self._conversation.scroll_end(animate=False)

    def _append_user(self, text: str) -> None:
        self._conversation.mount(Static(f"❯ {text}", classes="msg-user"))
        self._conversation.scroll_end(animate=False)

    async def _append_assistant_final(self, text: str) -> None:
        block = Markdown(text, classes="msg-assistant")
        await self._conversation.mount(block)
        self._conversation.scroll_end(animate=False)

    # -- 提交与执行 ---------------------------------------------------------
    def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        self._submit(message.text)

    def on_chat_input_cancel_requested(self, message: ChatInput.CancelRequested) -> None:
        self.action_cancel_run()

    def _submit(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            return
        if text.startswith("/"):
            self.query_one("#input", ChatInput).text = ""
            self.run_worker(self._dispatch_command(text), group="cmd", exclusive=False)
            return
        if text.startswith("!"):
            self._append_system("shell 直通（!命令）请在行式 CLI 中使用。")
            return
        if self._run_active:
            self._append_system("当前有 Run 在运行；先等待完成，或按 Ctrl+C 取消后再提交。")
            return
        self.query_one("#input", ChatInput).text = ""
        if self.state.plan_mode:
            self.run_worker(self._plan_flow(text), group="run", exclusive=True)
        else:
            self.run_worker(self._execute_task(text), group="run", exclusive=True)

    async def _execute_task(self, text: str) -> None:
        """跑一轮任务：run_stream 事件喂 ChatViewModel 并增量建块。"""
        self._run_active = True
        self._append_user(text)
        view = ChatViewModel()
        view.status.context_window_tokens = self.service.context_window_tokens
        self._view = view
        self._text_block = None
        self._rendered_text_len = 0
        self._refresh_status()
        try:
            stream = self.service.run_stream(
                text, model_name=self.state.model_name, session_id=self.state.session_id
            )
            async for event in stream:
                self._current_run_id = event.run_id
                delta = view.apply(event)
                if delta.text_appended:
                    await self._sync_text_block(view)
                if delta.tool_started is not None:
                    self._close_text_block(view)
                    self._mount_tool_block(delta.tool_started)
                if delta.tool_completed is not None:
                    self._update_tool_block(delta.tool_completed)
                if delta.terminal:
                    await self._finish_text_block(view)
                self._refresh_status()
        except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
            self._append_system(f"运行失败：{run_error_message(exc)}")
        finally:
            if self._current_run_id is not None:
                self._last_run_id = self._current_run_id
            self._current_run_id = None
            self._accumulate(view)
            self._run_active = False
            self._refresh_status()

    async def _sync_text_block(self, view: ChatViewModel) -> None:
        segment = view.text[self._rendered_text_len :]
        if self._text_block is None:
            if not segment:
                return
            self._text_block = Markdown("", classes="msg-assistant")
            await self._conversation.mount(self._text_block)
        await self._text_block.update(segment)
        self._conversation.scroll_end(animate=False)

    def _close_text_block(self, view: ChatViewModel) -> None:
        if self._text_block is not None:
            self._rendered_text_len = len(view.text)
            self._text_block = None

    async def _finish_text_block(self, view: ChatViewModel) -> None:
        """终态收口：流式文本缺失时用 final_text 补一块。"""
        if self._text_block is not None:
            segment = view.text[self._rendered_text_len :]
            if segment:
                await self._text_block.update(segment)
            self._close_text_block(view)
        elif view.final_text:
            await self._append_assistant_final(view.final_text)

    def _mount_tool_block(self, node: ToolNode) -> None:
        block = Static(self._tool_line(node), classes="msg-tool")
        self._tool_blocks[node.call_id] = block
        self._conversation.mount(block)
        self._conversation.scroll_end(animate=False)

    def _update_tool_block(self, node: ToolNode) -> None:
        block = self._tool_blocks.get(node.call_id)
        if block is not None:
            block.update(self._tool_line(node))

    def _tool_line(self, node: ToolNode) -> str:
        status = node.status or "运行中"
        elapsed = f" · {node.elapsed(time.monotonic):.1f}s" if node.finished_at else ""
        if node.expanded:
            return (
                f"▾ {node.name}（{node.call_id}）· {status}{elapsed}\n参数：{node.args_text or '—'}"
            )
        summary = node.arguments_summary() or "—"
        return f"▸ {node.name} · {status}{elapsed} · {summary}"

    def _accumulate(self, view: ChatViewModel) -> None:
        """把一轮的计数并入会话级状态（/cost、/status、状态栏的口径）。"""
        source, target = view.status, self.session_status
        target.turns += source.turns
        target.tool_calls += source.tool_calls
        if source.input_tokens is not None:
            target.input_tokens = (target.input_tokens or 0) + source.input_tokens
        if source.output_tokens is not None:
            target.output_tokens = (target.output_tokens or 0) + source.output_tokens
        if source.last_input_tokens is not None:
            target.last_input_tokens = source.last_input_tokens
        target.elapsed_seconds += source.elapsed_seconds

    # -- 审批/确认面板 ------------------------------------------------------
    def _present_prompt(self, pending: PendingPrompt) -> None:
        """呈现审批卡片或确认（桥从工作线程经 call_from_thread 调入）。"""
        self.query_one("#prompt-text", Static).update(pending.text or "（无内容）")
        if pending.kind == "approval":
            mapping = {
                "btn-allow-once": "1",
                "btn-allow-session": "2",
                "btn-allow-always": "4",
                "btn-deny": "3",
            }
            visible = {"btn-allow-once", "btn-allow-session", "btn-deny"}
            if pending.allow_permanent:
                visible.add("btn-allow-always")
            session_button = self.query_one("#btn-allow-session", Button)
            if pending.pattern:
                session_button.label = f"本次会话允许同类命令（{pending.pattern}）"
            else:
                session_button.label = "本次会话都允许"
            self._set_prompt_buttons(visible)
            self._prompt_handler = lambda bid: self._bridge.submit(mapping.get(bid, "3"))
            focus_id = "btn-allow-once"
        else:
            mapping = {"btn-confirm": "yes", "btn-cancel": ""}
            self.query_one("#btn-confirm", Button).label = "确认"
            self.query_one("#btn-cancel", Button).label = "取消"
            self._set_prompt_buttons({"btn-confirm", "btn-cancel"})
            self._prompt_handler = lambda bid: self._bridge.submit(mapping.get(bid, ""))
            focus_id = "btn-confirm"
        panel = self.query_one("#prompt-panel")
        panel.add_class("show")
        self.query_one(f"#{focus_id}", Button).focus()

    def _set_prompt_buttons(self, visible: set[str]) -> None:
        for button in self.query("#prompt-buttons Button"):
            assert isinstance(button, Button)
            button.styles.display = "block" if button.id in visible else "none"

    def _hide_prompt(self) -> None:
        self.query_one("#prompt-panel").remove_class("show")
        self._prompt_handler = None
        if self.is_running:
            self.query_one("#input", ChatInput).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = self._prompt_handler
        if handler is not None and event.button.id is not None:
            handler(event.button.id)

    async def _confirm(
        self, text: str, *, confirm_label: str = "确认", cancel_label: str = "取消"
    ) -> bool:
        """界面内二选一确认（计划构建、脏文件授权用）；退出时按取消收口。"""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirm_futures.append(future)
        self.query_one("#prompt-text", Static).update(text)
        self.query_one("#btn-confirm", Button).label = confirm_label
        self.query_one("#btn-cancel", Button).label = cancel_label
        self._set_prompt_buttons({"btn-confirm", "btn-cancel"})

        def handler(bid: str) -> None:
            if not future.done():
                future.set_result(bid == "btn-confirm")

        self._prompt_handler = handler
        self.query_one("#prompt-panel").add_class("show")
        self.query_one("#btn-confirm", Button).focus()
        try:
            return await future
        finally:
            self._confirm_futures.remove(future)
            self._hide_prompt()

    # -- 取消与退出 ---------------------------------------------------------
    def action_cancel_run(self) -> None:
        """Ctrl+C：有待决审批先拒绝它，再取消当前 Run；无 Run 时不误退。"""
        if self._bridge.pending is not None:
            self._bridge.submit("3")
        if self._run_active and self._current_run_id is not None:
            self.service.cancel(self._current_run_id)
            self._append_system("已请求取消当前 Run。")
        elif self._run_active:
            # run_id 尚未随首个事件到达：取消消费 worker，run_stream 的
            # finally 会连带取消底层 Run。
            self.workers.cancel_group(self, "run")
            self._append_system("已请求取消当前 Run。")

    async def action_quit(self) -> None:
        """退出：先 abort 审批桥与未决确认，再取消活动 Run，不留挂起。"""
        self._bridge.abort()
        for future in self._confirm_futures:
            if not future.done():
                future.set_result(False)
        if self._run_active and self._current_run_id is not None:
            self.service.cancel(self._current_run_id)
        self.exit()

    # -- 状态栏 -------------------------------------------------------------
    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
        except Exception:  # noqa: BLE001 - 未挂载时刷新是空操作
            return
        if self._bridge.controller.mode == "auto":
            mode = "自动接受"
        elif self.state.plan_mode:
            mode = "计划"
        else:
            mode = "普通"
        if self._run_active and self._view is not None:
            status = self._view.status
            current_tool = self._view.current_tool
            line = status.summary_line(
                model_name=self.state.model_name,
                mode=mode,
                current_tool=current_tool.name if current_tool is not None else None,
            )
            dots = "." * (int(time.monotonic() * 2) % 4)
            run_state = f"运行中{dots}"
        else:
            line = self.session_status.summary_line(model_name=self.state.model_name, mode=mode)
            run_state = "空闲"
        bar.update(
            f"{line} · {run_state} · 工作区 {self._workspace} · 会话 {self.state.session_id[:8]}"
        )

    # -- 计划模式 -----------------------------------------------------------
    async def _plan_flow(self, text: str) -> None:
        """计划模式提交：建计划 → 界面确认 → 脏文件确认 → Build。"""
        self._run_active = True
        self._append_user(text)
        self._refresh_status()
        try:
            artifact = await PlanService(self.service).create(
                text, model_name=self.state.model_name, session_id=self.state.session_id
            )
            self._last_plan_id = artifact.plan_id
            plan_text = "\n".join(plan_lines(artifact, ()))
            self._append_system(f"计划 ID：{artifact.plan_id}\n{plan_text}")
            if not await self._confirm(
                "批准此计划并执行 Build？", confirm_label="执行 Build", cancel_label="保留草案"
            ):
                self._append_system("已保留计划；输入 /build 可稍后执行。")
                return
            allowed = await self._authorize_dirty(artifact.plan_id, ())
            if allowed is None:
                return
            result = await build_approved(self.service, artifact.plan_id, allowed)
            await self._append_assistant_final(result.final_text or "无最终回答")
        except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
            self._append_system(f"运行失败：{run_error_message(exc)}")
        finally:
            self._run_active = False
            self._refresh_status()

    async def _authorize_dirty(
        self, plan_id: str, allow_dirty: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """脏文件集中确认：全部同意才返回授权集，拒绝返回 None 并保留计划。"""
        build = BuildService(
            self.service.db_path,
            self.service.workspace,
            plan_ttl_seconds=self.service.coding.plan_ttl_seconds,
        )
        permitted = set(allow_dirty)
        pending = [
            path for path in await build.initial_dirty_targets(plan_id) if path not in permitted
        ]
        if not pending:
            return tuple(sorted(permitted))
        listing = "\n".join(f"{path} 已有本地改动。" for path in pending)
        if not await self._confirm(
            f"{listing}\n允许本次 Build 修改以上全部文件？",
            confirm_label="允许修改",
            cancel_label="保留计划",
        ):
            self._append_system(
                f"已保留计划；可用 /build {plan_id} "
                f"--allow-dirty {' --allow-dirty '.join(pending)} 稍后授权。"
            )
            return None
        permitted.update(pending)
        return tuple(sorted(permitted))

    # -- 斜杠命令 -------------------------------------------------------------
    async def _dispatch_command(self, prompt: str) -> None:
        """工作台斜杠命令子集；语义对齐 chat_commands，不相容的明确提示。"""
        if prompt in {"/exit", "/quit"}:
            await self.action_quit()
            return
        if prompt == "/help":
            self._append_system(
                HELP_TEXT + "\n工作台按键：Enter 提交 · Ctrl+J 换行 · Ctrl+C 取消 · Ctrl+Q 退出"
            )
            return
        if prompt == "/status":
            await self._command_status()
            return
        if prompt == "/cost":
            self._append_system(self.session_status.cost_text())
            return
        if prompt == "/tools" or prompt.startswith("/tools "):
            self._command_tools(prompt)
            return
        if prompt == "/clear":
            await self._conversation.remove_children()
            self._append_system("已清屏。")
            return
        if prompt == "/compact":
            enabled = bool(self.service.limits.compaction_enabled)
            if enabled:
                self._append_system(
                    "上下文压缩已开启（[run] compaction_enabled = true），"
                    "在每轮运行时自动执行；没有独立的触发入口。"
                )
            else:
                self._append_system(
                    "上下文压缩未开启（[run] compaction_enabled = false）。"
                    "/compact 没有独立的触发入口；开启后压缩在每轮运行时自动执行。"
                )
            return
        if prompt == "/plan":
            if (
                not self.service.coding.plan_build_enabled
                or not self.service.coding.git_guard_enabled
            ):
                self._append_system(
                    "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
                )
                return
            self.state.plan_mode = True
            self._append_system("已进入计划模式。输入任务后将展示计划并询问是否构建。")
            self._refresh_status()
            return
        if prompt == "/normal":
            self.state.plan_mode = False
            if self._bridge.controller.mode == "auto":
                self._bridge.controller.exit_auto()
                self._append_system("已退出自动接受模式，恢复 ask。")
            self._append_system("已返回普通模式。")
            self._refresh_status()
            return
        if prompt == "/model" or prompt.startswith("/model "):
            requested = prompt.removeprefix("/model").strip()
            if not requested:
                self._append_system(
                    f"当前模型：{self.state.model_name}（提供方 {self.service.provider_id or '—'}）"
                )
            else:
                self.state.model_name = requested
                self._append_system(f"已切换模型：{requested}（仅本次会话内生效，下轮任务生效）。")
                self._refresh_status()
            return
        if prompt == "/auto":
            self._command_auto()
            return
        if prompt == "/doctor":
            report = run_doctor(
                self._config_path or Path.home() / ".config/deity/config.toml", self._workspace
            )
            lines = [f"{check.name}: {check.status} {check.detail}" for check in report.checks]
            self._append_system("\n".join(lines))
            return
        if prompt == "/sessions" or prompt.startswith("/sessions "):
            await self._command_sessions(prompt)
            return
        if prompt == "/build" or prompt.startswith("/build "):
            await self._command_build(prompt)
            return
        if prompt == "/resume" or prompt.startswith("/resume "):
            await self._command_resume(prompt)
            return
        if prompt.split(maxsplit=1)[0] in _CLI_ONLY_COMMANDS:
            self._append_system("该命令请在行式 CLI 中使用（非 TTY 或 deity run 的行式界面）。")
            return
        if prompt.startswith("/"):
            self._append_system("未知命令。输入 /help 查看可用命令。")
            return

    async def _command_status(self) -> None:
        if self.state.plan_mode:
            mode = "计划"
        elif self._bridge.controller.mode == "auto":
            mode = "自动接受"
        else:
            mode = "普通"
        status = self.session_status
        lines = [
            f"模型：{self.state.model_name}",
            f"模式：{mode}",
            f"会话：{self.state.session_id[:8]}",
            f"工作区：{self._workspace}",
            f"轮次：{status.turns} · 工具调用：{status.tool_calls} 次",
            f"Token：{status.token_text()}",
            f"耗时：{status.elapsed_seconds:.1f}s",
        ]
        repo = await SQLiteSessionRepository.open(self.service.db_path)
        try:
            messages = await repo.messages(self.state.session_id)
        finally:
            await repo.close()
        count = len([m for m in messages[-8:] if m.get("role") in {"user", "assistant"}])
        lines.append(f"上下文消息数：{count}（最近 8 条口径）")
        self._append_system("\n".join(lines))

    def _command_tools(self, prompt: str) -> None:
        view = self._view
        if view is None or not view.tools:
            self._append_system("上一轮没有工具调用记录。")
            return
        parts = prompt.split(maxsplit=1)
        targets = (
            [view.tools[parts[1].strip()]]
            if len(parts) == 2 and parts[1].strip() in view.tools
            else list(view.tools.values())
        )
        if len(parts) == 2 and parts[1].strip() not in view.tools:
            self._append_system(f"未找到工具调用：{parts[1].strip()}")
            return
        for node in targets:
            view.toggle_expanded(node.call_id)
            block = self._tool_blocks.get(node.call_id)
            if block is not None:
                block.update(self._tool_line(node))

    def _command_auto(self) -> None:
        """进入/退出自动接受模式；进入走桥的显式确认（confirm 面板）。"""
        controller = self._bridge.controller
        if controller.mode == "auto":
            controller.exit_auto()
            self._append_system("已退出自动接受模式，恢复 ask。")
            self._refresh_status()
            return
        self.run_worker(self._auto_worker(), group="prompt", exclusive=False)

    async def _auto_worker(self) -> None:
        accepted = await asyncio.to_thread(self._bridge.request_auto)
        if accepted:
            self._append_system("已进入自动接受模式。输入 /normal 退出并恢复 ask。")
        self._refresh_status()

    async def _command_sessions(self, prompt: str) -> None:
        parts = prompt.split(maxsplit=1)
        repo = await SQLiteSessionRepository.open(self.service.db_path)
        try:
            items = await repo.sessions(include_archived=True)
        finally:
            await repo.close()
        if len(parts) == 2:
            target = parts[1].strip()
            if self._run_active:
                self._append_system("有 Run 在运行，结束后再切换会话。")
                return
            if not any(item["id"] == target for item in items):
                self._append_system(f"未知会话：{target}")
                return
            self.state.session_id = target
            self._append_system(f"已切换会话：{target[:8]}")
            self._refresh_status()
            return
        if not items:
            self._append_system("（暂无已存储会话）")
            return
        lines = []
        for item in items:
            marker = "●" if item["id"] == self.state.session_id else " "
            archived = " [已归档]" if item["archived"] == "1" else ""
            title = f"  {item['title']}" if item["title"] else ""
            lines.append(f"{marker} {item['id'][:8]}  {item['workspace']}{title}{archived}")
        lines.append("切换会话：/sessions <完整会话ID>")
        self._append_system("\n".join(lines))

    async def _command_build(self, prompt: str) -> None:
        if self._run_active:
            self._append_system("当前有 Run 在运行；结束后再执行 Build。")
            return
        tokens = shlex.split(prompt)
        plan_id: str | None = None
        allow_dirty: list[str] = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token == "--allow-dirty" and index + 1 < len(tokens):
                allow_dirty.append(tokens[index + 1])
                index += 2
                continue
            if plan_id is None:
                plan_id = token
            index += 1
        plan_id = plan_id or self._last_plan_id
        if plan_id is None:
            self._append_system("暂无计划：先 /plan 进入计划模式并提交任务，或 /build <计划ID>。")
            return

        async def execute() -> None:
            self._run_active = True
            self._refresh_status()
            try:
                allowed = await self._authorize_dirty(plan_id, tuple(allow_dirty))
                if allowed is None:
                    return
                result = await build_approved(self.service, plan_id, allowed)
                await self._append_assistant_final(result.final_text or "无最终回答")
            except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
                self._append_system(f"运行失败：{run_error_message(exc)}")
            finally:
                self._run_active = False
                self._refresh_status()

        self.run_worker(execute(), group="run", exclusive=True)

    async def _command_resume(self, prompt: str) -> None:
        parts = prompt.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            hint = f"（最近一次：{self._last_run_id[:8]}）" if self._last_run_id else ""
            self._append_system(f"用法：/resume <运行ID>{hint}")
            return
        if self._run_active:
            self._append_system("当前有 Run 在运行；结束后再恢复。")
            return
        run_id = parts[1].strip()

        async def execute() -> None:
            self._run_active = True
            self._current_run_id = run_id
            self._refresh_status()
            try:
                result = await self.service.resume(run_id)
                self._last_run_id = result.run_id
                await self._append_assistant_final(result.final_text or "无最终回答")
                self._append_system(f"恢复完成：{result.run_id[:8]} · {result.status.value}")
            except (
                ModelFailure,
                ProtocolError,
                BudgetExceeded,
                ValueError,
                OSError,
                KeyError,
            ) as exc:
                self._append_system(f"运行失败：{run_error_message(exc)}")
            finally:
                self._current_run_id = None
                self._run_active = False
                self._refresh_status()

        self.run_worker(execute(), group="run", exclusive=True)


def run_interactive(
    service: RunService,
    *,
    bridge: TuiApprovalBridge,
    model_name: str,
    workspace: Path,
    session_id: str | None = None,
    config_path: Path | None = None,
    initial_task: str | None = None,
) -> None:
    """启动全屏交互工作台；调用方负责 Textual 未安装时的提示与回落。"""
    InteractiveApp(
        service,
        bridge=bridge,
        model_name=model_name,
        workspace=workspace,
        session_id=session_id,
        config_path=config_path,
        initial_task=initial_task,
    ).run()


__all__ = ["ChatInput", "InteractiveApp", "run_interactive"]
