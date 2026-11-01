import json
import sys
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.config import McpServerSettings, PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, ModelRequest


class EchoModel:
    def __init__(self) -> None:
        self.turn = 0
        self.output = ""

    async def stream(self, request: ModelRequest):
        self.turn += 1
        if self.turn == 1:
            alias = next(tool["name"] for tool in request.tools if tool["name"].startswith("mcp_"))
            assert ":" not in alias and len(alias) <= 64
            yield ModelEvent("tool_call_started", call_id="mcp-1", name=alias)
            yield ModelEvent("tool_call_arguments_delta", call_id="mcp-1", text='{"text":"hello"}')
        else:
            self.output = next(
                item["content"]
                for item in request.messages
                if item.get("call_id") == "mcp-1" and item["role"] == "tool"
            )
            yield ModelEvent("text_delta", text="done")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_mcp_run_uses_explicit_configuration_authorization(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    approvals: list[str] = []

    def approve(name: str, _args: dict[str, object]) -> bool:
        approvals.append(name)
        return True

    model = EchoModel()
    db_path = tmp_path / "deity.db"
    service = RunService(
        db_path=db_path,
        workspace=tmp_path,
        model=model,
        approve=approve,
        permission=PermissionConfig(shell="auto", workspace_write="auto"),
        mcp_enabled=True,
        mcp_servers=(McpServerSettings("one", sys.executable, (str(script),)),),
        extension_fingerprint="test-v0.4",
    )
    result = await service.run("echo", model_name="fake")
    assert result.final_text == "done" and "hello" in model.output
    assert approvals == []
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        events = await repo.events(result.run_id)
        assert [event["type"] for event in events].index("ToolClaimed") < [
            event["type"] for event in events
        ].index("ToolCompleted")
        claim = next(event for event in events if event["type"] == "ToolClaimed")
        assert claim["payload"]["approval"] == "approved"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_server_crash_after_effect_is_unknown_and_not_retried(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "fixtures/mcp_echo_server.py"
    marker = tmp_path / "effect.log"

    class CrashModel:
        calls = 0

        async def stream(self, request: ModelRequest):
            self.calls += 1
            alias = next(item["name"] for item in request.tools if item["name"].startswith("mcp_"))
            # Select the alias through the name embedded in the advertised description.
            for item in request.tools:
                if "crash_after_write" in item["description"]:
                    alias = item["name"]
                    break
            yield ModelEvent("tool_call_started", call_id="crash-1", name=alias)
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="crash-1",
                text='{"path":' + json.dumps(str(marker)) + "}",
            )
            yield ModelEvent("message_completed")

    model = CrashModel()
    db_path = tmp_path / "deity.db"
    service = RunService(
        db_path=db_path,
        workspace=tmp_path,
        model=model,
        approve=lambda _name, _args: True,
        mcp_enabled=True,
        mcp_servers=(McpServerSettings("one", sys.executable, (str(script),)),),
    )
    with pytest.raises(ExceptionGroup):
        await service.run("crash", model_name="fake")
    assert marker.read_text().count("executed") == 1
    assert model.calls == 1
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        async with repo.db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert (await repo.unresolved_tools(row[0]))[0]["status"] == "unknown"
    finally:
        await repo.close()
