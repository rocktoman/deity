import asyncio
from pathlib import Path

import pytest

from deity.adapters.tools import LocalToolDispatcher, auto_shell_allowed
from deity.core.contracts import AgentRequest, ToolCall


def request(root: Path) -> AgentRequest:
    return AgentRequest("s", "t", str(root), "task", "fake")


@pytest.mark.parametrize(
    "argv,allowed",
    [
        (["uv", "run", "pytest", "-q"], True),
        (["python3", "-m", "pytest"], True),
        (["ruff", "check", ".", "--fix"], False),
        (["ruff", "check", ".", "--fix-only"], False),
        (["/tmp/pytest"], False),
        (["python3", "-c", "print(1)"], False),
        (["sh", "-c", "rm -rf ."], False),
        (["git", "push"], False),
        (["rm", "-rf", "."], False),
        (["python3", "../outside.py"], False),
    ],
)
def test_auto_shell_policy(tmp_path: Path, argv: list[str], allowed: bool) -> None:
    assert auto_shell_allowed({"argv": argv}, tmp_path) is allowed


@pytest.mark.asyncio
async def test_read_and_outside_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    tools = LocalToolDispatcher()
    good = await tools.dispatch(ToolCall("1", "read_file", {"path": "a.txt"}), request(tmp_path))
    bad = await tools.dispatch(
        ToolCall("2", "read_file", {"path": "../outside"}), request(tmp_path)
    )
    assert good.status == "ok" and good.output == "hello"
    assert bad.status == "denied"


@pytest.mark.asyncio
async def test_write_requires_approval_and_preserves_existing(tmp_path: Path) -> None:
    tools = LocalToolDispatcher()
    call = ToolCall("1", "write_file", {"path": "a.txt", "content": "new"})
    assert (await tools.dispatch(call, request(tmp_path))).status == "denied"
    assert not (tmp_path / "a.txt").exists()
    approved = LocalToolDispatcher(approve=lambda name, args: True)
    assert (await approved.dispatch(call, request(tmp_path))).status == "ok"
    again = await approved.dispatch(
        ToolCall("2", "write_file", {"path": "a.txt", "content": "overwrite"}), request(tmp_path)
    )
    assert again.status == "error"
    assert (tmp_path / "a.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_invalid_unicode_write_is_rejected_before_file_creation(tmp_path: Path) -> None:
    tools = LocalToolDispatcher(approve=lambda name, args: True)
    result = await tools.dispatch(
        ToolCall("invalid", "write_file", {"path": "bad.py", "content": "x\ud800"}),
        request(tmp_path),
    )
    assert result.status == "error"
    assert result.output == "invalid_unicode"
    assert not (tmp_path / "bad.py").exists()


@pytest.mark.asyncio
async def test_patch_conflict_keeps_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("current")
    tools = LocalToolDispatcher(approve=lambda name, args: True)
    result = await tools.dispatch(
        ToolCall("1", "apply_patch", {"path": "a.txt", "old": "stale", "new": "next"}),
        request(tmp_path),
    )
    assert result.status == "error"
    assert path.read_text() == "current"


@pytest.mark.asyncio
async def test_shell_timeout(tmp_path: Path) -> None:
    tools = LocalToolDispatcher(approve=lambda name, args: True, timeout_seconds=0.1)
    result = await tools.dispatch(
        ToolCall("1", "shell", {"argv": ["python3", "-c", "import time;time.sleep(5)"]}),
        request(tmp_path),
    )
    assert result.status == "timeout"


@pytest.mark.asyncio
async def test_symlink_escape_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-file.txt"
    (tmp_path / "link").symlink_to(outside)
    tools = LocalToolDispatcher(approve=lambda name, args: True)
    result = await tools.dispatch(
        ToolCall("1", "write_file", {"path": "link", "content": "x"}), request(tmp_path)
    )
    assert result.status == "denied"


@pytest.mark.asyncio
async def test_cancelled_shell_stops(tmp_path: Path) -> None:
    tools = LocalToolDispatcher(approve=lambda name, args: True)
    task = asyncio.create_task(
        tools.dispatch(
            ToolCall("1", "shell", {"argv": ["python3", "-c", "import time;time.sleep(5)"]}),
            request(tmp_path),
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_read_only_tools_and_bad_arguments(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n")
    tools = LocalToolDispatcher()
    context = request(tmp_path)
    listed = await tools.dispatch(ToolCall("l", "list_files", {"path": "."}), context)
    globbed = await tools.dispatch(ToolCall("g", "glob", {"pattern": "*.txt"}), context)
    searched = await tools.dispatch(ToolCall("s", "grep", {"pattern": "hello"}), context)
    invalid = await tools.dispatch(
        ToolCall("i", "read_file", {"path": "a.txt", "extra": 1}), context
    )
    unknown = await tools.dispatch(ToolCall("u", "missing", {}), context)
    duplicate = await tools.dispatch(ToolCall("l", "list_files", {"path": "."}), context)
    assert "a.txt" in listed.output and "a.txt" in globbed.output
    assert "hello" in searched.output
    assert [invalid.output, unknown.output, duplicate.output] == [
        "invalid_arguments",
        "unknown_tool",
        "duplicate_call_id",
    ]


@pytest.mark.asyncio
async def test_shell_output_is_truncated(tmp_path: Path) -> None:
    tools = LocalToolDispatcher(approve=lambda name, args: True, max_output_chars=32)
    result = await tools.dispatch(
        ToolCall("o", "shell", {"argv": ["python3", "-c", "print('x' * 10000)"]}),
        request(tmp_path),
    )
    assert result.status == "ok"
    assert result.truncated is True
    assert len(result.output) <= 32
