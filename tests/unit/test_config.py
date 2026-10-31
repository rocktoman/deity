from pathlib import Path

import pytest

from deity.application.config import load_config


def test_default_config_is_safe(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.toml")
    assert config.permission.shell == "ask"
    assert config.permission.workspace_write == "ask"
    assert config.run.max_output_tokens is None


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


def test_explicit_auto_permission_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[permission]\nshell = "auto"\nworkspace_write = "auto"\n')
    config = load_config(path)
    assert config.permission.shell == "auto"
    assert config.permission.workspace_write == "auto"


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


def test_reliable_runtime_config_requires_explicit_window(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nprovider="openai_compatible"\nname="m"\napi_key_env="MODEL_KEY"\n'
        "context_window_tokens=8192\n[run]\ncompaction_enabled=true\nresume_enabled=true\n"
    )
    config = load_config(path)
    assert config.model is not None and config.model.context_window_tokens == 8192
    assert config.run.compaction_enabled and config.run.resume_enabled

    path.write_text(
        '[model]\nprovider="openai_compatible"\nname="m"\napi_key_env="MODEL_KEY"\n'
        "[run]\ncompaction_enabled=true\n"
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_v03_new_run_enables_recovery_and_configured_compaction(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nprovider="openai_compatible"\nname="m"\napi_key_env="MODEL_KEY"\n'
        "context_window_tokens=8192\n"
    )
    config = load_config(path)
    assert config.run.resume_enabled is True
    assert config.run.compaction_enabled is True
