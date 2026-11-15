"""V1.1-M1：deity init 生成最小可用配置；不写密钥值，不默认覆盖已有配置。"""

from pathlib import Path

from typer.testing import CliRunner

from deity.application.config import load_config
from deity.interfaces.cli.main import app as cli_app


def test_init_defaults_to_qwen_omni_flash(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    result = CliRunner().invoke(cli_app, ["init", "--config", str(target)])
    assert result.exit_code == 0, result.output
    loaded = load_config(target)
    assert loaded.model is not None
    assert loaded.model.provider == "qwen"
    assert loaded.model.name == "qwen3.8-omni-flash"
    assert loaded.model.api_key_env == "DASHSCOPE_API_KEY"
    assert loaded.model.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert "sk-" not in target.read_text(encoding="utf-8")


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("version = 1\n", encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["init", "--config", str(target)])
    assert result.exit_code == 2
    assert target.read_text(encoding="utf-8") == "version = 1\n"
    forced = CliRunner().invoke(cli_app, ["init", "--config", str(target), "--force"])
    assert forced.exit_code == 0, forced.output
    assert load_config(target).model is not None


def test_init_unknown_provider_fails(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    result = CliRunner().invoke(cli_app, ["init", "--config", str(target), "--provider", "nope"])
    assert result.exit_code == 2
    assert not target.exists()
