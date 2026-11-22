"""工作区 git 只读查询（`/diff` 与 `/status` 分支行共用）。

`/diff` 的 diff 文本走既有 `GitWorkspaceAdapter.diff()`（只读、有界、
非 git 仓库时回退快照摘要）；分支名与未追踪文件用短超时子进程直读，
失败一律返回空值、调用方省略对应行，不抛错。
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from deity.adapters.git import GitWorkspaceAdapter

_GIT_TIMEOUT_SECONDS = 5


def git_branch(workspace: Path) -> str | None:
    """当前分支名；非 git 仓库或 git 不可用时返回 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def untracked_files(workspace: Path, *, limit: int = 20) -> list[str]:
    """未追踪文件（相对路径，排序后截断）；非 git 仓库时为空列表。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    files = sorted(line for line in result.stdout.splitlines() if line.strip())
    return files[:limit]


async def workspace_diff(workspace: Path) -> dict[str, Any]:
    """工作区改动摘要：git diff 文本 + 未追踪文件 + 是否 git 仓库。

    返回 `{"diff": str, "untracked": [str, ...], "is_git": bool}`；
    `diff` 为空表示无已追踪改动。
    """
    adapter = GitWorkspaceAdapter(workspace)
    try:
        text = await adapter.diff()
    except OSError:
        return {"diff": "", "untracked": [], "is_git": False}
    if text.startswith("["):
        try:
            snapshot = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            snapshot = []
        untracked = [str(item.get("path", "")) for item in snapshot if isinstance(item, dict)]
        return {"diff": "", "untracked": untracked, "is_git": False}
    return {"diff": text, "untracked": untracked_files(workspace), "is_git": True}


__all__ = ["git_branch", "untracked_files", "workspace_diff"]
