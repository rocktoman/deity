"""Load and validate local Deity configuration without exposing secrets."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class PermissionConfig:
    shell: Literal["ask", "deny"] = "ask"
    workspace_write: Literal["ask", "deny"] = "ask"


@dataclass(frozen=True)
class RunConfig:
    max_turns: int = 30
    max_tool_calls: int = 100
    deadline_seconds: int = 900
    max_output_tokens: int | None = None
    compaction_enabled: bool = False
    resume_enabled: bool = True
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None


@dataclass(frozen=True)
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    model: "ModelConfig | None" = None


@dataclass(frozen=True)
class ModelConfig:
    provider: Literal["openai_compatible", "anthropic"]
    name: str
    api_key_env: str
    base_url: str | None = None
    context_window_tokens: int | None = None


def load_config(path: Path) -> Config:
    """Read a TOML config; reject invalid values with secret-free errors."""
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {"run", "permission", "model"}:
            raise ValueError
        run = data.get("run", {})
        permission = data.get("permission", {})
        model = data.get("model", {})
        if not all(isinstance(item, dict) for item in (run, permission, model)):
            raise ValueError
        if set(run) - {
            "max_turns",
            "max_tool_calls",
            "deadline_seconds",
            "max_output_tokens",
            "compaction_enabled",
            "resume_enabled",
            "max_input_tokens_total",
            "max_output_tokens_total",
        }:
            raise ValueError
        if set(permission) - {"shell", "workspace_write"}:
            raise ValueError
        if set(model) - {
            "provider",
            "name",
            "base_url",
            "api_key_env",
            "context_window_tokens",
        }:
            raise ValueError
        values = {
            key: run.get(key, getattr(RunConfig(), key))
            for key in ("max_turns", "max_tool_calls", "deadline_seconds")
        }
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError
        max_output_tokens = run.get("max_output_tokens")
        if max_output_tokens is not None and (
            type(max_output_tokens) is not int or max_output_tokens <= 0
        ):
            raise ValueError
        flags = {
            key: run.get(key, getattr(RunConfig(), key))
            for key in ("compaction_enabled", "resume_enabled")
        }
        if any(type(value) is not bool for value in flags.values()):
            raise ValueError
        token_limits = {
            key: run.get(key) for key in ("max_input_tokens_total", "max_output_tokens_total")
        }
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in token_limits.values()
        ):
            raise ValueError
        policy = PermissionConfig(**permission)
        if policy.shell not in ("ask", "deny") or policy.workspace_write not in ("ask", "deny"):
            raise ValueError
        model_config = ModelConfig(**model) if model else None
        if model_config is not None and (
            model_config.provider not in ("openai_compatible", "anthropic")
            or not model_config.name
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", model_config.api_key_env) is None
            or (
                model_config.context_window_tokens is not None
                and (
                    type(model_config.context_window_tokens) is not int
                    or model_config.context_window_tokens <= 0
                )
            )
        ):
            raise ValueError
        if "compaction_enabled" not in run and model_config is not None:
            flags["compaction_enabled"] = model_config.context_window_tokens is not None
        if flags["compaction_enabled"] and (
            model_config is None or model_config.context_window_tokens is None
        ):
            raise ValueError
        return Config(
            run=RunConfig(**values, max_output_tokens=max_output_tokens, **flags, **token_limits),
            permission=policy,
            model=model_config,
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ValueError("配置无效：请检查 TOML 字段和权限设置") from None
