"""S4 回放：不写文件、不发网络、写工具被拒、旧 schema 事件只读。"""

import asyncio
import json
from pathlib import Path

import pytest

from deity.application.replay import ReplayDispatcher, TraceReplayer
from deity.core.contracts import AgentRequest, ModelEvent, ToolCall
from deity.core.contracts.client import ClientEvent


def _request(tmp_path: Path) -> AgentRequest:
    return AgentRequest("s", "t", str(tmp_path), "goal", "fake")


@pytest.mark.asyncio
async def test_replay_dispatcher_denies_write_tools_and_records_blocked(
    tmp_path: Path,
) -> None:
    dispatcher = ReplayDispatcher()
    for name in ("write_file", "apply_patch", "delete_file", "shell"):
        result = await dispatcher.dispatch(
            ToolCall(f"c-{name}", name, {"path": "x.txt"}), _request(tmp_path)
        )
        assert result.status == "denied"
        assert "replay_blocked" in result.output
    assert not (tmp_path / "x.txt").exists()
    assert dispatcher.blocked == ("c-write_file", "c-apply_patch", "c-delete_file", "c-shell")


@pytest.mark.asyncio
async def test_replay_dispatcher_allows_read_only_tools_without_side_effects(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hello")
    dispatcher = ReplayDispatcher()
    result = await dispatcher.dispatch(
        ToolCall("c-read", "read_file", {"path": "a.txt"}), _request(tmp_path)
    )
    assert result.status == "ok"
    assert dispatcher.blocked == ()


def test_replay_events_rebuilds_terminal_state_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("untouched")
    replayer = TraceReplayer()
    events = [
        ClientEvent(seq=1, run_id="r1", kind="text_delta", payload={"text": "hi"}),
        ClientEvent(
            seq=2,
            run_id="r1",
            kind="tool_call_started",
            payload={"call_id": "c1", "name": "write_file"},
        ),
        ClientEvent(
            seq=3,
            run_id="r1",
            kind="RunStatusChanged",
            payload={"status": "completed"},
        ),
    ]
    result = replayer.replay_events("r1", events)
    assert result.status == "completed"
    assert result.events_seen == 3
    assert result.schema_mismatch is False
    assert target.read_text() == "untouched"


def test_replay_old_schema_events_are_read_only_and_flagged() -> None:
    replayer = TraceReplayer()
    events = [
        ClientEvent(
            seq=1,
            run_id="r1",
            kind="text_delta",
            payload={"text": "old"},
            schema_version=99,
        ),
        ClientEvent(
            seq=2,
            run_id="r1",
            kind="RunStatusChanged",
            payload={"status": "completed"},
            schema_version=99,
        ),
    ]
    result = replayer.replay_events("r1", events)
    assert result.schema_mismatch is True
    assert result.status == "completed"
    assert "old" in result.summary.get("text", "")


def test_replay_report_reconstructs_same_verdict(tmp_path: Path) -> None:
    report = {
        "case_id": "B01",
        "passed": True,
        "verification": "PASS",
        "duration_ms": 123,
        "environment": {"python": "3.12.3"},
        "model": "deterministic-pytest",
    }
    path = tmp_path / "B01.json"
    path.write_text(json.dumps(report))
    result = TraceReplayer().replay_report(path)
    assert result.case_id == "B01"
    assert result.status == "completed"
    assert result.events_seen == 0


def test_replay_report_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        TraceReplayer().replay_report(tmp_path / "nope.json")


@pytest.mark.asyncio
async def test_replay_sqlite_reads_events_without_writing(tmp_path: Path) -> None:
    from deity.application.run_service import RunService

    class FinalModel:
        async def stream(self, request):  # type: ignore[no-untyped-def]
            yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    db = tmp_path / "deity.db"
    service = RunService(db_path=db, workspace=tmp_path, model=FinalModel())  # type: ignore[arg-type]
    outcome = await service.run("hi", model_name="fake")
    assert outcome.status.value == "completed"
    before = db.stat().st_mtime_ns
    await asyncio.sleep(0.01)
    result = TraceReplayer().replay_sqlite(db, outcome.run_id)
    assert result.status == "completed"
    assert result.events_seen > 0
    assert db.stat().st_mtime_ns == before
