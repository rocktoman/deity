import asyncio
import sys
import time
from pathlib import Path

import pytest

from deity.adapters.mcp import McpServerConfig, McpSessionManager


@pytest.mark.asyncio
async def test_real_stdio_server_discover_and_call(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    manager = McpSessionManager((McpServerConfig("one", sys.executable, (str(script),), tmp_path),))
    tools = await manager.discover("one")
    assert "mcp:one:echo" in [tool.qualified_name for tool in tools]
    echo = next(tool for tool in tools if tool.name == "echo")
    result = await manager.call(echo, {"text": "hello"}, "call-1")
    assert result.status == "ok" and "hello" in result.output


@pytest.mark.asyncio
async def test_mcp_startup_timeout_is_bounded(tmp_path: Path) -> None:
    script = tmp_path / "silent_server.py"
    script.write_text("import time\ntime.sleep(10)\n")
    manager = McpSessionManager(
        (McpServerConfig("silent", sys.executable, (str(script),), tmp_path, timeout_seconds=0.2),)
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await manager.discover("silent")
    assert time.monotonic() - started < 3


@pytest.mark.asyncio
async def test_stdout_pollution_never_registers_tools(tmp_path: Path) -> None:
    script = tmp_path / "polluted_server.py"
    script.write_text("import time\nprint('not JSON-RPC', flush=True)\ntime.sleep(10)\n")
    manager = McpSessionManager(
        (McpServerConfig("bad", sys.executable, (str(script),), tmp_path, timeout_seconds=0.2),)
    )
    with pytest.raises((TimeoutError, ExceptionGroup)):
        await manager.discover("bad")


@pytest.mark.asyncio
async def test_mcp_call_cancellation_stops_waiting(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    manager = McpSessionManager((McpServerConfig("one", sys.executable, (str(script),), tmp_path),))
    slow = next(tool for tool in await manager.discover("one") if tool.name == "slow_echo")
    task = asyncio.create_task(manager.call(slow, {"text": "hello"}, "call-cancel"))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert time.monotonic() - started < 3
