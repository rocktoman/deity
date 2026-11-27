"""任意目录运行测试：不可读文件、超大目录、非 Git 目录均不阻断对话。"""

import os
import sys
from pathlib import Path

import pytest

from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.tools import workspace as workspace_mod
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import CodingConfig, PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent
from deity.interfaces.tui.widgets.composer import _workspace_files


class _HelloModel:
    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="你好，我在任意目录都能正常对话。")
        yield ModelEvent("message_completed")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod 0o000 permissions")
def test_workspace_digest_skips_unreadable_files_and_bounds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    secret = tmp_path / ".file"
    secret.write_text("restricted", encoding="utf-8")
    secret.chmod(0o000)
    try:
        first = workspace_digest(tmp_path)
        second = workspace_digest(tmp_path)
        assert first == second
        assert len(first) == 64

        # 超出 _MAX_FILES 上限时有界返回，不抛 ValueError 阻断对话。
        monkeypatch.setattr(workspace_mod, "_MAX_FILES", 1)
        (tmp_path / "extra1.txt").write_text("a", encoding="utf-8")
        (tmp_path / "extra2.txt").write_text("b", encoding="utf-8")
        bounded = workspace_digest(tmp_path)
        assert len(bounded) == 64
    finally:
        secret.chmod(0o644)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod 0o000 permissions")
@pytest.mark.asyncio
async def test_git_workspace_adapter_plain_files_skips_unreadable(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("ok", encoding="utf-8")
    locked = tmp_path / ".file"
    locked.write_text("nope", encoding="utf-8")
    locked.chmod(0o000)
    try:
        adapter = GitWorkspaceAdapter(tmp_path, max_files=2)
        snapshot = await adapter.capture()
        paths = {item.path for item in snapshot.files}
        assert "a.txt" in paths
        assert ".file" in paths
        locked_entry = next(item for item in snapshot.files if item.path == ".file")
        if os.geteuid() != 0:
            assert locked_entry.sha256 is None
    finally:
        locked.chmod(0o644)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod 0o000 permissions")
@pytest.mark.asyncio
async def test_run_service_chat_succeeds_with_unreadable_file_and_git_guard(
    tmp_path: Path,
) -> None:
    work = tmp_path / "anydir"
    work.mkdir()
    locked = work / ".file"
    locked.write_text("system", encoding="utf-8")
    locked.chmod(0o000)
    try:
        service = RunService(
            db_path=tmp_path / "deity.db",
            workspace=work,
            model=_HelloModel(),  # type: ignore[arg-type]
            permission=PermissionConfig(shell="ask", workspace_write="ask"),
            coding=CodingConfig(git_guard_enabled=True),
        )
        chunks: list[str] = []
        async for event in service.run_stream("你好", model_name="fake", session_id="s1"):
            if event.kind == "text_delta":
                chunks.append(str(event.payload.get("text", "")))
        assert "任意目录" in "".join(chunks)
    finally:
        locked.chmod(0o644)


def test_mention_workspace_files_respects_scan_cap(tmp_path: Path) -> None:
    for i in range(15):
        (tmp_path / f"f{i}.py").write_text("x=1\n", encoding="utf-8")
    found = _workspace_files(tmp_path, "", limit=100, max_scanned=5)
    assert len(found) <= 5
