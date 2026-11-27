"""工作台底部一体式 Composer：ChatInput 与 FocusBar。"""

import os
import re
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static, TextArea

from deity.interfaces.tui.commands import AVAILABLE_SLASH_COMMANDS

_MENTION_AT_END_RE = re.compile(r"@([^\s@]*)$")
_MENTION_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".Trash",
    "Library",
    "System",
    "Volumes",
    "Applications",
    "dev",
    "proc",
    "sys",
    "private",
}


def _workspace_files(
    workspace: Path, partial: str, *, limit: int = 200, max_scanned: int = 4_000
) -> list[str]:
    """工作区文件相对路径：按 @ 后缀子串匹配（不区分大小写），有界扫描防卡死。"""
    needle = partial.lower()
    found: list[str] = []
    scanned = 0
    for root, dirs, files in os.walk(workspace, followlinks=False, onerror=lambda _: None):
        dirs[:] = [d for d in dirs if d not in _MENTION_SKIP_DIRS and not d.startswith(".")]
        for name in files:
            scanned += 1
            if scanned > max_scanned:
                return sorted(found)
            rel = str(Path(root, name).relative_to(workspace))
            if needle in rel.lower():
                found.append(rel)
                if len(found) >= limit:
                    return sorted(found)
    return sorted(found)


class ChatInput(TextArea):
    """工作台输入框：Enter 提交、Ctrl+J 换行、Tab 补全、Ctrl+P 命令面板、方向键历史。"""

    class Submitted(Message):
        """用户提交了一段输入（Enter）。"""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CancelRequested(Message):
        """输入框内按 Ctrl+C / Esc：请求取消当前 Run（Ctrl+C 空闲时退出）。"""

        def __init__(self, key: str = "ctrl+c") -> None:
            super().__init__()
            self.key = key

    class ModeCycleRequested(Message):
        """输入框内按 Shift+Tab：请求循环切换模式（普通/计划/自动接受）。"""

    class CommandPaletteRequested(Message):
        """输入框内按 Ctrl+P：打开全局命令面板。"""

    class CommandHintUpdated(Message):
        """输入框内的联想更新：斜杠命令（kind="command"）或 @ 文件（kind="mention"）。"""

        def __init__(self, matches: list[tuple[str, str]], kind: str = "command") -> None:
            super().__init__()
            self.matches = matches
            self.kind = kind

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    def _slash_commands(self) -> list[tuple[str, str]]:
        provider = getattr(getattr(self, "app", None), "all_slash_commands", None)
        if callable(provider):
            return list(provider())
        return AVAILABLE_SLASH_COMMANDS

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        del event
        text = self.text
        if text.startswith("/") and not text.startswith("//"):
            prefix = text.split()[0] if text else ""
            matches = [
                (cmd, desc) for cmd, desc in self._slash_commands() if cmd.startswith(prefix)
            ]
            self.post_message(self.CommandHintUpdated(matches))
            return
        mention = _MENTION_AT_END_RE.search(text)
        workspace = getattr(getattr(self, "app", None), "_workspace", None)
        if mention is not None and isinstance(workspace, Path):
            matches = [
                (f"@{path}", "文件") for path in _workspace_files(workspace, mention.group(1))
            ][:5]
            self.post_message(self.CommandHintUpdated(matches, kind="mention"))
            return
        self.post_message(self.CommandHintUpdated([]))

    async def _on_key(self, event: Any) -> None:
        if (
            self.app is not None
            and getattr(self.app, "_prompt_handler", None) is not None
            and hasattr(self.app, "handle_prompt_key")
            and self.app.handle_prompt_key(event.key)
        ):
            event.prevent_default()
            event.stop()
            return

        if event.key == "ctrl+p":
            event.prevent_default()
            event.stop()
            self.post_message(self.CommandPaletteRequested())
            return
        if event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            self.post_message(self.ModeCycleRequested())
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            if text.strip():
                self._push_history(text)
            self.post_message(self.Submitted(text))
            return
        if event.key == "tab":
            text = self.text
            mention = _MENTION_AT_END_RE.search(text)
            workspace = getattr(getattr(self, "app", None), "_workspace", None)
            if mention is not None and isinstance(workspace, Path):
                matches = _workspace_files(workspace, mention.group(1))
                if matches:
                    event.prevent_default()
                    event.stop()
                    if len(matches) == 1:
                        completed = matches[0]
                    else:
                        completed = matches[0]
                        for candidate in matches[1:]:
                            while not candidate.startswith(completed):
                                completed = completed[:-1]
                    self.text = text[: mention.start()] + "@" + completed + " "
                    self.move_cursor((0, len(self.text)))
                    self.post_message(self.CommandHintUpdated([]))
                    return
            if text.startswith("/"):
                prefix = text.split()[0] if text else ""
                commands = self._slash_commands()
                matches = [cmd for cmd, _ in commands if cmd.startswith(prefix)]
                if matches:
                    event.prevent_default()
                    event.stop()
                    if len(matches) == 1:
                        self.text = matches[0] + " "
                        self.move_cursor((0, len(self.text)))
                        self.post_message(self.CommandHintUpdated([]))
                    else:
                        common = matches[0]
                        for m in matches[1:]:
                            while not m.startswith(common):
                                common = common[:-1]
                        if len(common) > len(prefix):
                            self.text = common
                            self.move_cursor((0, len(self.text)))
                        self.post_message(
                            self.CommandHintUpdated(
                                [(cmd, desc) for cmd, desc in commands if cmd in matches]
                            )
                        )
                    return
        if event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key in ("ctrl+c", "escape"):
            event.prevent_default()
            event.stop()
            self.post_message(self.CancelRequested(event.key))
            return
        if event.key == "up" and self.cursor_location[0] == 0 and self._history:
            event.prevent_default()
            event.stop()
            self._history_prev()
            return
        if (
            event.key == "down"
            and self._history_index is not None
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            event.prevent_default()
            event.stop()
            self._history_next()
            return
        await super()._on_key(event)

    def _push_history(self, text: str) -> None:
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = None
        self._draft = ""

    def _set_text(self, text: str) -> None:
        self.text = text
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))

    def _history_prev(self) -> None:
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_text(self._history[self._history_index])

    def _history_next(self) -> None:
        assert self._history_index is not None
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_text(self._history[self._history_index])
        else:
            self._history_index = None
            self._set_text(self._draft)


class FocusBar(Horizontal):
    """输入框上方 1 行 Focus 状态条：显示运行状态/当前工具/排队数量。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(id="focus-bar", **kwargs)

    def compose(self) -> ComposeResult:
        yield Static("", id="focus-status")
        yield Static("", id="focus-queue")

    def update_focus(self, *, status_text: str, queue_size: int) -> None:
        self.query_one("#focus-status", Static).update(status_text)
        queue_text = f"队列 {queue_size} 条待执行" if queue_size > 0 else ""
        self.query_one("#focus-queue", Static).update(queue_text)


__all__ = ["ChatInput", "FocusBar"]
