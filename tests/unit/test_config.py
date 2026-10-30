from pathlib import Path

import pytest

from deity.application.config import load_config


def test_default_config_is_safe(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.toml")
    assert config.permission.shell == "ask"
    assert config.permission.workspace_write == "ask"


def test_invalid_config_rejected_without_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[run]\nmax_turns = -1\n[model]\napi_key = "secret-marker"\n')
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert "secret-marker" not in str(error.value)


def test_project_config_cannot_relax_permission(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[permission]\nshell = "allow"\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_deepseek_environment_name_and_output_budget(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nprovider="openai_compatible"\nname="deepseek-v4-flash"\n'
        'base_url="https://api.deepseek.com"\napi_key_env="DEEPSEEK_API_KEY"\n'
        "[run]\nmax_turns=2\nmax_output_tokens=256\n"
    )
    config = load_config(path)
    assert config.model is not None
    assert config.model.api_key_env == "DEEPSEEK_API_KEY"
    assert config.run.max_output_tokens == 256
