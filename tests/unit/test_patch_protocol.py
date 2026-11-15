"""F1-A1：多文件 patch 协议（ADR-018）——解析、四级模糊定位、先校验后落盘。"""

from pathlib import Path
from typing import Any

import pytest

from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.git_guard import GitGuardedDispatcher
from deity.adapters.tools.patch import (
    AddFile,
    DeleteFile,
    PatchError,
    UpdateFile,
    parse_patch,
    plan_patch,
    seek_sequence,
)
from deity.core.contracts import AgentRequest, ToolCall
from deity.core.contracts.workspace import WorkspaceFileState, WorkspaceSnapshot


def _request(tmp_path: Path) -> AgentRequest:
    return AgentRequest("s", "t", str(tmp_path), "task", "fake", run_id="run")


def _tools() -> LocalToolDispatcher:
    return LocalToolDispatcher(approve=lambda _name, _args: True)


MULTI_PATCH = """\
*** Begin Patch
*** Add File: added.py
+value = 1
+
+value = 2
*** Update File: calc.py
*** Move to: renamed.py
@@ def add(a, b):
     return a - b
*** Delete File: gone.py
*** End Patch
"""


def test_parse_add_update_move_delete() -> None:
    hunks = parse_patch(MULTI_PATCH)
    assert isinstance(hunks[0], AddFile) and hunks[0].lines == ["value = 1", "", "value = 2"]
    assert isinstance(hunks[1], UpdateFile) and hunks[1].move_to == "renamed.py"
    assert hunks[1].chunks[0].context == "def add(a, b):"
    assert hunks[1].chunks[0].old_lines == ["    return a - b"]
    assert isinstance(hunks[2], DeleteFile)


def test_parse_rejects_missing_markers_and_garbage() -> None:
    with pytest.raises(PatchError):
        parse_patch("*** Update File: a.py\n x\n")
    with pytest.raises(PatchError):
        parse_patch("*** Begin Patch\n*** Update File: a.py\n?bad\n*** End Patch\n")
    with pytest.raises(PatchError):
        parse_patch("*** Begin Patch\n*** End Patch\n")


def test_seek_four_levels() -> None:
    lines = ["alpha", "beta  ", "  gamma", "delta — dash", "it's ok"]
    assert seek_sequence(lines, ["alpha"], 0, False) == 0
    assert seek_sequence(lines, ["beta"], 0, False) == 1  # 行尾空白
    assert seek_sequence(lines, ["gamma"], 0, False) == 2  # 首尾空白
    assert seek_sequence(lines, ["delta - dash"], 0, False) == 3  # Unicode 标点
    assert seek_sequence(lines, ["it‘s ok"], 0, False) == 4  # 弯引号归一
    assert seek_sequence(lines, ["missing"], 0, False) is None


def test_plan_rejects_duplicates_and_missing_targets() -> None:
    files: dict[str, str | None] = {"a.py": "x = 1\n"}
    read = files.get
    with pytest.raises(PatchError):
        plan_patch(
            "*** Begin Patch\n*** Update File: a.py\n x = 1\n+x = 2\n"
            "*** Update File: a.py\n x = 1\n+x = 3\n*** End Patch\n",
            read,
        )
    with pytest.raises(PatchError):
        plan_patch("*** Begin Patch\n*** Delete File: nope.py\n*** End Patch\n", read)
    with pytest.raises(PatchError):
        plan_patch("*** Begin Patch\n*** Add File: a.py\n+x\n*** End Patch\n", read)


def test_plan_multi_chunk_and_insertion_and_eof() -> None:
    original = "one\ntwo\nthree\nfour\nfive\n"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ one\n"
        "-two\n"
        "+TWO\n"
        "@@ four\n"
        "+inserted\n"
        " five\n"
        "*** End of File\n"
        "*** End Patch\n"
    )
    changes = plan_patch(patch, {"f.txt": original}.get)
    assert changes[0].new_content == "one\nTWO\nthree\nfour\ninserted\nfive\n"


@pytest.mark.asyncio
async def test_dispatcher_patch_form_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("old\n", encoding="utf-8")
    tools = _tools()
    result = await tools.dispatch(
        ToolCall("p", "apply_patch", {"patch": MULTI_PATCH}), _request(tmp_path)
    )
    assert result.status == "ok", result.output
    assert result.output.startswith("patched 3 file(s)")
    assert (tmp_path / "added.py").read_text(encoding="utf-8") == "value = 1\n\nvalue = 2\n"
    assert not (tmp_path / "calc.py").exists()
    assert (tmp_path / "renamed.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a - b\n"
    )
    assert not (tmp_path / "gone.py").exists()


@pytest.mark.asyncio
async def test_dispatcher_patch_is_atomic_on_validation_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "keep.py"
    target.write_text("stable = 1\n", encoding="utf-8")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: keep.py\n"
        "-stable = 1\n"
        "+stable = 2\n"
        "*** Update File: ghost.py\n"
        "-boo\n"
        "+bah\n"
        "*** End Patch\n"
    )
    result = await _tools().dispatch(
        ToolCall("p", "apply_patch", {"patch": patch}), _request(tmp_path)
    )
    assert result.status == "error" and "patch_invalid" in result.output
    assert target.read_text(encoding="utf-8") == "stable = 1\n"


@pytest.mark.asyncio
async def test_dispatcher_patch_rejects_workspace_escape(tmp_path: Path) -> None:
    patch = "*** Begin Patch\n*** Add File: ../evil.py\n+x = 1\n*** End Patch\n"
    result = await _tools().dispatch(
        ToolCall("p", "apply_patch", {"patch": patch}), _request(tmp_path)
    )
    assert result.status == "error" and "patch_invalid" in result.output
    assert not (tmp_path.parent / "evil.py").exists()


@pytest.mark.asyncio
async def test_legacy_form_still_works_and_mixed_form_rejected(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    tools = _tools()
    legacy = await tools.dispatch(
        ToolCall("p1", "apply_patch", {"path": "a.py", "old": "x = 1", "new": "x = 2"}),
        _request(tmp_path),
    )
    assert legacy.status == "ok" and legacy.output == "patched"
    assert target.read_text(encoding="utf-8") == "x = 2\n"
    mixed = await tools.dispatch(
        ToolCall(
            "p2",
            "apply_patch",
            {"patch": "*** Begin Patch\n*** End Patch\n", "old": "x", "path": "a.py", "new": "y"},
        ),
        _request(tmp_path),
    )
    assert mixed.status == "error" and mixed.output == "invalid_arguments"


class _Repo:
    def __init__(self) -> None:
        self.changes: list[Any] = []

    async def ensure_change_set(self, run_id: str, snapshot_id: str) -> str:
        return "cs-1"

    async def record_file_change(self, change_set_id: str, run_id: str, change: Any) -> None:
        self.changes.append(change)


def _guard(tmp_path: Path, repo: _Repo) -> GitGuardedDispatcher:
    snapshot = WorkspaceSnapshot(
        "snapshot",
        str(tmp_path),
        "head",
        (WorkspaceFileState("calc.py", "clean"),),
        "digest",
        "test",
        "2026-01-01T00:00:00+00:00",
    )
    return GitGuardedDispatcher(
        LocalToolDispatcher(approve=lambda _name, _args: True), repo, snapshot
    )


@pytest.mark.asyncio
async def test_guard_patch_requires_read_before_write(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("return a - b\n", encoding="utf-8")
    guard = _guard(tmp_path, _Repo())
    patch = (
        "*** Begin Patch\n*** Update File: calc.py\n-return a - b\n+return a + b\n*** End Patch\n"
    )
    denied = await guard.prepare(ToolCall("p", "apply_patch", {"patch": patch}), _request(tmp_path))
    assert denied is not None and denied.output == "read_before_write_required"


@pytest.mark.asyncio
async def test_guard_patch_applies_and_records_each_file(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("return a - b\n", encoding="utf-8")
    repo = _Repo()
    guard = _guard(tmp_path, repo)
    request = _request(tmp_path)
    read = await guard.dispatch(ToolCall("r", "read_file", {"path": "calc.py"}), request)
    assert read.status == "ok"
    patch = (
        "*** Begin Patch\n*** Update File: calc.py\n"
        "-return a - b\n+return a + b\n"
        "*** Add File: extra.py\n+new = 1\n*** End Patch\n"
    )
    call = ToolCall("p", "apply_patch", {"patch": patch})
    assert await guard.prepare(call, request) is None
    result = await guard.dispatch(call, request)
    assert result.status == "ok", result.output
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "return a + b\n"
    assert (tmp_path / "extra.py").read_text(encoding="utf-8") == "new = 1\n"
    await guard.after_persist(call, result, request)
    recorded = {(change.path, change.operation) for change in repo.changes}
    assert recorded == {("calc.py", "modify"), ("extra.py", "create")}


@pytest.mark.asyncio
async def test_guard_patch_preimage_drift_denied(tmp_path: Path) -> None:
    target = tmp_path / "calc.py"
    target.write_text("return a - b\n", encoding="utf-8")
    guard = _guard(tmp_path, _Repo())
    request = _request(tmp_path)
    await guard.dispatch(ToolCall("r", "read_file", {"path": "calc.py"}), request)
    patch = (
        "*** Begin Patch\n*** Update File: calc.py\n-return a - b\n+return a + b\n*** End Patch\n"
    )
    call = ToolCall("p", "apply_patch", {"patch": patch})
    assert await guard.prepare(call, request) is None
    target.write_text("return a * b\n", encoding="utf-8")  # prepare 后外部漂移
    result = await guard.dispatch(call, request)
    assert result.status == "denied" and result.output == "preimage_mismatch"
    assert target.read_text(encoding="utf-8") == "return a * b\n"
