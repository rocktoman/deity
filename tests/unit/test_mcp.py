from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from jsonschema import ValidationError
from mcp.types import TextContent

from deity.adapters.mcp import McpServerConfig, McpSessionManager


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
