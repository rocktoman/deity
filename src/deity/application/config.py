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
    max_output_tokens: int = 1024


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
        if set(run) - {"max_turns", "max_tool_calls", "deadline_seconds", "max_output_tokens"}:
            raise ValueError
        if set(permission) - {"shell", "workspace_write"}:
            raise ValueError
        if set(model) - {"provider", "name", "base_url", "api_key_env"}:
            raise ValueError
        values = {
            key: run.get(key, getattr(RunConfig(), key))
            for key in ("max_turns", "max_tool_calls", "deadline_seconds", "max_output_tokens")
        }
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError
        policy = PermissionConfig(**permission)
        if policy.shell not in ("ask", "deny") or policy.workspace_write not in ("ask", "deny"):
            raise ValueError
        model_config = ModelConfig(**model) if model else None
        if model_config is not None and (
            model_config.provider not in ("openai_compatible", "anthropic")
            or not model_config.name
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", model_config.api_key_env) is None
        ):
            raise ValueError
        return Config(run=RunConfig(**values), permission=policy, model=model_config)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ValueError("配置无效：请检查 TOML 字段和权限设置") from None
