"""S3 doctor CLI：干净工作区退出码 0，坏配置退出码 1。"""

from pathlib import Path

from typer.testing import CliRunner

from deity.interfaces.cli.main import app as cli_app


def test_doctor_cli_clean_workspace_exits_zero(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["doctor", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "python" in result.output
    assert "sk-" not in result.output


def test_doctor_cli_bad_config_exits_one(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[run]\nmax_turns = -1\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli_app, ["doctor", "--config", str(config), "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "fail" in result.output


def test_tui_command_disabled_when_configured_off(tmp_path: Path) -> None:
    """ADR-022：工作台默认启用；显式 [tui] enabled=false 时退出码 2。"""
    config = tmp_path / "config.toml"
    config.write_text("[tui]\nenabled = false\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli_app, ["tui", "--config", str(config), "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "tui" in result.output.lower()
