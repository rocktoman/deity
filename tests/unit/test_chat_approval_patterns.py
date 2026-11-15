"""V1.1-M4：会话级允许细化到命令模式（shell 按首 token 模式放行）。"""

import io
from pathlib import Path

from rich.console import Console

from deity.interfaces.cli.chat_approval import (
    ApprovalController,
    SessionAllowlist,
    command_pattern_for,
)


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def test_command_pattern_derivation() -> None:
    assert command_pattern_for({"command": "pytest -q tests/"}) == "pytest *"
    assert command_pattern_for({"command": "ls"}) == "ls"
    assert command_pattern_for({"argv": ["git", "status"]}) == "git *"
    assert command_pattern_for({"command": "  "}) is None
    assert command_pattern_for({}) is None


def test_allowlist_pattern_matching_scoped_to_workspace(tmp_path: Path) -> None:
    allowlist = SessionAllowlist()
    allowlist.allow_pattern(tmp_path, "pytest *")
    assert allowlist.is_allowed("shell", tmp_path, {"command": "pytest -q"})
    assert allowlist.is_allowed("shell", tmp_path, {"command": "pytest tests/unit"})
    assert not allowlist.is_allowed("shell", tmp_path, {"command": "rm -rf build"})
    assert not allowlist.is_allowed("shell", tmp_path, {"command": "pytest"})
    other = tmp_path / "other"
    other.mkdir()
    assert not allowlist.is_allowed("shell", other, {"command": "pytest -q"})
    assert allowlist.patterns(tmp_path) == ("pytest *",)
    assert len(allowlist) == 1


def test_decide_allow_session_records_pattern_not_whole_shell(tmp_path: Path) -> None:
    prompts: list[str] = []
    answers = iter(["2"])

    def input_fn(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    controller = ApprovalController(workspace=tmp_path, console=_console(), input_fn=input_fn)
    assert controller.decide("shell", {"command": "pytest -q"}) is True
    assert controller.allowlist.patterns(tmp_path) == ("pytest *",)
    # 同模式命令直接放行，不再出卡片（input_fn 不应被再次调用）。
    assert controller.decide("shell", {"command": "pytest tests/unit"}) is True
    # 不同命令仍需审批：input_fn 已耗尽会抛 StopIteration，故用拒绝验证走到了卡片。
    controller2 = ApprovalController(
        workspace=tmp_path, console=_console(), input_fn=lambda prompt="": "3"
    )
    controller2.allowlist.allow_pattern(tmp_path, "pytest *")
    assert controller2.decide("shell", {"command": "make build"}) is False


def test_decide_allow_session_non_shell_stays_tool_level(tmp_path: Path) -> None:
    controller = ApprovalController(
        workspace=tmp_path, console=_console(), input_fn=lambda prompt="": "2"
    )
    assert controller.decide("write_file", {"path": "a.txt", "content": "x"}) is True
    assert controller.allowlist.patterns(tmp_path) == ()
    assert controller.allowlist.is_allowed("write_file", tmp_path)
    # write_file 的工具级允许不影响 shell。
    assert not controller.allowlist.is_allowed("shell", tmp_path, {"command": "pytest -q"})
