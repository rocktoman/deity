"""居中模型选择浮层（ModalScreen）：实时过滤与选择。"""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ModelPickerModal(ModalScreen[str | None]):
    """交互式模型选择器（ADR-023 浮层版）。"""

    def __init__(self, models: tuple[str, ...], current_model: str) -> None:
        super().__init__()
        self._models = models
        self._current_model = current_model
        self._rows: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="model-dialog", classes="modal-card"):
            yield Static("选择模型（仅本次会话）", id="model-dialog-title", classes="modal-title")
            yield Input(placeholder="输入过滤…", id="model-filter", classes="modal-filter")
            yield VerticalScroll(id="model-list", classes="modal-list")

    async def on_mount(self) -> None:
        await self._render_list("")
        self.query_one("#model-filter", Input).focus()

    async def _render_list(self, filter_text: str) -> None:
        needle = filter_text.strip().lower()
        visible = [name for name in self._models if needle in name.lower()]
        container = self.query_one("#model-list", VerticalScroll)
        await container.remove_children()
        self._rows.clear()
        if not visible:
            await container.mount(Static("无匹配模型。", classes="msg-system"))
            return
        for index, name in enumerate(visible):
            label = f"● {name}（当前）" if name == self._current_model else name
            bid = f"model-row-{index}"
            self._rows[bid] = name
            await container.mount(
                Button(
                    label,
                    id=bid,
                    classes="modal-row current" if name == self._current_model else "modal-row",
                )
            )

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-filter":
            await self._render_list(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "model-filter":
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


__all__ = ["ModelPickerModal"]
