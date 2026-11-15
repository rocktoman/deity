"""F1-A4：per-turn diff（ADR-018）——快照/产出语义与 turn_diff 事件落盘。"""

import json
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.turn_diff import TurnDiffDispatcher
from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
)
from deity.core.runtime import AgentLoop


def _request(tmp_path: Path) -> AgentRequest:
    return AgentRequest("s", "t", str(tmp_path), "task", "fake", run_id="run-1")


def _stack() -> TurnDiffDispatcher:
    return TurnDiffDispatcher(LocalToolDispatcher(approve=lambda _n, _a: True))


@pytest.mark.asyncio
async def test_flush_reports_created_modified_deleted(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "del.py").write_text("bye\n", encoding="utf-8")
    tools = _stack()
    request = _request(tmp_path)
    await tools.dispatch(
        ToolCall("w", "write_file", {"path": "new.py", "content": "hello\n"}), request
    )
    await tools.dispatch(
        ToolCall("p", "apply_patch", {"path": "mod.py", "old": "x = 1", "new": "x = 2"}),
        request,
    )
    await tools.dispatch(ToolCall("d", "delete_file", {"path": "del.py"}), request)
    payload = await tools.flush_turn_diff(request)
    assert payload is not None and payload["files_changed"] == 3
    by_path = {entry["path"]: entry for entry in payload["files"]}
    assert by_path["new.py"]["status"] == "created"
    assert "+hello" in by_path["new.py"]["diff"]
    assert by_path["mod.py"]["status"] == "modified"
    assert "-x = 1" in by_path["mod.py"]["diff"]
    assert "+x = 2" in by_path["mod.py"]["diff"]
    assert by_path["del.py"]["status"] == "deleted"
    assert await tools.flush_turn_diff(request) is None  # 同一轮只产出一次


@pytest.mark.asyncio
async def test_flush_none_when_write_did_not_change_content(
    tmp_path: Path,
) -> None:
    (tmp_path / "same.py").write_text("x = 1\n", encoding="utf-8")
    tools = _stack()
    request = _request(tmp_path)
    result = await tools.dispatch(
        ToolCall("p", "apply_patch", {"path": "same.py", "old": "x = 1", "new": "x = 1"}),
        request,
    )
    assert result.status == "ok"
    assert await tools.flush_turn_diff(request) is None


@pytest.mark.asyncio
async def test_flush_truncates_oversized_diff(tmp_path: Path) -> None:
    tools = _stack()
    request = _request(tmp_path)
    big = "\n".join(f"line {index}" for index in range(2000)) + "\n"
    await tools.dispatch(
        ToolCall("w", "write_file", {"path": "big.txt", "content": big}), request
    )
    payload = await tools.flush_turn_diff(request)
    assert payload is not None
    entry = payload["files"][0]
    assert entry["truncated"] is True and len(entry["diff"]) == 8_000


@pytest.mark.asyncio
async def test_turn_diff_consistent_with_git_diff(tmp_path: Path) -> None:
    """抽查：两轮 turn_diff 的改动集合与内容应与 git status/git diff 一致。"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    tools = _stack()
    request = _request(tmp_path)
    await tools.dispatch(
        ToolCall("p", "apply_patch", {"path": "mod.py", "old": "x = 1", "new": "x = 10"}),
        request,
    )
    first = await tools.flush_turn_diff(request)
    await tools.dispatch(
        ToolCall("w", "write_file", {"path": "new.py", "content": "fresh\n"}), request
    )
    second = await tools.flush_turn_diff(request)
    assert first is not None and second is not None
    tracked = {entry["path"] for entry in first["files"]} | {
        entry["path"] for entry in second["files"]
    }
    porcelain = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    git_paths = {line[3:] for line in porcelain.splitlines() if line}
    assert tracked == git_paths == {"mod.py", "new.py"}
    git_diff = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--", "mod.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    turn_lines = {
        line
        for line in first["files"][0]["diff"].splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    }
    git_lines = {
        line
        for line in git_diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    }
    assert turn_lines == git_lines


class _WriteModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent("tool_call_started", call_id="w1", name="write_file")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="w1",
                text=json.dumps({"path": "a.txt", "content": "hello\n"}),
            )
            yield ModelEvent("message_completed")
        else:
            yield ModelEvent("text_delta", text="完成")
            yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_turn_diff_event_persisted_and_emitted(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    model = _WriteModel()
    kinds: list[str] = []
    result = await AgentLoop(model, _stack(), repository=repo).run(
        AgentRequest("s", "t", str(tmp_path), "task", "scripted"),
        live_event_sink=lambda event: kinds.append(event.kind),
    )
    assert result.status == RunStatus.COMPLETED
    assert "turn_diff" in kinds
    connection = sqlite3.connect(tmp_path / "deity.db")
    rows = connection.execute(
        "SELECT payload_json FROM events WHERE type='turn_diff'"
    ).fetchall()
    connection.close()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["files"][0]["path"] == "a.txt"
    assert payload["files"][0]["status"] == "created"
