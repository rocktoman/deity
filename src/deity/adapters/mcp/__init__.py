"""Bounded local stdio MCP discovery and invocation."""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from deity.core.contracts import AgentRequest, ToolCall, ToolResult

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class McpCallNotIssued(RuntimeError):
    """The connection failed before the business request could be sent."""


class McpToolChanged(ValueError):
    """A reconnected server changed an advertised tool definition."""


def _check_schema(value: Any, depth: int = 0) -> None:
    """Reject remote references and excessively nested server schemas."""
    if depth > 12:
        raise ValueError("MCP tool schema too deep")
    if isinstance(value, dict):
        if "$ref" in value or "$dynamicRef" in value or "$recursiveRef" in value:
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
        if (
            not _NAME.fullmatch(self.name)
            or not self.command
            or len(self.command) > 4096
            or len(self.args) > 32
            or any(len(arg) > 4096 for arg in self.args)
            or len(self.env_names) > 16
            or not 0 < self.timeout_seconds <= 120
        ):
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
        if len(servers) > 8 or len({item.name for item in servers}) != len(servers):
            raise ValueError("duplicate MCP server")
        self.servers = {item.name: item for item in servers}

    @staticmethod
    def _params(config: McpServerConfig) -> StdioServerParameters:
        env = {name: os.environ[name] for name in config.env_names if name in os.environ}
        return StdioServerParameters(
            command=config.command,
            args=list(config.args),
            cwd=config.cwd,
            env=env,
        )

    @classmethod
    @asynccontextmanager
    async def _connect(cls, config: McpServerConfig) -> AsyncIterator[Client]:
        """Keep server stderr out of CLI output and persisted evidence."""
        with open(os.devnull, "w", encoding="utf-8") as errlog:  # noqa: ASYNC230
            async with Client(stdio_client(cls._params(config), errlog=errlog)) as client:
                yield client

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
            async with self._connect(config) as client:
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
        issued = False

        async def invoke() -> ToolResult:
            nonlocal issued
            async with self._connect(config) as client:
                listed = await self._list_all(client)
                current = next((item for item in listed if item.name == tool.name), None)
                if (
                    current is None
                    or current.input_schema != tool.schema
                    or (current.description or "")[:2000] != tool.description
                ):
                    raise McpToolChanged("MCP tool changed after discovery")
                issued = True
                result = await client.call_tool(tool.name, arguments)
            output = "\n".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            for name in config.env_names:
                secret = os.environ.get(name)
                if secret:
                    output = output.replace(secret, "[REDACTED]")
            return ToolResult(
                call_id,
                "error" if result.is_error else "ok",
                output[:16_000],
                truncated=len(output) > 16_000,
            )

        try:
            return await asyncio.wait_for(invoke(), config.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except McpToolChanged:
            raise
        except Exception as exc:
            if not issued:
                raise McpCallNotIssued(type(exc).__name__) from None
            raise


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
        self._prepared: dict[tuple[str, str], str] = {}

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
            arguments_json = json.dumps(
                call.arguments, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            if len(arguments_json) > 16_000:
                return ToolResult(call.call_id, "error", "invalid_arguments")
            Draft202012Validator(tool.schema).validate(call.arguments)
        except (TypeError, ValueError, ValidationError, UnicodeError):
            return ToolResult(call.call_id, "error", "invalid_arguments")
        if self.approve is None or not await asyncio.to_thread(
            self.approve,
            "mcp_tool",
            {
                "server": tool.server,
                "tool": tool.name,
                "arguments": json.loads(arguments_json),
            },
        ):
            return ToolResult(call.call_id, "denied", "permission_denied")
        self._prepared[(context.run_id, call.call_id)] = arguments_json
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            result: ToolResult = await self.local.dispatch(call, context)
            return result
        key = (context.run_id, call.call_id)
        prepared_json = self._prepared.pop(key, None)
        if prepared_json is None:
            return ToolResult(call.call_id, "denied", "approval_required")
        try:
            current_json = json.dumps(
                call.arguments, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
        except (TypeError, ValueError, UnicodeError):
            return ToolResult(call.call_id, "denied", "approval_changed")
        if current_json != prepared_json:
            return ToolResult(call.call_id, "denied", "approval_changed")
        try:
            return await self.manager.call(tool, json.loads(prepared_json), call.call_id)
        except McpCallNotIssued:
            return ToolResult(call.call_id, "error", "mcp_call_not_issued")
