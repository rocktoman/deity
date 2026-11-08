"""S3 doctor：真实检查与脱敏，输出绝不含密钥。"""

from pathlib import Path

import pytest

from deity.application.doctor import DoctorReport, Redactor, run_doctor


def test_redactor_masks_key_values_and_token_patterns() -> None:
    text = 'api_key="sk-abcdefgh12345678" token=ghp_abcdefghij1234567890 password: hunter2'
    redacted = Redactor.redact(text)
    assert "sk-abcdefgh" not in redacted
    assert "ghp_abcdefghij" not in redacted
    assert "hunter2" not in redacted
    assert redacted.count("***") >= 3


def test_redactor_leaves_normal_text_alone() -> None:
    assert Redactor.redact("模型 deepseek-v4，无密钥") == "模型 deepseek-v4，无密钥"


def test_doctor_clean_workspace_has_no_fail(tmp_path: Path) -> None:
    report = run_doctor(tmp_path / "absent.toml", tmp_path)
    assert isinstance(report, DoctorReport)
    assert not report.failed
    names = {check.name for check in report.checks}
    assert {"python", "dependencies", "sqlite", "git", "rg", "model", "docker", "config"} <= names


def test_doctor_bad_toml_is_fail(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[run]\nmax_turns = -1\n", encoding="utf-8")
    report = run_doctor(config, tmp_path)
    assert report.failed
    config_check = next(check for check in report.checks if check.name == "config")
    assert config_check.status == "fail"


def test_doctor_model_env_presence_only_no_value_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[model]\nprovider="openai_compatible"\nname="m"\napi_key_env="DEITY_TEST_FAKE_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DEITY_TEST_FAKE_KEY", "fake-secret-value-123")
    report = run_doctor(config, tmp_path)
    model_check = next(check for check in report.checks if check.name == "model")
    assert model_check.status == "ok"
    assert "fake-secret-value-123" not in report.to_text()
    monkeypatch.delenv("DEITY_TEST_FAKE_KEY")
    report = run_doctor(config, tmp_path)
    model_check = next(check for check in report.checks if check.name == "model")
    assert model_check.status == "warn"


def test_doctor_sqlite_unwritable_is_fail(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x", encoding="utf-8")
    report = run_doctor(tmp_path / "absent.toml", not_a_dir)
    sqlite_check = next(check for check in report.checks if check.name == "sqlite")
    assert sqlite_check.status == "fail"


def test_doctor_report_text_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEITY_TEST_FAKE_KEY", "sk-abcdefgh12345678")
    config = tmp_path / "config.toml"
    config.write_text(
        '[model]\nprovider="openai_compatible"\nname="m"\napi_key_env="DEITY_TEST_FAKE_KEY"\n',
        encoding="utf-8",
    )
    report = run_doctor(config, tmp_path)
    assert "sk-abcdefgh12345678" not in report.to_text()
