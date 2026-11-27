"""全局命令面板（Ctrl+P，ModalScreen）：模糊过滤并触发斜杠命令或快捷动作。"""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class CommandPaletteModal(ModalScreen[str | None]):
    """AGY 风格全局命令面板：快速检索斜杠命令与工作台快捷操作。"""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__()
        self._items = items
        self._rows: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="command-palette", classes="modal-card"):
            yield Static("命令面板（Enter 执行 · Esc 关闭）", classes="modal-title")
            yield Input(placeholder="搜索命令或操作…", id="palette-filter", classes="modal-filter")
            yield VerticalScroll(id="palette-list", classes="modal-list")

    async def on_mount(self) -> None:
        await self._render_list("")
        self.query_one("#palette-filter", Input).focus()

    async def _render_list(self, query: str) -> None:
        needle = query.strip().lower()
        visible = [
            (cmd, desc)
            for cmd, desc in self._items
            if needle in cmd.lower() or needle in desc.lower()
        ]
        container = self.query_one("#palette-list", VerticalScroll)
        await container.remove_children()
        self._rows.clear()
        if not visible:
            await container.mount(Static("无匹配命令。", classes="msg-system"))
            return
        for index, (cmd, desc) in enumerate(visible):
            bid = f"palette-row-{index}"
            self._rows[bid] = cmd
            await container.mount(Button(f"{cmd}  —  {desc}", id=bid, classes="modal-row"))

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-filter":
            await self._render_list(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "palette-filter":
            first = next(iter(self._rows.values()), None)
            if first is not None:
                self.dismiss(first)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid is not None and bid in self._rows:
            self.dismiss(self._rows[bid])

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)


__all__ = ["CommandPaletteModal"]
