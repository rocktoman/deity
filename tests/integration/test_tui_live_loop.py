"""TUI 实时闭环：pilot 下按键作用于真实 FakeModel Run（批准/拒绝/取消/恢复）。

先补的失败测试：TuiApp 须经 ClientService 订阅事件、a/d/c/r 键真实生效。
"""

import asyncio
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools.workspace import workspace_digest
from deity.application.client_service import ClientService
from deity.application.config import PermissionConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, RunStatus


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]], delay: float = 0.0) -> None:
        self.turns = iter(turns)
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in next(self.turns):
            yield event


class FinalModel:
    async def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="resumed")
        yield ModelEvent("message_completed")


def _write_turns() -> list[list[ModelEvent]]:
    return [
        [
            ModelEvent("tool_call_started", call_id="c1", name="write_file"),
            ModelEvent(
                "tool_call_arguments_delta",
                call_id="c1",
                text='{"path": "b.txt", "content": "hello"}',
            ),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
    ]


def _service(tmp_path: Path, model: object) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )


async def _wait_pending(client: ClientService) -> None:
    for _ in range(100):
        if client.pending_approvals():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no pending approval appeared")


@pytest.mark.asyncio
async def test_tui_approve_key_completes_real_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    client = ClientService(_service(tmp_path, FakeModel(_write_turns())))
    handle = await client.start_run("写入 b.txt", "fake")
    await _wait_pending(client)
    app = TuiApp(client_service=client, run_id=handle.run_id)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.view_model.pending_approvals():
                break
            await pilot.pause()
        assert app.view_model.pending_approvals(), "TUI 未同步到待决审批"
        await pilot.press("a")
        result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "done"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_tui_deny_key_blocks_write(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    client = ClientService(_service(tmp_path, FakeModel(_write_turns())))
    handle = await client.start_run("写入 b.txt", "fake")
    await _wait_pending(client)
    app = TuiApp(client_service=client, run_id=handle.run_id)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.view_model.pending_approvals():
                break
            await pilot.pause()
        assert app.view_model.pending_approvals()
        await pilot.press("d")
        result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.asyncio
async def test_tui_cancel_key_cancels_running_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    client = ClientService(
        _service(
            tmp_path,
            FakeModel(
                [[ModelEvent("text_delta", text="slow"), ModelEvent("message_completed")]],
                delay=5.0,
            ),
        )
    )
    handle = await client.start_run("慢任务", "fake")
    await asyncio.sleep(0.3)
    app = TuiApp(client_service=client, run_id=handle.run_id)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.CANCELLED


async def _interrupted_run(tmp_path: Path) -> tuple[ClientService, str, Path]:
    """在 ws 子目录建一个可恢复的中断 Run；DB 在工作区外，避免 digest 漂移。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "code.py").write_text("value = 1\n")
    db = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db)
    parent = await repo.create_run(AgentRequest("s", "t", str(workspace), "goal", "fake"))
    await repo.set_status(parent, RunStatus.RUNNING)
    await repo.save_checkpoint(
        parent,
        [{"role": "user", "content": "goal"}],
        {"turns": 0, "tool_calls": 0, "workspace_fingerprint": workspace_digest(workspace)},
    )
    await repo.set_status(parent, RunStatus.INTERRUPTED)
    await repo.close()
    service = RunService(
        db_path=db,
        workspace=workspace,
        model=FinalModel(),  # type: ignore[arg-type]
        limits=RunConfig(resume_enabled=True),
    )
    return ClientService(service), parent, db


@pytest.mark.asyncio
async def test_tui_resume_key_resumes_interrupted_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    client, parent, db = await _interrupted_run(tmp_path)
    app = TuiApp(client_service=client, run_id=parent)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        for _ in range(100):
            if app.run_id != parent:
                break
            await pilot.pause()
        assert app.run_id != parent, f"恢复未产生后继 Run：{app.last_resume_error}"
        # 后继 Run 由 RunService.resume 直接创建并跑完（不在 ClientService._tasks 里），
        # 恢复返回时已终态，直接读仓储断言。
        successor = app.run_id
    assert successor is not None
    repo = await SQLiteSessionRepository.open(db)
    try:
        assert await repo.status(parent) == RunStatus.INTERRUPTED
        assert await repo.status(successor) == RunStatus.COMPLETED
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_tui_resume_key_rejects_non_interrupted_run(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from deity.interfaces.tui.app import TuiApp

    model = FakeModel([[ModelEvent("text_delta", text="hi"), ModelEvent("message_completed")]])
    client = ClientService(_service(tmp_path, model))
    handle = await client.start_run("打招呼", "fake")
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    app = TuiApp(client_service=client, run_id=handle.run_id)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        for _ in range(50):
            if app.last_resume_error is not None:
                break
            await pilot.pause()
    assert app.last_resume_error is not None
    assert "cannot resume" in app.last_resume_error
    assert app.run_id == handle.run_id
