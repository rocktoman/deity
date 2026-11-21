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


@pytest.mark.asyncio
async def test_tui_app_theme_and_tool_line_format() -> None:
    """D7 观感对齐：监视台共用 deity 主题，工具行与工作台同格式。"""
    pytest.importorskip("textual")
    from textual.widgets import Static

    from deity.core.contracts.client import ClientEvent
    from deity.interfaces.tui.app import TuiApp

    app = TuiApp()
    async with app.run_test() as pilot:
        assert app.theme.startswith("deity")
        app.feed(
            ClientEvent(
                seq=1,
                run_id="r1",
                kind="tool_call_started",
                payload={"call_id": "c1", "name": "read_file"},
            )
        )
        app.feed(
            ClientEvent(
                seq=2,
                run_id="r1",
                kind="ToolCompleted",
                payload={
                    "call_id": "c1",
                    "tool_name": "read_file",
                    "status": "ok",
                    "output": "第一行\n第二行",
                },
            )
        )
        await pilot.pause()
        tools = str(app.query_one("#run-tools", Static).render())
        assert "⏺ read_file [ok]" in tools
        assert "⎿ 第一行" in tools
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
    assert "uv run deity tui" in " ".join(result.output.split())
    assert ".[tui]" in result.output
