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


@pytest.mark.asyncio
async def test_auto_permission_completes_write_and_safe_shell_without_prompt(
    tmp_path: Path,
) -> None:
    class ToolModel:
        def __init__(self) -> None:
            self.turn = 0
            self.results: list[str] = []

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="w", name="write_file")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="w",
                    text='{"path":"bubble.py","content":"print(1)"}',
                )
                yield ModelEvent("tool_call_started", call_id="s", name="shell")
                yield ModelEvent("tool_call_arguments_delta", call_id="s", text='{"argv":["pwd"]}')
            else:
                self.results = [m["status"] for m in request.messages if m["role"] == "tool"]
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = ToolModel()

    def unexpected_approval(name: str, args: dict[str, object]) -> bool:
        raise AssertionError("auto mode must not prompt")

    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        approve=unexpected_approval,
        permission=PermissionConfig(shell="auto", workspace_write="auto"),
    )
    result = await service.run("write bubble sort", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert (tmp_path / "bubble.py").read_text() == "print(1)"
    assert model.results == ["ok", "ok"]


@pytest.mark.asyncio
async def test_auto_permission_rejects_destructive_shell(tmp_path: Path) -> None:
    class ShellModel:
        def __init__(self) -> None:
            self.turn = 0
            self.status = ""

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="s", name="shell")
                yield ModelEvent(
                    "tool_call_arguments_delta", call_id="s", text='{"argv":["rm","-rf","."]}'
                )
            else:
                self.status = next(m["status"] for m in request.messages if m["role"] == "tool")
                yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    model = ShellModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        permission=PermissionConfig(shell="auto"),
    )
    await service.run("delete", model_name="fake")
    assert model.status == "denied"


@pytest.mark.asyncio
async def test_auto_permission_deletes_explicitly_named_file(tmp_path: Path) -> None:
    target = tmp_path / "bubble_sort.py"
    target.write_text("print(1)")

    class DeleteModel:
        def __init__(self) -> None:
            self.turn = 0
            self.status = ""

        async def stream(self, request: ModelRequest):
            self.turn += 1
            if self.turn == 1:
                yield ModelEvent("tool_call_started", call_id="d", name="delete_file")
                yield ModelEvent(
                    "tool_call_arguments_delta", call_id="d", text='{"path":"bubble_sort.py"}'
                )
            else:
                self.status = next(m["status"] for m in request.messages if m["role"] == "tool")
                yield ModelEvent("text_delta", text="deleted")
            yield ModelEvent("message_completed")

    model = DeleteModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        permission=PermissionConfig(workspace_write="auto"),
    )
    result = await service.run("删除 bubble_sort.py", model_name="fake")
    assert result.status == RunStatus.COMPLETED
    assert model.status == "ok"
    assert not target.exists()


@pytest.mark.asyncio
async def test_auto_permission_deletes_file_resolved_from_previous_turn(tmp_path: Path) -> None:
    class ConversationModel:
        def __init__(self) -> None:
            self.saw_history = False
            self.delete_status = ""

        async def stream(self, request: ModelRequest):
            messages = request.messages
            if messages[-1]["role"] == "tool":
                if messages[-1]["call_id"] == "created-file":
                    yield ModelEvent("text_delta", text="已创建 bubble_sort.py")
                else:
                    self.delete_status = messages[-1]["status"]
                    yield ModelEvent("text_delta", text="已删除")
            elif messages[-1]["content"] == "帮我创建冒泡排序文件":
                yield ModelEvent("tool_call_started", call_id="created-file", name="write_file")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="created-file",
                    text='{"path":"bubble_sort.py","content":"print(1)"}',
                )
            else:
                self.saw_history = any(
                    item.get("content") == "已创建 bubble_sort.py" for item in messages
                )
                yield ModelEvent("tool_call_started", call_id="deleted-file", name="delete_file")
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="deleted-file",
                    text='{"path":"bubble_sort.py"}',
                )
            yield ModelEvent("message_completed")

    model = ConversationModel()
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,
        permission=PermissionConfig(workspace_write="auto"),
    )
    await service.run("帮我创建冒泡排序文件", model_name="fake", session_id="same-session")
    assert (tmp_path / "bubble_sort.py").exists()
    result = await service.run("删除刚才新建的文件", model_name="fake", session_id="same-session")
    assert result.status == RunStatus.COMPLETED
    assert model.saw_history
    assert model.delete_status == "ok"
    assert not (tmp_path / "bubble_sort.py").exists()
