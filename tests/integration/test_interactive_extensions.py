"""Interactive extension inspection keeps discovery scoped to one MCP server."""

import sys
from pathlib import Path

import pytest

from deity.application.config import McpServerSettings
from deity.application.run_service import RunService
from deity.interfaces.cli.menu import _move


def test_arrow_navigation_wraps() -> None:
    assert _move(0, "up", 2) == 1
    assert _move(1, "down", 2) == 0


@pytest.mark.asyncio
async def test_selected_mcp_discovery_does_not_start_other_server(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda name, args: name == "mcp_server_start",
        mcp_enabled=True,
        mcp_servers=(
            McpServerSettings("good", sys.executable, (str(fixture),)),
            McpServerSettings("unavailable", "/missing/deity-server"),
        ),
    )
    names = await service.mcp_test("good")
    assert "mcp:good:echo" in names
    assert all(not name.startswith("mcp:unavailable:") for name in names)
    with pytest.raises(ValueError, match="未配置"):
        await service.mcp_test("unknown")
