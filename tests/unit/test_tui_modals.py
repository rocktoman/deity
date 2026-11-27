"""TUI 浮层模态框单元测试：ApprovalModal / ModelPickerModal / CommandPaletteModal。"""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from deity.interfaces.tui.approval_bridge import PendingPrompt
from deity.interfaces.tui.modals import (
    ApprovalModal,
    CommandPaletteModal,
    ModelPickerModal,
)
from deity.interfaces.tui.theme import palette


class _ModalHost(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


@pytest.mark.asyncio
async def test_approval_modal_single_keys_and_diff() -> None:
    pytest.importorskip("textual")
    app = _ModalHost()
    results: list[str | None] = []
    pending = PendingPrompt(
        kind="approval",
        text="工具：apply_patch",
        pattern="pytest *",
        allow_permanent=True,
        diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    async with app.run_test() as pilot:
        modal = ApprovalModal(pending, palette(dark=True))
        app.push_screen(modal, results.append)
        await pilot.pause()
        diff_text = str(app.screen.query_one("#prompt-diff").render())
        assert "old" in diff_text and "new" in diff_text
        assert "pytest *" in str(app.screen.query_one("#btn-allow-session").label)
        await pilot.press("4")
        await pilot.pause()
    assert results == ["4"]


@pytest.mark.asyncio
async def test_model_picker_modal_filters_and_selects() -> None:
    pytest.importorskip("textual")
    app = _ModalHost()
    results: list[str | None] = []
    async with app.run_test() as pilot:
        modal = ModelPickerModal(("qwen3.8-omni-flash", "qwen4-max"), "qwen3.8-omni-flash")
        app.push_screen(modal, results.append)
        await pilot.pause()
        await pilot.press("q", "w", "e", "n", "4")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert results == ["qwen4-max"]


@pytest.mark.asyncio
async def test_command_palette_modal_filters_and_dismisses() -> None:
    pytest.importorskip("textual")
    app = _ModalHost()
    results: list[str | None] = []
    items = [("/help", "查看帮助"), ("/compact", "压缩当前会话历史上下文")]
    async with app.run_test() as pilot:
        modal = CommandPaletteModal(items)
        app.push_screen(modal, results.append)
        await pilot.pause()
        await pilot.press("c", "o", "m", "p")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert results == ["/compact"]
