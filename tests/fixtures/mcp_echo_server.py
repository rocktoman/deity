"""Deterministic local stdio MCP server for integration tests."""

import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("deity-test")

if os.environ.get("DEITY_TEST_MCP_STDERR_MARKER"):
    print(os.environ["DEITY_TEST_MCP_STDERR_MARKER"], file=sys.stderr, flush=True)


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


@server.tool()
def echo_secret() -> str:
    """Return the test allowlisted environment value."""
    return os.environ.get("DEITY_TEST_MCP_STDERR_MARKER", "")


if __name__ == "__main__":
    server.run("stdio")
