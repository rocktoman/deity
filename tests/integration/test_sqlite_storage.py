import asyncio
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
    ToolResult,
)
from deity.core.runtime import AgentLoop


@pytest.mark.asyncio
async def test_tool_result_and_event_survive_reopen(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "hi", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    assert await repo.claim_tool(run_id, "c1", "read_file", {"path": "a"})
    assert not await repo.claim_tool(run_id, "c1", "read_file", {"path": "a"})
    await repo.record_tool(run_id, ToolResult("c1", "ok", "content"))
    await repo.close()

    repo = await SQLiteSessionRepository.open(db)
    assert await repo.status(run_id) == RunStatus.INTERRUPTED
    events = await repo.events(run_id)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["type"] == "ToolCompleted" for event in events)
    await repo.close()


@pytest.mark.asyncio
async def test_failed_record_rolls_back(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "hi", "fake"))
    assert await repo.claim_tool(run_id, "c1", "read_file", {})
    with pytest.raises(ValueError):
        await repo.record_tool(run_id, ToolResult("wrong", "ok", "x"))
    assert not any(event["type"] == "ToolCompleted" for event in await repo.events(run_id))
    await repo.close()


@pytest.mark.asyncio
async def test_agent_loop_persists_tool_completion(tmp_path: Path) -> None:
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
        AgentRequest("s", "t", str(tmp_path), "hi", "fake")
    )
    assert await repo.status(result.run_id) == RunStatus.COMPLETED
    assert any(event["type"] == "ToolCompleted" for event in await repo.events(result.run_id))
    assert [step["kind"] for step in await repo.steps(result.run_id)] == ["model", "tool", "model"]
    await repo.close()


@pytest.mark.asyncio
async def test_concurrent_claims_keep_unique_event_sequence(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "hi", "fake"))
    claims = await asyncio.gather(
        *(
            repo.claim_tool(run_id, f"c{i}", "read_file", {"api_key": "secret-marker"})
            for i in range(10)
        )
    )
    assert all(claims)
    events = await repo.events(run_id)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert "secret-marker" not in str(events)
    await repo.close()


@pytest.mark.asyncio
async def test_session_history_and_evidence(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "hi", "fake"))
    await repo.append_message("s", run_id, "user", "hi")
    await repo.record_evidence(run_id, "test", "exit code 0", "c1")
    assert [item["id"] for item in await repo.sessions()] == ["s"]
    assert [item["role"] for item in await repo.messages("s")] == ["user"]
    assert [item["kind"] for item in await repo.evidence(run_id)] == ["test"]
    await repo.close()


@pytest.mark.asyncio
async def test_event_published_after_commit(tmp_path: Path) -> None:
    seen: list[str] = []

    async def publisher(run_id: str, kind: str) -> None:
        assert any(event["type"] == kind for event in await repo.events(run_id))
        seen.append(kind)

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db", publisher=publisher)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "hi", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    assert seen == ["RunCreated", "RunStatusChanged"]
    await repo.close()
