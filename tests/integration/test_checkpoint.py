"""Checkpoint persistence and corruption handling."""

import hashlib
import json
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, ToolCall, ToolResult
from deity.core.runtime import AgentLoop


@pytest.mark.asyncio
async def test_checkpoint_roundtrip_and_hash_validation(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    messages = [{"role": "user", "content": "goal"}]
    checkpoint = await repo.save_checkpoint(run_id, messages, {"turns": 0, "tool_calls": 0})
    assert (await repo.latest_checkpoint(run_id))["id"] == checkpoint
    with pytest.raises(ValueError):
        await repo.save_checkpoint(run_id, [{"role": "user", "content": "bad"}], {})
    await repo.close()

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    assert (await repo.latest_checkpoint(run_id))["messages"] == messages
    await repo.db.execute("UPDATE checkpoints SET sha256='bad' WHERE id=?", (checkpoint,))
    await repo.db.commit()
    assert await repo.latest_checkpoint(run_id) is None
    await repo.close()


@pytest.mark.asyncio
async def test_malformed_checkpoint_falls_back_to_prior_valid_version(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    first = await repo.save_checkpoint(run_id, [{"role": "user", "content": "goal"}], {"turns": 0})
    await repo.record_model_turn(run_id, "done", [])
    second = await repo.save_checkpoint(
        run_id,
        [{"role": "user", "content": "goal"}, {"role": "assistant", "content": "done"}],
        {"turns": 1},
    )
    malformed = "{broken"
    await repo.db.execute(
        "UPDATE checkpoints SET snapshot_json=?, sha256=? WHERE id=?",
        (malformed, hashlib.sha256(malformed.encode()).hexdigest(), second),
    )
    await repo.db.commit()
    assert (await repo.latest_checkpoint(run_id))["id"] == first
    await repo.close()


@pytest.mark.asyncio
async def test_pending_tool_blocks_checkpoint(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    assert await repo.claim_tool(run_id, "c1", "write_file", {"path": "x"})
    with pytest.raises(ValueError):
        await repo.save_checkpoint(run_id, [{"role": "user", "content": "goal"}], {})
    async with repo.db.execute("SELECT COUNT(*) FROM checkpoints") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0
    assert json.dumps(await repo.events(run_id))
    await repo.close()


@pytest.mark.asyncio
async def test_agent_loop_checkpoints_each_safe_frontier(tmp_path: Path) -> None:
    class Model:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="c1", name="read_file")
                yield ModelEvent("tool_call_arguments_delta", call_id="c1", text='{"path":"a"}')
            else:
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            return ToolResult(call.call_id, "ok", "content")

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    result = await AgentLoop(Model(), Tool(), repository=repo).run(
        AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    )
    checkpoint = await repo.latest_checkpoint(result.run_id)
    assert checkpoint is not None and checkpoint["version"] == 3
    assert checkpoint["messages"][-1]["content"] == "done"
    await repo.close()


@pytest.mark.asyncio
async def test_agent_loop_emits_context_compaction_audit(tmp_path: Path) -> None:
    class Model:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn <= 8:
                call_id = f"c{self.turn}"
                yield ModelEvent("tool_call_started", call_id=call_id, name="read_file")
                yield ModelEvent("tool_call_arguments_delta", call_id=call_id, text='{"path":"a"}')
            else:
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            return ToolResult(call.call_id, "ok", "x" * 1000)

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    result = await AgentLoop(Model(), Tool(), repository=repo).run(
        AgentRequest(
            "s",
            "t",
            str(tmp_path),
            "goal",
            "fake",
            max_turns=12,
            context_window_tokens=5000,
            compaction_enabled=True,
        )
    )
    events = await repo.events(result.run_id)
    compactions = [event for event in events if event["type"] == "ContextCompacted"]
    assert compactions
    assert all(
        event["payload"]["new_estimate"] < event["payload"]["old_estimate"] for event in compactions
    )
    await repo.close()
