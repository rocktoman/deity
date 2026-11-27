"""居中审批与确认浮层（ModalScreen）：支持单键直达与 Diff 预览。"""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from deity.interfaces.cli.diff_render import format_diff_text
from deity.interfaces.tui.approval_bridge import PendingPrompt


class ApprovalModal(ModalScreen[str]):
    """工具审批 / 计划构建 / 自动接受确认居中浮层。"""

    def __init__(
        self,
        pending: PendingPrompt,
        colors: dict[str, str],
        *,
        confirm_label: str = "确认",
        cancel_label: str = "取消",
    ) -> None:
        super().__init__()
        self._pending = pending
        self._colors = colors
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._button_map: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        title, subtitle = (
            ("审批", "工具调用需要你的确认") if self._pending.kind == "approval" else ("确认", "")
        )
        title_line = Text()
        title_line.append(title, style="bold")
        if subtitle:
            title_line.append(f"  {subtitle}", style=self._colors["tertiary"])

        diff_renderable: str | Text = (
            format_diff_text(self._pending.diff, self._colors) if self._pending.diff else ""
        )
        with Vertical(id="prompt-panel", classes="modal-card modal-card-warning"):
            yield Static(title_line, id="prompt-title", classes="modal-title")
            with VerticalScroll(id="prompt-scroll", classes="modal-scroll"):
                yield Static(self._pending.text or "（无内容）", id="prompt-text")
                yield Static(diff_renderable, id="prompt-diff")
            with Horizontal(id="prompt-buttons", classes="modal-actions"):
                yield Button("[1] 允许一次", id="btn-allow-once", variant="success")
                yield Button("[2] 本次会话都允许", id="btn-allow-session")
                yield Button("[4] 永久允许（保存为规则）", id="btn-allow-always")
                yield Button("[3/Esc] 拒绝", id="btn-deny", variant="error")
                yield Button(
                    f"[y/Enter] {self._confirm_label}", id="btn-confirm", variant="success"
                )
                yield Button(f"[n/Esc] {self._cancel_label}", id="btn-cancel")

    def on_mount(self) -> None:
        if self._pending.kind == "approval":
            self._button_map = {
                "btn-allow-once": "1",
                "btn-allow-session": "2",
                "btn-allow-always": "4",
                "btn-deny": "3",
            }
            visible = {"btn-allow-once", "btn-allow-session", "btn-deny"}
            if self._pending.allow_permanent:
                visible.add("btn-allow-always")
            session_btn = self.query_one("#btn-allow-session", Button)
            if self._pending.pattern:
                session_btn.label = f"[2] 本次会话允许同类命令（{self._pending.pattern}）"
            else:
                session_btn.label = "[2] 本次会话都允许"
            focus_id = "btn-allow-once"
        else:
            self._button_map = {"btn-confirm": "yes", "btn-cancel": ""}
            visible = {"btn-confirm", "btn-cancel"}
            focus_id = "btn-confirm"
        for button in self.query("#prompt-buttons Button"):
            assert isinstance(button, Button)
            button.styles.display = "block" if button.id in visible else "none"
        self.query_one(f"#{focus_id}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid is not None and bid in self._button_map:
            self.dismiss(self._button_map[bid])

    def on_key(self, event: Key) -> None:
        if self._pending.kind == "approval":
            mapping: dict[str, str] = {
                "1": "1",
                "2": "2",
                "3": "3",
                "escape": "3",
            }
            if self._pending.allow_permanent:
                mapping["4"] = "4"
            if event.key in mapping:
                event.prevent_default()
                event.stop()
                self.dismiss(mapping[event.key])
                return
        else:
            confirm_map = {
                "y": "yes",
                "Y": "yes",
                "enter": "yes",
                "n": "",
                "N": "",
                "escape": "",
            }
            if event.key in confirm_map:
                event.prevent_default()
                event.stop()
                self.dismiss(confirm_map[event.key])
                return

    def force_abort(self) -> None:
        """应用退出或外部取消时安全收口模态框。"""
        default_choice = "3" if self._pending.kind == "approval" else ""
        self.dismiss(default_choice)


__all__ = ["ApprovalModal"]
