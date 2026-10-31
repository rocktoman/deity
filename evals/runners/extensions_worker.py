"""Independent process for MCP external-effect crash boundaries."""

import asyncio
import os
import sys
from pathlib import Path

from deity.adapters.mcp import McpServerConfig, McpSessionManager
from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, RunStatus, ToolResult


async def run(root: Path, point: str) -> None:
    script = Path(__file__).parents[2] / "tests/fixtures/mcp_echo_server.py"
    repo = await SQLiteSessionRepository.open(root / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(root), "write marker", "fake"))
    lease = await repo.acquire_lease(run_id, f"{os.getpid()}:worker")
    await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    if point == "before_intent":
        os._exit(91)
    assert await repo.claim_tool(
        run_id,
        "m1",
        "mcp:one:write_marker",
        {"path": str(root / "effect.log")},
        epoch=lease.epoch,
    )
    if point == "after_intent":
        os._exit(91)
    manager = McpSessionManager((McpServerConfig("one", sys.executable, (str(script),), root),))
    tool = next(item for item in await manager.discover("one") if item.name == "write_marker")
    await manager.call(tool, {"path": str(root / "effect.log")}, "m1")
    if point == "after_effect":
        os._exit(91)
    await repo.record_tool(run_id, ToolResult("m1", "ok", "written"), epoch=lease.epoch)
    if point == "after_outcome":
        os._exit(91)
    raise ValueError(point)


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
