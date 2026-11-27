"""全屏交互工作台（ADR-024）：AGY 风格工作台 + Deity 神之眼与可验证护栏。

布局层级：
1. 顶部单行上下文条（TopContextBar）：分支 · 模型 · 模式 · 运行态 · 上下文占比
2. 主工作区（#workspace-body）：纯净对话画布 + 右侧可呼出三标签辅助抽屉（AuxDrawer）
3. 底部一体式 Composer：FocusBar + 按需提示条 + ChatInput + 极简快捷键底条
4. 居中模态浮层：ApprovalModal / ModelPickerModal / CommandPaletteModal
"""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Markdown, Static

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.build_service import BuildService
from deity.application.config import KNOWN_MODELS
from deity.application.custom_commands import CustomCommandCatalog
from deity.application.plan_service import PlanService
from deity.application.rewind_service import RewindHistory
from deity.application.run_service import RunService
from deity.core.runtime import BudgetExceeded, ModelFailure, ProtocolError
from deity.interfaces.cli.chat_commands import ChatState, run_error_message
from deity.interfaces.cli.chat_status import ChatStatus, fmt_elapsed_compact
from deity.interfaces.cli.chat_view_model import ChatViewModel, ToolNode
from deity.interfaces.cli.plan import build_approved, plan_lines
from deity.interfaces.cli.workspace_diff import git_branch
from deity.interfaces.tui.approval_bridge import PendingPrompt, TuiApprovalBridge
from deity.interfaces.tui.commands import AVAILABLE_SLASH_COMMANDS, CommandDispatcher
from deity.interfaces.tui.modals import (
    ApprovalModal,
    CommandPaletteModal,
    ModelPickerModal,
)
from deity.interfaces.tui.panes import AuxDrawer
from deity.interfaces.tui.state import (
    WELCOME_EMBLEM,
    InputQueue,
    ScrollThrottler,
    ToolGroupState,
    greeting_for_hour,
)
from deity.interfaces.tui.theme import apply_deity_theme, palette
from deity.interfaces.tui.widgets import (
    ChatInput,
    DiffBlock,
    FocusBar,
    ThinkingBlock,
    ToolBlock,
    ToolGroupWidget,
    TopContextBar,
    WelcomeCard,
)

_WELCOME_EMBLEM = WELCOME_EMBLEM
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_WAITING_FRAMES = ("●○○", "○●○", "○○●")
_TIPS = (
    "Ctrl+P 命令面板 · Ctrl+B 辅助抽屉 · /help 命令",
    "Shift+Tab 切换模式 · @ 文件引用 · /compact 压缩上下文",
    "/model 切换模型 · /diff 查看改动 · Ctrl+C 取消/退出",
)


class InteractiveApp(App[None]):
    """AGY 风格全屏交互工作台：薄壳装配对话区、抽屉、Composer 与模态浮层。"""

    CSS_PATH: ClassVar[str] = "styles.tcss"
    ENABLE_COMMAND_PALETTE: ClassVar[bool] = False

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("shift+tab", "cycle_mode", "切换模式"),
        Binding("ctrl+b", "toggle_sidebar", "辅助抽屉"),
        Binding("ctrl+p", "open_command_palette", "命令面板", priority=True),
        Binding("ctrl+c", "cancel_run", "取消/退出"),
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        theme_name = apply_deity_theme(self)
        self._palette = palette(dark=theme_name == "deity-dark")
        self._clock = clock or datetime.now
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
        self._cancel_requested = False
        self._current_run_id: str | None = None
        self._last_run_id: str | None = None
        self._last_plan_id: str | None = None
        self._text_block: Markdown | None = None
        self._rendered_text_len = 0
        self._thinking_block: ThinkingBlock | None = None
        self._rendered_reasoning_len = 0
        self._tool_blocks: dict[str, ToolBlock] = {}
        self._tool_groups: dict[str, ToolGroupWidget] = {}
        self._active_tool_group: ToolGroupWidget | None = None
        self._group_counter = 0
        self._scroller = ScrollThrottler()
        self._input_queue = InputQueue()
        self.custom_commands = CustomCommandCatalog(workspace)
        self.rewind_history = RewindHistory(workspace)
        self._waiting_block: Static | None = None
        self._waiting_frame = 0
        self._spinner_frame = 0
        self._run_started_at: float | None = None
        self._tip_index = 0
        self._was_run_active = False
        self._active_modal: ApprovalModal | None = None
        self._prompt_handler: Any = None
        self._confirm_futures: list[asyncio.Future[bool]] = []
        self._commands = CommandDispatcher(self)
        self.title = "Deity"
        self.sub_title = str(workspace)
        bridge.on_prompt = lambda pending: self.call_from_thread(self._present_prompt, pending)
        bridge.on_prompt_done = lambda: self.call_from_thread(self._hide_prompt)

    # -- WorkbenchHost 属性与接口 -----------------------------------------
    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def run_active(self) -> bool:
        return self._run_active

    @property
    def last_run_id(self) -> str | None:
        return self._last_run_id

    @property
    def last_plan_id(self) -> str | None:
        return self._last_plan_id

    @property
    def last_view(self) -> ChatViewModel | None:
        return self._view

    def mode_label(self) -> str:
        return self._mode_label()

    def append_system(self, text: str) -> None:
        self._append_system(text)

    def append_error(self, text: str) -> None:
        self._append_error(text)

    def append_section_header(self, title: str, subtitle: str = "") -> None:
        self._append_section_header(title, subtitle)

    def mount_raw_diff(self, diff_text: str) -> None:
        self._conversation.mount(
            DiffBlock(None, self._palette, raw_text=diff_text, classes="msg-diff")
        )
        self._scroll_end()

    async def append_assistant_final(self, text: str) -> None:
        await self._append_assistant_final(text)

    async def clear_conversation(self, *, mount_welcome: bool) -> None:
        self._view = None
        self._active_tool_group = None
        self._tool_blocks.clear()
        self._tool_groups.clear()
        await self._conversation.remove_children()
        if mount_welcome:
            self._mount_welcome()

    def refresh_status(self) -> None:
        self._refresh_status()

    def enter_plan(self) -> bool:
        return self._enter_plan()

    def enter_normal(self) -> None:
        self._enter_normal()

    def toggle_auto(self) -> None:
        controller = self._bridge.controller
        if controller.mode == "auto":
            controller.exit_auto()
            self._append_system("已退出自动接受模式，恢复 ask。")
            self._refresh_status()
            return
        self.run_worker(self._auto_worker(), group="prompt", exclusive=False)

    def toggle_tool_node(self, call_id: str) -> None:
        self._toggle_tool_node(call_id)

    def apply_session_switch(self, session_id: str) -> None:
        self._apply_session_switch(session_id)

    async def refresh_drawer_sessions(self) -> None:
        try:
            drawer = self.query_one(AuxDrawer)
        except Exception:  # noqa: BLE001
            return
        if drawer.has_class("show"):
            await drawer.refresh_sessions(
                self.service.db_path, self.state.session_id, self.state.model_name
            )

    def toggle_drawer(self) -> None:
        self.action_toggle_sidebar()

    def switch_theme(self, arg: str) -> None:
        name = arg.strip().lower()
        if name == "dark":
            dark = True
        elif name == "light":
            dark = False
        elif name == "":
            dark = self.theme != "deity-dark"
        else:
            self._append_system("用法：/theme [dark|light]")
            return
        self.theme = "deity-dark" if dark else "deity-light"
        self._palette = palette(dark=dark)
        self._append_system(f"已切换到{'深色' if dark else '浅色'}主题。")
        self._refresh_status()

    def copy_text(self, text: str) -> None:
        self.copy_to_clipboard(text)

    def _focus_input_safe(self) -> None:
        if not self.is_running:
            return
        try:
            self.screen_stack[0].query_one("#input", ChatInput).focus()
        except Exception:  # noqa: BLE001, S110
            pass

    def open_model_picker(self) -> None:
        known = KNOWN_MODELS.get(self.service.provider_id or "", ())
        if not known:
            return

        def _on_pick(selected: str | None) -> None:
            if selected is not None:
                self.state.model_name = selected
                self._append_system(f"已切换模型：{selected}（仅本次会话内生效，下轮任务生效）。")
                self._refresh_status()
            self._focus_input_safe()

        self.push_screen(ModelPickerModal(known, self.state.model_name), _on_pick)

    def all_slash_commands(self) -> list[tuple[str, str]]:
        """内置命令与发现的自定义项目/用户命令合并列表（补全与命令面板共用）。"""
        combined = list(AVAILABLE_SLASH_COMMANDS)
        known = {cmd for cmd, _ in combined}
        for item in self.custom_commands.list_commands():
            if item.slash_name not in known:
                combined.append((item.slash_name, f"[{item.source}] {item.description}"))
        return combined

    def submit_expanded_task(self, prompt: str) -> None:
        """执行由自定义命令展开后的普通任务（若繁忙则自动入队）。"""
        if self._run_active:
            if self._input_queue.enqueue(prompt):
                self._append_system(f"已加入排队（第 {self._input_queue.size} 条）：{prompt[:48]}")
            self._refresh_status()
            return
        self._start_prompt_task(prompt)

    def start_review(self, prompt: str) -> None:
        self.run_worker(self._execute_task(prompt), group="run", exclusive=True)

    def start_run_task(self, coro: Any) -> None:
        async def _wrapped() -> None:
            self._run_active = True
            self._refresh_status()
            try:
                await coro
            finally:
                self._run_active = False
                self._refresh_status()

        self.run_worker(_wrapped(), group="run", exclusive=True)

    async def authorize_dirty(
        self, plan_id: str, allow_dirty: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        return await self._authorize_dirty(plan_id, allow_dirty)

    async def quit_app(self) -> None:
        await self.action_quit()

    # -- 布局与挂载 ---------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield TopContextBar()
        with Horizontal(id="workspace-body"):
            yield VerticalScroll(id="conversation")
            yield AuxDrawer()
        with Vertical(id="composer-shell"):
            yield FocusBar()
            yield Static("", id="cmd-hint")
            with Horizontal(id="input-row"):
                yield Static("❯", id="input-prompt")
                yield ChatInput(id="input")
            yield Static("", id="composer-footer")

    def on_mount(self) -> None:
        self.query_one("#input", ChatInput).focus()
        self._refresh_status()
        self.set_interval(0.5, self._refresh_status)
        self.set_interval(0.05, self._scroll_tick)
        self.set_interval(0.35, self._tick_waiting)
        self._mount_welcome()
        if self._initial_task:
            self._submit(self._initial_task)

    def on_unmount(self) -> None:
        self._cancel_requested = True
        self._input_queue.clear()
        self._bridge.abort()
        for future in self._confirm_futures:
            if not future.done():
                future.set_result(False)
        if self._run_active and self._current_run_id is not None:
            self.service.cancel(self._current_run_id)

    def _mount_welcome(self) -> None:
        card = WelcomeCard(
            hour=self._clock().hour,
            model_name=self.state.model_name,
            workspace=self._workspace,
        )
        self._conversation.mount(card)
        self._scroll_end(force=True)

    # -- 对话区呈现 ---------------------------------------------------------
    @property
    def _conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    def _scroll_end(self, *, force: bool = False) -> None:
        conversation = self._conversation
        if force:
            self._scroller.force()
            conversation.scroll_end(animate=False)
            return
        if conversation.max_scroll_y - conversation.scroll_y > conversation.size.height:
            return
        if self._scroller.request():
            conversation.scroll_end(animate=False)

    def _scroll_tick(self) -> None:
        if self._scroller.flush_due():
            conversation = self._conversation
            if conversation.max_scroll_y - conversation.scroll_y <= conversation.size.height:
                conversation.scroll_end(animate=False)

    def _append_system(self, text: str) -> None:
        self._conversation.mount(Static(text, classes="msg-system"))
        self._scroll_end()

    def _append_section_header(self, title: str, subtitle: str = "") -> None:
        line = Text()
        line.append(title, style="bold")
        if subtitle:
            line.append(f"  {subtitle}", style=self._palette["tertiary"])
        self._conversation.mount(Static(line, classes="section-header"))
        self._scroll_end()

    def _append_error(self, text: str) -> None:
        self._conversation.mount(Static(text, classes="msg-error"))
        self._scroll_end(force=True)

    def _append_user(self, text: str) -> None:
        line = Text()
        line.append("> ", style=f"bold {self._palette['accent']}")
        line.append(text)
        self._conversation.mount(Static(line, classes="msg-user"))
        self._scroll_end(force=True)

    async def _append_assistant_final(self, text: str) -> None:
        await self._conversation.mount(Markdown(text, classes="msg-assistant"))
        self._scroll_end(force=True)

    def _show_waiting(self) -> None:
        if self._waiting_block is not None:
            return
        self._waiting_frame = 0
        block = Static(f"{_WAITING_FRAMES[0]} Thinking...", id="waiting-row", classes="msg-waiting")
        self._waiting_block = block
        self._conversation.mount(block)
        self._scroll_end()

    def _tick_waiting(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        if self._waiting_block is not None:
            self._waiting_frame = (self._waiting_frame + 1) % len(_WAITING_FRAMES)
            self._waiting_block.update(f"{_WAITING_FRAMES[self._waiting_frame]} Thinking...")
        self._update_focus_bar()

    def _hide_waiting(self) -> None:
        if self._waiting_block is not None:
            self._waiting_block.remove()
            self._waiting_block = None

    def _append_metrics(self, view: ChatViewModel) -> None:
        status = view.status
        parts = [f"⚡ {status.elapsed_seconds:.1f}s"]
        if status.output_tokens is not None:
            parts.append(f"{status.output_tokens} tokens")
        parts.append(f"工具 {status.tool_calls} 次")
        self._conversation.mount(Static(" · ".join(parts), classes="msg-metrics"))
        self._scroll_end(force=True)

    # -- 输入与队列执行 -----------------------------------------------------
    def on_chat_input_command_hint_updated(self, message: ChatInput.CommandHintUpdated) -> None:
        try:
            hint_box = self.query_one("#cmd-hint", Static)
        except Exception:  # noqa: BLE001
            return
        if not message.matches or self._run_active:
            hint_box.remove_class("show")
            hint_box.update("")
            return
        header = "文件 (Tab 补全):" if message.kind == "mention" else "可用命令 (Tab 补全):"
        lines = [f"[bold cyan]{header}[/bold cyan]"]
        for cmd, desc in message.matches[:5]:
            lines.append(f"  [bold]{cmd}[/bold] - {desc}")
        if len(message.matches) > 5:
            lines.append(f"  [dim]...还有 {len(message.matches) - 5} 个[/dim]")
        hint_box.update("\n".join(lines))
        hint_box.add_class("show")

    def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        self._submit(message.text)

    def on_chat_input_cancel_requested(self, message: ChatInput.CancelRequested) -> None:
        if message.key == "escape":
            self._cancel_current_run()
        else:
            self.action_cancel_run()

    def on_chat_input_command_palette_requested(
        self, message: ChatInput.CommandPaletteRequested
    ) -> None:
        del message
        self.action_open_command_palette()

    def _submit(self, raw: str) -> None:
        try:
            hint_box = self.query_one("#cmd-hint", Static)
            hint_box.remove_class("show")
            hint_box.update("")
        except Exception:  # noqa: BLE001, S110
            pass
        text = raw.strip()
        if not text:
            return
        if text.startswith("/"):
            self.query_one("#input", ChatInput).text = ""
            self.run_worker(self._commands.dispatch(text), group="cmd", exclusive=False)
            return
        if text.startswith("!"):
            self._append_system("shell 直通（!命令）请在行式 CLI 中使用。")
            return
        if self._run_active:
            self.query_one("#input", ChatInput).text = ""
            if self._input_queue.enqueue(text):
                self._append_system(f"已加入排队（第 {self._input_queue.size} 条）：{text[:48]}")
            else:
                self._append_system(
                    f"排队已满（最多 {self._input_queue.max_size} 条），请等待当前任务完成。"
                )
            self._refresh_status()
            return
        self.query_one("#input", ChatInput).text = ""
        self._start_prompt_task(text)

    def _start_prompt_task(self, text: str) -> None:
        self._cancel_requested = False
        if self.state.plan_mode:
            self.run_worker(self._plan_flow(text), group="run", exclusive=True)
        else:
            self.run_worker(self._execute_task(text), group="run", exclusive=True)

    def _drain_next_queued(self) -> None:
        if self._cancel_requested or not self.is_running:
            return
        next_task = self._input_queue.pop_next()
        if next_task is not None:
            self._start_prompt_task(next_task)

    async def _session_message_count(self) -> int:
        repo = await SQLiteSessionRepository.open(self.service.db_path)
        try:
            return len(await repo.messages(self.state.session_id))
        finally:
            await repo.close()

    async def _execute_task(self, text: str) -> None:
        self._run_active = True
        self._run_started_at = time.monotonic()
        msg_count_before = await self._session_message_count()
        self._append_user(text)
        self._show_waiting()
        view = ChatViewModel()
        view.status.context_window_tokens = self.service.context_window_tokens
        self._view = view
        self._text_block = None
        self._rendered_text_len = 0
        self._thinking_block = None
        self._rendered_reasoning_len = 0
        self._active_tool_group = None
        self._refresh_status()
        completed_run_id: str | None = None
        try:
            stream = self.service.run_stream(
                text, model_name=self.state.model_name, session_id=self.state.session_id
            )
            async for event in stream:
                self._current_run_id = event.run_id
                completed_run_id = event.run_id
                delta = view.apply(event)
                if (
                    delta.reasoning_appended
                    or delta.text_appended
                    or delta.tool_started is not None
                ):
                    self._hide_waiting()
                if delta.reasoning_appended:
                    await self._sync_thinking_block(view)
                if delta.text_appended:
                    self._finish_thinking_block()
                    self._active_tool_group = None
                    await self._sync_text_block(view)
                if delta.tool_started is not None:
                    self._finish_thinking_block()
                    self._close_text_block(view)
                    await self._mount_tool_block(delta.tool_started)
                if delta.tool_completed is not None:
                    self._update_tool_block(delta.tool_completed)
                if delta.diff is not None:
                    self._mount_diff_block(delta.diff)
                if delta.terminal:
                    self._finish_thinking_block()
                    await self._finish_text_block(view)
                    self._append_metrics(view)
                self._refresh_status()
        except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
            self._append_error(f"运行失败：{run_error_message(exc)}")
        finally:
            self._hide_waiting()
            self._finish_thinking_block()
            if completed_run_id is not None:
                backups = self.service.take_turn_preimages(completed_run_id)
                self.rewind_history.record(
                    session_id=self.state.session_id,
                    run_id=completed_run_id,
                    prompt=text,
                    message_count_before=msg_count_before,
                    file_backups=backups,
                )
            if self._current_run_id is not None:
                self._last_run_id = self._current_run_id
            self._current_run_id = None
            self._active_tool_group = None
            self._accumulate(view)
            self._run_active = False
            self._run_started_at = None
            self._refresh_status()
            self._refresh_drawer_active_tab()
            self._drain_next_queued()

    async def _sync_thinking_block(self, view: ChatViewModel) -> None:
        segment = view.reasoning[self._rendered_reasoning_len :]
        if not segment:
            return
        if self._thinking_block is None:
            self._thinking_block = ThinkingBlock(self._palette)
            await self._conversation.mount(self._thinking_block)
        self._thinking_block.set_text(segment)
        self._scroll_end()

    def _finish_thinking_block(self) -> None:
        if self._thinking_block is not None and self._view is not None:
            self._thinking_block.finish(auto_collapse=True)
            self._rendered_reasoning_len = len(self._view.reasoning)
            self._thinking_block = None

    def _mount_diff_block(self, payload: dict[str, Any]) -> None:
        self._conversation.mount(DiffBlock(payload, self._palette, classes="msg-diff"))
        self._scroll_end()

    async def _sync_text_block(self, view: ChatViewModel) -> None:
        segment = view.text[self._rendered_text_len :]
        if self._text_block is None:
            if not segment:
                return
            self._text_block = Markdown("", classes="msg-assistant")
            await self._conversation.mount(self._text_block)
        await self._text_block.update(segment)
        self._scroll_end()

    def _close_text_block(self, view: ChatViewModel) -> None:
        if self._text_block is not None:
            self._rendered_text_len = len(view.text)
            self._text_block = None

    async def _finish_text_block(self, view: ChatViewModel) -> None:
        if self._text_block is not None:
            segment = view.text[self._rendered_text_len :]
            if segment:
                await self._text_block.update(segment)
            self._close_text_block(view)
        elif view.final_text:
            await self._append_assistant_final(view.final_text)

    async def _mount_tool_block(self, node: ToolNode) -> None:
        if self._active_tool_group is None:
            self._group_counter += 1
            group_state = ToolGroupState(
                group_id=f"grp-{self._group_counter}",
                title="工具执行",
            )
            group_widget = ToolGroupWidget(group_state, self._palette)
            await self._conversation.mount(group_widget)
            self._active_tool_group = group_widget
        block = ToolBlock(
            node,
            on_toggle=self._toggle_tool_node,
            formatter=self._tool_line,
            classes="msg-tool",
        )
        self._tool_blocks[node.call_id] = block
        self._tool_groups[node.call_id] = self._active_tool_group
        self._active_tool_group.add_tool_block(block)
        self._scroll_end()

    def _update_tool_block(self, node: ToolNode) -> None:
        block = self._tool_blocks.get(node.call_id)
        if block is not None:
            block.refresh_node()
        group = self._tool_groups.get(node.call_id)
        if group is not None:
            group.mark_completed(node.status or "ok")

    def _toggle_tool_node(self, call_id: str) -> None:
        if self._view is not None and call_id in self._view.tools:
            self._view.toggle_expanded(call_id)
            block = self._tool_blocks.get(call_id)
            if block is not None:
                block.refresh_node()

    def _tool_line(self, node: ToolNode) -> Text:
        colors = self._palette
        line = Text()
        line.append("⏺ ", style=f"bold {colors['accent']}")
        line.append(node.display_label(), style="bold")
        if node.finished_at is None:
            line.append(" · 运行中", style=colors["tertiary"])
            return line
        status = node.status or "完成"
        status_style = {
            "ok": colors["success"],
            "error": colors["error"],
            "denied": colors["warning"],
            "timeout": colors["warning"],
        }.get(status, colors["secondary"])
        line.append("\n")
        line.append("  ⎿ ", style=colors["tertiary"])
        line.append(status, style=status_style)
        line.append(f" · {node.elapsed(time.monotonic):.1f}s", style=colors["secondary"])
        if node.exit_code is not None and node.exit_code != 0:
            line.append(f" (exit {node.exit_code})", style=colors["error"])
        if node.output_preview:
            for preview_line in node.output_preview.splitlines():
                line.append(f"\n    {preview_line}", style=colors["tertiary"])
        if node.expanded:
            line.append(
                f"\n参数：{node.args_text or '—'}（{node.call_id}）", style=colors["secondary"]
            )
        return line

    def _accumulate(self, view: ChatViewModel) -> None:
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

    # -- 审批/确认浮层 ------------------------------------------------------
    def _present_prompt(self, pending: PendingPrompt) -> None:
        modal = ApprovalModal(pending, self._palette)
        self._active_modal = modal

        def _on_dismiss(choice: str | None) -> None:
            if self._active_modal is modal:
                self._active_modal = None
            fallback = "3" if pending.kind == "approval" else ""
            self._bridge.submit(fallback if choice is None else choice)
            self._refresh_status()
            self._focus_input_safe()

        self.push_screen(modal, _on_dismiss)
        self._refresh_status()

    def _hide_prompt(self) -> None:
        self._refresh_status()
        self._focus_input_safe()

    async def _confirm(
        self, text: str, *, confirm_label: str = "确认", cancel_label: str = "取消"
    ) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirm_futures.append(future)
        modal = ApprovalModal(
            PendingPrompt(kind="confirm", text=text),
            self._palette,
            confirm_label=confirm_label,
            cancel_label=cancel_label,
        )
        self._active_modal = modal

        def _on_dismiss(choice: str | None) -> None:
            if self._active_modal is modal:
                self._active_modal = None
            if not future.done():
                future.set_result(choice == "yes")

        self.push_screen(modal, _on_dismiss)
        try:
            return await future
        finally:
            if future in self._confirm_futures:
                self._confirm_futures.remove(future)
            self._focus_input_safe()

    # -- 辅助抽屉与面板交互 -------------------------------------------------
    def action_toggle_sidebar(self) -> None:
        drawer = self.query_one(AuxDrawer)
        if drawer.has_class("show"):
            drawer.remove_class("show")
            return
        if self.size.width < 100:
            self._append_system(
                "窗口太窄（需 ≥100 列），无法打开辅助抽屉；可用 /sessions 或 /diff 查看。"
            )
            return
        drawer.add_class("show")
        self._refresh_drawer_active_tab()

    def _refresh_drawer_active_tab(self) -> None:
        try:
            drawer = self.query_one(AuxDrawer)
        except Exception:  # noqa: BLE001
            return
        if not drawer.has_class("show"):
            return
        if drawer.active_tab == "sessions":
            self.run_worker(
                drawer.refresh_sessions(
                    self.service.db_path, self.state.session_id, self.state.model_name
                ),
                group="sidebar",
                exclusive=False,
            )
        elif drawer.active_tab == "files":
            self.run_worker(
                drawer.refresh_files(self._workspace, self._palette),
                group="sidebar",
                exclusive=False,
            )
        else:
            drawer.refresh_tasks(
                run_active=self._run_active,
                run_id=self._current_run_id or self._last_run_id,
                last_plan_id=self._last_plan_id,
                tools=self._view.tools if self._view is not None else {},
                queue_items=self._input_queue.items(),
                shell_sessions=self.service.active_shell_sessions(),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid is None:
            return
        drawer = self.query_one(AuxDrawer)
        if bid in {"tab-sessions", "tab-files", "tab-tasks"}:
            tab = bid.removeprefix("tab-")
            if tab in {"sessions", "files", "tasks"}:
                drawer.select_tab(tab)  # type: ignore[arg-type]
                self._refresh_drawer_active_tab()
            return
        if bid == "btn-sidebar-doctor":
            self.run_worker(self._commands.dispatch("/doctor"), group="cmd", exclusive=False)
            return
        session_id = drawer.session_for_button(bid)
        if session_id is not None:
            if self._run_active:
                self._append_system("有 Run 在运行，结束后再切换会话。")
                return
            self._apply_session_switch(session_id)
            self._refresh_drawer_active_tab()

    def _apply_session_switch(self, target: str) -> None:
        self.state.session_id = target
        self._append_system(f"已切换会话：{target[:8]}")
        self._refresh_status()

    def action_open_command_palette(self) -> None:
        def _on_select(selected: str | None) -> None:
            if selected is not None:
                self.run_worker(self._commands.dispatch(selected), group="cmd", exclusive=False)
            self._focus_input_safe()

        self.push_screen(CommandPaletteModal(self.all_slash_commands()), _on_select)

    # -- 取消与退出 ---------------------------------------------------------
    def action_cancel_run(self) -> None:
        if self._confirm_futures:
            for future in list(self._confirm_futures):
                if not future.done():
                    future.set_result(False)
            if self._active_modal is not None:
                self._active_modal.force_abort()
            return
        if self._cancel_current_run():
            return
        self._bridge.abort()
        self.exit()

    def _cancel_current_run(self) -> bool:
        acted = False
        self._cancel_requested = True
        dropped = self._input_queue.clear()
        if dropped > 0:
            self._append_system(f"已清空排队任务（{dropped} 条）。")
            acted = True
        if self._bridge.pending is not None:
            if self._active_modal is not None:
                self._active_modal.force_abort()
            self._bridge.submit("3")
            acted = True
        if self._run_active and self._current_run_id is not None:
            self.service.cancel(self._current_run_id)
            self._append_system("已请求取消当前 Run。")
            acted = True
        elif self._run_active:
            self.workers.cancel_group(self, "run")
            self._append_system("已请求取消当前 Run。")
            acted = True
        self._refresh_status()
        return acted

    async def action_quit(self) -> None:
        self._cancel_requested = True
        self._input_queue.clear()
        self._bridge.abort()
        if self._active_modal is not None:
            self._active_modal.force_abort()
        for future in self._confirm_futures:
            if not future.done():
                future.set_result(False)
        if self._run_active and self._current_run_id is not None:
            self.service.cancel(self._current_run_id)
        self.exit()

    # -- 模式与状态更新 -----------------------------------------------------
    def _mode_label(self) -> str:
        if self._bridge.controller.mode == "auto":
            return "自动接受"
        if self.state.plan_mode:
            return "计划"
        return "普通"

    def _enter_plan(self) -> bool:
        if not self.service.coding.plan_build_enabled or not self.service.coding.git_guard_enabled:
            self._append_system(
                "Plan/Build 未启用，请在配置中开启 plan_build_enabled 和 git_guard_enabled。"
            )
            return False
        self.state.plan_mode = True
        self._append_system("已进入计划模式。输入任务后将展示计划并询问是否构建。")
        self._refresh_status()
        return True

    def _enter_normal(self) -> None:
        self.state.plan_mode = False
        if self._bridge.controller.mode == "auto":
            self._bridge.controller.exit_auto()
            self._append_system("已退出自动接受模式，恢复 ask。")
        self._append_system("已返回普通模式。")
        self._refresh_status()

    def on_chat_input_mode_cycle_requested(self, message: ChatInput.ModeCycleRequested) -> None:
        del message
        self.action_cycle_mode()

    def action_cycle_mode(self) -> None:
        if self._bridge.pending is not None or self._confirm_futures:
            self._append_system("有待决审批或确认，先处理后再切换模式。")
            return
        label = self._mode_label()
        if label == "自动接受":
            self._enter_normal()
        elif label == "计划":
            self.run_worker(self._auto_worker(came_from_plan=True), group="prompt", exclusive=False)
        else:
            self._enter_plan()

    async def _auto_worker(self, *, came_from_plan: bool = False) -> None:
        accepted = await asyncio.to_thread(self._bridge.request_auto)
        if accepted:
            if came_from_plan:
                self.state.plan_mode = False
            self._append_system("已进入自动接受模式。输入 /normal 退出并恢复 ask。")
        self._refresh_status()

    def _update_focus_bar(self) -> None:
        try:
            focus_bar = self.query_one(FocusBar)
        except Exception:  # noqa: BLE001
            return
        if self._bridge.pending is not None or self._confirm_futures:
            status_text = "⠏ 等待审批…"
        elif self._run_active:
            elapsed = (
                int(time.monotonic() - self._run_started_at)
                if self._run_started_at is not None
                else 0
            )
            current_tool = self._view.current_tool if self._view is not None else None
            tool_hint = f" · {current_tool.display_label()}" if current_tool is not None else ""
            status_text = (
                f"{_SPINNER_FRAMES[self._spinner_frame]} 生成中{tool_hint}"
                f" ({fmt_elapsed_compact(elapsed)} · 可继续输入排队 · Ctrl+C 取消)"
            )
        else:
            status_text = _TIPS[self._tip_index % len(_TIPS)]
        focus_bar.update_focus(status_text=status_text, queue_size=self._input_queue.size)

    def _refresh_status(self) -> None:
        try:
            top_bar = self.query_one(TopContextBar)
            footer = self.query_one("#composer-footer", Static)
            input_row = self.query_one("#input-row")
        except Exception:  # noqa: BLE001
            return
        if self._was_run_active and not self._run_active:
            self._tip_index += 1
        self._was_run_active = self._run_active
        mode = self._mode_label()
        top_bar.update_state(
            model_name=self.state.model_name,
            mode_label=mode,
            branch=git_branch(self._workspace),
            run_active=self._run_active,
            context_ratio=self.session_status.context_ratio(),
            tokens_text=self.session_status.token_text(),
            colors=self._palette,
            width=self.size.width,
        )
        input_row.set_class(self._run_active, "streaming")
        self._update_focus_bar()
        if self._bridge.pending is not None or self._confirm_futures:
            footer_text = "审批待决 · 请在弹窗中选择或按快捷键"
        elif self._run_active:
            footer_text = f"{mode}模式 · 运行中（输入可排队） · Ctrl+C 取消"
        else:
            footer_text = (
                f"{mode}模式 · Shift+Tab 切换 · Enter 发送 · Ctrl+P 命令面板 · Ctrl+B 抽屉"
            )
        footer.update(footer_text)

    # -- 计划模式 -----------------------------------------------------------
    async def _plan_flow(self, text: str) -> None:
        self._run_active = True
        self._run_started_at = time.monotonic()
        self._append_user(text)
        self._show_waiting()
        self._refresh_status()
        try:
            artifact = await PlanService(self.service).create(
                text, model_name=self.state.model_name, session_id=self.state.session_id
            )
            self._hide_waiting()
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
            self._append_error(f"运行失败：{run_error_message(exc)}")
        finally:
            self._hide_waiting()
            self._run_active = False
            self._run_started_at = None
            self._refresh_status()
            self._drain_next_queued()

    async def _authorize_dirty(
        self, plan_id: str, allow_dirty: tuple[str, ...]
    ) -> tuple[str, ...] | None:
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


__all__ = [
    "AVAILABLE_SLASH_COMMANDS",
    "_WELCOME_EMBLEM",
    "ChatInput",
    "DiffBlock",
    "InteractiveApp",
    "ScrollThrottler",
    "ToolBlock",
    "greeting_for_hour",
    "run_interactive",
]
