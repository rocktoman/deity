"""Deterministic local stdio MCP server for integration tests."""

import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("deity-test")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text."""
    return text


@server.tool()
def write_marker(path: str) -> str:
    """Append one line to a test marker file."""
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("executed\n")
    return "written"


@server.tool()
def crash_after_write(path: str) -> str:
    """Write a marker then terminate before sending a response."""
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("executed\n")
    os._exit(91)


@server.tool()
def slow_echo(text: str) -> str:
    """Wait before returning text, for cancellation tests."""
    time.sleep(10)
    return text


if __name__ == "__main__":
    server.run("stdio")
