import asyncio
import sys
from pathlib import Path

import pytest

from deity.adapters.tools import LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall


@pytest.mark.asyncio
async def test_read_patch_test_and_diff_in_fixture_repo(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec("git", "init", "-q", str(tmp_path))
    assert await process.wait() == 0
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    request = AgentRequest("s", "t", str(tmp_path), "fix", "fake")
    tools = LocalToolDispatcher(approve=lambda name, args: True)
    read = await tools.dispatch(ToolCall("r", "read_file", {"path": "calc.py"}), request)
    assert "a - b" in read.output
    patched = await tools.dispatch(
        ToolCall("p", "apply_patch", {"path": "calc.py", "old": "a - b", "new": "a + b"}),
        request,
    )
    assert patched.status == "ok"
    tested = await tools.dispatch(
        ToolCall("s", "shell", {"argv": [sys.executable, "-m", "pytest", "-q", "test_calc.py"]}),
        request,
    )
    assert tested.status == "ok"
    diff = await tools.dispatch(ToolCall("d", "git_diff", {}), request)
    assert diff.status == "ok"
    assert [entry[0] for entry in tools.audit] == ["r", "p", "s", "d"]
