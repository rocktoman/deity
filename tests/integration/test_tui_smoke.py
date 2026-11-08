"""S2 TUI 冒烟：有 Textual 时启动到首屏并退出；无 Textual 时退出码 2。"""

import sys

import pytest
from typer.testing import CliRunner


@pytest.mark.asyncio
async def test_tui_app_starts_and_quits() -> None:
    pytest.importorskip("textual")
    from deity.core.contracts.client import ClientEvent
    from deity.interfaces.tui.app import TuiApp

    app = TuiApp()
    async with app.run_test() as pilot:
        app.feed(ClientEvent(seq=1, run_id="r1", kind="text_delta", payload={"text": "hello"}))
        await pilot.pause()
        assert "hello" in app.view_model.snapshot()["text"]
        await pilot.press("q")


def test_tui_command_without_textual_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟未安装 Textual：CLI 不 traceback，给出安装提示。"""
    for name in [
        name
        for name in list(sys.modules)
        if name == "textual" or name.startswith(("textual.", "deity.interfaces.tui"))
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "textual", None)
    monkeypatch.setitem(sys.modules, "textual.app", None)
    from deity.interfaces.cli.main import app as cli_app

    result = CliRunner().invoke(cli_app, ["tui"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "textual" in result.output.lower() or "Textual" in result.output
