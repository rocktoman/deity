from pathlib import Path

import pytest

from deity.adapters.tools.read_only import ReadOnlyToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall


@pytest.mark.asyncio
async def test_child_dispatcher_denies_shell_and_write(tmp_path: Path) -> None:
    dispatcher = ReadOnlyToolDispatcher(("read_file", "grep"))
    request = AgentRequest("session", "task", str(tmp_path), "inspect", "fake")
    assert {item["name"] for item in dispatcher.definitions()} == {"read_file", "grep"}
    for name, arguments in (
        ("shell", {"argv": ["touch", "bad"]}),
        ("write_file", {"path": "bad", "content": "bad"}),
    ):
        call = ToolCall(name, name, arguments)
        prepared = await dispatcher.prepare(call, request)
        assert prepared is not None and prepared.status == "denied"
        assert (await dispatcher.dispatch(call, request)).status == "denied"
    assert not (tmp_path / "bad").exists()


def test_child_dispatcher_rejects_unsafe_allowlist() -> None:
    with pytest.raises(ValueError):
        ReadOnlyToolDispatcher(("shell",))


@pytest.mark.asyncio
async def test_child_dispatcher_keeps_workspace_boundary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "link.txt").symlink_to(outside)
    dispatcher = ReadOnlyToolDispatcher(("read_file",))
    request = AgentRequest("s", "t", str(root), "inspect", "fake")
    for path in ("../secret.txt", "link.txt"):
        result = await dispatcher.prepare(ToolCall(path, "read_file", {"path": path}), request)
        assert result is not None and result.status == "denied"
