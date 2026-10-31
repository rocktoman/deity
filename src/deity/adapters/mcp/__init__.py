"""Bounded local stdio MCP discovery and invocation."""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from deity.core.contracts import AgentRequest, ToolCall, ToolResult

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _check_schema(value: Any, depth: int = 0) -> None:
    """Reject remote references and excessively nested server schemas."""
    if depth > 12:
        raise ValueError("MCP tool schema too deep")
    if isinstance(value, dict):
        if "$ref" in value:
            raise ValueError("MCP tool schema references are unsupported")
        for nested in value.values():
            _check_schema(nested, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError("MCP tool schema array too large")
        for nested in value:
            _check_schema(nested, depth + 1)


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env_names: tuple[str, ...] = ()
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) or not self.command or self.timeout_seconds <= 0:
            raise ValueError("invalid MCP server configuration")


@dataclass(frozen=True)
class McpTool:
    server: str
    name: str
    description: str
    schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"mcp:{self.server}:{self.name}"


class McpSessionManager:
    """Use a fresh bounded connection per discovery or call."""

    def __init__(self, servers: tuple[McpServerConfig, ...]) -> None:
        if len({item.name for item in servers}) != len(servers):
            raise ValueError("duplicate MCP server")
        self.servers = {item.name: item for item in servers}

    @staticmethod
    def _params(config: McpServerConfig) -> StdioServerParameters:
        import os

        env = {name: os.environ[name] for name in config.env_names if name in os.environ}
        return StdioServerParameters(
            command=config.command,
            args=list(config.args),
            cwd=config.cwd,
            env=env,
        )

    @staticmethod
    async def _list_all(client: Client) -> list[Any]:
        listed = await client.list_tools()
        all_tools = list(listed.tools)
        cursors: set[str] = set()
        cursor = getattr(listed, "next_cursor", None)
        while cursor is not None:
            if cursor in cursors or len(cursors) >= 4 or len(all_tools) > 64:
                raise ValueError("MCP tool pagination exceeded limit")
            cursors.add(cursor)
            listed = await client.list_tools(cursor=cursor)
            all_tools.extend(listed.tools)
            cursor = getattr(listed, "next_cursor", None)
        if len(all_tools) > 64:
            raise ValueError("too many MCP tools")
        return all_tools

    async def discover(self, server: str) -> tuple[McpTool, ...]:
        config = self.servers[server]

        async def collect() -> tuple[McpTool, ...]:
            async with Client(self._params(config)) as client:
                all_tools = await self._list_all(client)
            found: list[McpTool] = []
            names: set[str] = set()
            for tool in all_tools:
                if not _NAME.fullmatch(tool.name) or tool.name in names:
                    raise ValueError("invalid or duplicate MCP tool name")
                names.add(tool.name)
                schema = tool.input_schema
                if len(json.dumps(schema, ensure_ascii=False)) > 16_000:
                    raise ValueError("MCP tool schema too large")
                _check_schema(schema)
                Draft202012Validator.check_schema(schema)
                if schema.get("type") != "object":
                    raise ValueError("MCP tool arguments must be an object")
                found.append(McpTool(server, tool.name, (tool.description or "")[:2000], schema))
            return tuple(found)

        return await asyncio.wait_for(collect(), config.timeout_seconds)

    async def call(self, tool: McpTool, arguments: dict[str, Any], call_id: str) -> ToolResult:
        config = self.servers[tool.server]
        Draft202012Validator(tool.schema).validate(arguments)

        async def invoke() -> ToolResult:
            async with Client(self._params(config)) as client:
                listed = await self._list_all(client)
                current = next((item for item in listed if item.name == tool.name), None)
                if current is None or current.input_schema != tool.schema:
                    raise ValueError("MCP tool changed after discovery")
                result = await client.call_tool(tool.name, arguments)
            output = "\n".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            return ToolResult(
                call_id,
                "error" if result.is_error else "ok",
                output[:16_000],
                truncated=len(output) > 16_000,
            )

        return await asyncio.wait_for(invoke(), config.timeout_seconds)


class McpCompositeDispatcher:
    """Keep MCP tools on the same durable loop while requiring per-call approval."""

    def __init__(
        self,
        local: Any,
        manager: McpSessionManager,
        tools: tuple[McpTool, ...],
        approve: Callable[[str, dict[str, Any]], bool] | None,
    ) -> None:
        names = [tool.qualified_name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate MCP tool")
        self.local = local
        self.manager = manager
        self.tools = {tool.qualified_name: tool for tool in tools}
        self.approve = approve
        self._prepared: set[tuple[str, str]] = set()

    def definitions(self) -> tuple[dict[str, Any], ...]:
        local: tuple[dict[str, Any], ...] = self.local.definitions()
        return local + tuple(
            {
                "name": tool.qualified_name,
                "description": f"{tool.qualified_name}: {tool.description}",
                "parameters": tool.schema,
            }
            for tool in self.tools.values()
        )

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        """Validate complete MCP arguments and authorize before durable intent."""
        tool = self.tools.get(call.name)
        if tool is None:
            result: ToolResult | None = await self.local.prepare(call, context)
            return result
        try:
            if len(json.dumps(call.arguments, ensure_ascii=False)) > 16_000:
                return ToolResult(call.call_id, "error", "invalid_arguments")
            Draft202012Validator(tool.schema).validate(call.arguments)
        except (ValidationError, UnicodeError):
            return ToolResult(call.call_id, "error", "invalid_arguments")
        if self.approve is None or not await asyncio.to_thread(
            self.approve,
            "mcp_tool",
            {"server": tool.server, "tool": tool.name, "arguments": call.arguments},
        ):
            return ToolResult(call.call_id, "denied", "permission_denied")
        self._prepared.add((context.run_id, call.call_id))
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            result: ToolResult = await self.local.dispatch(call, context)
            return result
        key = (context.run_id, call.call_id)
        if key not in self._prepared:
            return ToolResult(call.call_id, "denied", "approval_required")
        self._prepared.remove(key)
        return await self.manager.call(tool, call.arguments, call.call_id)
