"""S3 配置优先级：CLI > project > user > default，冲突注明来源。"""

from pathlib import Path

import pytest

from deity.application.config import ConfigResolver


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_cli_beats_project_beats_user_beats_default(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", "[run]\nmax_turns = 11\n")
    project = _write(tmp_path / "project.toml", "[run]\nmax_turns = 22\n")
    config, sources = ConfigResolver.resolve({}, project, user)
    assert config.run.max_turns == 22
    assert sources["run"] == "project"
    config, sources = ConfigResolver.resolve({"run": {"max_turns": 33}}, project, user)
    assert config.run.max_turns == 33
    assert sources["run"] == "cli"
    config, sources = ConfigResolver.resolve({}, tmp_path / "absent.toml", user)
    assert config.run.max_turns == 11
    assert sources["run"] == "user"
    config, sources = ConfigResolver.resolve(
        {}, tmp_path / "absent.toml", tmp_path / "absent2.toml"
    )
    assert config.run.max_turns == 30
    assert sources["run"] == "default"


def test_tui_and_doctor_sections_resolve_with_source(tmp_path: Path) -> None:
    project = _write(tmp_path / "project.toml", "[tui]\nenabled = true\nmax_events_buffer = 500\n")
    user = _write(tmp_path / "user.toml", "[doctor]\ncheck_docker = true\n")
    config, sources = ConfigResolver.resolve({}, project, user)
    assert config.tui.enabled is True
    assert config.tui.max_events_buffer == 500
    assert config.doctor.check_docker is True
    assert sources["tui"] == "project"
    assert sources["doctor"] == "user"


def test_invalid_tui_value_rejected(tmp_path: Path) -> None:
    project = _write(tmp_path / "project.toml", "[tui]\nmax_events_buffer = -1\n")
    with pytest.raises(ValueError):
        ConfigResolver.resolve({}, project, tmp_path / "absent.toml")


def test_invalid_config_error_leaks_no_secret(tmp_path: Path) -> None:
    project = _write(
        tmp_path / "project.toml",
        '[run]\nmax_turns = -1\n[model]\napi_key = "secret-marker"\n',
    )
    with pytest.raises(ValueError) as error:
        ConfigResolver.resolve({}, project, tmp_path / "absent.toml")
    assert "secret-marker" not in str(error.value)


def test_cli_override_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ConfigResolver.resolve(
            {"tui": {"no_such_key": True}},
            tmp_path / "absent.toml",
            tmp_path / "absent2.toml",
        )


def test_config_version_must_be_one(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", "version = 2\n")
    with pytest.raises(ValueError):
        ConfigResolver.resolve({}, tmp_path / "absent.toml", user)
