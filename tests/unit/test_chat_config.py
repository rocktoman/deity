"""Phase 1：[chat] 配置段解析（ChatConfig 默认值等于现状行为）。"""

from pathlib import Path

import pytest

from deity.application.config import ChatConfig, load_config


def test_chat_config_defaults_match_current_behavior(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.toml")
    chat = config.chat
    assert chat.enabled is True
    assert chat.history_file == ""
    assert chat.max_history == 500
    assert chat.multiline is True
    assert chat.status_line is True
    assert chat.streaming is True
    assert chat.approval_default == "ask"
    assert chat.mention_max_bytes == 2_000_000
    assert chat.shell_timeout_seconds == 30
    assert chat.shell_max_output_chars == 16_000


def test_chat_config_explicit_values_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[chat]\n"
        "enabled = false\n"
        'history_file = "/tmp/chat_history"\n'
        "max_history = 100\n"
        "multiline = false\n"
        "status_line = false\n"
        "streaming = false\n"
        'approval_default = "deny"\n'
        "mention_max_bytes = 1024\n"
        "shell_timeout_seconds = 5\n"
        "shell_max_output_chars = 1000\n"
    )
    chat = load_config(path).chat
    assert chat.enabled is False
    assert chat.history_file == "/tmp/chat_history"
    assert chat.max_history == 100
    assert chat.multiline is False
    assert chat.status_line is False
    assert chat.streaming is False
    assert chat.approval_default == "deny"
    assert chat.mention_max_bytes == 1024
    assert chat.shell_timeout_seconds == 5
    assert chat.shell_max_output_chars == 1000


def test_chat_config_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[chat]\nunknown_key = true\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_chat_config_invalid_values_are_rejected(tmp_path: Path) -> None:
    for body in (
        '[chat]\nenabled = "yes"\n',
        '[chat]\napproval_default = "always"\n',
        "[chat]\nmax_history = 0\n",
        "[chat]\nmention_max_bytes = 100\n",
        "[chat]\nshell_timeout_seconds = 0\n",
        "[chat]\nshell_max_output_chars = 10\n",
    ):
        path = tmp_path / "config.toml"
        path.write_text(body)
        with pytest.raises(ValueError):
            load_config(path)


def test_chat_config_dataclass_defaults() -> None:
    assert ChatConfig().enabled is True
    assert ChatConfig().approval_default == "ask"
