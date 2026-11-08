"""Phase 2：输入层（prompt_toolkit 可选 extra，未装/非 TTY 时回落 PlainInput）。"""

from pathlib import Path

import pytest

from deity.application.config import ChatConfig
from deity.interfaces.cli import chat_input
from deity.interfaces.cli.chat_input import (
    PlainInput,
    PromptToolkitInput,
    complete_candidates,
    create_chat_input,
    wrap_menu_index,
)


def test_wrap_menu_index_wraps_around() -> None:
    assert wrap_menu_index(0, "up", 2) == 1
    assert wrap_menu_index(1, "down", 2) == 0
    assert wrap_menu_index(0, "down", 3) == 1


def test_plain_input_reads_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt_text: "hello")
    assert PlainInput().read_line("Deity> ") == "hello"


def test_plain_input_select_numbered_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda prompt_text: next(answers))
    assert PlainInput().select("标题", ["a", "b", "c"]) == 1


def test_plain_input_select_empty_or_invalid_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PlainInput().select("标题", []) is None
    monkeypatch.setattr("builtins.input", lambda prompt_text: "9")
    assert PlainInput().select("标题", ["a", "b"]) is None
    monkeypatch.setattr("builtins.input", lambda prompt_text: "")
    assert PlainInput().select("标题", ["a", "b"]) is None


def test_complete_candidates_slash_commands() -> None:
    commands = ("/help", "/plan", "/normal", "/build", "/skill", "/mcp", "/exit")
    found = complete_candidates("/", commands=commands, workspace=Path("/tmp"))
    assert [item.text for item in found] == list(commands)
    assert [
        item.text for item in complete_candidates("/p", commands=commands, workspace=Path("/tmp"))
    ] == ["/plan"]
    assert complete_candidates("普通任务", commands=commands, workspace=Path("/tmp")) == []


def test_complete_candidates_build_ids() -> None:
    commands = ("/help", "/build")
    found = complete_candidates(
        "/build ", commands=commands, workspace=Path("/tmp"), buildable_ids=("p1", "p2")
    )
    assert [item.text for item in found] == ["p1", "p2"]
    assert (
        complete_candidates(
            "/build p", commands=commands, workspace=Path("/tmp"), buildable_ids=("p1", "p2")
        )[0].text
        == "p1"
    )


def test_complete_candidates_at_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("x", encoding="utf-8")
    found = complete_candidates("@src/", commands=(), workspace=tmp_path)
    assert [item.text for item in found] == ["@src/main.py", "@src/util.py"]
    assert complete_candidates("@zz", commands=(), workspace=tmp_path) == []


def test_create_input_falls_back_without_prompt_toolkit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(chat_input, "_HAS_PROMPT_TOOLKIT", False)
    assert isinstance(create_chat_input(ChatConfig(), workspace=tmp_path, tty=True), PlainInput)


def test_create_input_falls_back_when_not_tty(tmp_path: Path) -> None:
    assert isinstance(create_chat_input(ChatConfig(), workspace=tmp_path, tty=False), PlainInput)


def test_create_input_falls_back_on_dumb_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TERM", "dumb")
    assert isinstance(create_chat_input(ChatConfig(), workspace=tmp_path, tty=True), PlainInput)


def test_create_input_uses_prompt_toolkit_on_tty(tmp_path: Path) -> None:
    history = tmp_path / "history"
    config = ChatConfig(history_file=str(history))
    created = create_chat_input(config, workspace=tmp_path, tty=True)
    assert isinstance(created, PromptToolkitInput)
    assert created.config.history_file == str(history)


def test_prompt_toolkit_history_is_truncated_to_max(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.write_text("\n".join(f"line {i}" for i in range(600)) + "\n", encoding="utf-8")
    config = ChatConfig(history_file=str(history), max_history=500)
    PromptToolkitInput(config, workspace=tmp_path)
    assert len(history.read_text(encoding="utf-8").splitlines()) == 500
    assert history.read_text(encoding="utf-8").splitlines()[0] == "line 100"


def test_prompt_toolkit_select_returns_stub_result(tmp_path: Path) -> None:
    config = ChatConfig(history_file=str(tmp_path / "history"))
    selected = PromptToolkitInput(config, workspace=tmp_path)

    class StubApp:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def run(self) -> int | None:
            return 1

    assert selected.select("标题", ["a", "b"], app_factory=StubApp) == 1


def test_selector_text_marks_current_index() -> None:
    from deity.interfaces.cli.chat_input import _selector_lines

    lines = _selector_lines("标题", ["a", "b"], 1)
    assert lines[0] == "标题"
    assert lines[1] == "  a"
    assert lines[2].startswith("❯")
    assert "b" in lines[2]


def test_prompt_toolkit_select_falls_back_to_numbered_when_not_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = ChatConfig(history_file=str(tmp_path / "history"))
    selected = PromptToolkitInput(config, workspace=tmp_path, tty=False)
    monkeypatch.setattr("builtins.input", lambda prompt_text: "1")
    assert selected.select("标题", ["a", "b"]) == 0
