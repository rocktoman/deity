"""unified diff 的纯函数渲染（Codex `diff_render` 行为的 Python 重写）。

输入：`turn_diff` payload（`files[]` 含 unified diff 文本）或原始 git diff 文本。
输出：Rich `Text`，TUI（Static）与行式（console.print）共用。

格式对齐 Codex 的 diff 摘要：
  • 已修改 <path> (+N -M)            单文件总头
  • 已修改 N 个文件 (+A -B)           多文件总头
    └ <path> (+N -M)                 多文件时每文件一行
  diff 行：右对齐行号 + gutter（`+`/`-`/空格）+ 内容；增行中性浅灰底加粗、
  删行 error 红底（黑色主题无绿色，与 Codex 增绿/删红配色刻意不同）；
  hunk 头 `@@…@@` 用三级色；`---`/`+++` 头用弱提示色；超长截断并提示。

颜色由调用方传入调色板（`theme.palette()` 的子集）；默认用深色值，
与 `theme.palette(dark=True)` 同源，深色改动时同步——本模块不导入
Textual 主题，保持行式 CLI 在不装 `[tui]` 时也能用。
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from rich.text import Text

DiffKind = Literal["add", "del", "ctx", "hunk", "meta"]

# 默认深色（与 theme.palette(dark=True) 同源）。
DARK_DIFF_COLORS: dict[str, str] = {
    "accent": "#E8EAE8",
    "secondary": "#BDBDBD",
    "tertiary": "#8A8A8A",
    "quaternary": "#5E5E5E",
    "error": "#E5534B",
    "diff_add_bg": "#262626",
    "diff_del_bg": "#3A1F1C",
}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_STATUS_VERB = {"created": "新增", "deleted": "删除", "modified": "已修改"}


@dataclass(frozen=True)
class DiffLine:
    """unified diff 的一行：新旧行号（仅一侧有时另一侧为 None）+ 类型 + 内容。"""

    old_no: int | None
    new_no: int | None
    kind: DiffKind
    text: str


def count_diff_stats(diff_text: str) -> tuple[int, int]:
    """统计 unified diff 的增/删行数（`+++`/`---` 头不计）。"""
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def parse_diff_lines(diff_text: str) -> list[DiffLine]:
    """解析 unified diff：跟踪 `@@` 行号，逐行标类型。"""
    lines: list[DiffLine] = []
    old_no: int | None = None
    new_no: int | None = None
    for raw in diff_text.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            lines.append(DiffLine(None, None, "hunk", raw))
            continue
        if raw.startswith(("--- ", "+++ ", "diff --git ", "index ", "new file", "deleted file")):
            lines.append(DiffLine(None, None, "meta", raw))
            continue
        if raw.startswith("+"):
            lines.append(DiffLine(None, new_no, "add", raw[1:]))
            if new_no is not None:
                new_no += 1
        elif raw.startswith("-"):
            lines.append(DiffLine(old_no, None, "del", raw[1:]))
            if old_no is not None:
                old_no += 1
        elif raw.startswith("\\"):
            lines.append(DiffLine(None, None, "meta", raw))
        else:
            text = raw.removeprefix(" ")
            lines.append(DiffLine(old_no, new_no, "ctx", text))
            if old_no is not None:
                old_no += 1
            if new_no is not None:
                new_no += 1
    return lines


def format_diff_lines(
    parsed: list[DiffLine],
    colors: dict[str, str],
    *,
    max_lines: int | None = 60,
    prefix: str = "",
) -> Text:
    """把解析后的 diff 行画成 Rich 文本：行号 + gutter + 内容，超长截断。

    `prefix` 为每行前的无样式缩进（Codex 式 4 空格）。
    """
    out = Text()
    shown = parsed if max_lines is None else parsed[:max_lines]
    width = 1
    for line in shown:
        for number in (line.old_no, line.new_no):
            if number is not None:
                width = max(width, len(str(number)))
    for line in shown:
        number = line.new_no if line.kind in {"add", "ctx"} else line.old_no
        num_text = f"{number:>{width}}" if number is not None else " " * width
        out.append(prefix)
        if line.kind == "add":
            out.append(
                f"{num_text} + ", style=f"bold {colors['accent']} on {colors['diff_add_bg']}"
            )
            out.append(f"{line.text}\n", style=f"on {colors['diff_add_bg']}")
        elif line.kind == "del":
            out.append(f"{num_text} - ", style=f"bold {colors['error']} on {colors['diff_del_bg']}")
            out.append(f"{line.text}\n", style=f"on {colors['diff_del_bg']}")
        elif line.kind == "hunk":
            out.append(f"{' ' * width}   {line.text}\n", style=colors["tertiary"])
        elif line.kind == "meta":
            out.append(f"{' ' * width}   {line.text}\n", style=colors["quaternary"])
        else:
            out.append(f"{num_text}   {line.text}\n", style=colors["secondary"])
    if max_lines is not None and len(parsed) > max_lines:
        out.append(prefix)
        out.append(f"… 还有 {len(parsed) - max_lines} 行\n", style=colors["tertiary"])
    return out


def format_turn_diff_summary(payload: dict[str, Any], colors: dict[str, str]) -> Text:
    """turn_diff 的摘要头：总头 + 多文件时每文件一行（折叠态用）。"""
    files = payload.get("files", [])
    out = Text()
    total_added = total_removed = 0
    per_file: list[tuple[str, str, int, int]] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        status = str(entry.get("status", "modified"))
        added, removed = count_diff_stats(str(entry.get("diff", "")))
        total_added += added
        total_removed += removed
        per_file.append((path, status, added, removed))
    count = len(per_file)
    if count == 0:
        return out
    out.append("• ", style=colors["tertiary"])
    if count == 1:
        path, status, added, removed = per_file[0]
        out.append(_STATUS_VERB.get(status, "已修改"), style="bold")
        out.append(f" {path} ")
    else:
        out.append("已修改", style="bold")
        out.append(f" {count} 个文件 ")
    out.append(f"(+{total_added} ", style=f"bold {colors['accent']}")
    out.append(f"-{total_removed})", style=f"bold {colors['error']}")
    out.append("\n")
    if count > 1:
        for path, status, added, removed in per_file:
            out.append("  └ ", style=colors["tertiary"])
            out.append(f"{path} ", style=colors["secondary"])
            out.append(f"(+{added} ", style=colors["accent"])
            out.append(f"-{removed})\n", style=colors["error"])
    return out


def format_turn_diff(
    payload: dict[str, Any],
    colors: dict[str, str] | None = None,
    *,
    max_lines_per_file: int | None = 60,
) -> Text:
    """turn_diff 全文：摘要头 + 每文件 hunks（展开态用）。"""
    colors = colors or DARK_DIFF_COLORS
    out = format_turn_diff_summary(payload, colors)
    files = payload.get("files", [])
    for entry in files:
        if not isinstance(entry, dict):
            continue
        diff_text = str(entry.get("diff", ""))
        if not diff_text:
            continue
        if len(files) > 1:
            out.append(f"  └ {entry.get('path', '')}\n", style=colors["tertiary"])
        out.append(
            format_diff_lines(
                parse_diff_lines(diff_text),
                colors,
                max_lines=max_lines_per_file,
                prefix="    ",
            )
        )
        if entry.get("truncated"):
            out.append("…（已截断，只显示前 8000 字符）\n", style=colors["tertiary"])
    return out


def format_diff_text(
    diff_text: str,
    colors: dict[str, str] | None = None,
    *,
    max_lines: int | None = 200,
) -> Text:
    """原始 git diff 文本直接渲染（`/diff` 用）。"""
    colors = colors or DARK_DIFF_COLORS
    return format_diff_lines(parse_diff_lines(diff_text), colors, max_lines=max_lines)


__all__ = [
    "DARK_DIFF_COLORS",
    "DiffLine",
    "count_diff_stats",
    "format_diff_text",
    "format_turn_diff",
    "format_turn_diff_summary",
    "parse_diff_lines",
]
