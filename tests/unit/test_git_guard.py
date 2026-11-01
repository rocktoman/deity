import hashlib
from pathlib import Path

import pytest

from deity.adapters.git import SafePatchApplier
from deity.adapters.tools import LocalToolDispatcher
from deity.adapters.tools.git_guard import GitGuardedDispatcher
from deity.core.contracts import AgentRequest, ToolCall
from deity.core.contracts.workspace import (
    PatchApplyStatus,
    WorkspaceFileState,
    WorkspaceSnapshot,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_safe_patch_preserves_crlf_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_bytes(b"\xef\xbb\xbfdef value():\r\n    return 1\r\n")
    before = _sha(path)
    result = SafePatchApplier(tmp_path).apply(
        "sample.py", "return 1\n", "return 2\n", expected_sha256=before
    )
    assert result.status == PatchApplyStatus.APPLIED
    assert path.read_bytes() == b"\xef\xbb\xbfdef value():\r\n    return 2\r\n"
    assert result.before_sha256 == before and result.after_sha256 == _sha(path)


def test_safe_patch_conflict_and_preimage_mismatch_leave_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    original = path.read_bytes()
    duplicate = SafePatchApplier(tmp_path).apply(
        "sample.py", "value = 1", "value = 2", expected_sha256=_sha(path)
    )
    assert duplicate.status == PatchApplyStatus.CONFLICT
    assert path.read_bytes() == original

    mismatch = SafePatchApplier(tmp_path).apply(
        "sample.py", "value = 1\nvalue = 1", "changed", expected_sha256="0" * 64
    )
    assert mismatch.status == PatchApplyStatus.CONFLICT
    assert mismatch.error_code == "preimage_mismatch"
    assert path.read_bytes() == original


def test_safe_patch_rejects_binary_symlink_and_protected_path(tmp_path: Path) -> None:
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\x00old")
    binary_result = SafePatchApplier(tmp_path).apply(
        "binary.py", "old", "new", expected_sha256=_sha(binary)
    )
    assert binary_result.status == PatchApplyStatus.DENIED
    assert binary_result.error_code == "binary_file"

    target = tmp_path / "target.py"
    target.write_text("old", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    link_result = SafePatchApplier(tmp_path).apply(
        "link.py", "old", "new", expected_sha256=_sha(target)
    )
    assert link_result.status == PatchApplyStatus.DENIED
    assert target.read_text(encoding="utf-8") == "old"

    protected = SafePatchApplier(tmp_path).apply(
        ".git/config", "old", "new", expected_sha256="0" * 64
    )
    assert protected.status == PatchApplyStatus.DENIED


@pytest.mark.asyncio
async def test_git_guard_rejects_submodule_descendants(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "feature.py").write_text("old", encoding="utf-8")
    snapshot = WorkspaceSnapshot(
        "snapshot",
        str(tmp_path),
        "head",
        (WorkspaceFileState("vendor", "submodule:M."),),
        "digest",
        "test",
        "2026-01-01T00:00:00+00:00",
    )
    guard = GitGuardedDispatcher(
        LocalToolDispatcher(approve=lambda _name, _args: True), object(), snapshot
    )
    request = AgentRequest("s", "t", str(tmp_path), "task", "fake", run_id="run")
    denied = await guard.prepare(
        ToolCall(
            "patch",
            "apply_patch",
            {"path": "vendor/feature.py", "old": "old", "new": "new"},
        ),
        request,
    )
    assert denied is not None and denied.output == "submodule_boundary"
