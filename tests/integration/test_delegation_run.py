import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import MultiAgentConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest, RunStatus
from deity.interfaces.cli.main import app


class DelegateModel:
    async def stream(self, request: ModelRequest):
        user = next(message["content"] for message in request.messages if message["role"] == "user")
        has_tool = any(message["role"] == "tool" for message in request.messages)
        if user == "parent" and not has_tool:
            yield ModelEvent("tool_call_started", call_id="delegate-1", name="delegate")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="delegate-1",
                text=json.dumps(
                    {
                        "tasks": [
                            {
                                "goal": "inspect",
                                "allowed_tools": ["read_file"],
                                "max_turns": 2,
                                "max_tool_calls": 2,
                                "deadline_seconds": 20,
                            }
                        ]
                    }
                ),
            )
        elif user == "inspect" and not has_tool:
            assert {tool["name"] for tool in request.tools} == {"read_file"}
            assert all(message["content"] != "parent" for message in request.messages)
            yield ModelEvent("tool_call_started", call_id="read-1", name="read_file")
            yield ModelEvent("tool_call_arguments_delta", call_id="read-1", text='{"path":"a.txt"}')
        else:
            yield ModelEvent("text_delta", text="inspected")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_parent_delegates_read_only_child(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    path = tmp_path / "deity.db"
    service = RunService(
        db_path=path,
        workspace=tmp_path,
        model=DelegateModel(),
        limits=RunConfig(max_turns=6, max_tool_calls=10),
        multi_agent=MultiAgentConfig(enabled=True),
    )
    result = await service.run("parent", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    repo = await SQLiteSessionRepository.open(path)
    try:
        tree = await repo.delegation_tree(result.run_id)
        assert len(tree) == 1
        assert tree[0]["status"] == "completed"
        assert await repo.status(tree[0]["child_run_id"]) == RunStatus.COMPLETED
        stored_result = json.loads(tree[0]["result_json"])
        assert len(stored_result["evidence_ids"]) == 1
    finally:
        await repo.close()
    assert (tmp_path / "a.txt").read_text() == "hello"
    cli = await asyncio.to_thread(
        CliRunner().invoke, app, ["runs", "tree", result.run_id, "--db", str(path)]
    )
    assert cli.exit_code == 0
    assert "inspected" in cli.stdout


@pytest.mark.asyncio
async def test_parent_cancellation_stops_child(tmp_path: Path) -> None:
    started = asyncio.Event()

    class BlockingModel(DelegateModel):
        async def stream(self, request: ModelRequest):
            user = next(
                message["content"] for message in request.messages if message["role"] == "user"
            )
            if user == "inspect":
                started.set()
                await asyncio.Event().wait()
            else:
                async for event in super().stream(request):
                    yield event

    path = tmp_path / "deity.db"
    service = RunService(
        db_path=path,
        workspace=tmp_path,
        model=BlockingModel(),
        limits=RunConfig(max_turns=6, max_tool_calls=10),
        multi_agent=MultiAgentConfig(enabled=True),
    )
    task = asyncio.create_task(service.run("parent", model_name="fake"))
    await asyncio.wait_for(started.wait(), 5)
    repo = await SQLiteSessionRepository.open(path)
    try:
        async with repo.db.execute("SELECT parent_run_id FROM delegations") as cursor:
            parent_id = (await cursor.fetchone())[0]
    finally:
        await repo.close()
    assert service.cancel(parent_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    repo = await SQLiteSessionRepository.open(path)
    try:
        tree = await repo.delegation_tree(parent_id)
        assert tree[0]["status"] == "cancelled"
        assert await repo.status(tree[0]["child_run_id"]) == RunStatus.CANCELLED
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_two_children_run_concurrently_and_report_partial(tmp_path: Path) -> None:
    both_started = asyncio.Event()
    started: set[str] = set()

    class ParallelModel:
        async def stream(self, request: ModelRequest):
            user = next(
                message["content"] for message in request.messages if message["role"] == "user"
            )
            has_tool = any(message["role"] == "tool" for message in request.messages)
            if user == "parent" and not has_tool:
                yield ModelEvent("tool_call_started", call_id="batch", name="delegate")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="batch",
                    text=json.dumps(
                        {
                            "tasks": [
                                {
                                    "goal": goal,
                                    "allowed_tools": ["read_file"],
                                    "max_turns": 1,
                                    "max_tool_calls": 1,
                                    "deadline_seconds": 20,
                                }
                                for goal in ("child-one", "child-two")
                            ]
                        }
                    ),
                )
            elif user.startswith("child-"):
                started.add(user)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 5)
                if user == "child-two":
                    raise ValueError("injected child failure")
                yield ModelEvent("text_delta", text="first complete")
            else:
                tool = next(message for message in request.messages if message["role"] == "tool")
                payload = json.loads(tool["content"])
                assert payload["status"] == "partial"
                yield ModelEvent("text_delta", text="parent complete")
            yield ModelEvent("message_completed")

    path = tmp_path / "deity.db"
    service = RunService(
        db_path=path,
        workspace=tmp_path,
        model=ParallelModel(),
        limits=RunConfig(max_turns=6, max_tool_calls=10),
        multi_agent=MultiAgentConfig(enabled=True, max_concurrency=2),
    )
    result = await service.run("parent", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert {item["status"] for item in await repo.delegation_tree(result.run_id)} == {
            "completed",
            "failed",
        }
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_child_result_is_bounded(tmp_path: Path) -> None:
    class LongModel(DelegateModel):
        async def stream(self, request: ModelRequest):
            user = next(
                message["content"] for message in request.messages if message["role"] == "user"
            )
            has_tool = any(message["role"] == "tool" for message in request.messages)
            if user == "inspect" and has_tool:
                yield ModelEvent("text_delta", text="x" * 5000)
                yield ModelEvent("message_completed")
            else:
                async for event in super().stream(request):
                    yield event

    (tmp_path / "a.txt").write_text("hello")
    path = tmp_path / "deity.db"
    service = RunService(
        db_path=path,
        workspace=tmp_path,
        model=LongModel(),
        limits=RunConfig(max_turns=6, max_tool_calls=10),
        multi_agent=MultiAgentConfig(enabled=True, max_result_chars=1000),
    )
    result = await service.run("parent", model_name="fake")
    repo = await SQLiteSessionRepository.open(path)
    try:
        item = (await repo.delegation_tree(result.run_id))[0]
        assert len(json.loads(item["result_json"])["summary"]) == 1000
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_parent_cancel_also_cancels_queued_child(tmp_path: Path) -> None:
    first_started = asyncio.Event()

    class QueuedModel:
        async def stream(self, request: ModelRequest):
            user = next(
                message["content"] for message in request.messages if message["role"] == "user"
            )
            if user == "parent":
                yield ModelEvent("tool_call_started", call_id="batch", name="delegate")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="batch",
                    text=json.dumps(
                        {
                            "tasks": [
                                {
                                    "goal": goal,
                                    "allowed_tools": ["read_file"],
                                    "max_turns": 2,
                                    "max_tool_calls": 2,
                                    "deadline_seconds": 20,
                                }
                                for goal in ("first", "queued")
                            ]
                        }
                    ),
                )
                yield ModelEvent("message_completed")
            elif user == "first":
                first_started.set()
                await asyncio.Event().wait()
            else:
                raise AssertionError("queued child must not start")

    path = tmp_path / "deity.db"
    service = RunService(
        db_path=path,
        workspace=tmp_path,
        model=QueuedModel(),
        limits=RunConfig(max_turns=6, max_tool_calls=10),
        multi_agent=MultiAgentConfig(enabled=True, max_concurrency=1),
    )
    task = asyncio.create_task(service.run("parent", model_name="fake"))
    await asyncio.wait_for(first_started.wait(), 5)
    repo = await SQLiteSessionRepository.open(path)
    try:
        async with repo.db.execute("SELECT parent_run_id FROM delegations LIMIT 1") as cursor:
            parent_id = (await cursor.fetchone())[0]
    finally:
        await repo.close()
    assert service.cancel(parent_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    repo = await SQLiteSessionRepository.open(path)
    try:
        tree = await repo.delegation_tree(parent_id)
        assert len(tree) == 2
        assert all(item["status"] == "cancelled" for item in tree)
        statuses = [await repo.status(item["child_run_id"]) for item in tree]
        assert statuses == [RunStatus.CANCELLED, RunStatus.CANCELLED]
    finally:
        await repo.close()
