"""输入层：prompt_toolkit 可用且为 TTY 时用 PromptSession，否则回落 PlainInput。

prompt_toolkit 只在这里 import，且用 try/except 包裹；未安装时整个模块
仍可导入，create_chat_input() 返回 PlainInput（即现有 input() 行为）。
选择器同样：TTY 时用 prompt_toolkit 应用（↑↓ 回绕、Enter 确认、Esc 返回），
非 TTY 时走编号（与旧 menu.choose 的降级输出逐行一致）。
"""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deity.application.config import ChatConfig
from deity.interfaces.cli.chat_config import default_history_path
from deity.interfaces.cli.chat_mentions import complete_paths

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    _HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover - 未安装 extra 时的回落分支
    _HAS_PROMPT_TOOLKIT = False


def wrap_menu_index(index: int, direction: str, count: int) -> int:
    """Wrap arrow-key navigation around a nonempty menu（自旧 menu.py 搬运）."""
    return (index + (-1 if direction == "up" else 1)) % count


@dataclass(frozen=True)
class CompletionCandidate:
    """一条补全候选：插入文本与显示文本。"""

    text: str
    display: str = ""


def complete_candidates(
    text: str,
    *,
    commands: tuple[str, ...] = (),
    workspace: Path | None = None,
    buildable_ids: tuple[str, ...] = (),
) -> list[CompletionCandidate]:
    """纯补全逻辑（不依赖 prompt_toolkit）：

    - 输入 / 开头时补斜杠命令；
    - 输入 /build 加空格后补可 Build 的 plan_id；
    - 输入 @ 开头时补工作区内相对路径。
    """
    if text.startswith("/build "):
        remainder = text[len("/build ") :]
        return [
            CompletionCandidate(text=item, display=item)
            for item in buildable_ids
            if item.startswith(remainder)
        ]
    if text.startswith("/"):
        return [
            CompletionCandidate(text=name, display=name)
            for name in commands
            if name.startswith(text)
        ]
    if "@" in text:
        token = text.rsplit(" ", 1)[-1] if " " in text else text
        if token.startswith("@") and workspace is not None:
            prefix = token[1:]
            return [
                CompletionCandidate(text=f"@{path}", display=path)
                for path in complete_paths(prefix, workspace)
            ]
    return []


def _selector_lines(title: str, items: list[str], index: int) -> list[str]:
    """选择器的一屏文本：当前项前标 ❯，其余两格缩进。"""
    lines = [title]
    for position, item in enumerate(items):
        lines.append(f"{'❯' if position == index else ' '} {item}")
    lines.append("↑↓ 选择 · Enter 确认 · Esc 返回")
    return lines


class PlainInput:
    """现有 input() 行为：逐行读入；选择走编号（与旧 menu.choose 降级一致）。"""

    def read_line(self, prompt_text: str) -> str:
        """读一行输入。"""
        return input(prompt_text)

    def select(self, title: str, items: list[str]) -> int | None:
        """编号选择；空列表或无效输入返回 None。"""
        if not items:
            print(f"{title}：无可用项")
            return None
        print(title)
        for number, item in enumerate(items, 1):
            print(f"  {number}. {item}")
        try:
            answer = input("选择编号（回车取消）> ").strip()
        except EOFError:
            return None
        return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(items) else None


class PromptToolkitInput:
    """prompt_toolkit 输入：历史、多行、补全、按键绑定、底部状态栏。"""

    def __init__(
        self,
        config: ChatConfig,
        *,
        workspace: Path,
        commands: tuple[str, ...] = (),
        buildable_ids_provider: Callable[[], tuple[str, ...]] | None = None,
        status_text: Callable[[], str] | None = None,
        tty: bool = True,
    ) -> None:
        if not _HAS_PROMPT_TOOLKIT:
            raise RuntimeError("prompt_toolkit 未安装，无法创建 PromptToolkitInput")
        self.config = config
        self.workspace = Path(workspace)
        self.commands = tuple(commands)
        self._buildable_ids_provider = buildable_ids_provider
        self._status_text = status_text
        self._tty = tty
        history_path = default_history_path(config)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        self._truncate_history(history_path, config.max_history)
        assert PromptSession is not None
        assert FileHistory is not None
        self._session: Any = PromptSession(
            history=FileHistory(str(history_path)),
            multiline=True,
            key_bindings=self._key_bindings(),
            completer=self._completer(),
            bottom_toolbar=self._bottom_toolbar,
        )

    @staticmethod
    def _truncate_history(path: Path, max_history: int) -> None:
        """历史文件超过上限时只保留最近 max_history 行。"""
        if not path.is_file():
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_history:
            path.write_text("\n".join(lines[-max_history:]) + "\n", encoding="utf-8")

    def _key_bindings(self) -> Any:
        """Enter 提交、Alt+Enter 换行、Esc 清空当前输入行。"""
        assert KeyBindings is not None
        bindings = KeyBindings()

        @bindings.add("enter")
        def _submit(event: Any) -> None:
            event.app.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _newline(event: Any) -> None:
            event.app.current_buffer.insert_text("\n")

        @bindings.add("escape")
        def _clear(event: Any) -> None:
            event.app.current_buffer.reset()

        return bindings

    def _completer(self) -> Any:
        """把纯补全逻辑适配成 prompt_toolkit Completer。"""
        assert Completer is not None
        assert Completion is not None
        outer = self

        class _ChatCompleter(Completer):
            def get_completions(self, document: Any, complete_event: Any):  # type: ignore[no-untyped-def]
                before = document.text_before_cursor
                buildable = (
                    outer._buildable_ids_provider()
                    if outer._buildable_ids_provider is not None
                    else ()
                )
                candidates = complete_candidates(
                    before,
                    commands=outer.commands,
                    workspace=outer.workspace,
                    buildable_ids=buildable,
                )
                if not candidates:
                    return
                token = before.rsplit(" ", 1)[-1] if before else ""
                for candidate in candidates:
                    yield Completion(
                        candidate.text,
                        start_position=-len(token),
                        display=candidate.display or candidate.text,
                    )

        return _ChatCompleter()

    def _bottom_toolbar(self) -> str:
        """底部状态栏：有状态回调时显示，否则为空。"""
        if self._status_text is None:
            return ""
        return self._status_text()

    def read_line(self, prompt_text: str) -> str:
        """读一行输入（多行模式：Enter 提交，Alt+Enter 换行）。"""
        return str(self._session.prompt(prompt_text))

    def select(self, title: str, items: list[str], *, app_factory: Any | None = None) -> int | None:
        """选择一项：TTY 时用 prompt_toolkit 应用，否则走编号。"""
        if not items:
            print(f"{title}：无可用项")
            return None
        if not self._tty:
            return PlainInput().select(title, items)
        return self._select_with_app(title, items, app_factory=app_factory)

    def _select_with_app(
        self, title: str, items: list[str], *, app_factory: Any | None = None
    ) -> int | None:
        """prompt_toolkit 选择器：↑↓ 回绕、Enter 确认、Esc 返回。"""
        assert Application is not None
        assert KeyBindings is not None
        assert Layout is not None
        assert Window is not None
        assert FormattedTextControl is not None
        state: dict[str, Any] = {"index": 0, "app": None}
        bindings = KeyBindings()

        @bindings.add("up")
        def _up(event: Any) -> None:
            state["index"] = wrap_menu_index(state["index"], "up", len(items))

        @bindings.add("down")
        def _down(event: Any) -> None:
            state["index"] = wrap_menu_index(state["index"], "down", len(items))

        @bindings.add("enter")
        def _confirm(event: Any) -> None:
            state["app"].exit(state["index"])

        @bindings.add("escape")
        def _cancel(event: Any) -> None:
            state["app"].exit(None)

        control = FormattedTextControl(
            text=lambda: "\n".join(_selector_lines(title, items, state["index"]))
        )
        factory = app_factory or Application
        app: Any = factory(layout=Layout(Window(content=control)), key_bindings=bindings)
        state["app"] = app
        return app.run()  # type: ignore[no-any-return]


def create_chat_input(
    config: ChatConfig,
    *,
    workspace: Path,
    commands: tuple[str, ...] = (),
    buildable_ids_provider: Callable[[], tuple[str, ...]] | None = None,
    status_text: Callable[[], str] | None = None,
    tty: bool | None = None,
) -> PlainInput | PromptToolkitInput:
    """创建输入层：TTY + 已装 prompt_toolkit + 非 dumb 终端时用增强输入。

    否则回落 PlainInput（现有 input() 行为）。tty=None 时自动检测。
    """
    use_tty = tty if tty is not None else _detect_tty()
    if not _HAS_PROMPT_TOOLKIT or not use_tty or os.environ.get("TERM") == "dumb":
        return PlainInput()
    return PromptToolkitInput(
        config,
        workspace=workspace,
        commands=commands,
        buildable_ids_provider=buildable_ids_provider,
        status_text=status_text,
        tty=True,
    )


def _detect_tty() -> bool:
    """stdin/stdout 是否都是终端。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


__all__ = [
    "CompletionCandidate",
    "PlainInput",
    "PromptToolkitInput",
    "complete_candidates",
    "create_chat_input",
    "wrap_menu_index",
]
