"""审批用预期 diff 的计算（ADR-023）：纯计算，不写盘、不依赖 CLI 层。

Application 层提供，供 RunService/ClientService（审批请求载荷）与
CLI 审批卡片共用；统一 diff 渲染用标准库 difflib，与原先
`chat_render.render_diff` 同款格式（`a/<path>`/`b/<path>` 头、
200 行截断）。
"""

import difflib
from pathlib import Path
from typing import Any

_MAX_DIFF_CHARS = 200_000


def render_unified_diff(
    old_text: str, new_text: str, *, path: str = "", max_lines: int = 200
) -> str:
    """unified diff（只用于展示，不写盘）；超长截断并提示。"""
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{path}" if path else "a/old",
            tofile=f"b/{path}" if path else "b/new",
            lineterm="",
        )
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"…（已截断，只显示前 {max_lines} 行）"]
    return "\n".join(lines) + ("\n" if lines else "")


def _read_workspace_text(workspace: Path, path: str) -> str | None:
    """读工作区内文件内容；缺失、越界或不可读时返回 None。"""
    try:
        candidate = (workspace / path).resolve()
        if candidate.is_relative_to(workspace.resolve()) and candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return None


def approval_diff(tool_name: str, args: dict[str, Any], workspace: Path) -> str | None:
    """改文件工具的预期 unified diff；其余工具返回 None。

    apply_patch：旧形态（path+old/new）渲染 diff；新形态（patch 文本）
    直接取 patch 文本（已是 diff 形态，截断）。write_file：读盘取旧内容
    与新内容做 diff（新建文件返回 None，卡片另有「新建文件。」文案）。
    delete_file：旧内容对空内容做 diff。读盘失败或内容超限返回 None。

    旧形态的键名以工具 schema 为准（`old`/`new`）；`old_text`/`new_text`
    也接受（旧卡片代码曾用此拼写，兼容历史参数）。
    """
    if tool_name == "apply_patch":
        old = args.get("old", args.get("old_text", ""))
        new = args.get("new", args.get("new_text", ""))
        if isinstance(old, str) and isinstance(new, str) and (old or new):
            path = str(args.get("path", ""))
            if len(old) > _MAX_DIFF_CHARS or len(new) > _MAX_DIFF_CHARS:
                return None
            return render_unified_diff(old, new, path=path)
        patch = args.get("patch", "")
        if isinstance(patch, str) and patch.strip():
            if len(patch) > _MAX_DIFF_CHARS:
                return patch[:_MAX_DIFF_CHARS] + "\n…（已截断）"
            return patch
        return None
    if tool_name == "write_file":
        new = args.get("content", "")
        if not isinstance(new, str):
            return None
        old = _read_workspace_text(workspace, str(args.get("path", "")))
        if old is None:
            return None
        if len(old) > _MAX_DIFF_CHARS or len(new) > _MAX_DIFF_CHARS:
            return None
        return render_unified_diff(old, new, path=str(args.get("path", "")))
    if tool_name == "delete_file":
        old = _read_workspace_text(workspace, str(args.get("path", "")))
        if not old:
            return None
        if len(old) > _MAX_DIFF_CHARS:
            return None
        return render_unified_diff(old, "", path=str(args.get("path", "")))
    return None


__all__ = ["approval_diff", "render_unified_diff"]
