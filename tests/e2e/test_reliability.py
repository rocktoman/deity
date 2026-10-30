"""R01–R06 deterministic long-run and failure-injection gates."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools import LocalToolDispatcher
from deity.application.run_service import RunService
from deity.core.context.reliable import ContextBudget, ContextCompactor
from deity.core.contracts import AgentRequest, RunStatus, ToolCall


def test_r01_one_hundred_turns_and_twenty_compactions() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "Keep invariant R01"}]
    compactor = ContextCompactor()
    compacted = 0
    for index in range(100):
        messages.extend(
            [
                {"role": "assistant", "content": f"turn-{index}"},
                {"role": "tool", "call_id": f"r{index}", "content": "large" * 400},
            ]
        )
        prior = len(str(messages))
        snapshot = compactor.compact(messages, ContextBudget(4000, 400, 200))
        messages = list(snapshot.messages)
        if len(str(messages)) < prior:
            compacted += 1
        assert snapshot.estimated_tokens <= 3400
    assert compacted >= 20
    assert "Keep invariant R01" in str(messages)


@pytest.mark.asyncio
async def test_r02_six_crash_boundaries_never_replay_unknown_effect(tmp_path: Path) -> None:
    points = (
        "before_intent",
        "after_intent",
        "after_effect",
        "after_outcome",
        "before_checkpoint",
        "after_checkpoint",
    )
    for point in points:
        root = tmp_path / point
        root.mkdir()
        worker = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "evals.runners.reliability_worker", str(root), point],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert worker.returncode == 91, worker.stderr
        repo = await SQLiteSessionRepository.open(root / "deity.db")
        async with repo.db.execute("SELECT id FROM runs") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        run_id = row[0]
        assert await repo.status(run_id) == RunStatus.INTERRUPTED
        events = await repo.events(run_id)
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert "RunStatusChanged" in [event["type"] for event in events]
        marker = root / "effect.log"
        if marker.exists():
            assert marker.read_text().count("executed") <= 1
        if point in {"after_intent", "after_effect"}:
            assert (await repo.unresolved_tools(run_id))[0]["status"] == "unknown"
        if point in {"after_outcome", "before_checkpoint"}:
            checkpoint = await repo.latest_checkpoint(run_id)
            assert checkpoint is not None
            assert await repo.effects_after_checkpoint(run_id, checkpoint["last_event_seq"])
        await repo.close()


@pytest.mark.asyncio
async def test_r03_two_processes_cannot_own_same_run(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    lease = await repo.acquire_lease(run_id, f"{os.getpid()}:owner")
    await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    code = (
        "import asyncio,sys\nfrom pathlib import Path\n"
        "from deity.adapters.storage import SQLiteSessionRepository\n"
        "async def main():\n"
        " r=await SQLiteSessionRepository.open(Path(sys.argv[1]));\n"
        " try:\n  await r.acquire_lease(sys.argv[2], 'other'); raise AssertionError\n"
        " except ValueError: pass\n"
        " assert (await r.status(sys.argv[2])).value=='running'; await r.close()\n"
        "asyncio.run(main())"
    )
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code, str(db), run_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    await repo.release_lease(lease)
    await repo.close()


@pytest.mark.asyncio
async def test_r04_shell_cancel_converges_within_five_seconds(tmp_path: Path) -> None:
    dispatcher = LocalToolDispatcher(approve=lambda name, args: True)
    request = AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    task = asyncio.create_task(
        dispatcher.dispatch(ToolCall("c1", "shell", {"argv": ["sleep", "10"]}), request)
    )
    await asyncio.sleep(0.1)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_r05_auth_failure_never_retries(tmp_path: Path) -> None:
    from deity.core.contracts import ModelRequest
    from deity.core.runtime import AgentLoop, ModelFailure

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ModelRequest):
            self.calls += 1
            raise ModelFailure("model_auth", retryable=False)
            yield  # pragma: no cover

    model = Model()
    with pytest.raises(ModelFailure):
        await AgentLoop(model, LocalToolDispatcher()).run(
            AgentRequest("s", "t", str(tmp_path), "goal", "fake")
        )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_r06_corrupt_checkpoint_and_drift_refuse_resume(tmp_path: Path) -> None:
    db = tmp_path.parent / "r06.db"
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    await repo.save_checkpoint(
        run_id,
        [{"role": "user", "content": "goal"}],
        {"turns": 0, "tool_calls": 0, "workspace_fingerprint": "wrong"},
    )
    await repo.set_status(run_id, RunStatus.INTERRUPTED)
    await repo.close()
    service = RunService(db_path=db, workspace=tmp_path)
    assert (await service.inspect(run_id))["reason"] == "workspace_changed"
    repo = await SQLiteSessionRepository.open(db)
    await repo.db.execute("UPDATE checkpoints SET sha256='corrupt' WHERE run_id=?", (run_id,))
    await repo.db.commit()
    await repo.close()
    assert (await service.inspect(run_id))["reason"] == "no_valid_checkpoint"
