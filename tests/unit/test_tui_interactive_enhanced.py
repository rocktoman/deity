"""交互工作台增强特性测试：单键审批、Tab 命令补全、工具点击折叠与内置 /skills /mcp 命令。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.cli.chat_approval import make_approve
from deity.interfaces.cli.chat_view_model import ToolNode
from deity.interfaces.tui.approval_bridge import TuiApprovalBridge
from deity.interfaces.tui.interactive import (
    ChatInput,
    InteractiveApp,
    ToolBlock,
)
from deity.interfaces.tui.modals import ApprovalModal


class DummyModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self.turns):
            yield event


def _write_turns(path: str, content: str, call_id: str = "c1") -> list[list[ModelEvent]]:
    return [
        [
            ModelEvent("tool_call_started", call_id=call_id, name="write_file"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id=call_id,
                text=f'{{"path": "{path}", "content": "{content}"}}',
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
    ]


def _make_test_app(tmp_path: Path, model: object) -> InteractiveApp:
    bridge = TuiApprovalBridge(workspace=tmp_path)
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        approve=make_approve(bridge.controller),
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )
    return InteractiveApp(
        service,
        bridge=bridge,
        model_name="fake",
        workspace=tmp_path,
    )


async def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError("等待超时")


@pytest.mark.asyncio
async def test_instant_single_key_approval(tmp_path: Path) -> None:
    """测试单键审批：键盘直接按 '1' 瞬间放行写文件，无需鼠标点击。"""
    pytest.importorskip("textual")
    app = _make_test_app(tmp_path, DummyModel(_write_turns("approved.txt", "content123")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        inp = app.query_one("#input", ChatInput)
        inp.text = "写文件"
        await pilot.press("enter")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()

        # 审批浮层此时显示，直接按键盘 '1'
        assert isinstance(app.screen, ApprovalModal)
        app.screen.query_one("#prompt-panel")
        await pilot.press("1")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()

        assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "content123"
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_instant_single_key_deny(tmp_path: Path) -> None:
    """测试单键拒绝：键盘直接按 '3' 拒绝执行。"""
    pytest.importorskip("textual")
    app = _make_test_app(tmp_path, DummyModel(_write_turns("denied.txt", "content123")))
    bridge = app._bridge
    async with app.run_test() as pilot:
        inp = app.query_one("#input", ChatInput)
        inp.text = "写文件"
        await pilot.press("enter")
        await _wait_until(lambda: bridge.pending is not None)
        await pilot.pause()

        # 面板此时显示，按 '3' 拒绝
        await pilot.press("3")
        await _wait_until(lambda: not app._run_active)
        await pilot.pause()

        assert not (tmp_path / "denied.txt").exists()
        await pilot.press("ctrl+q")


@pytest.mark.asyncio
async def test_slash_command_tab_autocomplete(tmp_path: Path) -> None:
    """测试斜杠命令自动补全：输入 /pl 按 Tab 自动补全为 /plan。"""
    pytest.importorskip("textual")
    app = _make_test_app(tmp_path, DummyModel([]))
    async with app.run_test() as pilot:
        inp = app.query_one("#input", ChatInput)
        inp.text = "/pl"
        inp.move_cursor((0, 3))
        await pilot.press("tab")
        await pilot.pause()

        assert inp.text.startswith("/plan")
        await pilot.press("ctrl+q")


def test_tool_block_click_toggles_expanded() -> None:
    """测试工具块交互：点击 ToolBlock 切换展开/折叠。"""
    node = ToolNode(call_id="c1", name="test_tool", fragments=['{"arg": 1}'])
    toggled = []

    def on_toggle(call_id: str) -> None:
        toggled.append(call_id)
        node.expanded = not node.expanded

    block = ToolBlock(node, on_toggle=on_toggle, formatter=lambda n: f"Tool({n.name})")
    assert not node.expanded
    block.on_click()
    assert toggled == ["c1"]
    assert node.expanded
    block.on_click()
    assert toggled == ["c1", "c1"]
    assert not node.expanded


@pytest.mark.asyncio
async def test_in_tui_skills_and_mcp_commands(tmp_path: Path) -> None:
    """测试 TUI 内置 /skills 与 /mcp 命令不再返回被禁用提示。"""
    pytest.importorskip("textual")
    app = _make_test_app(tmp_path, DummyModel([]))
    async with app.run_test() as pilot:
        inp = app.query_one("#input", ChatInput)
        inp.text = "/skills"
        await pilot.press("enter")
        await pilot.pause()

        # 对话区应展示 Skills 结果或未发现说明，而不是“该命令请在行式 CLI 中使用”
        conv_text = " ".join(str(c.render()) for c in app.query_one("#conversation").children)
        assert "该命令请在行式 CLI 中使用" not in conv_text
        assert "Skills" in conv_text

        inp.text = "/mcp"
        await pilot.press("enter")
        await pilot.pause()
        conv_text = " ".join(str(c.render()) for c in app.query_one("#conversation").children)
        assert "该命令请在行式 CLI 中使用" not in conv_text
        assert "MCP" in conv_text

        await pilot.press("ctrl+q")
