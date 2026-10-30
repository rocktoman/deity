"""Crash-injection worker used only with disposable fixture databases."""

import asyncio
import os
import sys
from pathlib import Path

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, RunStatus, ToolCall, ToolResult


async def run(root: Path, point: str) -> None:
    """Exit abruptly at one persistence/effect boundary."""
    repo = await SQLiteSessionRepository.open(root / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(root), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    await repo.save_checkpoint(run_id, [{"role": "user", "content": "goal"}], {})
    if point == "before_intent":
        os._exit(91)
    await repo.record_model_turn(run_id, "", [ToolCall("c1", "write_file", {"path": "x"})])
    assert await repo.claim_tool(run_id, "c1", "write_file", {"path": "x"})
    if point == "after_intent":
        os._exit(91)
    (root / "effect.log").write_text("executed\n")
    if point == "after_effect":
        os._exit(91)
    await repo.record_tool(run_id, ToolResult("c1", "ok", "created"))
    if point == "after_outcome":
        os._exit(91)
    messages = await repo.context_messages(run_id)
    if point == "before_checkpoint":
        os._exit(91)
    await repo.save_checkpoint(run_id, messages, {"turns": 1, "tool_calls": 1})
    if point == "after_checkpoint":
        os._exit(91)
    await repo.close()
    raise ValueError("unknown injection point")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
