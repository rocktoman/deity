from pathlib import Path

import pytest

from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest, RunStatus


class FinalModel:
    async def stream(self, request: ModelRequest):
        yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_service_runs_and_persists(tmp_path: Path) -> None:
    service = RunService(db_path=tmp_path / "deity.db", workspace=tmp_path, model=FinalModel())
    result = await service.run("hello", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "done"
    from deity.adapters.storage import SQLiteSessionRepository

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    assert len(await repo.sessions()) == 1
    await repo.close()


@pytest.mark.asyncio
async def test_deny_policy_cannot_be_overridden_by_approval(tmp_path: Path) -> None:
    class WriteModel:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="w", name="write_file")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="w",
                    text='{"path":"a.txt","content":"bad"}',
                )
            else:
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=WriteModel(),
        approve=lambda name, args: True,
        permission=PermissionConfig(workspace_write="deny"),
    )
    await service.run("write", model_name="fake")
    assert not (tmp_path / "a.txt").exists()
