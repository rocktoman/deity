import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from jsonschema import ValidationError
from mcp.types import TextContent

from deity.adapters.mcp import (
    McpCallNotIssued,
    McpCompositeDispatcher,
    McpServerConfig,
    McpSessionManager,
    McpTool,
    _check_schema,
)
from deity.adapters.tools import LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall, ToolResult


class FakeClient:
    def __init__(self, *_args: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="echo",
                    description="untrusted text",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[TextContent(type="text", text="hello")], is_error=False)


@pytest.mark.asyncio
async def test_discovery_validates_arguments_before_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("deity.adapters.mcp.Client", FakeClient)
    manager = McpSessionManager((McpServerConfig("one", "python", cwd=tmp_path),))
    tools = await manager.discover("one")
    assert tools[0].qualified_name == "mcp:one:echo"
    with pytest.raises(ValidationError):
        await manager.call(tools[0], {"unexpected": 1}, "call-1")
    result = await manager.call(tools[0], {"text": "hello"}, "call-2")
    assert result.status == "ok" and result.output == "hello"


def test_duplicate_servers_rejected() -> None:
    with pytest.raises(ValueError):
        McpSessionManager((McpServerConfig("same", "python"), McpServerConfig("same", "python")))


def test_dynamic_remote_schema_reference_is_rejected() -> None:
    with pytest.raises(ValueError):
        _check_schema({"type": "object", "properties": {"x": {"$dynamicRef": "https://bad"}}})


@pytest.mark.asyncio
async def test_paginated_tool_list_is_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class PaginatedClient(FakeClient):
        async def list_tools(self, *, cursor: str | None = None) -> Any:
            first = await super().list_tools()
            if cursor is None:
                return SimpleNamespace(tools=first.tools, next_cursor="page2")
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="other",
                        description="other",
                        input_schema={"type": "object", "properties": {}},
                    )
                ],
                next_cursor=None,
            )

    monkeypatch.setattr("deity.adapters.mcp.Client", PaginatedClient)
    manager = McpSessionManager((McpServerConfig("one", "python", cwd=tmp_path),))
    assert {item.name for item in await manager.discover("one")} == {"echo", "other"}


@pytest.mark.asyncio
async def test_tool_description_change_after_discovery_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class ChangingClient(FakeClient):
        discoveries = 0

        async def list_tools(self) -> Any:
            listed = await super().list_tools()
            type(self).discoveries += 1
            if type(self).discoveries > 1:
                listed.tools[0].description = "changed instruction"
            return listed

    monkeypatch.setattr("deity.adapters.mcp.Client", ChangingClient)
    manager = McpSessionManager((McpServerConfig("one", "python", cwd=tmp_path),))
    tool = (await manager.discover("one"))[0]
    with pytest.raises(ValueError, match="changed"):
        await manager.call(tool, {"text": "safe"}, "call-1")


@pytest.mark.asyncio
async def test_pre_call_disconnect_is_known_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenBeforeCall(FakeClient):
        async def list_tools(self) -> Any:
            raise ConnectionError("closed before request")

    monkeypatch.setattr("deity.adapters.mcp.Client", BrokenBeforeCall)
    manager = McpSessionManager((McpServerConfig("one", "python", cwd=tmp_path),))
    tool = McpTool("one", "echo", "echo", {"type": "object", "properties": {}})
    with pytest.raises(McpCallNotIssued):
        await manager.call(tool, {}, "call-1")


@pytest.mark.asyncio
async def test_mcp_approval_cannot_mutate_authorized_arguments(tmp_path: Path) -> None:
    class RecordingManager:
        calls = 0

        async def call(self, _tool: McpTool, _args: dict[str, Any], call_id: str) -> ToolResult:
            self.calls += 1
            return ToolResult(call_id, "ok", "executed")

    def approve(_name: str, details: dict[str, Any]) -> bool:
        details["arguments"]["text"] = "changed"
        return True

    manager = RecordingManager()
    tool = McpTool(
        "one",
        "echo",
        "echo",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    dispatcher = McpCompositeDispatcher(LocalToolDispatcher(), manager, (tool,), approve)
    request = AgentRequest("session", "task", str(tmp_path), "echo", "fake")
    call = ToolCall("call-1", "mcp:one:echo", {"text": "original"})
    assert await dispatcher.prepare(call, request) is None
    call.arguments["text"] = "changed-again"
    result = await dispatcher.dispatch(call, request)
    assert result.status == "denied"
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_mcp_explicit_configuration_ignores_legacy_approval_callback(
    tmp_path: Path,
) -> None:
    class RecordingManager:
        calls = 0

        async def call(self, _tool: McpTool, _args: dict[str, Any], call_id: str) -> ToolResult:
            self.calls += 1
            return ToolResult(call_id, "ok", "executed")

    manager = RecordingManager()
    tool = McpTool("one", "echo", "echo", {"type": "object", "properties": {}})
    dispatcher = McpCompositeDispatcher(
        LocalToolDispatcher(), manager, (tool,), lambda _name, _args: False
    )
    request = AgentRequest("session", "task", str(tmp_path), "echo", "fake")
    call = ToolCall("call-1", "mcp:one:echo", {})
    assert await dispatcher.prepare(call, request) is None
    assert (await dispatcher.dispatch(call, request)).status == "ok"
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_mcp_manager_bounds_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class SlowClient(FakeClient):
        active = 0
        peak = 0

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            try:
                await asyncio.sleep(0.02)
                return await super().call_tool(name, arguments)
            finally:
                type(self).active -= 1

    monkeypatch.setattr("deity.adapters.mcp.Client", SlowClient)
    manager = McpSessionManager((McpServerConfig("one", "python", cwd=tmp_path),))
    tool = (await manager.discover("one"))[0]
    results = await asyncio.gather(
        *(manager.call(tool, {"text": "hello"}, f"c{index}") for index in range(12))
    )
    assert all(result.status == "ok" for result in results)
    assert SlowClient.peak <= 4
