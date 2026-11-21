"""全屏交互工作台（ADR-022）：底部输入、流式对话、界面内审批、状态栏。

定位：TTY 下的默认交互界面（无子命令的 `deity`、`deity run` 不带任务、
`deity tui` 无 --run-id 时进入）。与行式 REPL 共用同一条事件通路：
本进程内驱动 RunService.run_stream()，事件喂入既有 ChatViewModel
折叠成状态树，对话区按 ViewDelta 增量建块；审批经 TuiApprovalBridge
复用既有 ApprovalController（语义与 CLI 卡片完全一致），本模块只做
呈现与回填。监视既有 Run 是 interfaces/tui/app.py 的 TuiApp 的活。

按键：Enter 提交、Ctrl+J/Shift+Enter 换行、首/末行 ↑/↓ 走输入历史、
Shift+Tab 循环切换模式（普通 → 计划 → 自动接受）、Ctrl+C 或 Esc
取消当前 Run、Ctrl+Q 退出。

观感与交互对齐 Claude Code（ADR-022 修订，2026-11-17）：启动欢迎头
（神之眼徽记 + 版本/模型/工作区，边框块）、带 ❯ 提示的边框输入框、
输入框正下方的模式状态行、一行式工具条目 + 缩进结果行。

Klee 风格对齐（ADR-022 修订，2026-11-21）：自定义深浅两套主题
（theme.py，品牌色取 Klee 深绿 #1E4C35、四级文字色层级）、边框
圆角化、消息分级配色、Markdown 克制化样式、间距节奏统一。
"""

import asyncio
import shlex
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
from textual.message import Message
from textual.widgets import Button, Footer, Header, Markdown, Static, TextArea

from deity import __version__
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
from deity.interfaces.tui.theme import apply_deity_theme, palette

_CLI_ONLY_COMMANDS = ("/fork", "/timeline")

# 欢迎头徽记：神之眼（三角形 + 眼睛 + 光芒），字符绘制、对称极简。
_WELCOME_EMBLEM = "   \\ | /\n    \\|/\n    / \\\n   / ◉ \\\n  /______\\"

AVAILABLE_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "查看命令帮助与工作台按键"),
    ("/plan", "进入计划模式（建计划 → 审批 → 构建）"),
    ("/build", "执行已批准的计划"),
    ("/normal", "返回普通交互模式"),
    ("/auto", "进入自动接受模式（免审批）"),
    ("/model", "查看或切换当前模型"),
    ("/status", "查看会话状态与 Token 统计"),
    ("/cost", "查看当前会话 Token 消耗详情"),
    ("/tools", "展开或切换工具调用详情"),
    ("/skills", "查看已配置与已启用的 Skills"),
    ("/mcp", "查看已配置的 MCP 工具服务器"),
    ("/sessions", "列出历史会话或切换会话"),
    ("/resume", "从最近检查点恢复 Run"),
    ("/doctor", "运行环境与配置检查"),
    ("/clear", "清屏对话区"),
    ("/compact", "查看上下文压缩配置状态"),
    ("/exit", "退出 Deity 工作台"),
]

# 输入区下方免责行（Klee 风格对齐 P1-5）：deity 自己的文案。
_DISCLAIMER = "Deity 是 AI，可能会出错；它会修改工作区文件，重要操作会先征求审批，请核对结果。"

# 等待指示（P1-2）的三点动画帧：被激活的点依次轮动。
_WAITING_FRAMES = ("●○○", "○●○", "○○●")


def greeting_for_hour(hour: int) -> str:
    """按小时给时间问候语（P1-6）：5–12 早上好、12–17 下午好、17–22 晚上好、其余夜深了。"""
    if 5 <= hour < 12:
        return "早上好"
    if 12 <= hour < 17:
        return "下午好"
    if 17 <= hour < 22:
        return "晚上好"
    return "夜深了"


class ScrollThrottler:
    """流式滚动节流（P1-7）：间隔内的请求合并，到期由 flush_due 补滚。

    纯逻辑、无 Textual 依赖（可单测）：request() 返回 True 表示应
    立即滚动；返回 False 表示本次被合并、置 pending。调用方周期性
    查 flush_due()，为 True 时执行一次尾随滚动并清 pending。
    """

    def __init__(self, interval: float = 0.15, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._last: float | None = None
        self._pending = False

    def request(self) -> bool:
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            self._last = now
            self._pending = False
            return True
        self._pending = True
        return False

    def flush_due(self) -> bool:
        if (
            self._pending
            and self._last is not None
            and self._clock() - self._last >= self._interval
        ):
            self._last = self._clock()
            self._pending = False
            return True
        return False

    def force(self) -> None:
        """终态强制滚动前调用：清 pending 并重置节流窗口。"""
        self._last = self._clock()
        self._pending = False


class ChatInput(TextArea):
    """工作台输入框：Enter 提交、Ctrl+J 换行、Tab 命令补全、首/末行方向键走历史。"""

    class Submitted(Message):
        """用户提交了一段输入（Enter）；文本是否采纳由 App 决定。"""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CancelRequested(Message):
        """输入框内按 Ctrl+C / Esc：请求取消当前 Run。"""

    class ModeCycleRequested(Message):
        """输入框内按 Shift+Tab：请求循环切换模式（普通/计划/自动接受）。"""

    class CommandHintUpdated(Message):
        """输入框内的斜杠命令联想更新。"""

        def __init__(self, matches: list[tuple[str, str]]) -> None:
            super().__init__()
            self.matches = matches

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and not text.startswith("//"):
            prefix = text.split()[0] if text else ""
            matches = [
                (cmd, desc) for cmd, desc in AVAILABLE_SLASH_COMMANDS if cmd.startswith(prefix)
            ]
            self.post_message(self.CommandHintUpdated(matches))
        else:
            self.post_message(self.CommandHintUpdated([]))

    async def _on_key(self, event: Any) -> None:
        if (
            self.app is not None
            and getattr(self.app, "_prompt_handler", None) is not None
            and hasattr(self.app, "handle_prompt_key")
            and self.app.handle_prompt_key(event.key)
        ):
            event.prevent_default()
            event.stop()
            return

        if event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            self.post_message(self.ModeCycleRequested())
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            if text.strip():
                self._push_history(text)
            self.post_message(self.Submitted(text))
            return
        if event.key == "tab":
            text = self.text
            if text.startswith("/"):
                prefix = text.split()[0] if text else ""
                matches = [cmd for cmd, _ in AVAILABLE_SLASH_COMMANDS if cmd.startswith(prefix)]
                if matches:
                    event.prevent_default()
                    event.stop()
                    if len(matches) == 1:
                        self.text = matches[0] + " "
                        self.move_cursor((0, len(self.text)))
                        self.post_message(self.CommandHintUpdated([]))
                    else:
                        common = matches[0]
                        for m in matches[1:]:
                            while not m.startswith(common):
                                common = common[:-1]
                        if len(common) > len(prefix):
                            self.text = common
                            self.move_cursor((0, len(self.text)))
                        self.post_message(
                            self.CommandHintUpdated(
                                [
                                    (cmd, desc)
                                    for cmd, desc in AVAILABLE_SLASH_COMMANDS
                                    if cmd in matches
                                ]
                            )
                        )
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


class ToolBlock(Static):
    """可交互工具条目：支持点击切换展开/折叠完整参数与输出。"""

    def __init__(
        self,
        node: ToolNode,
        on_toggle: Callable[[str], None],
        formatter: Callable[[ToolNode], str | Text],
        **kwargs: Any,
    ) -> None:
        super().__init__(formatter(node), **kwargs)
        self.node = node
        self._on_toggle = on_toggle
        self._formatter = formatter

    def on_click(self) -> None:
        self._on_toggle(self.node.call_id)

    def refresh_node(self) -> None:
        self.update(self._formatter(self.node))


class InteractiveApp(App[None]):
    """Codex / Claude Code 式交互界面：对话区 + 审批面板 + 状态栏 + 输入框。"""

    CSS: ClassVar[str] = """
    #main-row { height: 1fr; }
    #sidebar { width: 36; display: none; border: round $primary; padding: 0 1; }
    #sidebar.show { display: block; }
    #sidebar-title { text-style: bold; color: $accent; }
    #sidebar-sessions { height: 1fr; }
    .session-row { width: 1fr; }
    .session-row:hover { background: $hover-bg; }
    .session-row.current { color: $accent; }
    #sidebar-model { color: $text-tertiary; }
    #conversation { height: 1fr; padding: 0 1; }
    #welcome-header { border: round $primary; padding: 0 1; height: auto; margin-bottom: 1; }
    #welcome-emblem { width: auto; padding-right: 2; color: $accent; text-style: bold; }
    #welcome-text { width: 1fr; }
    #prompt-panel { display: none; border: round $warning; padding: 0 1; }
    #prompt-panel.show { display: block; }
    #prompt-scroll { max-height: 14; }
    #prompt-buttons Button:hover { background: $hover-bg; }
    #cmd-hint { display: none; height: auto; max-height: 5; background: $panel; border: round $primary; padding: 0 1; margin-bottom: 1; }
    #cmd-hint.show { display: block; }
    #input-row { min-height: 3; max-height: 8; height: auto; border: round $primary; }
    #input-prompt { width: 2; padding: 1 0 0 1; color: $accent; text-style: bold; }
    #input { border: none; min-height: 1; max-height: 6; height: auto; width: 1fr; }
    #status-bar { height: 1; background: $panel; padding: 0 1; }
    .msg-user { margin-top: 1; text-style: bold; background: $user-bg; padding: 0 1; }
    .msg-system { color: $text-tertiary; }
    .msg-error { color: $error; border: round $error; padding: 0 1; margin: 1 0; }
    .msg-waiting { color: $text-secondary; padding-left: 1; }
    .msg-metrics { color: $text-quaternary; }
    .msg-tool { padding-left: 1; }
    .msg-assistant { margin-bottom: 1; }
    #input-row.streaming { border: round $text-tertiary; }
    #input-hint { height: 1; padding: 0 1; color: $text-tertiary; }
    #disclaimer { height: 1; padding: 0 1; color: $text-quaternary; }
    .msg-tool:hover { background: $boost; }
    MarkdownHeader { margin: 1 0 0 0; text-style: bold; }
    MarkdownFence { background: $code-bg; border-left: solid $accent; margin: 1 0; }
    MarkdownBlockQuote { color: $text-secondary; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("shift+tab", "cycle_mode", "切换模式"),
        Binding("ctrl+b", "toggle_sidebar", "会话侧栏"),
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
        self._current_run_id: str | None = None
        self._last_run_id: str | None = None
        self._last_plan_id: str | None = None
        self._text_block: Markdown | None = None
        self._rendered_text_len = 0
        self._tool_blocks: dict[str, Static] = {}
        self._sidebar_map: dict[str, str] = {}
        self._scroller = ScrollThrottler()
        self._waiting_block: Static | None = None
        self._waiting_frame = 0
        self._prompt_handler: Any = None
        self._confirm_futures: list[asyncio.Future[bool]] = []
        self.title = "Deity"
        self.sub_title = str(workspace)
        bridge.on_prompt = lambda pending: self.call_from_thread(self._present_prompt, pending)
        bridge.on_prompt_done = lambda: self.call_from_thread(self._hide_prompt)

    # -- 布局 -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-row"):
            with Vertical(id="sidebar"):
                yield Static("会话", id="sidebar-title")
                yield VerticalScroll(id="sidebar-sessions")
                yield Static("", id="sidebar-model")
                yield Button("运行 doctor", id="btn-sidebar-doctor")
            yield VerticalScroll(id="conversation")
        with Vertical(id="prompt-panel"):
            with VerticalScroll(id="prompt-scroll"):
                yield Static("", id="prompt-text")
            with Horizontal(id="prompt-buttons"):
                yield Button("[1] 允许一次", id="btn-allow-once", variant="success")
                yield Button("[2] 本次会话都允许", id="btn-allow-session")
                yield Button("[4] 永久允许（保存为规则）", id="btn-allow-always")
                yield Button("[3/Esc] 拒绝", id="btn-deny", variant="error")
                yield Button("[y/Enter] 确认", id="btn-confirm", variant="success")
                yield Button("[n/Esc] 取消", id="btn-cancel")
        yield Static("", id="cmd-hint")
        with Horizontal(id="input-row"):
            yield Static("❯", id="input-prompt")
            yield ChatInput(id="input")
        yield Static("", id="input-hint")
        yield Static(_DISCLAIMER, id="disclaimer")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#input", ChatInput).focus()
        self._hide_prompt()
        self._refresh_status()
        self.set_interval(0.5, self._refresh_status)
        self.set_interval(0.05, self._scroll_tick)
        self.set_interval(0.35, self._tick_waiting)
        self._mount_welcome()
        if self._initial_task:
            self._submit(self._initial_task)

    def _mount_welcome(self) -> None:
        """欢迎头（ADR-022 修订）：神之眼徽记与版本/模型/工作区并排，边框块。

        取代原先单独的一行系统欢迎消息，两者合并为这一块，不重复欢迎。
        版本号取包的 __version__（与 pyproject 同步），不硬编码。
        """
        greeting = greeting_for_hour(self._clock().hour)
        text = (
            f"{greeting}，欢迎来到 Deity v{__version__}，我是你的编程助手。\n"
            f"模型：{self.state.model_name}\n"
            f"工作区：{self._workspace}\n"
            "今天想让我帮你做什么？\n"
            "Enter 提交任务 · /help 查看命令\n"
            "Shift+Tab 切换模式 · Ctrl+C 取消 · Ctrl+Q 退出"
        )
        header = Horizontal(
            Static(_WELCOME_EMBLEM, id="welcome-emblem"),
            Static(text, id="welcome-text"),
            id="welcome-header",
        )
        self._conversation.mount(header)
        self._scroll_end(force=True)

    # -- 对话区 -----------------------------------------------------------
    @property
    def _conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    def _scroll_end(self, *, force: bool = False) -> None:
        """滚动跟随（P1-7 节流版）：流式期合并高频请求，终态强制到底。

        用户手动上滚超过一屏时不抢位置（等其滚回底部附近再恢复
        跟随）；force 用于终态/用户刚提交等必须到底的时点。
        """
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
        """节流尾随滚动：被合并的请求到期后补滚一次（仍尊重上滚）。"""
        if self._scroller.flush_due():
            conversation = self._conversation
            if conversation.max_scroll_y - conversation.scroll_y <= conversation.size.height:
                conversation.scroll_end(animate=False)

    def _append_system(self, text: str) -> None:
        self._conversation.mount(Static(text, classes="msg-system"))
        self._scroll_end()

    def _append_error(self, text: str) -> None:
        """错误块（P1-3）：error 色圆角边框独立成级，不混系统消息。"""
        self._conversation.mount(Static(text, classes="msg-error"))
        self._scroll_end(force=True)

    def _append_user(self, text: str) -> None:
        self._conversation.mount(Static(f"❯ {text}", classes="msg-user"))
        self._scroll_end(force=True)

    async def _append_assistant_final(self, text: str) -> None:
        block = Markdown(text, classes="msg-assistant")
        await self._conversation.mount(block)
        self._scroll_end(force=True)

    # -- 等待指示（P1-2） ---------------------------------------------------
    def _show_waiting(self) -> None:
        """插入等待行：三点轮动 + Thinking...，首个增量到达即撤下。

        注意：事件流没有真实 thinking 内容（P1-0 核查结论），这行
        只是等待态呈现，不冒充思考原文，与 Klee 的等待态药丸对应。
        """
        if self._waiting_block is not None:
            return
        self._waiting_frame = 0
        block = Static(f"{_WAITING_FRAMES[0]} Thinking...", id="waiting-row", classes="msg-waiting")
        self._waiting_block = block
        self._conversation.mount(block)
        self._scroll_end()

    def _tick_waiting(self) -> None:
        if self._waiting_block is None:
            return
        self._waiting_frame = (self._waiting_frame + 1) % len(_WAITING_FRAMES)
        self._waiting_block.update(f"{_WAITING_FRAMES[self._waiting_frame]} Thinking...")

    def _hide_waiting(self) -> None:
        if self._waiting_block is not None:
            self._waiting_block.remove()
            self._waiting_block = None

    def _append_metrics(self, view: ChatViewModel) -> None:
        """每轮性能指标行（P1-4）：⚡ 耗时 · 输出 tokens · 工具次数。

        数据取本轮 ViewModel 的状态聚合；usage 缺失时省略 tokens
        段（降级格式，不编造），与 Klee 的两档格式对应。
        """
        status = view.status
        parts = [f"⚡ {status.elapsed_seconds:.1f}s"]
        if status.output_tokens is not None:
            parts.append(f"{status.output_tokens} tokens")
        parts.append(f"工具 {status.tool_calls} 次")
        self._conversation.mount(Static(" · ".join(parts), classes="msg-metrics"))
        self._scroll_end(force=True)

    # -- 提交与执行 ---------------------------------------------------------
    def on_chat_input_command_hint_updated(self, message: ChatInput.CommandHintUpdated) -> None:
        try:
            hint_box = self.query_one("#cmd-hint", Static)
        except Exception:  # noqa: BLE001 - 未挂载时刷新是空操作
            return
        if not message.matches or self._run_active:
            hint_box.remove_class("show")
            hint_box.update("")
            return
        lines = ["[bold cyan]可用命令 (Tab 补全):[/bold cyan]"]
        for cmd, desc in message.matches[:5]:
            lines.append(f"  [bold]{cmd}[/bold] - {desc}")
        if len(message.matches) > 5:
            lines.append(f"  [dim]...还有 {len(message.matches) - 5} 个命令[/dim]")
        hint_box.update("\n".join(lines))
        hint_box.add_class("show")

    def on_chat_input_submitted(self, message: ChatInput.Submitted) -> None:
        self._submit(message.text)

    def on_chat_input_cancel_requested(self, message: ChatInput.CancelRequested) -> None:
        self.action_cancel_run()

    def _submit(self, raw: str) -> None:
        try:
            hint_box = self.query_one("#cmd-hint", Static)
            hint_box.remove_class("show")
            hint_box.update("")
        except Exception:  # noqa: BLE001, S110 - 未挂载时是空操作
            pass
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
        self._show_waiting()
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
                if delta.text_appended or delta.tool_started is not None:
                    self._hide_waiting()
                if delta.text_appended:
                    await self._sync_text_block(view)
                if delta.tool_started is not None:
                    self._close_text_block(view)
                    self._mount_tool_block(delta.tool_started)
                if delta.tool_completed is not None:
                    self._update_tool_block(delta.tool_completed)
                if delta.terminal:
                    await self._finish_text_block(view)
                    self._append_metrics(view)
                self._refresh_status()
        except (ModelFailure, ProtocolError, BudgetExceeded, ValueError, OSError) as exc:
            self._append_error(f"运行失败：{run_error_message(exc)}")
        finally:
            self._hide_waiting()
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
        self._scroll_end()

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
        block = ToolBlock(
            node,
            on_toggle=self._toggle_tool_node,
            formatter=self._tool_line,
            classes="msg-tool",
        )
        self._tool_blocks[node.call_id] = block
        self._conversation.mount(block)
        self._scroll_end()

    def _update_tool_block(self, node: ToolNode) -> None:
        block = self._tool_blocks.get(node.call_id)
        if block is not None:
            if isinstance(block, ToolBlock):
                block.refresh_node()
            else:
                block.update(self._tool_line(node))

    def _toggle_tool_node(self, call_id: str) -> None:
        if self._view is not None and call_id in self._view.tools:
            self._view.toggle_expanded(call_id)
            block = self._tool_blocks.get(call_id)
            if block is not None:
                if isinstance(block, ToolBlock):
                    block.refresh_node()
                else:
                    block.update(self._tool_line(self._view.tools[call_id]))

    def _tool_line(self, node: ToolNode) -> Text:
        """工具条目（ADR-022 修订，Claude Code 式紧凑排版；P0 分级配色）。

        首行一行式：⏺ 工具名(参数摘要)；完成后结果以 ⎿ 缩进次行呈现
        （状态 + 耗时）；运行中首行尾标「运行中」。展开时（/tools）在
        结果行后追加完整参数与 call_id。配色走 theme.palette()：
        ⏺ 与工具名用品牌绿，⎿ 行用次级色，状态词按结果着色
        （ok=success、error=error、denied/timeout=warning）。
        """
        colors = self._palette
        summary = node.arguments_summary() or "—"
        line = Text()
        line.append("⏺ ", style=f"bold {colors['accent']}")
        line.append(node.name, style="bold")
        line.append(f"({summary})")
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
        if node.expanded:
            line.append(
                f"\n参数：{node.args_text or '—'}（{node.call_id}）", style=colors["secondary"]
            )
        return line

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
    def handle_prompt_key(self, key: str) -> bool:
        """极速单键审批处理：面板激活时直达放行/拒绝。"""
        if self._prompt_handler is None:
            return False
        panel = self.query_one("#prompt-panel")
        if "show" not in panel.classes:
            return False
        kind = getattr(self, "_current_pending_kind", "approval")
        if kind == "approval":
            mapping = {
                "1": "btn-allow-once",
                "2": "btn-allow-session",
                "4": "btn-allow-always",
                "3": "btn-deny",
                "escape": "btn-deny",
            }
            if key in mapping:
                bid = mapping[key]
                handler = self._prompt_handler
                self._prompt_handler = None
                handler(bid)
                return True
        elif kind == "confirm":
            mapping = {
                "y": "btn-confirm",
                "Y": "btn-confirm",
                "enter": "btn-confirm",
                "n": "btn-cancel",
                "N": "btn-cancel",
                "escape": "btn-cancel",
            }
            if key in mapping:
                bid = mapping[key]
                handler = self._prompt_handler
                self._prompt_handler = None
                handler(bid)
                return True
        return False

    def on_key(self, event: Any) -> None:
        if self.handle_prompt_key(event.key):
            event.prevent_default()
            event.stop()

    def _present_prompt(self, pending: PendingPrompt) -> None:
        """呈现审批卡片或确认（桥从工作线程经 call_from_thread 调入）。"""
        self.query_one("#prompt-text", Static).update(pending.text or "（无内容）")
        self._current_pending_kind = pending.kind
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
            self.query_one("#btn-allow-once", Button).label = "[1] 允许一次"
            session_button = self.query_one("#btn-allow-session", Button)
            if pending.pattern:
                session_button.label = f"[2] 本次会话允许同类命令（{pending.pattern}）"
            else:
                session_button.label = "[2] 本次会话都允许"
            self.query_one("#btn-allow-always", Button).label = "[4] 永久允许（保存为规则）"
            self.query_one("#btn-deny", Button).label = "[3/Esc] 拒绝"
            self._set_prompt_buttons(visible)
            self._prompt_handler = lambda bid: self._bridge.submit(mapping.get(bid, "3"))
            focus_id = "btn-allow-once"
        else:
            mapping = {"btn-confirm": "yes", "btn-cancel": ""}
            self.query_one("#btn-confirm", Button).label = "[y/Enter] 确认"
            self.query_one("#btn-cancel", Button).label = "[n/Esc] 取消"
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
        bid = event.button.id
        # 侧栏按钮优先于审批 handler 路由：侧栏开着时误触不能当成审批答案。
        if bid is not None and bid in self._sidebar_map:
            self._sidebar_click(self._sidebar_map[bid])
            return
        if bid == "btn-sidebar-doctor":
            self.run_worker(self._dispatch_command("/doctor"), group="command", exclusive=False)
            return
        handler = self._prompt_handler
        if handler is not None and bid is not None:
            handler(bid)

    # -- 会话侧栏（P2-2 / D3：默认隐藏、Ctrl+B 呼出） ------------------------
    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        if sidebar.has_class("show"):
            sidebar.remove_class("show")
            return
        if self.size.width < 100:
            self._append_system(
                "窗口太窄（需 ≥100 列），无法打开会话侧栏；可用 /sessions 查看与切换会话。"
            )
            return
        sidebar.add_class("show")
        self.run_worker(self._fill_sidebar(), group="sidebar", exclusive=False)

    async def _fill_sidebar(self) -> None:
        container = self.query_one("#sidebar-sessions", VerticalScroll)
        await container.remove_children()
        self._sidebar_map = {}
        repo = await SQLiteSessionRepository.open(self.service.db_path)
        try:
            items = await repo.sessions(include_archived=True)
        finally:
            await repo.close()
        if not items:
            container.mount(Static("（暂无已存储会话）直接输入任务即可开始新会话。"))
        for index, item in enumerate(items):
            current = item["id"] == self.state.session_id
            title = f" {item['title'][:16]}" if item["title"] else ""
            button = Button(
                f"{'● ' if current else ''}{item['id'][:8]}{title}",
                id=f"session-row-{index}",
                classes="session-row current" if current else "session-row",
            )
            self._sidebar_map[f"session-row-{index}"] = item["id"]
            container.mount(button)
        self.query_one("#sidebar-model", Static).update(f"模型：{self.state.model_name}")

    def _sidebar_click(self, target: str) -> None:
        if self._run_active:
            self._append_system("有 Run 在运行，结束后再切换会话。")
            return
        self._apply_session_switch(target)
        self.run_worker(self._fill_sidebar(), group="sidebar", exclusive=False)

    def _apply_session_switch(self, target: str) -> None:
        self.state.session_id = target
        self._append_system(f"已切换会话：{target[:8]}")
        self._refresh_status()

    async def _confirm(
        self, text: str, *, confirm_label: str = "确认", cancel_label: str = "取消"
    ) -> bool:
        """界面内二选一确认（计划构建、脏文件授权用）；退出时按取消收口。"""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._confirm_futures.append(future)
        self._current_pending_kind = "confirm"
        self.query_one("#prompt-text", Static).update(text)
        self.query_one("#btn-confirm", Button).label = f"[y/Enter] {confirm_label}"
        self.query_one("#btn-cancel", Button).label = f"[n/Esc] {cancel_label}"
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

    # -- 模式与状态栏 ---------------------------------------------------------
    def _mode_label(self) -> str:
        """当前模式文案（状态栏、/status、Shift+Tab 循环共用同一口径）。"""
        if self._bridge.controller.mode == "auto":
            return "自动接受"
        if self.state.plan_mode:
            return "计划"
        return "普通"

    def _enter_plan(self) -> bool:
        """进入计划模式（/plan 与 Shift+Tab 共用）：未启用时提示并保持原模式。"""
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
        """返回普通模式（/normal 与 Shift+Tab 共用）：退出计划与自动接受。"""
        self.state.plan_mode = False
        if self._bridge.controller.mode == "auto":
            self._bridge.controller.exit_auto()
            self._append_system("已退出自动接受模式，恢复 ask。")
        self._append_system("已返回普通模式。")
        self._refresh_status()

    def on_chat_input_mode_cycle_requested(self, message: ChatInput.ModeCycleRequested) -> None:
        self.action_cycle_mode()

    def action_cycle_mode(self) -> None:
        """Shift+Tab 循环切换模式：普通 → 计划 → 自动接受 → 普通。

        状态转移与 /plan、/auto、/normal 完全共用，不另起语义：
        进入计划走同一启用检查（未启用则留在普通并提示）；进入自动
        接受仍走审批桥的显式确认——确认通过后才离开计划模式，取消
        确认则留在计划模式，绝不静默绕过自动接受的安全确认；有待
        决审批/确认时不切换，避免与审批面板争用。
        """
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

    def _hint_text(self) -> str:
        """输入区状态提示（P1-5）：随待审批/运行/模式三类状态切换文案。"""
        if self._bridge.pending is not None or self._confirm_futures:
            return "有待决审批 · 请先在上方面板中处理"
        if self._run_active:
            return "生成中 · Ctrl+C 停止"
        mode = self._mode_label()
        if mode == "计划":
            return "计划模式 · 输入任务将先生成计划"
        if mode == "自动接受":
            return "自动接受模式 · 工具调用将自动批准 · /normal 退出"
        return "输入任务开始 · Enter 提交 · Shift+Tab 切换模式 · /help 查看命令"

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
        except Exception:  # noqa: BLE001 - 未挂载时刷新是空操作
            return
        try:
            self.query_one("#input-hint", Static).update(self._hint_text())
            self.query_one("#input-row").set_class(self._run_active, "streaming")
        except Exception:  # noqa: BLE001, S110 - 未挂载时刷新是空操作
            pass
        mode = self._mode_label()
        # 状态栏瘦身（P2-5 / D5）：只留 模型 · 模式 · 运行态 三项；
        # token/上下文/工具/工作区/会话统计回归 /status 详情。
        if self._run_active:
            dots = "." * (int(time.monotonic() * 2) % 4)
            run_state = f"运行中{dots}"
        else:
            run_state = "空闲"
        bar.update(f"模型 {self.state.model_name} · {mode}模式 · {run_state}")

    # -- 计划模式 -----------------------------------------------------------
    async def _plan_flow(self, text: str) -> None:
        """计划模式提交：建计划 → 界面确认 → 脏文件确认 → Build。"""
        self._run_active = True
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
                HELP_TEXT + "\n工作台按键：Enter 提交 · Ctrl+J 换行 · Shift+Tab 切换模式"
                " · Ctrl+C 取消 · Ctrl+Q 退出"
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
            self._enter_plan()
            return
        if prompt == "/normal":
            self._enter_normal()
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
        if prompt in {"/skills", "/skill"} or prompt.startswith(("/skills ", "/skill ")):
            try:
                skills = self.service.skills_list()
                errors = self.service.skills_errors()
                if not skills and not errors:
                    self._append_system(
                        "未发现配置的 Skills。可在 .deity/skills 或 ~/.config/deity/skills 中添加。"
                    )
                else:
                    lines = ["[bold]已配置的 Skills:[/bold]"]
                    for item in skills:
                        lines.append(
                            f"• [cyan]{item['name']}[/cyan] [{item['source']}]: {item['description']}"
                        )
                    for err in errors:
                        lines.append(f"[red]错误: {err}[/red]")
                    self._append_system("\n".join(lines))
            except Exception as exc:  # noqa: BLE001
                self._append_system(f"获取 Skills 失败：{exc}")
            return
        if prompt == "/mcp" or prompt.startswith("/mcp "):
            try:
                mcp_servers = getattr(self.service, "mcp_servers", {})
                if not mcp_servers:
                    self._append_system(
                        "未配置 MCP 服务器。可在配置文件的 [mcp.servers.*] 段中添加。"
                    )
                else:
                    lines = ["[bold]已配置的 MCP 服务器:[/bold]"]
                    for name, srv in mcp_servers.items():
                        cmd_str = (
                            f"{srv.command} {' '.join(srv.args)}"
                            if hasattr(srv, "command")
                            else str(srv)
                        )
                        lines.append(f"• [cyan]{name}[/cyan]: {cmd_str}")
                    self._append_system("\n".join(lines))
            except Exception as exc:  # noqa: BLE001
                self._append_system(f"获取 MCP 配置失败：{exc}")
            return
        if prompt.split(maxsplit=1)[0] in _CLI_ONLY_COMMANDS:
            self._append_system("该命令请在行式 CLI 中使用（非 TTY 或 deity run 的行式界面）。")
            return
        if prompt.startswith("/"):
            self._append_system("未知命令。输入 /help 查看可用命令。")
            return

    async def _command_status(self) -> None:
        mode = self._mode_label()
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
        context_text = status.context_text()
        if context_text is not None:
            # 上下文占用自状态栏瘦身后回归 /status 详情（P2-5）；
            # context_text 自带「上下文 x/y」文案，直接入列。
            lines.append(context_text)
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
            self._append_system(
                "上一轮没有工具调用记录。先完成一轮会用到工具的任务，再用 /tools 查看。"
            )
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
            self._toggle_tool_node(node.call_id)

    def _command_auto(self) -> None:
        """进入/退出自动接受模式；进入走桥的显式确认（confirm 面板）。"""
        controller = self._bridge.controller
        if controller.mode == "auto":
            controller.exit_auto()
            self._append_system("已退出自动接受模式，恢复 ask。")
            self._refresh_status()
            return
        self.run_worker(self._auto_worker(), group="prompt", exclusive=False)

    async def _auto_worker(self, *, came_from_plan: bool = False) -> None:
        accepted = await asyncio.to_thread(self._bridge.request_auto)
        if accepted:
            if came_from_plan:
                # Shift+Tab 自计划模式切入：显式确认通过后才离开计划
                # 模式；取消确认时不进自动接受、仍留在计划模式。
                self.state.plan_mode = False
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
            self._apply_session_switch(target)
            return
        if not items:
            self._append_system("（暂无已存储会话）直接在下方输入任务即可开始新会话。")
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
                self._append_error(f"运行失败：{run_error_message(exc)}")
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
                self._append_error(f"运行失败：{run_error_message(exc)}")
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
