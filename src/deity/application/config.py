"""Load and validate local Deity configuration without exposing secrets."""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class PermissionConfig:
    shell: Literal["ask", "auto", "deny"] = "ask"
    workspace_write: Literal["ask", "auto", "deny"] = "ask"


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
    extensions: "ExtensionConfig" = field(default_factory=lambda: ExtensionConfig())
    mcp_servers: tuple["McpServerSettings", ...] = ()
    hook_specs: tuple["HookSpec", ...] = ()


@dataclass(frozen=True)
class ExtensionConfig:
    version: int = 1
    mcp_enabled: bool = False
    skills_enabled: bool = False
    hooks_enabled: bool = False

    def fingerprint(self) -> str:
        """Hash recovery-relevant extension settings without secrets."""
        payload = {
            "version": self.version,
            "mcp_enabled": self.mcp_enabled,
            "skills_enabled": self.skills_enabled,
            "hooks_enabled": self.hooks_enabled,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class McpServerSettings:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str = "."
    env_names: tuple[str, ...] = ()
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class HookSpec:
    name: str
    point: Literal["BeforeModel", "BeforeTool", "AfterTool", "BeforeFinish"]
    kind: Literal["observer", "policy"]
    callable_path: str
    priority: int = 0


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
        if not isinstance(data, dict) or set(data) - {
            "run",
            "permission",
            "model",
            "extensions",
            "mcp",
            "hooks",
        }:
            raise ValueError
        run = data.get("run", {})
        permission = data.get("permission", {})
        model = data.get("model", {})
        extensions = data.get("extensions", {})
        mcp = data.get("mcp", {})
        hooks = data.get("hooks", {})
        if not all(
            isinstance(item, dict) for item in (run, permission, model, extensions, mcp, hooks)
        ):
            raise ValueError
        if set(hooks) - {"registered"} or not isinstance(hooks.get("registered", []), list):
            raise ValueError
        hook_specs: list[HookSpec] = []
        for item in hooks.get("registered", []):
            if not isinstance(item, dict) or set(item) != {
                "name",
                "point",
                "kind",
                "callable",
                "priority",
            }:
                raise ValueError
            if (
                not isinstance(item["name"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["name"]) is None
                or item["point"] not in {"BeforeModel", "BeforeTool", "AfterTool", "BeforeFinish"}
                or item["kind"] not in {"observer", "policy"}
                or (item["point"] == "AfterTool" and item["kind"] == "policy")
                or not isinstance(item["callable"], str)
                or re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_][\w]*", item["callable"]) is None
                or type(item["priority"]) is not int
                or abs(item["priority"]) > 1000
            ):
                raise ValueError
            hook_specs.append(
                HookSpec(
                    item["name"], item["point"], item["kind"], item["callable"], item["priority"]
                )
            )
        if set(mcp) - {"servers"} or not isinstance(mcp.get("servers", {}), dict):
            raise ValueError
        if len(mcp.get("servers", {})) > 8:
            raise ValueError
        servers: list[McpServerSettings] = []
        for name, item in mcp.get("servers", {}).items():
            if not isinstance(item, dict) or set(item) - {
                "command",
                "args",
                "cwd",
                "env_names",
                "timeout_seconds",
            }:
                raise ValueError
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
                raise ValueError
            command = item.get("command")
            args = item.get("args", [])
            cwd = item.get("cwd", ".")
            env_names = item.get("env_names", [])
            timeout = item.get("timeout_seconds", 15.0)
            if (
                not isinstance(command, str)
                or not command
                or len(command) > 4096
                or not isinstance(args, list)
                or len(args) > 32
                or not all(isinstance(x, str) for x in args)
                or any(len(x) > 4096 for x in args)
                or not isinstance(cwd, str)
                or not cwd
                or len(cwd) > 4096
                or not isinstance(env_names, list)
                or len(env_names) > 16
                or not all(
                    isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", x) for x in env_names
                )
                or type(timeout) not in (int, float)
                or timeout <= 0
                or timeout > 120
            ):
                raise ValueError
            servers.append(
                McpServerSettings(name, command, tuple(args), cwd, tuple(env_names), float(timeout))
            )
        if set(extensions) - {"version", "mcp_enabled", "skills_enabled", "hooks_enabled"}:
            raise ValueError
        if extensions.get("version", 1) != 1 or any(
            type(extensions.get(key, False)) is not bool
            for key in ("mcp_enabled", "skills_enabled", "hooks_enabled")
        ):
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
        if policy.shell not in ("ask", "auto", "deny") or policy.workspace_write not in (
            "ask",
            "auto",
            "deny",
        ):
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
            extensions=ExtensionConfig(**extensions),
            mcp_servers=tuple(servers),
            hook_specs=tuple(hook_specs),
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        raise ValueError("配置无效：请检查 TOML 字段和权限设置") from None
