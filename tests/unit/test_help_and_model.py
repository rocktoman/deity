"""`/help` 一行一命令与 `/model` 可选模型列表。"""

from deity.interfaces.cli.chat_commands import (
    COMMANDS,
    HELP_TEXT,
    model_listing_lines,
)


def test_help_text_lists_one_command_per_line() -> None:
    lines = [line for line in HELP_TEXT.split("\n") if line]
    assert len(lines) == len(COMMANDS)
    assert all(line.startswith("/") for line in lines)
    assert "；" not in HELP_TEXT
    assert lines[0].startswith("/help")
    assert any(line.startswith("/model") for line in lines)


def test_model_listing_shows_qwen_catalog_with_current_marked() -> None:
    lines = model_listing_lines("qwen4-max", "qwen")
    assert lines[0] == "当前模型：qwen4-max（提供方 qwen）"
    assert "可选模型（qwen）：" in lines
    assert "  ● qwen4-max（当前）" in lines
    assert "    qwen3.8-omni-flash" in lines
    assert lines[-1] == "/model <名称> 切换（仅本次会话内生效）"


def test_model_listing_for_openai_compatible_has_no_catalog() -> None:
    lines = model_listing_lines("deepseek-v4-flash", "openai_compatible")
    assert lines[0].startswith("当前模型：deepseek-v4-flash")
    assert any("无固定模型列表" in line for line in lines)
