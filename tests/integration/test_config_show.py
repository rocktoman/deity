"""V1.1-M4：deity config show 分层展示（来源标注、密钥只查存在性）。"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deity.application.config import Config, load_config
from deity.interfaces.cli.config_show import format_config_show
from deity.interfaces.cli.main import app as cli_app


def test_format_config_show_labels_layers_and_hides_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'version = 1\n\n[model]\nprovider = "qwen"\nname = "qwen3.8-omni-flash"\n'
        'api_key_env = "DASHSCOPE_API_KEY"\n',
        encoding="utf-8",
    )
    config = load_config(config_file)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-appear-in-output")
    text = format_config_show(config, {"model": "user", "chat": "project"})
    assert "[model]（来源：用户）" in text
    assert "provider = qwen" in text
    assert "qwen3.8-omni-flash" in text
    assert "[chat]（来源：项目）" in text
    assert "[run]（来源：默认）" in text
    assert "已设置" in text
    assert "must-not-appear-in-output" not in text


def test_format_config_show_without_model(tmp_path: Path) -> None:
    text = format_config_show(Config(), {})
    assert "[model]（来源：默认）" in text
    assert "（未配置）" in text
    assert "密钥环境变量" not in text


def test_config_show_cli_reads_user_and_project_layers(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".deity").mkdir(parents=True)
    (workspace / ".deity" / "config.toml").write_text(
        "version = 1\n\n[chat]\nstreaming = false\n", encoding="utf-8"
    )
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        'version = 1\n\n[model]\nprovider = "qwen"\nname = "qwen3.8-omni-flash"\n'
        'api_key_env = "DASHSCOPE_API_KEY"\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli_app,
        ["config", "show", "--config", str(user_config), "--workspace", str(workspace)],
    )
    assert result.exit_code == 0, result.output
    assert "[model]（来源：用户）" in result.output
    assert "[chat]（来源：项目）" in result.output
    assert "streaming = false" in result.output
