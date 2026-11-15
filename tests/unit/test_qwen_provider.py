"""V1.1-M1：Qwen（DashScope OpenAI 兼容端点）作为一等厂商的配置与组装测试。"""

from pathlib import Path

import pytest

from deity.adapters.model.openai_compatible import OpenAICompatibleProvider
from deity.application.config import (
    QWEN_DEFAULT_BASE_URL,
    QWEN_DEFAULT_MODEL,
    load_config,
)
from deity.application.run_service import RunService


def _write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return config


def test_qwen_config_loads_with_defaults_from_init_shape(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        '[model]\nprovider="qwen"\nname="qwen3.8-omni-flash"\napi_key_env="DASHSCOPE_API_KEY"\n',
    )
    loaded = load_config(config)
    assert loaded.model is not None
    assert loaded.model.provider == "qwen"
    assert loaded.model.name == QWEN_DEFAULT_MODEL
    assert loaded.model.base_url is None


def test_qwen_from_config_builds_openai_compatible_provider_with_dashscope_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    config = _write_config(
        tmp_path,
        '[model]\nprovider="qwen"\nname="qwen3.8-omni-flash"\napi_key_env="DASHSCOPE_API_KEY"\n',
    )
    service = RunService.from_config(
        load_config(config),
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda name, args: False,
    )
    assert isinstance(service.model, OpenAICompatibleProvider)
    assert service.model.model == QWEN_DEFAULT_MODEL
    assert str(service.model.client.base_url).rstrip("/") == QWEN_DEFAULT_BASE_URL


def test_qwen_from_config_respects_explicit_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    config = _write_config(
        tmp_path,
        '[model]\nprovider="qwen"\nname="qwen3.8-omni-flash"\n'
        'base_url="http://127.0.0.1:9999/v1"\napi_key_env="DASHSCOPE_API_KEY"\n',
    )
    service = RunService.from_config(
        load_config(config),
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        approve=lambda name, args: False,
    )
    assert isinstance(service.model, OpenAICompatibleProvider)
    assert str(service.model.client.base_url).rstrip("/") == "http://127.0.0.1:9999/v1"
