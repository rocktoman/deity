"""Phase 3：审批卡片与会话级允许（ApprovalCard/ApprovalController）。"""

import io
from pathlib import Path

from rich.console import Console

from deity.interfaces.cli.chat_approval import (
    ApprovalCard,
    ApprovalController,
    SessionAllowlist,
    make_approve,
)


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


def _choices(*answers: str):
    script = iter(answers)

    def input_fn(prompt_text: str = "") -> str:
        return next(script)

    return input_fn


def test_shell_card_shows_command_and_risk() -> None:
    console, buffer = _console()
    card = ApprovalCard("shell", {"command": "pytest tests/unit -q"}, workspace=Path("/tmp/ws"))
    card.render(console)
    output = buffer.getvalue()
    assert "shell" in output
    assert "pytest tests/unit -q" in output
    assert "风险" in output


def test_write_file_card_shows_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\nline\n", encoding="utf-8")
    console, buffer = _console()
    card = ApprovalCard(
        "write_file", {"path": "a.txt", "content": "new\nline\n"}, workspace=tmp_path
    )
    card.render(console)
    output = buffer.getvalue()
    assert "write_file" in output
    assert "a.txt" in output
    assert "-old" in output
    assert "+new" in output


def test_write_file_card_new_file_reports_creation(tmp_path: Path) -> None:
    console, buffer = _console()
    card = ApprovalCard("write_file", {"path": "b.txt", "content": "hello\n"}, workspace=tmp_path)
    card.render(console)
    assert "新建文件" in buffer.getvalue()


def test_apply_patch_card_shows_snippet_diff(tmp_path: Path) -> None:
    console, buffer = _console()
    card = ApprovalCard(
        "apply_patch",
        {"path": "a.txt", "old_text": "x = 1\n", "new_text": "x = 2\n"},
        workspace=tmp_path,
    )
    card.render(console)
    output = buffer.getvalue()
    assert "-x = 1" in output
    assert "+x = 2" in output


def test_delete_file_card_shows_path(tmp_path: Path) -> None:
    console, buffer = _console()
    card = ApprovalCard("delete_file", {"path": "a.txt"}, workspace=tmp_path)
    card.render(console)
    output = buffer.getvalue()
    assert "a.txt" in output
    assert "删除" in output


def test_card_prompt_choices() -> None:
    console, _ = _console()
    card = ApprovalCard("shell", {"command": "ls"}, workspace=Path("/tmp/ws"))
    assert card.prompt(console, input_fn=_choices("1")) == "allow_once"
    assert card.prompt(console, input_fn=_choices("2")) == "allow_session"
    assert card.prompt(console, input_fn=_choices("3")) == "deny"
    assert card.prompt(console, input_fn=_choices("")) == "deny"
    assert card.prompt(console, input_fn=_choices("x")) == "deny"

    def eof(_prompt: str = "") -> str:
        raise EOFError

    assert card.prompt(console, input_fn=eof) == "deny"


def test_allowlist_keys_on_tool_and_workspace(tmp_path: Path) -> None:
    allowlist = SessionAllowlist()
    other = tmp_path / "other"
    allowlist.allow("write_file", tmp_path)
    assert allowlist.is_allowed("write_file", tmp_path)
    assert not allowlist.is_allowed("write_file", other)
    assert not allowlist.is_allowed("read_file", tmp_path)
    assert allowlist


def test_decide_allow_once_prompts_every_time(tmp_path: Path) -> None:
    console, _ = _console()
    controller = ApprovalController(
        workspace=tmp_path, console=console, input_fn=_choices("1", "1")
    )
    assert controller.decide("shell", {"command": "ls"}) is True
    assert controller.decide("shell", {"command": "ls"}) is True
    assert controller.mode == "ask"


def test_decide_allow_session_skips_second_prompt(tmp_path: Path) -> None:
    console, _ = _console()
    calls: list[str] = []

    def input_fn(prompt_text: str = "") -> str:
        calls.append(prompt_text)
        return "2"

    controller = ApprovalController(workspace=tmp_path, console=console, input_fn=input_fn)
    assert controller.decide("write_file", {"path": "a.txt", "content": "x"}) is True
    assert controller.mode == "session"
    assert controller.decide("write_file", {"path": "b.txt", "content": "y"}) is True
    assert len(calls) == 1


def test_decide_deny_returns_false(tmp_path: Path) -> None:
    console, _ = _console()
    controller = ApprovalController(workspace=tmp_path, console=console, input_fn=_choices("3"))
    assert controller.decide("shell", {"command": "rm -rf /"}) is False


def test_decide_auto_mode_never_prompts(tmp_path: Path) -> None:
    console, _ = _console()

    def input_fn(prompt_text: str = "") -> str:
        raise AssertionError("auto 模式不应出卡片")

    controller = ApprovalController(workspace=tmp_path, console=console, input_fn=input_fn)
    controller.mode = "auto"
    assert controller.decide("shell", {"command": "rm -rf /"}) is True


def test_request_auto_requires_explicit_yes(tmp_path: Path) -> None:
    console, _ = _console()
    controller = ApprovalController(workspace=tmp_path, console=console, input_fn=_choices("no"))
    assert controller.request_auto() is False
    assert controller.mode == "ask"

    controller2 = ApprovalController(workspace=tmp_path, console=console, input_fn=_choices("yes"))
    assert controller2.request_auto() is True
    assert controller2.mode == "auto"


def test_exit_auto_restores_session_or_ask(tmp_path: Path) -> None:
    console, _ = _console()
    controller = ApprovalController(workspace=tmp_path, console=console)
    controller.mode = "auto"
    controller.exit_auto()
    assert controller.mode == "ask"
    controller.allowlist.allow("write_file", tmp_path)
    controller.mode = "auto"
    controller.exit_auto()
    assert controller.mode == "session"


def test_make_approve_delegates_to_controller(tmp_path: Path) -> None:
    console, _ = _console()
    controller = ApprovalController(workspace=tmp_path, console=console, input_fn=_choices("1"))
    approve = make_approve(controller)
    assert approve("shell", {"command": "ls"}) is True
